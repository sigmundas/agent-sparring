"""One plan across two repositories: managed start of the home part, the
authorized continuation into the target repository, and its refusals."""

import contextlib
import io
import json
import subprocess
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import logical_plan, managed_run
from agent_sparring.cli import main
from agent_sparring.logical_plan import LogicalPlanError, RepositoryBinding
from agent_sparring.providers import StageAgentResult

from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import NEEDS_YOU, READY, _run_git, _SparringAdapter
from test_plan_ownership import plan

K = "plan-cross-0001"
K2 = f"{K}-s2"
CROSS = plan("Build here.", "Repository: web\n\nBuild there.")
BELONGS = "Stage 2 belongs to web. Finish this part, then continue the plan."


class _WorktreeStageAdapter:
    """Commits and pushes one file per turn in the worktree it runs in."""

    def __init__(self, worktree: Path):
        self.worktree = Path(worktree)
        self.calls = 0

    def _turn(self, session_id: str) -> StageAgentResult:
        self.calls += 1
        name = f"impl-{self.worktree.name}-{self.calls}.txt"
        (self.worktree / name).write_text("implementation\n", encoding="utf-8")
        _run_git(self.worktree, "add", name)
        _run_git(self.worktree, "commit", "-q", "-m", f"turn {self.calls}")
        branch = _git(self.worktree, "symbolic-ref", "--short", "HEAD")
        _run_git(self.worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt: str) -> StageAgentResult:
        return self._turn(f"impl-{self.calls + 1}")

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        return self._turn(session_id)


