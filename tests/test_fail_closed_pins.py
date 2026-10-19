"""Fail closed on missing pins; an acknowledgement is not authorization.

What these tests hold, one sentence each:

- a run the engine's record outside the project directory calls pinned,
  whose pin is gone (state deleted, ``remote_pin`` removed, or a
  legacy-shaped state in its place), refuses ``finish-run`` with
  ``remote_pin_missing`` and touches no ref of either remote;
- freeze/accept of a stage resolve its owner from engine run state and,
  for an unpinned legacy run, refuse rather than fall back to reachability;
- an acknowledgement is a baseline for further movement, never an
  explanation of a candidate -- not even one behind the acknowledged tip;
- a push raced by someone else between the movement check and the engine's
  push is movement, not a receipt;
- a turn that raises after an out-of-band push is still checked, so the
  movement pause comes before any later turn.

Real repositories and real bare remotes throughout; no network, no provider.
"""

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import engine_git, managed_run
from agent_sparring.cli import main
from agent_sparring.plan import PlanRunStatus, resume_plan
from agent_sparring.providers import ProviderError
from agent_sparring.push_gate import authorization_for_run, ensure_candidate_pushed
from agent_sparring.remote_pin import (
    AUTHORIZED_WITHOUT_PUSH,
    PUSHED_CANDIDATE_UNAUTHORIZED,
    REMOTE_PIN_MISSING,
    UNAUTHORIZED_REMOTE_MOVEMENT,
    PushedCandidateUnauthorized,
    UnauthorizedRemoteMovement,
    acknowledgement,
    authorization_without_push,
    check_movement,
    pin_remote,
)
from agent_sparring.stage import StageStatus
from test_engine_git_authority import _AuthorityCase, _remote_sha
from test_independent_review import REVIEW_STAGE, _ReviewRepoTestCase
from test_managed_finish import _FinishTestCase
from test_managed_run import _git
from test_plan import READY, S1, _SparringAdapter, _StageAdapter, _fixed, _head_sha, _run_git


# -- a pinned run whose pin is gone --------------------------------------------


