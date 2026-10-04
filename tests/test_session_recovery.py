"""Stage 2 of fresh agent sessions: recovery diagnostics and the CLI.

- Recoverable provider failures are classified
  (``ProviderSessionUnresumable`` / ``ProviderUnavailable``) and pause a
  managed run, naming the role, with the candidate untouched and nothing
  retried or discarded.
- Evidence answering a NEEDS_YOU raised over a recorded candidate is held to
  that candidate.
- ``--fresh-*`` / ``--next-turn`` reach the engine from the CLI, and
  requests the entered stage cannot honour are refused, not ignored.
"""

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

import argparse
import json
import shlex

from agent_sparring.cli import _retry_command, _retry_lines, build_parser, main
from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.plan import (
    PAUSE_PROVIDER_UNAVAILABLE,
    PAUSE_SESSION_UNRESUMABLE,
    PlanError,
    PlanRunError,
    PlanRunStatus,
    ProviderPause,
)
from agent_sparring.providers import (
    ProviderError,
    ProviderSessionUnresumable,
    ProviderUnavailable,
    StageAgentResult,
    classify_failure_text,
    recoverable_provider_failure,
)
from agent_sparring.providers.claude_cli import ClaudeCliAdapter
from agent_sparring.providers.codex_cli import CodexCliAdapter
from agent_sparring.sessions import generations, start_fresh_session
from agent_sparring.stage import Stage, StageStatus

from test_fresh_session import _head, _LoopRepo, _Stuck
from test_plan import NEEDS_YOU, READY, S1, _PlanRepoTestCase, _SparringAdapter, _StageAdapter, _run_git
from test_stage_agent_pin import PROVIDERS_ONLY


def _completed(stdout="", stderr="", returncode=1):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ClassificationTests(unittest.TestCase):
    def test_failure_texts(self):
        cases = [
            ("codex: invalid_encrypted_content: could not decrypt", True, ProviderSessionUnresumable),
            ("thread not found: 0199", True, ProviderSessionUnresumable),
            ("No conversation found with session ID: abc", True, ProviderSessionUnresumable),
            # A brand-new conversation is never "unresumable".
            ("No conversation found with session ID: abc", False, None),
            ("You've hit your usage limit. Try again later", False, ProviderUnavailable),
            ("429 Too Many Requests", True, ProviderUnavailable),
            ("Rate limit reached for requests", False, ProviderUnavailable),
            ("insufficient_quota", False, ProviderUnavailable),
            ("the model wrote an invalid verdict", True, None),
        ]
        for text, resuming, expected in cases:
            with self.subTest(text=text, resuming=resuming):
                self.assertIs(classify_failure_text(text, resuming=resuming), expected)

    def test_claude_resume_of_an_unknown_conversation_is_unresumable(self):
        runner = lambda *a: _completed(stderr="No conversation found with session ID: s-1")
        adapter = ClaudeCliAdapter(repo_root=Path("."), runner=runner)
        with self.assertRaises(ProviderSessionUnresumable) as ctx:
            adapter.resume("s-1", "go on")
        self.assertIn("No conversation found", str(ctx.exception))
        # A fresh start failing the same way stays an ordinary failure.
        with self.assertRaises(ProviderError) as ctx:
            adapter.start("go")
        self.assertNotIsInstance(ctx.exception, ProviderSessionUnresumable)

    def test_codex_resume_with_unreadable_encrypted_content_is_unresumable(self):
        stdout = (
            '{"type":"thread.started","thread_id":"t-1"}\n'
            '{"type":"turn.failed","error":{"message":"invalid_encrypted_content"}}\n'
        )
        runner = lambda *a: _completed(stdout=stdout)
        adapter = CodexCliAdapter(repo_root=Path("."), runner=runner)
        with self.assertRaises(ProviderSessionUnresumable):
            adapter.resume("t-1", "review")

    def test_codex_rate_limit_is_unavailable(self):
        runner = lambda *a: _completed(stderr="stream error: exceeded retry limit, last status: 429 Too Many Requests")
        adapter = CodexCliAdapter(repo_root=Path("."), runner=runner)
        with self.assertRaises(ProviderUnavailable):
            adapter.start("review")

    def test_the_cause_chain_is_followed(self):
        root = ProviderUnavailable("quota")
        try:
            try:
                raise RuntimeError("wrapped") from root
            except RuntimeError as inner:
                raise LoopError("loop failed") from inner
        except LoopError as exc:
            self.assertIs(recoverable_provider_failure(exc), root)
        self.assertIsNone(recoverable_provider_failure(LoopError("plain")))


