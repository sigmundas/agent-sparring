import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.git_context import (
    GitContextError,
    changed_files,
    current_branch,
    diff_patch,
    dirty_paths,
    gather_git_context,
    resolve_commit,
    verify_pushed,
)


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")


class GitContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        _run(self.repo, "add", "a.txt")
        _run(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        (self.repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        (self.repo / "b.txt").write_text("new file\n", encoding="utf-8")
        _run(self.repo, "add", "a.txt", "b.txt")
        _run(self.repo, "commit", "-q", "-m", "candidate")
        self.candidate_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_current_branch(self):
        self.assertEqual(current_branch(self.repo), "main")

    def test_current_branch_detached_raises(self):
        _run(self.repo, "checkout", "-q", self.base_sha)
        with self.assertRaises(GitContextError):
            current_branch(self.repo)

    def test_resolve_commit(self):
        self.assertEqual(resolve_commit(self.repo, "HEAD"), self.candidate_sha)
        self.assertEqual(resolve_commit(self.repo, self.base_sha[:10]), self.base_sha)

    def test_resolve_commit_unknown_revision_raises(self):
        with self.assertRaises(GitContextError):
            resolve_commit(self.repo, "not-a-revision")

    def test_changed_files(self):
        files = changed_files(self.repo, self.base_sha, self.candidate_sha)
        paths = {f.path for f in files}
        self.assertEqual(paths, {"a.txt", "b.txt"})
        statuses = {f.path: f.status for f in files}
        self.assertEqual(statuses["b.txt"], "A")

    def test_dirty_paths_clean(self):
        self.assertEqual(dirty_paths(self.repo), tuple())

    def test_dirty_paths_reports_untracked_and_modified(self):
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "c.txt").write_text("untracked\n", encoding="utf-8")
        paths = dirty_paths(self.repo)
        self.assertIn("a.txt", paths)
        self.assertIn("c.txt", paths)

    def test_verify_pushed_no_remote(self):
        pushed, detail = verify_pushed(self.repo, self.candidate_sha, "main")
        self.assertFalse(pushed)
        self.assertTrue(detail)

    def test_verify_pushed_true_when_remote_matches(self):
        remote = Path(self._tmp.name) / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
        )
        _run(self.repo, "remote", "add", "origin", str(remote))
        _run(self.repo, "push", "-q", "-u", "origin", "main")
        pushed, detail = verify_pushed(self.repo, self.candidate_sha, "main")
        self.assertTrue(pushed)
        self.assertIn(self.candidate_sha, detail)

    def test_gather_git_context_without_base(self):
        ctx = gather_git_context(self.repo, candidate_sha=self.candidate_sha, check_pushed=False)
        self.assertEqual(ctx.branch, "main")
        self.assertEqual(ctx.candidate_sha, self.candidate_sha)
        self.assertIsNone(ctx.base_sha)
        self.assertEqual(ctx.changed_files, tuple())
        self.assertIsNone(ctx.pushed)

    def test_changed_files_preserves_spaces_and_tabs_in_filenames(self):
        weird_name = "a file\twith tab.txt"
        (self.repo / weird_name).write_text("content\n", encoding="utf-8")
        _run(self.repo, "add", weird_name)
        _run(self.repo, "commit", "-q", "-m", "weird filename")
        new_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        files = changed_files(self.repo, self.candidate_sha, new_sha)
        paths = {f.path for f in files}
        self.assertIn(weird_name, paths)

    def test_dirty_paths_preserves_spaces_and_tabs_in_filenames(self):
        weird_name = "untracked file\twith tab.txt"
        (self.repo / weird_name).write_text("content\n", encoding="utf-8")
        self.assertIn(weird_name, dirty_paths(self.repo))

    def test_changed_files_rename_retains_both_paths(self):
        # Needs enough shared content for git's similarity heuristic to
        # detect a rename rather than reporting a delete+add pair.
        (self.repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _run(self.repo, "add", "a.txt")
        _run(self.repo, "commit", "-q", "-m", "grow a.txt")
        grown_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        _run(self.repo, "mv", "a.txt", "renamed.txt")
        (self.repo / "renamed.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-q", "-m", "rename a.txt")
        renamed_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        files = changed_files(self.repo, grown_sha, renamed_sha)
        renames = [f for f in files if f.status.startswith("R")]
        self.assertEqual(len(renames), 1)
        self.assertEqual(renames[0].old_path, "a.txt")
        self.assertEqual(renames[0].path, "renamed.txt")

    def test_diff_patch_preserves_trailing_whitespace_on_changed_line(self):
        (self.repo / "trailing.txt").write_text("line one\nline two \n", encoding="utf-8")
        _run(self.repo, "add", "trailing.txt")
        _run(self.repo, "commit", "-q", "-m", "trailing whitespace")
        new_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        patch = diff_patch(self.repo, self.candidate_sha, new_sha)
        self.assertIn("+line two \n", patch)

    def test_gather_git_context_with_base_and_dirty(self):
        (self.repo / "c.txt").write_text("dirty\n", encoding="utf-8")
        ctx = gather_git_context(
            self.repo,
            base_sha=self.base_sha,
            candidate_sha=self.candidate_sha,
            check_pushed=False,
        )
        self.assertEqual({f.path for f in ctx.changed_files}, {"a.txt", "b.txt"})
        self.assertIn("c.txt", ctx.dirty_paths)


if __name__ == "__main__":
    unittest.main()
