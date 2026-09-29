"""Fixable setup problems: reported as data by show-config, repaired by fix-config."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.config import parse_project_config
from agent_sparring.setup_check import (
    KIND_OBSOLETE_AGENT_SETTING,
    fix_setup,
    obsolete_setting_problems,
    setup_problems,
)

PROJECT = 'project = "demo"\n\n[repo]\nroot = "."\n'

# A hand-written file with an obsolete model/effort key for the stage role,
# plus settings the fix must never touch: a comment, [commands], and the
# sparring role's own provider.
WITH_OBSOLETE_SETTING = """\
# Notes I keep at the top of this file.
project = "demo"

[repo]
root = "."

[commands]
test = "pytest -q"   # trailing comment

[agents.stage]
provider = "claude-cli"
model = "opus"

[agents.sparring]
provider = "codex-cli"
"""


class SetupCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.sparring_dir = self.root / ".sparring"
        self.sparring_dir.mkdir(parents=True)
        (self.sparring_dir / "project.toml").write_text(PROJECT, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        self.gitignore = self.root / ".gitignore"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue()

    def test_only_the_directory_git_can_see_is_a_problem(self):
        self.gitignore.write_text("node_modules/\n.sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        problems = setup_problems(self.root, self.sparring_dir)
        self.assertEqual([(p.kind, p.what, p.ignore_line) for p in problems], [("not-ignored", "plan intake", ".sparring/intake/")])
        self.assertIn("plan intake artifacts under .sparring/intake/ are not ignored by git", problems[0].message)

    def test_show_config_json_reports_setup_problems(self):
        self.gitignore.write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        code, out = self.cli("show-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([p["ignore_line"] for p in payload["setup_problems"]], [".sparring/intake/"])
        self.assertEqual(payload["setup_problems"][0]["gitignore"], str(self.root / ".gitignore"))

    def test_a_correct_setup_reports_an_empty_list(self):
        self.gitignore.write_text(".sparring/stages/\n.sparring/plans/\n.sparring/intake/\n", encoding="utf-8")
        self.assertEqual(json.loads(self.cli("show-config", "--json")[1])["setup_problems"], [])

    def test_fix_appends_only_the_missing_lines_and_keeps_the_rest_byte_for_byte(self):
        original = "node_modules/\n.sparring/stages/\n.sparring/plans/"  # no trailing newline
        self.gitignore.write_text(original, encoding="utf-8")
        code, out = self.cli("fix-config", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["added"], [".sparring/intake/"])
        self.assertEqual(self.gitignore.read_text(encoding="utf-8"), original + "\n.sparring/intake/\n")
        self.assertEqual(setup_problems(self.root, self.sparring_dir), [])
        second = fix_setup(self.root, self.sparring_dir)
        self.assertEqual(second.added, [], "a second fix writes nothing")
        self.assertEqual(second.removed, [])
        self.assertEqual(self.gitignore.read_text(encoding="utf-8"), original + "\n.sparring/intake/\n")

    def test_fix_creates_the_file_with_every_line_when_there_is_none(self):
        fix = fix_setup(self.root, self.sparring_dir)
        self.assertEqual(fix.added, [".sparring/stages/", ".sparring/plans/", ".sparring/intake/"])
        self.assertEqual(self.gitignore.read_text(encoding="utf-8"), "# Agent Sparring workflow state\n.sparring/stages/\n.sparring/plans/\n.sparring/intake/\n")


class ObsoleteAgentSettingTests(unittest.TestCase):
    """(6)-(8): project-level model/effort keys are diagnosed, never used for
    resolution, and removed surgically by fix-config."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.sparring_dir = self.root / ".sparring"
        self.sparring_dir.mkdir(parents=True)
        (self.sparring_dir / "project.toml").write_text(WITH_OBSOLETE_SETTING, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        self.gitignore = self.root / ".gitignore"
        self.gitignore.write_text(".sparring/stages/\n.sparring/plans/\n.sparring/intake/\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue()

    def test_the_obsolete_key_is_reported_as_a_setup_problem(self):
        problems = obsolete_setting_problems(self.sparring_dir)
        self.assertEqual(len(problems), 1)
        problem = problems[0]
        self.assertEqual(problem.kind, KIND_OBSOLETE_AGENT_SETTING)
        self.assertEqual(problem.setting.role, "stage")
        self.assertEqual(problem.setting.field, "model")
        self.assertEqual(problem.setting.value, "opus")
        self.assertIn("[agents.stage] model", problem.message)

    def test_it_is_never_used_by_resolution(self):
        config = parse_project_config(
            (self.sparring_dir / "project.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(config.obsolete_agent_settings[0].value, "opus")
        # Nothing on ProjectConfig exposes it as an active model/effort field.
        self.assertFalse(hasattr(config, "stage_agent_model"))
        self.assertFalse(hasattr(config, "stage_agent_effort"))

    def test_check_config_exits_nonzero_and_names_fix_config(self):
        code, out = self.cli("check-config", "--repo-root", str(self.root))
        self.assertEqual(code, 1)

    def test_fix_config_removes_only_the_obsolete_key(self):
        code, out = self.cli("fix-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["removed"], [{"role": "stage", "field": "model", "value": "opus"}])
        text = (self.sparring_dir / "project.toml").read_text(encoding="utf-8")
        self.assertIn("# Notes I keep at the top of this file.", text)
        self.assertIn('test = "pytest -q"   # trailing comment', text)
        self.assertIn('provider = "claude-cli"', text)
        self.assertIn('provider = "codex-cli"', text)
        self.assertNotIn("model", text)
        config = parse_project_config(text)
        self.assertEqual(config.obsolete_agent_settings, ())
        self.assertEqual(config.stage_agent_provider, "claude-cli")

    def test_fix_config_is_idempotent(self):
        self.cli("fix-config", "--json")
        after_first = (self.sparring_dir / "project.toml").read_text(encoding="utf-8")
        mtime = (self.sparring_dir / "project.toml").stat().st_mtime_ns
        code, out = self.cli("fix-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["removed"], [])
        self.assertEqual(payload["added"], [])
        self.assertEqual((self.sparring_dir / "project.toml").read_text(encoding="utf-8"), after_first)
        self.assertEqual((self.sparring_dir / "project.toml").stat().st_mtime_ns, mtime)

    def test_a_rule_that_overrides_the_fix_is_an_error_not_a_success(self):
        self.gitignore.write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        (self.sparring_dir / ".gitignore").write_text("!intake/\n", encoding="utf-8")  # a nested file outranks the root one
        code, out = self.cli("fix-config", "--json")
        self.assertEqual(code, 1)
        self.assertIn("still does not ignore", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
