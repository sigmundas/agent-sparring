import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.activity import ActivityLog
from agent_sparring.providers import ProviderError
from agent_sparring.providers.claude_cli import ClaudeCliAdapter


def _fake_result(returncode: int, stdout: str, stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ClaudeCliAdapterStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_start_builds_expected_args_and_parses_session_id(self):
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            captured["cwd"] = cwd
            return _fake_result(0, '{"session_id": "abc-123", "result": "done", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.start("do the thing")

        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(result.text, "done")
        self.assertFalse(result.is_error)
        self.assertEqual(captured["cwd"], self.repo_root)
        self.assertEqual(
            captured["args"],
            [
                "claude",
                "-p",
                "do the thing",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
            ],
        )

    def test_start_never_passes_resume_flag(self):
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "x", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.start("hello")
        self.assertNotIn("--resume", captured["args"])

    def test_model_and_extra_args_are_included(self):
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "x", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=runner,
            model="opus",
            extra_args=("--add-dir", "/tmp/extra"),
        )
        adapter.start("hello")
        self.assertIn("--model", captured["args"])
        self.assertIn("opus", captured["args"])
        self.assertIn("--add-dir", captured["args"])
        self.assertIn("/tmp/extra", captured["args"])


class ClaudeCliAdapterResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_resume_passes_session_id_and_returns_same_id(self):
        captured = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "abc-123", "result": "ok", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.resume("abc-123", "keep going")

        self.assertEqual(result.session_id, "abc-123")
        self.assertIn("--resume", captured["args"])
        self.assertIn("abc-123", captured["args"])

    def test_resume_rejects_empty_session_id_without_invoking_runner(self):
        called = []

        def runner(args, cwd, timeout_seconds, on_line=None):
            called.append(True)
            return _fake_result(0, "{}")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.resume("", "keep going")
        self.assertEqual(called, [])

    def test_resume_of_unknown_session_raises_provider_error(self):
        # Verified against a real claude CLI: resuming an unknown session id
        # exits non-zero and prints a plain-text error, not JSON.
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(
                1, "No conversation found with session ID: 00000000-0000-0000-0000-000000000000"
            )

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError) as ctx:
            adapter.resume("00000000-0000-0000-0000-000000000000", "keep going")
        self.assertIn("No conversation found", str(ctx.exception))


class ClaudeCliAdapterParsingFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_non_json_output_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(0, "not json at all")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_missing_session_id_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(0, '{"result": "done", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_json_array_output_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(0, "[1, 2, 3]")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_launch_failure_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            raise FileNotFoundError("no such executable")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_is_error_true_is_surfaced_not_raised(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            return _fake_result(0, '{"session_id": "x", "result": "oops", "is_error": true}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.start("hi")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "oops")



def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


_SECRET_PROMPT = "PROMPT-TEXT-MUST-NOT-LEAK"
_SECRET_OLD = "OLD-STRING-MUST-NOT-LEAK"
_SECRET_NEW = "NEW-STRING-MUST-NOT-LEAK"
_SECRET_CMD = "pytest -q --token=COMMAND-TEXT-MUST-NOT-LEAK"
_SECRET_OUT = "TOOL-OUTPUT-MUST-NOT-LEAK 12 passed"


def _stream_fixture(*, with_model: bool = True, session_id: str = "abc-123") -> str:
    init = {"type": "system", "subtype": "init", "session_id": session_id, "cwd": "/repo"}
    if with_model:
        init["model"] = "claude-fable-5-1"
    return _jsonl(
        init,
        {
            "type": "assistant",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me edit."},
                    {
                        "type": "tool_use",
                        "id": "toolu_edit",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/statistics.py",
                            "old_string": _SECRET_OLD,
                            "new_string": _SECRET_NEW,
                        },
                    },
                ],
            },
        },
        {
            "type": "user",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_edit", "content": "ok"}
                ],
            },
        },
        {
            "type": "assistant",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash",
                        "name": "Bash",
                        "input": {"command": _SECRET_CMD, "description": "run tests"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_bash", "content": _SECRET_OUT}
                ],
            },
        },
        {
            "type": "assistant",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_read", "name": "Read",
                     "input": {"file_path": "src/other.py"}}
                ],
                # Shaped as the real CLI reports it: nearly the whole
                # conversation sits in the cache fields, and `input_tokens`
                # is only the newest slice.
                "usage": {
                    "input_tokens": 9,
                    "cache_read_input_tokens": 120_000,
                    "cache_creation_input_tokens": 30_000,
                    "output_tokens": 35,
                },
            },
        },
        {"type": "stream_event", "session_id": session_id,
         "event": {"type": "content_block_delta", "delta": {"text": "partial"}}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": session_id,
            "num_turns": 4,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {
                "claude-fable-5-1": {"contextWindow": 200_000, "canonicalModel": "claude-fable-5-1"}
            },
        },
    )


class ClaudeCliAdapterStreamJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.activity_path = self.repo_root / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("stage", provider="claude-cli")

    def _streaming_runner(self, stdout: str, *, returncode: int = 0):
        """A fake runner that feeds stdout to on_line line by line, as the
        real streaming runner does, then returns the completed process."""

        def runner(args, cwd, timeout_seconds, on_line=None):
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(returncode, stdout)

        return runner

    def test_jsonl_output_yields_the_same_final_result_as_the_single_object(self):
        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(_stream_fixture())
        )
        result = adapter.start(_SECRET_PROMPT)

        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(result.text, "done")
        self.assertFalse(result.is_error)
        self.assertEqual(result.raw["type"], "result")
        self.assertEqual(result.raw["num_turns"], 4)

    def test_jsonl_without_a_result_line_raises_provider_error(self):
        stdout = _jsonl({"type": "system", "subtype": "init", "session_id": "x"})
        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=self._streaming_runner(stdout))
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_jsonl_result_with_is_error_true_is_surfaced_not_raised(self):
        stdout = _jsonl(
            {"type": "system", "subtype": "init", "session_id": "x"},
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "oops", "session_id": "x"},
        )
        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=self._streaming_runner(stdout))
        result = adapter.start("hi")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "oops")

    def test_translator_emits_semantic_events_only(self):
        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_stream_fixture()),
            activity=self.emitter,
        )
        adapter.start(_SECRET_PROMPT)

        events = _read_events(self.activity_path)
        self.assertEqual(
            [e["event"] for e in events],
            ["session.observed", "file.changed", "command.started", "command.finished",
             "provider.usage", "tool.call", "provider.usage", "provider.result"],
        )
        session = events[0]
        self.assertEqual((session["session_id"], session["model"], session["provider"]),
                         ("abc-123", "claude-fable-5-1", "claude-cli"))
        edited = events[1]
        self.assertEqual((edited["tool"], edited["path"]), ("Edit", "src/statistics.py"))
        self.assertEqual(set(edited) - {"v", "ts", "actor", "event", "provider"}, {"tool", "path"})
        started, finished = events[2], events[3]
        self.assertEqual((started["tool"], started["tool_use_id"]), ("Bash", "toolu_bash"))
        self.assertEqual(finished["tool_use_id"], "toolu_bash")
        self.assertEqual(events[5]["tool"], "Read")
        # Tokens as flat fields, never the provider's `usage` object: the
        # leak guard below asserts that key never reaches the log at all.
        # The occupancy counts the cached prompt (9 + 120000 + 30000 + 35),
        # which is the whole point -- `input_tokens` alone would report a
        # 150k-token conversation as nine.
        usage = events[4]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (9, 35))
        self.assertEqual(usage["context_used_tokens"], 150_044)
        # The window comes off the final `result` line, where the CLI
        # states it per model. It is quoted, never derived from the model
        # name, which for `claude-opus-5` would cover two different sizes.
        self.assertEqual(events[6]["context_window"], 200_000)
        # No rate limits anywhere: this CLI does not report them, so they
        # stay absent and the panel shows unknown rather than zero.
        for event in events:
            for absent in ("rate_limit_percent", "rate_limit_secondary_percent"):
                self.assertNotIn(absent, event)
        self.assertEqual(events[7]["summary"], "success, 4 turn(s)")

    def test_forbidden_content_never_reaches_the_activity_log(self):
        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_stream_fixture()),
            activity=self.emitter,
        )
        adapter.start(_SECRET_PROMPT)

        text = self.activity_path.read_text(encoding="utf-8")
        for secret in (_SECRET_PROMPT, _SECRET_OLD, _SECRET_NEW, _SECRET_CMD, _SECRET_OUT,
                       "partial", "Let me edit.", "run tests", "12 passed"):
            self.assertNotIn(secret, text)
        for key in ("command", "old_string", "new_string", "input", "content", "usage",
                    "total_cost_usd"):
            self.assertNotIn(f'"{key}"', text)

    def test_model_and_session_only_when_supplied(self):
        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(_stream_fixture(with_model=False)),
            activity=self.emitter,
        )
        adapter.start("hi")
        session = _read_events(self.activity_path)[0]
        self.assertEqual(session["event"], "session.observed")
        self.assertNotIn("model", session)
        self.assertEqual(session["session_id"], "abc-123")

        # No init line at all: no session.observed, and nothing invented.
        self.activity_path.unlink()
        stdout = _jsonl({"type": "result", "subtype": "success", "is_error": False,
                         "result": "x", "session_id": "s"})
        ClaudeCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(stdout), activity=self.emitter
        ).start("hi")
        names = [e["event"] for e in _read_events(self.activity_path)]
        self.assertEqual(names, ["provider.result"])

    def test_subagent_detection_is_conservative(self):
        stdout = _jsonl(
            {"type": "system", "subtype": "init", "session_id": "s"},
            # A nested message alone (parent_tool_use_id set) is NOT a new
            # subagent: it is just attributed to its parent.
            {"type": "assistant", "session_id": "s", "parent_tool_use_id": "toolu_parent",
             "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Grep",
                                      "input": {"pattern": "x"}}]}},
            # An actual Task tool call IS a subagent start.
            {"type": "assistant", "session_id": "s", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "id": "toolu_task", "name": "Task",
                                      "input": {"description": "explore",
                                                "prompt": "SUBAGENT-PROMPT-MUST-NOT-LEAK",
                                                "subagent_type": "Explore"}}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "x",
             "session_id": "s"},
        )
        ClaudeCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(stdout), activity=self.emitter
        ).start("hi")

        events = _read_events(self.activity_path)
        names = [e["event"] for e in events]
        self.assertEqual(names, ["session.observed", "tool.call", "subagent.started",
                                 "provider.result"])
        nested = events[1]
        self.assertEqual((nested["tool"], nested["parent_id"]), ("Grep", "toolu_parent"))
        subagent = events[2]
        self.assertEqual((subagent["tool"], subagent["tool_use_id"]), ("Task", "toolu_task"))
        self.assertNotIn("parent_id", subagent)
        text = self.activity_path.read_text(encoding="utf-8")
        self.assertNotIn("SUBAGENT-PROMPT", text)
        self.assertNotIn("Explore", text)
        self.assertNotIn("explore", text)

    def test_malformed_lines_and_translator_errors_do_not_break_the_turn(self):
        stdout = "garbage\n[1,2]\n" + _jsonl(
            {"type": "assistant", "message": "not-a-dict"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": 5}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
             "session_id": "s"},
        )
        result = ClaudeCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(stdout), activity=self.emitter
        ).start("hi")
        self.assertEqual(result.text, "ok")
        self.assertEqual([e["event"] for e in _read_events(self.activity_path)],
                         ["provider.result"])

    def test_on_session_observed_fires_from_the_init_line_before_the_result(self):
        # The hook exists so a caller can record the session identity while
        # the turn is still running; it must therefore fire from the init
        # line, not from the final result.
        seen: list[str] = []
        stdout = _jsonl(
            {"type": "system", "subtype": "init", "session_id": "abc-123", "model": "m"},
            {"type": "result", "is_error": False, "result": "done", "session_id": "abc-123"},
        )

        def runner(args, cwd, timeout_seconds, on_line=None):
            for line in stdout.splitlines():
                on_line(line)
            # By the time the provider's own result exists, the id was known.
            self.assertEqual(seen, ["abc-123"])
            return _fake_result(0, stdout)

        ClaudeCliAdapter(
            repo_root=self.repo_root, runner=runner, on_session_observed=seen.append
        ).start("hi")
        self.assertEqual(seen, ["abc-123"])

    def test_on_session_observed_fires_once_and_without_an_activity_emitter(self):
        seen: list[str] = []
        stdout = _jsonl(
            {"type": "system", "subtype": "init", "session_id": "abc-123"},
            {"type": "system", "subtype": "init", "session_id": "abc-123"},
            {"type": "result", "is_error": False, "result": "done", "session_id": "abc-123"},
        )
        ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=self._streaming_runner(stdout),
            on_session_observed=seen.append,
        ).start("hi")

        self.assertEqual(seen, ["abc-123"])
        self.assertFalse(self.activity_path.exists())

    def test_no_activity_emitter_means_no_telemetry_and_no_behavior_change(self):
        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root, runner=self._streaming_runner(_stream_fixture())
        )
        result = adapter.start("hi")
        self.assertEqual(result.session_id, "abc-123")
        self.assertFalse(self.activity_path.exists())

    def test_default_runner_streams_a_fake_executable(self):
        # A fake "claude": a script that prints stream-json lines with a
        # pause between them, so the run genuinely exercises the streaming
        # default runner end to end without any real provider.
        script = self.repo_root / "fake-claude.py"
        script.write_text(
            "import sys, time\n"
            f"lines = {_stream_fixture().splitlines()!r}\n"
            "for line in lines:\n"
            "    print(line, flush=True)\n"
            "    time.sleep(0.02)\n",
            encoding="utf-8",
        )
        wrapper = self.repo_root / "fake-claude"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)

        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root, executable=str(wrapper), activity=self.emitter
        )
        result = adapter.start("hi")
        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(result.text, "done")
        self.assertEqual([e["event"] for e in _read_events(self.activity_path)][:2],
                         ["session.observed", "file.changed"])



