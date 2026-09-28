"""Per-worktree local model/effort overrides (``set-config --local``).

The point of this layer is that changing the next stage's model or effort
during a managed run must not dirty the repository -- a tracked
``project.toml`` edit stops the run at acceptance. So every write here also
asserts that ``git status`` is still empty, and that ``project.toml``'s bytes
are unchanged.
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.agent_config import SOURCE_LOCAL, SOURCE_PROJECT, resolve_agent_configs
from agent_sparring.cli import main
from agent_sparring.config import ProjectConfigError, parse_project_config
from agent_sparring.local_config import load_local_overrides, local_overrides_path

PROJECT = """\
project = "demo"

[repo]
root = "."

[agents.stage]
provider = "claude-cli"
model = "opus"
effort = "medium"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-sol"
effort = "medium"
"""


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout


class LocalOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.sparring_dir = self.root / ".sparring"
        self.sparring_dir.mkdir()
        self.config_path = self.sparring_dir / "project.toml"
        self.config_path.write_text(PROJECT, encoding="utf-8")
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
        _git(self.root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--sparring-dir", str(self.sparring_dir), *argv])
            except SystemExit as exit_code:
                code = int(exit_code.code or 0)
        return code, out.getvalue(), err.getvalue()

    def effective(self):
        config = parse_project_config(self.config_path.read_text(encoding="utf-8"), source="t")
        return resolve_agent_configs(config, local=load_local_overrides(self.sparring_dir), environ={})

    def assert_repository_clean(self) -> None:
        self.assertEqual(_git(self.root, "status", "--porcelain", "--untracked-files=all", "--ignored"), "")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), PROJECT)

    def test_the_file_lives_in_the_git_directory_not_the_worktree(self):
        path = local_overrides_path(self.sparring_dir)
        git_dir = Path(_git(self.root, "rev-parse", "--absolute-git-dir").strip())
        self.assertEqual(path, git_dir / "agent-sparring" / ".sparring.toml")

    def test_setting_a_model_locally_overrides_project_toml_and_leaves_the_repository_clean(self):
        code, _out, err = self.cli("set-config", "stage", "--model", "claude-fable-5-1", "--local")
        self.assertEqual(code, 0, err)
        effective = self.effective()
        self.assertEqual((effective.stage.model, effective.stage.model_source), ("claude-fable-5-1", SOURCE_LOCAL))
        self.assertEqual((effective.stage.effort, effective.stage.effort_source), ("medium", SOURCE_PROJECT))
        self.assertEqual(effective.sparring.model, "gpt-5.6-sol", "the other role is untouched")
        self.assert_repository_clean()

    def test_an_effort_the_provider_cannot_honour_is_refused_before_anything_is_written(self):
        code, _out, err = self.cli("set-config", "sparring", "--effort", "ludicrous", "--local")
        self.assertEqual(code, 1)
        self.assertIn("ludicrous", err)
        self.assertFalse(local_overrides_path(self.sparring_dir).exists())

    def test_the_provider_cannot_be_set_locally(self):
        code, _out, err = self.cli("set-config", "stage", "--provider", "codex-cli", "--local")
        self.assertEqual(code, 1)
        self.assertIn("provider cannot be overridden locally", err)

    def test_clearing_the_last_local_value_removes_the_file_and_project_toml_applies_again(self):
        self.assertEqual(self.cli("set-config", "sparring", "--model", "gpt-6-astra", "--effort", "high", "--local")[0], 0)
        self.assertEqual(self.effective().sparring.model, "gpt-6-astra")
        self.assertEqual(self.cli("set-config", "sparring", "--model-default", "--local")[0], 0)
        self.assertEqual((self.effective().sparring.model_source, self.effective().sparring.effort), (SOURCE_PROJECT, "high"))
        self.assertEqual(self.cli("set-config", "sparring", "--effort-default", "--local")[0], 0)
        self.assertFalse(local_overrides_path(self.sparring_dir).exists())
        self.assertEqual(self.effective().sparring.effort_source, SOURCE_PROJECT)
        self.assert_repository_clean()

    def test_show_config_json_reports_the_local_source_and_where_it_is(self):
        self.cli("set-config", "stage", "--effort", "high", "--local")
        code, out, err = self.cli("show-config", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["local_config_path"], str(local_overrides_path(self.sparring_dir)))
        self.assertTrue(payload["local_config_exists"])
        self.assertEqual((payload["stage"]["effort"], payload["stage"]["effort_source"]), ("high", "local"))

    def test_set_config_local_json_has_show_configs_shape(self):
        code, out, err = self.cli("set-config", "stage", "--model", "claude-sonnet-5", "--local", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["stage"]["model"], "claude-sonnet-5")
        code, out, _err = self.cli("set-config", "stage", "--model", "claude-sonnet-5", "--local", "--json")
        self.assertFalse(json.loads(out)["changed"], "a request the file already satisfies writes nothing")

    def test_the_environment_and_the_command_line_still_outrank_the_local_file(self):
        self.cli("set-config", "stage", "--model", "claude-haiku-4-5-20251001", "--local")
        config = parse_project_config(PROJECT, source="t")
        local = load_local_overrides(self.sparring_dir)
        env = resolve_agent_configs(config, local=local, environ={"SPARRING_STAGE_MODEL": "sonnet"})
        self.assertEqual((env.stage.model, env.stage.model_source), ("sonnet", "env"))

    def test_an_unreadable_local_file_is_a_configuration_error_not_a_silent_default(self):
        path = local_overrides_path(self.sparring_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[agents.stage]\nprovider = \"codex-cli\"\n", encoding="utf-8")
        with self.assertRaises(ProjectConfigError):
            load_local_overrides(self.sparring_dir)

    def test_outside_git_there_is_no_local_location(self):
        with tempfile.TemporaryDirectory() as plain:
            sparring = Path(plain) / ".sparring"
            sparring.mkdir()
            (sparring / "project.toml").write_text(PROJECT, encoding="utf-8")
            self.assertIsNone(local_overrides_path(sparring))
            self.assertIsNone(load_local_overrides(sparring))


if __name__ == "__main__":
    unittest.main()