class _UnavailableBeforeSession(_StageAdapter):
    """Fails at start, before any session id is announced."""

    def start(self, prompt):
        self.start_calls.append(prompt)
        raise ProviderUnavailable("usage limit reached")


class _UnresumableReviewer(_SparringAdapter):
    def resume(self, session_id, prompt):
        self.resume_calls.append((session_id, prompt))
        raise ProviderSessionUnresumable(f"thread not found: {session_id}")


class ProviderPauseTests(_Stuck):
    def test_failure_before_a_session_id_pauses_and_records_no_session(self):
        before = _head(self.repo)
        stage_adapter = _UnavailableBeforeSession(self.repo, commit=True)
        sparring = _SparringAdapter([READY])

        with self.assertRaises(ProviderPause) as ctx:
            self._start(stage_adapter, sparring)

        pause = ctx.exception
        self.assertEqual((pause.role, pause.kind), ("stage", PAUSE_PROVIDER_UNAVAILABLE))
        self.assertFalse(pause.has_session)
        self.assertEqual(pause.stage_id, S1)
        self.assertTrue(pause.run)
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)
        state = self._stage(S1).read_state()
        self.assertIsNone(state.implementation_session_id)
        self.assertTrue(all(g.session_id is None for g in generations(state, "stage")))
        self.assertEqual(state.next_turn, "stage")
        self.assertEqual(_head(self.repo), before)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])

        # An ordinary retry starts the first conversation, as if nothing happened.
        retry = _StageAdapter(self.repo, commit=True)
        result = self._resume(retry, _SparringAdapter([READY]), stop_after_stage=S1)
        self.assertEqual(len(retry.start_calls), 1)
        self.assertIn(S1, dict(result.accepted))

    def test_an_unresumable_reviewer_pauses_then_a_fresh_reviewer_continues(self):
        stage, stage_adapter = self.leave_stuck()
        candidate = stage.read_state().next_turn_candidate
        reviewer = _UnresumableReviewer([READY])

        with self.assertRaises(ProviderPause) as ctx:
            self._resume(stage_adapter, reviewer, stop_after_stage=S1)

        pause = ctx.exception
        self.assertEqual((pause.role, pause.kind), ("sparring", PAUSE_SESSION_UNRESUMABLE))
        self.assertTrue(pause.has_session)
        state = stage.read_state()
        # Nothing discarded, nothing advanced, nothing touched.
        self.assertEqual(state.sparring_session_id, "spar-1")
        self.assertEqual((state.next_turn, state.next_turn_candidate), ("sparring", candidate))
        self.assertEqual(len(generations(state, "sparring")), 1)

        fresh = _SparringAdapter([READY])
        result = self._resume(
            stage_adapter,
            fresh,
            stop_after_stage=S1,
            fresh_roles=("sparring",),
            fresh_reason="session-unresumable",
        )
        self.assertEqual(len(fresh.start_calls), 1)
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2)
        self.assertEqual(dict(result.accepted)[S1], candidate.head_sha)
        history = generations(stage.read_state(), "sparring")
        self.assertEqual([g.generation for g in history], [1, 2])
        self.assertEqual(history[-1].start_reason, "fresh:session-unresumable")

    def test_a_failing_pending_fresh_reviewer_stays_pending_without_a_bogus_id(self):
        stage, stage_adapter = self.leave_stuck()

        class _Down(_SparringAdapter):
            def start(self, prompt):
                self.start_calls.append(prompt)
                raise ProviderUnavailable("rate limit")

        with self.assertRaises(ProviderPause) as ctx:
            self._resume(stage_adapter, _Down([]), fresh_roles=("sparring",), fresh_reason="x")
        self.assertFalse(ctx.exception.has_session)
        state = stage.read_state()
        self.assertIsNone(state.sparring_session_id)
        self.assertIsNone(generations(state, "sparring")[-1].session_id)
        # Retrying plainly starts that same pending generation, not a third.
        fresh = _SparringAdapter([READY])
        self._resume(stage_adapter, fresh, stop_after_stage=S1)
        self.assertEqual(len(fresh.start_calls), 1)
        self.assertEqual(len(generations(stage.read_state(), "sparring")), 2)

    def test_an_ordinary_provider_failure_still_fails(self):
        stage, stage_adapter = self.leave_stuck()

        class _Broken(_SparringAdapter):
            def resume(self, session_id, prompt):
                raise ProviderError("segfault")

        with self.assertRaises(PlanRunError) as ctx:
            self._resume(stage_adapter, _Broken([]))
        self.assertNotIsInstance(ctx.exception, ProviderPause)

    def test_a_capacity_is_error_turn_is_typed_unavailable(self):
        class _LimitedTurn(_StageAdapter):
            def start(self, prompt):
                self.start_calls.append(prompt)
                # Classified from the provider's structured errors only.
                return StageAgentResult(
                    session_id="impl-1", text="", is_error=True,
                    raw={"errors": ["usage_limit_reached"]},
                )

        with self.assertRaises(ProviderPause) as ctx:
            self._start(_LimitedTurn(self.repo), _SparringAdapter([READY]))
        self.assertEqual((ctx.exception.role, ctx.exception.kind), ("stage", PAUSE_PROVIDER_UNAVAILABLE))
        # The turn ran, so its session is real and kept for a deliberate retry.
        self.assertTrue(ctx.exception.has_session)