def _make_repo(root: Path, name: str, project: str) -> Path:
    remote = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    repo = root / name
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    _run_git(repo, "remote", "add", "origin", str(remote))
    (repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
    (repo / ".sparring").mkdir()
    (repo / ".sparring" / "project.toml").write_text(f'project = "{project}"\n', encoding="utf-8")
    (repo / "tracked.txt").write_text("web\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "base")
    _run_git(repo, "push", "-q", "-u", "origin", "main")
    return repo


class _CrossCase(_ManagedRepoTestCase):
    verdicts = [READY] * 6

    def setUp(self):
        super().setUp()
        self.plan_path.write_text(CROSS, encoding="utf-8")
        _run_git(self.repo, "commit", "-qam", "cross plan")
        self.web = _make_repo(self.repo.parent, "web", "web")
        self.sparring = _SparringAdapter(list(self.verdicts))
        self.stage_adapters: dict[str, _WorktreeStageAdapter] = {}

    def _adapters(self, args, sparring_dir, repo_root, **_):
        key = str(Path(repo_root).resolve())
        stage = self.stage_adapters.setdefault(key, _WorktreeStageAdapter(Path(repo_root)))
        return stage, self.sparring

    def run_cli(self, *argv: str, sparring_dir: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("agent_sparring.cli._build_loop_adapters", side_effect=self._adapters), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(sparring_dir or self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def from_web(self, *argv: str):
        return self.run_cli(*argv, "--repo-root", str(self.web), sparring_dir=self.web / ".sparring")

    def start(self, *extra: str):
        return self.run_cli(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--run-key", K, "--repository", f"web={self.web}", *extra,
        )

    def home_common(self) -> Path:
        return managed_run.git_common_dir(self.repo)

    def logical(self):
        return logical_plan.load(self.home_common(), K)

    def finish_first(self):
        code, out, err = self.run_cli("finish-run", "--run-key", K, "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, out + err)

    def status_json(self, *extra: str, where: str = "home") -> dict:
        if where == "home":
            code, out, err = self.run_cli("resume-plan", "--run-key", K, "--json", "--repo-root", str(self.repo), *extra)
        else:
            code, out, err = self.from_web("resume-plan", "--run-key", K, "--json", *extra)
        payload = json.loads(out)
        payload["_code"] = code
        payload["_err"] = err
        return payload

    def web_records(self):
        return managed_run.list_records(self.web)

    def slice_events(self):
        return [e for e in self.logical().events if e["event"] == "slice_created"]


class StartTests(_CrossCase):
    def test_start_creates_the_logical_record_and_stops_at_the_end_of_the_home_part(self):
        code, out, err = self.start()

        self.assertEqual(code, 0, err)
        self.assertIn(BELONGS, out)
        self.assertIn(f"resume-plan --run-key {K}", out)
        record = self.logical()
        self.assertEqual([s.owner for s in record.stages], ["repo", "web"])
        self.assertEqual(record.binding("web").path, str(self.web.resolve()))
        self.assertEqual(record.binding("web").project, "web")
        self.assertEqual([(e["detail"]["index"], e["detail"]["run_key"]) for e in self.slice_events()], [(1, K)])
        snapshot = logical_plan.snapshot_path(self.home_common(), record)
        self.assertEqual(snapshot.read_bytes(), self.plan_path.read_bytes())
        (execution,) = managed_run.list_records(self.repo)
        self.assertEqual(execution.run_key, K)
        self.assertEqual(execution.logical_slice["index"], 1)
        status = logical_plan.derived_status(record)
        self.assertEqual([s.complete for s in status.slices], [True, False])
        self.assertEqual(self.web_records(), ())

    def test_start_plan_status_lists_the_later_part_and_binds_the_repositories(self):
        code, out, err = self.run_cli(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--repository", f"web={self.web}", "--json",
        )
        self.assertEqual(code, 0, out + err)
        status = json.loads(out)
        self.assertEqual(status["status"], "ready")
        self.assertEqual([s["label"] for s in status["slice"]["stages"]], ["Stage 1"])
        self.assertEqual(status["later_slices"], [{"run_id": None, "primary_repository": "web", "stages": ["2"]}])
        self.assertEqual([r["name"] for r in status["repositories"]], ["repo", "web"])

        code, out, err = self.run_cli(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--repository", f"web={self.web}", "--confirm", status["confirm_token"],
        )
        self.assertEqual(code, 0, err)
        self.assertIn(BELONGS, out)


class WrongRepositoryTests(_CrossCase):
    def assert_refused(self, code_name: str, *repositories: str):
        argv = ["run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", "--run-key", K]
        for value in repositories:
            argv += ["--repository", value]
        code, _, err = self.run_cli(*argv)
        self.assertEqual(code, 1)
        self.assertIn(f"[{code_name}]", err)
        self.assertEqual(managed_run.list_records(self.repo), ())
        self.assertFalse(logical_plan.records_dir(self.home_common()).exists())

    def test_unknown_name(self):
        self.assert_refused("repository_unknown", f"web={self.web}", f"other={self.web}")
        self.assert_refused("repository_unknown", f"webx={self.web}")

    def test_not_a_repository(self):
        plain = self.repo.parent / "plain"
        plain.mkdir()
        self.assert_refused("repository_mismatch", f"web={plain}")
        self.assert_refused("repository_mismatch", f"web={self.repo.parent / 'missing'}")

    def test_project_mismatch(self):
        (self.web / ".sparring" / "project.toml").write_text('project = "webx"\n', encoding="utf-8")
        _run_git(self.web, "commit", "-qam", "rename")
        self.assert_refused("repository_mismatch", f"web={self.web}")

    def test_a_home_name_must_be_the_home(self):
        self.assert_refused("repository_mismatch", f"web={self.web}", f"repo={self.web}")

    def test_two_names_one_common_dir(self):
        self.plan_path.write_text(
            plan("Build here.", "Repository: web\n\nBuild there.", "Repository: docs\n\nDocument."),
            encoding="utf-8",
        )
        _run_git(self.repo, "commit", "-qam", "three repositories")
        docs = self.repo.parent / "web-docs"
        _run_git(self.web, "worktree", "add", "-q", "-b", "docs", str(docs))
        (docs / ".sparring" / "project.toml").write_text('project = "docs"\n', encoding="utf-8")
        _run_git(docs, "commit", "-qam", "docs project")
        self.assert_refused("repository_ambiguous", f"web={self.web}", f"docs={docs}")

    def test_resolve_bindings_refuses_ambiguity_directly(self):
        home = RepositoryBinding("repo", str(self.repo), str(self.home_common()), "repo")
        with mock.patch.object(
            logical_plan, "bind_repository",
            return_value=(RepositoryBinding("web", str(self.web), str(self.home_common()), "web"), "main", "0" * 40),
        ):
            with self.assertRaises(LogicalPlanError) as raised:
                logical_plan.resolve_bindings(home, {"web": self.web}, {"repo", "web"})
        self.assertEqual(raised.exception.code, "repository_ambiguous")


class ContinuationTests(_CrossCase):
    def setUp(self):
        super().setUp()
        code, _, err = self.start()
        self.assertEqual(code, 0, err)

    def test_prepare_is_refused_until_the_previous_part_is_integrated(self):
        status = self.status_json()
        self.assertEqual((status["_code"], status["status"], status["code"]), (1, "refused", "previous_not_integrated"))
        self.assertIsNone(status["confirm_token"])
        code, _, err = self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("previous_not_integrated", err)
        self.assertEqual(self.web_records(), ())

    def test_the_whole_plan_across_both_repositories(self):
        self.finish_first()
        web_main = _git(self.web, "rev-parse", "main")
        status = self.status_json()
        self.assertEqual(status["status"], "ready", status)
        nxt = status["next"]
        self.assertEqual((nxt["repository"], nxt["target_branch"], nxt["base_sha"]), ("web", "main", web_main))
        self.assertEqual(nxt["run_key"], K2)
        self.assertEqual([s["label"] for s in nxt["stages"]], ["2"])

        code, out, err = self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertIn("Stage 2 belongs to web", out)
        self.assertIn(f"--confirm {status['confirm_token']}", out)
        self.assertEqual(self.web_records(), ())  # a status creates nothing

        code, out, err = self.run_cli(
            "resume-plan", "--run-key", K, "--confirm", status["confirm_token"], "--repo-root", str(self.repo)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("plan complete", out)
        (execution,) = self.web_records()
        self.assertEqual(execution.run_key, K2)
        self.assertEqual(execution.base_sha, web_main)
        self.assertEqual(execution.target_branch, "main")
        self.assertEqual(
            execution.logical_slice, {"logical_key": K, "home_common_dir": str(self.home_common()), "index": 2}
        )
        self.assertEqual(execution.input_path, str(logical_plan.snapshot_path(self.home_common(), self.logical())))
        self.assertTrue(Path(execution.worktree_path).name.startswith("web-sparring-"))
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1, 2])
        self.assertTrue(logical_plan.derived_status(self.logical()).complete)

    def test_dirty_target_checkout_is_left_alone_and_kept_out_of_the_run(self):
        self.finish_first()
        (self.web / "tracked.txt").write_text("my edit\n", encoding="utf-8")
        (self.web / "mine.txt").write_text("mine\n", encoding="utf-8")
        before = (
            _git(self.web, "rev-parse", "HEAD"), _git(self.web, "symbolic-ref", "HEAD"),
            _git(self.web, "status", "--porcelain"),
        )
        token = self.status_json()["confirm_token"]
        with mock.patch("agent_sparring.cli._run_in_record", return_value=0):
            code, _, err = self.run_cli("resume-plan", "--run-key", K, "--confirm", token, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertIn("not part of this run", err)
        (execution,) = self.web_records()
        worktree = Path(execution.worktree_path)
        self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "web\n")
        self.assertFalse((worktree / "mine.txt").exists())
        self.assertEqual(
            (
                _git(self.web, "rev-parse", "HEAD"), _git(self.web, "symbolic-ref", "HEAD"),
                _git(self.web, "status", "--porcelain"),
            ),
            before,
        )
        self.assertEqual((self.web / "tracked.txt").read_text(encoding="utf-8"), "my edit\n")
        self.assertEqual((self.web / "mine.txt").read_text(encoding="utf-8"), "mine\n")

    def test_target_main_moving_before_creation_refuses_the_token(self):
        self.finish_first()
        token = self.status_json()["confirm_token"]
        (self.web / "tracked.txt").write_text("moved\n", encoding="utf-8")
        _run_git(self.web, "commit", "-qam", "main moves")
        moved = _git(self.web, "rev-parse", "main")

        code, out, err = self.run_cli(
            "resume-plan", "--run-key", K, "--confirm", token, "--repo-root", str(self.repo)
        )
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)
        self.assertIn(moved, out)
        self.assertEqual(self.web_records(), ())
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1])
        status = self.status_json("--confirm", token)
        self.assertEqual((status["status"], status["code"], status["next"]["base_sha"]), ("refused", "confirm_mismatch", moved))
        self.assertNotEqual(status["confirm_token"], token)

    def test_the_token_binds_the_push_flag(self):
        self.finish_first()
        plain = self.status_json()["confirm_token"]
        pushing = self.status_json("--allow-push-for-run")["confirm_token"]
        self.assertNotEqual(plain, pushing)
        code, _, err = self.run_cli("resume-plan", "--run-key", K, "--confirm", plain, "--allow-push-for-run",
                                    "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertEqual(self.web_records(), ())

    def test_another_active_managed_run_in_the_target_is_untouched(self):
        self.finish_first()
        other_plan = self.web / "docs" / "other.md"
        other_plan.parent.mkdir()
        other_plan.write_text(plan("Other work."), encoding="utf-8")
        _run_git(self.web, "add", ".")
        _run_git(self.web, "commit", "-qm", "other plan")
        prepared = managed_run.plan_managed_run(
            self.web, self.web / ".sparring", source_label=lambda read, mapped: "docs/other.md",
            input_kind="markdown", input_path=other_plan, target_branch="main", run_key="other-run-1",
            new_run_key=lambda label: "other-run-1",
        )
        other = managed_run.create_managed_worktree(self.web, prepared)
        other_state = Path(other.worktree_path) / ".sparring" / "plans" / "other-run-1.json"
        other_state.parent.mkdir(parents=True)
        other_state.write_text('{"status": "running"}\n', encoding="utf-8")
        record_file = managed_run.record_path(self.web, "other-run-1")

        def snapshot():
            worktree = Path(other.worktree_path)
            files = {
                str(p.relative_to(worktree)): p.read_bytes()
                for p in sorted(worktree.rglob("*")) if p.is_file() and ".git" not in p.parts
            }
            return record_file.read_bytes(), other_state.read_bytes(), files, _git(worktree, "rev-parse", "HEAD")

        before = snapshot()
        token = self.status_json()["confirm_token"]
        code, _, err = self.run_cli("resume-plan", "--run-key", K, "--confirm", token, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertEqual({r.run_key for r in self.web_records()}, {"other-run-1", K2})
        self.assertEqual(snapshot(), before)


class InterruptionTests(_CrossCase):
    verdicts = [READY, NEEDS_YOU, NEEDS_YOU, NEEDS_YOU]

    def setUp(self):
        super().setUp()
        code, _, err = self.start()
        self.assertEqual(code, 0, err)
        self.finish_first()
        self.token = self.status_json()["confirm_token"]

    def confirm(self, token=None):
        return self.run_cli("resume-plan", "--run-key", K, "--confirm", token or self.token, "--repo-root", str(self.repo))

    def worktrees(self):
        return [e for e in managed_run.worktree_list(self.web) if e["branch"] == managed_run.managed_branch(K2)]

    def test_interrupted_between_record_creation_and_slice_created(self):
        with mock.patch.object(
            logical_plan, "record_slice_created", side_effect=managed_run.ManagedRunError("interrupted", "boom")
        ):
            code, _, _ = self.confirm()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.web_records()), 1)
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1])

        code, out, err = self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1, 2])
        self.assertEqual(len(self.web_records()), 1)
        self.assertEqual(len(self.worktrees()), 1)

    def test_interrupted_creation_before_created_is_proven_on_resume(self):
        real = managed_run.append_event

        def no_created(repo_root, run_key, event, detail=None):
            if event == "created":
                raise managed_run.ManagedRunError("interrupted", "boom")
            return real(repo_root, run_key, event, detail)

        with mock.patch.object(managed_run, "append_event", side_effect=no_created):
            code, _, _ = self.confirm()
        self.assertEqual(code, 1)
        (execution,) = self.web_records()
        self.assertEqual(execution.lifecycle, "creating")

        code, _, err = self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        (execution,) = self.web_records()
        self.assertEqual(execution.lifecycle, "created")
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1, 2])
        self.assertEqual(len(self.worktrees()), 1)

    def test_retry_never_duplicates(self):
        with mock.patch("agent_sparring.cli._run_in_record", return_value=1):
            code, _, _ = self.confirm()
        self.assertEqual(code, 1)
        records = self.web_records()
        self.assertEqual(len(records), 1)

        code, _, err = self.confirm()  # the same confirmation again
        self.assertEqual(code, 0, err)
        code, _, err = self.confirm()
        self.assertEqual(code, 0, err)
        self.assertEqual([r.run_key for r in self.web_records()], [K2])
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1, 2])

    def test_home_and_target_resolve_the_same_derived_status(self):
        code, _, err = self.confirm()
        self.assertEqual(code, 0, err)  # paused on NEEDS_YOU in the web part
        from_home = self.status_json()
        from_web = self.status_json(where="web")
        for payload in (from_home, from_web):
            payload.pop("_err")
        self.assertEqual(from_home, from_web)
        self.assertEqual(from_home["status"], "exists")
        self.assertEqual([s["complete"] for s in from_home["slices"]], [True, False])

        # And either checkout resumes the same part.
        code, _, err = self.from_web("resume-plan", "--run-key", K, "--evidence", "checked")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded human evidence", err)
        self.assertEqual(len(self.web_records()), 1)


