"""Four decisions about when the person's model/effort choice takes effect.

1. Obsolete project-level model/effort keys block every command that would
   start a provider turn, before any run state is written or any provider
   starts; ``fix-config`` is the way forward.
2. A stage runs with one agent configuration from its first provider turn to
   its last -- SEND_BACK cycles and resumes in a new process included -- so a
   preference changed mid-stage applies from the next stage.
3. Claude's model suggestions are engine-known, incomplete, and never a
   validation boundary.
4. A saved model id may not begin with "-".
"""

import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest.mock
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.agent_config import AgentConfigError, validate_preference
from agent_sparring.cli import _build_loop_adapters, main
from agent_sparring.model_choices import model_choices
from agent_sparring.stage import PinnedAgent, Stage, StageState
from agent_sparring.user_config import user_config_path

PROVIDERS_ONLY = (
    'project = "demo"\n\n[agents.stage]\nprovider = "claude-cli"\n\n'
    '[agents.sparring]\nprovider = "codex-cli"\n'
)
OBSOLETE = (
    'project = "demo"\n\n[agents.stage]\nprovider = "claude-cli"\nmodel = "opus"\n\n'
    '[agents.sparring]\nprovider = "codex-cli"\neffort = "medium"\n'
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _set(role: str, *flags: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--sparring-dir", "/nonexistent/.sparring", "set-config", role, *flags])
    assert code == 0, err.getvalue()


def _loop_args(**overrides) -> argparse.Namespace:
    base = dict(
        stage_provider=None,
        sparring_provider=None,
        stage_model=None,
        sparring_model=None,
        stage_effort=None,
        sparring_effort=None,
        claude_executable="claude",
        codex_executable="codex",
        permission_mode="acceptEdits",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n.sparring/intake/\n")
        # A provider executable that only records that it was started.
        self.marker = Path(self._tmp.name) / "provider-started"
        self.fake = Path(self._tmp.name) / "fake-provider"
        self.fake.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 1\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def write_config(self, text: str) -> None:
        (self.sparring_dir / "project.toml").write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "config")

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()


class ObsoleteSettingsBlockProviderTurnsTests(_Repo):
    def setUp(self):
        super().setUp()
        self.write_config(OBSOLETE)
        _git(self.repo, "switch", "-q", "-c", "feature/x")
        plans = self.repo / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "x.md").write_text("## Stage 1 — Do it\nWork.\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "plan")

    def assertRefused(self, code: int, err: str) -> None:
        self.assertEqual(code, 1, err)
        self.assertIn("[agents.stage] model = 'opus'", err)
        self.assertIn("[agents.sparring] effort = 'medium'", err)
        self.assertIn("sparring fix-config", err)
        self.assertFalse(self.marker.exists(), "no provider may start")

    def test_run_plan_refuses_before_any_run_state_or_provider(self):
        code, _, err = self.cli(
            "run-plan", str(self.repo / "docs" / "plans" / "x.md"), "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            "--claude-executable", str(self.fake), "--codex-executable", str(self.fake),
        )
        self.assertRefused(code, err)
        self.assertFalse((self.sparring_dir / "plans").exists(), "no plan-run state is written")
        self.assertFalse((self.sparring_dir / "stages").exists(), "no stage is created")

    def test_resume_plan_refuses_too(self):
        code, _, err = self.cli(
            "resume-plan", str(self.repo / "docs" / "plans" / "x.md"), "--repo-root", str(self.repo), "--expected-branch", "feature/x",
            "--claude-executable", str(self.fake), "--codex-executable", str(self.fake),
        )
        self.assertRefused(code, err)

    def test_single_stage_commands_refuse(self):
        self.assertEqual(self.cli("new-stage", "s1")[0], 0)
        for argv in (
            ("run-loop", "s1", "--repo-root", str(self.repo), "--expected-branch", "feature/x",
             "--claude-executable", str(self.fake), "--codex-executable", str(self.fake)),
            ("run-stage", "s1", "--repo-root", str(self.repo), "--expected-branch", "feature/x",
             "--claude-executable", str(self.fake)),
            ("run-sparring", "s1", "--repo-root", str(self.repo), "--expected-branch", "feature/x",
             "--codex-executable", str(self.fake)),
        ):
            with self.subTest(command=argv[0]):
                code, _, err = self.cli(*argv)
                self.assertRefused(code, err)
        self.assertIsNone(Stage.resolve(self.sparring_dir, "s1").read_state().agents, "nothing pinned")

    def test_prepare_plan_refuses(self):
        code, _, err = self.cli(
            "prepare-plan", str(self.repo / "docs" / "plans" / "x.md"), "--repo-root", str(self.repo), "--codex-executable", str(self.fake)
        )
        self.assertRefused(code, err)

    def test_configuration_commands_still_diagnose_it(self):
        code, out, _ = self.cli("show-config", "--json")
        self.assertEqual(code, 0)
        kinds = [problem["kind"] for problem in json.loads(out)["setup_problems"]]
        self.assertEqual(kinds, ["obsolete-agent-setting", "obsolete-agent-setting"])
        self.assertEqual(self.cli("check-config")[0], 1)

    def test_after_fix_config_the_refusal_is_gone(self):
        self.assertEqual(self.cli("fix-config")[0], 0)
        text = (self.sparring_dir / "project.toml").read_text()
        self.assertNotIn("model", text)
        self.assertNotIn("effort", text)
        self.assertIn('provider = "claude-cli"', text)
        self.assertEqual(self.cli("check-config")[0], 0)
        self.assertFalse(user_config_path().exists(), "the fix chooses no preference")


class StageKeepsOneConfigurationTests(_Repo):
    def setUp(self):
        super().setUp()
        self.write_config(PROVIDERS_ONLY)
        self.stage = Stage.resolve(self.sparring_dir, "s1")
        self.stage.create()

    def build(self, stage=None, **overrides):
        return _build_loop_adapters(
            _loop_args(**overrides), self.sparring_dir, self.repo, stage=stage or self.stage
        )

    def test_first_turn_pins_both_roles(self):
        _set("stage", "--model", "claude-opus-5-5", "--effort", "low")
        _set("sparring", "--model", "gpt-6-astra")
        stage_adapter, sparring_adapter = self.build()
        self.assertEqual((stage_adapter.model, stage_adapter.effort), ("claude-opus-5-5", "low"))
        pins = self.stage.read_state().agents
        self.assertEqual(
            pins["stage"],
            PinnedAgent(provider="claude-cli", model="claude-opus-5-5", model_source="user", effort="low", effort_source="user"),
        )
        self.assertEqual(pins["sparring"].model, "gpt-6-astra")
        self.assertIsNone(pins["sparring"].effort)
        self.assertEqual(pins["sparring"].effort_source, "provider-default")

    def test_a_preference_changed_mid_stage_applies_from_the_next_stage(self):
        _set("stage", "--model", "claude-opus-5-5")
        self.build()
        _set("stage", "--model", "claude-sonnet-5-5", "--effort", "high")
        # A SEND_BACK cycle or a resume-plan in a new process rebuilds the
        # adapters for the same stage: still the stage's own configuration.
        again, _ = self.build()
        self.assertEqual((again.model, again.effort), ("claude-opus-5-5", None))
        # The next stage starts with the new preference.
        nxt = Stage.resolve(self.sparring_dir, "s2")
        nxt.create()
        fresh, _ = self.build(stage=nxt)
        self.assertEqual((fresh.model, fresh.effort), ("claude-sonnet-5-5", "high"))

    def test_the_pin_survives_state_rewrites_and_a_new_process(self):
        _set("sparring", "--effort", "xhigh")
        self.build()
        state = self.stage.read_state()
        state.candidate_sha = "abc123"  # an ordinary later write of the same state
        self.stage.write_state(state)
        _set("sparring", "--effort", "low")
        _, sparring = self.build()
        self.assertEqual(sparring.effort, "xhigh")
        raw = json.loads((self.stage.directory / "state.json").read_text())
        self.assertEqual(raw["agents"]["sparring"]["effort"], "xhigh")

    def test_an_override_that_contradicts_the_pin_is_refused_not_ignored(self):
        _set("stage", "--model", "claude-opus-5-5")
        self.build()
        with self.assertRaisesRegex(AgentConfigError, "applies from the next stage"):
            self.build(stage_model="claude-haiku-4-5-20251001")
        with unittest.mock.patch.dict(os.environ, {"SPARRING_SPARRING_EFFORT": "max"}):
            with self.assertRaisesRegex(AgentConfigError, "applies from the next stage"):
                self.build()
        # The same value as the pin is not a contradiction.
        same, _ = self.build(stage_model="claude-opus-5-5")
        self.assertEqual(same.model, "claude-opus-5-5")

    def test_an_unpinned_state_file_is_unchanged_on_disk(self):
        before = (self.stage.directory / "state.json").read_bytes()
        self.assertNotIn(b"agents", before)
        self.assertEqual(StageState.from_dict(json.loads(before)).to_dict(), json.loads(before))


class ModelChoiceDecisionsTests(unittest.TestCase):
    def test_claude_suggestions_are_engine_known_incomplete_and_not_validation(self):
        choices = model_choices("stage", "claude-cli").as_dict()
        self.assertEqual(choices["source"], "engine-known")
        self.assertFalse(choices["complete"])
        self.assertTrue(choices["custom_allowed"])
        listed = {choice["model"] for choice in choices["choices"]}
        self.assertNotIn("claude-future-9", listed)
        validate_preference("stage", "claude-cli", "model", "claude-future-9")  # accepted

    def test_a_saved_model_may_not_begin_with_a_dash(self):
        for value in ("-x", "--model", " -opus"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AgentConfigError, "must not begin with '-'"):
                    validate_preference("stage", "claude-cli", "model", value)
                with self.assertRaisesRegex(AgentConfigError, "must not begin with '-'"):
                    validate_preference("sparring", "codex-cli", "model", value)

    def test_set_config_refuses_a_dash_model_before_writing(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", "/nonexistent/.sparring", "set-config", "stage", "--model=-x", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("must not begin with '-'", json.loads(out.getvalue())["error"])
        self.assertFalse(user_config_path().exists())


if __name__ == "__main__":
    unittest.main()
