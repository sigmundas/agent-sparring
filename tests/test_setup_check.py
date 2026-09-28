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
from agent_sparring.setup_check import fix_setup, setup_problems

PROJECT = 'project = "demo"\n\n[repo]\nroot = "."\n'


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
        self.assertEqual(fix_setup(self.root, self.sparring_dir), [], "a second fix writes nothing")
        self.assertEqual(self.gitignore.read_text(encoding="utf-8"), original + "\n.sparring/intake/\n")

    def test_fix_creates_the_file_with_every_line_when_there_is_none(self):
        self.assertEqual(fix_setup(self.root, self.sparring_dir), [".sparring/stages/", ".sparring/plans/", ".sparring/intake/"])
        self.assertEqual(self.gitignore.read_text(encoding="utf-8"), "# Agent Sparring workflow state\n.sparring/stages/\n.sparring/plans/\n.sparring/intake/\n")

    def test_a_rule_that_overrides_the_fix_is_an_error_not_a_success(self):
        self.gitignore.write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        (self.sparring_dir / ".gitignore").write_text("!intake/\n", encoding="utf-8")  # a nested file outranks the root one
        code, out = self.cli("fix-config", "--json")
        self.assertEqual(code, 1)
        self.assertIn("still does not ignore", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
