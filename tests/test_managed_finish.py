import hashlib
import json
import os
import subprocess
import time
import unittest
from unittest import mock
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import managed_finish, managed_run
from agent_sparring.concurrency import worktree_lock
from agent_sparring.plan import MarkdownPlanSource, PlanRunState, PlanRunStatus, load_plan_source
from agent_sparring.stage import Stage, StageStatus

from git_recorder import GitRecorder
from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import _run_git


#: Plan L Stage 4: finish-run is local. The remote finish -- the pushed
#: check, ``--push-target`` and ``[finish] delete_remote_branch`` -- is gone
#: from the engine, and with it the test-only switch that ran it with the
#: I-L1 refusal patched out. Each test of it is converted or removed with
#: its named replacement in the Stage 4 handoff; what remains of the
#: properties is :class:`RemoteFinishRemovedTests`.


class _FinishTestCase(_ManagedRepoTestCase):
    def _legacy_pin(self, record) -> None:
        """Make ``record``'s run what an engine from before local-first runs
        left: its managed record says ``pinned``, and its run state holds the
        remote pin taken at its start (with that pin's trusted snapshot)
        instead of a local-first run's own snapshot.

        Plan L: local-first runs pin nothing. A legacy run's pin loads
        inertly: finish reads only the trusted snapshot it carries, as the
        run's local configuration guard, and never its remote."""

        from legacy_pins import legacy_pin

        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["pinned"] = True
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        run_state = PlanRunState.load(state_path)
        run_state.remote_pin = legacy_pin(Path(record.worktree_path), record.branch)
        run_state.config_snapshot = None
        run_state.save(state_path)
        # An engine from before local-first runs never wrote the snapshot
        # record either.
        managed_run.snapshot_flag_path(self.repo, record.run_key).unlink(missing_ok=True)

    #: Runs are local-first by default; a class about legacy runs sets this.
    LEGACY_PIN = False

    def _complete(self, *, push: bool = True, legacy_pin: bool | None = None):
        """A managed run with every stage accepted at the worktree's HEAD
        (a legacy pinned run when ``legacy_pin``, default
        :attr:`LEGACY_PIN`; see :meth:`_legacy_pin`)."""

        self._start_managed()
        if self.LEGACY_PIN if legacy_pin is None else legacy_pin:
            (started,) = self._records()
            self._legacy_pin(started)
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
        # Plan L: a finish reads no remote (was: git["candidate_pushed"]
        # true, read from origin). Stage 4: there is no candidate_not_pushed
        # check at all and nothing is kept on a remote; the local history and
        # the no-remote-finish checks hold.
        self.assertIsNone(git["candidate_pushed"])
        codes = {c["code"]: c for c in finish["checks"]}
        self.assertNotIn("candidate_not_pushed", codes)
        self.assertTrue(codes["run_history_rewritten"]["ok"])
        self.assertTrue(codes["remote_finish_removed"]["ok"])
        self.assertEqual(finish["kept"], [])
        self.assertEqual(git["siblings"], [])
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

    def test_an_unpushed_candidate_of_a_legacy_pinned_run_is_eligible_locally(self):
        # Converted from test_candidate_not_pushed (Plan L Stage 4): a legacy
        # pinned run's unpushed candidate was refused candidate_not_pushed.
        # Now the pin is inert and nothing remote is an input: the finish is
        # eligible on the local checks alone, and status reads no remote.
        record, head = self._complete(push=False, legacy_pin=True)
        self.assertEqual(record.remote, "origin")
        remote_before = _git(self.remote, "for-each-ref")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            status = self._status(record)
        failed = {c["code"] for c in status["finish"]["checks"] if not c["ok"]}
        self.assertEqual(failed, set())
        self.assertEqual(status["finish"]["eligible"], {"merge": True, "cleanup": True})
        self.assertIsNone(status["git"]["candidate_pushed"])
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)
        self.assertNotIn(head, remote_before)

    def test_the_recorded_remote_and_upstream_configuration_are_never_read(self):
        # Converted from test_candidate_pushed_is_bound_to_the_recorded_
        # remote_and_branch: what the record's remote or the branch's
        # upstream holds no longer decides anything. A record naming another
        # remote, and upstream configuration pointing elsewhere, leave the
        # local status exactly as it is -- and no remote is contacted.
        other = self.repo.parent / "other.git"
        _run_git(self.repo.parent, "init", "-q", "--bare", str(other))
        _run_git(self.repo, "remote", "add", "upstream", str(other))
        record, head = self._complete(push=False)
        worktree = Path(record.worktree_path)
        _run_git(worktree, "config", f"branch.{record.branch}.remote", "origin")
        _run_git(worktree, "config", f"branch.{record.branch}.merge", "refs/heads/elsewhere")
        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["remote"] = "upstream"
        path.write_text(json.dumps(payload), encoding="utf-8")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            failed, finish = self._failed(record)
        self.assertEqual(failed, set())
        self.assertTrue(finish["eligible"]["merge"])
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(other, "for-each-ref"), "")

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
        # Plan L adds the engine's capabilities to finish-run --json (an
        # added field); everything else is the same report.
        dry_run = json.loads(out)
        self.assertEqual(dry_run.pop("capabilities"), ["local_first"])
        self.assertEqual(dry_run, run["finish"])
        # Plan L Stage 4 adds "siblings" (an added field).
        self.assertEqual(set(run["git"]), {
            "head", "branch_tip", "clean", "final_candidate", "candidate_pushed",
            "target_tip", "target_contains_candidate", "target_checked_out_at", "siblings",
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

    def test_a_live_runner_refuses_and_an_unpushed_candidate_then_finishes_locally(self):
        # Converted from test_unpushed_candidate_and_live_runner_refuse
        # (Plan L Stage 4): an unpushed candidate of a legacy pinned run is no
        # longer refused; a live runner still is.
        record, head = self._complete(push=False, legacy_pin=True)
        with worktree_lock(Path(record.worktree_path)):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("runner_live", report["reason"])
        self.assertNotIn("merged", self._events(record))
        remote_before = _git(self.remote, "for-each-ref")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)

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

    def test_push_target_refuses_with_remote_finish_removed(self):
        # Converted from test_push_target (Plan L Stage 4, L-D3): the flag is
        # never an alias for a local finish. Refused before anything is read
        # or changed, merge-only or not; nothing is merged or pushed.
        record, head = self._complete()
        before = self._refs_and_files(record)
        remote_before = _git(self.remote, "for-each-ref")
        for flags in (("--merge-only", "--push-target"), ("--push-target",), ("--push-target", "--dry-run")):
            with self.subTest(flags=flags):
                code, report, err = self._finish(record, *flags)
                self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (3, "checks", []), err)
                self.assertIn("remote_finish_removed", report["reason"])
                self.assertIn("Push to review", report["reason"])
                self.assertEqual(report["capabilities"], ["local_first"])
        self.assertEqual(self._refs_and_files(record), before)
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)
        self.assertNotIn("merged", self._events(record))
        # Positive control: the same finish without the flag merges locally.
        code, report, err = self._finish(record, "--merge-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)

    def test_partial_failure_then_rerun_completes(self):
        record, head = self._complete()
        _run_git(self.repo, "worktree", "lock", record.worktree_path)
        code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual(report["stopped_at"], "remove_worktree")
        self.assertEqual(report["completed_steps"], ["merge", "archive_state"])
        self.assertEqual(report["remaining"], ["remove_worktree", "delete_branch", "finished"])
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
        self.assertEqual(report["remaining"], ["delete_branch", "finished"])
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

    def test_delete_remote_branch_false_finishes_locally_and_emits_no_keep_entry(self):
        # Converted from test_remote_branch_kept_by_default_with_alias_and_
        # precise_code (Plan L Stage 4): no keep_remote_branch entry and no
        # remote_delete_* code any more; the finish is local, and the remote
        # branch is never read, kept or deleted by it.
        record, _ = self._complete(legacy_pin=True)
        head = self._set_policy(record, False)
        finish = self._status(record)["finish"]
        self.assertEqual(finish["kept"], [])
        self.assertFalse(any("remote" in action for action in finish["actions"]))
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"], ["merge", "archive_state", "remove_worktree", "delete_branch", "finished"])
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["remote_branch"]["code"], "local_finish")
        self.assertEqual(_git(self.remote, "rev-parse", f"refs/heads/{record.branch}"), head)
        self._assert_cleaned(record, head)

    def test_without_a_policy_the_remote_branch_is_untouched_and_reported_local(self):
        # Converted from test_remote_branch_kept_without_policy.
        record, head = self._complete()
        remote_before = _git(self.remote, "for-each-ref")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual((report["kept"], report["remote_branch"]["code"]), ([], "local_finish"))
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)
        self.assertIn(record.branch, remote_before)
        self._assert_cleaned(record, head)