FOREIGN_FIRST = plan("Repository: web\n\nBuild there.", "Build here.")


class HomeFirstTests(_CrossCase):
    def setUp(self):
        super().setUp()
        self.plan_path.write_text(FOREIGN_FIRST, encoding="utf-8")
        _run_git(self.repo, "commit", "-qam", "foreign first")

    def assert_nothing_created(self):
        self.assertEqual(managed_run.list_records(self.repo), ())
        self.assertEqual(self.web_records(), ())
        self.assertFalse(logical_plan.records_dir(self.home_common()).exists())
        self.assertFalse(logical_plan.records_dir(managed_run.git_common_dir(self.web)).exists())

    def test_run_plan_refuses_a_plan_whose_first_stage_is_not_home(self):
        code, _, err = self.start()
        self.assertEqual(code, 1)
        self.assertIn("[first_stage_not_home]", err)
        self.assert_nothing_created()

    def test_start_plan_refuses_a_plan_whose_first_stage_is_not_home(self):
        code, out, _ = self.run_cli(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed",
            "--repository", f"web={self.web}", "--json",
        )
        status = json.loads(out)
        self.assertEqual((code, status["status"], status["confirm_token"]), (1, "refused", None))
        self.assertIn("[first_stage_not_home]", status["error"])
        self.assert_nothing_created()


