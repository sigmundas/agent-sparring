"""Fail closed on missing pins; an acknowledgement is not authorization.

What these tests hold, one sentence each:

- a legacy run the engine's record outside the project directory calls
  pinned, whose pin is gone (state deleted, ``remote_pin`` removed, or a
  legacy-shaped state in its place), refuses ``finish-run`` with
  ``remote_pin_missing`` (exit 3) and touches no ref of either remote
  (Stage 4 of Plan L replaces this with the run-state integrity refusal);
- Plan L: freeze/accept resolve a stage's owner from engine run state and
  are decided locally -- reachability is never an input -- but never
  unguarded: a trusted snapshot (a legacy pin's) that is gone or damaged
  refuses with ``trusted_config_missing``, and a run from before snapshots
  refuses until a resume records one;
- an acknowledgement is a baseline for further movement, never an
  explanation of a candidate -- not even one behind the acknowledged tip;
- a push raced by someone else between the movement check and the engine's
  push primitive is movement, not a receipt (the primitive is kept for
  Plan P); a run never calls that primitive;
- Plan L: a turn that raises after an out-of-band push fails exactly as it
  would without the push -- runs no longer monitor remote movement.

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

from agent_sparring import acceptance, engine_git, managed_run
from agent_sparring.cli import main
from agent_sparring.plan import PlanError, PlanRunError, PlanRunStatus, resume_plan
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

    def _mirror(self) -> Path:
        mirror = self.remote.parent / "push.git"
        subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True, capture_output=True)
        return mirror

    def _refused(self, damage) -> None:
        record, head = self._complete()
        self.assertTrue(managed_run.read_record(self.repo, record.run_key).pinned)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        damage(state_path)
        mirror = self._mirror()
        _run_git(self.repo, "remote", "set-url", "--push", "origin", str(mirror))
        before = self._refs_everywhere(mirror)

        code, report, err = self._finish(record, "--push-target")

        # 3: refused by its checks (docs/plans.md, finish-run).
        self.assertEqual(code, 3, err)
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

    def test_an_intact_pin_finishes_and_the_mirror_stays_empty(self):
        # Positive control for the five refusals above: the same run, its
        # pin intact and no pushurl planted, does finish -- so their exit 3
        # is the missing pin, not a fixture that could never finish.
        record, head = self._complete()
        self.assertTrue(managed_run.read_record(self.repo, record.run_key).pinned)
        mirror = self._mirror()

        code, report, err = self._finish(record, "--push-target")

        self.assertEqual(code, 0, err)
        self.assertIn("merged", self._events(record))
        self.assertEqual(_git(self.remote, "rev-parse", "refs/heads/main"), head)
        self.assertEqual(_git(mirror, "for-each-ref"), "")

    def test_a_record_from_before_pins_is_the_one_unguarded_case(self):
        record, _ = self._complete()
        payload = json.loads(managed_run.record_path(self.repo, record.run_key).read_text(encoding="utf-8"))
        self.assertIs(payload["pinned"], True)
        legacy = managed_run.ManagedRunRecord.from_dict({k: v for k, v in payload.items() if k != "pinned"})
        self.assertFalse(legacy.pinned)
        self.assertNotIn("pinned", legacy.to_dict())


# -- ownership and legacy runs at freeze/accept ---------------------------------


class LegacyAcceptTests(_AuthorityCase):
    """Plan L: freeze and accept are local (I-L2) -- reachability is never an
    input -- but never unguarded (I-L4). An owned stage's run must hold the
    trusted configuration snapshot its Git commands are compared with: a
    local-first run's own, or a legacy run's pin. A snapshot the engine's
    outside record says existed, and that is gone or damaged, refuses
    (``trusted_config_missing``); a run from before any snapshot refuses
    until a resume records one. The owning run is resolved from engine run
    state, because its snapshot, accepted history and any recorded remote
    pause apply to the stage."""

    GRANT_PUSH_FOR_RUN = False

    def _ready_not_accepted(self) -> str:
        """The reviewer said READY over a commit and the run stopped before
        the acceptance gate (here: interrupted)."""

        with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]))
        return _head_sha(self.repo)

    def _legacy_pinned(self) -> None:
        """The run state rewritten as a pinning engine left it: a remote pin
        and the engine's record outside the project directory that it was
        pinned, and no local-first snapshot."""

        pin = pin_remote(self.repo, "feature/x")
        self._rewrite(self.state_path, lambda p: (p.pop("config_snapshot", None),
                                                  p.__setitem__("remote_pin", pin.to_dict())))
        managed_run.snapshot_flag_path(self.repo, self.run_key).unlink(missing_ok=True)
        managed_run.mark_pinned(self.repo, self.run_key)

    def _from_before_snapshots(self) -> None:
        """The run state as an engine from before pins left it: no pin, no
        snapshot, and no engine record outside the project directory of
        either."""

        self._rewrite(self.state_path, lambda p: (p.pop("config_snapshot", None), p.pop("remote_pin", None)))
        managed_run.snapshot_flag_path(self.repo, self.run_key).unlink(missing_ok=True)
        managed_run.run_flag_path(self.repo, self.run_key).unlink(missing_ok=True)

    def _legacy_with_the_candidate_on_the_remote(self, *, pinned: bool = False) -> str:
        """A run recorded by an older engine -- pinned, or from before pins --
        its candidate pushed by hand, and the stage frozen as such an engine
        could have left it."""

        head = self._ready_not_accepted()
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        if pinned:
            self._legacy_pinned()
        else:
            self._from_before_snapshots()
        stage = self._stage(S1)
        state = stage.read_state()
        state.status = StageStatus.FROZEN
        state.candidate_sha = head
        stage.write_state(state)
        return head

    def _with_a_recorded_movement_pause(self) -> None:
        movement = UnauthorizedRemoteMovement(
            stage_id=S1, remote="origin", url=str(self.remote), ref="refs/heads/feature/x",
            expected_tip=self.remote_before, observed_tip=_head_sha(self.repo),
        )
        self._rewrite(self.state_path, lambda p: p.__setitem__("awaiting", movement.to_dict()))

    def _accept(self) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "accept-candidate", S1,
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            ])
        return code, err.getvalue()

    def test_accept_candidate_on_an_unpinned_legacy_run_refuses(self):
        # Still refuses -- never falling back to reachability -- now for want
        # of a trusted snapshot (Plan L), and the refusal says a resume
        # records one.
        self._legacy_with_the_candidate_on_the_remote()

        code, err = self._accept()

        self.assertEqual(code, 1)
        self.assertIn("trusted_config_missing", err)
        self.assertIn("resume the run", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    def test_after_a_resume_records_a_snapshot_the_legacy_run_is_decided_locally(self):
        # Positive control for the refusal above: the resume records the
        # snapshot (visibly), and acceptance then follows on the local rules
        # and the reviewer's READY -- with the candidate deleted from the
        # remote, which is not an input.
        head = self._legacy_with_the_candidate_on_the_remote()
        _run_git(self.repo, "push", "-q", "origin", "--delete", "feature/x")
        reports: list[str] = []

        self._resume(_StageAdapter(self.repo), _SparringAdapter([]), stop_after_stage=S1,
                     report=reports.append)

        self.assertTrue(any("weaker than a run-start snapshot" in line for line in reports), reports)
        self.assertIn("config_snapshot", json.loads(self.state_path.read_text(encoding="utf-8")))
        state = self._stage(S1).read_state()
        self.assertIs(state.status, StageStatus.ACCEPTED)
        self.assertEqual(state.candidate_sha, head)

    def test_an_emptied_owner_field_does_not_make_the_stage_hand_driven(self):
        # The run state is positioned at the stage, so the run owns it, and
        # the remote incident it recorded still refuses acceptance.
        self._legacy_with_the_candidate_on_the_remote(pinned=True)
        self._with_a_recorded_movement_pause()
        stage = self._stage(S1)
        state = stage.read_state()
        state.run = None
        stage.write_state(state)

        code, err = self._accept()

        self.assertEqual(code, 1)
        self.assertIn(UNAUTHORIZED_REMOTE_MOVEMENT, err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    def test_without_the_owner_check_the_stage_would_be_accepted(self):
        # Positive control for the refusal above: with the owning-run lookup
        # disabled in this test only, the same frozen stage is accepted --
        # the run's recorded pause would no longer apply to it.
        self._legacy_with_the_candidate_on_the_remote(pinned=True)
        self._with_a_recorded_movement_pause()
        stage = self._stage(S1)
        state = stage.read_state()
        state.run = None
        stage.write_state(state)

        with mock.patch.object(acceptance, "_owning_run", return_value=None):
            code, err = self._accept()

        self.assertEqual(code, 0, err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

    def _freeze(self) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "freeze-candidate", S1,
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            ])
        return code, err.getvalue()

    def _freeze_after(self, damage) -> tuple[int, str]:
        self._ready_not_accepted()
        self._legacy_pinned()
        damage(self.state_path)
        return self._freeze()

    def _rewrite(self, path: Path, change) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        change(payload)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # A pinned legacy run's pin carries its trusted snapshot. Gone or
    # damaged, it is never read as "no snapshot, so no guard": freeze refuses
    # with trusted_config_missing (it used to say remote_pin_missing).

    def test_a_pinned_run_whose_pin_is_gone_refuses_freeze(self):
        code, err = self._freeze_after(lambda path: self._rewrite(path, lambda p: p.pop("remote_pin")))
        self.assertEqual(code, 1)
        self.assertIn("trusted_config_missing", err)
        self.assertIn("not taken again", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_a_pinned_run_whose_pin_is_malformed_refuses_freeze(self):
        code, err = self._freeze_after(
            lambda path: self._rewrite(path, lambda p: p.__setitem__("remote_pin", {"branch": 7}))
        )
        self.assertEqual(code, 1)
        self.assertIn("trusted_config_missing", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_a_pinned_run_whose_state_is_unreadable_refuses_freeze(self):
        code, err = self._freeze_after(lambda path: path.write_text("{broken", encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read run state", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_positive_control_the_intact_legacy_state_freezes(self):
        # The three refusals above are the damage, not the fixture.
        code, err = self._freeze_after(lambda path: None)
        self.assertEqual(code, 0, err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    def test_damaging_the_pin_does_not_let_a_changed_configuration_through(self):
        # The point of refusing: with the pin intact, a watched setting
        # changed since the run started is refused by name...
        code, err = self._freeze_after(
            lambda path: _run_git(self.repo, "config", "core.sshCommand", "/bin/false")
        )
        self.assertEqual(code, 1)
        self.assertIn("repository_config_changed", err)
        # ...and damaging the pin as well does not turn that into an
        # unguarded freeze.
        self._rewrite(self.state_path, lambda p: p.__setitem__("remote_pin", {"branch": 7}))
        code, err = self._freeze()
        self.assertEqual(code, 1)
        self.assertIn("trusted_config_missing", err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_positive_control_without_the_guard_the_changed_configuration_is_followed(self):
        # The same change, with the trusted-configuration guard disabled in
        # this test only: the freeze goes through, so the refusals above are
        # the guard's.
        from agent_sparring import trusted_snapshots

        with mock.patch.object(trusted_snapshots, "guard", lambda *a, **k: contextlib.nullcontext()):
            code, err = self._freeze_after(
                lambda path: _run_git(self.repo, "config", "core.sshCommand", "/bin/false")
            )
        self.assertEqual(code, 0, err)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.FROZEN)

    # A resume never pins (runs are local-first), and never re-takes a
    # snapshot the engine's record says existed; a run from before any
    # snapshot gets one, visibly.

    def test_resuming_a_pinned_run_whose_pin_is_gone_does_not_re_pin_it(self):
        self._ready_not_accepted()
        self._legacy_pinned()
        self._rewrite(self.state_path, lambda p: p.pop("remote_pin"))

        stage_adapter = _StageAdapter(self.repo)
        with self.assertRaises(PlanError) as ctx:
            self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        self.assertIn("trusted_config_missing", str(ctx.exception))
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("remote_pin", payload)
        self.assertNotIn("config_snapshot", payload)
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_resuming_a_run_the_engine_never_pinned_records_a_snapshot_not_a_pin(self):
        # Positive control for the refusal above: with no engine record of a
        # pin or snapshot (a run from before either), the resume records a
        # snapshot -- and still no pin -- and the stage completes.
        self._ready_not_accepted()
        self._from_before_snapshots()

        self._resume(_StageAdapter(self.repo), _SparringAdapter([]), stop_after_stage=S1)

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("remote_pin", payload)
        self.assertIn("config_snapshot", payload)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)


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

    def test_a_run_never_reaches_the_push_primitive_so_there_is_nothing_to_race(self):
        # Was test_a_raced_engine_push_in_a_run_pauses_it_on_movement. Runs
        # are local-first (I-L1): the engine's push primitive is never called
        # by a run, so no race exists to detect; the stage is accepted
        # locally and the remote is untouched. The primitive itself is still
        # race-checked by the three tests above (kept for Plan P).
        stage_adapter = _StageAdapter(self.repo, commit=True)
        patch, raced = self._racing_head()
        with patch as run_remote:
            result = self._start(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)

        run_remote.assert_not_called()
        self.assertEqual(raced, [])
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsNone(result.awaiting)
        self.assertEqual(self._plan_state().remote_records, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(_remote_sha(self.remote, "feature/x"), self.remote_before)

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

    # Was: a failed turn after an out-of-band push was checked and paused on
    # remote movement. Plan L (L-D2) drops remote-movement monitoring from
    # runs: the failed turn stands as the failure it is -- exactly as the
    # same turn without a push (the two tests after these) -- no remote is
    # read, and a resume carries on with local work.

    def test_an_interrupted_turn_after_an_out_of_band_push_fails_as_without_the_push(self):
        with self.assertRaises(_Interrupted):
            self._start(
                _PushesThenIsInterrupted(self.repo, commit=True, push=True),
                _SparringAdapter([READY]),
                stop_after_stage=S1,
            )
        self.assertEqual(_remote_sha(self.remote, "feature/x"), _head_sha(self.repo))
        self.assertIsNone(self._plan_state().awaiting)
        self.assertEqual(self._plan_state().remote_records, ())

    def test_a_turn_that_raises_after_an_out_of_band_push_fails_and_resume_carries_on(self):
        failing = _PushesThenFails(self.repo, commit=True, push=True)

        with self.assertRaises(PlanRunError) as ctx:
            self._start(failing, _SparringAdapter([READY]), stop_after_stage=S1)

        self.assertIn("stage-agent turn failed", str(ctx.exception))
        self.assertEqual(_remote_sha(self.remote, "feature/x"), _head_sha(self.repo))
        self.assertIsNone(self._plan_state().awaiting)

        # The resume is not stopped by what was pushed: the stage's work goes
        # on locally and the stage is accepted.
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([READY])
        again = resume_plan(
            self.plan_path, self.sparring_dir, self.repo, _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x", stop_after_stage=S1,
        )
        self.assertIsNone(again.awaiting)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

    # The same failing turns, without the out-of-band push: the same outcome,
    # so the push makes no difference to a run.

    def test_an_interrupted_turn_without_a_push_is_not_movement(self):
        with self.assertRaises(_Interrupted):
            self._start(
                _PushesThenIsInterrupted(self.repo, commit=True), _SparringAdapter([READY]),
                stop_after_stage=S1,
            )
        self.assertEqual(_remote_sha(self.remote, "feature/x"), self.remote_before)
        self.assertIsNone(self._plan_state().awaiting)

    def test_a_turn_that_raises_without_a_push_is_not_movement(self):
        with self.assertRaises(PlanRunError) as ctx:
            self._start(_PushesThenFails(self.repo, commit=True), _SparringAdapter([READY]), stop_after_stage=S1)
        self.assertIn("stage-agent turn failed", str(ctx.exception))
        self.assertEqual(_remote_sha(self.remote, "feature/x"), self.remote_before)
        self.assertIsNone(self._plan_state().awaiting)


class _ReviewerPushesThenFails(_SparringAdapter):
    """READY for the build stage; then, as the independent reviewer, a
    commit is pushed out of band (unless ``push`` is false) and the
    reviewer's turn fails."""

    def __init__(self, repo: Path, verdicts, *, push: bool = True):
        super().__init__(verdicts)
        self.repo = repo
        self.push = push
        self.calls = 0

    def _next(self, session_id):
        self.calls += 1
        if self.calls == 2:
            if self.push:
                tree = subprocess.run(
                    ["git", "-C", str(self.repo), "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "out of band"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                _run_git(self.repo, "push", "-q", "origin", f"{tree}:refs/heads/feature/x")
            raise ProviderError("the reviewer's provider timed out")
        return super()._next(session_id)


class FailedReviewTurnTests(_ReviewRepoTestCase):
    def test_a_failed_review_only_turn_after_an_out_of_band_push_fails_as_without_it(self):
        # Was ..._is_still_checked_and_resume_runs_nothing: Plan L (L-D2)
        # drops remote-movement monitoring from runs. The failed review turn
        # stands as the failure it is, with no remote read and no pause.
        with self.assertRaises(PlanRunError) as ctx:
            self.start(
                _StageAdapter(self.repo, commit=True), _ReviewerPushesThenFails(self.repo, [READY])
            )
        self.assertIn("independent-review turn failed", str(ctx.exception))
        self.assertIsNone(self.plan_state().awaiting)
        self.assertIs(self.stage(REVIEW_STAGE).read_state().status, StageStatus.WORKING)

    def test_a_failed_review_turn_without_a_push_is_not_movement(self):
        # The same failing review turn with no out-of-band push: the same
        # outcome as the test above.
        with self.assertRaises(PlanRunError) as ctx:
            self.start(
                _StageAdapter(self.repo, commit=True), _ReviewerPushesThenFails(self.repo, [READY], push=False)
            )
        self.assertIn("independent-review turn failed", str(ctx.exception))
        self.assertIsNone(self.plan_state().awaiting)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
