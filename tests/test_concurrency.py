import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.concurrency import WorktreeLockError, worktree_lock


class WorktreeLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_a = Path(self._tmp.name) / "repo-a"
        self.repo_b = Path(self._tmp.name) / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()

    def test_lock_is_released_after_context_exits(self):
        with worktree_lock(self.repo_a):
            pass
        # Reacquiring immediately afterwards must succeed.
        with worktree_lock(self.repo_a):
            pass

    def test_second_acquire_for_same_worktree_is_refused(self):
        with worktree_lock(self.repo_a):
            with self.assertRaises(WorktreeLockError):
                with worktree_lock(self.repo_a):
                    pass

    def test_different_worktrees_do_not_block_each_other(self):
        with worktree_lock(self.repo_a):
            with worktree_lock(self.repo_b):
                pass  # must not raise

    def test_same_worktree_addressed_with_a_trailing_slash_still_contends(self):
        # A merely-different spelling of the same path must resolve to the
        # same lock, since it is the same worktree.
        with worktree_lock(self.repo_a):
            with self.assertRaises(WorktreeLockError):
                with worktree_lock(Path(str(self.repo_a) + "/")):
                    pass

    def test_lock_released_even_if_body_raises(self):
        with self.assertRaises(RuntimeError):
            with worktree_lock(self.repo_a):
                raise RuntimeError("boom")
        with worktree_lock(self.repo_a):
            pass


if __name__ == "__main__":
    unittest.main()
