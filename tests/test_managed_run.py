import contextlib
import io
import json
import subprocess
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import managed_run
from agent_sparring.cli import main
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord
from agent_sparring.plan import PlanRunState

from test_plan import NEEDS_YOU, PLAN, _PlanRepoTestCase, _run_git, _SparringAdapter, _StageAdapter


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class ManagedRunTests(_PlanRepoTestCase):
    def setUp(self):
        super().setUp()
        _run_git(self.repo, "checkout", "-q", "main")
        self.sparring_dir.mkdir()
        (self.sparring_dir / "project.toml").write_text('project = "repo"\n', encoding="utf-8")
        _run_git(self.repo, "add", ".sparring/project.toml")
        _run_git(self.repo, "commit", "-q", "-m", "project")

    def _main(self, *argv: str, adapters=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        adapters = adapters or (_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU] * 5))
        with mock.patch("agent_sparring.cli._build_loop_adapters", return_value=adapters), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def _snapshot(self):
        return (
            _git(self.repo, "rev-parse", "HEAD"),
            _git(self.repo, "symbolic-ref", "HEAD"),
            _git(self.repo, "status", "--porcelain"),
            _git(self.repo, "ls-files", "-s"),
        )

    def _records(self):
        return managed_run.list_records(self.repo)

    def _start_managed(self, *extra: str):
        return self._main("run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", *extra)

    def test_creation_makes_one_branch_worktree_and_record_leaving_checkout_untouched(self):
        (self.repo / "dirty.txt").write_text("mine\n", encoding="utf-8")
        (self.repo / "docs" / "plan.md").write_text(PLAN, encoding="utf-8")
        before = self._snapshot()
        branches_before = _git(self.repo, "branch", "--list").splitlines()

        code, out, err = self._start_managed()

        self.assertEqual(code, 0, err)
        self.assertIn("not part of this run: dirty.txt", err)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual((self.repo / "dirty.txt").read_text(encoding="utf-8"), "mine\n")
        (record,) = self._records()
        self.assertEqual(record.lifecycle, "created")
        self.assertEqual(record.target_branch, "main")
        self.assertEqual(record.base_sha, before[0])
        self.assertTrue(record.branch.startswith("sparring/plan-"))
        self.assertEqual(Path(record.worktree_path).name, f"repo-sparring-{record.run_key}")
        self.assertEqual(record.input_path, str(Path(record.worktree_path) / "docs" / "plan.md"))
        branches_after = _git(self.repo, "branch", "--list").splitlines()
        self.assertEqual(len(branches_after), len(branches_before) + 1)
        managed_worktrees = [e for e in managed_run.worktree_list(self.repo) if e["branch"] == record.branch]
        self.assertEqual(len(managed_worktrees), 1)
        state = PlanRunState.load(record.sparring_dir / "plans" / f"{record.run_key}.json")
        self.assertTrue(state.managed)
        self.assertEqual(state.expected_branch, record.branch)
        self.assertFalse((self.sparring_dir / "plans").exists())

    def test_resume_from_primary_checkout_reuses_the_worktree(self):
        self._start_managed()
        (record,) = self._records()
        code, out, err = self._main("resume-plan", "--run-key", record.run_key, "--evidence", "ok")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._records()), 1)
        self.assertIn("recorded human evidence", err)
        self.assertEqual(
            len([e for e in managed_run.worktree_list(self.repo) if e["branch"] == record.branch]), 1
        )

    def test_restart_after_interrupted_first_start_reuses_the_worktree(self):
        with mock.patch("agent_sparring.cli._run_in_record", return_value=1):
            self._start_managed()
        (record,) = self._records()
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        self.assertFalse(state_path.exists())
        worktrees_before = managed_run.worktree_list(self.repo)

        code, out, err = self._main("resume-plan", "--run-key", record.run_key)

        self.assertEqual(code, 0, err)
        self.assertTrue(state_path.is_file())
        self.assertEqual(managed_run.worktree_list(self.repo), worktrees_before)
        self.assertEqual(len(self._records()), 1)

    def test_second_creation_with_the_same_run_key_refuses_and_creates_nothing(self):
        self._start_managed("--run-key", "plan-abc-1234abcd")
        before = (managed_run.worktree_list(self.repo), _git(self.repo, "branch", "--list"))
        code, _, err = self._start_managed("--run-key", "plan-abc-1234abcd")
        self.assertEqual(code, 1)
        self.assertIn("record_exists", err)
        self.assertEqual((managed_run.worktree_list(self.repo), _git(self.repo, "branch", "--list")), before)

    def test_colliding_branch_or_path_refuses_and_creates_nothing(self):
        key = "plan-abc-1234abcd"
        _run_git(self.repo, "branch", "sparring/plan-1234abcd")
        code, _, err = self._start_managed("--run-key", key)
        self.assertEqual(code, 1)
        self.assertIn("branch_exists", err)
        self.assertEqual(self._records(), ())
        _run_git(self.repo, "branch", "-d", "sparring/plan-1234abcd")
        path = self.repo.parent / f"repo-sparring-{key}"
        path.mkdir()
        code, _, err = self._start_managed("--run-key", key)
        self.assertEqual(code, 1)
        self.assertIn("path_exists", err)
        self.assertEqual(self._records(), ())
        self.assertEqual(list(path.iterdir()), [])

    def test_expected_branch_with_managed_refuses(self):
        code, _, err = self._start_managed("--expected-branch", "feature/x")
        self.assertEqual(code, 1)
        self.assertIn("expected_branch_with_managed", err)
        self.assertEqual(self._records(), ())

    def test_project_not_committed_at_target_refuses(self):
        code, _, err = self._start_managed("--target-branch", "feature/x")
        self.assertEqual(code, 1)
        self.assertIn("project_not_committed", err)
        self.assertEqual(self._records(), ())

    def test_resume_with_disagreeing_branch_or_input_refuses(self):
        self._start_managed()
        (record,) = self._records()
        code, _, err = self._main("resume-plan", "--run-key", record.run_key, "--expected-branch", "main")
        self.assertEqual(code, 1)
        self.assertIn("branch_mismatch", err)
        other = self.repo / "docs" / "other.md"
        other.write_text(PLAN, encoding="utf-8")
        code, _, err = self._main("resume-plan", str(other), "--run-key", record.run_key)
        self.assertEqual(code, 1)
        self.assertIn("input_mismatch", err)
        code, _, err = self._main("resume-plan", "--manifest", str(other), "--run-key", record.run_key)
        self.assertEqual(code, 1)
        self.assertIn("input_kind_mismatch", err)
        # The same plan named from the primary checkout agrees with the record.
        code, _, err = self._main(
            "resume-plan", str(self.plan_path), "--run-key", record.run_key, "--evidence", "ok"
        )
        self.assertEqual(code, 0, err)

    def test_unmanaged_run_inside_a_managed_worktree_refuses(self):
        self._start_managed()
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        code, _, err = self._main(
            "run-plan", str(worktree / "docs" / "plan.md"), "--repo-root", str(worktree),
            "--expected-branch", record.branch,
        )
        self.assertEqual(code, 1)
        self.assertIn("managed_worktree", err)

    def test_unmanaged_run_is_unchanged(self):
        _run_git(self.repo, "checkout", "-q", "feature/x")
        code, _, err = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--expected-branch", "feature/x",
        )
        self.assertEqual(code, 0, err)
        (state_file,) = (self.sparring_dir / "plans").glob("*.json")
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertNotIn("managed", payload)
        self.assertEqual(self._records(), ())

    def _manifest(self, *, repositories=None) -> Path:
        stage = {"stage_id": "m-stage-1", "label": "1", "title": "One", "brief": "Do one.", "mode": "implementation"}
        if repositories is not None:
            stage["repositories"] = repositories
        path = self.repo / "docs" / "run.json"
        path.write_text(
            json.dumps({"version": 1, "plan_label": "docs/run.json", "source_digest": "0" * 64, "stages": [stage]}), encoding="utf-8"
        )
        _run_git(self.repo, "add", "docs/run.json")
        _run_git(self.repo, "commit", "-q", "-m", "manifest")
        return path

    def test_manifest_backed_managed_run_resumes_as_manifest_run(self):
        manifest = self._manifest()
        code, _, err = self._main("run-plan", "--manifest", str(manifest), "--repo-root", str(self.repo), "--managed")
        self.assertEqual(code, 0, err)
        (record,) = self._records()
        self.assertEqual(record.input_kind, "manifest")
        code, _, err = self._main("resume-plan", "--run-key", record.run_key, "--evidence", "ok")
        self.assertEqual(code, 0, err)
        state = PlanRunState.load(record.sparring_dir / "plans" / f"{record.run_key}.json")
        self.assertEqual(state.source, "manifest")

    def test_managed_with_sibling_repositories_refuses(self):
        manifest = self._manifest(
            repositories=[{"name": "other", "path": "../other", "branch": "main", "candidate_sha": None}]
        )
        code, _, err = self._main("run-plan", "--manifest", str(manifest), "--repo-root", str(self.repo), "--managed")
        self.assertEqual(code, 1)
        self.assertIn("sibling_repositories", err)
        self.assertEqual(self._records(), ())

    def test_runs_json_shape(self):
        self._start_managed()
        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        (run,) = payload["runs"]
        self.assertEqual(
            set(run),
            {"run_key", "plan_label", "managed", "worktree_path", "worktree_exists", "branch",
             "target_branch", "base_sha", "created_at", "lifecycle", "run_status"},
        )
        self.assertTrue(run["managed"])
        self.assertTrue(run["worktree_exists"])
        self.assertEqual(run["run_status"], "paused")
        self.assertEqual(run["lifecycle"], "created")

    def test_start_plan_managed_direct_route_confirms_and_runs(self):
        (self.repo / "dirty.txt").write_text("mine\n", encoding="utf-8")
        code, out, err = self._main("start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["managed"]["target_branch"], "main")
        self.assertIn("dirty.txt", payload["managed"]["not_part_of_this_run"])
        self.assertEqual(self._records(), ())
        code, _, err = self._main(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--confirm", payload["confirm_token"],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._records()), 1)


class RecordTests(_PlanRepoTestCase):
    def _record(self, **overrides) -> ManagedRunRecord:
        fields = dict(
            run_key="k-12345678", plan_label="docs/plan.md", input_kind="markdown",
            input_path="/x/docs/plan.md", worktree_path="/x-sparring-k", branch="sparring/plan-12345678",
            target_branch="main", base_sha="a" * 40, remote=None, created_at="2026-01-01T00:00:00Z",
            project_dir=".sparring",
        )
        fields.update(overrides)
        return ManagedRunRecord(**fields)

    def test_round_trip_exclusive_create_and_events(self):
        record = self._record()
        managed_run.create_record(self.repo, record)
        with self.assertRaises(ManagedRunError) as ctx:
            managed_run.create_record(self.repo, record)
        self.assertEqual(ctx.exception.code, "record_exists")
        self.assertEqual(managed_run.read_record(self.repo, "k-12345678"), record)
        self.assertEqual(record.lifecycle, "creating")
        updated = managed_run.append_event(self.repo, "k-12345678", "created")
        self.assertEqual(updated.lifecycle, "created")
        self.assertEqual(managed_run.read_record(self.repo, "k-12345678"), updated)

    def test_unknown_schema_version_refuses(self):
        payload = self._record().to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(ManagedRunError) as ctx:
            ManagedRunRecord.from_dict(payload)
        self.assertEqual(ctx.exception.code, "record_schema_unknown")
        payload = self._record().to_dict()
        payload["extra"] = 1
        with self.assertRaises(ManagedRunError):
            ManagedRunRecord.from_dict(payload)
