import json
import os
import subprocess
import unittest
from unittest import mock
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import managed_finish, managed_run
from agent_sparring.concurrency import worktree_lock
from agent_sparring.plan import MarkdownPlanSource, PlanRunState, PlanRunStatus, load_plan_source
from agent_sparring.stage import Stage, StageStatus

from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import _run_git


class _FinishTestCase(_ManagedRepoTestCase):
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

    def _index(self, checkout: Path) -> bytes:
        return Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "index")).read_bytes()

    def _refs_and_files(self, record):
        """Refs, both checkouts' index bytes and engine files -- read without
        running ``git status``, which may itself refresh an index."""

        worktree = Path(record.worktree_path)
        return (
            _git(self.repo, "for-each-ref"),
            self._index(self.repo),
            self._index(worktree),
            sorted(p.name for p in worktree.iterdir()),
            managed_run.record_path(self.repo, record.run_key).read_bytes(),
            (record.sparring_dir / "plans" / f"{record.run_key}.json").read_bytes(),
        )



class ManagedFinishTests(_FinishTestCase):
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

    def test_candidate_pushed_is_bound_to_the_recorded_remote_and_branch(self):
        record, head = self._complete(push=False)
        worktree = Path(record.worktree_path)
        # Upstream config pointing at another ref that has the candidate does not count.
        _run_git(worktree, "push", "-q", "origin", f"{head}:refs/heads/elsewhere")
        _run_git(worktree, "config", f"branch.{record.branch}.remote", "origin")
        _run_git(worktree, "config", f"branch.{record.branch}.merge", "refs/heads/elsewhere")
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"candidate_not_pushed"})
        # A recorded non-origin remote is checked, not origin.
        _run_git(worktree, "push", "-q", "origin", record.branch)
        other = self.repo.parent / "other.git"
        _run_git(self.repo.parent, "init", "-q", "--bare", str(other))
        _run_git(self.repo, "remote", "add", "upstream", str(other))
        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["remote"] = "upstream"
        path.write_text(json.dumps(payload), encoding="utf-8")
        failed, _ = self._failed(record)
        self.assertEqual(failed, {"candidate_not_pushed"})
        _run_git(worktree, "push", "-q", "upstream", record.branch)
        failed, _ = self._failed(record)
        self.assertEqual(failed, set())

    def test_stale_index_stat_is_not_refreshed(self):
        record, _ = self._complete()
        worktree = Path(record.worktree_path)
        for tracked in (worktree / "feature.txt", self.repo / "docs" / "plan.md"):
            os.utime(tracked, (1_000_000_000, 1_000_000_000))
        before = (self._index(self.repo), self._index(worktree))
        self.assertTrue(self._status(record)["finish"]["eligible"]["merge"])
        self.assertEqual((self._index(self.repo), self._index(worktree)), before)

    def test_edited_plan_is_not_complete(self):
        record, _ = self._complete()
        plan = Path(record.input_path)
        plan.write_text(plan.read_text(encoding="utf-8").replace("Lay the groundwork.", "Do something else."), encoding="utf-8")
        failed, finish = self._failed(record)
        self.assertIn("run_not_complete", failed)
        detail = next(c["detail"] for c in finish["checks"] if c["code"] == "run_not_complete")
        self.assertIn("no longer matches", detail)

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

    def test_dry_run_reports_ineligible_with_exit_3_and_execution_refuses(self):
        self._start_managed()
        (record,) = self._records()
        code, out, _ = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--dry-run", "--json")
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(out)["eligible"]["merge"])
        code, out, _ = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 3)
        report = json.loads(out)
        self.assertEqual((report["stopped_at"], report["completed_steps"]), ("checks", []))
        self.assertIn("run_not_complete", report["reason"])



