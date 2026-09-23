"""The environment layer of provider/model/effort resolution.

``project.toml`` is a tracked file in the consuming repository, so changing a
model there dirties the working tree and finalization treats the edit as
candidate content. The environment layer exists so a person can try a
different model for one run without staging and committing a configuration
change. These tests pin the three things that makes it safe to rely on:
where it sits in the precedence chain, that it is validated exactly as the
file is, and that every orchestration path sees it -- because it is applied
inside the single resolver they all already go through.
"""

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.activity import ActivityLog
from agent_sparring.cli import _build_loop_adapters
from agent_sparring.stage import Stage
from agent_sparring.usage import collect_stage_usage

from agent_sparring.agent_config import (
    ROLE_SPARRING,
    ROLE_STAGE,
    SOURCE_CLI,
    SOURCE_ENGINE_DEFAULT,
    SOURCE_ENV,
    SOURCE_PROJECT,
    SOURCE_PROVIDER_DEFAULT,
    AgentConfigError,
    RoleOverrides,
    env_var,
    read_role_env,
    resolve_agent_configs,
    resolve_role_config,
)
from agent_sparring.config import parse_project_config

CONFIGURED = parse_project_config(
    """
project = "demo"

[agents.stage]
provider = "claude-cli"
model = "opus"
effort = "medium"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-sol"
effort = "medium"
"""
)


class EnvVarNamesTest(unittest.TestCase):
    def test_names_are_mechanically_derived(self):
        self.assertEqual(env_var(ROLE_STAGE, "model"), "SPARRING_STAGE_MODEL")
        self.assertEqual(env_var(ROLE_SPARRING, "model"), "SPARRING_SPARRING_MODEL")
        self.assertEqual(env_var(ROLE_SPARRING, "effort"), "SPARRING_SPARRING_EFFORT")
        self.assertEqual(env_var(ROLE_STAGE, "provider"), "SPARRING_STAGE_PROVIDER")

    def test_unrecognised_sparring_variable_is_inert(self):
        # A near-miss name must do nothing at all rather than half-apply.
        resolved = resolve_role_config(
            ROLE_SPARRING,
            CONFIGURED,
            environ={"SPARRING_SPARRING_MODLE": "gpt-6-astra"},
        )
        self.assertEqual(resolved.model, "gpt-5.6-sol")
        self.assertEqual(resolved.model_source, SOURCE_PROJECT)


class PrecedenceTest(unittest.TestCase):
    def test_env_overrides_project(self):
        resolved = resolve_role_config(
            ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_MODEL": "gpt-6-astra"}
        )
        self.assertEqual(resolved.model, "gpt-6-astra")
        self.assertEqual(resolved.model_source, SOURCE_ENV)

    def test_cli_overrides_env(self):
        resolved = resolve_role_config(
            ROLE_SPARRING,
            CONFIGURED,
            RoleOverrides(model="from-flag"),
            environ={"SPARRING_SPARRING_MODEL": "gpt-6-astra"},
        )
        self.assertEqual(resolved.model, "from-flag")
        self.assertEqual(resolved.model_source, SOURCE_CLI)

    def test_fields_are_independent(self):
        # Setting the model must not disturb the effort the project configured.
        resolved = resolve_role_config(
            ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_MODEL": "gpt-6-astra"}
        )
        self.assertEqual((resolved.effort, resolved.effort_source), ("medium", SOURCE_PROJECT))
        self.assertEqual(
            (resolved.provider, resolved.provider_source), ("codex-cli", SOURCE_PROJECT)
        )

    def test_roles_are_independent(self):
        effective = resolve_agent_configs(
            CONFIGURED, environ={"SPARRING_SPARRING_MODEL": "gpt-6-astra"}
        )
        self.assertEqual(effective.sparring.model, "gpt-6-astra")
        self.assertEqual(effective.stage.model, "opus")
        self.assertEqual(effective.stage.model_source, SOURCE_PROJECT)

    def test_env_applies_without_any_project_file(self):
        resolved = resolve_role_config(
            ROLE_SPARRING, None, environ={"SPARRING_SPARRING_MODEL": "gpt-6-astra"}
        )
        self.assertEqual(resolved.model, "gpt-6-astra")
        self.assertEqual(resolved.model_source, SOURCE_ENV)
        # The provider still falls back to the engine default, not to the
        # model's layer.
        self.assertEqual(resolved.provider_source, SOURCE_ENGINE_DEFAULT)

    def test_absent_env_leaves_provider_default_intact(self):
        resolved = resolve_role_config(ROLE_SPARRING, parse_project_config('project = "d"'))
        self.assertIsNone(resolved.model)
        self.assertEqual(resolved.model_source, SOURCE_PROVIDER_DEFAULT)

    def test_env_can_select_the_provider_and_its_effort_together(self):
        resolved = resolve_role_config(
            ROLE_STAGE,
            CONFIGURED,
            environ={
                "SPARRING_STAGE_PROVIDER": "claude-cli",
                "SPARRING_STAGE_EFFORT": "high",
            },
        )
        self.assertEqual(resolved.provider_source, SOURCE_ENV)
        self.assertEqual((resolved.effort, resolved.effort_source), ("high", SOURCE_ENV))