class EvidenceCandidateTests(_PlanRepoTestCase):
    def leave_needs_you(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        result = self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        stage = self._stage(S1)
        self.assertEqual(stage.read_state().next_turn, "sparring")
        return stage, stage_adapter

    def test_evidence_over_the_unchanged_candidate_is_judged(self):
        stage, stage_adapter = self.leave_needs_you()
        reviewed = _head(self.repo)
        sparring = _SparringAdapter([READY])

        result = self._resume(stage_adapter, sparring, evidence="checked", stop_after_stage=S1)

        self.assertEqual([sid for sid, _ in sparring.resume_calls], ["spar-1"])
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 1)
        self.assertEqual(dict(result.accepted)[S1], reviewed)

    def test_evidence_after_drift_is_refused_before_it_is_recorded(self):
        for drift in ("commit", "worktree"):
            with self.subTest(drift=drift):
                self.setUp()
                stage, stage_adapter = self.leave_needs_you()
                notes = (stage.directory / "notes.md").read_text()
                (self.repo / "drift.txt").write_text("not reviewed\n")
                if drift == "commit":
                    _run_git(self.repo, "add", "drift.txt")
                    _run_git(self.repo, "commit", "-q", "-m", "drift")
                sparring = _SparringAdapter([READY])

                with self.assertRaises(PlanError) as ctx:
                    self._resume(stage_adapter, sparring, evidence="checked")

                self.assertIn("waiting for review of", str(ctx.exception))
                self.assertIn("--next-turn sparring", str(ctx.exception))
                self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
                self.assertEqual((stage.directory / "notes.md").read_text(), notes)
                self.assertIsNot(stage.read_state().status, StageStatus.ACCEPTED)

    def test_the_loop_itself_refuses_an_evidence_turn_over_a_drifted_candidate(self):
        stage, stage_adapter = self.leave_needs_you()
        (self.repo / "drift.txt").write_text("not reviewed\n")
        sparring = _SparringAdapter([READY])
        with self.assertRaises(LoopError):
            run_unattended_loop(
                stage, self.sparring_dir, self.repo, stage_adapter, sparring,
                expected_branch="feature/x", start_with="sparring",
            )
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])

    def test_an_explicit_re_pin_lets_deliberately_changed_content_be_judged(self):
        stage, stage_adapter = self.leave_needs_you()
        (self.repo / "fix.txt").write_text("asked for by the gate\n")
        _run_git(self.repo, "add", "fix.txt")
        _run_git(self.repo, "commit", "-q", "-m", "manual fix")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        sparring = _SparringAdapter([READY])

        result = self._resume(
            stage_adapter, sparring, evidence="fixed by hand", next_turn="sparring",
            stop_after_stage=S1,
        )

        self.assertEqual(dict(result.accepted)[S1], _head(self.repo))
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 1)

    def test_evidence_with_next_turn_stage_is_refused(self):
        self.leave_needs_you()
        with self.assertRaises(PlanError):
            self._resume(_StageAdapter(self.repo), _SparringAdapter([]), evidence="x", next_turn="stage")

    def test_needs_you_without_evidence_keeps_the_pause_and_refuses_recovery_flags(self):
        stage, stage_adapter = self.leave_needs_you()
        before = stage.read_state()
        sparring = _SparringAdapter([READY])

        # Deliberate: a resume that answers nothing keeps the recorded pause,
        # even though next_turn says sparring -- running the reviewer again
        # would re-ask the same question.
        result = self._resume(stage_adapter, sparring)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsNotNone(result.recorded)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertEqual(stage.read_state().next_turn, "sparring")

        for kwargs in ({"fresh_roles": ("sparring",)}, {"next_turn": "sparring"}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaises(PlanRunError) as ctx:
                    self._resume(stage_adapter, sparring, **kwargs)
                self.assertIn("waiting for a person", str(ctx.exception))
                after = stage.read_state()
                self.assertEqual(after.sessions, before.sessions)
                self.assertEqual(after.sparring_session_id, "spar-1")
                self.assertEqual(after.next_turn_source, before.next_turn_source)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])

    def test_evidence_and_a_fresh_reviewer_together(self):
        stage, stage_adapter = self.leave_needs_you()
        sparring = _SparringAdapter([READY])
        self._resume(
            stage_adapter, sparring, evidence="checked", fresh_roles=("sparring",),
            fresh_reason="new eyes", stop_after_stage=S1,
        )
        self.assertEqual(len(sparring.start_calls), 1)
        self.assertIn("checked", sparring.start_calls[0])
        self.assertEqual(len(generations(stage.read_state(), "sparring")), 2)