class FinishExecutionTests(_FinishTestCase):
    """``finish-run`` without ``--dry-run``: merge, then clean up."""

    def _finish(self, record, *flags: str):
        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--json", *flags
        )
        return code, (json.loads(out) if out.strip() else None), err

    def _events(self, record):
        return [e["event"] for e in managed_run.read_record(self.repo, record.run_key).events]

    def _set_policy(self, record, delete_remote: bool):
        """Commit ``[finish] delete_remote_branch`` into the candidate."""

        worktree = Path(record.worktree_path)
        toml = worktree / ".sparring" / "project.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + f"\n[finish]\ndelete_remote_branch = {str(delete_remote).lower()}\n",
            encoding="utf-8",
        )
        _run_git(worktree, "add", ".sparring/project.toml")
        _run_git(worktree, "commit", "-q", "-m", "policy")
        _run_git(worktree, "push", "-q", "origin", record.branch)
        head = _git(worktree, "rev-parse", "HEAD")
        for stage_dir in (record.sparring_dir / "stages").iterdir():
            stage = Stage.resolve(record.sparring_dir, stage_dir.name)
            state = stage.read_state()
            state.candidate_sha = head
            stage.write_state(state)
        return head

    def _assert_cleaned(self, record, head):
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertNotIn(record.worktree_path, [e["path"] for e in managed_run.worktree_list(self.repo)])
        self.assertEqual(_git(self.repo, "branch", "--list", record.branch), "")
        self.assertEqual(self._events(record)[-1], "finished")

    def test_fast_forward_merges_and_fully_cleans_up(self):
        record, head = self._complete()
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"], ["merge", "archive_state", "remove_worktree", "delete_branch", "finished"])
        self.assertEqual((report["stopped_at"], report["reason"], report["remaining"]), (None, None, []))
        self._assert_cleaned(record, head)
        # The user's checkout fast-forwarded in place and is clean.
        self.assertEqual(_git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        events = managed_run.read_record(self.repo, record.run_key).events
        merged = next(e for e in events if e["event"] == "merged")
        self.assertEqual(merged["detail"]["mode"], "fast_forward")
        self.assertEqual(merged["detail"]["target_sha"], head)
        # The remote branch is kept by default.
        self.assertIn(record.branch, _git(self.repo, "ls-remote", "origin"))

    def test_archived_state_is_readable(self):
        record, _ = self._complete()
        state_before = (record.sparring_dir / "plans" / f"{record.run_key}.json").read_bytes()
        stage_names = sorted(p.name for p in (record.sparring_dir / "stages").iterdir())
        code, _, err = self._finish(record)
        self.assertEqual(code, 0, err)
        archive = managed_finish.archive_dir(self.repo, record.run_key)
        archived = archive / "plans" / f"{record.run_key}.json"
        self.assertEqual(archived.read_bytes(), state_before)
        self.assertEqual(PlanRunState.load(archived).status, PlanRunStatus.COMPLETE)
        self.assertEqual(sorted(p.name for p in (archive / "stages").iterdir()), stage_names)
        for name in stage_names:
            self.assertIs(Stage.resolve(archive, name).read_state().status, StageStatus.ACCEPTED)

    def test_target_not_checked_out_uses_compare_and_swap(self):
        record, head = self._complete()
        _run_git(self.repo, "checkout", "-q", "feature/x")
        code, _, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), head)
        self.assertEqual(_git(self.repo, "symbolic-ref", "--short", "HEAD"), "feature/x")
        self.assertEqual(_git(self.repo, "branch", "--list", record.branch), "")

    def _advance_target(self):
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        _run_git(self.repo, "add", "other.txt")
        _run_git(self.repo, "commit", "-q", "-m", "other")
        return _git(self.repo, "rev-parse", "HEAD")

    def test_merge_commit_only_with_the_flag(self):
        record, head = self._complete()
        old = self._advance_target()
        before = self._refs_and_files(record)
        code, report, _ = self._finish(record)
        self.assertEqual(code, 3)
        self.assertEqual(report["stopped_at"], "checks")
        self.assertIn("target_advanced", report["reason"])
        self.assertEqual(self._refs_and_files(record), before)
        code, report, err = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 0, err)
        tip = _git(self.repo, "rev-parse", "main")
        self.assertEqual(_git(self.repo, "rev-list", "--parents", "-n", "1", tip).split()[1:], [old, head])
        self.assertFalse(Path(record.worktree_path).exists())

    def test_merge_commit_when_target_not_checked_out(self):
        record, head = self._complete()
        old = self._advance_target()
        _run_git(self.repo, "checkout", "-q", "feature/x")
        code, _, err = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 0, err)
        tip = _git(self.repo, "rev-parse", "refs/heads/main")
        self.assertEqual(_git(self.repo, "rev-list", "--parents", "-n", "1", tip).split()[1:], [old, head])
        self.assertEqual(_git(self.repo, "symbolic-ref", "--short", "HEAD"), "feature/x")

    def test_conflict_changes_nothing(self):
        record, _ = self._complete()
        (self.repo / "feature.txt").write_text("conflicting\n", encoding="utf-8")
        _run_git(self.repo, "add", "feature.txt")
        _run_git(self.repo, "commit", "-q", "-m", "conflict")
        before = self._refs_and_files(record)
        status_before = (_git(self.repo, "status", "--porcelain"), _git(Path(record.worktree_path), "status", "--porcelain"))
        code, report, _ = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 3)
        self.assertIn("merge_conflict", report["reason"])
        self.assertEqual(self._refs_and_files(record), before)
        self.assertEqual(
            (_git(self.repo, "status", "--porcelain"), _git(Path(record.worktree_path), "status", "--porcelain")),
            status_before,
        )

    def test_failed_merge_in_checkout_is_aborted(self):
        record, _ = self._complete()
        self._advance_target()
        # An untracked file the merge would overwrite makes git refuse mid-way.
        (self.repo / "feature.txt").write_text("mine\n", encoding="utf-8")
        tip = _git(self.repo, "rev-parse", "main")
        code, report, _ = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"], "merge")
        self.assertEqual(report["completed_steps"], [])
        self.assertEqual(_git(self.repo, "rev-parse", "main"), tip)
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertNotIn("merged", self._events(record))

    def test_every_refusal_blocks_execution_with_nothing_changed(self):
        cases = {
            "worktree_dirty": lambda r: (Path(r.worktree_path) / "stray.txt").write_text("x\n", encoding="utf-8"),
            "target_checkout_dirty": lambda r: (self.repo / "docs" / "plan.md").write_text("edited\n", encoding="utf-8"),
            "human_gate_pending": self._pend_evidence,
            "candidate_mismatch": self._late_commit,
        }
        for code_name, break_it in cases.items():
            with self.subTest(code_name):
                self.tearDown()
                self.setUp()
                record, _ = self._complete()
                break_it(record)
                before = self._refs_and_files(record)
                code, report, _ = self._finish(record)
                self.assertEqual(code, 3)
                self.assertEqual(report["stopped_at"], "checks")
                self.assertIn(code_name, report["reason"])
                self.assertEqual(self._refs_and_files(record), before)
                self.assertFalse(managed_finish.archive_dir(self.repo, record.run_key).exists())

    def _pend_evidence(self, record):
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        state = PlanRunState.load(state_path)
        state.evidence_pending = {"stage": "x", "sparring_digest": "y"}
        state.save(state_path)

    def _late_commit(self, record):
        worktree = Path(record.worktree_path)
        (worktree / "late.txt").write_text("late\n", encoding="utf-8")
        _run_git(worktree, "add", "late.txt")
        _run_git(worktree, "commit", "-q", "-m", "late")

    def test_unpushed_candidate_and_live_runner_refuse(self):
        record, _ = self._complete(push=False)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("candidate_not_pushed", report["reason"])
        _run_git(Path(record.worktree_path), "push", "-q", "origin", record.branch)
        with worktree_lock(Path(record.worktree_path)):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("runner_live", report["reason"])
        self.assertNotIn("merged", self._events(record))

    def test_unmanaged_worktree_is_never_removed(self):
        record, _ = self._complete()
        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["created_by"] = "person"
        path.write_text(json.dumps(payload), encoding="utf-8")
        main_before = _git(self.repo, "rev-parse", "main")
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("unmanaged", report["reason"])
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main_before)
        unknown = self.repo.parent / "loose"
        _run_git(self.repo, "worktree", "add", "-q", "-b", "loose", str(unknown))
        code, out, _ = self._main("finish-run", "--run-key", "loose-1234abcd", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 3)
        self.assertTrue(unknown.is_dir())

    def test_merge_only_stops_before_cleanup_then_finish_completes(self):
        record, head = self._complete()
        code, report, err = self._finish(record, "--merge-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"], ["merge"])
        self.assertEqual(report["remaining"], [])
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertTrue(Path(record.worktree_path).is_dir())
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(self._events(record).count("merged"), 1)

    def test_push_target(self):
        record, head = self._complete()
        code, report, err = self._finish(record, "--merge-only", "--push-target")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"], ["merge", "push_target"])
        self.assertEqual(_git(self.remote, "rev-parse", "refs/heads/main"), head)
        self.assertIn("target_pushed", self._events(record))

    def test_rejected_target_push_stops_with_merge_recorded(self):
        record, head = self._complete()
        # The remote target moves on independently: a non-forced push is rejected.
        main = _git(self.repo, "rev-parse", "main")
        remote_only = _git(self.repo, "commit-tree", f"{main}^{{tree}}", "-p", main, "-m", "remote only")
        _run_git(self.repo, "push", "-q", "origin", f"{remote_only}:refs/heads/main")
        code, report, _ = self._finish(record, "--push-target")
        self.assertEqual(code, 1)
        self.assertEqual((report["stopped_at"], report["completed_steps"]), ("push_target", ["merge"]))
        self.assertEqual(report["remaining"][0], "push_target")
        self.assertIn("merged", self._events(record))
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)

    def test_partial_failure_then_rerun_completes(self):
        record, head = self._complete()
        _run_git(self.repo, "worktree", "lock", record.worktree_path)
        code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"], "remove_worktree")
        self.assertEqual(report["completed_steps"], ["merge", "archive_state"])
        self.assertEqual(report["remaining"], ["remove_worktree", "delete_branch", "delete_remote_branch", "finished"])
        self.assertTrue(managed_run.record_path(self.repo, record.run_key).is_file())
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), head)
        self.assertTrue(Path(record.worktree_path).is_dir())
        _run_git(self.repo, "worktree", "unlock", record.worktree_path)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        events = self._events(record)
        self.assertEqual(events.count("merged"), 1)
        self.assertEqual(events.count("state_archived"), 1)

    def test_rerun_after_worktree_removed_deletes_the_branch(self):
        record, head = self._complete()
        real_git = managed_run._git

        def failing_branch_delete(cwd, *args):
            if args[:2] == ("branch", "-d"):
                return subprocess.CompletedProcess(args, 1, "", "simulated failure")
            return real_git(cwd, *args)

        with mock.patch.object(managed_run, "_git", side_effect=failing_branch_delete):
            code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"], "delete_branch")
        self.assertEqual(report["remaining"], ["delete_branch", "delete_remote_branch", "finished"])
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), head)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"][-2:], ["delete_branch", "finished"])
        self._assert_cleaned(record, head)

    def test_repeated_finish_is_a_no_op(self):
        record, head = self._complete()
        self.assertEqual(self._finish(record)[0], 0)
        record_bytes = managed_run.record_path(self.repo, record.run_key).read_bytes()
        refs = _git(self.repo, "for-each-ref")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertIsNone(report["stopped_at"])
        self.assertEqual(report["reason"], "the run is already finished")
        self.assertEqual(managed_run.record_path(self.repo, record.run_key).read_bytes(), record_bytes)
        self.assertEqual(_git(self.repo, "for-each-ref"), refs)

    def test_remote_branch_deleted_only_under_policy_and_containment(self):
        record, _ = self._complete()
        head = self._set_policy(record, True)
        # Policy on, but the remote target does not contain the candidate: kept.
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertNotIn("delete_remote_branch", report["completed_steps"])
        self.assertIn(record.branch, _git(self.repo, "ls-remote", "origin"))
        self.assertNotIn("remote_branch_deleted", self._events(record))
        self._assert_cleaned(record, head)

    def test_remote_branch_deleted_with_policy_and_pushed_target(self):
        record, _ = self._complete()
        head = self._set_policy(record, True)
        code, report, err = self._finish(record, "--push-target")
        self.assertEqual(code, 0, err)
        self.assertIn("delete_remote_branch", report["completed_steps"])
        self.assertNotIn(record.branch, _git(self.repo, "ls-remote", "origin"))
        self.assertIn("remote_branch_deleted", self._events(record))

    def test_remote_branch_kept_without_policy(self):
        record, _ = self._complete()
        self._set_policy(record, False)
        code, report, err = self._finish(record, "--push-target")
        self.assertEqual(code, 0, err)
        self.assertNotIn("delete_remote_branch", report["completed_steps"])
        self.assertIn(record.branch, _git(self.repo, "ls-remote", "origin"))


