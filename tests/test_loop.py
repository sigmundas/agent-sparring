import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.loop import (
    DEFAULT_MAX_SEND_BACK_CYCLES,
    LoopError,
    LoopRunawayError,
    run_unattended_loop,
)
from agent_sparring.providers import ProviderError, SparringAgentResult, StageAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import Stage


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _verdict_text(action: str, summary: str, *, findings: str | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": None,
            "findings": findings if findings is not None else summary,
            "deferred": None,
        }
    )


class _ScriptedStageAdapter:
    """A fake stage-agent adapter that always returns the same session id
    (mirroring a real provider resuming the same conversation) and can be
    scripted to raise :class:`ProviderError` on a given (1-indexed) call
    number."""

    def __init__(
        self,
        *,
        session_id: str = "impl-sess",
        fail_at: int | None = None,
        is_error_at: int | None = None,
    ):
        self.session_id = session_id
        self.fail_at = fail_at
        self.is_error_at = is_error_at
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self._call_count = 0

    def start(self, prompt: str) -> StageAgentResult:
        self._call_count += 1
        self.start_calls.append(prompt)
        if self.fail_at == self._call_count:
            raise ProviderError("stage provider boom")
        is_error = self.is_error_at == self._call_count
        return StageAgentResult(session_id=self.session_id, text="did it", is_error=is_error)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self._call_count += 1
        self.resume_calls.append((session_id, prompt))
        if self.fail_at == self._call_count:
            raise ProviderError("stage provider boom")
        is_error = self.is_error_at == self._call_count
        return StageAgentResult(session_id=self.session_id, text="did more", is_error=is_error)


class _ScriptedSparringAdapter:
    """A fake sparring-agent adapter that always returns the same session id
    and yields verdicts from a scripted list in call order."""

    def __init__(self, verdicts: list[str], *, session_id: str = "spar-sess"):
        self.session_id = session_id
        self._verdicts = list(verdicts)
        self._index = 0
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []

    def _next_text(self) -> str:
        text = self._verdicts[self._index]
        self._index += 1
        return text

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        return SparringAgentResult(session_id=self.session_id, text=self._next_text(), is_error=False)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls.append((session_id, prompt))
        return SparringAgentResult(session_id=self.session_id, text=self._next_text(), is_error=False)


class _WritingSparringAdapter:
    """A fake sparring adapter that writes to the repo -- exactly what a
    read-only sparrer must never do -- used to exercise the read-only
    integrity failure path through the loop."""

    def __init__(self, repo: Path, *, filename: str = "sneaky.txt", session_id: str = "spar-sess"):
        self.repo = repo
        self.filename = filename
        self.session_id = session_id
        self.start_calls: list[str] = []

    def _write(self) -> SparringAgentResult:
        (self.repo / self.filename).write_text("written by a sparrer\n", encoding="utf-8")
        return SparringAgentResult(
            session_id=self.session_id, text=_verdict_text("READY", "looks fine"), is_error=False
        )

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        return self._write()

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        return self._write()


class UnattendedLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nDo the thing.\n", encoding="utf-8"
        )

    def test_ready_stops_after_one_cycle(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter([_verdict_text("READY", "looks good")])

        result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual(len(sparring_adapter.start_calls), 1)
        self.assertEqual(sparring_adapter.resume_calls, [])
        self.assertEqual(result.send_back_count, 0)
        self.assertEqual(len(result.cycles), 1)

    def test_send_back_resumes_same_implementation_and_sparring_sessions(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter(
            [_verdict_text("SEND_BACK", "fix x"), _verdict_text("READY", "fixed")]
        )

        result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(len(stage_adapter.resume_calls), 1)
        resumed_stage_session, _ = stage_adapter.resume_calls[0]
        self.assertEqual(resumed_stage_session, stage_adapter.session_id)

        self.assertEqual(len(sparring_adapter.start_calls), 1)
        self.assertEqual(len(sparring_adapter.resume_calls), 1)
        resumed_sparring_session, _ = sparring_adapter.resume_calls[0]
        self.assertEqual(resumed_sparring_session, sparring_adapter.session_id)

        state = self.stage.read_state()
        self.assertEqual(state.implementation_session_id, stage_adapter.session_id)
        self.assertEqual(state.sparring_session_id, sparring_adapter.session_id)

    def test_multiple_send_back_cycles_preserve_identity(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter(
            [
                _verdict_text("SEND_BACK", "fix 1"),
                _verdict_text("SEND_BACK", "fix 2"),
                _verdict_text("SEND_BACK", "fix 3"),
                _verdict_text("READY", "fixed for real"),
            ]
        )

        result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(result.send_back_count, 3)
        self.assertEqual(len(result.cycles), 4)
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(len(stage_adapter.resume_calls), 3)
        self.assertTrue(all(sid == stage_adapter.session_id for sid, _ in stage_adapter.resume_calls))
        self.assertEqual(len(sparring_adapter.start_calls), 1)
        self.assertEqual(len(sparring_adapter.resume_calls), 3)
        self.assertTrue(
            all(sid == sparring_adapter.session_id for sid, _ in sparring_adapter.resume_calls)
        )

    def test_needs_you_stops_without_resuming_implementation(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter(
            [_verdict_text("NEEDS_YOU", "choose between A and B")]
        )

        result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(result.outcome, RoutingAction.NEEDS_YOU)
        self.assertEqual(result.routing.summary, "choose between A and B")
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(stage_adapter.resume_calls, [])

    def test_escalate_stops_without_resuming_implementation(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter(
            [_verdict_text("ESCALATE", "needs stronger sparring")]
        )

        result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(result.outcome, RoutingAction.ESCALATE)
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(stage_adapter.resume_calls, [])

    def test_runaway_limit_stops_the_loop_cleanly(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter(
            [_verdict_text("SEND_BACK", f"fix {i}") for i in range(10)]
        )

        with self.assertRaises(LoopRunawayError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
                max_send_back_cycles=2,
            )

        # Exactly 3 full cycles ran (the 2 allowed SEND_BACKs plus the one
        # that discovers the limit is exceeded) -- not a 4th.
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(len(stage_adapter.resume_calls), 2)
        self.assertEqual(len(sparring_adapter.start_calls), 1)
        self.assertEqual(len(sparring_adapter.resume_calls), 2)

    def test_runaway_limit_default_is_a_small_positive_number(self):
        self.assertIsInstance(DEFAULT_MAX_SEND_BACK_CYCLES, int)
        self.assertGreater(DEFAULT_MAX_SEND_BACK_CYCLES, 0)

    def test_max_send_back_cycles_must_be_at_least_one(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _ScriptedSparringAdapter([_verdict_text("READY", "ok")])
        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
                max_send_back_cycles=0,
            )
        self.assertEqual(stage_adapter.start_calls, [])

    def test_stage_provider_failure_stops_the_loop_without_invoking_sparring(self):
        stage_adapter = _ScriptedStageAdapter(fail_at=1)
        sparring_adapter = _ScriptedSparringAdapter([_verdict_text("READY", "ok")])

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
            )

        self.assertEqual(sparring_adapter.start_calls, [])

    def test_sparring_read_only_integrity_failure_stops_the_loop(self):
        stage_adapter = _ScriptedStageAdapter()
        sparring_adapter = _WritingSparringAdapter(self.repo)

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
            )

        # The stage agent ran once (its turn is legitimate); the loop must
        # not silently continue or resume anything after the integrity
        # violation is discovered.
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertIsNone(self.stage.read_state().sparring_session_id)

    def test_stage_provider_reported_is_error_stops_loop_before_sparring(self):
        # ClaudeCliAdapter's StageAgentResult.is_error reflects the
        # provider's own machine-readable output; a turn that completes
        # without raising but reports is_error=true must not be sent to
        # the sparrer as though it succeeded.
        stage_adapter = _ScriptedStageAdapter(session_id="impl-sess", is_error_at=1)
        sparring_adapter = _ScriptedSparringAdapter([_verdict_text("READY", "ok")])

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
            )

        # The sparring agent must never see a failed implementation turn.
        self.assertEqual(sparring_adapter.start_calls, [])
        self.assertEqual(sparring_adapter.resume_calls, [])

        # The real provider-issued implementation session id, already
        # recorded by run_stage_agent before the loop's is_error check
        # runs, must not be discarded -- a later deliberate retry can
        # resume it.
        self.assertEqual(self.stage.read_state().implementation_session_id, "impl-sess")

    def test_second_send_back_cycle_after_first_failure_type_is_not_attempted(self):
        # A stage agent that fails on its SECOND call (the resumed turn)
        # must stop the loop cleanly too -- not just a first-call failure.
        stage_adapter = _ScriptedStageAdapter(fail_at=2)
        sparring_adapter = _ScriptedSparringAdapter(
            [_verdict_text("SEND_BACK", "fix x"), _verdict_text("READY", "fixed")]
        )

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
            )

        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(len(stage_adapter.resume_calls), 1)
        # The sparring agent must not have been invoked a second time.
        self.assertEqual(len(sparring_adapter.start_calls), 1)
        self.assertEqual(sparring_adapter.resume_calls, [])


if __name__ == "__main__":
    unittest.main()