class RemoteFinishRemovedTests(_FinishTestCase):
    """Plan L Stage 4 (I-L1, L-D3): finish-run is local.

    Replaces ``RemoteBranchDeletionTests``: ``[finish] delete_remote_branch``
    no longer deletes anything -- a reviewed candidate that sets it true is
    refused with ``remote_finish_removed`` and nothing is merged -- and the
    module that finishes runs no remote command at all. The tests of the
    compare-and-delete (its lease form, races, hook and porcelain handling,
    an absent branch, a remote target missing or unknown, a push URL with a
    space) are removed with that code; the Stage 4 handoff names each with
    the test here that covers what remains of its property. Planted
    configuration is still refused by name, now for the local finish."""

    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _set_policy = FinishExecutionTests._set_policy
    _assert_cleaned = FinishExecutionTests._assert_cleaned

    def test_delete_remote_branch_refuses_and_nothing_is_merged(self):
        for legacy in (False, True):
            with self.subTest(legacy_pin=legacy):
                self.tearDown()
                self.setUp()
                record, _ = self._complete(legacy_pin=legacy)
                head = self._set_policy(record, True)
                before = self._refs_and_files(record)
                remote_before = _git(self.remote, "for-each-ref")
                recorder = GitRecorder(self.repo.parent)
                with recorder.active():
                    failed, finish = self._failed(record)
                    dry = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo),
                                     "--dry-run", "--json")
                    code, report, err = self._finish(record)
                self.assertEqual(failed, {"remote_finish_removed"})
                (check,) = [c for c in finish["checks"] if c["code"] == "remote_finish_removed"]
                self.assertIn("delete_remote_branch", check["detail"])
                self.assertIn("Push to review", check["detail"])
                self.assertEqual(dry[0], 3, dry[2])
                self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (3, "checks", []), err)
                self.assertIn("remote_finish_removed", report["reason"])
                self.assertEqual(self._refs_and_files(record), before)
                self.assertNotIn("merged", self._events(record))
                self.assertEqual(recorder.remote_commands(), [])
                self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)
                self.assertEqual(_git(self.remote, "rev-parse", f"refs/heads/{record.branch}"), head)

    def test_positive_control_without_the_setting_the_same_run_finishes_locally(self):
        record, _ = self._complete(legacy_pin=True)
        head = self._set_policy(record, False)
        remote_before = _git(self.remote, "for-each-ref")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)

    def test_the_finish_module_runs_no_remote_command(self):
        # Replaces test_the_lease_is_the_only_force_in_the_module: the
        # module that finishes names no remote Git command and no force.
        import io
        import tokenize

        source = Path(managed_finish.__file__).read_text(encoding="utf-8")
        strings = [
            tok.string.strip("\"'") for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.STRING
        ]
        for command in ("push", "fetch", "pull", "ls-remote", "--force", "--force-with-lease", "-f"):
            self.assertNotIn(command, strings)
        self.assertFalse(hasattr(managed_finish, "_delete_remote_branch"))
        self.assertFalse(hasattr(managed_finish, "_push_target"))

    def test_a_remote_url_changed_mid_finish_is_refused_by_name_then_rerun_completes(self):
        # Converted (Plan L Stage 4): the finish is local, so the change is
        # made just before its merge rather than its remote delete. The
        # engine's next Git invocation refuses, naming the key; nothing is
        # merged, and nothing reaches the new URL. Restored, it completes.
        record, head = self._complete(legacy_pin=True)
        url = _git(self.repo, "remote", "get-url", "origin")
        gone = Path(str(self.remote) + ".gone")
        subprocess.run(["git", "init", "-q", "--bare", str(gone)], check=True, capture_output=True)
        real_git = managed_run._git

        def retarget_before_merge(cwd, *args):
            if args[:1] == ("merge",):
                subprocess.run(["git", "-C", str(self.repo), "remote", "set-url", "origin", str(gone)], check=True)
            return real_git(cwd, *args)

        with mock.patch.object(managed_run, "_git", side_effect=retarget_before_merge):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "merge"))
        self.assertIn("repository_config_changed", report["reason"])
        self.assertIn("remote.origin.url", report["reason"])
        self.assertNotIn("merged", self._events(record))
        self.assertNotEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(_git(gone, "for-each-ref"), "")
        _run_git(self.repo, "remote", "set-url", "origin", url)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)

    def test_a_push_url_added_after_the_run_started_is_refused_by_name(self):
        # Converted (Plan L Stage 4): a planted remote.origin.pushurl is a
        # change to the run's trusted configuration, so the local finish
        # refuses at its first Git invocation, naming the key. Nothing is
        # merged, and the planted destination receives nothing.
        record, head = self._complete()
        mirror = self.remote.parent / "push.git"
        subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True, capture_output=True)
        _run_git(self.repo, "remote", "set-url", "--push", "origin", str(mirror))
        main_before = _git(self.repo, "rev-parse", "main")
        code, report, _ = self._finish(record)
        self.assertNotEqual(code, 0)
        self.assertIsNotNone(report["stopped_at"])
        self.assertIn("repository_config_changed", report["reason"])
        self.assertIn("remote.origin.pushurl", report["reason"])
        self.assertEqual(_git(mirror, "for-each-ref"), "")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main_before)
        self.assertNotIn("merged", self._events(record))
        # Positive control: with the planted key gone, the same run finishes.
        _run_git(self.repo, "config", "--unset", "remote.origin.pushurl")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)

    def test_a_record_naming_another_branch_is_never_deleted(self):
        # Converted (Plan L Stage 4): the remote deletion it guarded is gone;
        # the same rule guards the local branch deletion, which only ever
        # deletes a managed branch distinct from its target.
        import dataclasses

        record, _ = self._complete()
        for branch in ("main", "someone-elses-branch"):
            forged = dataclasses.replace(record, branch=branch)
            self.assertIn("managed branch", managed_finish._foreign_branch(forged))
        self.assertIsNone(managed_finish._foreign_branch(record))
        legacy = dataclasses.replace(record, branch="sparring/feature-plan-0123abcd")
        self.assertIsNone(managed_finish._foreign_branch(legacy))
        self.assertIsNotNone(managed_finish._foreign_branch(dataclasses.replace(legacy, target_branch=legacy.branch)))


