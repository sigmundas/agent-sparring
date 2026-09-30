"""Fixture-based regression tests for the migration-order classification
engine.

The fixture repo mirrors the shape of the sporely-web 3W/1B incident (see
the Stage A brief): a deferred migration deliberately absent from
production, two later pending migrations that become "stale" once
production is observed to have advanced past them, and an emergency
remote-only migration pair applied out of band. It is a synthetic tmp-dir
git repo built fresh per test -- sporely-web's real repository/state is
never touched by this suite.
"""

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.config import MigrationsConfig
from agent_sparring.migration_history import HISTORY_VERSION, SOURCE_RECORDED, HistorySnapshot
from agent_sparring.migration_status import (
    CLASS_APPLIED,
    CLASS_DEFERRED,
    CLASS_DEFERRED_TAMPERED,
    CLASS_DUPLICATE_VERSION,
    CLASS_MIGRATION_ORDER_STALE,
    CLASS_REMOTE_ONLY,
    CLASS_UNAPPLIED,
    PROBLEM_DUPLICATE_VERSION,
    PROBLEM_UNRECOGNISED_FILE,
    MigrationStatusError,
    ReconciliationProposal,
    RetimestampProposal,
    classify,
    render_report,
)

DEFERRED_VERSION = "20260914090000"
DEFERRED_FILE = "20260914090000_extend_reference_snapshots_to_version_2.sql"
DEFERRED_CONTENT = "-- deferred migration: snapshot v2 rollout gate unmet\nselect 1;\n"

APPLIED_FILES = [
    "20260910100000_a.sql",
    "20260912110000_b.sql",
    DEFERRED_FILE,
    "20260920140000_c.sql",
    "20260925160000_d.sql",
]
APPLIED_VERSIONS = ["20260910100000", "20260912110000", "20260920140000", "20260925160000"]
HEAD_A = "20260925160000"

PENDING_ONE = "20260929120000_pending_one.sql"
PENDING_TWO = "20260929130000_pending_two.sql"
PENDING_ONE_VERSION = "20260929120000"
PENDING_TWO_VERSION = "20260929130000"

REMOTE_ONLY_ONE = "20260930181144"
REMOTE_ONLY_TWO = "20260930181742"


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_fixture_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")

    for name in APPLIED_FILES:
        content = DEFERRED_CONTENT if name == DEFERRED_FILE else f"select '{name}';\n"
        _write(repo, f"supabase/migrations/{name}", content)
    deferred_sha = hashlib.sha256(DEFERRED_CONTENT.encode("utf-8")).hexdigest()
    _write(
        repo,
        "supabase/deploy-exceptions.json",
        (
            '{"deferredMigrations": [{'
            f'"version": "{DEFERRED_VERSION}", "file": "{DEFERRED_FILE}", '
            f'"sha256": "{deferred_sha}", "reason": "rollout gate unmet"'
            "}]}"
        ),
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "applied migration history plus one deferred")

    _run_git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, f"supabase/migrations/{PENDING_ONE}", "select 'pending one';\n")
    _write(repo, f"supabase/migrations/{PENDING_TWO}", "select 'pending two';\n")
    _write(
        repo,
        "docs/runbook.md",
        f"Deploy {PENDING_ONE} and {PENDING_TWO} together, in that order.\n",
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "pending migrations plus runbook")

    return repo


def _config(**overrides) -> MigrationsConfig:
    values = dict(
        adapter="supabase",
        directory="supabase/migrations",
        main_ref="main",
        target="production",
        target_ref=None,
        deferred_registry="supabase/deploy-exceptions.json",
        max_observation_age_minutes=60,
    )
    values.update(overrides)
    return MigrationsConfig(**values)


def _snapshot(applied: list[str], head: str | None, observed_at: str) -> HistorySnapshot:
    return HistorySnapshot(
        adapter="supabase",
        target_ref="origin/main",
        observed_at=observed_at,
        raw_sha256="deadbeef",
        applied=tuple(applied),
        head=head,
        source=SOURCE_RECORDED,
        version=HISTORY_VERSION,
    )


SNAPSHOT_A = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-25T16:05:00Z")
SNAPSHOT_B = _snapshot(
    APPLIED_VERSIONS + [REMOTE_ONLY_ONE, REMOTE_ONLY_TWO], REMOTE_ONLY_TWO, "2026-09-30T18:20:00Z"
)
NOW = datetime(2026, 9, 30, 19, 0, 0, tzinfo=timezone.utc)


class MigrationStatusFixtureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = build_fixture_repo(Path(self._tmp.name))
        self.config = _config()

    def _classify(self, snapshot, branch_ref="feature"):
        return classify(self.repo, self.config, branch_ref=branch_ref, snapshot=snapshot, now=NOW)

    def _status(self, report, version):
        return next(v for v in report.versions if v.version == version)

    # 1. prepared at head X stays valid while head = X
    def test_pending_migrations_stay_unapplied_while_head_has_not_advanced_past_them(self):
        report = self._classify(SNAPSHOT_A)
        self.assertEqual(self._status(report, PENDING_ONE_VERSION).classification, CLASS_UNAPPLIED)
        self.assertEqual(self._status(report, PENDING_TWO_VERSION).classification, CLASS_UNAPPLIED)
        self.assertEqual(report.remote_head, HEAD_A)

    # 2. production advances to Y past an unapplied version -> migration_order_stale
    def test_migration_order_stale_once_production_advances_past_pending_versions(self):
        report = self._classify(SNAPSHOT_B)
        self.assertEqual(
            self._status(report, PENDING_ONE_VERSION).classification, CLASS_MIGRATION_ORDER_STALE
        )
        self.assertEqual(
            self._status(report, PENDING_TWO_VERSION).classification, CLASS_MIGRATION_ORDER_STALE
        )
        stale = set(report.by_classification(CLASS_MIGRATION_ORDER_STALE))
        self.assertEqual(stale, {PENDING_ONE_VERSION, PENDING_TWO_VERSION})

    # 3. a remote-applied migration is never proposed for retimestamp/edit
    def test_applied_migration_is_never_proposed_for_retimestamp(self):
        report = self._classify(SNAPSHOT_B)
        for version in APPLIED_VERSIONS:
            self.assertEqual(self._status(report, version).classification, CLASS_APPLIED)
        proposed_old_versions = {
            p.old_version for p in report.proposals if isinstance(p, RetimestampProposal)
        }
        self.assertTrue(proposed_old_versions.isdisjoint(APPLIED_VERSIONS))

    # 4. an unapplied (now stale) migration may be proposed for retimestamping,
    #    preserving order after the head, with reference files listed
    def test_stale_migrations_are_proposed_for_retimestamp_with_references(self):
        report = self._classify(SNAPSHOT_B)
        retimestamps = [p for p in report.proposals if isinstance(p, RetimestampProposal)]
        by_old = {p.old_version: p for p in retimestamps}
        self.assertEqual(set(by_old), {PENDING_ONE_VERSION, PENDING_TWO_VERSION})

        one, two = by_old[PENDING_ONE_VERSION], by_old[PENDING_TWO_VERSION]
        # Strictly after the remote head, and relative order preserved.
        self.assertGreater(int(one.new_version), int(REMOTE_ONLY_TWO))
        self.assertGreater(int(two.new_version), int(one.new_version))
        # Every reference to the old version, file by file; the migration
        # file itself and .sparring are excluded.
        self.assertEqual(one.references, ("docs/runbook.md",))
        self.assertEqual(two.references, ("docs/runbook.md",))

    # 5. a deferred migration is not treated as drift
    def test_deferred_migration_is_not_treated_as_drift(self):
        for snapshot in (SNAPSHOT_A, SNAPSHOT_B):
            report = self._classify(snapshot)
            status = self._status(report, DEFERRED_VERSION)
            self.assertEqual(status.classification, CLASS_DEFERRED)
            self.assertEqual(status.flags, ())

    # 5b. deferred_tampered on sha mismatch
    def test_deferred_tampered_when_file_no_longer_matches_registered_sha(self):
        _run_git(self.repo, "checkout", "-q", "-b", "tampered", "feature")
        _write(
            self.repo,
            f"supabase/migrations/{DEFERRED_FILE}",
            DEFERRED_CONTENT + "-- tampered\n",
        )
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "tamper with the deferred migration")

        report = self._classify(SNAPSHOT_A, branch_ref="tampered")
        self.assertEqual(self._status(report, DEFERRED_VERSION).classification, CLASS_DEFERRED_TAMPERED)

    # 6. a remote-only emergency migration -> remote_history_not_reconciled,
    #    with a reconciliation proposal, clearing once the files are added
    def test_remote_only_migrations_flag_not_reconciled_until_files_are_added(self):
        report = self._classify(SNAPSHOT_B)
        self.assertTrue(report.remote_history_not_reconciled)
        remote_only = set(report.by_classification(CLASS_REMOTE_ONLY))
        self.assertEqual(remote_only, {REMOTE_ONLY_ONE, REMOTE_ONLY_TWO})
        reconciliations = {
            p.version for p in report.proposals if isinstance(p, ReconciliationProposal)
        }
        self.assertEqual(reconciliations, {REMOTE_ONLY_ONE, REMOTE_ONLY_TWO})

        _run_git(self.repo, "checkout", "-q", "-b", "reconciled", "feature")
        _write(
            self.repo,
            f"supabase/migrations/{REMOTE_ONLY_ONE}_hotfix_one.sql",
            "select 'hotfix one';\n",
        )
        _write(
            self.repo,
            f"supabase/migrations/{REMOTE_ONLY_TWO}_hotfix_two.sql",
            "select 'hotfix two';\n",
        )
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "reconcile the emergency hotfix migrations")

        cleared = self._classify(SNAPSHOT_B, branch_ref="reconciled")
        self.assertFalse(cleared.remote_history_not_reconciled)
        self.assertEqual(cleared.by_classification(CLASS_REMOTE_ONLY), ())
        self.assertEqual(self._status(cleared, REMOTE_ONLY_ONE).classification, CLASS_APPLIED)
        self.assertEqual(self._status(cleared, REMOTE_ONLY_TWO).classification, CLASS_APPLIED)
        self.assertFalse(
            any(isinstance(p, ReconciliationProposal) for p in cleared.proposals)
        )

    # no snapshot -> production_history_unknown
    def test_no_snapshot_means_production_history_unknown(self):
        report = self._classify(snapshot=None)
        self.assertTrue(report.production_history_unknown)
        self.assertIsNone(report.remote_head)
        # Without a known head, nothing can be judged "stale" -- everything
        # local-only is merely unapplied.
        self.assertEqual(
            self._status(report, PENDING_ONE_VERSION).classification, CLASS_UNAPPLIED
        )
        self.assertEqual(report.by_classification(CLASS_MIGRATION_ORDER_STALE), ())

    # a stale (too-old) snapshot warning
    def test_stale_snapshot_warning_past_max_observation_age(self):
        old_snapshot = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-25T16:05:00Z")
        far_future = datetime(2026, 9, 30, 19, 0, 0, tzinfo=timezone.utc)
        report = classify(
            self.repo, self.config, branch_ref="feature", snapshot=old_snapshot, now=far_future
        )
        self.assertTrue(report.stale_snapshot)
        self.assertIn("Warning", render_report(report))

    def test_fresh_snapshot_is_not_flagged_stale(self):
        recent = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-30T18:50:00Z")
        report = classify(self.repo, self.config, branch_ref="feature", snapshot=recent, now=NOW)
        self.assertFalse(report.stale_snapshot)

    def test_naive_snapshot_timestamp_is_a_clean_status_error(self):
        naive = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-30T18:50:00")
        with self.assertRaisesRegex(MigrationStatusError, "timezone"):
            self._classify(naive)

    def test_future_snapshot_beyond_clock_skew_is_a_status_error(self):
        future = _snapshot(APPLIED_VERSIONS, HEAD_A, "2099-01-01T00:00:00Z")
        with self.assertRaisesRegex(MigrationStatusError, "future"):
            self._classify(future)

    def test_snapshot_within_clock_skew_never_reports_a_negative_age(self):
        slightly_ahead = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-30T19:02:00Z")
        report = self._classify(slightly_ahead)
        self.assertEqual(report.snapshot_age_minutes, 0.0)
        self.assertFalse(report.stale_snapshot)

    def _commit_on(self, branch: str, files: dict[str, str]) -> None:
        _run_git(self.repo, "checkout", "-q", "-b", branch, "feature")
        for rel_path, content in files.items():
            _write(self.repo, rel_path, content)
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", f"commit on {branch}")

    # Finding 3: two files with one version must not collapse into one.
    def test_two_files_with_one_version_are_reported_not_collapsed(self):
        self._commit_on(
            "dup",
            {
                "supabase/migrations/20260920000000_m.sql": "select 'm';\n",
                "supabase/migrations/20260920000000_dup.sql": "select 'dup';\n",
            },
        )
        report = self._classify(SNAPSHOT_A, branch_ref="dup")
        status = self._status(report, "20260920000000")
        self.assertEqual(status.classification, CLASS_DUPLICATE_VERSION)
        problems = [p for p in report.file_problems if p.kind == PROBLEM_DUPLICATE_VERSION]
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].version, "20260920000000")
        self.assertEqual(
            problems[0].paths,
            (
                "supabase/migrations/20260920000000_dup.sql",
                "supabase/migrations/20260920000000_m.sql",
            ),
        )
        self.assertTrue(problems[0].blocking)
        # Not silently retimed as if it were one ordinary stale migration.
        self.assertFalse(
            any(
                isinstance(p, RetimestampProposal) and p.old_version == "20260920000000"
                for p in report.proposals
            )
        )
        self.assertTrue(report.has_findings())
        self.assertIn("20260920000000", render_report(report))
        self.assertEqual(report.as_dict()["file_problems"][0]["kind"], PROBLEM_DUPLICATE_VERSION)

    def test_duplicate_of_a_deferred_version_is_not_hash_checked_against_either(self):
        self._commit_on(
            "dup-deferred",
            {f"supabase/migrations/{DEFERRED_VERSION}_other.sql": "select 'other';\n"},
        )
        report = self._classify(SNAPSHOT_A, branch_ref="dup-deferred")
        self.assertEqual(
            self._status(report, DEFERRED_VERSION).classification, CLASS_DUPLICATE_VERSION
        )
        self.assertTrue(report.has_findings())

    # Finding 4: files the adapter cannot parse are reported, never dropped.
    def test_unrecognised_files_in_the_migrations_directory_are_reported(self):
        self._commit_on(
            "odd-files",
            {
                "supabase/migrations/README.md": "notes\n",
                "supabase/migrations/20260929140000-dash.sql": "select 1;\n",
                "supabase/migrations/nested/20260929150000_nested.sql": "select 2;\n",
            },
        )
        report = self._classify(SNAPSHOT_A, branch_ref="odd-files")
        problems = {
            p.paths[0]: p for p in report.file_problems if p.kind == PROBLEM_UNRECOGNISED_FILE
        }
        self.assertEqual(
            set(problems),
            {
                "supabase/migrations/README.md",
                "supabase/migrations/20260929140000-dash.sql",
                "supabase/migrations/nested/20260929150000_nested.sql",
            },
        )
        # A .sql file the migration tool would skip never deploys: blocking.
        self.assertTrue(problems["supabase/migrations/20260929140000-dash.sql"].blocking)
        self.assertTrue(problems["supabase/migrations/nested/20260929150000_nested.sql"].blocking)
        # A non-SQL file is reported but does not block a clean result.
        self.assertFalse(problems["supabase/migrations/README.md"].blocking)
        self.assertEqual(problems["supabase/migrations/README.md"].refs, ("odd-files",))
        self.assertTrue(report.has_findings())
        self.assertNotIn("20260929150000", {v.version for v in report.versions})
        text = render_report(report)
        self.assertIn("20260929140000-dash.sql", text)
        self.assertIn("README.md", text)

    def test_a_non_sql_file_alone_is_reported_without_blocking(self):
        self._commit_on("readme-only", {"supabase/migrations/README.md": "notes\n"})
        recent = _snapshot(APPLIED_VERSIONS, HEAD_A, "2026-09-30T18:50:00Z")
        report = self._classify(recent, branch_ref="readme-only")
        self.assertEqual(len(report.file_problems), 1)
        self.assertFalse(report.has_findings())
        self.assertIn("README.md", render_report(report))

    def test_short_version_file_is_classified_not_dropped(self):
        self._commit_on("short", {"supabase/migrations/1_early.sql": "select 1;\n"})
        report = self._classify(SNAPSHOT_A, branch_ref="short")
        # "1" sorts before the head as a string, exactly as the CLI orders it.
        self.assertEqual(self._status(report, "1").classification, CLASS_MIGRATION_ORDER_STALE)

    def test_registry_entry_whose_file_encodes_another_version_is_refused(self):
        registry = json.dumps(
            {
                "deferredMigrations": [
                    {
                        "version": DEFERRED_VERSION,
                        "file": "20260914090001_wrong.sql",
                        "sha256": hashlib.sha256(DEFERRED_CONTENT.encode()).hexdigest(),
                        "reason": "r",
                    }
                ]
            }
        )
        self._commit_on("bad-registry", {"supabase/deploy-exceptions.json": registry})
        with self.assertRaisesRegex(MigrationStatusError, "20260914090001_wrong.sql"):
            self._classify(SNAPSHOT_A, branch_ref="bad-registry")

    def test_registry_entry_naming_a_different_file_for_its_version_is_tampered(self):
        registry = json.dumps(
            {
                "deferredMigrations": [
                    {
                        "version": DEFERRED_VERSION,
                        "file": f"{DEFERRED_VERSION}_some_other_name.sql",
                        "sha256": hashlib.sha256(DEFERRED_CONTENT.encode()).hexdigest(),
                        "reason": "r",
                    }
                ]
            }
        )
        self._commit_on("renamed-registry", {"supabase/deploy-exceptions.json": registry})
        report = self._classify(SNAPSHOT_A, branch_ref="renamed-registry")
        self.assertEqual(
            self._status(report, DEFERRED_VERSION).classification, CLASS_DEFERRED_TAMPERED
        )

    def test_migration_repair_never_appears_in_rendered_report(self):
        for snapshot in (None, SNAPSHOT_A, SNAPSHOT_B):
            report = self._classify(snapshot)
            text = render_report(report)
            self.assertNotIn("migration repair", text.lower())
            self.assertNotIn("migration repair", json.dumps(report.as_dict()).lower())


if __name__ == "__main__":
    unittest.main()