class FinishConfigTests(unittest.TestCase):
    def test_finish_table(self):
        from agent_sparring.config import ProjectConfigError, parse_project_config

        self.assertFalse(parse_project_config('project = "p"\n').finish_delete_remote_branch)
        config = parse_project_config('project = "p"\n[finish]\ndelete_remote_branch = true\n')
        self.assertTrue(config.finish_delete_remote_branch)
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = "p"\n[finish]\ndelete_remote = true\n')
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = "p"\n[finish]\ndelete_remote_branch = "yes"\n')


class FinishRecoveryTests(_FinishTestCase):
    """Ignored-file preservation, interrupted cleanup and operational failures."""

    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _assert_cleaned = FinishExecutionTests._assert_cleaned

    def _ignored_collision(self):
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "feature.txt\n", encoding="utf-8")
        (self.repo / "feature.txt").write_text("ignored but mine\n", encoding="utf-8")

    def test_ignored_user_file_is_never_overwritten_by_a_fast_forward(self):
        record, _ = self._complete()
        self._ignored_collision()
        tip = _git(self.repo, "rev-parse", "main")
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (1, "merge", []))
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "ignored but mine\n")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), tip)
        self.assertNotIn("merged", self._events(record))
        self.assertTrue(Path(record.worktree_path).is_dir())

    def test_ignored_user_file_is_never_overwritten_by_a_merge_commit(self):
        record, _ = self._complete()
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        _run_git(self.repo, "add", "other.txt")
        _run_git(self.repo, "commit", "-q", "-m", "other")
        self._ignored_collision()
        tip = _git(self.repo, "rev-parse", "main")
        code, report, _ = self._finish(record, "--allow-merge-commit")
        self.assertEqual((code, report["stopped_at"]), (1, "merge"))
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "ignored but mine\n")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), tip)
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_removal_without_its_event_is_reconciled_on_rerun(self):
        record, head = self._complete()
        real_append = managed_run.append_event

        def fail_removed(repo_root, run_key, event, detail=None):
            if event == "worktree_removed":
                raise OSError("simulated crash after removal")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_removed):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "remove_worktree"))
        self.assertIn("simulated crash", report["reason"])
        self.assertFalse(Path(record.worktree_path).exists())
        self.assertNotIn("worktree_removed", self._events(record))
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(self._events(record).count("worktree_removed"), 1)

    def test_archive_failure_is_a_structured_report(self):
        record, head = self._complete()
        with mock.patch.object(managed_finish.shutil, "copy2", side_effect=OSError("disk full")):
            code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual((report["stopped_at"], report["completed_steps"]), ("archive_state", ["merge"]))
        self.assertIn("disk full", report["reason"])
        self.assertEqual(report["remaining"][0], "archive_state")
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), head)
        code, _, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)

    def test_record_write_failure_is_a_structured_report(self):
        record, _ = self._complete()
        with mock.patch.object(managed_run, "_write_temp", side_effect=PermissionError("read-only")):
            code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"], "merge")
        self.assertIn("read-only", report["reason"])

    def test_recovery_rechecks_target_containment(self):
        record, head = self._complete()
        real_git = managed_run._git

        def failing_remote_delete(cwd, *args):
            if args[:1] == ("push",) and args[-1].startswith(":"):
                return subprocess.CompletedProcess(args, 1, "", "simulated")
            return real_git(cwd, *args)

        # Get past local branch deletion, then stop before finished.
        real_append = managed_run.append_event

        def fail_finished(repo_root, run_key, event, detail=None):
            if event == "finished":
                raise OSError("simulated crash before finished")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_finished):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "delete_remote_branch"))
        # The target is moved back behind the candidate.
        _run_git(self.repo, "update-ref", "refs/heads/main", record.base_sha)
        _run_git(self.repo, "reset", "-q", "--hard", record.base_sha)
        for flags in ((), ("--merge-only",)):
            code, report, _ = self._finish(record, *flags)
            self.assertEqual((code, report["stopped_at"]), (1, "merge"), flags)
            self.assertIn("no longer contains", report["reason"])
        self.assertNotIn("finished", self._events(record))
