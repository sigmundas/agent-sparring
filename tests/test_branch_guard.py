import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.branch_guard import BranchGuardError, ensure_branch_for_unattended_run


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class BranchGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")

    def test_refuses_main(self):
        with self.assertRaises(BranchGuardError):
            ensure_branch_for_unattended_run(self.repo)

    def test_refuses_master(self):
        _run_git(self.repo, "branch", "-m", "main", "master")
        with self.assertRaises(BranchGuardError):
            ensure_branch_for_unattended_run(self.repo)

    def test_allows_feature_branch_with_no_expectation(self):
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        branch = ensure_branch_for_unattended_run(self.repo)
        self.assertEqual(branch, "feature/x")

    def test_refuses_mismatched_expected_branch(self):
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        with self.assertRaises(BranchGuardError):
            ensure_branch_for_unattended_run(self.repo, expected_branch="feature/y")

    def test_allows_matching_expected_branch(self):
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        branch = ensure_branch_for_unattended_run(self.repo, expected_branch="feature/x")
        self.assertEqual(branch, "feature/x")

    def test_expected_branch_does_not_override_protected_branch_refusal(self):
        with self.assertRaises(BranchGuardError):
            ensure_branch_for_unattended_run(self.repo, expected_branch="main")


if __name__ == "__main__":
    unittest.main()
