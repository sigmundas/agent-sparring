"""An implementation slice is put on a feature branch no later than approval.

The fixture is the real-world shape that exposed the gap: slice ``app`` is
approved, run and accepted; slice ``web`` (Stage 1B) runs in a repository that
intake inspected on ``main``. Before approvals refused a protected branch,
``web`` could be sealed on ``main`` and then refused by run-plan's branch guard
before its first provider turn. These tests cover the new approval rule, the
branch action the engine reports, and the one recovery for such an approval.
"""

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.intake import IntakeError
from agent_sparring.intake_approval import BRANCH_MOVE_FILENAME, IntakeApprovalError, read_approval
from agent_sparring.intake_branch import (
    ACTION_CREATE,
    ACTION_MOVE,
    create_slice_branch,
    move_slice_approval,
    slice_branch_status,
)
from agent_sparring.intake_branch import suggested_branch
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    load_plan_source,
    resume_plan,
    run_state_path,
    start_plan,
)
from agent_sparring.stage import Stage, StageStatus
from test_intake import _git, good_interpretation
from test_intake_sealing import APP_BRANCH, _Committer, _head, _Sealed
from test_plan import READY, _SparringAdapter

FEATURE = "feature/widgets-stage-1b"


class _NoTurn:
    """A provider that fails the test if it is ever given a turn."""

    def start(self, prompt):
        raise AssertionError("a provider turn happened")

    def resume(self, session_id, prompt):
        raise AssertionError("a provider turn happened")


class _WebOnMain(_Sealed):
    def setUp(self):
        super().setUp()
        _git(self.web, "checkout", "-q", "main")  # the context repository, as prepare-plan saw it
        self.web_sparring = self.web / ".sparring"

    # -- the world up to Stage 1B -------------------------------------------------

    def app_done(self, payload=None):
        intake = self.prepare(payload)
        approval = self.approve(intake.directory)
        result, _ = self.run_app(approval)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        return intake, approval

    def approve_web(self, intake_dir):
        return self.approve(intake_dir, run="web", confirmed_prerequisites=["release"])

    def legacy_approve_web(self, intake_dir):
        """An approval sealed on main, as the engine did before it refused one."""

        with mock.patch("agent_sparring.intake.is_protected_branch", return_value=False):
            return self.approve_web(intake_dir)

    def web_source(self, approval):
        return load_plan_source(approval.manifest_path, self.web, manifest=True)

    def start_web(self, approval, adapters, branch):
        return start_plan(self.web_source(approval), self.web_sparring, self.web, adapters, expected_branch=branch)

    def failed_on_main(self):
        """Stage 1B sealed on main, and a run refused before any provider turn."""

        intake, app = self.app_done()
        web = self.legacy_approve_web(intake.directory)
        self.assertEqual(web.expected_branch, "main")
        with self.assertRaisesRegex(PlanRunError, "refusing unattended stage-agent run on protected branch 'main'"):
            self.start_web(web, lambda s: (_NoTurn(), _NoTurn()), "main")
        state = PlanRunState.load(run_state_path(self.web_sparring, web.run_key))
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        return intake, app, web

    def web_stage(self, web):
        (stage_id,) = json.loads(web.approval_path.read_text(encoding="utf-8"))["stage_ids"]
        return Stage.resolve(self.web_sparring, stage_id)

    def status(self, intake, run="web"):
        return slice_branch_status(intake.directory, run_id=run, repo_root=self.web, sparring_dir=self.web_sparring)

    def finish_web(self, web, branch):
        stage = _Committer(self.web, branch)
        return resume_plan(
            self.web_source(web), self.web_sparring, self.web,
            lambda s: (stage, _SparringAdapter([READY] * 4)), expected_branch=branch,
        )


