"""Unrelated untracked files never become the reviewed candidate.

The incident: READY was given over HEAD plus untracked files nobody meant to
submit (``.DS_Store``), so the candidate was recorded as a worktree, the
bounded commit turn rightly refused to commit them, and removing them left
a clean commit whose digest no longer matched -- with no supported way on.
"""

import contextlib
import io
import json
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.next_turn import (
    AmbiguousNextTurn,
    NextTurnError,
    check_untracked_before_review,
    resolve_finalization,
)
from agent_sparring.providers import StageAgentResult
from agent_sparring.plan import PlanRunError
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring.stage import StageStatus

from test_fresh_session import _DirtyStageAdapter, _LoopRepo, _head, _strip_marker
from test_plan import (
    NEEDS_YOU,
    READY,
    S1,
    _PlanRepoTestCase,
    _run_git,
    _SparringAdapter,
    _StageAdapter,
)


class _NewDirStageAdapter(_StageAdapter):
    """Creates a brand-new directory, which plain git status collapses."""

    def _turn(self, session_id):
        self._calls += 1
        (self.repo / "new-dir").mkdir(exist_ok=True)
        (self.repo / "new-dir" / "legit.txt").write_text("mine\n")
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)


class ReviewerStartGuardTests(_LoopRepo):
    def test_an_unrelated_untracked_file_refuses_the_reviewer(self):
        (self.repo / "stray.txt").write_text("not mine\n")
        sparring = _SparringAdapter([READY])

        with self.assertRaises(LoopError) as ctx:
            run_unattended_loop(
                self.stage, self.sparring_dir, self.repo, _StageAdapter(self.repo), sparring,
                expected_branch="feature/x",
            )

        message = str(ctx.exception)
        self.assertIn("stray.txt", message)
        self.assertIn("Remove them, or ignore them", message)
        self.assertIn("'sparring run-loop s1'", message)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertIsNone(read_recorded_outcome(self.stage))
        state = self.stage.read_state()
        self.assertEqual(state.untracked_baseline, ("stray.txt",))
        # The pinned candidate leaves the refused file out.
        self.assertEqual(state.next_turn, "sparring")
        self.assertEqual(state.next_turn_candidate.kind, "commit")
        self.assertEqual(state.next_turn_candidate.untracked, ())

    def test_a_file_added_under_an_implementation_directory_is_refused(self):
        sparring = _SparringAdapter([NEEDS_YOU])
        run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, _NewDirStageAdapter(self.repo), sparring,
            expected_branch="feature/x",
        )
        self.assertEqual(len(sparring.start_calls), 1)
        (self.repo / "new-dir" / "stray.txt").write_text("later\n")

        with self.assertRaises(NextTurnError) as ctx:
            check_untracked_before_review(self.repo, self.stage)
        message = str(ctx.exception)
        self.assertIn("new-dir/stray.txt", message)
        self.assertNotIn("new-dir/legit.txt", message)

    def test_a_legacy_collapsed_handoff_entry_vouches_for_nothing(self):
        run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, _NewDirStageAdapter(self.repo),
            _SparringAdapter([NEEDS_YOU]), expected_branch="feature/x",
        )
        self.assertIn("- new-dir/\n", self.stage.read_handoff())
        path = self.stage.directory / "state.json"
        raw = json.loads(path.read_text())
        raw.pop("untracked_produced")
        path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")

        with self.assertRaises(NextTurnError) as ctx:
            check_untracked_before_review(self.repo, self.stage)
        self.assertIn("new-dir/legit.txt", str(ctx.exception))

    def test_standalone_run_sparring_names_its_own_command(self):
        (self.repo / "stray.txt").write_text("not mine\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "run-sparring", "s1",
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                "--codex-executable", "/nonexistent/codex",
            ])
        self.assertEqual(code, 1)
        self.assertIn("stray.txt", err.getvalue())
        self.assertIn("'sparring run-sparring s1 --expected-branch feature/x'", err.getvalue())
        self.assertNotIn("agents", self.stage.read_state().to_dict())

    def test_an_untracked_file_the_implementation_created_is_reviewed(self):
        sparring = _SparringAdapter([NEEDS_YOU])
        run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, _DirtyStageAdapter(self.repo), sparring,
            expected_branch="feature/x",
        )
        self.assertEqual(len(sparring.start_calls), 1)
        self.assertEqual(self.stage.read_state().untracked_baseline, ())


