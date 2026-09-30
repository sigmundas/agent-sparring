import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import CHECK_MIGRATIONS_EXIT_FINDINGS, main
from agent_sparring.migration_history import now_observed_at

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


# What show-config and check-config print for NO_MIGRATIONS_TOML, captured by
# running the engine at 2e33833 -- the commit before [migrations] existed --
# on this same fixture (paths replaced by <REPO>/<XDG>). A project without
# [migrations] must see exactly this. If an unrelated change to these
# commands alters it, re-capture it from that change's parent, not from a
# checkout that has migration code.
NO_MIGRATIONS_TOML = (
    'project = "fictitious-widgets"\n\n'
    "[commands]\n"
    'test = "pytest -q"\n\n'
    "[agents.stage]\n"
    'provider = "claude-cli"\n'
)
BASELINE_SHOW_CONFIG_TEXT = (
    "project.toml: <REPO>/.sparring/project.toml\n"
    "stage agent: claude-cli (project) | model: provider default | effort: provider default\n"
    "sparring agent: codex-cli (engine-default) | model: provider default | effort: provider default\n"
)
BASELINE_SHOW_CONFIG_JSON_KEYS = [
    "config_path",
    "config_exists",
    "project",
    "error",
    "user_config_path",
    "user_config_exists",
    "stage",
    "sparring",
    "setup_problems",
]
BASELINE_SETUP_PROBLEMS = [
    ("not-ignored", "stage artifacts", ".sparring/stages/"),
    ("not-ignored", "plan-run state", ".sparring/plans/"),
    ("not-ignored", "plan intake", ".sparring/intake/"),
]
BASELINE_CHECK_CONFIG_EXIT = 1
BASELINE_CHECK_CONFIG_STDOUT = (
    "project: fictitious-widgets\n"
    "repo_root: .\n"
    "commands: {'test': 'pytest -q'}\n"
    "stage_agent_provider: claude-cli\n"
    "sparring_agent_provider: None\n"
    "default_sparring_mode: None\n"
    "PROJECT.md present: False\n"
    "user preferences: <XDG>/agent-sparring/config.toml\n"
    "stage agent: claude-cli (project) | model: provider default | effort: provider default\n"
    "sparring agent: codex-cli (engine-default) | model: provider default | effort: provider default\n"
    "stage artifacts git-ignored: NO\n"
    "plan-run state git-ignored: NO\n"
    "plan intake git-ignored: NO\n"
)