class FinishWithoutThePinTests(_FinishTestCase):
    """``finish-run --push-target`` of a pinned managed run, with a planted
    ``pushurl`` it would otherwise follow."""

    def _finish(self, record, *flags: str):
        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--json", *flags
        )
        return code, (json.loads(out) if out.strip() else None), err

    def _events(self, record):
        return [e["event"] for e in managed_run.read_record(self.repo, record.run_key).events]

    def _refs_everywhere(self, mirror: Path) -> tuple[str, str, str]:
        return (_git(self.remote, "for-each-ref"), _git(mirror, "for-each-ref"), _git(self.repo, "for-each-ref"))

    def _refused(self, damage) -> None:
        record, head = self._complete()
        self.assertTrue(managed_run.read_record(self.repo, record.run_key).pinned)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        damage(state_path)
        mirror = self.remote.parent / "push.git"
        subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True, capture_output=True)
        _run_git(self.repo, "remote", "set-url", "--push", "origin", str(mirror))
        before = self._refs_everywhere(mirror)

        code, report, err = self._finish(record, "--push-target")

        self.assertEqual(code, 1, err)
        self.assertEqual(report["stopped_at"], "checks")
        self.assertIn(REMOTE_PIN_MISSING, report["reason"])
        self.assertEqual(report["completed_steps"], [])
        # Nothing merged, pushed or deleted: every ref of both remotes, and
        # of the repository itself, is as it was.
        self.assertEqual(self._refs_everywhere(mirror), before)
        self.assertEqual(_git(self.remote, "rev-parse", f"refs/heads/{record.branch}"), head)
        self.assertNotIn("merged", self._events(record))
        self.assertTrue(Path(record.worktree_path).exists())

    def test_the_state_file_deleted(self):
        self._refused(lambda path: path.unlink())

    def test_the_pin_removed_from_the_state(self):
        def drop_pin(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("remote_pin")
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        self._refused(drop_pin)

    def test_the_state_replaced_with_a_legacy_shaped_one(self):
        def legacy(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            shaped = {
                key: payload[key]
                for key in (
                    "plan", "plan_digest", "expected_branch", "current_stage_index",
                    "current_stage", "status",
                )
            }
            path.write_text(json.dumps(shaped, indent=2) + "\n", encoding="utf-8")

        self._refused(legacy)

    def test_the_state_file_unreadable(self):
        self._refused(lambda path: path.write_text("{broken", encoding="utf-8"))

    def test_the_pin_malformed(self):
        def malformed(path: Path) -> None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["remote_pin"] = {"branch": 7}
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        self._refused(malformed)

    def test_a_record_from_before_pins_is_the_one_unguarded_case(self):
        record, _ = self._complete()
        payload = json.loads(managed_run.record_path(self.repo, record.run_key).read_text(encoding="utf-8"))
        self.assertIs(payload["pinned"], True)
        legacy = managed_run.ManagedRunRecord.from_dict({k: v for k, v in payload.items() if k != "pinned"})
        self.assertFalse(legacy.pinned)
        self.assertNotIn("pinned", legacy.to_dict())


# -- ownership and legacy runs at freeze/accept ---------------------------------


class LegacyAcceptTests(_AuthorityCase):
    GRANT_PUSH_FOR_RUN = False

    def _legacy_with_the_candidate_on_the_remote(self) -> str:
        """A run that stopped for push permission; the candidate then pushed
        by hand; the run state rewritten as an engine from before pins wrote
        it, with no engine record of a pin; and the stage frozen as such an
        engine could have left it."""

        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]), stop_after_stage=S1)
        head = _head_sha(self.repo)
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        del payload["remote_pin"]
        payload.pop("remote_records", None)
        payload["awaiting"] = None
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        managed_run.run_flag_path(self.repo, self.run_key).unlink()
        stage = self._stage(S1)
        state = stage.read_state()
        state.status = StageStatus.FROZEN
        state.candidate_sha = head
        stage.write_state(state)
        return head

    def _accept(self) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "accept-candidate", S1,
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            ])
        return code, err.getvalue()

    def test_accept_candidate_on_an_unpinned_legacy_run_refuses(self):
        self._legacy_with_the_candidate_on_the_remote()

        code, err = self._accept()

        self.assertEqual(code, 1)
        self.assertIn("recorded before runs pinned", err)
        self.assertIn("reachability alone is never taken as pushed", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    def test_an_emptied_owner_field_does_not_make_the_stage_hand_driven(self):
        self._legacy_with_the_candidate_on_the_remote()
        stage = self._stage(S1)
        state = stage.read_state()
        state.run = None
        stage.write_state(state)

        code, err = self._accept()

        # The run state is positioned at the stage, so the run owns it.
        self.assertEqual(code, 1)
        self.assertIn("recorded before runs pinned", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    def _freeze_refused_after(self, damage) -> None:
        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]), stop_after_stage=S1)
        damage(self.state_path)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "freeze-candidate", S1,
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            ])

        self.assertEqual(code, 1)
        self.assertIn(REMOTE_PIN_MISSING, err.getvalue())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def _rewrite(self, path: Path, change) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        change(payload)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_a_pinned_run_whose_pin_is_gone_refuses_freeze(self):
        self._freeze_refused_after(lambda path: self._rewrite(path, lambda p: p.pop("remote_pin")))

    def test_a_pinned_run_whose_pin_is_malformed_refuses_freeze(self):
        self._freeze_refused_after(
            lambda path: self._rewrite(path, lambda p: p.__setitem__("remote_pin", {"branch": 7}))
        )

    def test_a_pinned_run_whose_state_is_unreadable_refuses_freeze(self):
        self._freeze_refused_after(lambda path: path.write_text("{broken", encoding="utf-8"))

    def test_resuming_a_pinned_run_whose_pin_is_gone_does_not_re_pin_it(self):
        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]), stop_after_stage=S1)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        del payload["remote_pin"]
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        stage_adapter = _StageAdapter(self.repo)
        with self.assertRaises(Exception) as ctx:
            self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        self.assertIn(REMOTE_PIN_MISSING, str(ctx.exception))
        self.assertNotIn("remote_pin", json.loads(self.state_path.read_text(encoding="utf-8")))
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])


# -- what an acknowledgement explains -------------------------------------------