class PlanRemovalTests(_FinishTestCase):
    """``[finish] remove_plan``: byte-exact removal of the plan on the target."""

    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events

    def _enable(self, record, *, edit_title: bool = False):
        """Commit ``[finish] remove_plan = true`` (and optionally a one-byte
        plan edit outside the stages) into the candidate."""

        worktree = Path(record.worktree_path)
        toml = worktree / ".sparring" / "project.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + "\n[finish]\nremove_plan = true\n", encoding="utf-8")
        _run_git(worktree, "add", ".sparring/project.toml")
        if edit_title:
            plan = worktree / "docs" / "plan.md"
            plan.write_bytes(plan.read_bytes().replace(b"# Feature plan", b"# Feature plaN", 1))
            _run_git(worktree, "add", "docs/plan.md")
        _run_git(worktree, "commit", "-q", "-m", "policy")
        _run_git(worktree, "push", "-q", "origin", record.branch)
        head = _git(worktree, "rev-parse", "HEAD")
        for stage_dir in (record.sparring_dir / "stages").iterdir():
            stage = Stage.resolve(record.sparring_dir, stage_dir.name)
            state = stage.read_state()
            state.candidate_sha = head
            stage.write_state(state)
        return head

    def _tracked(self, ref="main"):
        return _git(self.repo, "ls-tree", "-r", "--name-only", ref).splitlines()

    def _assert_removed(self, record, head, report):
        removal = report["plan_removal"]
        self.assertEqual(removal["code"], "plan_removed", removal)
        self.assertEqual(_git(self.repo, "rev-parse", "main^"), head)
        self.assertEqual(_git(self.repo, "rev-parse", "main"), removal["commit"])
        self.assertNotIn("docs/plan.md", self._tracked())
        message = _git(self.repo, "log", "-1", "--format=%B", "main")
        self.assertTrue(message.startswith(
            f"sparring: remove finished plan {record.plan_label} (run {record.run_key})"
        ), message)
        self.assertIn(f"Sparring-Run: {record.run_key}", message)
        self.assertEqual(_git(self.repo, "log", "-1", "--format=%an <%ae>", "main"), "Test <test@example.com>")
        (event,) = [e for e in managed_run.read_record(self.repo, record.run_key).events if e["event"] == "plan_removed"]
        self.assertEqual(event["detail"]["path"], "docs/plan.md")
        self.assertEqual(event["detail"]["commit"], removal["commit"])

    def test_default_off_leaves_the_plan(self):
        record, head = self._complete()
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_removal_disabled")
        self.assertNotIn("remove_plan", report["completed_steps"])
        self.assertIn("docs/plan.md", self._tracked())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)

    def test_removal_succeeds_and_the_archive_holds_the_snapshot(self):
        record, _ = self._complete()
        plan_bytes = Path(record.input_path).read_bytes()
        head = self._enable(record)
        self.assertTrue(any("remove the plan" in a for a in self._status(record)["finish"]["actions"]))
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"][:2], ["merge", "remove_plan"])
        self._assert_removed(record, head, report)
        self.assertFalse((self.repo / "docs" / "plan.md").exists())
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self._events(record)[-1], "finished")
        archive = managed_finish.archive_dir(self.repo, record.run_key)
        self.assertEqual((archive / "plans" / f"{record.run_key}.source").read_bytes(), plan_bytes)
        self.assertTrue((archive / "plans" / f"{record.run_key}.source.sha256").is_file())

    def test_one_byte_edit_outside_the_stages_is_plan_changed(self):
        record, _ = self._complete()
        head = self._enable(record, edit_title=True)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)  # a reported refusal, not a stop
        self.assertEqual(report["plan_removal"]["code"], "plan_changed")
        self.assertIn("docs/plan.md", self._tracked())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(self._events(record)[-1], "finished")

    def test_a_removal_commit_whose_event_was_lost_is_reconciled(self):
        # Converted from ..._is_reconciled_and_pushed (Plan L Stage 4): the
        # reconciliation is unchanged; the finish pushes nothing, so the
        # remote target is left exactly as it was.
        record, _ = self._complete()
        head = self._enable(record)
        real = managed_run.append_event
        remote_before = _git(self.remote, "for-each-ref")

        def interrupted(repo_root, run_key, event, *args, **kwargs):
            if event == "plan_removed":
                raise KeyboardInterrupt("killed after the removal commit")
            return real(repo_root, run_key, event, *args, **kwargs)

        with mock.patch.object(managed_run, "append_event", side_effect=interrupted):
            code, _, _ = self._finish(record)
        self.assertNotEqual(code, 0)
        removal = _git(self.repo, "rev-parse", "main")
        self.assertEqual(_git(self.repo, "rev-parse", "main^"), head)
        self.assertNotIn("plan_removed", self._events(record))
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_removed", report["plan_removal"])
        self.assertEqual(report["plan_removal"]["commit"], removal)
        (event,) = [e for e in managed_run.read_record(self.repo, record.run_key).events if e["event"] == "plan_removed"]
        self.assertEqual(event["detail"]["commit"], removal)
        self.assertTrue(event["detail"]["reconciled"])
        self.assertNotIn("plan_removal_refused", self._events(record))
        self.assertEqual(_git(self.repo, "rev-parse", "main"), removal)
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)

    def test_the_removal_commit_leaves_what_is_staged_meanwhile(self):
        record, _ = self._complete()
        head = self._enable(record)
        real = managed_run._git

        def stage_after_check(cwd, *args):
            if args[:2] == ("rm", "-q"):
                (Path(cwd) / "unrelated.txt").write_text("mine\n", encoding="utf-8")
                real(cwd, "add", "unrelated.txt")
            return real(cwd, *args)

        with mock.patch.object(managed_run, "_git", side_effect=stage_after_check):
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_removed(record, head, report)
        self.assertNotIn("unrelated.txt", self._tracked())
        self.assertEqual(_git(self.repo, "diff", "--cached", "--name-only"), "unrelated.txt")

    def _before_commit(self, action):
        real = managed_finish._commit_removal_in_checkout

        def wrapper(*args, **kwargs):
            action()
            return real(*args, **kwargs)

        return mock.patch.object(managed_finish, "_commit_removal_in_checkout", side_effect=wrapper)

    def test_dirty_plan_in_the_target_checkout_is_refused_without_stopping(self):
        record, _ = self._complete()
        head = self._enable(record)
        with self._before_commit(lambda: (self.repo / "docs" / "plan.md").write_text("mine\n", encoding="utf-8")):
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "target_checkout_dirty")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        self.assertEqual((self.repo / "docs" / "plan.md").read_text(encoding="utf-8"), "mine\n")
        self.assertEqual(_git(self.repo, "stash", "list"), "")

    def test_unrelated_unstaged_edit_does_not_block_and_is_kept(self):
        record, _ = self._complete()
        head = self._enable(record)
        with self._before_commit(lambda: (self.repo / "feature.txt").write_text("my edit\n", encoding="utf-8")):
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_removed(record, head, report)
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "my edit\n")
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "M feature.txt")
        self.assertEqual(_git(self.repo, "stash", "list"), "")

    def test_no_checkout_has_the_target_uses_update_ref(self):
        record, _ = self._complete()
        head = self._enable(record)
        _run_git(self.repo, "checkout", "-q", "feature/x")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_removed(record, head, report)
        self.assertEqual(_git(self.repo, "symbolic-ref", "--short", "HEAD"), "feature/x")

    def test_the_merge_and_the_removal_land_on_the_local_target_and_nothing_is_pushed(self):
        # Converted from test_push_target_carries_the_merge_and_the_removal_
        # in_one_push (Plan L Stage 4): both commits are on the local target;
        # the recorder sees no remote command and origin's refs are unchanged.
        record, _ = self._complete()
        head = self._enable(record)
        remote_before = _git(self.remote, "for-each-ref")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["completed_steps"][:2], ["merge", "remove_plan"])
        self._assert_removed(record, head, report)
        self.assertEqual(_git(self.repo, "rev-parse", "main"), report["plan_removal"]["commit"])
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)

    def test_resume_after_removal_does_not_remove_twice(self):
        record, _ = self._complete()
        head = self._enable(record)
        _run_git(self.repo, "worktree", "lock", record.worktree_path)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "remove_worktree"))
        self.assertEqual(report["plan_removal"]["code"], "plan_removed")
        _run_git(self.repo, "worktree", "unlock", record.worktree_path)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(self._events(record).count("plan_removed"), 1)
        self._assert_removed(record, head, report)

    def test_old_style_run_without_a_snapshot(self):
        from agent_sparring.plan import plan_snapshot_paths

        record, _ = self._complete()
        head = self._enable(record)
        for path in plan_snapshot_paths(record.sparring_dir, record.run_key):
            path.unlink()
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_snapshot_missing")
        self.assertIn("docs/plan.md", self._tracked())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)


    def test_a_refusal_is_final_even_after_the_condition_is_fixed(self):
        record, _ = self._complete()
        head = self._enable(record)
        with self._before_commit(lambda: (self.repo / "docs" / "plan.md").write_text("mine\n", encoding="utf-8")):
            _run_git(self.repo, "worktree", "lock", record.worktree_path)
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "remove_worktree"))
        self.assertEqual(report["plan_removal"]["code"], "target_checkout_dirty")
        self.assertIn("plan_removal_refused", self._events(record))
        # Fix the condition, then resume: the refusal stands.
        _run_git(self.repo, "checkout", "--", "docs/plan.md")
        _run_git(self.repo, "worktree", "unlock", record.worktree_path)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "target_checkout_dirty")
        self.assertIn("docs/plan.md", self._tracked())
        self.assertTrue((self.repo / "docs" / "plan.md").is_file())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)
        # Replaying the finished run shows the recorded code.
        code, report, err = self._finish(record)
        self.assertEqual((code, report["plan_removal"]["code"]), (0, "target_checkout_dirty"))

    def test_snapshot_not_matching_the_record_digest_is_refused(self):
        from agent_sparring.plan import plan_snapshot_paths

        record, _ = self._complete()
        head = self._enable(record)
        source_path, digest_path = plan_snapshot_paths(record.sparring_dir, record.run_key)
        forged = b"forged\n"
        source_path.write_bytes(forged)
        digest_path.write_text(hashlib.sha256(forged).hexdigest() + "\n", encoding="utf-8")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_snapshot_mismatch")
        self.assertIn("docs/plan.md", self._tracked())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)

    def test_record_without_a_digest_is_snapshot_missing(self):
        record, _ = self._complete()
        self._enable(record)
        path = managed_run.record_path(self.repo, record.run_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for event in payload["events"]:
            event["detail"].pop("plan_sha256", None)
        path.write_text(json.dumps(payload), encoding="utf-8")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_snapshot_missing")
        self.assertIn("docs/plan.md", self._tracked())

    def test_git_identity_missing_is_refused(self):
        record, _ = self._complete()
        head = self._enable(record)
        _run_git(self.repo, "config", "--unset", "user.name")
        _run_git(self.repo, "config", "--unset", "user.email")
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                            "EMAIL")}
        # The engine ignores GIT_CONFIG_GLOBAL (see engine_git._environment):
        # the user's own configuration is found from HOME, so that is what
        # is emptied here.
        empty_home = self.repo.parent / "empty-home"
        (empty_home / ".config").mkdir(parents=True)
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(empty_home), "XDG_CONFIG_HOME": str(empty_home / ".config"),
        })
        with mock.patch.dict(os.environ, env, clear=True):
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "git_identity_missing")
        self.assertIn("docs/plan.md", self._tracked())
        self.assertEqual(_git(self.repo, "rev-parse", "main"), head)

    def test_a_symlinked_plan_at_the_target_is_not_removed(self):
        record, _ = self._complete()
        self._enable(record)
        real = managed_finish._git_bytes

        def as_symlink(cwd, *args, **kwargs):
            result = real(cwd, *args, **kwargs)
            if args[:1] == ("ls-tree",):
                result = subprocess.CompletedProcess(
                    result.args, 0, result.stdout.replace(b"100644 blob", b"120000 blob", 1), result.stderr
                )
            return result

        with mock.patch.object(managed_finish, "_git_bytes", side_effect=as_symlink):
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_not_tracked")
        self.assertIn("docs/plan.md", self._tracked())