class DiscoveryTests(_CrossCase):
    def setUp(self):
        super().setUp()
        code, _, err = self.start()
        self.assertEqual(code, 0, err)

    def test_the_target_names_the_home_from_the_start(self):
        pointer = logical_plan.pointer_path(managed_run.git_common_dir(self.web), K)
        self.assertEqual(
            json.loads(pointer.read_text(encoding="utf-8")),
            {"schema_version": 1, "logical_key": K, "home_common_dir": str(self.home_common())},
        )
        self.assertFalse(logical_plan.pointer_path(self.home_common(), K).exists())

    def test_status_and_confirmation_from_the_target_before_its_part_exists(self):
        refused = self.status_json(where="web")
        self.assertEqual((refused["status"], refused["code"]), ("refused", "previous_not_integrated"))
        self.finish_first()
        from_home = self.status_json()
        from_web = self.status_json(where="web")
        for payload in (from_home, from_web):
            payload.pop("_err")
        self.assertEqual(from_home, from_web)
        self.assertEqual(from_web["status"], "ready")
        self.assertEqual(self.web_records(), ())

        code, out, err = self.from_web("resume-plan", "--run-key", K, "--confirm", from_web["confirm_token"])
        self.assertEqual(code, 0, err)
        self.assertIn("plan complete", out)
        self.assertEqual([r.run_key for r in self.web_records()], [K2])
        self.assertTrue(logical_plan.derived_status(self.logical()).complete)

    def test_a_pointer_to_a_plan_that_does_not_bind_the_repository_is_refused(self):
        other = _make_repo(self.repo.parent, "other", "other")
        pointer = logical_plan.pointer_path(managed_run.git_common_dir(other), K)
        pointer.parent.mkdir(parents=True)
        pointer.write_bytes(logical_plan.pointer_path(managed_run.git_common_dir(self.web), K).read_bytes())
        code, _, err = self.run_cli(
            "resume-plan", "--run-key", K, "--json", "--repo-root", str(other), sparring_dir=other / ".sparring"
        )
        self.assertEqual(code, 1)
        self.assertIn("does not bind this repository", err)