class AcknowledgementExplainsNoCandidateTests(_AuthorityCase):
    def test_an_acknowledged_tip_ahead_of_the_candidate_explains_no_candidate(self):
        pin = pin_remote(self.repo, "feature/x")
        first = self.commit("one.txt")
        second = self.commit("two.txt")
        ahead = self.commit("three.txt")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        with self.guard(pin):
            _observed, movement = check_movement(self.repo, pin, (), stage_id=S1, candidate_sha=first)
            self.assertIsInstance(movement, UnauthorizedRemoteMovement)
            records = (acknowledgement(run=self.run_key, movement=movement, by="tester"),)

            # The acknowledgement is the baseline: the ref is where it says...
            _observed, nothing = check_movement(self.repo, pin, records, stage_id=S1)
            self.assertIsNone(nothing)
            # ...but no candidate behind it is explained by it.
            for candidate in (first, second, ahead):
                _observed, pause = check_movement(
                    self.repo, pin, records, stage_id=S1, candidate_sha=candidate
                )
                self.assertIsInstance(pause, PushedCandidateUnauthorized, candidate)
                self.assertEqual(pause.kind, PUSHED_CANDIDATE_UNAUTHORIZED)
                self.assertEqual(pause.candidate_sha, candidate)

            # A per-commit authorization explains exactly its commit, and a
            # later, different candidate is still unexplained.
            _observed, first_pause = check_movement(
                self.repo, pin, records, stage_id=S1, candidate_sha=first
            )
            authorized = records + (
                authorization_without_push(run=self.run_key, pause=first_pause, by="tester"),
            )
            self.assertEqual(authorized[-1].kind, AUTHORIZED_WITHOUT_PUSH)
            _observed, cleared = check_movement(
                self.repo, pin, authorized, stage_id=S1, candidate_sha=first
            )
            self.assertIsNone(cleared)
            _observed, later = check_movement(
                self.repo, pin, authorized, stage_id=S1, candidate_sha=second
            )
            self.assertIsInstance(later, PushedCandidateUnauthorized)
            self.assertEqual(later.candidate_sha, second)


# -- a push raced between the check and the engine's push ------------------------