class CliTests(_LoopRepo):
    def setUp(self):
        super().setUp()
        (self.sparring_dir / "project.toml").write_text(PROVIDERS_ONLY)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def loop_argv(self, *extra):
        return ("run-loop", "s1", "--repo-root", str(self.repo), "--expected-branch", "feature/x", *extra)

    def test_flags_parse_on_run_loop_and_resume_plan(self):
        parser = build_parser()
        for command in (["run-loop", "s1"], ["resume-plan", "plan.md"]):
            with self.subTest(command=command[0]):
                args = parser.parse_args(
                    [*command, "--expected-branch", "b", "--fresh-sparrer", "--fresh-stage-agent",
                     "--fresh-reason", "why", "--next-turn", "sparring"]
                )
                self.assertTrue(args.fresh_sparrer and args.fresh_stage_agent)
                self.assertEqual((args.fresh_reason, args.next_turn), ("why", "sparring"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["run-loop", "s1", "--expected-branch", "b", "--next-turn", "finalization"])

    def test_a_reason_without_a_fresh_flag_is_refused(self):
        code, _, err = self.run_cli(*self.loop_argv("--fresh-reason", "why"))
        self.assertEqual(code, 1)
        self.assertIn("--fresh-sparrer", err)

    def test_a_fresh_session_with_nothing_to_replace_is_refused_before_any_turn(self):
        with mock.patch("agent_sparring.cli.run_unattended_loop") as loop:
            code, _, err = self.run_cli(*self.loop_argv("--fresh-sparrer"))
        self.assertEqual(code, 1)
        self.assertIn("no sparring session yet", err)
        loop.assert_not_called()

    def test_resolved_configuration_is_printed_before_launch(self):
        with mock.patch("agent_sparring.cli.run_unattended_loop") as loop:
            loop.side_effect = LoopError("stop here")
            code, _, err = self.run_cli(*self.loop_argv())
        self.assertEqual(code, 1)
        self.assertIn("stage agent: adapter claude-cli (claude)", err)
        self.assertIn("sparrer: adapter codex-cli (codex)", err)
        self.assertIn("new conversation at its first turn", err)
        self.assertIn("provider-reported model not reported", err)
        self.assertIn("backend/account not reported", err)
        self.assertLess(err.index("agents for stage s1"), err.index("stop here"))

    def test_an_unresumable_reviewer_prints_the_exact_fresh_retry_command(self):
        from agent_sparring.sessions import record_session_id
        from agent_sparring.sparring_agent import SparringAgentRunError

        state = self.stage.read_state()
        record_session_id(state, "sparring", "t-1")
        self.stage.write_state(state)

        def fail(*a, **k):
            try:
                try:
                    raise ProviderSessionUnresumable("invalid_encrypted_content")
                except ProviderError as exc:
                    raise SparringAgentRunError(str(exc)) from exc
            except SparringAgentRunError as exc:
                raise LoopError(f"sparring-agent turn failed: {exc}") from exc

        with mock.patch("agent_sparring.cli.run_unattended_loop", side_effect=fail):
            code, _, err = self.run_cli(*self.loop_argv())
        self.assertEqual(code, 1)
        self.assertIn("paused: stage s1: sparring provider turn session-unresumable", err)
        self.assertIn(
            f"sparring --sparring-dir {self.sparring_dir.resolve()} run-loop s1 --repo-root "
            f"{self.repo.resolve()} --expected-branch feature/x "
            "--fresh-sparrer --fresh-reason session-unresumable",
            err,
        )
        self.assertIn("did not retry", err)
        self.assertEqual(self.stage.read_state().sparring_session_id, "t-1")

    def test_retry_lines(self):
        unresumable = _retry_lines("CMD", role="stage", kind=PAUSE_SESSION_UNRESUMABLE, has_session=True)
        self.assertIn("  CMD --fresh-stage-agent --fresh-reason session-unresumable", unresumable)
        unavailable = _retry_lines("CMD", role="sparring", kind=PAUSE_PROVIDER_UNAVAILABLE, has_session=True)
        self.assertIn("  CMD", unavailable)
        self.assertIn("  CMD --fresh-sparrer --fresh-reason provider-unavailable", unavailable)
        # No conversation yet: a fresh session would be refused, so it is not offered.
        first = _retry_lines("CMD", role="stage", kind=PAUSE_PROVIDER_UNAVAILABLE, has_session=False)
        self.assertFalse(any("--fresh" in line for line in first))

    def test_run_loop_fresh_sparrer_opens_a_generation_and_reports_it(self):
        from agent_sparring.sessions import record_session_id

        state = self.stage.read_state()
        record_session_id(state, "sparring", "t-1")
        self.stage.write_state(state)
        with mock.patch("agent_sparring.cli.run_unattended_loop", side_effect=LoopError("stop")):
            code, _, err = self.run_cli(
                *self.loop_argv("--fresh-sparrer", "--fresh-reason", "stale", "--sparring-model", "m2")
            )
        self.assertEqual(code, 1)
        history = generations(self.stage.read_state(), "sparring")
        self.assertEqual([g.start_reason for g in history][-1], "fresh:stale")
        self.assertIn("fresh conversation, generation 2 (fresh:stale)", err)
        self.assertIn("configured model m2", err)

    def test_an_override_conflicting_with_the_active_session_stays_refused(self):
        from agent_sparring.sessions import record_session_id

        with mock.patch("agent_sparring.cli.run_unattended_loop", side_effect=LoopError("stop")):
            self.run_cli(*self.loop_argv())  # pins both roles
        state = self.stage.read_state()
        record_session_id(state, "sparring", "t-1")
        self.stage.write_state(state)
        with mock.patch("agent_sparring.cli.run_unattended_loop") as loop:
            code, _, err = self.run_cli(*self.loop_argv("--sparring-model", "other"))
        self.assertEqual(code, 1)
        self.assertIn("fresh sparring session", err)
        loop.assert_not_called()


def _claude_runner(payload):
    line = json.dumps({"type": "result", **payload})
    return lambda *a: _completed(stdout=line + "\n", returncode=1)


class ClaudeStructuredErrorTests(_LoopRepo):
    def run_loop(self, adapter):
        return run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, adapter, _SparringAdapter([READY]),
            expected_branch="feature/x",
        )

    def test_a_rate_limit_only_in_errors_is_unavailable_through_the_loop(self):
        payload = {"is_error": True, "session_id": "c-1", "errors": ["429 Too Many Requests"]}
        adapter = ClaudeCliAdapter(repo_root=self.repo, runner=_claude_runner(payload))
        with self.assertRaises(ProviderUnavailable) as ctx:
            adapter.start("go")
        self.assertIn("429 Too Many Requests", str(ctx.exception))

        with self.assertRaises(LoopError) as ctx:
            self.run_loop(ClaudeCliAdapter(repo_root=self.repo, runner=_claude_runner(payload)))
        self.assertIsInstance(recoverable_provider_failure(ctx.exception), ProviderUnavailable)
        self.assertEqual(self.stage.read_state().next_turn, "stage")

    def test_an_unknown_conversation_only_in_errors_is_unresumable_on_resume(self):
        from agent_sparring.sessions import record_session_id

        state = self.stage.read_state()
        record_session_id(state, "stage", "c-1")
        self.stage.write_state(state)
        payload = {
            "is_error": True,
            "session_id": "c-1",
            "errors": ["No conversation found with session ID: c-1"],
        }
        with self.assertRaises(LoopError) as ctx:
            self.run_loop(ClaudeCliAdapter(repo_root=self.repo, runner=_claude_runner(payload)))
        self.assertIsInstance(
            recoverable_provider_failure(ctx.exception), ProviderSessionUnresumable
        )
        self.assertEqual(self.stage.read_state().implementation_session_id, "c-1")

    def test_an_unclassified_error_result_is_still_an_is_error_result(self):
        payload = {"is_error": True, "session_id": "c-1", "errors": ["tool crashed"]}
        result = ClaudeCliAdapter(repo_root=Path("."), runner=_claude_runner(payload)).start("go")
        self.assertTrue(result.is_error)