class ValidationTest(unittest.TestCase):
    def test_set_but_empty_is_refused_rather_than_ignored(self):
        with self.assertRaises(AgentConfigError) as caught:
            resolve_role_config(
                ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_MODEL": "   "}
            )
        message = str(caught.exception)
        self.assertIn("SPARRING_SPARRING_MODEL", message)
        self.assertIn("unset it", message)

    def test_value_is_stripped(self):
        resolved = resolve_role_config(
            ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_MODEL": " gpt-6-astra\n"}
        )
        self.assertEqual(resolved.model, "gpt-6-astra")

    def test_unsupported_effort_names_the_variable_that_set_it(self):
        with self.assertRaises(AgentConfigError) as caught:
            resolve_role_config(
                ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_EFFORT": "turbo"}
            )
        message = str(caught.exception)
        self.assertIn("SPARRING_SPARRING_EFFORT", message)
        # And still says what the provider does accept.
        self.assertIn("supported levels", message)

    def test_unknown_provider_from_env_is_a_configuration_error(self):
        with self.assertRaises(AgentConfigError):
            resolve_role_config(
                ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_PROVIDER": "nope-cli"}
            )

    def test_provider_wrong_for_the_role_is_refused(self):
        with self.assertRaises(AgentConfigError):
            resolve_role_config(
                ROLE_SPARRING, CONFIGURED, environ={"SPARRING_SPARRING_PROVIDER": "claude-cli"}
            )

    def test_read_role_env_reports_only_its_own_role(self):
        overrides = read_role_env(
            ROLE_STAGE,
            {"SPARRING_SPARRING_MODEL": "gpt-6-astra", "SPARRING_STAGE_EFFORT": "high"},
        )
        self.assertEqual(overrides, RoleOverrides(provider=None, model=None, effort="high"))


class ProcessEnvironmentTest(unittest.TestCase):
    """The default source is the real process environment.

    Everything above passes an explicit mapping so it cannot be perturbed by
    the developer's own shell; this one test pins that omitting the argument
    reads ``os.environ``, which is the behaviour every CLI command relies on.
    """

    def test_os_environ_is_the_default_source(self):
        with mock.patch.dict(os.environ, {"SPARRING_SPARRING_MODEL": "gpt-6-astra"}):
            resolved = resolve_role_config(ROLE_SPARRING, CONFIGURED)
        self.assertEqual(resolved.model, "gpt-6-astra")
        self.assertEqual(resolved.model_source, SOURCE_ENV)

    def test_a_clean_environment_falls_back_to_the_project(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            resolved = resolve_role_config(ROLE_SPARRING, CONFIGURED)
        self.assertEqual(resolved.model, "gpt-5.6-sol")
        self.assertEqual(resolved.model_source, SOURCE_PROJECT)


class AdapterAndTelemetryTest(unittest.TestCase):
    """The end an operator actually cares about.

    An environment override is only useful if it reaches the provider
    process, and it is only safe if the run says so afterwards -- unlike a
    ``project.toml`` edit, it leaves no trace in the repository, so the
    stage's own activity log is the only record that it happened.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.sparring_dir = self.repo / ".sparring"
        self.stage_dir = self.sparring_dir / "stages" / "demo"
        self.stage_dir.mkdir(parents=True)
        (self.sparring_dir / "project.toml").write_text(
            """
project = "demo"

[agents.stage]
provider = "claude-cli"
model = "opus"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-sol"
""",
            encoding="utf-8",
        )

    def _args(self):
        return argparse.Namespace(
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

    def test_env_model_reaches_the_adapter(self):
        with mock.patch.dict(os.environ, {"SPARRING_SPARRING_MODEL": "gpt-6-astra"}):
            _stage_adapter, sparring_adapter = _build_loop_adapters(
                self._args(), self.sparring_dir, self.repo
            )
        self.assertEqual(sparring_adapter.model, "gpt-6-astra")

    def test_the_stage_log_records_the_override_and_its_source(self):
        log = ActivityLog(path=self.stage_dir / "activity.jsonl")
        with mock.patch.dict(os.environ, {"SPARRING_SPARRING_MODEL": "gpt-6-astra"}):
            _build_loop_adapters(
                self._args(), self.sparring_dir, self.repo, activity_log=log
            )

        usage = collect_stage_usage(Stage.resolve(self.sparring_dir, "demo"))
        sparring = usage.roles[ROLE_SPARRING]
        self.assertEqual(sparring.requested_model, "gpt-6-astra")
        self.assertEqual(sparring.model_source, SOURCE_ENV)
        # The role that was not overridden still records where its value
        # came from, so the log distinguishes the two.
        stage = usage.roles[ROLE_STAGE]
        self.assertEqual(stage.requested_model, "opus")
        self.assertEqual(stage.model_source, SOURCE_PROJECT)

    def test_a_provider_default_is_recorded_as_such_not_as_missing(self):
        log = ActivityLog(path=self.stage_dir / "activity.jsonl")
        _build_loop_adapters(self._args(), self.sparring_dir, self.repo, activity_log=log)
        sparring = collect_stage_usage(
            Stage.resolve(self.sparring_dir, "demo")
        ).roles[ROLE_SPARRING]
        self.assertTrue(sparring.resolved_seen)
        self.assertIsNone(sparring.requested_effort)
        self.assertEqual(sparring.effort_source, SOURCE_PROVIDER_DEFAULT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
