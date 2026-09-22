import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.activity import ActivityLog
from agent_sparring.providers import ProviderError
from agent_sparring.providers import codex_cli
from agent_sparring.providers.codex_cli import CodexCliAdapter


def _fake_result(returncode: int, stdout: str, stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _sandbox_config_value(args: list[str]) -> str:
    """The value passed via ``-c sandbox_mode="..."`` in ``args``.

    Real ``codex exec resume`` rejects the top-level ``--sandbox`` flag
    outright (verified live) and, without any override, does not even
    inherit the original session's read-only sandbox -- a real write
    succeeded during a live smoke test. ``-c sandbox_mode=...`` is the
    fix, verified live to work for both ``codex exec`` and
    ``codex exec resume``, so the adapter always uses it instead of
    ``--sandbox``.
    """

    idx = args.index("-c")
    raw = args[idx + 1]
    assert raw.startswith("sandbox_mode="), raw
    return raw[len("sandbox_mode=") :].strip('"')


class CodexCliAdapterStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def _make_runner(self, stdout: str, *, returncode: int = 0, write_output: str | None = None):
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            captured["cwd"] = cwd
            if write_output is not None:
                # -o/--output-last-message is always the argument right
                # after "-o" in the real CLI; find it the same way.
                idx = args.index("-o")
                Path(args[idx + 1]).write_text(write_output, encoding="utf-8")
            return _fake_result(returncode, stdout)

        return runner, captured

    def test_start_builds_expected_args_and_parses_session_id(self):
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "thread-abc"},
            {"type": "turn.started"},
            {"type": "turn.completed"},
        )
        verdict = json.dumps({"action": "READY", "summary": "looks good", "needs_you_reason": None})
        runner, captured = self._make_runner(stdout, write_output=verdict)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.start("spar on this")

        self.assertEqual(result.session_id, "thread-abc")
        self.assertEqual(result.text, verdict)
        self.assertFalse(result.is_error)
        self.assertEqual(captured["cwd"], self.repo_root)

        args = captured["args"]
        self.assertEqual(args[0], "codex")
        self.assertEqual(args[1], "exec")
        self.assertNotIn("--sandbox", args)  # rejected by real "codex exec resume"
        self.assertEqual(_sandbox_config_value(args), "read-only")
        self.assertIn("--json", args)
        self.assertIn("--output-schema", args)
        self.assertIn("-o", args)
        self.assertNotIn("resume", args)
        self.assertEqual(args[-1], "spar on this")

    def test_start_writes_a_valid_json_schema_file(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})
        verdict = json.dumps({"action": "READY", "summary": "ok", "needs_you_reason": None})
        captured_schema = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            # Read the schema file inside the runner: the adapter's temp
            # directory is cleaned up once start() returns.
            schema_path = Path(args[args.index("--output-schema") + 1])
            captured_schema["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
            idx = args.index("-o")
            Path(args[idx + 1]).write_text(verdict, encoding="utf-8")
            return _fake_result(0, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.start("hello")

        schema = captured_schema["schema"]
        # Codex's strict-schema requirement (verified against a real
        # install): every property must appear in "required".
        self.assertEqual(set(schema["required"]), set(schema["properties"].keys()))
        self.assertEqual(
            schema["properties"]["action"]["enum"],
            ["SEND_BACK", "READY", "NEEDS_YOU", "ESCALATE"],
        )
        # Detailed human-readable prose lives in "findings"/"deferred",
        # separate from the tiny routing verdict fields.
        self.assertIn("findings", schema["properties"])
        self.assertIn("deferred", schema["properties"])

    def test_model_is_configurable(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})
        runner, captured = self._make_runner(stdout, write_output="{}")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner, model="o3")
        adapter.start("hello")

        args = captured["args"]
        self.assertIn("--model", args)
        self.assertIn("o3", args)

    def test_default_sandbox_is_read_only(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})
        runner, captured = self._make_runner(stdout, write_output="{}")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.start("hello")

        args = captured["args"]
        self.assertNotIn("--sandbox", args)
        self.assertEqual(_sandbox_config_value(args), "read-only")

    def test_sandbox_is_not_a_supported_constructor_parameter(self):
        # Stage 4 finding: read-only must be an invariant, not a
        # configuration option -- there must be no "sandbox" constructor
        # field to override at all (not even one that validates and
        # rejects unsafe values). Passing it must fail as an unknown
        # keyword argument, exactly like any other nonexistent parameter.
        with self.assertRaises(TypeError):
            CodexCliAdapter(
                repo_root=self.repo_root, runner=lambda *a, **k: None, sandbox="workspace-write"
            )

    def test_extra_args_is_not_a_supported_constructor_parameter(self):
        # Stage 4 finding: extra_args could inject an arbitrary Codex/config
        # flag (including a conflicting sandbox override) after the fixed
        # read-only config arg, so this adapter exposes no such passthrough.
        with self.assertRaises(TypeError):
            CodexCliAdapter(
                repo_root=self.repo_root,
                runner=lambda *a, **k: None,
                extra_args=("-c", 'sandbox_mode="workspace-write"'),
            )

    def test_mutating_sandbox_attribute_after_construction_has_no_effect(self):
        # Even if a caller sets an attribute of this name directly on the
        # instance (Python does not prevent arbitrary attribute
        # assignment), there is no runtime path by which it changes what
        # is actually invoked: _build_args never reads it.
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})
        runner, captured = self._make_runner(stdout, write_output="{}")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.sandbox = "workspace-write"  # not a real field; has no effect
        adapter.start("hello")

        args = captured["args"]
        self.assertNotIn("--sandbox", args)
        self.assertEqual(_sandbox_config_value(args), "read-only")


class CodexCliAdapterResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_resume_passes_session_id_and_returns_same_id(self):
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "thread-abc"},
            {"type": "turn.completed"},
        )

        def runner(args, cwd, timeout_seconds, on_line=None):
            idx = args.index("-o")
            Path(args[idx + 1]).write_text(
                json.dumps({"action": "READY", "summary": "ok", "needs_you_reason": None}),
                encoding="utf-8",
            )
            return _fake_result(0, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.resume("thread-abc", "keep going")

        self.assertEqual(result.session_id, "thread-abc")

    def test_resume_args_include_resume_subcommand_and_session_id(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "thread-abc"}, {"type": "turn.completed"})
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            idx = args.index("-o")
            Path(args[idx + 1]).write_text(
                json.dumps({"action": "READY", "summary": "ok", "needs_you_reason": None}),
                encoding="utf-8",
            )
            return _fake_result(0, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.resume("thread-abc", "keep going")

        args = captured["args"]
        self.assertEqual(args[:2], ["codex", "exec"])
        self.assertEqual(args[2:4], ["resume", "thread-abc"])
        # Stage 4 finding from a live smoke test: "codex exec resume"
        # rejects the top-level --sandbox flag outright, and does not
        # inherit the original session's read-only sandbox on its own --
        # a real write succeeded without this. -c sandbox_mode=... is the
        # verified fix, and must be present here too.
        self.assertNotIn("--sandbox", args)
        self.assertEqual(_sandbox_config_value(args), "read-only")

    def test_resume_rejects_empty_session_id_without_invoking_runner(self):
        called = []

        def runner(args, cwd, timeout_seconds, on_line=None):
            called.append(True)
            return _fake_result(0, "")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.resume("", "keep going")
        self.assertEqual(called, [])

    def test_resume_of_unknown_session_raises_provider_error(self):
        # Verified against a real codex CLI: resuming an unknown thread id
        # exits non-zero and prints a plain-text error on stderr, no JSONL.
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(
                1, "", "Error: thread/resume: thread/resume failed: no rollout found"
            )

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError) as ctx:
            adapter.resume("00000000-0000-0000-0000-000000000000", "keep going")
        self.assertIn("no rollout found", str(ctx.exception))


class CodexCliAdapterParsingFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_no_thread_started_event_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(0, "not json at all\n")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_turn_failed_event_raises_provider_error_with_message(self):
        # Verified against a real codex CLI: an invalid --output-schema (or
        # any turn failure) still emits thread.started, then turn.failed.
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {
                "type": "turn.failed",
                "error": {"message": "invalid_json_schema: missing 'needs_you_reason'"},
            },
        )

        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(1, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError) as ctx:
            adapter.start("hi")
        self.assertIn("invalid_json_schema", str(ctx.exception))

    def test_missing_output_file_raises_provider_error(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})

        def runner(args, cwd, timeout_seconds, on_line=None):
            # Deliberately do not write the -o output file.
            return _fake_result(0, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_empty_output_file_raises_provider_error(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})

        def runner(args, cwd, timeout_seconds, on_line=None):
            idx = args.index("-o")
            Path(args[idx + 1]).write_text("", encoding="utf-8")
            return _fake_result(0, stdout)

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_launch_failure_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            raise FileNotFoundError("no such executable")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_nonzero_exit_without_turn_failed_still_raises(self):
        stdout = _jsonl({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.completed"})

        def runner(args, cwd, timeout_seconds, on_line=None):
            idx = args.index("-o")
            Path(args[idx + 1]).write_text(
                json.dumps({"action": "READY", "summary": "ok", "needs_you_reason": None}),
                encoding="utf-8",
            )
            return _fake_result(1, stdout, "some unrelated non-zero exit")

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")



def _read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


_REASONING = "REASONING-TEXT-MUST-NOT-LEAK"
_AGENT_MESSAGE = "AGENT-MESSAGE-MUST-NOT-LEAK"
_COMMAND = "git diff --stat COMMAND-TEXT-MUST-NOT-LEAK"
_OUTPUT = "AGGREGATED-OUTPUT-MUST-NOT-LEAK"
_PROMPT = "PROMPT-TEXT-MUST-NOT-LEAK"
_MCP_ARGS = {"query": "MCP-ARGS-MUST-NOT-LEAK"}


def _codex_fixture() -> str:
    return _jsonl(
        {"type": "thread.started", "thread_id": "thread-abc"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "item_0", "type": "reasoning", "text": _REASONING}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": _REASONING}},
        {"type": "item.started", "item": {"id": "item_1", "type": "command_execution",
                                          "command": _COMMAND, "aggregated_output": "",
                                          "status": "in_progress"}},
        {"type": "item.updated", "item": {"id": "item_1", "type": "command_execution",
                                          "command": _COMMAND, "aggregated_output": _OUTPUT,
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                            "command": _COMMAND, "aggregated_output": _OUTPUT,
                                            "exit_code": 0, "status": "completed"}},
        {"type": "item.started", "item": {"id": "item_2", "type": "mcp_tool_call",
                                          "server": "github", "tool": "search_issues",
                                          "arguments": _MCP_ARGS, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call",
                                            "server": "github", "tool": "search_issues",
                                            "arguments": _MCP_ARGS, "result": {"x": _OUTPUT},
                                            "error": None, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "file_change",
                                            "changes": [{"path": "src/a.py", "kind": "update"},
                                                        {"path": "src/b.py", "kind": "add"}],
                                            "status": "completed"}},
        {"type": "item.started", "item": {"id": "item_4", "type": "collab_tool_call",
                                          "tool": "spawn_agent", "sender_thread_id": "thread-abc",
                                          "receiver_thread_ids": ["thread-child"],
                                          "prompt": _PROMPT, "agents_states": {},
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "collab_tool_call",
                                            "tool": "spawn_agent", "sender_thread_id": "thread-abc",
                                            "receiver_thread_ids": ["thread-child"],
                                            "prompt": _PROMPT, "agents_states": {},
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "web_search",
                                            "query": "QUERY-MUST-NOT-LEAK", "action": None}},
        {"type": "item.completed", "item": {"id": "item_6", "type": "todo_list",
                                            "items": [{"text": "TODO-MUST-NOT-LEAK",
                                                       "completed": False}]}},
        {"type": "item.completed", "item": {"id": "item_7", "type": "agent_message",
                                            "text": _AGENT_MESSAGE}},
        {"type": "turn.completed", "usage": {"input_tokens": 24763, "cached_input_tokens": 24448,
                                             "output_tokens": 122, "reasoning_output_tokens": 0}},
    )


class CodexCliAdapterStreamingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.activity_path = self.repo_root / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("sparrer", provider="codex-cli")
        self.verdict = json.dumps(
            {"action": "READY", "summary": "looks good", "needs_you_reason": None,
             "findings": "fine", "deferred": None}
        )

    def _streaming_runner(self, stdout: str, *, returncode: int = 0, write_output=None):
        def runner(args, cwd, timeout_seconds, on_line=None):
            if write_output is not None:
                Path(args[args.index("-o") + 1]).write_text(write_output, encoding="utf-8")
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(returncode, stdout)

        return runner

    def test_streaming_preserves_the_existing_final_result(self):
        adapter = CodexCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_codex_fixture(), write_output=self.verdict),
            activity=self.emitter,
        )
        result = adapter.start(_PROMPT)
        self.assertEqual(result.session_id, "thread-abc")
        self.assertEqual(result.text, self.verdict)
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.raw["events"]), 16)

    def test_translator_emits_semantic_events_only(self):
        CodexCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_codex_fixture(), write_output=self.verdict),
            activity=self.emitter,
        ).start(_PROMPT)

        events = _read_events(self.activity_path)
        self.assertEqual(
            [e["event"] for e in events],
            ["session.observed", "command.started", "command.finished", "tool.call",
             "file.changed", "file.changed", "subagent.started", "tool.call",
             "provider.usage", "provider.result"],
        )
        self.assertEqual(events[0]["session_id"], "thread-abc")
        self.assertEqual(events[0]["provider"], "codex-cli")
        self.assertNotIn("model", events[0])  # Codex never states a model
        self.assertEqual(events[1]["tool"], "shell")
        self.assertEqual((events[2]["tool"], events[2]["exit_code"]), ("shell", 0))
        self.assertEqual(events[3]["tool"], "github:search_issues")
        self.assertEqual((events[4]["path"], events[4]["kind"]), ("src/a.py", "update"))
        self.assertEqual((events[5]["path"], events[5]["kind"]), ("src/b.py", "add"))
        self.assertEqual(events[6]["tool"], "spawn_agent")
        self.assertEqual(events[7]["tool"], "web_search")
        self.assertEqual((events[8]["input_tokens"], events[8]["output_tokens"]), (24763, 122))
        self.assertEqual(events[9]["summary"], "tokens in=24763 out=122")

    def test_forbidden_content_never_reaches_the_activity_log(self):
        CodexCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_codex_fixture(), write_output=self.verdict),
            activity=self.emitter,
        ).start(_PROMPT)

        text = self.activity_path.read_text(encoding="utf-8")
        for secret in (_REASONING, _AGENT_MESSAGE, _COMMAND, _OUTPUT, _PROMPT,
                       "MCP-ARGS", "QUERY-MUST", "TODO-MUST", "thread-child", "looks good"):
            self.assertNotIn(secret, text)
        for key in ("command", "aggregated_output", "arguments", "result", "text",
                    "prompt", "agents_states", "receiver_thread_ids", "usage", "item"):
            self.assertNotIn(f'"{key}"', text)

    def test_turn_failed_emits_provider_error_without_message_text(self):
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.failed", "error": {"message": "ERROR-TEXT-MUST-NOT-LEAK"}},
        )
        adapter = CodexCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(stdout, returncode=1),
            activity=self.emitter,
        )
        with self.assertRaises(ProviderError) as ctx:
            adapter.start("hi")
        # The adapter's own error still carries the message (existing
        # behavior) -- only the activity log does not.
        self.assertIn("ERROR-TEXT-MUST-NOT-LEAK", str(ctx.exception))
        events = _read_events(self.activity_path)
        self.assertEqual([e["event"] for e in events], ["session.observed", "provider.error"])
        self.assertNotIn("ERROR-TEXT", self.activity_path.read_text(encoding="utf-8"))

    def test_completed_only_items_still_yield_one_event(self):
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                                "command": "x", "aggregated_output": "y",
                                                "exit_code": 2, "status": "failed"}},
            {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call",
                                                "server": "s", "tool": "t", "status": "completed"}},
            {"type": "turn.completed"},
        )
        CodexCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(stdout, write_output=self.verdict),
            activity=self.emitter,
        ).start("hi")
        events = _read_events(self.activity_path)
        self.assertEqual([e["event"] for e in events],
                         ["session.observed", "command.finished", "tool.call", "provider.result"])
        self.assertEqual(events[1]["exit_code"], 2)
        self.assertNotIn("summary", events[3])  # no usage: nothing invented

    def test_no_activity_emitter_means_no_telemetry(self):
        adapter = CodexCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_codex_fixture(), write_output=self.verdict),
        )
        result = adapter.start("hi")
        self.assertEqual(result.session_id, "thread-abc")
        self.assertFalse(self.activity_path.exists())



class CodexCliAdapterPathNormalizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name) / "repo"
        self.repo_root.mkdir()
        self.activity_path = Path(self._tmp.name) / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("sparrer", provider="codex-cli")

    def test_file_change_paths_are_repo_relative_or_omitted(self):
        outside = str(Path(self._tmp.name) / "elsewhere" / "x.py")
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"id": "f", "type": "file_change", "changes": [
                {"path": str(self.repo_root / "src" / "a.py"), "kind": "update"},
                {"path": "src/b.py", "kind": "add"},
                {"path": outside, "kind": "add"},
                {"path": "../escape.py", "kind": "delete"},
            ], "status": "completed"}},
            {"type": "turn.completed"},
        )
        verdict = json.dumps({"action": "READY", "summary": "ok", "needs_you_reason": None,
                              "findings": "f", "deferred": None})

        def runner(args, cwd, timeout_seconds, on_line=None):
            Path(args[args.index("-o") + 1]).write_text(verdict, encoding="utf-8")
            for line in stdout.splitlines():
                on_line(line)
            return _fake_result(0, stdout)

        CodexCliAdapter(repo_root=self.repo_root, runner=runner, activity=self.emitter).start("hi")
        changed = [e for e in _read_events(self.activity_path) if e["event"] == "file.changed"]
        self.assertEqual([(e.get("path"), e["kind"]) for e in changed],
                         [("src/a.py", "update"), ("src/b.py", "add"), (None, "add"),
                          (None, "delete")])
        text = self.activity_path.read_text(encoding="utf-8")
        for leak in (str(self.repo_root), "elsewhere", "escape.py", self._tmp.name):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()


