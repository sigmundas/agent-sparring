"""Plan L Stage 4: ``finish-run`` is local (I-L1, I-L2b).

What these tests hold, one sentence each:

- the default finish merges into the local target branch of every
  participating repository -- the primary and each sibling the run accepted
  a candidate in -- with or without a bare remote, and the git recorder
  first on ``PATH`` shows no remote subcommand while every remote's refs
  stay byte-identical;
- a finish after an accepted commit was rewritten away, in the primary or in
  a sibling, refuses with ``run_history_rewritten`` naming the repository
  and the commit, and nothing is merged; positive control: the same run,
  unrewritten, finishes;
- ``check-config`` reports a config that still sets ``[finish]
  delete_remote_branch``.

Real disposable repositories and bare remotes throughout; no network, no
provider.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import managed_run, trusted_snapshots
from agent_sparring.plan import MarkdownPlanSource, PlanRunState, PlanRunStatus, load_plan_source
from agent_sparring.stage import CandidateRepository, Stage, StageStatus
from agent_sparring.trusted_snapshots import SiblingSnapshot

from git_recorder import GitRecorder
from test_managed_finish import FinishExecutionTests, _FinishTestCase
from test_managed_run import _git
from test_multi_repo_candidates import _make_repo
from test_plan import _run_git

SIBLING = "sib"
SIBLING_BRANCH = "feature/cloud"


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _run_git(repo, "add", name)
    _run_git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _rewrite(repo: Path, name: str) -> str:
    """Drop HEAD from the branch and commit something else in its place: the
    commit that was HEAD is no longer an ancestor of the new one."""

    _run_git(repo, "reset", "-q", "--hard", "HEAD~1")
    return _commit(repo, name)


class _LocalFinishCase(_FinishTestCase):
    _finish = FinishExecutionTests._finish
    _events = FinishExecutionTests._events
    _assert_cleaned = FinishExecutionTests._assert_cleaned

    def _sibling(self) -> Path:
        sibling = _make_repo(self.repo.parent, SIBLING, SIBLING_BRANCH)
        self.sibling_remote = self.repo.parent / f"{SIBLING}.git"
        return sibling

    def _run(self, *, sibling: Path | None = None, rewrite_primary: bool = False,
             rewrite_sibling: bool = False):
        """A complete managed run of the two-stage plan: stage 1 accepted
        one commit and stage 2 the next, in the primary and (when given) in
        ``sibling``. ``rewrite_*`` rewrites stage 1's commit away before
        stage 2's is made, as a person rewriting history between stages
        would."""

        self._start_managed()
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        first = _commit(worktree, "one.txt")
        second = _rewrite(worktree, "two.txt") if rewrite_primary else _commit(worktree, "two.txt")
        sibling_shas: tuple[str, str] | None = None
        if sibling is not None:
            sib_first = _commit(sibling, "cloud-one.txt")
            sib_second = _rewrite(sibling, "cloud-two.txt") if rewrite_sibling else _commit(sibling, "cloud-two.txt")
            sibling_shas = (sib_first, sib_second)
        source = load_plan_source(Path(record.input_path), worktree)
        assert isinstance(source, MarkdownPlanSource)
        for index, planned in enumerate(source.in_namespace(record.run_key).stages()):
            stage = Stage.resolve(record.sparring_dir, planned.stage_id)
            if not stage.exists():
                stage.create(run=record.run_key)
            state = stage.read_state()
            state.status = StageStatus.ACCEPTED
            state.candidate_sha = (first, second)[index]
            state.run = record.run_key
            if sibling is not None:
                state.repositories = (CandidateRepository(
                    name=SIBLING, path=str(sibling), branch=SIBLING_BRANCH,
                    candidate_sha=sibling_shas[index],
                ),)
            stage.write_state(state)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        run_state = PlanRunState.load(state_path)
        run_state.status = PlanRunStatus.COMPLETE
        run_state.awaiting = None
        run_state.evidence_pending = None
        if sibling is not None:
            run_state.sibling_snapshots = (SiblingSnapshot(
                path=str(sibling.resolve()), entries=trusted_snapshots.capture(sibling),
            ),)
        run_state.save(state_path)
        return record, second, sibling_shas

    def _remotes(self) -> tuple[str, ...]:
        found = [_git(self.remote, "for-each-ref")] if self.remote.exists() else []
        sibling_remote = getattr(self, "sibling_remote", None)
        if sibling_remote is not None:
            found.append(_git(sibling_remote, "for-each-ref"))
        return tuple(found)


class LocalFinishTests(_LocalFinishCase):
    def test_with_a_bare_remote_the_target_is_merged_locally_and_the_remote_is_untouched(self):
        record, head, _ = self._run()
        remotes_before = self._remotes()
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(report["remote_branch"]["code"], "local_finish")
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(self._remotes(), remotes_before)

    def test_without_any_remote_the_target_is_merged_locally(self):
        _run_git(self.repo, "remote", "remove", "origin")
        record, head, _ = self._run()
        self.assertIsNone(record.remote)
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        self.assertEqual(recorder.remote_commands(), [])

    def test_with_a_sibling_each_target_is_merged_locally(self):
        sibling = self._sibling()
        record, head, (_first, sib_head) = self._run(sibling=sibling)
        sibling_main = _git(sibling, "rev-parse", "main")
        remotes_before = self._remotes()

        status = self._status(record)
        (entry,) = status["git"]["siblings"]
        self.assertEqual((entry["name"], entry["candidate"], entry["merge_mode"]), (SIBLING, sib_head, "fast_forward"))
        self.assertTrue(status["finish"]["eligible"]["cleanup"], status["finish"]["checks"])
        history = [c for c in status["finish"]["checks"] if c["code"] == "run_history_rewritten"]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(c["ok"] for c in history))
        self.assertIn(f"fast-forward main of sibling repository {SIBLING!r} to {sib_head}", status["finish"]["actions"])

        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self._assert_cleaned(record, head)
        # The sibling's local target holds its accepted candidate; nothing of
        # the sibling's own is deleted, and it stays on its branch.
        self.assertNotEqual(sibling_main, sib_head)
        self.assertEqual(_git(sibling, "rev-parse", "refs/heads/main"), sib_head)
        self.assertEqual(_git(sibling, "rev-parse", f"refs/heads/{SIBLING_BRANCH}"), sib_head)
        self.assertEqual(_git(sibling, "symbolic-ref", "--short", "HEAD"), SIBLING_BRANCH)
        events = managed_run.read_record(self.repo, record.run_key).events
        (merged,) = [e["detail"] for e in events if e["event"] == "sibling_merged"]
        self.assertEqual((merged["name"], merged["candidate"], merged["mode"]), (SIBLING, sib_head, "fast_forward"))
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(self._remotes(), remotes_before)

    def test_a_sibling_whose_target_is_checked_out_is_merged_in_its_checkout(self):
        sibling = self._sibling()
        record, head, (_first, sib_head) = self._run(sibling=sibling)
        checkout = self.repo.parent / "sib-main"
        _run_git(sibling, "worktree", "add", "-q", str(checkout), "main")
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, err)
        self.assertEqual(_git(checkout, "rev-parse", "HEAD"), sib_head)
        self.assertEqual(_git(checkout, "status", "--porcelain"), "")
        self._assert_cleaned(record, head)


class FinishHistoryTests(_LocalFinishCase):
    """I-L2b at finish, per repository."""

    def _refused(self, *, rewrite_primary: bool = False, rewrite_sibling: bool = False) -> None:
        sibling = self._sibling()
        record, _, _ = self._run(sibling=sibling, rewrite_primary=rewrite_primary, rewrite_sibling=rewrite_sibling)
        before = (self._refs_and_files(record), _git(sibling, "for-each-ref"), self._remotes())
        failed, finish = self._failed(record)
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            code, report, err = self._finish(record)
        self.assertEqual(failed, {"run_history_rewritten"})
        (check,) = [c for c in finish["checks"] if c["code"] == "run_history_rewritten" and not c["ok"]]
        where = "the primary repository" if rewrite_primary else f"sibling repository {SIBLING!r}"
        self.assertIn(where, check["detail"])
        self.assertEqual((code, report["stopped_at"], report["completed_steps"]), (3, "checks", []), err)
        self.assertIn("run_history_rewritten", report["reason"])
        # Nothing is merged anywhere.
        self.assertEqual((self._refs_and_files(record), _git(sibling, "for-each-ref"), self._remotes()), before)
        self.assertNotIn("merged", self._events(record))
        self.assertNotIn("sibling_merged", self._events(record))
        self.assertEqual(recorder.remote_commands(), [])

    def test_a_rewritten_primary_history_refuses_and_nothing_is_merged(self):
        self._refused(rewrite_primary=True)

    def test_a_rewritten_sibling_history_refuses_and_nothing_is_merged(self):
        self._refused(rewrite_sibling=True)

    def test_positive_control_the_unrewritten_run_finishes(self):
        sibling = self._sibling()
        record, head, (_first, sib_head) = self._run(sibling=sibling)
        failed, _ = self._failed(record)
        self.assertEqual(failed, set())
        code, report, err = self._finish(record)
        self.assertEqual(code, 0, (err, report))
        self._assert_cleaned(record, head)
        self.assertEqual(_git(sibling, "rev-parse", "refs/heads/main"), sib_head)


class CheckConfigTests(_FinishTestCase):
    def setUp(self):
        super().setUp()
        # check-config also checks the intake ignore line (unrelated here).
        gitignore = self.repo / ".gitignore"
        gitignore.write_text(gitignore.read_text(encoding="utf-8") + ".sparring/intake/\n", encoding="utf-8")

    def _check(self, finish_table: str) -> tuple[int, str]:
        toml = self.sparring_dir / "project.toml"
        toml.write_text('project = "repo"\n' + finish_table, encoding="utf-8")
        code, _out, err = self._main("check-config")
        return code, err

    def test_check_config_reports_a_config_that_still_sets_delete_remote_branch(self):
        code, err = self._check("\n[finish]\ndelete_remote_branch = true\n")
        self.assertEqual(code, 1)
        self.assertIn("remote_finish_removed", err)
        self.assertIn("delete_remote_branch", err)
        code, err = self._check("\n[finish]\ndelete_remote_branch = false\n")
        self.assertEqual(code, 0, err)
        self.assertIn("delete_remote_branch = false has no effect", err)
        # Positive control: without the setting nothing is reported.
        code, err = self._check("")
        self.assertEqual(code, 0, err)
        self.assertNotIn("delete_remote_branch", err)


del FinishExecutionTests  # imported for its helpers; its tests run in their own module
