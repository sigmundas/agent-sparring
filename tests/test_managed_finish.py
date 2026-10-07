import json
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import managed_finish, managed_run
from agent_sparring.concurrency import worktree_lock
from agent_sparring.plan import MarkdownPlanSource, PlanRunState, PlanRunStatus, load_plan_source
from agent_sparring.stage import Stage, StageStatus

from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import _run_git


class ManagedFinishTests(_ManagedRepoTestCase):
    def _complete(self, *, push: bool = True):
        """A managed run with every stage accepted at the worktree's HEAD."""

        self._start_managed()
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
        _run_git(worktree, "add", "feature.txt")
        _run_git(worktree, "commit", "-q", "-m", "feature")
        if push and record.remote:
            _run_git(worktree, "push", "-q", record.remote, record.branch)
        head = _git(worktree, "rev-parse", "HEAD")
        source = load_plan_source(Path(record.input_path), worktree)
        assert isinstance(source, MarkdownPlanSource)
        for planned in source.in_namespace(record.run_key).stages():
            stage = Stage.resolve(record.sparring_dir, planned.stage_id)
            if not stage.exists():
                stage.create(run=record.run_key)
            state = stage.read_state()
            state.status = StageStatus.ACCEPTED
            state.candidate_sha = head
            state.run = record.run_key
            stage.write_state(state)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        run_state = PlanRunState.load(state_path)
        run_state.status = PlanRunStatus.COMPLETE
        run_state.awaiting = None
        run_state.evidence_pending = None
        run_state.save(state_path)
        return record, head

    def _status(self, record, **kwargs):
        return managed_finish.finish_status(self.repo, record.run_key, **kwargs)

    def _failed(self, record, **kwargs):
        finish = self._status(record, **kwargs)["finish"]
        return {check["code"] for check in finish["checks"] if not check["ok"]}, finish

    def _refs_and_files(self, record):
        worktree = Path(record.worktree_path)
        return (
            _git(self.repo, "for-each-ref"),
            _git(self.repo, "status", "--porcelain", "--ignored"),
            _git(worktree, "status", "--porcelain", "--ignored"),
            managed_run.record_path(self.repo, record.run_key).read_bytes(),
            (record.sparring_dir / "plans" / f"{record.run_key}.json").read_bytes(),
        )

    # -- eligible ------------------------------------------------------------

    def test_eligible_fast_forward(self):
        record, head = self._complete()
        status = self._status(record)
        finish = status["finish"]
        self.assertEqual(finish["schema_version"], 1)
        self.assertTrue(finish["managed"])
        self.assertEqual(finish["eligible"], {"merge": True, "cleanup": True}, finish["checks"])
        self.assertEqual(finish["merge_mode"], "fast_forward")
        self.assertIn(f"fast-forward main to {head}", finish["actions"])
        git = status["git"]
        self.assertEqual(git["head"], head)
        self.assertEqual(git["branch_tip"], head)
        self.assertEqual(git["final_candidate"], head)
        self.assertTrue(git["clean"])
        self.assertTrue(git["candidate_pushed"])
        self.assertFalse(git["target_contains_candidate"])
        self.assertEqual(git["target_checked_out_at"], str(self.repo.resolve()))

    def test_eligible_already_merged(self):
        record, head = self._complete()
        _run_git(self.repo, "merge", "-q", "--ff-only", head)
        finish = self._status(record)["finish"]
        self.assertEqual(finish["merge_mode"], "already_merged")
        self.assertEqual(finish["eligible"], {"merge": True, "cleanup": True})

    def test_merge_commit_needs_the_flag(self):
        record, _ = self._complete()
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        _run_git(self.repo, "add", "other.txt")
        _run_git(self.repo, "commit", "-q", "-m", "other")
        failed, finish = self._failed(record)
        self.assertEqual(failed, {"target_advanced"})
        self.assertEqual(finish["merge_mode"], "merge_commit")
        self.assertFalse(finish["eligible"]["merge"])
        failed, finish = self._failed(record, allow_merge_commit=True)
        self.assertEqual(failed, set())
        self.assertEqual(finish["eligible"], {"merge": True, "cleanup": True})

    # -- refusals ------------------------------------------------------------

    def test_unmanaged_and_unknown_run_keys(self):
        status = managed_finish.finish_status(self.repo, "no-such-run-1234abcd")
        finish = status["finish"]
        self.assertFalse(finish["managed"])
        self.assertEqual(finish["eligible"], {"merge": False, "cleanup": False})
        self.assertEqual([c["code"] for c in finish["checks"]], ["unmanaged"])
        self.assertIsNone(finish["merge_mode"])
        bad = managed_finish.finish_status(self.repo, "../escape")["finish"]
        self.assertEqual([c["code"] for c in bad["checks"]], ["unmanaged"])
        record, _ = self._complete()
        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["created_by"] = "person"
        path.write_text(json.dumps(payload), encoding="utf-8")
        failed, finish = self._failed(record)
        self.assertEqual(failed, {"unmanaged"})
        self.assertFalse(finish["managed"])

    def test_run_not_complete(self):
        self._start_managed()
        (record,) = self._records()
        failed, _ = self._failed(record)
        self.assertIn("run_not_complete", failed)

    def test_run_not_complete_when_a_stage_is_unaccepted(self):
        record, _ = self._complete()
        source = load_plan_source(Path(record.input_path), Path(record.worktree_path)).in_namespace(record.run_key)
        stage = Stage.resolve(record.sparring_dir, source.stages()[-1].stage_id)
        state = stage.read_state()
        state.status = StageStatus.WORKING
        stage.write_state(state)
        failed, _ = self._failed(record)
        self.assertIn("run_not_complete", failed)

    def test_human_gate_pending(self):
        record, _ = self._complete()
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        state = PlanRunState.load(state_path)
        state.evidence_pending = {"stage": "x", "sparring_digest": "y"}
        state.save(state_path)
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"human_gate_pending"})

    def test_runner_live(self):
        record, _ = self._complete()
        with worktree_lock(Path(record.worktree_path)):
            failed, _ = self._failed(record)
        self.assertEqual(failed, {"runner_live"})

    def test_worktree_missing(self):
        record, _ = self._complete()
        _run_git(self.repo, "worktree", "move", record.worktree_path, str(self.repo.parent / "moved"))
        failed, _ = self._failed(record)
        self.assertIn("worktree_missing", failed)

    def test_branch_mismatch(self):
        record, _ = self._complete()
        _run_git(Path(record.worktree_path), "checkout", "-q", "-b", "elsewhere")
        failed, _ = self._failed(record)
        self.assertIn("branch_mismatch", failed)

    def test_worktree_dirty(self):
        record, _ = self._complete()
        (Path(record.worktree_path) / "stray.txt").write_text("x\n", encoding="utf-8")
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"worktree_dirty"})

    def test_candidate_mismatch(self):
        record, _ = self._complete()
        worktree = Path(record.worktree_path)
        (worktree / "late.txt").write_text("late\n", encoding="utf-8")
        _run_git(worktree, "add", "late.txt")
        _run_git(worktree, "commit", "-q", "-m", "late")
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"candidate_mismatch"})

    def test_candidate_not_pushed(self):
        record, _ = self._complete(push=False)
        self.assertEqual(record.remote, "origin")
        status = self._status(record)
        failed = {c["code"] for c in status["finish"]["checks"] if not c["ok"]}
        self.assertEqual(failed, {"candidate_not_pushed"})
        self.assertFalse(status["git"]["candidate_pushed"])
        _run_git(Path(record.worktree_path), "push", "-q", "origin", record.branch)
        status = self._status(record)
        self.assertTrue(status["git"]["candidate_pushed"])
        self.assertTrue(status["finish"]["eligible"]["merge"])

    def test_branch_in_use(self):
        record, _ = self._complete()
        _run_git(self.repo, "worktree", "add", "-q", "-f", str(self.repo.parent / "second"), record.branch)
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"branch_in_use"})

    def test_target_checkout_dirty(self):
        record, _ = self._complete()
        (self.repo / "untracked.txt").write_text("fine\n", encoding="utf-8")
        failed, _ = self._failed(record)
        self.assertEqual(failed, set())
        (self.repo / "docs" / "plan.md").write_text("edited\n", encoding="utf-8")
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"target_checkout_dirty"})

    def test_merge_conflict(self):
        record, _ = self._complete()
        (self.repo / "feature.txt").write_text("conflicting\n", encoding="utf-8")
        _run_git(self.repo, "add", "feature.txt")
        _run_git(self.repo, "commit", "-q", "-m", "conflict")
        failed, finish = self._failed(record, allow_merge_commit=True)
        self.assertEqual(failed, {"merge_conflict"})
        self.assertEqual(finish["eligible"], {"merge": False, "cleanup": False})

    # -- reporting -------------------------------------------------------------

    def test_deleted_ignored_paths_exclude_the_project_dir(self):
        record, _ = self._complete()
        worktree = Path(record.worktree_path)
        (worktree / "build").mkdir()
        (worktree / "build" / "out.bin").write_text("x", encoding="utf-8")
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "build/\n", encoding="utf-8")
        finish = self._status(record)["finish"]
        self.assertIn("build", finish["deleted_ignored_paths"])
        self.assertNotIn(".sparring", finish["deleted_ignored_paths"])

    def test_runs_json_and_dry_run_json_agree_and_change_nothing(self):
        record, _ = self._complete()
        before = self._refs_and_files(record)
        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        (run,) = json.loads(out)["runs"]
        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--dry-run", "--json"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), run["finish"])
        self.assertEqual(set(run["git"]), {
            "head", "branch_tip", "clean", "final_candidate", "candidate_pushed",
            "target_tip", "target_contains_candidate", "target_checked_out_at",
        })
        self.assertEqual(self._refs_and_files(record), before)

    def test_dry_run_reports_ineligible_with_exit_3_and_without_dry_run_refuses(self):
        self._start_managed()
        (record,) = self._records()
        code, out, _ = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--dry-run", "--json")
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(out)["eligible"]["merge"])
        code, _, err = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("--dry-run", err)
