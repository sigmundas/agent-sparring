import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.migration_history import (
    MigrationHistoryError,
    history_dir,
    latest_snapshot,
    record_history_snapshot,
    snapshot_from_dict,
)

VALID_LISTING = (
    "        Local          | Remote         | Time (UTC)\n"
    "  ----------------------|----------------|---------------------\n"
    "        20260913120000 | 20260913120000 | 2026-09-13 12:00:00\n"
    "        20260925160000 | 20260925160000 | 2026-09-25 16:00:00\n"
)


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    _run_git(repo, "add", "f.txt")
    _run_git(repo, "commit", "-q", "-m", "base")


class RecordHistorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)

    def test_records_a_parseable_listing(self):
        snapshot, path = record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=VALID_LISTING,
            target_ref="origin/main",
            observed_at="2026-09-30T12:00:00Z",
        )
        self.assertEqual(snapshot.applied, ("20260913120000", "20260925160000"))
        self.assertEqual(snapshot.head, "20260925160000")
        self.assertEqual(snapshot.adapter, "supabase")
        self.assertEqual(snapshot.target_ref, "origin/main")
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(history_dir(self.repo))))

    def test_written_under_git_common_dir_not_worktree(self):
        _, path = record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=VALID_LISTING,
            target_ref=None,
            observed_at="2026-09-30T12:00:00Z",
        )
        # Never inside the tracked working tree.
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(status.stdout.strip(), "")
        self.assertTrue(str(path).startswith(str(self.repo.resolve() / ".git")))

    def test_refuses_unparseable_input(self):
        with self.assertRaises(MigrationHistoryError):
            record_history_snapshot(
                self.repo, adapter_id="supabase", raw_text="not a migration listing", target_ref=None
            )

    def test_refuses_without_usable_git_common_dir(self):
        not_a_repo = Path(self._tmp.name) / "not-a-repo"
        not_a_repo.mkdir()
        with self.assertRaises(MigrationHistoryError):
            record_history_snapshot(
                not_a_repo, adapter_id="supabase", raw_text=VALID_LISTING, target_ref=None
            )

    def test_latest_snapshot_none_when_nothing_recorded(self):
        self.assertIsNone(latest_snapshot(self.repo))

    def test_latest_snapshot_picks_most_recently_observed(self):
        record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=VALID_LISTING,
            target_ref=None,
            observed_at="2026-09-25T00:00:00Z",
        )
        later_listing = VALID_LISTING + "        20260930181742 | 20260930181742 | 2026-09-30 18:17:42\n"
        record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=later_listing,
            target_ref=None,
            observed_at="2026-09-30T18:20:00Z",
        )
        snapshot = latest_snapshot(self.repo)
        self.assertEqual(snapshot.head, "20260930181742")
        self.assertEqual(snapshot.observed_at, "2026-09-30T18:20:00Z")

    def test_two_worktrees_share_one_history(self):
        # A linked worktree of the same clone must see the same snapshots:
        # history lives under the *common* git dir, not per-worktree.
        record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=VALID_LISTING,
            target_ref=None,
            observed_at="2026-09-25T00:00:00Z",
        )
        worktree = Path(self._tmp.name) / "worktree"
        _run_git(self.repo, "worktree", "add", "-q", "-b", "other", str(worktree))
        snapshot = latest_snapshot(worktree)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.head, "20260925160000")


def _store_raw_snapshot(repo: Path, name: str, payload) -> Path:
    """Write a snapshot file by hand, bypassing record_history_snapshot's
    validation -- the shape an older/damaged/hand-edited file could have."""

    directory = history_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _snapshot_payload(observed_at: str, head: str = "20260925160000") -> dict:
    return {
        "version": 1,
        "adapter": "supabase",
        "target_ref": None,
        "observed_at": observed_at,
        "source": "recorded",
        "raw_sha256": "0" * 64,
        "applied": [head],
        "head": head,
    }


class ObservedAtTimezoneTests(unittest.TestCase):
    """Finding 1: a timestamp without a timezone must never reach the
    naive-minus-aware subtraction in check-migrations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)

    def test_record_refuses_observed_at_without_timezone(self):
        with self.assertRaisesRegex(MigrationHistoryError, "timezone"):
            record_history_snapshot(
                self.repo,
                adapter_id="supabase",
                raw_text=VALID_LISTING,
                target_ref=None,
                observed_at="2026-10-01T10:00:00",
            )
        self.assertFalse(history_dir(self.repo).exists())

    def test_record_accepts_explicit_offset(self):
        snapshot, _ = record_history_snapshot(
            self.repo,
            adapter_id="supabase",
            raw_text=VALID_LISTING,
            target_ref=None,
            observed_at="2026-09-30T14:00:00+02:00",
        )
        self.assertEqual(snapshot.observed_at, "2026-09-30T14:00:00+02:00")

    def test_snapshot_from_dict_refuses_naive_observed_at(self):
        with self.assertRaisesRegex(MigrationHistoryError, "timezone"):
            snapshot_from_dict(_snapshot_payload("2026-10-01T10:00:00"), source="x.json")

    def test_latest_snapshot_reports_a_stored_naive_snapshot_cleanly(self):
        path = _store_raw_snapshot(self.repo, "a.json", _snapshot_payload("2026-10-01T10:00:00"))
        with self.assertRaises(MigrationHistoryError) as ctx:
            latest_snapshot(self.repo)
        self.assertIn(str(path), str(ctx.exception))
        self.assertIn("timezone", str(ctx.exception))

    def test_latest_snapshot_with_two_naive_snapshots_is_a_clean_error(self):
        _store_raw_snapshot(self.repo, "a.json", _snapshot_payload("2026-09-30T10:00:00"))
        _store_raw_snapshot(self.repo, "b.json", _snapshot_payload("2026-09-30T11:00:00"))
        with self.assertRaises(MigrationHistoryError):
            latest_snapshot(self.repo)

    def test_latest_snapshot_reports_a_malformed_snapshot_cleanly(self):
        path = _store_raw_snapshot(self.repo, "broken.json", "{not json")
        with self.assertRaises(MigrationHistoryError) as ctx:
            latest_snapshot(self.repo)
        self.assertIn(str(path), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