class PruneTests(_FinishTestCase):
    """``sparring prune``: selection, the summary first, nothing unfinished."""

    NOW = time.time()  # the CLI reads the real clock

    def _fake(self, key: str, *, finished_days_ago: float | None, archive: bool = True,
              lifecycle_events=("created", "merged")):
        from datetime import datetime, timezone

        base = _git(self.repo, "rev-parse", "main")
        events = [{"at": "2026-01-01T00:00:00Z", "event": e,
                   "detail": {"candidate": base, "mode": "fast_forward", "target_sha": base} if e == "merged" else {}}
                  for e in lifecycle_events]
        if finished_days_ago is not None:
            at = datetime.fromtimestamp(self.NOW - finished_days_ago * 86400, timezone.utc)
            events.append({"at": at.isoformat(timespec="seconds").replace("+00:00", "Z"), "event": "finished",
                           "detail": {}})
        record = managed_run.ManagedRunRecord(
            run_key=key, plan_label="docs/plan.md", input_kind="markdown",
            input_path=str(self.repo / "docs" / "plan.md"),
            worktree_path=str(self.repo.parent / f"wt-{key}"), branch=f"sparring/plan-{key}",
            target_branch="main", base_sha=base, remote="origin", created_at="2026-01-01T00:00:00Z",
            events=tuple(events),
        )
        managed_run.create_record(self.repo, record)
        if archive:
            plans = managed_finish.archive_dir(self.repo, key) / "plans"
            plans.mkdir(parents=True)
            (plans / f"{key}.json").write_text("{}", encoding="utf-8")
            (plans / f"{key}.source").write_bytes(b"# plan\n")
            (plans / f"{key}.source.sha256").write_text(hashlib.sha256(b"# plan\n").hexdigest() + "\n")
        return record

    def _prune(self, **kwargs):
        return managed_finish.prune(self.repo, now=self.NOW, **kwargs)

    def _actions(self, report):
        return {(i["kind"], i["run_key"]): (i["action"], i["reason"]) for i in report["items"]}

    def _summary(self):
        path = managed_finish.prune_summary_path(self.repo)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def test_dry_run_without_selectors_is_todays_report(self):
        self._fake("old", finished_days_ago=40)
        code, out, err = self._main("prune", "--dry-run", "--json", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report, managed_finish.prune_report(self.repo))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report), {"schema_version", "dry_run", "items", "engine_snapshots"})
        self.assertTrue(managed_run.record_path(self.repo, "old").is_file())

    def test_refuses_to_delete_without_a_selector(self):
        self._fake("old", finished_days_ago=40)
        code, _, err = self._main("prune", "--repo-root", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("--older-than", err)
        self.assertTrue(managed_run.record_path(self.repo, "old").is_file())

    def test_age_and_keep_selection(self):
        self._fake("a", finished_days_ago=40)
        self._fake("b", finished_days_ago=20)
        self._fake("c", finished_days_ago=1)
        report = self._prune(dry_run=True, older_than_days=10)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["criteria"], {"older_than_days": 10, "keep": None})
        actions = self._actions(report)
        self.assertEqual(actions[("record", "a")], ("would_delete", "finished"))
        self.assertEqual(actions[("archive", "b")], ("would_delete", "finished"))
        self.assertEqual(actions[("record", "c")], ("kept", "too_recent"))
        actions = self._actions(self._prune(dry_run=True, keep=2))
        self.assertEqual(actions[("record", "a")], ("would_delete", "finished"))
        self.assertEqual(actions[("record", "b")], ("kept", "within_keep"))
        # Both selectors: an item must meet both.
        actions = self._actions(self._prune(dry_run=True, keep=1, older_than_days=30))
        self.assertEqual(actions[("record", "a")], ("would_delete", "finished"))
        self.assertEqual(actions[("record", "b")], ("kept", "too_recent"))
        self.assertEqual(actions[("record", "c")], ("kept", "within_keep"))
        self.assertEqual(self._summary(), [])  # a dry run writes nothing
        # And for real.
        report = self._prune(dry_run=False, older_than_days=10)
        self.assertFalse(report["dry_run"])
        self.assertEqual(self._actions(report)[("record", "a")], ("deleted", "finished"))
        self.assertFalse(managed_run.record_path(self.repo, "a").exists())
        self.assertFalse(managed_finish.archive_dir(self.repo, "b").exists())
        self.assertTrue(managed_run.record_path(self.repo, "c").is_file())
        summary = {line["run_key"]: line for line in self._summary()}
        self.assertEqual(set(summary), {"a", "b"})
        line = summary["a"]
        self.assertEqual(line["schema_version"], 1)
        self.assertEqual(line["plan_sha256"], hashlib.sha256(b"# plan\n").hexdigest())
        self.assertEqual(line["merge"]["mode"], "fast_forward")
        self.assertTrue(line["archive_digest"])
        self.assertNotIn("stages", line)

    def test_unfinished_records_are_never_touched(self):
        self._fake("live", finished_days_ago=None)
        report = self._prune(dry_run=False, older_than_days=0, keep=0)
        self.assertEqual(self._actions(report)[("record", "live")], ("report_only", "worktree_missing"))
        self.assertTrue(managed_run.record_path(self.repo, "live").is_file())
        self.assertTrue(managed_finish.archive_dir(self.repo, "live").is_dir())
        self.assertEqual(self._summary(), [])

    def test_orphan_archive_is_eligible_by_mtime(self):
        self._fake("gone", finished_days_ago=40)
        managed_run.record_path(self.repo, "gone").unlink()
        archive = managed_finish.archive_dir(self.repo, "gone")
        os.utime(archive, (self.NOW - 50 * 86400, self.NOW - 50 * 86400))
        report = self._prune(dry_run=False, older_than_days=10)
        self.assertEqual(self._actions(report)[("archive", "gone")], ("deleted", "finished"))
        self.assertFalse(archive.exists())

    def test_summary_write_failure_deletes_nothing(self):
        self._fake("a", finished_days_ago=40)
        with mock.patch.object(managed_finish, "_append_summaries", side_effect=OSError("disk full")):
            code, out, _ = self._main("prune", "--older-than", "10", "--json", "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(self._actions(report)[("record", "a")], ("kept", "delete_failed"))
        self.assertTrue(managed_run.record_path(self.repo, "a").is_file())
        self.assertTrue(managed_finish.archive_dir(self.repo, "a").is_dir())

    def test_non_finite_older_than_is_refused(self):
        self._fake("a", finished_days_ago=40)
        for value in ("nan", "inf"):
            code, _, err = self._main("prune", "--older-than", value, "--repo-root", str(self.repo))
            self.assertEqual(code, 2, value)
        self.assertTrue(managed_run.record_path(self.repo, "a").is_file())

    def test_corrupt_summary_is_a_clean_error_and_deletes_nothing(self):
        self._fake("a", finished_days_ago=40)
        path = managed_finish.prune_summary_path(self.repo)
        path.write_bytes(b"{not json\n")
        code, out, err = self._main("prune", "--older-than", "10", "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("prune_summary_corrupt", err)
        self.assertNotIn("Traceback", err)
        self.assertTrue(managed_run.record_path(self.repo, "a").is_file())
        self.assertEqual(path.read_bytes(), b"{not json\n")

    def test_summary_is_idempotent_per_run(self):
        self._fake("a", finished_days_ago=40)
        lines = [{"run_key": "a", "schema_version": 1}]
        path = managed_finish.prune_summary_path(self.repo)
        managed_finish._append_summaries(path, lines)
        managed_finish._append_summaries(path, lines)
        self._prune(dry_run=False, older_than_days=10)
        self.assertEqual([line["run_key"] for line in self._summary()], ["a"])
        self.assertFalse(managed_run.record_path(self.repo, "a").exists())



class ManifestPlanRemovalTests(_FinishTestCase):
    """A manifest run removes only the manifest file, never its sidecars."""

    _finish = FinishExecutionTests._finish

    def test_only_the_manifest_file_is_removed(self):
        stage = {"stage_id": "m-stage-1", "label": "1", "title": "One", "brief": "Do one.", "mode": "implementation"}
        manifest = self.repo / "docs" / "run.json"
        manifest.write_text(json.dumps({"version": 1, "plan_label": "docs/run.json", "source_digest": "0" * 64,
                                        "stages": [stage]}), encoding="utf-8")
        sidecar = self.repo / "docs" / "run.json.approval"
        sidecar.write_text("approved\n", encoding="utf-8")
        _run_git(self.repo, "add", "docs/run.json", "docs/run.json.approval")
        _run_git(self.repo, "commit", "-q", "-m", "manifest")
        _run_git(self.repo, "push", "-q", "origin", "main")
        code, _, err = self._main("run-plan", "--manifest", str(manifest), "--repo-root", str(self.repo), "--managed")
        self.assertEqual(code, 0, err)
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        toml = worktree / ".sparring" / "project.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + "\n[finish]\nremove_plan = true\n", encoding="utf-8")
        _run_git(worktree, "add", ".sparring/project.toml")
        _run_git(worktree, "commit", "-q", "-m", "policy")
        _run_git(worktree, "push", "-q", "origin", record.branch)
        head = _git(worktree, "rev-parse", "HEAD")
        source = load_plan_source(Path(record.input_path), worktree, manifest=True)
        for planned in source.stages():
            stage_obj = Stage.resolve(record.sparring_dir, planned.stage_id)
            if not stage_obj.exists():
                stage_obj.create(run=record.run_key)
            state = stage_obj.read_state()
            state.status, state.candidate_sha, state.run = StageStatus.ACCEPTED, head, record.run_key
            stage_obj.write_state(state)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        run_state = PlanRunState.load(state_path)
        run_state.status, run_state.awaiting, run_state.evidence_pending = PlanRunStatus.COMPLETE, None, None
        run_state.save(state_path)
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["plan_removal"]["code"], "plan_removed", report)
        tracked = _git(self.repo, "ls-tree", "-r", "--name-only", "main").splitlines()
        self.assertNotIn("docs/run.json", tracked)
        self.assertIn("docs/run.json.approval", tracked)
        self.assertEqual(_git(self.repo, "rev-parse", "main^"), head)


class FinishSchemaTests(_FinishTestCase):
    _finish = FinishExecutionTests._finish

    def test_dry_run_runs_and_execution_all_stay_at_1(self):
        record, _ = self._complete()
        self.assertEqual(managed_finish.FINISH_SCHEMA_VERSION, 1)
        self.assertEqual(managed_finish.FINISH_REPORT_SCHEMA_VERSION, 1)  # new fields are additive
        self.assertEqual(self._status(record)["finish"]["schema_version"], 1)
        (run,) = managed_finish.runs_report(self.repo)["runs"]
        self.assertEqual(run["finish"]["schema_version"], 1)
        code, report, err = self._finish(record)
        self.assertEqual((code, report["schema_version"]), (0, 1), err)
        self.assertIn("plan_removal", report)
        self.assertIn("remote_branch", report)


class LegacyRemoteFinishRefusedTests(_FinishTestCase):
    """Plan L Stage 4 (I-L1, L-D3): a legacy pinned run's remote finish --
    ``--push-target`` and ``delete_remote_branch`` -- is refused with
    ``remote_finish_removed``, contacting nothing: the git recorder first on
    ``PATH`` sees no remote command, the remote's refs are unchanged, and
    nothing is merged. Positive control: the same legacy run, without the
    flag and the setting, finishes locally -- still contacting nothing."""

    LEGACY_PIN = True
    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _set_policy = FinishExecutionTests._set_policy
    _assert_cleaned = FinishExecutionTests._assert_cleaned

    def test_the_legacy_remote_finish_and_its_status_contact_no_remote(self):
        # Retargeted from Stage 3 (was: refused at its candidate_not_pushed
        # check, remote_operation_not_allowed in the detail).
        record, _ = self._complete()
        head = self._set_policy(record, True)
        remote_before = _git(self.remote, "for-each-ref")
        main_before = _git(self.repo, "rev-parse", "main")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            runs = self._main("runs", "--repo-root", str(self.repo), "--json")
            dry = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo),
                             "--dry-run", "--json")
            flagged = self._finish(record, "--push-target")
            code, report, err = self._finish(record)

        self.assertEqual(runs[0], 0, runs[2])
        self.assertEqual(dry[0], 3, dry[2])
        self.assertEqual((flagged[0], flagged[1]["stopped_at"]), (3, "checks"), flagged[2])
        self.assertIn("remote_finish_removed", flagged[1]["reason"])
        self.assertEqual((code, report["stopped_at"]), (3, "checks"), err)
        self.assertIn("remote_finish_removed", report["reason"])
        failed, finish = self._failed(record)
        self.assertEqual(failed, {"remote_finish_removed"})
        self.assertNotIn("candidate_not_pushed", {c["code"] for c in finish["checks"]})
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main_before)
        self.assertNotIn("merged", self._events(record))
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.remote, "rev-parse", f"refs/heads/{record.branch}"), head)

    def test_positive_control_the_same_legacy_run_finishes_locally(self):
        # Replaces ..._with_the_test_only_switch_the_same_finish_reaches_the_
        # remote: there is no remote finish to reach any more.
        record, _ = self._complete()
        head = self._set_policy(record, False)
        remote_before = _git(self.remote, "for-each-ref")
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(_git(self.remote, "for-each-ref"), remote_before)


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
        # Get past local branch deletion, then stop before finished.
        real_append = managed_run.append_event

        def fail_finished(repo_root, run_key, event, detail=None):
            if event == "finished":
                raise OSError("simulated crash before finished")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_finished):
            code, report, _ = self._finish(record)
        # The remote branch is always kept, so the failure is at finished.
        self.assertEqual((code, report["stopped_at"]), (1, "finished"))
        self.assertEqual(report["remaining"], ["finished"])
        # The target is moved back behind the candidate.
        _run_git(self.repo, "update-ref", "refs/heads/main", record.base_sha)
        _run_git(self.repo, "reset", "-q", "--hard", record.base_sha)
        for flags in ((), ("--merge-only",)):
            code, report, _ = self._finish(record, *flags)
            self.assertEqual((code, report["stopped_at"]), (1, "merge"), flags)
            self.assertIn("no longer contains", report["reason"])
        self.assertNotIn("finished", self._events(record))


