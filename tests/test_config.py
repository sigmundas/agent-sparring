import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401  (adds src/ to sys.path)

from agent_sparring.config import (
    ProjectConfigError,
    load_project_config,
    load_project_markdown,
    parse_project_config,
)

VALID_TOML = """
project = "fictitious-widgets"

[repo]
root = "."

[commands]
test = "make test"
build = "make build"

[agents.stage]
provider = "claude"

[agents.sparring]
provider = "codex"

[sparring]
default_mode = "local-auto"
"""

MINIMAL_TOML = """
project = "fictitious-widgets"
"""


class ParseProjectConfigTests(unittest.TestCase):
    def test_valid_config_loads_all_fields(self):
        config = parse_project_config(VALID_TOML)
        self.assertEqual(config.project, "fictitious-widgets")
        self.assertEqual(config.repo_root, ".")
        self.assertEqual(config.commands, {"test": "make test", "build": "make build"})
        self.assertEqual(config.stage_agent_provider, "claude")
        self.assertEqual(config.sparring_agent_provider, "codex")
        self.assertEqual(config.default_sparring_mode, "local-auto")
        self.assertEqual(config.command("test"), "make test")
        self.assertIsNone(config.command("missing"))

    def test_minimal_config_uses_defaults(self):
        config = parse_project_config(MINIMAL_TOML)
        self.assertEqual(config.project, "fictitious-widgets")
        self.assertEqual(config.repo_root, ".")
        self.assertEqual(config.commands, {})
        self.assertIsNone(config.stage_agent_provider)
        self.assertIsNone(config.sparring_agent_provider)
        self.assertIsNone(config.default_sparring_mode)

    def test_missing_project_name_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config("[repo]\nroot = \".\"\n")

    def test_malformed_toml_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config("this is not [valid toml")

    def test_wrong_type_field_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = 123\n')

    def test_non_string_command_value_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config(
                'project = "x"\n\n[commands]\ntest = 5\n'
            )

    def test_no_sporely_specific_assumptions(self):
        # The parser must not require or special-case any particular
        # project name, path, or command.
        config = parse_project_config('project = "anything-goes"\n')
        self.assertEqual(config.project, "anything-goes")

    def test_self_check_defaults_to_false_when_absent(self):
        config = parse_project_config(MINIMAL_TOML)
        self.assertFalse(config.stage_self_check)

    def test_self_check_true_is_parsed(self):
        config = parse_project_config('project = "x"\n\n[stage]\nself_check = true\n')
        self.assertTrue(config.stage_self_check)

    def test_self_check_false_is_parsed(self):
        config = parse_project_config('project = "x"\n\n[stage]\nself_check = false\n')
        self.assertFalse(config.stage_self_check)

    def test_self_check_non_boolean_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = "x"\n\n[stage]\nself_check = "yes"\n')

    def test_finish_options_default_false_and_parse_as_bools(self):
        config = parse_project_config(MINIMAL_TOML)
        self.assertFalse(config.finish_remove_plan)
        self.assertFalse(config.finish_delete_remote_branch)
        config = parse_project_config(
            'project = "x"\n\n[finish]\nremove_plan = true\ndelete_remote_branch = true\n'
        )
        self.assertTrue(config.finish_remove_plan)
        self.assertTrue(config.finish_delete_remote_branch)

    def test_finish_remove_plan_non_boolean_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = "x"\n\n[finish]\nremove_plan = "yes"\n')

    def test_finish_unknown_field_fails_and_names_remove_plan(self):
        with self.assertRaises(ProjectConfigError) as caught:
            parse_project_config('project = "x"\n\n[finish]\nremove_plans = true\n')
        self.assertIn("remove_plan", str(caught.exception))

    def test_unknown_top_level_table_is_ignored(self):
        # Older-engine tolerance: today, parse_project_config only ever looks
        # at the top-level keys it recognises (project/repo/commands/agents/
        # sparring/stage/migrations). Any other top-level table -- including
        # one from a future engine version this one has never heard of -- is
        # simply never read, not rejected. This is a load-bearing property
        # for forward compatibility across engine versions sharing one
        # project.toml, so it is pinned here rather than left implicit.
        config = parse_project_config(
            'project = "x"\n\n[totally_unknown_future_table]\nfoo = 1\nbar = "baz"\n'
        )
        self.assertEqual(config.project, "x")
        self.assertIsNone(config.migrations)