# Agent-written text that names provider failures; never provider error data.
_AGENT_SAYS = (
    "session not found",
    "No conversation found with session ID: c-1",
    "quota",
    "rate limit",
    "overloaded",
    "invalid_encrypted_content",
    "429 Too Many Requests",
)


class AgentTextIsNeverClassifiedTests(_LoopRepo):
    """Only provider error data (structured fields, stderr) is classified;
    transcripts and result text are the agent's and may say anything."""

    def assert_plain(self, raised):
        self.assertIsInstance(raised, ProviderError)
        self.assertNotIsInstance(raised, (ProviderSessionUnresumable, ProviderUnavailable))
        self.assertIsNone(recoverable_provider_failure(raised))

    def test_claude_transcript_without_a_result_is_not_classified(self):
        for text in _AGENT_SAYS:
            with self.subTest(text=text):
                transcript = json.dumps(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
                )
                runner = lambda *a, t=transcript: _completed(stdout=t + "\n")
                with self.assertRaises(ProviderError) as ctx:
                    ClaudeCliAdapter(repo_root=self.repo, runner=runner).resume("c-1", "go")
                self.assert_plain(ctx.exception)

    def test_claude_payload_without_session_id_is_not_classified_by_result_text(self):
        for text in _AGENT_SAYS:
            with self.subTest(text=text):
                runner = _claude_runner({"is_error": True, "result": text})
                with self.assertRaises(ProviderError) as ctx:
                    ClaudeCliAdapter(repo_root=self.repo, runner=runner).resume("c-1", "go")
                self.assert_plain(ctx.exception)

    def test_claude_is_error_result_text_is_not_classified(self):
        for text in _AGENT_SAYS:
            with self.subTest(text=text):
                payload = {"is_error": True, "session_id": "c-1", "result": text}
                adapter = ClaudeCliAdapter(repo_root=self.repo, runner=_claude_runner(payload))
                self.assertTrue(adapter.resume("c-1", "go").is_error)

    def test_codex_stdout_is_not_classified(self):
        for text in _AGENT_SAYS:
            with self.subTest(text=text):
                item = json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
                )
                # No thread.started: the stdout transcript is the only text.
                runner = lambda *a, t=item: _completed(stdout=t + "\n")
                with self.assertRaises(ProviderError) as ctx:
                    CodexCliAdapter(repo_root=self.repo, runner=runner).resume("t-1", "review")
                self.assert_plain(ctx.exception)
                # With a thread and a non-zero exit, still only stderr counts.
                stdout = '{"type":"thread.started","thread_id":"t-1"}\n' + item + "\n"
                runner = lambda *a, o=stdout: _completed(stdout=o, stderr="boom")
                with self.assertRaises(ProviderError) as ctx:
                    CodexCliAdapter(repo_root=self.repo, runner=runner).resume("t-1", "review")
                self.assert_plain(ctx.exception)

    def test_loop_is_error_result_text_is_not_classified(self):
        for index, text in enumerate(_AGENT_SAYS):
            with self.subTest(text=text):
                stage = Stage.resolve(self.sparring_dir, f"err{index}").create()

                class _ErrorText(_StageAdapter):
                    def _turn(self, session_id, t=text):
                        return StageAgentResult(session_id="c-1", text=t, is_error=True)

                with self.assertRaises(LoopError) as ctx:
                    run_unattended_loop(
                        stage, self.sparring_dir, self.repo, _ErrorText(self.repo),
                        _SparringAdapter([READY]), expected_branch="feature/x",
                    )
                self.assertIsNone(recoverable_provider_failure(ctx.exception))

    def test_structured_fields_and_stderr_are_still_classified(self):
        payload = {"is_error": True, "session_id": "c-1", "subtype": "error_during_execution",
                   "errors": [{"type": "rate_limit_error"}]}
        with self.assertRaises(ProviderUnavailable):
            ClaudeCliAdapter(repo_root=self.repo, runner=_claude_runner(payload)).start("go")
        stdout = (
            '{"type":"thread.started","thread_id":"t-1"}\n'
            '{"type":"error","message":"thread not found: t-1"}\n'
            '{"type":"turn.failed","error":{"message":"stream disconnected"}}\n'
        )
        with self.assertRaises(ProviderSessionUnresumable):
            CodexCliAdapter(repo_root=self.repo, runner=lambda *a: _completed(stdout=stdout)
                            ).resume("t-1", "review")
        with self.assertRaises(ProviderUnavailable):
            CodexCliAdapter(
                repo_root=self.repo,
                runner=lambda *a: _completed(stderr="Error: usage_limit_reached"),
            ).start("review")