class EarlyInterruptionTests(_CrossCase):
    verdicts = [READY, NEEDS_YOU, NEEDS_YOU]

    def setUp(self):
        super().setUp()
        code, _, err = self.start()
        self.assertEqual(code, 0, err)
        self.finish_first()
        self.token = self.status_json()["confirm_token"]
        self.branch = managed_run.managed_branch(K2)

    def interrupt_at(self, git_step: tuple[str, ...]):
        real = managed_run._git

        def crash(cwd, *args):
            if args[: len(git_step)] == git_step:
                raise managed_run.ManagedRunError("interrupted", "boom")
            return real(cwd, *args)

        with mock.patch.object(managed_run, "_git", side_effect=crash):
            code, _, _ = self.run_cli(
                "resume-plan", "--run-key", K, "--confirm", self.token, "--repo-root", str(self.repo)
            )
        self.assertEqual(code, 1)
        (execution,) = self.web_records()
        self.assertEqual(execution.lifecycle, "creating")
        self.assertFalse(Path(execution.worktree_path).exists())
        return execution

    def resume(self):
        return self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))

    def assert_one_execution(self):
        (execution,) = self.web_records()
        self.assertEqual(execution.lifecycle, "created")
        self.assertEqual(len([e for e in managed_run.worktree_list(self.web) if e["branch"] == self.branch]), 1)
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1, 2])
        self.assertEqual(_git(Path(execution.worktree_path), "symbolic-ref", "--short", "HEAD"), self.branch)

    def test_interrupted_right_after_the_record(self):
        self.interrupt_at(("update-ref",))
        self.assertFalse(managed_run.branch_exists(self.web, self.branch))
        code, _, err = self.resume()
        self.assertEqual(code, 0, err)
        self.assert_one_execution()
        code, _, err = self.resume()  # and again: still one
        self.assertEqual(code, 0, err)
        self.assert_one_execution()

    def test_interrupted_after_the_branch(self):
        execution = self.interrupt_at(("worktree", "add"))
        self.assertTrue(managed_run.branch_exists(self.web, self.branch))
        self.assertEqual(_git(self.web, "rev-parse", f"refs/heads/{self.branch}"), execution.base_sha)
        code, _, err = self.resume()
        self.assertEqual(code, 0, err)
        self.assert_one_execution()

    def test_a_branch_not_made_by_the_engine_is_never_attributed(self):
        execution = self.interrupt_at(("update-ref",))
        _run_git(self.web, "branch", self.branch, execution.base_sha)
        code, _, err = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("creation_incomplete", err)
        (after,) = self.web_records()
        self.assertEqual(after.lifecycle, "creating")
        self.assertFalse(Path(execution.worktree_path).exists())
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1])


