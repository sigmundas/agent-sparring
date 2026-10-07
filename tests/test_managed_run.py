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


class _ManagedRepoTestCase(_PlanRepoTestCase):
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


class ManagedRunTests(_ManagedRepoTestCase):
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
        _run_git(self.repo, "branch", "sparring/plan-abc-1234abcd")
        code, _, err = self._start_managed("--run-key", key)
        self.assertEqual(code, 1)
        self.assertIn("branch_exists", err)
        self.assertEqual(self._records(), ())
        _run_git(self.repo, "branch", "-d", "sparring/plan-abc-1234abcd")
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
             "target_branch", "base_sha", "created_at", "lifecycle", "run_status", "git", "finish"},
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

    def test_run_key_used_by_a_run_state_in_another_worktree_refuses(self):
        other = self.repo.parent / "other-wt"
        _run_git(self.repo, "worktree", "add", "-q", str(other), "feature/x")
        plans = other / ".sparring" / "plans"
        plans.mkdir(parents=True)
        (plans / "plan-abc-1234abcd.json").write_text("{}", encoding="utf-8")
        before = _git(self.repo, "branch", "--list")
        code, _, err = self._start_managed("--run-key", "plan-abc-1234abcd")
        self.assertEqual(code, 1)
        self.assertIn("run_key_used", err)
        self.assertEqual(self._records(), ())
        self.assertEqual(_git(self.repo, "branch", "--list"), before)

    def test_start_plan_managed_refuses_without_committed_project(self):
        code, out, err = self._main(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--target-branch", "feature/x", "--json",
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "refused")
        self.assertIsNone(payload["confirm_token"])
        self.assertIn("project_not_committed", payload["error"])

    def test_start_plan_managed_binds_the_committed_configuration_not_the_checkouts(self):
        argv = ("start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", "--json")
        code, out, err = self._main(*argv)
        self.assertEqual(code, 0, err)
        clean_token = json.loads(out)["confirm_token"]
        # A dirty, different configuration in the invoking checkout is not
        # what the managed worktree runs with, so it changes nothing bound.
        (self.sparring_dir / "project.toml").write_text(
            'project = "repo"\n\n[agents.stage]\nprovider = "codex-cli"\n', encoding="utf-8"
        )
        code, out, err = self._main(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["confirm_token"], clean_token)
        # Committed on the target, it is what the managed run resolves.
        _run_git(self.repo, "commit", "-q", "-am", "stage on codex")
        code, out, err = self._main(*argv)
        self.assertEqual(code, 1)
        self.assertIn("'codex-cli'", json.loads(out)["error"])

    def test_resume_refuses_a_project_dir_escaping_the_worktree_by_symlink(self):
        import shutil

        with mock.patch("agent_sparring.cli._run_in_record", return_value=1):
            self._start_managed()
        (record,) = self._records()
        outside = self.repo.parent / "outside"
        outside.mkdir()
        project = Path(record.worktree_path) / ".sparring"
        shutil.rmtree(project)
        project.symlink_to(outside)
        adapters = (_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU]))
        with mock.patch("agent_sparring.cli._run_plan_command") as run:
            code, _, err = self._main("resume-plan", "--run-key", record.run_key, adapters=adapters)
        self.assertEqual(code, 1)
        self.assertIn("project_dir_outside_worktree", err)
        run.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def _preview(self):
        code, out, err = self._main(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", "--json"
        )
        return code, json.loads(out), err

    def test_managed_preview_ignores_a_malformed_dirty_checkout_configuration(self):
        code, clean, err = self._preview()
        self.assertEqual(code, 0, err)
        (self.sparring_dir / "project.toml").write_text("this is [not toml\n", encoding="utf-8")
        code, payload, err = self._preview()
        self.assertEqual(code, 0, payload.get("error"))
        self.assertEqual(payload["confirm_token"], clean["confirm_token"])

    def test_managed_preview_ignores_an_uncommitted_project_name_change(self):
        code, clean, err = self._preview()
        self.assertEqual(code, 0, err)
        (self.sparring_dir / "project.toml").write_text('project = "renamed"\n', encoding="utf-8")
        code, payload, err = self._preview()
        self.assertEqual(code, 0, payload.get("error"))
        self.assertEqual(payload["confirm_token"], clean["confirm_token"])
        code, _, err = self._main(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--confirm", clean["confirm_token"],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._records()), 1)
        # Committed, the name is bound.
        _run_git(self.repo, "commit", "-q", "-am", "rename")
        code, payload, err = self._preview()
        self.assertEqual(code, 0, payload.get("error"))
        self.assertNotEqual(payload["confirm_token"], clean["confirm_token"])


class _Crash(BaseException):
    """A simulated process death: not caught by any engine handler."""


class CreationLifecycleTests(_ManagedRepoTestCase):
    KEY = "plan-abc-1234abcd"

    def _crash_on(self, target: str, should_crash, key=KEY):
        real = getattr(managed_run, target)

        def crashing(*args, **kwargs):
            if should_crash(*args):
                raise _Crash(target)
            return real(*args, **kwargs)

        with mock.patch.object(managed_run, target, crashing), self.assertRaises(_Crash):
            self._start_managed("--run-key", key)
        return managed_run.read_record(self.repo, key)

    def _crash_before_created(self, key=KEY):
        """Crash after ``worktree add``, before the ``created`` event."""

        record = self._crash_on("append_event", lambda repo, run_key, event, *rest: event == "created", key)
        self.assertEqual(record.lifecycle, "creating")
        self.assertFalse(record.owns_git_state)
        self.assertTrue(Path(record.worktree_path).is_dir())
        return record

    def _resume_refused(self, key, code_name):
        before = managed_run.read_record(self.repo, key)
        with mock.patch("agent_sparring.cli._run_plan_command") as run:
            code, _, err = self._main("resume-plan", "--run-key", key)
        self.assertEqual(code, 1)
        self.assertIn(code_name, err)
        run.assert_not_called()
        self.assertEqual(managed_run.read_record(self.repo, key), before)
        return err

    def _unmanaged_runs_work(self):
        _run_git(self.repo, "checkout", "-q", "feature/x")
        try:
            code, _, err = self._main(
                "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                "--json",
            )
            self.assertEqual(code, 0, err)
            code, _, err = self._main(
                "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            )
            self.assertEqual(code, 0, err)
        finally:
            _run_git(self.repo, "checkout", "-q", "main")

    def test_crash_after_record_before_worktree_add(self):
        record = self._crash_on("_git", lambda cwd, *args: args[:1] == ("update-ref",))
        self.assertEqual(record.lifecycle, "creating")
        self.assertFalse(managed_run.branch_exists(self.repo, record.branch))
        self.assertFalse(Path(record.worktree_path).exists())
        self._resume_refused(self.KEY, "creation_incomplete")
        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        (run,) = json.loads(out)["runs"]
        self.assertEqual(run["lifecycle"], "creating")
        self.assertFalse(run["finish"]["managed"])
        code, _, err = self._start_managed("--run-key", "plan-abc-0000ffff")
        self.assertEqual(code, 0, err)
        self._unmanaged_runs_work()

    def test_worktree_add_fails_on_a_preexisting_user_branch(self):
        real_git = managed_run._git

        def racing_git(cwd, *args):
            # The user's branch appears between preflight and the creation.
            if args[:2] == ("update-ref", "--create-reflog"):
                real_git(cwd, "branch", args[4][len("refs/heads/"):], "feature/x")
            return real_git(cwd, *args)

        user_tip = _git(self.repo, "rev-parse", "feature/x")
        with mock.patch.object(managed_run, "_git", racing_git):
            code, _, err = self._start_managed("--run-key", self.KEY)
        self.assertEqual(code, 1)
        self.assertIn("worktree_add_failed", err)
        (record,) = self._records()
        self.assertEqual(record.lifecycle, "creation_failed")
        self.assertFalse(record.owns_git_state)
        self.assertEqual(record.events[-1]["detail"]["branch_found"], True)
        self.assertFalse(record.events[-1]["detail"]["attributed"])
        self._resume_refused(self.KEY, "creation_failed")
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), user_tip)
        self.assertFalse(Path(record.worktree_path).exists())
        self._unmanaged_runs_work()

    def test_crash_before_created_event_resume_confirms_once(self):
        record = self._crash_before_created()
        def shape():
            return [(e["path"], e["branch"], e["head"]) for e in managed_run.worktree_list(self.repo)]

        worktrees = shape()
        code, _, err = self._main("resume-plan", "--run-key", self.KEY)
        self.assertEqual(code, 0, err)
        entry = next(e for e in managed_run.worktree_list(self.repo) if e["branch"] == record.branch)
        self.assertIsNone(entry["locked"])
        code, _, err = self._main("resume-plan", "--run-key", self.KEY, "--evidence", "ok")
        self.assertEqual(code, 0, err)
        (after,) = self._records()
        self.assertEqual([e["event"] for e in after.events], ["created"])
        self.assertEqual(shape(), worktrees)
        self.assertEqual(after.worktree_path, record.worktree_path)

    def test_crash_before_created_event_refuses_a_changed_worktree(self):
        def dirty(worktree):
            (worktree / "docs" / "plan.md").write_text("changed\n", encoding="utf-8")

        def untracked(worktree):
            (worktree / "new.txt").write_text("new\n", encoding="utf-8")

        def moved(worktree):
            untracked(worktree)
            _run_git(worktree, "add", "new.txt")
            _run_git(worktree, "commit", "-q", "-m", "moved")

        def reset_back(worktree):
            moved(worktree)
            _run_git(worktree, "reset", "-q", "--hard", "HEAD~1")

        for index, change in enumerate((dirty, untracked, moved, reset_back)):
            with self.subTest(change=change.__name__):
                key = f"plan-x-{index:08x}"
                record = self._crash_before_created(key)
                change(Path(record.worktree_path))
                self._resume_refused(key, "creation_incomplete")
                self.assertTrue(Path(record.worktree_path).is_dir())
                self.assertTrue(managed_run.branch_exists(self.repo, record.branch))

    def _user_worktree_at(self, record, *, branch_args):
        _run_git(self.repo, *branch_args)
        _run_git(self.repo, "worktree", "add", "-q", record.worktree_path, record.branch)

    def test_user_branch_and_worktree_made_after_an_interrupted_creation_are_not_attributed(self):
        record = self._crash_on("_git", lambda cwd, *args: args[:1] == ("update-ref",))
        self.assertFalse(managed_run.branch_exists(self.repo, record.branch))
        # Later, a user makes the same branch at the same base and a clean
        # worktree at the recorded path: same shape, no engine mark.
        self._user_worktree_at(record, branch_args=("branch", record.branch, record.base_sha))
        self._resume_refused(self.KEY, "creation_incomplete")
        self.assertFalse(managed_run.read_record(self.repo, self.KEY).owns_git_state)
        self.assertTrue(Path(record.worktree_path).is_dir())

    def test_engine_branch_with_a_user_worktree_is_not_attributed(self):
        # Crash after the engine's branch, before its worktree; the user adds one.
        record = self._crash_on("_git", lambda cwd, *args: args[:2] == ("worktree", "add"))
        self.assertTrue(managed_run.branch_exists(self.repo, record.branch))
        _run_git(self.repo, "worktree", "add", "-q", record.worktree_path, record.branch)
        err = self._resume_refused(self.KEY, "creation_incomplete")
        self.assertIn("creation lock", err)

    def test_same_base_collision_then_crash_before_creation_failed_refuses(self):
        real_git = managed_run._git
        real_append = managed_run.append_event

        def racing_git(cwd, *args):
            if args[:2] == ("update-ref", "--create-reflog"):
                real_git(cwd, "branch", args[4][len("refs/heads/"):], args[5])
            return real_git(cwd, *args)

        def crash(repo_root, run_key, event, detail=None):
            if event == "creation_failed":
                raise _Crash(event)
            return real_append(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "_git", racing_git), \
                mock.patch.object(managed_run, "append_event", crash), self.assertRaises(_Crash):
            self._start_managed("--run-key", self.KEY)
        record = managed_run.read_record(self.repo, self.KEY)
        self.assertEqual(record.lifecycle, "creating")
        user_tip = _git(self.repo, "rev-parse", record.branch)
        self.assertEqual(user_tip, record.base_sha)
        _run_git(self.repo, "worktree", "add", "-q", record.worktree_path, record.branch)
        self._resume_refused(self.KEY, "creation_incomplete")
        self.assertEqual(_git(self.repo, "rev-parse", record.branch), user_tip)

    def _set_events(self, key, events):
        path = managed_run.record_path(self.repo, key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["events"] = events
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_plan_state_guard_requires_proven_ownership(self):
        code, _, err = self._start_managed("--run-key", self.KEY)
        self.assertEqual(code, 0, err)
        (record,) = self._records()
        worktree = Path(record.worktree_path)
        state_path = record.sparring_dir / "plans" / f"{self.KEY}.json"
        for lifecycle, events in (
            ("creating", []),
            ("creation_failed", [{"at": "x", "event": "creation_failed", "detail": {}}]),
        ):
            with self.subTest(lifecycle=lifecycle):
                self._set_events(self.KEY, events)
                with self.assertRaises(ManagedRunError) as ctx:
                    managed_run.require_state_matches(worktree, self.KEY, expected_branch=record.branch)
                self.assertEqual(ctx.exception.code, "managed_record_unowned")
                state_before = state_path.read_bytes()
                # Resume without --run-key, from inside the worktree.
                err_io = io.StringIO()
                with mock.patch("agent_sparring.cli._build_loop_adapters") as adapters, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_io):
                    code = main([
                        "--sparring-dir", str(record.sparring_dir), "resume-plan",
                        str(worktree / "docs" / "plan.md"), "--repo-root", str(worktree),
                        "--expected-branch", record.branch, "--evidence", "ok",
                    ])
                self.assertEqual(code, 1)
                self.assertIn("managed_record_unowned", err_io.getvalue())
                adapters.assert_not_called()
                self.assertEqual(state_path.read_bytes(), state_before)
                # And through the plan module's own guard.
                from agent_sparring.plan import PlanError, _require_managed_record

                with self.assertRaises(PlanError) as plan_ctx:
                    _require_managed_record(worktree, self.KEY, record.branch)
                self.assertIn("managed_record_unowned", str(plan_ctx.exception))

    def test_crash_during_append_event_leaves_no_temp_and_record_unchanged(self):
        code, _, err = self._start_managed("--run-key", self.KEY)
        self.assertEqual(code, 0, err)
        path = managed_run.record_path(self.repo, self.KEY)
        before = path.read_bytes()
        for patch in (
            mock.patch.object(managed_run.os, "replace", side_effect=_Crash("rename")),
            mock.patch.object(managed_run.json, "dumps", side_effect=_Crash("write")),
        ):
            with self.subTest(patch=patch), patch, self.assertRaises(_Crash):
                managed_run.append_event(self.repo, self.KEY, "merged", {})
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])
            self.assertEqual(path.read_bytes(), before)

    def test_failed_exclusive_create_leaves_no_temp(self):
        code, _, err = self._start_managed("--run-key", self.KEY)
        self.assertEqual(code, 0, err)
        directory = managed_run.records_dir(self.repo)
        with mock.patch.object(managed_run.os, "link", side_effect=_Crash("link")), self.assertRaises(_Crash):
            managed_run.create_record(self.repo, ManagedRunRecord(**{**self._records()[0].__dict__, "run_key": "other-1"}))
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [f"{self.KEY}.json"])

    def test_hidden_and_temp_files_do_not_poison_discovery(self):
        record = self._crash_before_created()
        directory = managed_run.records_dir(self.repo)
        (directory / ".tmp-abc123.json").write_text("{not json", encoding="utf-8")
        (directory / ".hidden.json").write_text("garbage", encoding="utf-8")
        (directory / ".DS_Store").write_text("x", encoding="utf-8")
        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["run_key"] for r in json.loads(out)["runs"]], [self.KEY])
        code, _, err = self._main("resume-plan", "--run-key", self.KEY)
        self.assertEqual(code, 0, err)
        self.assertEqual(self._records()[0].lifecycle, "created")
        self._unmanaged_runs_work()
        self.assertTrue((directory / ".tmp-abc123.json").exists())
        self.assertEqual(Path(record.worktree_path).is_dir(), True)

    def test_run_keys_differing_outside_the_former_suffix_get_distinct_branches(self):
        for key in ("plan-a-1234abcd", "plan-b-1234abcd"):
            code, _, err = self._start_managed("--run-key", key)
            self.assertEqual(code, 0, err)
        branches = {record.branch for record in self._records()}
        self.assertEqual(len(branches), 2)


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
        payload = self._record().to_dict()
        payload["schema_version"] = True
        with self.assertRaises(ManagedRunError) as ctx:
            ManagedRunRecord.from_dict(payload)
        self.assertEqual(ctx.exception.code, "record_schema_unknown")

    def test_plan_schema_record_without_project_dir_reads_with_the_default(self):
        payload = self._record().to_dict()
        del payload["project_dir"]
        self.assertEqual(ManagedRunRecord.from_dict(payload).project_dir, ".sparring")

    def test_relative_input_and_escaping_project_dir_refuse(self):
        for key, value in (
            ("input", {"kind": "markdown", "path": "docs/plan.md"}),
            ("project_dir", "/outside"),
            ("project_dir", "../outside"),
            ("project_dir", "a/../../b"),
            ("project_dir", "."),
        ):
            payload = self._record().to_dict()
            payload[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ManagedRunError) as ctx:
                    ManagedRunRecord.from_dict(payload)
                self.assertEqual(ctx.exception.code, "record_malformed")
        with self.assertRaises(ManagedRunError):
            self._record(project_dir="/outside").sparring_dir

    def test_project_dir_symlinked_outside_the_worktree_refuses(self):
        worktree = Path(self._tmp.name) / "wt"
        worktree.mkdir()
        (worktree / ".sparring").symlink_to(Path(self._tmp.name))
        with self.assertRaises(ManagedRunError) as ctx:
            self._record(worktree_path=str(worktree)).sparring_dir
        self.assertEqual(ctx.exception.code, "project_dir_outside_worktree")

    def test_branch_derives_from_the_whole_run_key(self):
        self.assertNotEqual(
            managed_run.managed_branch("plan-a-1234abcd"), managed_run.managed_branch("plan-b-1234abcd")
        )
        branches = {managed_run.managed_branch(key) for key in ("a.b-1", "a_2eb-1", "A.b-1", "a.B-1", "x.lock", "a..b")}
        self.assertEqual(len(branches), 6)
        for branch in branches:
            self.assertEqual(
                subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"]).returncode, 0, branch
            )
