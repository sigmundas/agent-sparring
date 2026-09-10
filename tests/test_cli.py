import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import main


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class CliHandoffAndSparringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        self.sparring_dir = self.repo / ".sparring"

    def test_handoff_then_record_sparring_end_to_end(self):
        exit_code = main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        self.assertEqual(exit_code, 0)

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = self.sparring_dir / "stages" / "stage-1" / "handoff.md"
        self.assertTrue(handoff_path.is_file())
        self.assertIn("did the thing", handoff_path.read_text(encoding="utf-8"))

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-sparring",
                "stage-1",
                "--action",
                "SEND_BACK",
                "--summary",
                "fix the widget",
            ]
        )
        self.assertEqual(exit_code, 0)
        sparring_path = self.sparring_dir / "stages" / "stage-1" / "sparring.md"
        self.assertIn("fix the widget", sparring_path.read_text(encoding="utf-8"))

    def test_handoff_honors_configured_repo_root_relative_to_project(self):
        # .sparring lives in a plain project directory; the actual git repo
        # is a sibling directory, reachable only through [repo].root in
        # project.toml. Without --repo-root, the CLI must resolve that
        # configured root relative to the project root (parent of
        # .sparring), not the process CWD.
        project_dir = Path(self._tmp.name) / "project"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = "../repo"\n', encoding="utf-8"
        )

        exit_code = main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])
        self.assertEqual(exit_code, 0)

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = sparring_dir / "stages" / "stage-1" / "handoff.md"
        content = handoff_path.read_text(encoding="utf-8")
        self.assertIn("`main`", content)
        self.assertIn("did the thing", content)

    def test_handoff_explicit_repo_root_overrides_configured_root(self):
        project_dir = Path(self._tmp.name) / "project2"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = "../nonexistent"\n', encoding="utf-8"
        )
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_handoff_missing_project_toml_uses_default_repo_root(self):
        # No project.toml at all: the default repo root (parent of
        # .sparring) must be used, and must not require --repo-root.
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = self.sparring_dir / "stages" / "stage-1" / "handoff.md"
        self.assertIn("`main`", handoff_path.read_text(encoding="utf-8"))

    def test_handoff_malformed_toml_fails_cleanly(self):
        # project.toml exists but is not valid TOML: must fail cleanly
        # (no traceback), and must NOT silently fall back to the default
        # repo root as if project.toml were absent.
        project_dir = Path(self._tmp.name) / "project3"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text("this is [ not valid toml", encoding="utf-8")
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_handoff_invalid_repo_root_type_fails_cleanly(self):
        # [repo].root has an invalid type: must fail cleanly, not silently
        # pick the default repo root nor raise an unhandled traceback.
        project_dir = Path(self._tmp.name) / "project4"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = 123\n', encoding="utf-8"
        )
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_handoff_explicit_repo_root_overrides_malformed_toml(self):
        # --repo-root must short-circuit before project.toml is even read,
        # so it works even when project.toml cannot be parsed at all.
        project_dir = Path(self._tmp.name) / "project5"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text("this is [ not valid toml", encoding="utf-8")
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_handoff_missing_stage_fails(self):
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "no-such-stage",
                "--claims",
                "x",
                "--repo-root",
                str(self.repo),
            ]
        )
        self.assertEqual(exit_code, 1)


class CliRunStageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.sparring_dir = self.repo / ".sparring"

    def test_dry_run_prints_prompt_without_invoking_any_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        (self.sparring_dir / "stages" / "stage-1" / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_run_stage_missing_stage_fails(self):
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "no-such-stage",
                "--repo-root",
                str(self.repo),
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_stage_refuses_on_main_before_invoking_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        _run_git(self.repo, "checkout", "-q", "main")

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--claude-executable",
                "/nonexistent/claude-should-not-be-invoked",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_stage_unsupported_provider_fails_cleanly(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--provider",
                "codex",
            ]
        )
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