class RetryCommandTests(unittest.TestCase):
    def test_the_printed_command_parses_back_to_the_same_run(self):
        args = argparse.Namespace(
            expected_branch="feature/x y",
            stage_provider=None, sparring_provider="codex-cli", stage_model=None,
            sparring_model="m 2", stage_effort=None, sparring_effort=None,
            stop_after_stage="s 1", claude_executable="/opt/my claude",
            codex_executable="codex", permission_mode="acceptEdits", max_send_back_cycles=7,
        )
        command = _retry_command(
            args, Path("/tmp/my state/.sparring"), Path("/tmp/my repo"),
            ["resume-plan", "/tmp/my repo/docs/plan.md", "--run-key", "k1"],
        )
        argv = shlex.split(command)
        self.assertEqual(argv[0], "sparring")
        parsed = build_parser().parse_args(argv[1:] + ["--fresh-sparrer"])
        self.assertEqual(parsed.sparring_dir, str(Path("/tmp/my state/.sparring").resolve()))
        self.assertEqual(parsed.repo_root, str(Path("/tmp/my repo").resolve()))
        self.assertEqual(parsed.plan_path, "/tmp/my repo/docs/plan.md")
        self.assertEqual(
            (parsed.run_key, parsed.expected_branch, parsed.sparring_model, parsed.stop_after_stage),
            ("k1", "feature/x y", "m 2", "s 1"),
        )
        self.assertEqual((parsed.claude_executable, parsed.max_send_back_cycles), ("/opt/my claude", 7))
        self.assertEqual(parsed.sparring_provider, "codex-cli")


