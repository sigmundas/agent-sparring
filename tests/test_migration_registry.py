import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.migration_registry import (
    DeferredRegistryError,
    load_deferred_registry,
    parse_deferred_registry,
    sha256_hex,
)

# Mirrors the real sporely-web supabase/deploy-exceptions.json shape,
# including the extra fields (productionProjectRef, doc) this module does
# not read.
REAL_SHAPE = """
{
  "productionProjectRef": "zkpjklzfwzefhjluvhfw",
  "deferredMigrations": [
    {
      "version": "20260914090000",
      "file": "20260914090000_extend_reference_snapshots_to_version_2.sql",
      "sha256": "01972eb65f1660813f1568aa704ef5de158fad329f03ad9f1796cf85deaaf94c",
      "reason": "Snapshot v2 rollout gates unmet.",
      "doc": "docs/deployments/2026-09-25-migration-order-exception.md"
    }
  ]
}
"""


class ParseDeferredRegistryTests(unittest.TestCase):
    def test_matches_real_world_shape_and_ignores_extra_fields(self):
        registry = parse_deferred_registry(REAL_SHAPE, source="deploy-exceptions.json")
        self.assertEqual(len(registry.entries), 1)
        entry = registry.entries[0]
        self.assertEqual(entry.version, "20260914090000")
        self.assertEqual(entry.file, "20260914090000_extend_reference_snapshots_to_version_2.sql")
        self.assertEqual(
            entry.sha256, "01972eb65f1660813f1568aa704ef5de158fad329f03ad9f1796cf85deaaf94c"
        )
        self.assertIn("rollout gates", entry.reason)

    def test_by_version_lookup(self):
        registry = parse_deferred_registry(REAL_SHAPE)
        by_version = registry.by_version()
        self.assertIn("20260914090000", by_version)

    def test_empty_deferred_list_is_fine(self):
        registry = parse_deferred_registry('{"deferredMigrations": []}')
        self.assertEqual(registry.entries, ())

    def test_missing_deferred_key_is_fine(self):
        registry = parse_deferred_registry('{"productionProjectRef": "x"}')
        self.assertEqual(registry.entries, ())

    def test_not_json_fails(self):
        with self.assertRaises(DeferredRegistryError):
            parse_deferred_registry("not json")

    def test_not_an_object_fails(self):
        with self.assertRaises(DeferredRegistryError):
            parse_deferred_registry("[]")

    def test_missing_required_field_fails(self):
        with self.assertRaises(DeferredRegistryError):
            parse_deferred_registry(
                '{"deferredMigrations": [{"version": "20260914090000", "file": "f.sql"}]}'
            )

    def test_duplicate_version_fails(self):
        text = (
            '{"deferredMigrations": ['
            '{"version": "1", "file": "a.sql", "sha256": "aa", "reason": "r"},'
            '{"version": "1", "file": "b.sql", "sha256": "bb", "reason": "r"}'
            "]}"
        )
        with self.assertRaises(DeferredRegistryError):
            parse_deferred_registry(text)


class LoadDeferredRegistryTests(unittest.TestCase):
    def test_reads_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy-exceptions.json"
            path.write_text(REAL_SHAPE, encoding="utf-8")
            registry = load_deferred_registry(path)
            self.assertEqual(len(registry.entries), 1)

    def test_missing_file_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DeferredRegistryError):
                load_deferred_registry(Path(tmp) / "missing.json")


class Sha256HexTests(unittest.TestCase):
    def test_matches_hashlib(self):
        import hashlib

        content = b"some migration file content\n"
        self.assertEqual(sha256_hex(content), hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
