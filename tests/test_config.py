import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401  (adds src/ to sys.path)

from sparring_v2.config import (
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
