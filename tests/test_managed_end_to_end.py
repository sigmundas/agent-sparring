import json
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import managed_finish, managed_run
from agent_sparring.providers import StageAgentResult

from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import READY, _run_git, _SparringAdapter


class _ManagedStageAdapter:
    """Commits and pushes one file per turn in the run's managed worktree --
    found through the engine's record, as the engine itself does."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._calls = 0

    def _turn(self, session_id: str) -> StageAgentResult:
        self._calls += 1
        (record,) = managed_run.list_records(self.repo)
        worktree = Path(record.worktree_path)
        (worktree / f"impl-{self._calls}.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(worktree, "add", f"impl-{self._calls}.txt")
        _run_git(worktree, "commit", "-q", "-m", f"turn {self._calls}")
        _run_git(worktree, "push", "-q", "origin", record.branch)
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt: str) -> StageAgentResult:
        return self._turn(f"impl-{self._calls + 1}")

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        return self._turn(session_id)


class ManagedRunEndToEndTests(_ManagedRepoTestCase):
    def test_managed_run_to_finished(self):
        before = self._snapshot()
        main_before = before[0]
        untracked = self.repo / "mine.txt"
        untracked.write_text("mine\n", encoding="utf-8")
        adapters = (_ManagedStageAdapter(self.repo), _SparringAdapter([READY] * 4))

        code, _, err = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", adapters=adapters
        )
        self.assertEqual(code, 0, err)
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        candidate = _git(worktree, "rev-parse", "HEAD")
        self.assertNotEqual(candidate, main_before)

        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--dry-run", "--json"
        )
        self.assertEqual(code, 0, out + err)
        finish = json.loads(out)
        self.assertEqual(finish["eligible"], {"merge": True, "cleanup": True})
        self.assertEqual(finish["merge_mode"], "fast_forward")

        code, out, err = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, out + err)
        self.assertIsNone(json.loads(out)["stopped_at"])

        # The target contains the candidate; the worktree and branch are gone.
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), candidate)
        self.assertFalse(worktree.exists())
        self.assertEqual(_git(self.repo, "branch", "--list", record.branch), "")
        self.assertNotIn(record.worktree_path, [e["path"] for e in managed_run.worktree_list(self.repo)])
        self.assertEqual(managed_run.read_record(self.repo, record.run_key).lifecycle, "finished")

        # The primary checkout is unchanged except the fast-forwarded target:
        # same branch, now at the candidate, clean but for the user's own file.
        head, branch, status, _ = self._snapshot()
        self.assertEqual((head, branch), (candidate, before[1]))
        self.assertEqual(status, "?? mine.txt")
        self.assertEqual(untracked.read_text(encoding="utf-8"), "mine\n")

        # The finished run is reported as prunable, never deleted.
        report = managed_finish.prune_report(self.repo)
        reasons = {(item["kind"], item["reason"]) for item in report["items"]}
        self.assertEqual(reasons, {("record", "finished"), ("archive", "archived")})


class PruneReportTests(_ManagedRepoTestCase):
    def test_reports_missing_worktree_and_never_unrecorded_worktrees(self):
        self._start_managed()
        (record,) = self._records()
        snapshot = self.repo.parent / "agent-sparring-engine-0123abcd"
        _run_git(self.repo, "worktree", "add", "-q", "--detach", str(snapshot))
        self.assertEqual(managed_finish.prune_report(self.repo)["items"], [])

        _run_git(self.repo, "worktree", "remove", record.worktree_path)
        code, out, err = self._main("prune", "--dry-run", "--json", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(
            [(i["kind"], i["run_key"], i["reason"]) for i in report["items"]],
            [("record", record.run_key, "worktree_missing")],
        )
        self.assertEqual(report["engine_snapshots"], [])
        self.assertNotIn("agent-sparring-engine", out)
        self.assertIsNotNone(managed_run.read_record(self.repo, record.run_key))

    def test_dry_run_is_required(self):
        with self.assertRaises(SystemExit):
            self._main("prune", "--json", "--repo-root", str(self.repo))