def _printed_commands(err: str) -> list[list[str]]:
    return [shlex.split(line.strip())[1:] for line in err.splitlines() if line.startswith("  sparring ")]


class _UnavailableReviewer(_SparringAdapter):
    def resume(self, session_id, prompt):
        self.resume_calls.append((session_id, prompt))
        raise ProviderUnavailable("usage limit reached")


class EvidenceRetryTests(_PlanRepoTestCase):
    """The printed retry after a failed evidence review re-enters that
    evidence turn, over the same verified candidate."""

    def cli(self, argv, stage_adapter, sparring):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "agent_sparring.cli._build_loop_adapters", return_value=(stage_adapter, sparring)
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def fail_evidence_review(self, reviewer):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        code, _, err = self.cli(
            ["--sparring-dir", str(self.sparring_dir), "resume-plan", str(self.plan_path),
             "--repo-root", str(self.repo), "--expected-branch", "feature/x",
             "--stop-after-stage", S1, "--evidence", "checked on device"],
            stage_adapter, reviewer,
        )
        self.assertEqual(code, 1, err)
        self.assertIn("paused: stage", err)
        return stage_adapter, _printed_commands(err)

    def test_plain_retry_after_an_unavailable_evidence_review(self):
        stage_adapter, commands = self.fail_evidence_review(_UnavailableReviewer([]))
        reviewer = _SparringAdapter([READY])
        code, out, err = self.cli(commands[0], stage_adapter, reviewer)
        self.assertEqual(code, 0, err)
        self.assertIn("checked on device", reviewer.resume_calls[0][1])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 1)

    def test_fresh_retry_after_an_unresumable_evidence_review(self):
        stage_adapter, commands = self.fail_evidence_review(_UnresumableReviewer([]))
        self.assertIn("--fresh-sparrer", commands[0])
        reviewer = _SparringAdapter([READY])
        code, _, err = self.cli(commands[0], stage_adapter, reviewer)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(reviewer.start_calls), 1)
        self.assertIn("checked on device", reviewer.start_calls[0])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(len(generations(self._stage(S1).read_state(), "sparring")), 2)

    def test_the_replayed_evidence_turn_still_verifies_the_candidate(self):
        stage_adapter, commands = self.fail_evidence_review(_UnavailableReviewer([]))
        (self.repo / "drift.txt").write_text("not reviewed\n")
        reviewer = _SparringAdapter([READY])
        code, _, err = self.cli(commands[0], stage_adapter, reviewer)
        self.assertEqual(code, 1)
        self.assertIn("waiting for review of", err)
        self.assertEqual(reviewer.start_calls + reviewer.resume_calls, [])

    def test_a_new_verdict_retires_the_pending_evidence(self):
        stage_adapter, commands = self.fail_evidence_review(_UnavailableReviewer([]))
        # The evidence turn now completes with another NEEDS_YOU ...
        self.cli(commands[0], stage_adapter, _SparringAdapter([NEEDS_YOU]))
        # ... so a plain resume keeps that new pause instead of replaying.
        reviewer = _SparringAdapter([READY])
        result = self._resume(stage_adapter, reviewer)
        self.assertIsNotNone(result.recorded)
        self.assertEqual(reviewer.start_calls + reviewer.resume_calls, [])


