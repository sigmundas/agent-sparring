import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import CHECK_MIGRATIONS_EXIT_FINDINGS, main
from agent_sparring.migration_history import now_observed_at
from agent_sparring.setup_check import setup_problems

VALID_LISTING = (
    "        Local          | Remote         | Time (UTC)\n"
    "  ----------------------|----------------|---------------------\n"
    "        20260910100000 | 20260910100000 | 2026-09-10 10:00:00\n"
    "        20260925160000 | 20260925160000 | 2026-09-25 16:00:00\n"
)

STALE_LISTING = VALID_LISTING + "        20260930181742 | 20260930181742 | 2026-09-30 18:17:42\n"

MIGRATIONS_TOML = (
    'project = "fictitious-widgets"\n\n'
    "[migrations]\n"
    'adapter = "supabase"\n'
    'directory = "supabase/migrations"\n'
    'main_ref = "main"\n'
)


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")


class CheckMigrationsWithoutTableTests(unittest.TestCase):
    """Required regression: a repo without [migrations] is unaffected."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)
        _write(self.repo, "f.txt", "hi\n")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        self.sparring_dir = self.repo / ".sparring"

    def test_check_migrations_refuses_clearly_without_migrations_table(self):
        exit_code = main(["--sparring-dir", str(self.sparring_dir), "check-migrations", "--json"])
        self.assertEqual(exit_code, 1)
        # Nothing is written anywhere -- no history dir, no .sparring dir.
        self.assertFalse((self.repo / ".git" / "agent-sparring").exists())
        self.assertFalse(self.sparring_dir.exists())

    def test_record_migration_history_refuses_clearly_without_migrations_table(self):
        listing_path = Path(self._tmp.name) / "listing.txt"
        listing_path.write_text(VALID_LISTING, encoding="utf-8")
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-migration-history",
                "--file",
                str(listing_path),
            ]
        )
        self.assertEqual(exit_code, 1)
        self.assertFalse((self.repo / ".git" / "agent-sparring").exists())

    def test_show_config_and_setup_problems_unaffected_by_absent_migrations_table(self):
        # No project.toml at all: show-config's setup-problem reporting must
        # behave exactly as it did before this feature existed -- whatever
        # pre-existing workflow-state problems it reports (unrelated to
        # migrations), it must report none that mention migrations at all.
        exit_code = main(["--sparring-dir", str(self.sparring_dir), "show-config", "--json"])
        self.assertEqual(exit_code, 0)
        problems = setup_problems(self.repo, self.sparring_dir)
        self.assertFalse(any("migration" in p.as_dict()["what"].lower() for p in problems))


class RecordAndCheckMigrationsEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)
        _write(self.repo, "supabase/migrations/20260910100000_a.sql", "select 1;\n")
        _write(self.repo, "supabase/migrations/20260925160000_b.sql", "select 2;\n")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "base migrations")
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()
        (self.sparring_dir / "project.toml").write_text(MIGRATIONS_TOML, encoding="utf-8")
        self.listing_path = Path(self._tmp.name) / "listing.txt"

    def _record(self, listing: str) -> int:
        self.listing_path.write_text(listing, encoding="utf-8")
        return main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-migration-history",
                "--file",
                str(self.listing_path),
                "--observed-at",
                now_observed_at(),
                "--repo-root",
                str(self.repo),
            ]
        )

    def test_record_then_clean_check_migrations_exits_zero(self):
        self.assertEqual(self._record(VALID_LISTING), 0)
        history_dir = self.repo / ".git" / "agent-sparring" / "migrations" / "history"
        self.assertEqual(len(list(history_dir.glob("*.json"))), 1)

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "check-migrations",
                "--json",
                "--repo-root",
                str(self.repo),
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_check_migrations_json_shape_is_versioned(self):
        self._record(VALID_LISTING)
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "check-migrations",
                    "--json",
                    "--repo-root",
                    str(self.repo),
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["version"], 1)
        self.assertIn("versions", payload)
        self.assertIn("proposals", payload)
        self.assertNotIn("migration repair", json.dumps(payload).lower())

    def test_check_migrations_exits_with_findings_code_when_stale(self):
        # A pending migration on the branch that is now older than a
        # recorded, more-advanced production head.
        _write(self.repo, "supabase/migrations/20260929120000_pending.sql", "select 3;\n")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "pending migration")

        self.assertEqual(self._record(STALE_LISTING), 0)

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "check-migrations",
                "--repo-root",
                str(self.repo),
            ]
        )
        self.assertEqual(exit_code, CHECK_MIGRATIONS_EXIT_FINDINGS)

    def test_record_migration_history_refuses_garbage_via_cli(self):
        garbage = Path(self._tmp.name) / "garbage.txt"
        garbage.write_text("not a migration listing at all", encoding="utf-8")
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-migration-history",
                "--file",
                str(garbage),
                "--repo-root",
                str(self.repo),
                "--json",
            ]
        )
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
