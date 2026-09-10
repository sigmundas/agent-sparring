import os
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.concurrency import LOCK_FILENAME, StageLockError, stage_lock


class StageLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stage_dir = Path(self._tmp.name) / "stage-1"

    def test_lock_is_released_after_context_exits(self):
        with stage_lock(self.stage_dir):
            self.assertTrue((self.stage_dir / LOCK_FILENAME).is_file())
        self.assertFalse((self.stage_dir / LOCK_FILENAME).is_file())

    def test_lock_held_by_live_process_blocks_a_second_acquire(self):
        self.stage_dir.mkdir(parents=True)
        (self.stage_dir / LOCK_FILENAME).write_text(str(os.getpid()), encoding="utf-8")
        with self.assertRaises(StageLockError):
            with stage_lock(self.stage_dir):
                pass

    def test_stale_lock_from_dead_process_is_reclaimed(self):
        self.stage_dir.mkdir(parents=True)
        # A pid essentially guaranteed not to be alive in the test process's
        # pid namespace.
        dead_pid = 2**30
        (self.stage_dir / LOCK_FILENAME).write_text(str(dead_pid), encoding="utf-8")
        with stage_lock(self.stage_dir):
            self.assertTrue((self.stage_dir / LOCK_FILENAME).is_file())
        self.assertFalse((self.stage_dir / LOCK_FILENAME).is_file())

    def test_malformed_lock_contents_are_treated_as_stale(self):
        self.stage_dir.mkdir(parents=True)
        (self.stage_dir / LOCK_FILENAME).write_text("not-a-pid", encoding="utf-8")
        with stage_lock(self.stage_dir):
            pass

    def test_lock_released_even_if_body_raises(self):
        with self.assertRaises(RuntimeError):
            with stage_lock(self.stage_dir):
                raise RuntimeError("boom")
        self.assertFalse((self.stage_dir / LOCK_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