from test_independent_review import REVIEW_STAGE, _ReviewRepoTestCase  # noqa: E402


class ReviewOnlyEvidenceRetryTests(_ReviewRepoTestCase):
    def cli(self, argv, stage_adapter, sparring):
        return EvidenceRetryTests.cli(self, argv, stage_adapter, sparring)

    def test_retries_after_a_failed_independent_evidence_review(self):
        for failing, expect_fresh in ((_UnavailableReviewer([]), False), (_UnresumableReviewer([]), True)):
            with self.subTest(failing=type(failing).__name__):
                self.setUp()
                stage_adapter = _StageAdapter(self.repo, commit=True)
                self.start(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))
                code, _, err = self.cli(
                    ["--sparring-dir", str(self.sparring_dir), "resume-plan",
                     "--manifest", str(self.manifest_path), "--repo-root", str(self.repo),
                     "--expected-branch", "feature/x", "--evidence", "verified by hand"],
                    stage_adapter, failing,
                )
                self.assertEqual(code, 1, err)
                commands = _printed_commands(err)
                self.assertEqual("--fresh-sparrer" in commands[0], expect_fresh)
                reviewer = _SparringAdapter([READY])
                code, _, err = self.cli(commands[0], stage_adapter, reviewer)
                self.assertEqual(code, 0, err)
                prompts = reviewer.start_calls + [p for _, p in reviewer.resume_calls]
                self.assertEqual(len(prompts), 1)
                self.assertIn("verified by hand", prompts[0])
                self.assertIs(self.stage(REVIEW_STAGE).read_state().status, StageStatus.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