class ClaudeContextOccupancyTests(unittest.TestCase):
    """How full the window is, as the Claude CLI actually states it.

    The CLI puts nearly the whole conversation in the cache fields and
    only the newest slice in `input_tokens`, so reading `input_tokens`
    alone reported a 200k-token session as eighteen tokens. These tests
    pin the arithmetic and the two lines it must not be taken from.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.activity_path = self.repo_root / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("stage", provider="claude-cli")

    def _usage(self, *messages) -> list[dict]:
        def runner(args, cwd, timeout_seconds, on_line=None):
            stdout = _jsonl(
                {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5"},
                *messages,
                {"type": "result", "subtype": "success", "is_error": False,
                 "result": "done", "session_id": "s"},
            )
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(0, stdout)

        ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, activity=self.emitter).start("hi")
        return [e for e in _read_events(self.activity_path) if e["event"] == "provider.usage"]

    @staticmethod
    def _assistant(usage: dict, *, parent: str | None = None) -> dict:
        return {
            "type": "assistant",
            "session_id": "s",
            "parent_tool_use_id": parent,
            "message": {"role": "assistant", "content": [], "usage": usage},
        }

    def test_the_cached_prompt_counts_toward_the_window(self):
        usage = self._usage(
            self._assistant({"input_tokens": 2, "cache_read_input_tokens": 180_000,
                             "cache_creation_input_tokens": 1_200, "output_tokens": 16})
        )

        self.assertEqual(usage[0]["context_used_tokens"], 181_218)
        # The provider's own two figures are still quoted as it stated them.
        self.assertEqual((usage[0]["input_tokens"], usage[0]["output_tokens"]), (2, 16))

    def test_the_latest_message_replaces_the_previous_one_rather_than_adding(self):
        # Each request re-sends the conversation, so summing messages
        # would count the same prompt once per request.
        usage = self._usage(
            self._assistant({"input_tokens": 1, "cache_read_input_tokens": 50_000,
                             "output_tokens": 10}),
            self._assistant({"input_tokens": 1, "cache_read_input_tokens": 90_000,
                             "output_tokens": 10}),
        )

        self.assertEqual([e["context_used_tokens"] for e in usage], [50_011, 90_011])

    def test_a_subagent_s_context_is_not_the_stage_s_context(self):
        # A Task subagent is a separate, much smaller conversation. Letting
        # one land would make the dial collapse whenever work was delegated.
        usage = self._usage(
            self._assistant({"input_tokens": 1, "cache_read_input_tokens": 190_000,
                             "output_tokens": 10}),
            self._assistant({"input_tokens": 1, "cache_read_input_tokens": 300,
                             "output_tokens": 5}, parent="toolu_task"),
        )

        self.assertEqual([e["context_used_tokens"] for e in usage], [190_011])

    def test_a_line_that_states_no_prompt_side_is_not_an_occupancy(self):
        usage = self._usage(self._assistant({"output_tokens": 16}))

        self.assertEqual(usage[0]["output_tokens"], 16)
        self.assertNotIn("context_used_tokens", usage[0])

    def test_no_window_is_reported_when_the_turn_states_several_that_disagree(self):
        # A turn that ran a subagent on another model lists both. Where the
        # stage's own model cannot be matched and the sizes differ, the
        # window is left unstated rather than picked from.
        def runner(args, cwd, timeout_seconds, on_line=None):
            stdout = _jsonl(
                {"type": "system", "subtype": "init", "session_id": "s", "model": "unmatched"},
                {"type": "result", "subtype": "success", "is_error": False, "result": "d",
                 "session_id": "s",
                 "modelUsage": {"claude-opus-5": {"contextWindow": 1_000_000},
                                "claude-haiku-4-5": {"contextWindow": 200_000}}},
            )
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(0, stdout)

        ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, activity=self.emitter).start("hi")
        events = [e for e in _read_events(self.activity_path) if e["event"] == "provider.usage"]
        self.assertEqual(events, [])

    def test_the_stage_s_own_model_picks_the_window_among_several(self):
        def runner(args, cwd, timeout_seconds, on_line=None):
            stdout = _jsonl(
                {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5"},
                {"type": "result", "subtype": "success", "is_error": False, "result": "d",
                 "session_id": "s",
                 "modelUsage": {"claude-opus-5": {"contextWindow": 1_000_000},
                                "claude-haiku-4-5": {"contextWindow": 200_000}}},
            )
            if on_line is not None:
                for line in stdout.splitlines():
                    on_line(line)
            return _fake_result(0, stdout)

        ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, activity=self.emitter).start("hi")
        events = [e for e in _read_events(self.activity_path) if e["event"] == "provider.usage"]
        self.assertEqual(events[0]["context_window"], 1_000_000)


class ClaudeCliAdapterPathNormalizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name) / "repo"
        self.repo_root.mkdir()
        self.activity_path = Path(self._tmp.name) / "activity.jsonl"
        self.emitter = ActivityLog(self.activity_path).bind("stage", provider="claude-cli")

    def _run(self, *file_paths: str):
        blocks = [
            {"type": "tool_use", "id": f"t{i}", "name": "Write",
             "input": {"file_path": fp, "content": "CONTENT-MUST-NOT-LEAK"}}
            for i, fp in enumerate(file_paths)
        ]
        stdout = _jsonl(
            {"type": "system", "subtype": "init", "session_id": "s"},
            {"type": "assistant", "session_id": "s", "parent_tool_use_id": None,
             "message": {"content": blocks}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
             "session_id": "s"},
        )

        def runner(args, cwd, timeout_seconds, on_line=None):
            for line in stdout.splitlines():
                on_line(line)
            return _fake_result(0, stdout)

        ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, activity=self.emitter).start("hi")
        return [e for e in _read_events(self.activity_path) if e["event"] == "file.changed"]

    def test_absolute_in_repo_path_is_persisted_repo_relative(self):
        # Verified live: Claude's Write/Edit tools report absolute paths.
        (event,) = self._run(str(self.repo_root / "src" / "statistics.py"))
        self.assertEqual(event["path"], "src/statistics.py")
        text = self.activity_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.repo_root), text)
        self.assertNotIn(str(Path.home()), text)

    def test_outside_repo_path_is_omitted_not_leaked(self):
        outside = Path(self._tmp.name) / "elsewhere" / "notes.txt"
        home = Path.home() / "private.txt"
        events = self._run(str(outside), str(home), "../escape.txt")
        self.assertEqual(len(events), 3)
        for event in events:
            self.assertNotIn("path", event)
            self.assertEqual(event["tool"], "Write")
        text = self.activity_path.read_text(encoding="utf-8")
        for leak in ("elsewhere", "private.txt", "escape.txt", str(Path.home()), self._tmp.name):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()