class NoMigrationsTableTests(unittest.TestCase):
    """A project without [migrations] behaves exactly as before the feature:
    no history directory, no new output, and the migration commands say
    plainly that the feature is not configured (exit 1, as documented)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.repo = root / "repo"
        self.xdg = root / "xdg"
        _init_repo(self.repo)
        _write(self.repo, "f.txt", "hi\n")
        _write(self.repo, ".sparring/project.toml", NO_MIGRATIONS_TOML)
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        self.sparring_dir = self.repo / ".sparring"
        env = {k: v for k, v in os.environ.items() if not k.startswith("SPARRING_")}
        env["XDG_CONFIG_HOME"] = str(self.xdg)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        placeholders = lambda text: text.replace(str(self.repo), "<REPO>").replace(str(self.xdg), "<XDG>")
        return code, placeholders(out.getvalue()), placeholders(err.getvalue())

    def _assert_no_migration_state(self):
        self.assertFalse((self.repo / ".git" / "agent-sparring").exists())
        self.assertEqual(sorted(p.name for p in self.sparring_dir.iterdir()), ["project.toml"])

    def test_check_migrations_says_not_configured_with_exit_1(self):
        code, out, _ = self._run("check-migrations", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["version"], 1)
        self.assertIs(payload["configured"], False)
        self.assertIn("not configured", payload["error"])
        self._assert_no_migration_state()

        code, out, err = self._run("check-migrations")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("not configured", err)
        self._assert_no_migration_state()

    def test_check_migrations_without_any_project_toml_says_not_configured(self):
        (self.sparring_dir / "project.toml").unlink()
        code, out, _ = self._run("check-migrations", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIs(payload["configured"], False)
        self.assertIn("not configured", payload["error"])
        self.assertFalse((self.repo / ".git" / "agent-sparring").exists())

    def test_record_migration_history_says_not_configured_and_writes_nothing(self):
        listing = Path(self._tmp.name) / "listing.txt"
        listing.write_text(VALID_LISTING, encoding="utf-8")
        code, _, err = self._run("record-migration-history", "--file", str(listing))
        self.assertEqual(code, 1)
        self.assertIn("not configured", err)
        self._assert_no_migration_state()

    def test_show_config_text_is_unchanged(self):
        self.assertEqual(self._run("show-config"), (0, BASELINE_SHOW_CONFIG_TEXT, ""))
        self._assert_no_migration_state()

    def test_show_config_json_and_setup_problems_are_unchanged(self):
        code, out, err = self._run("show-config", "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(list(payload), BASELINE_SHOW_CONFIG_JSON_KEYS)
        self.assertEqual(
            [(p["kind"], p["what"], p["ignore_line"]) for p in payload["setup_problems"]],
            BASELINE_SETUP_PROBLEMS,
        )
        self.assertNotIn("migration", out.lower())
        self._assert_no_migration_state()

    def test_check_config_output_is_unchanged(self):
        code, out, err = self._run("check-config")
        self.assertEqual(code, BASELINE_CHECK_CONFIG_EXIT)
        self.assertEqual(out, BASELINE_CHECK_CONFIG_STDOUT)
        self.assertNotIn("migration", err.lower())
        self._assert_no_migration_state()


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

    def _record(self, listing: str, observed_at: str | None = None) -> int:
        self.listing_path.write_text(listing, encoding="utf-8")
        return main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-migration-history",
                "--file",
                str(self.listing_path),
                "--observed-at",
                observed_at or now_observed_at(),
                "--repo-root",
                str(self.repo),
            ]
        )

    def _check_json(self) -> tuple[int, dict]:
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
        return exit_code, json.loads(buf.getvalue())

    def _history_dir(self) -> Path:
        return self.repo / ".git" / "agent-sparring" / "migrations" / "history"

    def test_record_refuses_observed_at_without_timezone(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._record(VALID_LISTING, observed_at="2026-10-01T10:00:00"), 1)
        self.assertIn("timezone", err.getvalue())
        self.assertFalse(self._history_dir().exists())

    def test_record_refuses_a_future_observed_at(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._record(VALID_LISTING, observed_at="2099-01-01T00:00:00Z"), 1)
        self.assertIn("future", err.getvalue())
        self.assertFalse(self._history_dir().exists())

    def test_check_migrations_reports_a_stored_future_snapshot(self):
        self.assertEqual(self._record(VALID_LISTING), 0)
        (self._history_dir() / "20990101T000000Z-000000000000.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "adapter": "supabase",
                    "target_ref": None,
                    "observed_at": "2099-01-01T00:00:00Z",
                    "source": "recorded",
                    "raw_sha256": "0" * 64,
                    "applied": ["20260925160000"],
                    "head": "20260925160000",
                }
            ),
            encoding="utf-8",
        )
        exit_code, payload = self._check_json()
        self.assertEqual(exit_code, 1)
        self.assertIn("future", payload["error"])

    def test_check_migrations_reports_a_stored_naive_snapshot_without_a_traceback(self):
        # A file written before this validation existed (or edited by hand).
        self._history_dir().mkdir(parents=True)
        (self._history_dir() / "20261001T100000Z-000000000000.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "adapter": "supabase",
                    "target_ref": None,
                    "observed_at": "2026-10-01T10:00:00",
                    "source": "recorded",
                    "raw_sha256": "0" * 64,
                    "applied": ["20260925160000"],
                    "head": "20260925160000",
                }
            ),
            encoding="utf-8",
        )
        exit_code, payload = self._check_json()
        self.assertEqual(exit_code, 1)
        self.assertIn("timezone", payload["error"])

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