class CollisionTests(_CrossCase):
    """A run in the target that merely uses the part's derived key is
    refused, never adopted, recovered or resumed."""

    def setUp(self):
        super().setUp()
        code, _, err = self.start()
        self.assertEqual(code, 0, err)
        self.finish_first()
        self.token = self.status_json()["confirm_token"]
        other_plan = self.web / "docs" / "other.md"
        other_plan.parent.mkdir()
        other_plan.write_text(plan("Other work."), encoding="utf-8")
        _run_git(self.web, "add", ".")
        _run_git(self.web, "commit", "-qm", "other plan")
        self.prepared = managed_run.plan_managed_run(
            self.web, self.web / ".sparring", source_label=lambda read, mapped: "docs/other.md",
            input_kind="markdown", input_path=other_plan, target_branch="main", run_key=K2,
            new_run_key=lambda label: K2,
        )

    def collide(self, logical_slice=None, *, git_state=True):
        from dataclasses import replace

        record = replace(self.prepared.record, logical_slice=logical_slice)
        if git_state:
            created = managed_run.create_managed_worktree(self.web, replace(self.prepared, record=record))
            state = Path(created.worktree_path) / ".sparring" / "plans" / f"{K2}.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"status": "running"}\n', encoding="utf-8")
        else:
            managed_run.create_record(self.web, record)

    def snapshot(self):
        record = managed_run.read_record(self.web, K2)
        worktree = Path(record.worktree_path)
        files = (
            {str(p.relative_to(worktree)): p.read_bytes() for p in sorted(worktree.rglob("*"))
             if p.is_file() and ".git" not in p.parts}
            if worktree.exists() else None
        )
        return (
            managed_run.record_path(self.web, K2).read_bytes(), files, worktree.exists(),
            managed_run.branch_exists(self.web, record.branch),
            logical_plan.record_path(self.home_common(), K).read_bytes(),
        )

    def assert_refused_everywhere(self):
        before = self.snapshot()
        adapters_before = set(self.stage_adapters)
        status = self.status_json()
        self.assertEqual((status["_code"], status["status"], status["code"]), (1, "refused", "execution_not_in_plan"))
        for where in ("home", "web"):
            argv = ["resume-plan", "--run-key", K, "--evidence", "x"]
            code, _, err = self.run_cli(*argv, "--repo-root", str(self.repo)) if where == "home" else self.from_web(*argv)
            self.assertEqual(code, 1)
            self.assertIn("execution_not_in_plan", err)
        code, _, err = self.run_cli(
            "resume-plan", "--run-key", K, "--confirm", self.token, "--allow-push-for-run", "--repo-root", str(self.repo)
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([e["detail"]["index"] for e in self.slice_events()], [1])
        self.assertEqual(set(self.stage_adapters), adapters_before)  # no agent ran

    def test_an_ordinary_run_with_the_derived_key(self):
        self.collide()
        self.assert_refused_everywhere()

    def test_a_run_naming_another_logical_plan(self):
        self.collide({"logical_key": "plan-other-0002", "home_common_dir": str(self.home_common()), "index": 2})
        self.assert_refused_everywhere()

    def test_a_run_naming_another_home(self):
        self.collide({"logical_key": K, "home_common_dir": str(managed_run.git_common_dir(self.web)), "index": 2})
        self.assert_refused_everywhere()

    def test_an_unrelated_interrupted_creation_is_not_recovered(self):
        self.collide(git_state=False)
        self.assert_refused_everywhere()
        self.assertFalse(Path(managed_run.read_record(self.web, K2).worktree_path).exists())