class PreventionResumeTests(_PlanRepoTestCase):
    def test_cleanup_after_a_refusal_resumes_into_review_without_another_turn(self):
        (self.repo / ".DS_Store").write_text("finder\n")
        stage_adapter, sparring = _StageAdapter(self.repo, commit=True), _SparringAdapter([READY])
        with self.assertRaises(PlanRunError) as ctx:
            self._start(stage_adapter, sparring, stop_after_stage=S1)
        self.assertIn(".DS_Store", str(ctx.exception))
        stage = self._stage(S1)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertIsNone(read_recorded_outcome(stage))
        state = stage.read_state()
        self.assertEqual(state.next_turn, "sparring")
        self.assertEqual(state.next_turn_candidate.untracked, ())
        pinned = state.next_turn_candidate

        # Resuming without cleanup refuses again and changes nothing.
        before = (stage.directory / "state.json").read_bytes()
        with self.assertRaises(PlanRunError):
            self._resume(stage_adapter, _SparringAdapter([]), stop_after_stage=S1)
        self.assertEqual((stage.directory / "state.json").read_bytes(), before)

        (self.repo / ".DS_Store").unlink()
        sparring = _SparringAdapter([READY])
        result = self._resume(stage_adapter, sparring, stop_after_stage=S1)
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 1)
        self.assertEqual(len(sparring.start_calls + sparring.resume_calls), 1)
        self.assertEqual(dict(result.accepted)[S1], pinned.head_sha)


class UntrackedRemovalRecoveryTests(_PlanRepoTestCase):
    def leave_ready_over_stray_files(self):
        """Reproduce the incident as an older engine recorded it: READY over
        HEAD plus an unrelated untracked file, stopped before the commit
        turn."""

        (self.repo / ".DS_Store").write_text("finder\n")
        stage_adapter = _StageAdapter(self.repo, commit=True, fail_at=2)
        with mock.patch("agent_sparring.loop.check_untracked_before_review"), mock.patch(
            "agent_sparring.loop.unrelated_untracked_paths", return_value=()
        ):
            with self.assertRaises(PlanRunError):
                self._start(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        stage = self._stage(S1)
        state = stage.read_state()
        self.assertEqual(state.next_turn, "finalization")
        self.assertEqual(state.next_turn_candidate.kind, "worktree")
        self.assertEqual([p for p, _ in state.next_turn_candidate.untracked], [".DS_Store"])
        return stage

    def test_removed_stray_files_are_re_reviewed_then_accepted(self):
        stage = self.leave_ready_over_stray_files()
        reviewed = _head(self.repo)
        (self.repo / ".DS_Store").unlink()
        stage_adapter, sparring = _StageAdapter(self.repo, commit=True), _SparringAdapter([READY])

        with contextlib.redirect_stderr(io.StringIO()):
            result = self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(len(sparring.start_calls + sparring.resume_calls), 1)
        self.assertEqual(dict(result.accepted)[S1], reviewed)
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)

    def assert_refused(self, stage, *expected):
        before = (stage.directory / "state.json").read_bytes()
        with self.assertRaises(AmbiguousNextTurn) as ctx:
            resolve_finalization(self.repo, stage, stage.read_state())
        for text in expected:
            self.assertIn(text, str(ctx.exception))
        self.assertIn(f"'sparring reset-stage {stage.stage_id} <plan> --expected-branch feature/x'", str(ctx.exception))
        self.assertIn("rerun 'sparring resume-plan'", str(ctx.exception))
        self.assertEqual((stage.directory / "state.json").read_bytes(), before)

    def test_a_tracked_change_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").unlink()
        (self.repo / "impl-1.txt").write_text("edited\n")
        self.assert_refused(stage, "uncommitted tracked changes: impl-1.txt")

    def test_a_different_head_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").unlink()
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        self.assert_refused(stage, "HEAD moved", "(changing later.txt)")

    def test_a_new_untracked_file_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").unlink()
        (self.repo / "other.txt").write_text("new\n")
        self.assert_refused(stage, "new untracked files: other.txt")

    def test_a_modified_reviewed_untracked_file_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").write_text("changed\n")
        self.assert_refused(stage, "modified previously reviewed untracked files: .DS_Store")

    def test_an_older_marker_recovers_on_head_siblings_and_a_clean_tree(self):
        stage = self.leave_ready_over_stray_files()
        path = stage.directory / "state.json"
        raw = json.loads(path.read_text())
        for key in ("tracked_digest", "untracked"):
            raw["next_turn_candidate"].pop(key)
        path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        (self.repo / ".DS_Store").unlink()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(resolve_finalization(self.repo, stage, stage.read_state()), "sparring")
        self.assertIn("without its untracked-file detail", err.getvalue())
        state = stage.read_state()
        self.assertEqual((state.next_turn, state.next_turn_candidate.kind), ("sparring", "commit"))


class ManualResolutionProvenanceTests(_PlanRepoTestCase):
    def test_a_manual_choice_survives_later_marker_writes(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        from test_fresh_session import _FailingSparringAdapter, FAIL, SEND_BACK

        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, _FailingSparringAdapter([SEND_BACK, FAIL]))
        stage = self._stage(S1)
        _strip_marker(stage)
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        self._resume(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1, next_turn="stage")

        state = stage.read_state()
        self.assertIs(state.status, StageStatus.ACCEPTED)
        resolution = state.next_turn_resolution
        self.assertEqual((resolution.turn, resolution.source), ("stage", "manual"))
        self.assertIn("choose explicitly", resolution.reason)
        self.assertTrue(resolution.recorded_at)
        # The live source names the current marker's writer: the engine.
        self.assertEqual(state.next_turn_source, "engine")