class ApprovalNeedsAFeatureBranchTests(_WebOnMain):
    def test_an_implementation_slice_on_main_is_refused_not_sealed(self):
        intake, _ = self.app_done()
        status = self.status(intake)
        self.assertTrue(status.needs_branch)
        self.assertEqual((status.action, status.suggested_branch), (ACTION_CREATE, "feature/plan-stage-1b"))
        self.assertIsNone(status.approved_branch)
        with self.assertRaisesRegex(IntakeError, "Stage 1B needs a feature branch.*protected branch 'main'"):
            self.approve_web(intake.directory)
        self.assertFalse((intake.directory / "runs" / "web" / "approval.json").exists(), "nothing is sealed")

    def test_a_feature_branch_at_the_inspected_commit_permits_approval_and_a_run(self):
        intake, app = self.app_done()
        head = _head(self.web)
        created, moved = create_slice_branch(
            intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring, branch=FEATURE
        )
        self.assertEqual((created, moved), (FEATURE, None))
        self.assertEqual(_head(self.web), head, "the branch is created at the commit intake inspected")
        self.assertIsNone(self.status(intake).action)

        web = self.approve_web(intake.directory)
        self.assertEqual(web.expected_branch, FEATURE)
        record = json.loads(web.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(record["starting_snapshot"]["branch"], FEATURE)
        self.assertEqual(record["repositories"]["inspected"]["web"]["branch"], "main", "what intake saw stays recorded")

        stage = _Committer(self.web, FEATURE)
        result = self.start_web(web, lambda s: (stage, _SparringAdapter([READY] * 4)), FEATURE)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(stage.calls, 1)

    def test_a_new_branch_at_another_commit_is_still_drift(self):
        intake, _ = self.app_done()
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        (self.web / "later.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", "later.txt")
        _git(self.web, "commit", "-q", "-m", "moved")
        with self.assertRaisesRegex(IntakeError, "web: feature/widgets-stage-1b moved from"):
            self.approve_web(intake.directory)

    def test_a_review_only_slice_is_not_asked_for_a_feature_branch(self):
        payload = good_interpretation()
        payload["runs"][1]["stages"][0]["mode"] = "independent_review"
        intake = self.prepare(payload)
        status = self.status(intake)
        self.assertFalse(status.runs_agent)
        self.assertEqual((status.needs_branch, status.action), (False, None))

    def test_the_suggested_name_comes_from_the_plan_file_and_its_stages(self):
        run = mock.Mock(id="run_2_web", stages=[mock.Mock(label="2"), mock.Mock(label="3P")])
        self.assertEqual(
            suggested_branch("docs/plans/active/2026-09-27-taxonomy-v3.md", run), "feature/taxonomy-v3-stage-2-3p"
        )

    def test_the_branch_name_offered_must_not_be_protected_or_invalid(self):
        intake, _ = self.app_done()
        for name in ("main", "master", "bad..name", " "):
            with self.subTest(name=name), self.assertRaisesRegex(IntakeError, "not a branch name a stage agent may run on"):
                create_slice_branch(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring, branch=name)
        self.assertEqual(_git_branch(self.web), "main")


class RecoveryTests(_WebOnMain):
    def test_a_slice_refused_on_main_before_any_turn_moves_and_then_runs(self):
        intake, app, web = self.failed_on_main()
        app_approval = app.approval_path.read_bytes()
        web_approval = web.approval_path.read_bytes()
        app_stage = Stage.resolve(self.sparring_dir, json.loads(app_approval)["stage_ids"][-1]).read_state()

        status = self.status(intake)
        self.assertEqual((status.approved_branch, status.action, status.blocked), ("main", ACTION_CREATE, None))
        self.assertEqual(status.suggested_branch, "feature/plan-stage-1b")

        created, moved = create_slice_branch(
            intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring, branch=FEATURE
        )
        self.assertEqual(created, FEATURE)
        self.assertEqual((moved.from_branch, moved.to_branch, moved.created), ("main", FEATURE, True))

        self.assertEqual(web.approval_path.read_bytes(), web_approval, "the approval itself is never rewritten")
        record = json.loads((web.approval_path.parent / BRANCH_MOVE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual((record["from_branch"], record["to_branch"], record["head"]), ("main", FEATURE, _head(self.web)))
        self.assertEqual(record["run_state"]["status"], "paused")
        self.assertIn("before any provider turn", record["reason"])
        self.assertEqual(read_approval(web.approval_path).expected_branch, FEATURE)
        self.assertEqual(PlanRunState.load(run_state_path(self.web_sparring, web.run_key)).expected_branch, FEATURE)
        self.assertEqual(self.status(intake).moved_from, "main")
        self.assertIsNone(self.status(intake).action)

        # A repeat is the same move, not a second one.
        again = move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        self.assertFalse(again.created)

        result = self.finish_web(web, FEATURE)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

        # The earlier slice is exactly as it was accepted.
        self.assertEqual(app.approval_path.read_bytes(), app_approval)
        self.assertFalse((app.approval_path.parent / BRANCH_MOVE_FILENAME).exists())
        after = Stage.resolve(self.sparring_dir, json.loads(app_approval)["stage_ids"][-1]).read_state()
        self.assertEqual((after.status, after.candidate_sha), (StageStatus.ACCEPTED, app_stage.candidate_sha))

    def test_the_move_refuses_after_a_provider_turn(self):
        intake, _, web = self.failed_on_main()
        stage = self.web_stage(web)
        state = stage.read_state()
        state.implementation_session_id = "impl-1"
        stage.write_state(state)
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        status = self.status(intake)
        self.assertIsNone(status.action)
        self.assertIn("has already run", status.blocked)
        with self.assertRaisesRegex(IntakeError, "has already run"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        self.assertFalse((web.approval_path.parent / BRANCH_MOVE_FILENAME).exists())

    def test_a_turn_recorded_only_in_the_activity_also_refuses(self):
        intake, _, web = self.failed_on_main()
        with self.web_stage(web).activity_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 1, "actor": "stage", "event": "turn.started"}) + "\n")
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        with self.assertRaisesRegex(IntakeError, r"provider turn \(turn.started\)"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)

    def test_the_move_refuses_a_branch_at_another_commit_or_with_new_changes(self):
        intake, _, web = self.failed_on_main()
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        (self.web / "stray.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "changes it did not have when approved: stray.txt"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        _git(self.web, "add", "stray.txt")
        _git(self.web, "commit", "-q", "-m", "moved")
        with self.assertRaisesRegex(IntakeError, "not the approved commit"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        self.assertFalse((web.approval_path.parent / BRANCH_MOVE_FILENAME).exists())

    def test_there_is_no_protected_branch_bypass(self):
        intake, _, web = self.failed_on_main()
        # Still on main: nothing to move to.
        with self.assertRaisesRegex(IntakeError, "still on protected branch 'main'"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        # A hand-written move onto another protected branch authorizes nothing.
        record = {
            "version": 1, "run_id": "web", "run_key": web.run_key,
            "approval_sha256": read_approval(web.approval_path).sha256,
            "from_branch": "main", "to_branch": "master", "head": _head(self.web),
        }
        (web.approval_path.parent / BRANCH_MOVE_FILENAME).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(IntakeApprovalError, "protected branch 'master'"):
            read_approval(web.approval_path)
        # ...and the stage-agent guard itself is unchanged: run-plan on main still refuses.
        (web.approval_path.parent / BRANCH_MOVE_FILENAME).unlink()
        with self.assertRaisesRegex((PlanError, PlanRunError), "protected branch 'main'"):
            resume_plan(self.web_source(web), self.web_sparring, self.web, lambda s: (_NoTurn(), _NoTurn()), expected_branch="main")

    def test_a_slice_approved_on_a_feature_branch_is_never_moved(self):
        intake, _ = self.app_done()
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        web = self.approve_web(intake.directory)
        _git(self.web, "checkout", "-q", "-b", "feature/other")
        with self.assertRaisesRegex(IntakeError, "where it can run; an approved branch is not changed"):
            move_slice_approval(intake.directory, run_id="web", repo_root=self.web, sparring_dir=self.web_sparring)
        self.assertEqual(read_approval(web.approval_path).expected_branch, FEATURE)


class CliTests(_WebOnMain):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.web_sparring), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_slice_branch_json_reports_then_fixes(self):
        intake, _, web = self.failed_on_main()
        code, out, _ = self._main("slice-branch", str(intake.directory), "--run", "web", "--repo-root", str(self.web), "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"]["action"], ACTION_CREATE)
        self.assertEqual(payload["status"]["stages"], "Stage 1B")
        self.assertIn("approved on protected branch 'main'", payload["status"]["problem"])

        code, out, _ = self._main(
            "slice-branch", str(intake.directory), "--run", "web", "--repo-root", str(self.web), "--create", FEATURE, "--json"
        )
        payload = json.loads(out)
        self.assertEqual(code, 0, payload)
        self.assertEqual((payload["created"], payload["moved"]["to_branch"]), (FEATURE, FEATURE))
        self.assertEqual((payload["status"]["action"], payload["status"]["needs_branch"]), (None, False))

    def test_move_to_a_branch_checked_out_by_hand(self):
        intake, _, web = self.failed_on_main()
        _git(self.web, "checkout", "-q", "-b", FEATURE)
        self.assertEqual(self.status(intake).action, ACTION_MOVE)
        code, out, _ = self._main("slice-branch", str(intake.directory), "--run", "web", "--repo-root", str(self.web), "--move", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["moved"]["from_branch"], "main")


def _git_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True, check=True
    ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