class CodexBudgetTelemetryTests(unittest.TestCase):
    """What the provider says about tokens, context and rate limits.

    All of it is a quotation. The panel draws a dial from these numbers, so
    a value that was never reported has to stay absent rather than become a
    zero or a guess -- a context ring filled in from a model name would be
    an invention shown as a measurement.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.activity_path = self.repo_root / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("sparrer", provider="codex-cli")
        self.verdict = json.dumps(
            {"action": "READY", "summary": "looks good", "needs_you_reason": None,
             "findings": "fine", "deferred": None}
        )

    def _run(self, *events, codex_home: Path | None = None) -> list[dict]:
        def runner(args, cwd, timeout_seconds, on_line=None):
            Path(args[args.index("-o") + 1]).write_text(self.verdict, encoding="utf-8")
            stdout = _jsonl(*events)
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(0, stdout)

        CodexCliAdapter(
            repo_root=self.repo_root,
            runner=runner,
            activity=self.emitter,
            codex_home=codex_home,
        ).start(_PROMPT)
        return [e for e in _read_events(self.activity_path) if e["event"] == "provider.usage"]

    def _rollout(self, thread_id: str, *records) -> Path:
        """A Codex session log for ``thread_id``, laid out as Codex lays it out."""

        home = self.repo_root / "codex-home"
        day = home / "sessions" / "2026" / "09" / "22"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-09-22T12-42-24-{thread_id}.jsonl"
        path.write_text(_jsonl(*records), encoding="utf-8")
        return home

    def test_the_cumulative_totals_context_window_and_both_limits_are_recorded(self):
        # The shape a real session reports: cumulative usage with the
        # context window beside it, and the account's two rate-limit
        # windows. This is what every dial needs.
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 135812, "output_tokens": 665,
                                          "total_tokens": 136477},
                    "model_context_window": 258400,
                },
                "rate_limits": {
                    "primary": {"used_percent": 8.0, "window_minutes": 300},
                    "secondary": {"used_percent": 39.0, "window_minutes": 10080},
                },
            },
        )

        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["total_tokens"], 136477)
        self.assertEqual(usage[0]["context_window"], 258400)
        self.assertEqual((usage[0]["rate_limit_percent"], usage[0]["rate_limit_window_minutes"]), (8, 300))
        self.assertEqual(
            (usage[0]["rate_limit_secondary_percent"], usage[0]["rate_limit_secondary_window_minutes"]),
            (39, 10080),
        )

    def test_a_turn_that_reports_only_tokens_leaves_the_rest_absent(self):
        # Older Codex, and the plain `turn.completed` path: tokens and
        # nothing else. The context window must not be invented.
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
        )

        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0]["input_tokens"], usage[0]["output_tokens"]), (100, 7))
        for absent in ("context_window", "rate_limit_percent", "rate_limit_secondary_percent"):
            self.assertNotIn(absent, usage[0])

    def test_cumulative_totals_win_over_one_response_s_usage(self):
        # Both in one event. The context dial is about how full the session
        # is, so the cumulative figure is the one recorded.
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 136477}},
                "usage": {"total_tokens": 44085},
            },
        )

        self.assertEqual(usage[0]["total_tokens"], 136477)

    def test_unchanged_numbers_are_not_re_emitted(self):
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 9}},
        )

        self.assertEqual([e["output_tokens"] for e in usage], [7, 9])

    def test_a_nonsense_value_is_read_as_not_stated(self):
        # Another program's JSON: a string where a count belongs, a
        # percentage out of range, a negative total. None of it becomes a
        # dial, and none of it stops the rest being recorded.
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": "lots", "total_tokens": -1,
                                               "output_tokens": 12}},
                "rate_limits": {"primary": {"used_percent": 400, "window_minutes": 300}},
            },
        )

        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["output_tokens"], 12)
        for absent in ("input_tokens", "total_tokens", "rate_limit_percent"):
            self.assertNotIn(absent, usage[0])
        self.assertEqual(usage[0]["rate_limit_window_minutes"], 300)

    def test_events_that_say_nothing_about_budget_emit_nothing(self):
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "hi"}},
        )

        self.assertEqual(usage, [])

    def test_the_last_request_s_total_is_the_context_occupancy(self):
        # `total_token_usage` grows all session and passes the window
        # several times over, because Codex re-sends the conversation on
        # every request. Only `last_token_usage` is a share of the window.
        usage = self._run(
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": 653353},
                    "last_token_usage": {"total_tokens": 220354},
                    "model_context_window": 258400,
                },
            },
        )

        self.assertEqual(usage[0]["context_used_tokens"], 220354)
        self.assertEqual(usage[0]["total_tokens"], 653353)


class CodexRolloutBudgetTests(unittest.TestCase):
    """The budget facts that exist only in Codex's own session log.

    Verified against codex-cli 0.153.4: `codex exec --json` emits
    `turn.completed` with per-turn token counts and nothing else, while
    the rollout file for the same thread states the context window, the
    last request's occupancy and both rate-limit windows.
    """

    setUp = CodexBudgetTelemetryTests.setUp
    _run = CodexBudgetTelemetryTests._run
    _rollout = CodexBudgetTelemetryTests._rollout

    @staticmethod
    def _token_count_record(**info) -> dict:
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 652332, "output_tokens": 1021,
                                          "total_tokens": 653353},
                    "last_token_usage": {"total_tokens": 220354},
                    "model_context_window": 258400,
                    **info,
                },
                "rate_limits": {
                    "primary": {"used_percent": 34.0, "window_minutes": 300},
                    "secondary": {"used_percent": 59.0, "window_minutes": 10080},
                },
            },
        }

    def test_the_window_and_both_limits_are_read_from_the_session_log(self):
        home = self._rollout(
            "01a0c8b5-ac2b-7083-bd82-9a0472a666a5",
            {"type": "response_item", "payload": {"type": "message", "content": "secret"}},
            self._token_count_record(),
        )

        usage = self._run(
            {"type": "thread.started", "thread_id": "01a0c8b5-ac2b-7083-bd82-9a0472a666a5"},
            {"type": "turn.completed", "usage": {"input_tokens": 652332, "output_tokens": 1021}},
            codex_home=home,
        )

        # The stream's own event first, then what the log adds.
        window = usage[-1]
        self.assertEqual(window["context_window"], 258400)
        self.assertEqual(window["context_used_tokens"], 220354)
        self.assertEqual((window["rate_limit_percent"], window["rate_limit_window_minutes"]), (34, 300))
        self.assertEqual(
            (window["rate_limit_secondary_percent"], window["rate_limit_secondary_window_minutes"]),
            (59, 10080),
        )

    def test_only_the_numbers_are_read_never_the_transcript(self):
        # The same file holds prompts, reasoning and command output. None
        # of it may reach the activity log.
        home = self._rollout(
            "01a0c8b5-ac2b-7083-bd82-9a0472a666a5",
            {"type": "response_item",
             "payload": {"type": "message", "content": "SECRET-TRANSCRIPT"}},
            self._token_count_record(),
        )

        self._run(
            {"type": "thread.started", "thread_id": "01a0c8b5-ac2b-7083-bd82-9a0472a666a5"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            codex_home=home,
        )

        self.assertNotIn("SECRET-TRANSCRIPT", self.activity_path.read_text(encoding="utf-8"))

    def test_the_latest_record_is_the_one_read(self):
        home = self._rollout(
            "01a0c8b5-ac2b-7083-bd82-9a0472a666a5",
            self._token_count_record(last_token_usage={"total_tokens": 1000}),
            self._token_count_record(last_token_usage={"total_tokens": 220354}),
        )

        usage = self._run(
            {"type": "thread.started", "thread_id": "01a0c8b5-ac2b-7083-bd82-9a0472a666a5"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            codex_home=home,
        )

        self.assertEqual(usage[-1]["context_used_tokens"], 220354)

    def test_a_missing_session_log_leaves_the_dials_unreported(self):
        usage = self._run(
            {"type": "thread.started", "thread_id": "01a0c8b5-ac2b-7083-bd82-9a0472a666a5"},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
            codex_home=self.repo_root / "nowhere",
        )

        self.assertEqual(len(usage), 1)
        for absent in ("context_window", "context_used_tokens", "rate_limit_percent"):
            self.assertNotIn(absent, usage[0])

    def test_a_thread_id_that_is_not_an_id_is_never_globbed(self):
        # The id goes into a glob pattern, so anything that could act as
        # one is refused outright rather than matched loosely.
        self.assertIsNone(codex_cli._rollout_path("../*", self.repo_root))
        self.assertIsNone(codex_cli._rollout_path("", self.repo_root))
        self.assertIsNone(codex_cli._rollout_path(None, self.repo_root))