class FinishHardeningTests(_FinishTestCase):
    """Project-state archival, target races, in-progress operations, the
    finished report and the record re-read under the lock."""

    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _assert_cleaned = FinishExecutionTests._assert_cleaned

    def _extra_state(self, record):
        """A reset stage's archived attempt, an unreadable stage and a loose file."""

        stages = record.sparring_dir / "stages"
        attempt = stages / ".archive" / "old-stage" / "attempt-1"
        attempt.mkdir(parents=True)
        (attempt / "state.json").write_text('{"previous": true}\n', encoding="utf-8")
        broken = stages / "broken-stage"
        broken.mkdir()
        (broken / "state.json").write_text("{not json", encoding="utf-8")
        (record.sparring_dir / "plans" / "notes.log").write_text("loose\n", encoding="utf-8")
        return {
            "stages/.archive/old-stage/attempt-1/state.json": '{"previous": true}\n',
            "stages/broken-stage/state.json": "{not json",
            "plans/notes.log": "loose\n",
        }

    def _after_checks(self, action):
        """Run ``action`` between the eligibility checks and the first write."""

        real = managed_finish.finish_status

        def wrapper(*args, **kwargs):
            status = real(*args, **kwargs)
            if kwargs.get("lock_held"):
                action()
            return status

        return mock.patch.object(managed_finish, "finish_status", side_effect=wrapper)

    def _ignore_build(self, record):
        worktree = Path(record.worktree_path)
        (worktree / "build").mkdir()
        (worktree / "build" / "out.bin").write_text("x", encoding="utf-8")
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "build/\n", encoding="utf-8")

    # -- F1: nothing under the project dir is deleted unarchived -------------------

    def test_reset_attempts_and_unreadable_stages_are_archived(self):
        record, head = self._complete()
        extra = self._extra_state(record)
        self.assertTrue(self._status(record)["finish"]["eligible"]["cleanup"])
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        archive = managed_finish.archive_dir(self.repo, record.run_key)
        for rel, content in extra.items():
            self.assertEqual((archive / rel).read_text(encoding="utf-8"), content, rel)
        self.assertFalse([p for p in report["deleted_ignored_paths"] if p.startswith(".sparring")])

    def test_unarchivable_entry_refuses_cleanup_with_nothing_removed(self):
        record, _ = self._complete()
        self._extra_state(record)
        secret = record.sparring_dir / "stages" / ".archive" / "old-stage" / "attempt-1" / "state.json"
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        failed, finish = self._failed(record)
        self.assertEqual(failed, {"unarchived_project_state"})
        self.assertEqual(finish["eligible"], {"merge": True, "cleanup": False})
        self.assertIn("stages/.archive/old-stage/attempt-1/state.json", next(
            c["detail"] for c in finish["checks"] if c["code"] == "unarchived_project_state"
        ))
        before = self._refs_and_files(record)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("unarchived_project_state", report["reason"])
        self.assertEqual(self._refs_and_files(record), before)
        self.assertTrue(secret.exists())
        self.assertFalse(managed_finish.archive_dir(self.repo, record.run_key).exists())

    def test_entry_becoming_unarchivable_after_the_checks_refuses_at_archive(self):
        record, head = self._complete()
        self._extra_state(record)
        secret = record.sparring_dir / "stages" / "broken-stage" / "state.json"
        self.addCleanup(secret.chmod, 0o644)
        with self._after_checks(lambda: secret.chmod(0)):
            code, report, _ = self._finish(record)
        self.assertEqual(code, 1)
        self.assertEqual((report["stopped_at"], report["completed_steps"]), ("archive_state", ["merge"]))
        self.assertIn("unarchived_project_state", report["reason"])
        self.assertTrue(secret.exists())
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), head)
        self.assertNotIn("state_archived", self._events(record))

    def test_file_written_after_archiving_blocks_removal(self):
        record, _ = self._complete()
        real = managed_finish._archive_state

        def archive_then_write(repo_root, rec):
            result = real(repo_root, rec)
            (rec.sparring_dir / "plans" / "late.log").write_text("late\n", encoding="utf-8")
            return result

        with mock.patch.object(managed_finish, "_archive_state", side_effect=archive_then_write):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "remove_worktree"))
        self.assertIn("plans/late.log", report["reason"])
        self.assertTrue((record.sparring_dir / "plans" / "late.log").exists())

    def test_deleted_ignored_paths_in_dry_run_and_execution(self):
        record, _ = self._complete()
        self._ignore_build(record)
        finish = self._status(record)["finish"]
        self.assertEqual(finish["deleted_ignored_paths"], ["build"])
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(report["deleted_ignored_paths"], ["build"])

    def test_ignored_ancestor_of_the_project_dir_lists_only_outside_paths(self):
        record, _ = self._complete()
        fake = type("R", (), {"worktree_path": record.worktree_path, "project_dir": "outer/.sparring"})()
        outer = Path(record.worktree_path) / "outer"
        (outer / ".sparring").mkdir(parents=True)
        (outer / ".sparring" / "state.json").write_text("{}", encoding="utf-8")
        (outer / "cache.bin").write_text("x", encoding="utf-8")
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "outer/\n", encoding="utf-8")
        self.assertIn("outer/cache.bin", managed_finish._deleted_ignored(fake))
        self.assertNotIn("outer/.sparring/state.json", managed_finish._deleted_ignored(fake))

    # -- F5: archive failure is not completed ------------------------------------

    def test_unreadable_archive_on_rerun_is_not_reported_completed(self):
        record, _ = self._complete()
        real_append = managed_run.append_event

        def fail_branch_deleted(repo_root, run_key, event, detail=None):
            if event == "branch_deleted":
                raise OSError("simulated crash")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_branch_deleted):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "delete_branch"))
        archived = managed_finish.archive_dir(self.repo, record.run_key) / "plans" / f"{record.run_key}.json"
        archived.write_text("{broken", encoding="utf-8")
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "archive_state"))
        # An unreadable run state -- the run's trusted snapshot -- stops the
        # finish before any step runs (Plan L Stage 4: the run-state
        # integrity refusal; was run_state_unreadable).
        self.assertIn("trusted_config_missing", report["reason"])
        self.assertIn("cannot be read", report["reason"])
        self.assertNotIn("finished", report["completed_steps"])
        self.assertNotIn("finished", self._events(record))

    def test_unreadable_recorded_authority_refuses_before_any_remote_operation(self):
        # The worktree is gone, the archived run state is damaged, and the
        # repository's remote and credential configuration changed since the
        # run started. The finish must not fall back to that live
        # configuration: it refuses before any Git operation, and no planted
        # helper runs. (Plan L Stage 4: --push-target is refused outright
        # with remote_finish_removed; the local finish refuses with the
        # run-state integrity refusal.)
        record, _ = self._complete()
        real_append = managed_run.append_event

        def fail_branch_deleted(repo_root, run_key, event, detail=None):
            if event == "branch_deleted":
                raise OSError("simulated crash")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_branch_deleted):
            self._finish(record)
        self.assertFalse(Path(record.worktree_path).exists())
        archived = managed_finish.archive_dir(self.repo, record.run_key) / "plans" / f"{record.run_key}.json"
        archived.write_text("{broken", encoding="utf-8")

        marker = self.repo.parent / "MARKER"
        helper = self.repo.parent / "helper.sh"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        helper.chmod(0o755)
        evil = self.repo.parent / "evil.git"
        _run_git(self.repo.parent, "init", "-q", "--bare", str(evil))
        _run_git(self.repo, "config", "credential.helper", f"!{helper}")
        _run_git(self.repo, "remote", "set-url", "origin", str(evil))
        main_before = _git(self.remote, "rev-parse", "refs/heads/main")

        real_git = managed_run._git
        remote_calls = []

        def recording(cwd, *args):
            if any(a in ("push", "ls-remote", "fetch") for a in args):
                remote_calls.append(args)
            return real_git(cwd, *args)

        with mock.patch.object(managed_run, "_git", side_effect=recording):
            flagged, flagged_report, _ = self._finish(record, "--push-target")
            code, report, _ = self._finish(record)

        self.assertEqual((flagged, flagged_report["stopped_at"]), (3, "checks"))
        self.assertIn("remote_finish_removed", flagged_report["reason"])
        self.assertEqual((code, report["stopped_at"]), (1, "archive_state"))
        self.assertIn("trusted_config_missing", report["reason"])
        self.assertEqual(remote_calls, [])
        self.assertFalse(marker.exists())
        self.assertEqual(_git(evil, "for-each-ref"), "")
        self.assertEqual(_git(self.remote, "rev-parse", "refs/heads/main"), main_before)
        self.assertNotIn("target_pushed", self._events(record))

    def test_prune_retains_the_archive_of_an_interrupted_finish(self):
        record, _ = self._complete()
        real_append = managed_run.append_event

        def fail_branch_deleted(repo_root, run_key, event, detail=None):
            if event == "branch_deleted":
                raise OSError("simulated crash")
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=fail_branch_deleted):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "delete_branch"))
        self.assertTrue(managed_finish.archive_dir(self.repo, record.run_key).is_dir())
        items = managed_finish.prune_report(self.repo)["items"]
        self.assertEqual([(i["kind"], i["reason"]) for i in items], [("record", "worktree_missing")])

        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (0, None))
        items = managed_finish.prune_report(self.repo)["items"]
        self.assertEqual(
            sorted((i["kind"], i["reason"]) for i in items), [("archive", "archived"), ("record", "finished")]
        )

    # -- F2/F3: the target checkout immediately before the merge -------------------

    def test_target_switched_branch_before_merge_refuses(self):
        record, _ = self._complete()
        main = _git(self.repo, "rev-parse", "main")
        with self._after_checks(lambda: _run_git(self.repo, "checkout", "-q", "feature/x")):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (1, "merge", []))
        self.assertIn("target_moved", report["reason"])
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main)
        self.assertNotIn("merged", self._events(record))

    def test_target_switched_to_another_branch_in_same_checkout_refuses(self):
        record, _ = self._complete()
        main = _git(self.repo, "rev-parse", "main")
        other = self.repo.parent / "main-elsewhere"
        refs_before = _git(self.repo, "for-each-ref")

        def move():
            _run_git(self.repo, "checkout", "-q", "feature/x")
            _run_git(self.repo, "worktree", "add", "-q", str(other), "main")

        with self._after_checks(move):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "merge"))
        self.assertIn("target_moved", report["reason"])
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main)
        self.assertEqual(_git(self.repo, "for-each-ref"), refs_before)

    def test_target_head_moved_before_merge_refuses(self):
        record, _ = self._complete()

        def commit():
            (self.repo / "late.txt").write_text("late\n", encoding="utf-8")
            _run_git(self.repo, "add", "late.txt")
            _run_git(self.repo, "commit", "-q", "-m", "late")

        with self._after_checks(commit):
            code, report, _ = self._finish(record)
        moved = _git(self.repo, "rev-parse", "main")
        self.assertEqual((code, report["stopped_at"]), (1, "merge"))
        self.assertIn("target_moved", report["reason"])
        self.assertEqual(_git(self.repo, "log", "-1", "--format=%s", moved), "late")
        self.assertNotIn("merged", self._events(record))

    def _git_path(self, name):
        return Path(_git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", name))

    def test_user_merge_in_progress_refuses_and_is_left_intact(self):
        record, _ = self._complete()
        _run_git(self.repo, "checkout", "-q", "-b", "side", "main")
        (self.repo / "side.txt").write_text("side\n", encoding="utf-8")
        _run_git(self.repo, "add", "side.txt")
        _run_git(self.repo, "commit", "-q", "-m", "side")
        _run_git(self.repo, "checkout", "-q", "main")
        _run_git(self.repo, "merge", "-q", "--no-ff", "--no-commit", "side")
        merge_head = self._git_path("MERGE_HEAD").read_text(encoding="utf-8")
        failed, _ = self._failed(record)
        self.assertIn("target_operation_in_progress", failed)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("target_operation_in_progress", report["reason"])
        self.assertEqual(self._git_path("MERGE_HEAD").read_text(encoding="utf-8"), merge_head)
        self.assertTrue((self.repo / "side.txt").exists())

    def test_operation_started_after_the_checks_is_never_aborted(self):
        for name, content in (("MERGE_HEAD", None), ("rebase-merge", "dir")):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                record, _ = self._complete()
                path = self._git_path(name)
                main = _git(self.repo, "rev-parse", "main")

                def start():
                    if content == "dir":
                        path.mkdir()
                        (path / "head-name").write_text("refs/heads/main\n", encoding="utf-8")
                    else:
                        path.write_text(main + "\n", encoding="utf-8")

                with self._after_checks(start):
                    code, report, _ = self._finish(record)
                self.assertEqual((code, report["stopped_at"]), (1, "merge"))
                self.assertIn("target_operation_in_progress", report["reason"])
                self.assertTrue(path.exists())
                self.assertEqual(_git(self.repo, "rev-parse", "main"), main)
                self.assertNotIn("merged", self._events(record))

    # -- F6/F7: reporting and the record under the lock ----------------------------

    def test_rerun_after_finished_reports_only_recorded_events(self):
        record, _ = self._complete()
        self.assertEqual(self._finish(record)[0], 0)
        # Plan L Stage 4: the rerun is plain (--push-target is refused
        # before anything is read; see test_push_target_refuses_with_remote_
        # finish_removed).
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            report["completed_steps"], ["merge", "archive_state", "remove_worktree", "delete_branch", "finished"]
        )
        self.assertEqual(report["remaining"], [])
        self.assertEqual(report["remote_branch"]["code"], "local_finish")
        self.assertNotIn("target_pushed", self._events(record))

    def test_record_changed_before_the_lock_is_re_read(self):
        record, _ = self._complete()
        real_lock = managed_finish.worktree_lock
        path = managed_run.record_path(self.repo, record.run_key)

        def edit_then_lock(worktree):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["created_by"] = "person"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return real_lock(worktree)

        main = _git(self.repo, "rev-parse", "main")
        with mock.patch.object(managed_finish, "worktree_lock", side_effect=edit_then_lock):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("unmanaged", report["reason"])
        self.assertEqual(_git(self.repo, "rev-parse", "main"), main)
        self.assertTrue(Path(record.worktree_path).is_dir())


