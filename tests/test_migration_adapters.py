import unittest

import conftest_path  # noqa: F401

from agent_sparring.migration_adapters import (
    MigrationAdapterError,
    SupabaseMigrationAdapter,
    get_adapter,
)

# A realistic 'supabase migration list' capture: some rows applied both
# locally and remotely, one local-only (pending) row, and one remote-only
# (emergency/out-of-band) row -- the asymmetric shapes real output has.
REALISTIC_OUTPUT = """\
Connecting to remote database...
        Local          | Remote         | Time (UTC)
  ----------------------|----------------|---------------------
        20260913120000 | 20260913120000 | 2026-09-13 12:00:00
        20260925160000 | 20260925160000 | 2026-09-25 16:00:00
        20260929120000 |                |
                        | 20260930181144 |
"""


class SupabaseAdapterVersionOfTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SupabaseMigrationAdapter()

    def test_recognises_standard_filename(self):
        self.assertEqual(
            self.adapter.version_of("20260925160000_add_widgets.sql"), "20260925160000"
        )

    def test_recognises_full_path(self):
        self.assertEqual(
            self.adapter.version_of("supabase/migrations/20260925160000_add_widgets.sql"),
            "20260925160000",
        )

    def test_rejects_non_migration_file(self):
        self.assertIsNone(self.adapter.version_of("README.md"))

    # The Supabase CLI accepts any all-digit version prefix: its
    # migrateFilePattern is ^([0-9]+)_(.*)\.sql$ (pkg/migration/file.go).
    def test_accepts_any_all_digit_prefix_like_the_supabase_cli(self):
        self.assertEqual(self.adapter.version_of("2026091516_add_widgets.sql"), "2026091516")
        self.assertEqual(self.adapter.version_of("1_init.sql"), "1")
        self.assertEqual(self.adapter.version_of("123_.sql"), "123")

    def test_rejects_names_the_supabase_cli_would_skip(self):
        for name in (
            "20260925160000-add_widgets.sql",
            "v20260925160000_add_widgets.sql",
            "20260925160000.sql",
            "_add_widgets.sql",
            "\u0661\u0662_arabic_indic_digits.sql",
        ):
            with self.subTest(name=name):
                self.assertIsNone(self.adapter.version_of(name))

    def test_rejects_wrong_extension(self):
        self.assertIsNone(self.adapter.version_of("20260925160000_add_widgets.txt"))


class SupabaseAdapterOrderKeyTests(unittest.TestCase):
    def test_fixed_width_strings_sort_correctly(self):
        adapter = SupabaseMigrationAdapter()
        versions = ["20260930181742", "20260913120000", "20260925160000"]
        self.assertEqual(
            sorted(versions, key=adapter.order_key),
            ["20260913120000", "20260925160000", "20260930181742"],
        )


    def test_mixed_width_versions_sort_as_strings_like_the_supabase_cli(self):
        # db push orders local files by name (fs.ReadDir) and remote versions
        # by a text ORDER BY, comparing them as strings (FindPendingMigrations).
        adapter = SupabaseMigrationAdapter()
        self.assertEqual(sorted(["9", "10", "20260913120000"], key=adapter.order_key), ["10", "20260913120000", "9"])


class SupabaseAdapterParseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SupabaseMigrationAdapter()

    def test_realistic_output_with_asymmetric_rows(self):
        applied = self.adapter.parse_history(REALISTIC_OUTPUT)
        # Only Remote-column values are "applied"; the local-only pending row
        # (20260929120000) must not appear, and the remote-only emergency row
        # (20260930181144) must.
        self.assertEqual(applied, ["20260913120000", "20260925160000", "20260930181144"])

    def test_up_to_date_all_paired(self):
        text = (
            "        Local          | Remote         | Time (UTC)\n"
            "  ----------------------|----------------|---------------------\n"
            "        20260913120000 | 20260913120000 | 2026-09-13 12:00:00\n"
        )
        self.assertEqual(self.adapter.parse_history(text), ["20260913120000"])

    def test_short_versions_are_accepted_in_history_cells(self):
        text = (
            "        Local          | Remote         | Time (UTC)\n"
            "  ----------------------|----------------|---------------------\n"
            "        1              | 1              | 1\n"
            "        20260913120000 | 20260913120000 | 2026-09-13 12:00:00\n"
        )
        self.assertEqual(self.adapter.parse_history(text), ["1", "20260913120000"])

    def test_no_header_raises(self):
        with self.assertRaises(MigrationAdapterError):
            self.adapter.parse_history("Remote database is up to date.\n")

    def test_header_with_no_rows_raises(self):
        text = "        Local          | Remote         | Time (UTC)\n  ---|---|---\n"
        with self.assertRaises(MigrationAdapterError):
            self.adapter.parse_history(text)

    def test_malformed_version_cell_raises(self):
        text = (
            "        Local          | Remote         | Time (UTC)\n"
            "  ----------------------|----------------|---------------------\n"
            "        not-a-version  | 20260913120000 | 2026-09-13 12:00:00\n"
        )
        with self.assertRaises(MigrationAdapterError):
            self.adapter.parse_history(text)

    def test_garbage_input_raises(self):
        with self.assertRaises(MigrationAdapterError):
            self.adapter.parse_history("this is not migration list output at all")


class GetAdapterTests(unittest.TestCase):
    def test_returns_supabase_adapter(self):
        self.assertIsInstance(get_adapter("supabase"), SupabaseMigrationAdapter)

    def test_unknown_adapter_raises(self):
        with self.assertRaises(MigrationAdapterError):
            get_adapter("flyway")


if __name__ == "__main__":
    unittest.main()
