"""reset-stage: restarting a wrong-mode stage without hand-editing state.

The point of these tests is what the operation refuses. Archiving an attempt
is easy; doing it without touching the preceding stages' acceptance, without
discarding someone's unrelated work, and without quietly reviewing a
different commit than the plan says was accepted is the part that has to
hold.
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.manifest import load_manifest_source
from agent_sparring.plan import PlanRunState, PlanRunStatus, plan_state_path
from agent_sparring.recovery import RecoveryError, reset_stage, turn_touched_paths
from agent_sparring.stage import Stage, StageMode, StageState, StageStatus
from test_independent_review import BUILD_STAGE, REVIEW_STAGE, _review_manifest
from test_manifest import PLAN_LABEL
from test_plan import _head_sha, _run_git

SCRATCH = "tests/test_zz_stage5_reviewer_scratch.py"


class ResetStageTestCase(unittest.TestCase):
    """A run positioned at Stage 5, which was mistakenly started through the
    implementation lifecycle: it holds a Claude session and left a scratch
    test file in the worktree. Stage 4 is accepted and must stay so."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text(
            ".sparring/stages/\n.sparring/plans/\n__pycache__/\n", encoding="utf-8"
        )
        (self.repo / "tests" / "test_real.py").write_text("def test_real():\n    pass\n", "utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("stage 4 work\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "stage 4")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.accepted_sha = _head_sha(self.repo)

        self.sparring_dir = self.repo / ".sparring"
        self.manifest_path = root / "manifest.json"
        self.write_manifest()
        self.state_path = plan_state_path(self.sparring_dir, PLAN_LABEL)

        # Stage 4, accepted. Never written to by the recovery.
        self.build = Stage.resolve(self.sparring_dir, BUILD_STAGE).create()
        self.build.write_state(
            StageState(
                status=StageStatus.ACCEPTED,
                base_sha="0" * 40,
                candidate_sha=self.accepted_sha,
                implementation_session_id="stage-4-impl",
                sparring_session_id="stage-4-spar",
            )
        )
        # Stage 5 as the wrong lifecycle left it.
        self.review = Stage.resolve(self.sparring_dir, REVIEW_STAGE).create(
            brief="# Stage brief: the old one\n"
        )
        self.review.write_state(
            StageState(
                base_sha=self.accepted_sha,
                implementation_session_id="03f6ebee-f90e-48fa-b948-4a233c649bc6",
            )
        )
        self.review.activity_log().emit("stage", "turn.started", resumed=False)
        self.review.activity_log().emit("stage", "file.changed", tool="Write", path=SCRATCH)
        self.review.activity_log().emit("stage", "file.changed", tool="Edit", path=SCRATCH)

        # The run was recorded against the plan input as it was *before* the
        # mode was declared -- which is exactly how a stage comes to be
        # running under the wrong one -- so its recorded digest is the
        # mode-less manifest's and the reset has to re-record it.
        self.write_manifest(mode=None)
        recorded_digest = load_manifest_source(self.manifest_path).digest()
        self.write_manifest()

        PlanRunState(
            plan=PLAN_LABEL,
            plan_digest=recorded_digest,
            expected_branch="feature/x",
            current_stage_index=1,
            current_stage=REVIEW_STAGE,
            status=PlanRunStatus.RUNNING,
            source="manifest",
        ).save(self.state_path)

    def write_manifest(self, *, mode: str | None = "independent_review") -> None:
        self.manifest_path.write_text(
            json.dumps(_review_manifest(mode=mode), indent=2) + "\n", encoding="utf-8"
        )

    def source(self):
        return load_manifest_source(self.manifest_path)

    def scratch(self, *, compiled: bool = False) -> None:
        """Recreate what the erroneous turn left behind."""

        (self.repo / SCRATCH).write_text("def test_scratch():\n    pass\n", encoding="utf-8")
        if compiled:
            cache = self.repo / "tests" / "__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / "test_zz_stage5_reviewer_scratch.cpython-314.pyc").write_bytes(b"\x00cached")

    def reset(self, **kwargs):
        return reset_stage(
            self.source(),
            self.sparring_dir,
            self.repo,
            stage_id=kwargs.pop("stage_id", REVIEW_STAGE),
            expected_branch=kwargs.pop("expected_branch", "feature/x"),
            **kwargs,
        )

    def plan_state(self) -> PlanRunState:
        return PlanRunState.load(self.state_path)


