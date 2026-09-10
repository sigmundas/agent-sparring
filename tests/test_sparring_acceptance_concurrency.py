"""Regressions for serializing the sparring agent's state.json write with
freeze/accept via the shared worktree lock.

Before this fix, run_sparring_agent read state.json, ran a potentially long
provider turn, and only then wrote sparring_session_id back onto the object
it read at the start -- with no lock held for any of it. A concurrent
freeze_candidate/accept_candidate that completed *during* that window would
have its lifecycle transition silently reversed when the sparring turn's
stale object was written back afterward (ACCEPTED -> FROZEN, or FROZEN ->
WORKING).

These tests prove the fix using lock coordination, not timing sleeps: a
fake sparring adapter's own start() call -- invoked from inside
run_sparring_agent while it still holds the worktree lock -- reentrantly
attempts the "concurrent" freeze/accept operation in the same process and
captures what happens. Because both operations serialize on the same
non-blocking worktree_lock, that reentrant attempt is refused immediately
(lock contention) rather than racing to completion, which is exactly what
makes the reversal impossible.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.acceptance import (
    AcceptanceError,
    accept_candidate,
    freeze_candidate,
)
from agent_sparring.loop import run_unattended_loop
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_agent import run_sparring_agent
from agent_sparring.stage import Stage, StageStatus


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _head_sha(repo: Path) -> str:
    return _git_out(repo, "rev-parse", "HEAD")


def _verdict_text(action: str, summary: str) -> str:
    import json

    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": None,
            "findings": summary,
            "deferred": None,
        }
    )


class _ReentrantAdapter:
    """A fake sparring adapter whose ``start()`` reentrantly calls a
    "concurrent" acceptance operation while the caller (run_sparring_agent)
    still holds the worktree lock, then returns a normal verdict.

    Captures the exception the reentrant call raised (or, if it didn't
    raise, that fact -- a bug, since it should always be refused as
    contended while the lock is held).
    """

    def __init__(self, reentrant_call, *, session_id: str = "spar-sess", verdict: str):
        self._reentrant_call = reentrant_call
        self.session_id = session_id
        self.verdict = verdict
        self.reentrant_error: Exception | None = None
        self.reentrant_succeeded = False
        self.start_calls = 0

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls += 1
        try:
            self._reentrant_call()
            self.reentrant_succeeded = True
        except Exception as exc:  # noqa: BLE001 - captured for assertion, not swallowed
            self.reentrant_error = exc
        return SparringAgentResult(session_id=self.session_id, text=self.verdict, is_error=False)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        raise AssertionError("resume should not be called in these tests")


class _FixedSparringAdapter:
    """A plain, non-reentrant fake sparring adapter for the ACCEPTED-stage
    and loop-deadlock tests below."""

    def __init__(self, verdicts: list[str], *, session_id: str = "spar-sess"):
        self._verdicts = list(verdicts)
        self.session_id = session_id
        self._index = 0
        self.start_calls = 0
        self.resume_calls = 0

    def _next(self) -> SparringAgentResult:
        text = self._verdicts[self._index]
        self._index += 1
        return SparringAgentResult(session_id=self.session_id, text=text, is_error=False)

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls += 1
        return self._next()

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls += 1
        return self._next()


class _FixedStageAdapter:
    """A plain fake stage-agent adapter that always returns the same
    session id and never fails -- used only to exercise the loop's
    sequencing, not any implementation-side behavior."""

    def __init__(self, *, session_id: str = "impl-sess"):
        self.session_id = session_id
        self.start_calls = 0
        self.resume_calls = 0

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls += 1
        return StageAgentResult(session_id=self.session_id, text="did it", is_error=False)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls += 1
        return StageAgentResult(session_id=self.session_id, text="did more", is_error=False)


class SparringAcceptanceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )
        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = _head_sha(self.repo)

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def _freeze(self):
        return freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )

    def _accept(self):
        return accept_candidate(self.stage, self.repo, expected_branch="feature/x")

    # -- Finding: sparring must not reverse a concurrent ACCEPTED transition

    def test_sparring_turn_from_frozen_cannot_reverse_a_concurrent_acceptance(self):
        self._freeze()
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

        adapter = _ReentrantAdapter(
            self._accept, verdict=_verdict_text("READY", "looks fine")
        )

        result = run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        # The "concurrent" acceptance attempted from inside the sparring
        # turn was refused as contended -- it could not interleave.
        self.assertIsNotNone(adapter.reentrant_error)
        self.assertFalse(adapter.reentrant_succeeded)
        self.assertIsInstance(adapter.reentrant_error, AcceptanceError)

        # The sparring turn itself completed normally and did not touch
        # lifecycle status -- it stayed exactly FROZEN, never reversed to
        # or from ACCEPTED.
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, self.candidate_sha)
        self.assertEqual(state.sparring_session_id, adapter.session_id)
        self.assertEqual(result.routing.action, RoutingAction.READY)

        # Acceptance, attempted for real afterward (lock now free), succeeds
        # and is not clobbered by anything the sparring turn wrote.
        accept_result = self._accept()
        self.assertEqual(accept_result.candidate_sha, self.candidate_sha)
        final_state = self.stage.read_state()
        self.assertEqual(final_state.status, StageStatus.ACCEPTED)
        self.assertEqual(final_state.candidate_sha, self.candidate_sha)
        # The sparring session id recorded during the FROZEN turn survives
        # the later acceptance untouched.
        self.assertEqual(final_state.sparring_session_id, adapter.session_id)

    # -- Finding: sparring must not reverse a concurrent freeze with stale
    #    WORKING state

    def test_sparring_turn_from_working_cannot_reverse_a_concurrent_freeze(self):
        self.assertEqual(self.stage.read_state().status, StageStatus.WORKING)

        adapter = _ReentrantAdapter(
            self._freeze, verdict=_verdict_text("SEND_BACK", "needs a fix")
        )

        result = run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertIsNotNone(adapter.reentrant_error)
        self.assertFalse(adapter.reentrant_succeeded)
        self.assertIsInstance(adapter.reentrant_error, AcceptanceError)

        # Sparring never touches status/candidate_sha itself -- it should
        # still be WORKING, not silently frozen-then-reverted, and not
        # anything but WORKING (the freeze that ran reentrantly was
        # refused, so there is nothing to "revert" -- this asserts the
        # state sparring wrote is exactly what was there before, apart from
        # its own field).
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.WORKING)
        self.assertIsNone(state.candidate_sha)
        self.assertEqual(state.sparring_session_id, adapter.session_id)
        self.assertEqual(result.routing.action, RoutingAction.SEND_BACK)

        # Freezing for real afterward succeeds normally and is not stale
        # WORKING data written by the sparring turn.
        freeze_result = self._freeze()
        self.assertEqual(freeze_result.candidate_sha, self.candidate_sha)
        final_state = self.stage.read_state()
        self.assertEqual(final_state.status, StageStatus.FROZEN)
        self.assertEqual(final_state.candidate_sha, self.candidate_sha)
        self.assertEqual(final_state.sparring_session_id, adapter.session_id)

    # -- Sparring an ACCEPTED stage remains permitted and non-destructive

    def test_sparring_an_accepted_stage_preserves_accepted_status_and_sha(self):
        self._freeze()
        self._accept()
        accepted_state = self.stage.read_state()
        self.assertEqual(accepted_state.status, StageStatus.ACCEPTED)

        adapter = _FixedSparringAdapter([_verdict_text("READY", "post-acceptance review")])
        result = run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(result.routing.action, RoutingAction.READY)
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.ACCEPTED)
        self.assertEqual(state.candidate_sha, self.candidate_sha)
        self.assertEqual(state.sparring_session_id, adapter.session_id)
        self.assertIn("post-acceptance review", self.stage.read_sparring())

    # -- The Stage 5 loop still completes with no deadlock

    def test_send_back_then_ready_loop_completes_without_deadlock(self):
        stage_adapter = _FixedStageAdapter()
        sparring_adapter = _FixedSparringAdapter(
            [_verdict_text("SEND_BACK", "fix x"), _verdict_text("READY", "fixed")]
        )

        loop_result = run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
        )

        self.assertEqual(loop_result.outcome, RoutingAction.READY)
        self.assertEqual(len(loop_result.cycles), 2)
        self.assertEqual(stage_adapter.start_calls, 1)
        self.assertEqual(stage_adapter.resume_calls, 1)
        self.assertEqual(sparring_adapter.start_calls, 1)
        self.assertEqual(sparring_adapter.resume_calls, 1)


if __name__ == "__main__":
    unittest.main()
