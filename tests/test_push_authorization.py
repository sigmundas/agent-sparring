"""Push authorization: the push primitive kept for Plan P, and what became of
run-time push permission when runs became local-first (Plan L).

Since Plan L Stage 2 a run never pushes: ``ManagedRunPushTests`` and
``PushAuthorizationCliTests`` hold that a verified candidate is accepted
locally, that the retired flags refuse with typed codes, and that pauses an
older engine recorded are kept until resolved explicitly and are never
acceptance. The primitive tests below (command shape, binding) are
unchanged; since Plan L Stage 3 the run-time push gate is gone, and
``PushGateTests`` exercises the retained ``engine_git.run_remote`` directly.

The behaviour the primitive tests hold, in one sentence each:

- by default nothing is ever pushed, and a verified candidate that is not on
  its intended remote branch stops the run with a *typed* request naming the
  exact commit, remote and branch -- not a review outcome, and not prose;
- a person's permission is data in the run's own state, bound to one commit
  or to one run, and it survives a reload;
- what an authorized push may be is exactly one git command shape, with no
  force, no other branch, no tags and no ref deletion;
- the acceptance gate itself is unchanged: it runs afterwards and makes its
  own checks, and neither a push failure nor a failed re-verification lets a
  candidate through.

Every repository here is a temporary one with a real local bare remote, so
reachability is proven the same way the engine proves it in a real project.
No network, and no provider.
"""

import contextlib
import io
import json
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.acceptance import AcceptanceError, freeze_candidate
from agent_sparring.cli import main
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    acknowledge_remote_movement,
    authorize_pushed_candidate,
    resume_plan,
)
from agent_sparring.capabilities import PUSH_NOT_PART_OF_RUNS, PUSH_PERMISSION_DEPRECATED
from agent_sparring.remote_pin import (
    AUTHORIZED_WITHOUT_PUSH,
    MOVEMENT_ACKNOWLEDGED,
    PUSH_RECEIPT,
    PUSHED_CANDIDATE_UNAUTHORIZED,
    PushedCandidateUnauthorized,
    UnauthorizedRemoteMovement,
)
from agent_sparring.push_gate import (
    PUSH_AUTHORIZATION_REQUIRED,
    PushAuthorization,
    PushError,
    PushRequired,
    PushScope,
    PushTarget,
    authorization_for_candidate,
    authorization_for_run,
    intended_remote,
    pinned_target,
    push_command,
)
from agent_sparring import engine_git
from git_recorder import GitRecorder
from legacy_pins import legacy_pin
from agent_sparring.stage import StageStatus
from agent_sparring.providers import StageAgentResult
from test_plan import (  # noqa: E402  (shared scripted adapters, verdicts and fixture)
    NEEDS_YOU,
    READY,
    S1,
    S2,
    SEND_BACK,
    _PlanRepoTestCase,
    _SparringAdapter,
    _fixed,
    _head_sha,
    _run_git,
)


