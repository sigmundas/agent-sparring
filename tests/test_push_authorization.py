"""Push authorization: the permission a managed run needs before a verified
candidate can be put where the acceptance gate requires it.

The behaviour these tests hold, in one sentence each:

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

from agent_sparring.cli import main
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    resume_plan,
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
    ensure_candidate_pushed,
    intended_remote,
    push_command,
)
from agent_sparring.stage import StageStatus
from agent_sparring.providers import StageAgentResult
from test_plan import (  # noqa: E402  (shared scripted adapters, verdicts and fixture)
    NEEDS_YOU,
    READY,
    S1,
    S2,
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
        candidate_sha="a" * 40, branch="feature/x", remote="origin", remote_branch="feature/x"
    )

    def test_pushes_one_fully_qualified_refspec_to_the_named_remote(self):
        self.assertEqual(
            push_command(self.TARGET),
            [
                "-c",
                "push.followTags=false",
                "push",
                "origin",
                "refs/heads/feature/x:refs/heads/feature/x",
            ],
        )

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
        # A refspec with a non-empty source cannot delete a ref, and both
        # sides are fully qualified so no push configuration can widen it.
        refspec = command[-1]
        source, _, destination = refspec.partition(":")
        self.assertTrue(source.startswith("refs/heads/"))
        self.assertTrue(destination.startswith("refs/heads/"))
        self.assertEqual(refspec.count(":"), 1)

    def test_the_remote_branch_is_the_only_destination_even_when_it_differs(self):
        command = push_command(
            PushTarget(
                candidate_sha="b" * 40,
                branch="feature/local",
                remote="upstream",
                remote_branch="feature/remote",
            )
        )
        self.assertEqual(command[-2], "upstream")
        self.assertEqual(command[-1], "refs/heads/feature/local:refs/heads/feature/remote")


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

    def setUp(self):
        super().setUp()
        self.remote_before = _remote_sha(self.remote, "feature/x")
        self.assertIsNotNone(self.remote_before)

    def assertNothingPushed(self):
        self.assertEqual(_remote_sha(self.remote, "feature/x"), self.remote_before)

    def assertPushed(self, sha: str):
        self.assertEqual(_remote_sha(self.remote, "feature/x"), sha)


class PushGateTests(_RemoteAwareTestCase):
    """The gate in front of the gate, exercised directly against a real
    repository and a real bare remote."""

    def _commit(self, name: str) -> str:
        path = self.repo / name
        path.write_text("work\n", encoding="utf-8")
        _run_git(self.repo, "add", name)
        _run_git(self.repo, "commit", "-q", "-m", name)
        return _head_sha(self.repo)

    def test_an_already_pushed_candidate_is_left_alone(self):
        sha = self._commit("one.txt")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        outcome = ensure_candidate_pushed(
            self.repo, stage_id="stage-1", expected_branch="feature/x", authorization=None
        )
        self.assertIsNone(outcome.required)
        self.assertFalse(outcome.pushed)
        self.assertEqual(outcome.target.candidate_sha, sha)

    def test_without_authorization_nothing_is_pushed_and_the_request_is_typed(self):
        sha = self._commit("one.txt")

        outcome = ensure_candidate_pushed(
            self.repo, stage_id="stage-1", expected_branch="feature/x", authorization=None
        )

        self.assertIsNotNone(outcome.required)
        required = outcome.required
        self.assertEqual(required.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(required.candidate_sha, sha)
        self.assertEqual(required.branch, "feature/x")
        self.assertEqual(required.remote, "origin")
        self.assertEqual(required.remote_branch, "feature/x")
        self.assertNothingPushed()

    def test_an_authorized_push_lands_the_candidate_and_re_proves_reachability(self):
        sha = self._commit("one.txt")
        authorization = authorization_for_candidate(
            self.repo, stage_id="stage-1", candidate_sha=sha, branch="feature/x"
        )

        outcome = ensure_candidate_pushed(
            self.repo,
            stage_id="stage-1",
            expected_branch="feature/x",
            authorization=authorization,
        )

        self.assertTrue(outcome.pushed)
        self.assertIsNone(outcome.required)
        self.assertPushed(sha)

    def test_authorization_for_a_different_commit_does_not_authorize_this_one(self):
        first = self._commit("one.txt")
        authorization = authorization_for_candidate(
            self.repo, stage_id="stage-1", candidate_sha=first, branch="feature/x"
        )
        second = self._commit("two.txt")

        outcome = ensure_candidate_pushed(
            self.repo,
            stage_id="stage-1",
            expected_branch="feature/x",
            authorization=authorization,
        )

        self.assertIsNotNone(outcome.required)
        self.assertEqual(outcome.required.candidate_sha, second)
        self.assertNothingPushed()

    def test_the_wrong_branch_is_refused_before_anything_is_pushed(self):
        self._commit("one.txt")
        authorization = authorization_for_run(self.repo, branch="feature/x")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/other")

        with self.assertRaises(PushError) as ctx:
            ensure_candidate_pushed(
                self.repo,
                stage_id="stage-1",
                expected_branch="feature/x",
                authorization=authorization,
            )

        self.assertIn("feature/other", str(ctx.exception))
        self.assertIsNone(_remote_sha(self.remote, "feature/other"))

    def test_a_run_authorization_for_another_branch_does_not_cover_this_one(self):
        # The run is on feature/x; the permission names main. Nothing is
        # pushed and the request is made again.
        self._commit("one.txt")
        authorization = PushAuthorization(
            scope=PushScope.RUN,
            repo_root=str(self.repo.resolve()),
            branch="main",
            remote="origin",
            remote_branch="main",
        )

        outcome = ensure_candidate_pushed(
            self.repo,
            stage_id="stage-1",
            expected_branch="feature/x",
            authorization=authorization,
        )

        self.assertIsNotNone(outcome.required)
        self.assertNothingPushed()

    def test_a_failing_push_is_reported_and_the_candidate_stays_unpushed(self):
        self._commit("one.txt")
        authorization = authorization_for_run(self.repo, branch="feature/x")
        # A remote that cannot be written to: the ordinary, realistic failure.
        (self.remote / "HEAD").chmod(0o444)
        _run_git(self.repo, "remote", "set-url", "origin", str(self.remote / "gone.git"))

        with self.assertRaises(PushError) as ctx:
            ensure_candidate_pushed(
                self.repo,
                stage_id="stage-1",
                expected_branch="feature/x",
                authorization=authorization,
            )

        self.assertIn("failed", str(ctx.exception))

    def test_a_push_that_reports_success_but_does_not_land_is_not_accepted(self):
        sha = self._commit("one.txt")
        authorization = authorization_for_run(self.repo, branch="feature/x")

        # git reported success and the candidate is still not reachable: the
        # one shape in which a "successful" push must not be believed.
        with mock.patch(
            "agent_sparring.push_gate.verify_pushed",
            side_effect=[(False, "not there yet"), (False, "still not there")],
        ):
            with self.assertRaises(PushError) as ctx:
                ensure_candidate_pushed(
                    self.repo,
                    stage_id="stage-1",
                    expected_branch="feature/x",
                    authorization=authorization,
                )

        self.assertIn("still", str(ctx.exception).lower())
        self.assertIn(sha, str(ctx.exception))

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
    """The whole loop: a managed run reaching READY with an unpushed
    candidate, and what each answer to that does."""

    def _unpushed(self) -> _Implementer:
        return _Implementer(self.repo)

    def test_a_verified_candidate_that_is_not_on_the_remote_pauses_the_run(self):
        stage_adapter = self._unpushed()
        sparring_adapter = _SparringAdapter([READY, READY])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, S1)
        self.assertIsNone(result.routing)
        self.assertIsNone(result.recorded)
        self.assertIsNotNone(result.awaiting)
        self.assertEqual(result.awaiting.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(result.awaiting.candidate_sha, _head_sha(self.repo))
        self.assertEqual(result.awaiting.remote, "origin")
        self.assertEqual(result.awaiting.remote_branch, "feature/x")
        self.assertEqual(result.awaiting.stage_id, S1)
        self.assertEqual(result.accepted, ())

        # Nothing was pushed, nothing was frozen, nothing was accepted and the
        # run did not advance.
        self.assertNothingPushed()
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.WORKING)
        self.assertIsNone(s1.candidate_sha)
        self.assertFalse(self._stage(S2).exists())
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        self.assertEqual(state.current_stage_index, 0)
        self.assertIsNone(state.push_authorization)
        self.assertEqual(state.awaiting, result.awaiting)

    def test_the_pause_is_recorded_in_the_run_state_and_survives_a_reload(self):
        self._start(self._unpushed(), _SparringAdapter([READY, READY]))

        reloaded = PlanRunState.load(self.state_path)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["awaiting"]["kind"], PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(payload["awaiting"]["candidate_sha"], _head_sha(self.repo))
        self.assertIsNone(payload["push_authorization"])
        self.assertIsInstance(reloaded.awaiting, PushRequired)

    def test_authorizing_the_exact_candidate_pushes_it_and_accepts_the_stage(self):
        self._start(self._unpushed(), _SparringAdapter([READY, READY]))
        sha = _head_sha(self.repo)

        # The second stage is never reached: this resume is bounded to the one
        # it was stopped on, so the scripted adapters below would be unused.
        stage_adapter = _Implementer(self.repo)
        sparring_adapter = _SparringAdapter([])
        result = self._resume(
            stage_adapter, sparring_adapter, allow_push_candidate=sha, stop_after_stage=S1
        )

        # No agent ran: the reviewer's READY was already on disk.
        self.assertEqual(stage_adapter.start_calls, [])
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual(sparring_adapter.start_calls, [])
        self.assertEqual(sparring_adapter.resume_calls, [])

        self.assertPushed(sha)
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, sha)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1])
        state = self._plan_state()
        self.assertIsNone(state.awaiting)
        self.assertEqual(state.push_authorization.scope, PushScope.CANDIDATE)
        self.assertEqual(state.push_authorization.candidate_sha, sha)

    def test_authorizing_a_commit_the_run_is_not_waiting_on_is_refused(self):
        self._start(self._unpushed(), _SparringAdapter([READY, READY]))
        real = _head_sha(self.repo)
        stale = "0" * 40

        with self.assertRaises(PlanError) as ctx:
            self._resume(
                _Implementer(self.repo), _SparringAdapter([]), allow_push_candidate=stale
            )

        message = str(ctx.exception)
        self.assertIn(real, message)
        self.assertIn(stale, message)
        self.assertNothingPushed()
        self.assertIsNone(self._plan_state().push_authorization)

    def test_authorizing_a_candidate_when_nothing_is_waiting_is_refused(self):
        # A run paused for an ordinary reason -- its reviewer asked the human
        # something. There is no candidate awaiting permission, so there is
        # nothing this flag could be about.
        self._start(
            _Implementer(self.repo, push=True), _SparringAdapter([NEEDS_YOU]), stop_after_stage=S1
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

        self.assertIn("not waiting", str(ctx.exception))

    def test_a_run_authorization_pushes_every_later_verified_candidate_without_asking(self):
        stage_adapter = self._unpushed()
        sparring_adapter = _SparringAdapter([READY, READY])

        result = self._start(
            stage_adapter, sparring_adapter, allow_push_for_run=True
        )

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1, S2])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertIs(self._stage(S2).read_state().status, StageStatus.ACCEPTED)
        self.assertPushed(_head_sha(self.repo))
        state = self._plan_state()
        self.assertIsNone(state.awaiting)
        self.assertEqual(state.push_authorization.scope, PushScope.RUN)

    def test_granting_the_run_scope_at_a_pause_stops_the_asking_for_the_next_stage(self):
        # Stage 1 stops for permission. The person allows this candidate and
        # opts out of being asked again; stage 2's candidate is then pushed
        # without a second request.
        self._start(self._unpushed(), _SparringAdapter([READY]))
        sha = _head_sha(self.repo)

        result = self._resume(
            self._unpushed(),
            _SparringAdapter([READY]),
            allow_push_candidate=sha,
            allow_push_for_run=True,
        )

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1, S2])
        self.assertPushed(_head_sha(self.repo))
        self.assertEqual(self._plan_state().push_authorization.scope, PushScope.RUN)

    def test_a_run_scoped_authorization_survives_a_fresh_process_reading_the_state(self):
        self._start(self._unpushed(), _SparringAdapter([READY]), allow_push_for_run=True,
                    stop_after_stage=S1)

        # Everything a later process has: the file on disk.
        reloaded = PlanRunState.load(self.state_path)
        self.assertEqual(reloaded.push_authorization.scope, PushScope.RUN)
        self.assertEqual(reloaded.push_authorization.repo_root, str(self.repo.resolve()))
        self.assertEqual(reloaded.push_authorization.remote_branch, "feature/x")

        result = self._resume(self._unpushed(), _SparringAdapter([READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIs(self._stage(S2).read_state().status, StageStatus.ACCEPTED)

    def test_a_candidate_that_moved_since_the_pause_is_not_covered_by_its_authorization(self):
        self._start(self._unpushed(), _SparringAdapter([READY]), stop_after_stage=S1)
        first = _head_sha(self.repo)

        # The candidate moves on after the run stopped -- work committed on
        # the branch while the person was deciding. They then authorize the
        # commit they were shown, which is no longer the candidate.
        (self.repo / "later.txt").write_text("more work\n", encoding="utf-8")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "later")
        moved = _head_sha(self.repo)
        self.assertNotEqual(moved, first)

        stage_adapter = self._unpushed()
        # HEAD had moved, so this resume is not the authorized push, and the
        # stage's next_turn marker records READY over the *first* commit: the
        # moved candidate is refused outright -- no agent turn, no push.
        with self.assertRaises(PlanRunError) as ctx:
            self._resume(
                stage_adapter,
                _SparringAdapter([READY]),
                allow_push_candidate=first,
                stop_after_stage=S1,
            )
        self.assertIn("Only that reviewed commit may be pushed and accepted", str(ctx.exception))
        self.assertEqual(stage_adapter.turns, 0)
        self.assertNothingPushed()

        # Choosing explicitly to continue implementation runs the ordinary
        # loop; the candidate it produces is a commit nobody authorized.
        # Nothing reached the remote and nothing was accepted.
        result = self._resume(
            stage_adapter,
            _SparringAdapter([READY]),
            stop_after_stage=S1,
            next_turn="stage",
        )
        self.assertEqual(stage_adapter.turns, 1)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsNotNone(result.awaiting)
        self.assertNotEqual(result.awaiting.candidate_sha, first)
        self.assertEqual(result.awaiting.candidate_sha, _head_sha(self.repo))
        self.assertNothingPushed()
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_a_push_failure_stops_the_run_and_accepts_nothing(self):
        _run_git(self.repo, "remote", "set-url", "origin", str(self.remote.parent / "gone.git"))

        with self.assertRaises(PlanRunError) as ctx:
            self._start(self._unpushed(), _SparringAdapter([READY]), allow_push_for_run=True)

        message = str(ctx.exception)
        self.assertIn("could not be put on its intended remote branch", message)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_a_push_whose_result_cannot_be_verified_accepts_nothing(self):
        with mock.patch(
            "agent_sparring.push_gate.verify_pushed",
            side_effect=[(False, "not there"), (False, "still not there")],
        ):
            with self.assertRaises(PlanRunError):
                self._start(
                    self._unpushed(), _SparringAdapter([READY]), allow_push_for_run=True
                )

        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_pushing_it_yourself_and_resuming_accepts_it_without_any_permission(self):
        # The third answer to the request, and the one that needs nothing from
        # the engine: the person pushes the branch and resumes normally.
        self._start(self._unpushed(), _SparringAdapter([READY]), stop_after_stage=S1)
        sha = _head_sha(self.repo)
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        stage_adapter = self._unpushed()
        result = self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)

        # No agent ran and nothing was pushed by the engine: the recorded
        # READY was already over this exact commit, and it is now reachable.
        self.assertEqual(stage_adapter.turns, 0)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1])
        self.assertIsNone(self._plan_state().push_authorization)
        self.assertIsNone(self._plan_state().awaiting)
        self.assertPushed(sha)

    def test_an_authorization_is_permission_and_not_an_instruction(self):
        # The stage pushes its own candidate, as a well-behaved agent may.
        # Nothing else is pushed on top of that.
        stage_adapter = _Implementer(self.repo, push=True)

        result = self._start(
            stage_adapter, _SparringAdapter([READY, READY]), allow_push_for_run=True
        )

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertPushed(_head_sha(self.repo))

    def test_a_run_recorded_before_push_authorization_existed_loads_as_no_authorization(self):
        self._start(self._unpushed(), _SparringAdapter([READY]), stop_after_stage=S1)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        del payload["push_authorization"]
        del payload["awaiting"]
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        state = PlanRunState.load(self.state_path)

        self.assertIsNone(state.push_authorization)
        self.assertIsNone(state.awaiting)

        # And such a run behaves as the default does: it asks.
        result = self._resume(self._unpushed(), _SparringAdapter([READY]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsNotNone(result.awaiting)


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

    def test_run_plan_reports_the_typed_request_with_both_ways_to_answer_it(self):
        code, out, _err = self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)
        sha = _head_sha(self.repo)

        self.assertEqual(code, 0)
        self.assertIn(f"needs: {PUSH_AUTHORIZATION_REQUIRED}", out)
        self.assertIn(f"candidate: {sha}", out)
        self.assertIn("push to: origin/feature/x", out)
        self.assertIn(f"--allow-push-candidate {sha}", out)
        self.assertIn("--allow-push-for-run", out)
        # Not dressed up as a review outcome or a manual check.
        self.assertNotIn("action:", out)
        self.assertNotIn("required before READY", out)
        self.assertNothingPushed()

    def test_resume_plan_with_the_candidate_flag_pushes_and_accepts(self):
        self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)
        sha = _head_sha(self.repo)

        code, out, _err = self._main(
            "resume-plan",
            str(self.plan_path),
            "--allow-push-candidate",
            sha,
            "--stop-after-stage",
            S1,
        )

        self.assertEqual(code, 0)
        self.assertPushed(sha)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

    def test_resume_plan_refuses_a_stale_candidate_flag(self):
        self._main("run-plan", str(self.plan_path), "--stop-after-stage", S1)

        code, _out, err = self._main(
            "resume-plan", str(self.plan_path), "--allow-push-candidate", "0" * 40
        )

        self.assertEqual(code, 1)
        self.assertIn("refusing to authorize a commit this run is not waiting on", err)
        self.assertNothingPushed()

    def test_run_plan_can_record_the_run_scoped_permission_up_front(self):
        code, _out, err = self._main(
            "run-plan", str(self.plan_path), "--allow-push-for-run", "--stop-after-stage", S1
        )

        self.assertEqual(code, 0)
        self.assertIn("push authorization recorded for this run", err)
        self.assertPushed(_head_sha(self.repo))
        self.assertEqual(
            PlanRunState.load(self.state_path).push_authorization.scope, PushScope.RUN
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
