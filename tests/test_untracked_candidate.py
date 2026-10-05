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

from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.next_turn import AmbiguousNextTurn, resolve_finalization
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
        self.assertIn("Remove them, commit them, or ignore them", message)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertIsNone(read_recorded_outcome(self.stage))
        self.assertEqual(self.stage.read_state().untracked_baseline, ("stray.txt",))

    def test_an_untracked_file_the_implementation_created_is_reviewed(self):
        sparring = _SparringAdapter([NEEDS_YOU])
        run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, _DirtyStageAdapter(self.repo), sparring,
            expected_branch="feature/x",
        )
        self.assertEqual(len(sparring.start_calls), 1)
        self.assertEqual(self.stage.read_state().untracked_baseline, ())


class UntrackedRemovalRecoveryTests(_PlanRepoTestCase):
    def leave_ready_over_stray_files(self):
        """Reproduce the incident as an older engine recorded it: READY over
        HEAD plus an unrelated untracked file, stopped before the commit
        turn."""

        (self.repo / ".DS_Store").write_text("finder\n")
        stage_adapter = _StageAdapter(self.repo, commit=True, fail_at=2)
        with mock.patch("agent_sparring.loop.check_untracked_before_review"):
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
        self.assertIn("reset-stage", str(ctx.exception))
        self.assertEqual((stage.directory / "state.json").read_bytes(), before)

    def test_a_tracked_change_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").unlink()
        (self.repo / "impl-1.txt").write_text("edited\n")
        self.assert_refused(stage)

    def test_a_different_head_is_refused(self):
        stage = self.leave_ready_over_stray_files()
        (self.repo / ".DS_Store").unlink()
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        self.assert_refused(stage, "HEAD moved")

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