def _remote_sha(remote: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


class _Implementer:
    """One implementation turn: commit something no other turn has committed,
    and by default do not push it.

    That is the shape these tests are about -- an agent under instructions
    never to push without being told -- and the unique file name matters:
    across a pause and a resume, two turns writing byte-identical content
    would leave the second with nothing to commit, which is a fixture
    accident rather than anything the engine does.
    """

    def __init__(self, repo: Path, *, push: bool = False):
        self.repo = repo
        self.push = push
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self._starts = 0

    @property
    def turns(self) -> int:
        return len(self.start_calls) + len(self.resume_calls)

    def _turn(self, session_id: str) -> StageAgentResult:
        name = f"impl-{uuid.uuid4().hex[:12]}.txt"
        (self.repo / name).write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", name)
        _run_git(self.repo, "commit", "-q", "-m", name)
        if self.push:
            _run_git(self.repo, "push", "-q", "origin", "feature/x")
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        self._starts += 1
        return self._turn(f"impl-{self._starts}")

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        return self._turn(session_id)


class PushCommandShapeTests(unittest.TestCase):
    """What an authorized push is *allowed to be*, asserted on the one
    function that builds it."""

    TARGET = PushTarget(
        candidate_sha="a" * 40,
        branch="feature/x",
        remote="origin",
        remote_branch="feature/x",
        url="/srv/pinned.git",
    )

    def test_pushes_exactly_the_authorized_commit_to_the_pinned_url(self):
        self.assertEqual(
            push_command(self.TARGET),
            [
                "-c",
                "push.followTags=false",
                "push",
                # Its old->new is compared with the tip observed before it.
                "--porcelain",
                "/srv/pinned.git",
                f"{'a' * 40}:refs/heads/feature/x",
            ],
        )

    def test_without_a_pin_it_names_the_remote(self):
        command = push_command(
            PushTarget(candidate_sha="a" * 40, branch="feature/x", remote="origin", remote_branch="feature/x")
        )
        self.assertEqual(command[-2], "origin")

    def test_carries_no_force_no_tags_and_no_deletion(self):
        command = push_command(self.TARGET)
        for forbidden in (
            "--force",
            "-f",
            "--force-with-lease",
            "--force-if-includes",
            "--tags",
            "--follow-tags",
            "--delete",
            "-d",
            "--all",
            "--mirror",
            "--prune",
            "--no-verify",
        ):
            self.assertNotIn(forbidden, command)
        # A refspec with a non-empty source cannot delete a ref; the source is
        # the exact commit (not a branch that could move) and the destination
        # is fully qualified, so no push configuration can widen it.
        refspec = command[-1]
        source, _, destination = refspec.partition(":")
        self.assertEqual(source, "a" * 40)
        self.assertTrue(destination.startswith("refs/heads/"))
        self.assertEqual(refspec.count(":"), 1)

    def test_the_remote_branch_is_the_only_destination_even_when_it_differs(self):
        command = push_command(
            PushTarget(
                candidate_sha="b" * 40,
                branch="feature/local",
                remote="upstream",
                remote_branch="feature/remote",
                url="/srv/upstream.git",
            )
        )
        self.assertEqual(command[-2], "/srv/upstream.git")
        self.assertEqual(command[-1], f"{'b' * 40}:refs/heads/feature/remote")


class AuthorizationBindingTests(unittest.TestCase):
    """What a recorded permission does and does not cover. Pure data: no
    repository is touched."""

    def _candidate(self, **overrides) -> PushAuthorization:
        fields = dict(
            scope=PushScope.CANDIDATE,
            repo_root="/work/repo",
            branch="feature/x",
            remote="origin",
            remote_branch="feature/x",
            stage_id="stage-1",
            candidate_sha="a" * 40,
        )
        fields.update(overrides)
        return PushAuthorization(**fields)

    def _target(self, **overrides) -> PushTarget:
        fields = dict(
            candidate_sha="a" * 40,
            branch="feature/x",
            remote="origin",
            remote_branch="feature/x",
        )
        fields.update(overrides)
        return PushTarget(**fields)

    def test_one_candidate_authorization_covers_exactly_that_commit(self):
        authorization = self._candidate()
        self.assertTrue(
            authorization.covers(
                repo_root=Path("/work/repo"),
                branch="feature/x",
                target=self._target(),
                stage_id="stage-1",
            )
        )

    def test_a_changed_candidate_is_not_covered(self):
        authorization = self._candidate()
        self.assertFalse(
            authorization.covers(
                repo_root=Path("/work/repo"),
                branch="feature/x",
                target=self._target(candidate_sha="c" * 40),
                stage_id="stage-1",
            )
        )

    def test_another_stage_another_branch_another_remote_and_another_worktree_are_not_covered(self):
        authorization = self._candidate()
        base = dict(repo_root=Path("/work/repo"), branch="feature/x", stage_id="stage-1")
        self.assertFalse(authorization.covers(**{**base, "stage_id": "stage-2"}, target=self._target()))
        self.assertFalse(
            authorization.covers(
                **{**base, "branch": "main"}, target=self._target(branch="main")
            )
        )
        self.assertFalse(
            authorization.covers(**base, target=self._target(remote="fork"))
        )
        self.assertFalse(
            authorization.covers(**base, target=self._target(remote_branch="other"))
        )
        self.assertFalse(
            authorization.covers(**{**base, "repo_root": Path("/work/other")}, target=self._target())
        )

    def test_run_authorization_covers_any_candidate_of_the_same_run_identity(self):
        authorization = PushAuthorization(
            scope=PushScope.RUN,
            repo_root="/work/repo",
            branch="feature/x",
            remote="origin",
            remote_branch="feature/x",
        )
        for sha in ("a" * 40, "d" * 40):
            self.assertTrue(
                authorization.covers(
                    repo_root=Path("/work/repo"),
                    branch="feature/x",
                    target=self._target(candidate_sha=sha),
                    stage_id="stage-7",
                )
            )
        # but still not another branch or another remote branch
        self.assertFalse(
            authorization.covers(
                repo_root=Path("/work/repo"),
                branch="main",
                target=self._target(branch="main"),
                stage_id="stage-7",
            )
        )

    def test_a_one_candidate_authorization_without_a_commit_is_refused(self):
        with self.assertRaises(PushError):
            PushAuthorization(
                scope=PushScope.CANDIDATE,
                repo_root="/work/repo",
                branch="feature/x",
                remote="origin",
                remote_branch="feature/x",
            )

    def test_round_trips_through_its_recorded_form(self):
        for authorization in (
            self._candidate(),
            PushAuthorization(
                scope=PushScope.RUN,
                repo_root="/work/repo",
                branch="feature/x",
                remote="origin",
                remote_branch="feature/x",
            ),
        ):
            payload = json.loads(json.dumps(authorization.to_dict()))
            self.assertEqual(PushAuthorization.from_dict(payload), authorization)


class _RemoteAwareTestCase(_PlanRepoTestCase):
    """Adds the one thing every assertion below needs: what ``origin`` held
    before the engine did anything, so "nothing was pushed" is checked
    against the real starting point rather than against the branch not
    existing (the fixture pushes it in setUp, as a real project has)."""

    # These tests are about the permission itself, so runs start without one.
    GRANT_PUSH_FOR_RUN = False

    def setUp(self):
        super().setUp()
        self.remote_before = _remote_sha(self.remote, "feature/x")
        self.assertIsNotNone(self.remote_before)

    def _reject_pushes(self):
        """Make the bare remote refuse every push (its own pre-receive hook,
        on the remote's side), while it can still be read."""

        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\necho rejected by the remote >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

    def assertNothingPushed(self):
        self.assertEqual(_remote_sha(self.remote, "feature/x"), self.remote_before)

    def assertPushed(self, sha: str):
        self.assertEqual(_remote_sha(self.remote, "feature/x"), sha)


class PushGateTests(_RemoteAwareTestCase):
    """What is left of the push gate under Plan L, against a real repository
    and a real bare remote.

    Plan L Stage 3 removed ``ensure_candidate_pushed`` (the run-time push
    gate) with the last production path to ``engine_git.run_remote``. Its
    two primitive properties are kept here, through ``run_remote`` and
    ``push_command`` directly -- what Plan P is specified against. Its
    run-time properties (who may push during a run, when, and what the
    records explain) left with it; each removed test is named in the Stage
    3 handoff with the test that now covers what remains of its property:

    - ``test_an_already_pushed_candidate_is_unexplained_movement_not_success``
    - ``test_without_authorization_nothing_is_pushed_and_the_request_is_typed``
    - ``test_authorization_for_a_different_commit_does_not_authorize_this_one``
    - ``test_the_wrong_branch_is_refused_before_anything_is_pushed``
    - ``test_a_run_authorization_for_another_branch_does_not_cover_this_one``
    - ``test_a_push_that_reports_success_but_does_not_land_is_not_accepted``
    """

    def _commit(self, name: str) -> str:
        path = self.repo / name
        path.write_text("work\n", encoding="utf-8")
        _run_git(self.repo, "add", name)
        _run_git(self.repo, "commit", "-q", "-m", name)
        return _head_sha(self.repo)

    def _push(self, sha: str):
        pin = legacy_pin(self.repo, "feature/x")
        return engine_git.run_remote(
            self.repo, pin.trusted_config, *push_command(pinned_target(pin, sha)),
            pinned=pin.destination,
        )

    def test_an_authorized_push_lands_the_candidate_and_re_proves_reachability(self):
        # Through the retained primitive: exactly the push_command shape, to
        # the pinned destination, lands exactly the candidate.
        sha = self._commit("one.txt")

        result = self._push(sha)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertPushed(sha)

    def test_a_failing_push_is_reported_and_the_candidate_stays_unpushed(self):
        sha = self._commit("one.txt")
        # A remote that refuses the write: the ordinary, realistic failure.
        self._reject_pushes()

        result = self._push(sha)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejected by the remote", result.stderr)
        self.assertNothingPushed()

    def test_the_intended_remote_is_the_one_the_branch_tracks(self):
        self.assertEqual(intended_remote(self.repo, "feature/x"), ("origin", "feature/x"))
        _run_git(self.repo, "config", "branch.feature/x.remote", "origin")
        _run_git(self.repo, "config", "branch.feature/x.merge", "refs/heads/renamed")
        self.assertEqual(intended_remote(self.repo, "feature/x"), ("origin", "renamed"))

    def test_a_one_candidate_authorization_needs_the_exact_commit_id(self):
        sha = self._commit("one.txt")
        with self.assertRaises(PushError):
            authorization_for_candidate(
                self.repo, stage_id="stage-1", candidate_sha=sha[:7], branch="feature/x"
            )


class ManagedRunPushTests(_RemoteAwareTestCase):
    """Plan L: runs are local-first. A managed run reaching READY with an
    unpushed candidate accepts it locally and pushes nothing; the retired
    push permissions refuse with typed codes; and the pauses an older
    engine recorded (push permission, remote movement, a pushed candidate)
    are kept until resolved explicitly, and never become acceptance (I-L3).

    Each test keeps its pre-Plan L name where the property carries over, and
    says what it was where it changed. The push primitive itself is still
    tested above (PushGateTests), for Plan P."""

    def _unpushed(self) -> _Implementer:
        return _Implementer(self.repo)

    def _ready_not_accepted(self, stage_adapter=None) -> str:
        """The reviewer said READY over a commit and the run stopped before
        the acceptance gate (interrupted), as an older engine's push pause
        left it."""

        with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._start(stage_adapter or self._unpushed(), _SparringAdapter([READY]))
        return _head_sha(self.repo)

    def _legacy_pause(self, awaiting) -> None:
        state = self._plan_state()
        state.status = PlanRunStatus.PAUSED
        state.awaiting = awaiting
        state.save(self.state_path)

    def _legacy_push_pause(self) -> str:
        sha = self._ready_not_accepted()
        self._legacy_pause(PushRequired(
            kind=PUSH_AUTHORIZATION_REQUIRED, stage_id=S1, candidate_sha=sha, branch="feature/x",
            remote="origin", remote_branch="feature/x",
        ))
        return sha

    def _movement(self, observed: str) -> UnauthorizedRemoteMovement:
        return UnauthorizedRemoteMovement(
            stage_id=S1, remote="origin", url=str(self.remote), ref="refs/heads/feature/x",
            expected_tip=self.remote_before, observed_tip=observed,
        )

    def test_a_verified_candidate_that_is_not_on_the_remote_is_accepted_locally(self):
        # Was ..._pauses_the_run (push_authorization_required).
        stage_adapter = self._unpushed()
        sparring_adapter = _SparringAdapter([READY, READY])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIsNone(result.awaiting)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1, S2])
        self.assertEqual(self._stage(S2).read_state().candidate_sha, _head_sha(self.repo))
        self.assertNothingPushed()
        state = self._plan_state()
        self.assertIsNone(state.push_authorization)
        self.assertIsNone(state.remote_pin)

    def test_the_pause_is_recorded_in_the_run_state_and_survives_a_reload(self):
        # Now: a legacy push pause loads as written, and a plain resume keeps
        # it exactly as it was -- no agent, no push, no acceptance.
        sha = self._legacy_push_pause()

        reloaded = PlanRunState.load(self.state_path)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["awaiting"]["kind"], PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(payload["awaiting"]["candidate_sha"], sha)
        self.assertIsInstance(reloaded.awaiting, PushRequired)

        before = self.state_path.read_bytes()
        stage_adapter, sparring_adapter = self._unpushed(), _SparringAdapter([])
        result = self._resume(stage_adapter, sparring_adapter)
        self.assertEqual(result.awaiting.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(stage_adapter.turns, 0)
        self.assertEqual(sparring_adapter.start_calls + sparring_adapter.resume_calls, [])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        self.assertNothingPushed()

    def test_authorizing_the_exact_candidate_is_refused_as_deprecated(self):
        # Was ..._pushes_it_and_accepts_the_stage. --allow-push-candidate is
        # never an alias for progression (I-L6): refused, nothing changed.
        sha = self._legacy_push_pause()
        before = self.state_path.read_bytes()
        stage_adapter, sparring_adapter = _Implementer(self.repo), _SparringAdapter([])

        with self.assertRaises(PlanError) as ctx:
            self._resume(stage_adapter, sparring_adapter, allow_push_candidate=sha, stop_after_stage=S1)

        self.assertIn(PUSH_PERMISSION_DEPRECATED, str(ctx.exception))
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(stage_adapter.turns, 0)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        self.assertIsNone(self._plan_state().push_authorization)
        self.assertNothingPushed()

    def test_authorizing_a_commit_the_run_is_not_waiting_on_is_refused(self):
        # Refused the same way, before the commit is even compared.
        self._legacy_push_pause()
        before = self.state_path.read_bytes()

        with self.assertRaises(PlanError) as ctx:
            self._resume(_Implementer(self.repo), _SparringAdapter([]), allow_push_candidate="0" * 40)

        self.assertIn(PUSH_PERMISSION_DEPRECATED, str(ctx.exception))
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertNothingPushed()

    def test_authorizing_a_candidate_when_nothing_is_waiting_is_refused(self):
        # A run paused for an ordinary reason -- its reviewer asked the human
        # something. The retired flag is refused there too.
        self._start(
            _Implementer(self.repo), _SparringAdapter([NEEDS_YOU]), stop_after_stage=S1
        )
        self.assertIsNone(self._plan_state().awaiting)

        with self.assertRaises(PlanError) as ctx:
            resume_plan(
                self.plan_path,
                self.sparring_dir,
                self.repo,
                _fixed(_Implementer(self.repo), _SparringAdapter([])),
                expected_branch="feature/x",
                allow_push_candidate="a" * 40,
            )

        self.assertIn(PUSH_PERMISSION_DEPRECATED, str(ctx.exception))

    def test_a_run_authorization_is_refused_and_nothing_is_created(self):
        # Was ..._pushes_every_later_verified_candidate_without_asking.
        with self.assertRaises(PlanError) as ctx:
            self._start(self._unpushed(), _SparringAdapter([READY, READY]), allow_push_for_run=True)

        self.assertIn(PUSH_NOT_PART_OF_RUNS, str(ctx.exception))
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self._stage(S1).exists())
        self.assertNothingPushed()

    def test_granting_the_run_scope_at_a_pause_is_refused(self):
        # Was ..._stops_the_asking_for_the_next_stage.
        self._legacy_push_pause()
        before = self.state_path.read_bytes()

        with self.assertRaises(PlanError) as ctx:
            self._resume(self._unpushed(), _SparringAdapter([READY]), allow_push_for_run=True)

        self.assertIn(PUSH_NOT_PART_OF_RUNS, str(ctx.exception))
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertNothingPushed()

    def test_a_legacy_run_scoped_authorization_survives_a_reload_and_grants_nothing(self):
        # Was test_a_run_scoped_authorization_survives_a_fresh_process_reading_
        # the_state. A grant an older engine recorded still loads as written,
        # and is inert: the run accepts locally and pushes nothing.
        self._start(self._unpushed(), _SparringAdapter([READY]), stop_after_stage=S1)
        state = self._plan_state()
        state.push_authorization = authorization_for_run(self.repo, branch="feature/x")
        state.save(self.state_path)

        reloaded = PlanRunState.load(self.state_path)
        self.assertEqual(reloaded.push_authorization.scope, PushScope.RUN)
        self.assertEqual(reloaded.push_authorization.repo_root, str(self.repo.resolve()))

        result = self._resume(self._unpushed(), _SparringAdapter([READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIs(self._stage(S2).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(self._plan_state().push_authorization, reloaded.push_authorization)
        self.assertNothingPushed()

    def test_a_candidate_that_moved_since_the_ready_is_not_accepted(self):
        # Was ..._since_the_pause_is_not_covered_by_its_authorization. The
        # run stopped after READY; the candidate then moves on.
        first = self._ready_not_accepted()

        (self.repo / "later.txt").write_text("more work\n", encoding="utf-8")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "later")
        moved = _head_sha(self.repo)
        self.assertNotEqual(moved, first)

        stage_adapter = self._unpushed()
        # The stage's next_turn marker records READY over the *first* commit:
        # the moved candidate is refused outright -- no agent turn, no push.
        with self.assertRaises(PlanRunError) as ctx:
            self._resume(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        self.assertIn("Only that reviewed commit may be accepted", str(ctx.exception))
        self.assertEqual(stage_adapter.turns, 0)
        self.assertNothingPushed()

        # An explicit --next-turn cannot override the recorded finalization
        # marker either: no unrestricted implementation turn on reviewed
        # work, and nothing recorded, pushed or accepted. Refused before any
        # plan-run state changes.
        with self.assertRaises(PlanError) as ctx:
            self._resume(
                stage_adapter,
                _SparringAdapter([READY]),
                stop_after_stage=S1,
                next_turn="stage",
            )
        self.assertIn("already records next_turn = finalization", str(ctx.exception))
        self.assertEqual(stage_adapter.turns, 0)
        self.assertEqual(self._stage(S1).read_state().next_turn, "finalization")
        self.assertNothingPushed()
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_a_remote_that_refuses_pushes_does_not_affect_a_run(self):
        # Was test_a_push_failure_stops_the_run_and_accepts_nothing. A run
        # pushes nothing, so a remote refusing pushes changes nothing.
        self._reject_pushes()

        result = self._start(self._unpushed(), _SparringAdapter([READY, READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertNothingPushed()

    def test_a_run_never_calls_the_push_gate(self):
        # Was test_a_push_whose_result_cannot_be_verified_accepts_nothing:
        # there is no push result to verify, because a run never contacts a
        # remote (I-L1). Plan L Stage 3: the push gate is gone, so this is
        # shown by the recorder -- every git command the run ran -- and the
        # remote's refs, not by mocking it.
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            result = self._start(self._unpushed(), _SparringAdapter([READY, READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertNothingPushed()

    def test_pushing_it_yourself_is_not_monitored_and_accepts_on_the_ready(self):
        # Was ..._is_unexplained_movement_until_acknowledged. Plan L (L-D2):
        # runs do not watch the remote. A push made by hand is neither read
        # nor a pause; the stage is accepted because the reviewer's READY is
        # for this exact commit.
        sha = self._ready_not_accepted()
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        stage_adapter = self._unpushed()
        result = self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        self.assertEqual(stage_adapter.turns, 0)
        self.assertIsNone(result.awaiting)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1])
        self.assertEqual(self._stage(S1).read_state().candidate_sha, sha)
        self.assertEqual(self._plan_state().remote_records, ())

    def test_a_legacy_pushed_candidate_pause_is_cleared_only_by_its_own_command(self):
        # Kept for legacy runs (I-L3): an older engine stopped on movement,
        # a person acknowledged it, and the engine stopped again on the
        # pushed candidate. Neither the acknowledgement nor a push flag clears
        # that; only the explicit per-commit action does, and it is not
        # acceptance -- the READY at that exact commit is.
        sha = self._ready_not_accepted()
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        self._legacy_pause(self._movement(sha))
        acknowledge_remote_movement(self.sparring_dir, run_key=self.run_key, observed=sha, by="tester")
        self._legacy_pause(PushedCandidateUnauthorized(
            stage_id=S1, candidate_sha=sha, remote="origin", url=str(self.remote),
            ref="refs/heads/feature/x", observed_tip=sha,
        ))

        result = self._resume(self._unpushed(), _SparringAdapter([]), stop_after_stage=S1)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, PUSHED_CANDIDATE_UNAUTHORIZED)
        self.assertEqual(result.accepted, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

        with self.assertRaises(PlanError) as ctx:
            self._resume(self._unpushed(), _SparringAdapter([]), allow_push_candidate=sha)
        self.assertIn(PUSH_PERMISSION_DEPRECATED, str(ctx.exception))
        with self.assertRaises(PlanError) as ctx:
            self._resume(self._unpushed(), _SparringAdapter([]), allow_push_for_run=True)
        self.assertIn(PUSH_NOT_PART_OF_RUNS, str(ctx.exception))
        self.assertEqual(self._plan_state().awaiting.kind, PUSHED_CANDIDATE_UNAUTHORIZED)

        # The explicit per-commit action names the run and the exact commit,
        # and is recorded on its own, with who and when.
        with self.assertRaises(PlanError):
            authorize_pushed_candidate(
                self.sparring_dir, run_key=self.run_key, candidate_sha=self.remote_before, by="tester"
            )
        with self.assertRaises(PlanError):
            authorize_pushed_candidate(
                self.sparring_dir, run_key=self.run_key, candidate_sha=sha[:12], by="tester"
            )
        record = authorize_pushed_candidate(
            self.sparring_dir, run_key=self.run_key, candidate_sha=sha, by="tester"
        )
        state = self._plan_state()
        self.assertIsNone(state.awaiting)
        self.assertEqual(
            [r.kind for r in state.remote_records], [MOVEMENT_ACKNOWLEDGED, AUTHORIZED_WITHOUT_PUSH]
        )
        self.assertEqual(state.remote_records[-1], record)
        self.assertEqual((record.candidate_sha, record.stage_id, record.by), (sha, S1, "tester"))
        self.assertTrue(record.recorded_at)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

        # Accepted now only because the reviewer's READY is for this exact
        # commit; nothing was pushed by the engine.
        result = self._resume(self._unpushed(), _SparringAdapter([]), stop_after_stage=S1)

        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, sha)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1])
        self.assertNotIn(PUSH_RECEIPT, [r.kind for r in self._plan_state().remote_records])
        self.assertPushed(sha)

    def _paused_on_a_pushed_candidate(self) -> str:
        """Reviewed, pushed by hand, the movement acknowledged, and the run
        stopped again on the candidate -- the legacy pauses an older engine
        recorded, written here as it wrote them (runs no longer reach them)."""

        sha = self._ready_not_accepted()
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        self._legacy_pause(self._movement(sha))
        acknowledge_remote_movement(self.sparring_dir, run_key=self.run_key, observed=sha, by="tester")
        self._legacy_pause(PushedCandidateUnauthorized(
            stage_id=S1, candidate_sha=sha, remote="origin", url=str(self.remote),
            ref="refs/heads/feature/x", observed_tip=sha,
        ))
        result = self._resume(self._unpushed(), _SparringAdapter([]), stop_after_stage=S1)
        self.assertEqual(result.awaiting.kind, PUSHED_CANDIDATE_UNAUTHORIZED)
        return sha

    def test_an_authorized_candidate_sent_back_by_its_review_is_not_accepted(self):
        # Authorized without push, then reviewed again (the person resumes
        # with evidence, so the reviewer judges before anything is accepted)
        # and sent back: the authorization is not a review and accepts nothing.
        sha = self._paused_on_a_pushed_candidate()
        record = authorize_pushed_candidate(
            self.sparring_dir, run_key=self.run_key, candidate_sha=sha, by="tester"
        )

        stage_adapter = self._unpushed()
        sparring_adapter = _SparringAdapter([SEND_BACK, NEEDS_YOU])
        result = self._resume(
            stage_adapter,
            sparring_adapter,
            evidence="Looked at the pushed commit again.",
            stop_after_stage=S1,
        )

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.accepted, ())
        # The reviewer ran first and its SEND_BACK routed to an implementation
        # turn -- not to the push or acceptance gates.
        self.assertEqual(len(sparring_adapter.start_calls + sparring_adapter.resume_calls), 2)
        self.assertEqual(stage_adapter.turns, 1)
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.WORKING)
        self.assertIsNone(s1.candidate_sha)
        state = self._plan_state()
        self.assertEqual(state.current_stage_index, 0)
        self.assertFalse(self._stage(S2).exists())
        # The authorization stands on its own, unchanged, and no receipt or
        # other record was made out of the review.
        self.assertEqual(
            [r.kind for r in state.remote_records], [MOVEMENT_ACKNOWLEDGED, AUTHORIZED_WITHOUT_PUSH]
        )
        self.assertEqual(state.remote_records[-1], record)
        self.assertPushed(sha)

    def test_deleting_the_acknowledgement_after_authorizing_changes_nothing(self):
        sha = self._paused_on_a_pushed_candidate()
        authorize_pushed_candidate(self.sparring_dir, run_key=self.run_key, candidate_sha=sha, by="tester")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        payload["remote_records"] = [
            entry for entry in payload["remote_records"] if entry["kind"] != MOVEMENT_ACKNOWLEDGED
        ]
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        result = self._resume(self._unpushed(), _SparringAdapter([]), stop_after_stage=S1)

        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(
            [r.kind for r in self._plan_state().remote_records], [AUTHORIZED_WITHOUT_PUSH]
        )

    def test_a_forged_acknowledgement_authorizes_nothing(self):
        sha = self._paused_on_a_pushed_candidate()
        # Someone who can write the run state adds an "acknowledgement" naming
        # an unrelated commit -- and, for good measure, one naming the
        # candidate itself. (Plan L: runs no longer re-derive pauses from
        # records, so the recorded pause is what stands; a record, forged or
        # not, does not clear it. Clearing the pause itself by hand is run-
        # state tampering, Stage 5's integrity work.)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        real = payload["remote_records"][-1]
        forged = [
            {**real, "tip": tip, "previous_tip": tip, "by": "forger"}
            for tip in (self.remote_before, sha)
        ]
        payload["remote_records"] = forged + payload["remote_records"]
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        stage_adapter = self._unpushed()
        result = self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        self.assertEqual(stage_adapter.turns, 0)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, PUSHED_CANDIDATE_UNAUTHORIZED)
        self.assertEqual(result.accepted, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        stage = self._stage(S1)
        with self.assertRaises(AcceptanceError):
            freeze_candidate(stage, self.sparring_dir, self.repo, expected_branch="feature/x")
        self.assertIs(stage.read_state().status, StageStatus.WORKING)

    def test_an_acknowledged_candidate_is_not_accepted_without_a_ready_review(self):
        # Kept for legacy runs (I-L3). The agent pushes its own commit and the
        # reviewer does not say READY; an older engine recorded the movement,
        # and a person acknowledges it. The acknowledgement clears only that
        # pause: nothing is accepted, and there is no pushed-candidate pause
        # for anyone to authorize.
        stage_adapter = _Implementer(self.repo, push=True)
        self._start(stage_adapter, _SparringAdapter([SEND_BACK, NEEDS_YOU]), stop_after_stage=S1)
        pushed = _head_sha(self.repo)
        self._legacy_pause(self._movement(pushed))
        acknowledge_remote_movement(
            self.sparring_dir, run_key=self.run_key, observed=pushed, by="tester"
        )

        stage_adapter.push = False
        result = self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.accepted, ())
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.WORKING)
        self.assertIsNone(s1.candidate_sha)
        with self.assertRaises(PlanError):
            authorize_pushed_candidate(
                self.sparring_dir, run_key=self.run_key, candidate_sha=pushed, by="tester"
            )
        self.assertNotIn(
            AUTHORIZED_WITHOUT_PUSH, [r.kind for r in self._plan_state().remote_records]
        )

    def test_the_paused_run_prints_the_per_commit_command(self):
        sha = self._paused_on_a_pushed_candidate()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "agent_sparring.cli._build_loop_adapters",
            return_value=(self._unpushed(), _SparringAdapter([])),
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "resume-plan", str(self.plan_path),
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                "--stop-after-stage", S1,
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(f"needs: {PUSHED_CANDIDATE_UNAUTHORIZED}", out.getvalue())
        self.assertIn(
            f"authorize-pushed-candidate --run-key {self.run_key} --sha {sha}", out.getvalue()
        )

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "authorize-pushed-candidate",
                "--run-key", self.run_key, "--sha", sha, "--repo-root", str(self.repo),
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(AUTHORIZED_WITHOUT_PUSH, out.getvalue())
        self.assertEqual(self._plan_state().remote_records[-1].kind, AUTHORIZED_WITHOUT_PUSH)

    def test_an_agent_pushing_its_own_candidate_does_not_stop_the_run(self):
        # Was ..._stops_the_run_even_under_a_run_grant. Plan L (L-D2): an
        # agent's own push is neither read nor monitored by a run; the
        # candidates are accepted on the local rules and the reviewer's READY.
        # (An agent able to push at all is Plan D's boundary.)
        stage_adapter = _Implementer(self.repo, push=True)

        result = self._start(stage_adapter, _SparringAdapter([READY, READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1, S2])
        self.assertEqual(self._plan_state().remote_records, ())

    def test_a_run_recorded_before_push_authorization_existed_loads_as_no_authorization(self):
        self._start(self._unpushed(), _SparringAdapter([READY]), stop_after_stage=S1)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        del payload["push_authorization"]
        del payload["awaiting"]
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        state = PlanRunState.load(self.state_path)

        self.assertIsNone(state.push_authorization)
        self.assertIsNone(state.awaiting)

        # And such a run behaves as every run now does (Plan L): it does not
        # ask; it accepts locally.
        result = self._resume(self._unpushed(), _SparringAdapter([READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIsNone(result.awaiting)
        self.assertNothingPushed()


class PushAuthorizationCliTests(_RemoteAwareTestCase):
    """The command-line contract, end to end through ``main``."""

    def _main(self, *args: str) -> tuple[int, str, str]:
        # `run-plan` is given this suite's run key explicitly, because the
        # stage ids these tests assert (S1/S2) are namespaced by it. Left
        # out, every run would mint its own key and its own stage ids --
        # which is the point of run instances, and not what is under test
        # here.
        if args and args[0] == "run-plan":
            args = (*args, "--run-key", self.run_key)
        out, err = io.StringIO(), io.StringIO()
        adapters = (_Implementer(self.repo), _SparringAdapter([READY]))
        with mock.patch("agent_sparring.cli._build_loop_adapters", return_value=adapters):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(
                    [
                        "--sparring-dir",
                        str(self.sparring_dir),
                        *args,
                        "--repo-root",
                        str(self.repo),
                        "--expected-branch",
                        "feature/x",
                    ]
                )
        return code, out.getvalue(), err.getvalue()

    def test_run_plan_accepts_locally_and_asks_for_no_push(self):
        # Was test_run_plan_reports_the_typed_request_with_both_ways_to_answer_it.
        code, out, _err = self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)

        self.assertEqual(code, 0)
        self.assertNotIn(PUSH_AUTHORIZATION_REQUIRED, out)
        self.assertNotIn("--allow-push-candidate", out)
        self.assertNotIn("--allow-push-for-run", out)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertNothingPushed()

    def test_resume_plan_with_the_candidate_flag_is_refused_as_deprecated(self):
        # Was ..._pushes_and_accepts.
        self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)
        sha = _head_sha(self.repo)
        before = self.state_path.read_bytes()

        code, _out, err = self._main(
            "resume-plan", str(self.plan_path), "--allow-push-candidate", sha, "--stop-after-stage", S1,
        )

        self.assertEqual(code, 1)
        self.assertIn(PUSH_PERMISSION_DEPRECATED, err)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertNothingPushed()

    def test_resume_plan_refuses_a_stale_candidate_flag(self):
        self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)

        code, _out, err = self._main(
            "resume-plan", str(self.plan_path), "--allow-push-candidate", "0" * 40
        )

        self.assertEqual(code, 1)
        self.assertIn(PUSH_PERMISSION_DEPRECATED, err)
        self.assertNothingPushed()

    def test_run_plan_refuses_the_run_scoped_permission_up_front(self):
        # Was ..._can_record_the_run_scoped_permission_up_front.
        code, _out, err = self._main(
            "run-plan", str(self.plan_path), "--allow-push-for-run", "--stop-after-stage", S1
        )

        self.assertEqual(code, 1)
        self.assertIn(PUSH_NOT_PART_OF_RUNS, err)
        self.assertFalse(self.state_path.exists())
        self.assertNothingPushed()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