class FinishHardeningFollowUpTests(_FinishTestCase):
    """Incomplete traversal, the kept remote branch on every return, and
    deleted paths reported only after a removal."""

    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _assert_cleaned = FinishExecutionTests._assert_cleaned
    _extra_state = FinishHardeningTests._extra_state
    _after_checks = FinishHardeningTests._after_checks
    _ignore_build = FinishHardeningTests._ignore_build

    def _lock_dir(self, record):
        locked = record.sparring_dir / "stages" / ".archive" / "old-stage"
        self.addCleanup(locked.chmod, 0o755)
        return locked

    def test_unreadable_directory_refuses_cleanup_with_nothing_removed(self):
        record, _ = self._complete()
        self._extra_state(record)
        locked = self._lock_dir(record)
        locked.chmod(0)
        failed, finish = self._failed(record)
        self.assertEqual(failed, {"unarchived_project_state"})
        detail = next(c["detail"] for c in finish["checks"] if c["code"] == "unarchived_project_state")
        self.assertIn("stages/.archive/old-stage/", detail)
        before = self._refs_and_files(record)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (3, "checks"))
        self.assertIn("unarchived_project_state", report["reason"])
        self.assertEqual(self._refs_and_files(record), before)
        locked.chmod(0o755)
        self.assertTrue((locked / "attempt-1" / "state.json").exists())
        self.assertFalse(managed_finish.archive_dir(self.repo, record.run_key).exists())

    def test_directory_becoming_unreadable_after_the_checks_refuses_at_archive(self):
        record, head = self._complete()
        self._extra_state(record)
        locked = self._lock_dir(record)
        with self._after_checks(lambda: locked.chmod(0)):
            code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (1, "archive_state", ["merge"]))
        self.assertIn("unarchived_project_state", report["reason"])
        self.assertIn("stages/.archive/old-stage/", report["reason"])
        self.assertTrue(Path(record.worktree_path).is_dir())
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), head)

    def test_listing_with_a_git_warning_is_incomplete(self):
        record, _ = self._complete()
        real_git = managed_run._git

        def warning(cwd, *args):
            result = real_git(cwd, *args)
            if args[:1] == ("ls-files",):
                return subprocess.CompletedProcess(result.args, 0, result.stdout, "warning: could not open directory 'x/'")
            return result

        with mock.patch.object(managed_run, "_git", side_effect=warning):
            failed, _ = self._failed(record)
            with self.assertRaises(managed_run.ManagedRunError):
                managed_finish._project_state_files(record)
        self.assertEqual(failed, {"unarchived_project_state"})

    def _kept_codes(self, report):
        return [k["code"] for k in report["kept"]]

    def test_kept_is_reported_on_every_return(self):
        # Plan L Stage 4: the fields stay on every return (schema_version 1,
        # nothing removed), but a local finish keeps nothing on a remote:
        # kept is empty and remote_branch says the finish is local (was:
        # keep_remote_branch / remote_delete_unavailable).
        record, _ = self._complete()

        def local(report):
            return self._kept_codes(report), report["remote_branch"]["code"]

        # A refusal at the checks.
        (Path(record.worktree_path) / "stray.txt").write_text("x\n", encoding="utf-8")
        code, report, _ = self._finish(record)
        self.assertEqual((code, local(report)), (3, ([], "local_finish")))
        (Path(record.worktree_path) / "stray.txt").unlink()
        # --merge-only.
        code, report, err = self._finish(record, "--merge-only")
        self.assertEqual(code, 0, err)
        self.assertEqual(local(report), ([], "local_finish"))
        # A failure before local branch deletion.
        _run_git(self.repo, "worktree", "lock", record.worktree_path)
        code, report, _ = self._finish(record)
        self.assertEqual((code, report["stopped_at"]), (1, "remove_worktree"))
        self.assertEqual(local(report), ([], "local_finish"))
        _run_git(self.repo, "worktree", "unlock", record.worktree_path)
        code, report, err = self._finish(record)
        self.assertEqual((code, local(report)), (0, ([], "local_finish")), err)
        # A finished rerun.
        code, report, err = self._finish(record)
        self.assertEqual((code, local(report)), (0, ([], "local_finish")), err)

    def _text(self, record, *flags):
        return self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), *flags)

    def test_deleted_ignored_reported_only_after_removal(self):
        record, _ = self._complete()
        self._ignore_build(record)
        code, out, err = self._text(record, "--merge-only")
        self.assertEqual(code, 0, err)
        self.assertNotIn("deleted ignored", out)
        self.assertNotIn("kept (", out)
        self.assertIn("remote_branch (local_finish)", out)
        code, report, _ = self._finish(record, "--merge-only")
        self.assertEqual(report["deleted_ignored_paths"], [])
        _run_git(self.repo, "worktree", "lock", record.worktree_path)
        code, out, _ = self._text(record)
        self.assertEqual(code, 1)
        self.assertNotIn("deleted ignored", out)
        _run_git(self.repo, "worktree", "unlock", record.worktree_path)
        code, out, err = self._text(record)
        self.assertEqual(code, 0, err)
        self.assertIn("deleted ignored: build", out)
        self.assertFalse(Path(record.worktree_path).exists())