class ResetStageTests(ResetStageTestCase):
    def test_reads_the_attempts_own_activity_log_for_what_it_wrote(self):
        self.assertEqual(turn_touched_paths(self.review), (SCRATCH,))

    def test_archives_the_attempt_quarantines_its_file_and_restarts_the_stage(self):
        self.scratch(compiled=True)
        previous_digest = self.plan_state().plan_digest

        result = self.reset(expect_mode=StageMode.INDEPENDENT_REVIEW)

        # The attempt is history, at its own path, and is no longer a stage.
        self.assertTrue((result.archive / "stage" / "state.json").is_file())
        archived = json.loads((result.archive / "stage" / "state.json").read_text("utf-8"))
        self.assertEqual(
            archived["implementation_session_id"], "03f6ebee-f90e-48fa-b948-4a233c649bc6"
        )
        self.assertIn("activity.jsonl", [p.name for p in (result.archive / "stage").iterdir()])
        self.assertIn(
            "03f6ebee-f90e-48fa-b948-4a233c649bc6",
            (result.archive / "RECOVERY.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            result.discarded_sessions,
            (("implementation", "03f6ebee-f90e-48fa-b948-4a233c649bc6"),),
        )

        # The scratch file and its compiled copy are preserved, not deleted,
        # and are gone from the worktree.
        self.assertFalse((self.repo / SCRATCH).exists())
        self.assertTrue((result.archive / "worktree" / SCRATCH).is_file())
        self.assertEqual(
            list((self.repo / "tests" / "__pycache__").glob("*scratch*.pyc")),
            [],
            "a stale compiled module of a removed test must not survive",
        )
        self.assertEqual(result.already_absent, ())
        self.assertIn(SCRATCH, result.quarantined)

        # The stage is fresh, under the declared mode, at the same id.
        state = Stage.resolve(self.sparring_dir, REVIEW_STAGE).read_state()
        self.assertIs(state.mode, StageMode.INDEPENDENT_REVIEW)
        self.assertIs(state.status, StageStatus.WORKING)
        self.assertIsNone(state.implementation_session_id)
        self.assertIsNone(state.sparring_session_id)
        self.assertIsNone(state.base_sha)
        self.assertEqual(
            Stage.resolve(self.sparring_dir, REVIEW_STAGE).read_brief(),
            self.source().stages()[1].brief,
        )
        # No duplicate stage was created.
        stages = sorted(
            p.name for p in (self.sparring_dir / "stages").iterdir() if not p.name.startswith(".")
        )
        self.assertEqual(stages, sorted([BUILD_STAGE, REVIEW_STAGE]))

        # Stage 4 is untouched, and the run is paused where it was, with the
        # digest of the plan input it will now continue with.
        self.assertEqual(
            self.build.read_state().to_dict(),
            StageState(
                status=StageStatus.ACCEPTED,
                base_sha="0" * 40,
                candidate_sha=self.accepted_sha,
                implementation_session_id="stage-4-impl",
                sparring_session_id="stage-4-spar",
            ).to_dict(),
        )
        run = self.plan_state()
        self.assertIs(run.status, PlanRunStatus.PAUSED)
        self.assertEqual(run.current_stage, REVIEW_STAGE)
        self.assertEqual(run.current_stage_index, 1)
        self.assertEqual(run.plan_digest, self.source().digest())
        self.assertEqual(result.previous_digest, previous_digest)
        self.assertEqual(result.reviewed_sha, self.accepted_sha)

    def test_a_file_already_gone_is_reported_rather_than_invented(self):
        # The real case: the scratch file had already been removed by hand
        # before the recovery ran. That is not an error, and it is also not
        # something to stay silent about.
        result = self.reset()
        self.assertEqual(result.already_absent, (SCRATCH,))
        self.assertEqual(result.quarantined, ())
        self.assertIn(
            SCRATCH, (result.archive / "RECOVERY.md").read_text(encoding="utf-8")
        )

    def test_repeated_resets_keep_every_attempt(self):
        self.reset()
        # Put the stage back into the wrong mode to reset it a second time.
        stage = Stage.resolve(self.sparring_dir, REVIEW_STAGE)
        stage.write_state(StageState(implementation_session_id="second-attempt"))
        second = self.reset()
        attempts = sorted(p.name for p in second.archive.parent.iterdir())
        self.assertEqual(len(attempts), 2)
        self.assertTrue(attempts[0].startswith("0001-"))
        self.assertTrue(attempts[1].startswith("0002-"))


class ResetStageRefusalTests(ResetStageTestCase):
    def _refuses(self, needle: str, **kwargs) -> str:
        before = self.review.read_state().to_dict()
        with self.assertRaises(RecoveryError) as ctx:
            self.reset(**kwargs)
        self.assertIn(needle, str(ctx.exception))
        self.assertEqual(
            self.review.read_state().to_dict(), before, "a refusal touches nothing"
        )
        self.assertTrue((self.sparring_dir / "stages" / REVIEW_STAGE).is_dir())
        return str(ctx.exception)

    def test_refuses_a_stage_that_is_not_the_runs_current_stage(self):
        self._refuses("Only the current stage can be reset", stage_id=BUILD_STAGE)
        self.assertIs(self.build.read_state().status, StageStatus.ACCEPTED)

    def test_refuses_an_accepted_stage(self):
        self.review.write_state(
            StageState(status=StageStatus.ACCEPTED, candidate_sha=self.accepted_sha)
        )
        self._refuses("acceptance is terminal")

    def test_refuses_when_the_plan_input_declares_the_mode_it_already_ran(self):
        self.write_manifest(mode=None)
        self._refuses("no wrong-mode attempt to recover from")

    def test_refuses_when_the_asked_for_mode_disagrees_with_the_plan_input(self):
        self._refuses(
            "refusing to act on a disagreement", expect_mode=StageMode.IMPLEMENTATION
        )

    def test_refuses_when_the_repository_moved_past_the_accepted_candidate(self):
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        message = self._refuses("would review a different commit")
        self.assertIn(self.accepted_sha, message)

    def test_refuses_when_the_worktree_holds_unrelated_changes(self):
        self.scratch()
        (self.repo / "impl.txt").write_text("someone is mid-edit here\n", encoding="utf-8")
        message = self._refuses("cannot account for")
        self.assertIn("impl.txt", message)
        # The unrelated edit is still there, and so is the attempt's file.
        self.assertIn("mid-edit", (self.repo / "impl.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.repo / SCRATCH).is_file())

    def test_refuses_when_the_attempt_modified_committed_content(self):
        self.review.activity_log().emit("stage", "file.changed", tool="Edit", path="impl.txt")
        (self.repo / "impl.txt").write_text("the attempt edited tracked content\n", "utf-8")
        message = self._refuses("tracked and modified")
        self.assertIn("impl.txt", message)

    def test_refuses_an_unfinished_predecessor(self):
        self.build.write_state(StageState(status=StageStatus.WORKING))
        self._refuses("not accepted")

    def test_refuses_a_branch_the_run_was_not_started_for(self):
        self._refuses("refusing to reset a stage of it for a different branch",
                      expected_branch="feature/other")

    def test_refuses_without_a_recorded_run(self):
        self.state_path.unlink()
        self._refuses("no plan run is recorded")


class ResetStageCliTests(ResetStageTestCase):
    def test_the_command_reports_what_it_did_and_how_to_continue(self):
        self.scratch()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "reset-stage",
                    REVIEW_STAGE,
                    "--manifest",
                    str(self.manifest_path),
                    "--mode",
                    "independent_review",
                    "--repo-root",
                    str(self.repo),
                    "--expected-branch",
                    "feature/x",
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        printed = out.getvalue()
        self.assertIn("was: implementation", printed)
        self.assertIn("now: independent_review", printed)
        self.assertIn("archived attempt:", printed)
        self.assertIn("03f6ebee-f90e-48fa-b948-4a233c649bc6", printed)
        self.assertIn(f"quarantined: {SCRATCH}", printed)
        self.assertIn(self.accepted_sha, printed)
        self.assertIn("plan digest re-recorded:", printed)
        self.assertIn("sparring resume-plan --manifest", printed)

    def test_a_refusal_is_reported_and_exits_non_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "reset-stage",
                    BUILD_STAGE,
                    "--manifest",
                    str(self.manifest_path),
                    "--repo-root",
                    str(self.repo),
                    "--expected-branch",
                    "feature/x",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("could not reset stage", err.getvalue())
        self.assertIs(self.build.read_state().status, StageStatus.ACCEPTED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