class PushRaceTests(_AuthorityCase):
    def _racing(self, agent_pushes: str):
        """``engine_git.run_remote``, with an agent's push of
        ``agent_pushes`` inserted just before the engine's own push -- after
        every check the engine made. Reachable only by patching it here."""

        real = engine_git.run_remote
        raced: list[str] = []

        def run_remote(repo_root, trusted, *args, **kwargs):
            if "push" in args and not raced:
                raced.append(agent_pushes)
                _run_git(self.repo, "push", "-q", "origin", f"{agent_pushes}:refs/heads/feature/x")
            return real(repo_root, trusted, *args, **kwargs)

        return mock.patch.object(engine_git, "run_remote", side_effect=run_remote), raced

    def _push(self, pin, candidate: str, agent_pushes: str):
        patch, raced = self._racing(agent_pushes)
        authorization = authorization_for_run(self.repo, branch="feature/x", pin=pin)
        with self.guard(pin), patch:
            outcome = ensure_candidate_pushed(
                self.repo, stage_id=S1, expected_branch="feature/x",
                authorization=authorization, pin=pin, run=self.run_key,
            )
        self.assertEqual(raced, [agent_pushes])
        self.assertEqual(_head_sha(self.repo), candidate)
        return outcome

    def test_an_agent_pushing_the_same_commit_first_makes_the_push_a_no_op_and_movement(self):
        pin = pin_remote(self.repo, "feature/x")
        candidate = self.commit("one.txt")

        outcome = self._push(pin, candidate, agent_pushes=candidate)

        self.assertIsNone(outcome.receipt)
        self.assertFalse(outcome.pushed)
        self.assertIsInstance(outcome.movement, UnauthorizedRemoteMovement)
        self.assertEqual(outcome.movement.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(outcome.movement.expected_tip, self.remote_before)
        self.assertEqual(outcome.movement.observed_tip, candidate)
        self.assertIn("no-op", outcome.movement.detail)

    def test_an_agent_pushing_an_ancestor_first_is_movement_not_a_receipt(self):
        pin = pin_remote(self.repo, "feature/x")
        ancestor = self.commit("one.txt")
        candidate = self.commit("two.txt")

        outcome = self._push(pin, candidate, agent_pushes=ancestor)

        self.assertIsNone(outcome.receipt)
        self.assertIsInstance(outcome.movement, UnauthorizedRemoteMovement)
        self.assertEqual(outcome.movement.expected_tip, self.remote_before)
        self.assertEqual(outcome.movement.observed_tip, candidate)
        self.assertEqual(_remote_sha(self.remote, "feature/x"), candidate)

    def test_an_unraced_push_still_makes_a_receipt(self):
        pin = pin_remote(self.repo, "feature/x")
        candidate = self.commit("one.txt")
        authorization = authorization_for_run(self.repo, branch="feature/x", pin=pin)
        with self.guard(pin):
            outcome = ensure_candidate_pushed(
                self.repo, stage_id=S1, expected_branch="feature/x",
                authorization=authorization, pin=pin, run=self.run_key,
            )
        self.assertTrue(outcome.pushed)
        self.assertIsNone(outcome.movement)
        self.assertEqual((outcome.receipt.previous_tip, outcome.receipt.tip), (self.remote_before, candidate))

    def test_a_raced_engine_push_in_a_run_pauses_it_on_movement(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        patch, raced = self._racing_head()
        with patch:
            result = self._start(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)

        self.assertEqual(len(raced), 1)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(self._plan_state().remote_records, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def _racing_head(self):
        """As :meth:`_racing`, pushing whatever HEAD is when the engine
        pushes -- the candidate itself."""

        real = engine_git.run_remote
        raced: list[str] = []

        def run_remote(repo_root, trusted, *args, **kwargs):
            if "push" in args and not raced:
                raced.append(_head_sha(self.repo))
                _run_git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
            return real(repo_root, trusted, *args, **kwargs)

        return mock.patch.object(engine_git, "run_remote", side_effect=run_remote), raced


# -- a turn that raises -----------------------------------------------------------


class _PushesThenFails(_StageAdapter):
    """Commits, pushes on its own, and then fails the turn."""

    def _turn(self, session_id):
        super()._turn(session_id)
        raise ProviderError("the provider timed out after the agent pushed")


class _Interrupted(BaseException):
    """What an interrupt or a hard timeout looks like to the loop: not an
    ``Exception``. (Not ``KeyboardInterrupt`` itself, so a regression fails
    this test instead of stopping the test session.)"""


class _PushesThenIsInterrupted(_StageAdapter):
    def _turn(self, session_id):
        super()._turn(session_id)
        raise _Interrupted("interrupted after the agent pushed")


class FailedTurnTests(_AuthorityCase):
    GRANT_PUSH_FOR_RUN = False

    def test_an_interrupted_turn_after_an_out_of_band_push_is_still_checked(self):
        result = self._start(
            _PushesThenIsInterrupted(self.repo, commit=True, push=True),
            _SparringAdapter([READY]),
            stop_after_stage=S1,
        )

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(result.awaiting.observed_tip, _head_sha(self.repo))
        stage_adapter, sparring_adapter = _StageAdapter(self.repo, commit=True), _SparringAdapter([READY])
        again = resume_plan(
            self.plan_path, self.sparring_dir, self.repo, _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x", stop_after_stage=S1,
        )
        self.assertEqual(again.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(sparring_adapter.start_calls + sparring_adapter.resume_calls, [])

    def test_a_turn_that_raises_after_an_out_of_band_push_is_still_checked(self):
        failing = _PushesThenFails(self.repo, commit=True, push=True)

        result = self._start(failing, _SparringAdapter([READY]), stop_after_stage=S1)

        pushed = _head_sha(self.repo)
        self.assertEqual(_remote_sha(self.remote, "feature/x"), pushed)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(result.awaiting.observed_tip, pushed)

        # On resume, the movement pause comes before any stage-agent turn.
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY])
        again = resume_plan(
            self.plan_path, self.sparring_dir, self.repo, _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x", stop_after_stage=S1,
        )
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        self.assertEqual(again.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(sparring_adapter.start_calls + sparring_adapter.resume_calls, [])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)


class _ReviewerPushesThenFails(_SparringAdapter):
    """READY for the build stage; then, as the independent reviewer, a
    commit is pushed out of band and the reviewer's turn fails."""

    def __init__(self, repo: Path, verdicts):
        super().__init__(verdicts)
        self.repo = repo
        self.calls = 0

    def _next(self, session_id):
        self.calls += 1
        if self.calls == 2:
            tree = subprocess.run(
                ["git", "-C", str(self.repo), "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "out of band"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            _run_git(self.repo, "push", "-q", "origin", f"{tree}:refs/heads/feature/x")
            raise ProviderError("the reviewer's provider timed out")
        return super()._next(session_id)


class FailedReviewTurnTests(_ReviewRepoTestCase):
    def test_a_failed_review_only_turn_is_still_checked_and_resume_runs_nothing(self):
        result = self.start(
            _StageAdapter(self.repo, commit=True), _ReviewerPushesThenFails(self.repo, [READY])
        )

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, REVIEW_STAGE)
        self.assertEqual(result.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(self.plan_state().awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)

        stage_adapter, sparring_adapter = _StageAdapter(self.repo, commit=True), _SparringAdapter([READY])
        again = self.resume(stage_adapter, sparring_adapter)
        self.assertEqual(again.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(sparring_adapter.start_calls + sparring_adapter.resume_calls, [])
        self.assertIs(self.stage(REVIEW_STAGE).read_state().status, StageStatus.WORKING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