class MigrationsConfigTests(unittest.TestCase):
    VALID = (
        'project = "x"\n\n'
        "[migrations]\n"
        'adapter = "supabase"\n'
        'directory = "supabase/migrations"\n'
        'main_ref = "origin/main"\n'
    )

    def test_absent_migrations_table_is_none(self):
        # The command-level proof that an absent table changes no behaviour
        # lives in tests/test_cli_migrations.py (NoMigrationsTableTests).
        self.assertIsNone(parse_project_config(MINIMAL_TOML).migrations)

    def test_minimal_migrations_table_uses_defaults(self):
        config = parse_project_config(self.VALID)
        self.assertIsNotNone(config.migrations)
        m = config.migrations
        self.assertEqual(m.adapter, "supabase")
        self.assertEqual(m.directory, "supabase/migrations")
        self.assertEqual(m.main_ref, "origin/main")
        self.assertEqual(m.target, "production")
        self.assertIsNone(m.target_ref)
        self.assertIsNone(m.deferred_registry)
        self.assertEqual(m.max_observation_age_minutes, 60)

    def test_full_migrations_table_is_parsed(self):
        toml = self.VALID + (
            'target = "staging"\n'
            'target_ref = "origin/staging"\n'
            'deferred_registry = "supabase/deploy-exceptions.json"\n'
            "max_observation_age_minutes = 30\n"
        )
        m = parse_project_config(toml).migrations
        self.assertEqual(m.target, "staging")
        self.assertEqual(m.target_ref, "origin/staging")
        self.assertEqual(m.deferred_registry, "supabase/deploy-exceptions.json")
        self.assertEqual(m.max_observation_age_minutes, 30)

    def test_missing_required_field_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config(
                'project = "x"\n\n[migrations]\nadapter = "supabase"\ndirectory = "d"\n'
            )

    def test_unknown_adapter_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config(
                'project = "x"\n\n[migrations]\nadapter = "flyway"\ndirectory = "d"\nmain_ref = "origin/main"\n'
            )

    def test_probe_key_is_rejected_as_a_config_error(self):
        # Stage A must never run a live probe. A `probe` key is rejected
        # outright by the closed schema below, the same as any other
        # misspelled/invented field -- it is never merely ignored, and it is
        # certainly never executed.
        with self.assertRaises(ProjectConfigError) as ctx:
            parse_project_config(self.VALID + 'probe = "supabase migration list"\n')
        self.assertIn("probe", str(ctx.exception))

    def test_unknown_field_fails_closed(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config(self.VALID + 'unexpected = "value"\n')

    def test_non_positive_max_observation_age_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config(self.VALID + "max_observation_age_minutes = 0\n")

    # Finding 5: both paths are repository-relative and must stay inside it.
    def _with_paths(self, directory: str, registry: str | None = None) -> str:
        text = (
            'project = "x"\n\n[migrations]\nadapter = "supabase"\n'
            f"directory = {directory!r}\nmain_ref = \"origin/main\"\n"
        )
        if registry is not None:
            text += f"deferred_registry = {registry!r}\n"
        return text

    def test_directory_outside_the_repository_is_refused(self):
        for directory in ("/etc/migrations", "../elsewhere", "supabase/../../x", "..", "C:\\migrations"):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(ProjectConfigError, "directory"):
                    parse_project_config(self._with_paths(directory))

    def test_deferred_registry_outside_the_repository_is_refused(self):
        for registry in ("/tmp/deploy-exceptions.json", "../deploy-exceptions.json", "a/../../b.json"):
            with self.subTest(registry=registry):
                with self.assertRaisesRegex(ProjectConfigError, "deferred_registry"):
                    parse_project_config(self._with_paths("supabase/migrations", registry))

    def test_repository_relative_paths_are_accepted(self):
        config = parse_project_config(
            self._with_paths("supabase/./migrations", "supabase/x/../deploy-exceptions.json")
        )
        self.assertEqual(config.migrations.directory, "supabase/./migrations")
        self.assertEqual(config.migrations.deferred_registry, "supabase/x/../deploy-exceptions.json")

    def test_migrations_not_a_table_fails(self):
        with self.assertRaises(ProjectConfigError):
            parse_project_config('project = "x"\nmigrations = "nope"\n')


class LoadProjectConfigFilesystemTests(unittest.TestCase):
    def test_load_project_config_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            (sparring_dir / "project.toml").write_text(VALID_TOML, encoding="utf-8")
            config = load_project_config(sparring_dir)
            self.assertEqual(config.project, "fictitious-widgets")

    def test_load_missing_project_config_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProjectConfigError):
                load_project_config(Path(tmp))

    def test_load_project_markdown_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            (sparring_dir / "PROJECT.md").write_text("# Fictitious Widgets\n\nSome context.\n", encoding="utf-8")
            text = load_project_markdown(sparring_dir)
            self.assertIn("Fictitious Widgets", text)

    def test_load_project_markdown_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_project_markdown(Path(tmp)))

    def test_project_markdown_not_interpreted(self):
        # Arbitrary/adversarial-looking prose must load verbatim, unparsed.
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            weird = "---\nstatus: changes_requested\n---\n# Not real front matter\n"
            (sparring_dir / "PROJECT.md").write_text(weird, encoding="utf-8")
            text = load_project_markdown(sparring_dir)
            self.assertEqual(text, weird)


if __name__ == "__main__":
    unittest.main()
