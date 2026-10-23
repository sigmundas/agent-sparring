"""Provider/model/effort configuration: resolution, argv and introspection.

The point of these tests is that there is exactly ONE resolution path. They
therefore check the resolver directly, check the argv each adapter builds
from a resolved value, and then check that the orchestration commands
(run-stage, run-sparring, run-loop, run-plan, resume-plan and the
independent-review stage) all end up with what that resolver produced --
rather than trusting that each command remembered to consult project.toml or
the user's preferences.

Provider is a project decision (``project.toml``); model and effort are the
person's own preference, shared by every project and keyed by role and
provider (:mod:`agent_sparring.user_config`). project.toml may still carry
old ``model``/``effort`` keys under ``[agents.<role>]`` -- they are parsed
only as obsolete settings (see ``tests/test_setup_check.py`` for their
diagnosis and removal) and never resolved.
"""

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.agent_config import (
    AgentConfigError,
    ROLE_SPARRING,
    ROLE_STAGE,
    RoleOverrides,
    resolve_agent_configs,
    resolve_role_config,
)
from agent_sparring.cli import _build_loop_adapters, main
from agent_sparring.config import ProjectConfigError, parse_project_config
from agent_sparring.providers import ProviderError
from agent_sparring.providers.claude_cli import ClaudeCliAdapter
from agent_sparring.providers.codex_cli import CodexCliAdapter
from agent_sparring.stage import StageMode
from agent_sparring.user_config import RolePreference, UserPreferences, update_user_preferences, user_config_path
from test_independent_review import REVIEW_STAGE, _ReviewRepoTestCase
from test_plan import (  # the shared scripted adapters, verdicts and repo fixture
    NEEDS_YOU,
    READY,
    _PlanRepoTestCase,
    _run_git,
    _SparringAdapter,
    _StageAdapter,
)

LEGACY_CONFIG = """\
project = "demo"

[agents.stage]
provider = "claude-cli"

[agents.sparring]
provider = "codex-cli"
"""

# Still legal to have around (a file written before model/effort became a
# user preference): parsed as obsolete settings, never resolved.
FULL_CONFIG = """\
project = "demo"

[agents.stage]
provider = "claude-cli"
model = "opus"
effort = "high"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-terra"
effort = "xhigh"
"""

# The same model/effort FULL_CONFIG used to carry as project settings, as a
# resolver-level user preference object -- for tests of resolve_role_config
# and resolve_agent_configs directly (no disk, no CLI).
PREFERRED = UserPreferences(
    path=Path("unused"),
    entries={
        (ROLE_STAGE, "claude-cli"): RolePreference(model="opus", effort="high"),
        (ROLE_SPARRING, "codex-cli"): RolePreference(model="gpt-5.6-terra", effort="xhigh"),
    },
)


def _write_preferences(
    *, stage_model=None, stage_effort=None, sparring_model=None, sparring_effort=None
) -> None:
    """Write this test process's isolated user preference file (see
    ``tests/conftest.py``) for the roles' only implemented provider.
    ``project.toml`` no longer carries model/effort at all."""

    def edit(current: UserPreferences) -> UserPreferences:
        if stage_model is not None:
            current = current.with_edit(ROLE_STAGE, "claude-cli", "model", stage_model)
        if stage_effort is not None:
            current = current.with_edit(ROLE_STAGE, "claude-cli", "effort", stage_effort)
        if sparring_model is not None:
            current = current.with_edit(ROLE_SPARRING, "codex-cli", "model", sparring_model)
        if sparring_effort is not None:
            current = current.with_edit(ROLE_SPARRING, "codex-cli", "effort", sparring_effort)
        return current

    update_user_preferences(user_config_path(), edit)


def _fake_completed(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class ProjectConfigParsingTests(unittest.TestCase):
    def test_legacy_provider_only_config_still_parses_and_leaves_the_rest_unset(self):
        config = parse_project_config(LEGACY_CONFIG)
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        # Absent is absent: no obsolete setting is invented when there is none.
        self.assertEqual(config.obsolete_agent_settings, ())

    def test_model_and_effort_are_read_as_obsolete_settings_never_active_fields(self):
        # project.toml no longer carries execution model/effort: these keys
        # are parsed only so 'sparring fix-config' can remove them, never so
        # a run can resolve them.
        config = parse_project_config(FULL_CONFIG)
        by_role_field = {(s.role, s.field): s.value for s in config.obsolete_agent_settings}
        self.assertEqual(
            by_role_field,
            {
                ("stage", "model"): "opus",
                ("stage", "effort"): "high",
                ("sparring", "model"): "gpt-5.6-terra",
                ("sparring", "effort"): "xhigh",
            },
        )

    def test_a_misspelled_field_is_reported_rather_than_silently_ignored(self):
        # Silently ignoring "efort" would buy a full-price turn at an effort
        # nobody chose, which is the whole failure this feature avoids.
        with self.assertRaises(ProjectConfigError) as ctx:
            parse_project_config('project = "d"\n\n[agents.stage]\nefort = "high"\n')
        message = str(ctx.exception)
        self.assertIn("'efort'", message)
        self.assertIn("effort, model, provider", message)

    def test_invalid_toml_says_so_with_the_source_name(self):
        with self.assertRaises(ProjectConfigError) as ctx:
            parse_project_config("project = \n", source=".sparring/project.toml")
        self.assertIn(".sparring/project.toml", str(ctx.exception))
        self.assertIn("not valid TOML", str(ctx.exception))


class ResolutionTests(unittest.TestCase):
    def test_no_config_at_all_falls_back_to_the_engine_default_providers(self):
        effective = resolve_agent_configs(None)
        self.assertEqual(effective.stage.provider, "claude-cli")
        self.assertEqual(effective.sparring.provider, "codex-cli")
        self.assertEqual(effective.stage.provider_source, "engine-default")

    def test_omitted_model_and_effort_mean_provider_default(self):
        effective = resolve_agent_configs(parse_project_config(LEGACY_CONFIG))
        for resolved in (effective.stage, effective.sparring):
            self.assertIsNone(resolved.model)
            self.assertIsNone(resolved.effort)
            self.assertEqual(resolved.model_source, "provider-default")
            self.assertEqual(resolved.effort_source, "provider-default")

    def test_user_preference_values_are_used_and_reported_as_coming_from_the_user(self):
        effective = resolve_agent_configs(parse_project_config(LEGACY_CONFIG), preferences=PREFERRED)
        self.assertEqual(effective.stage.model, "opus")
        self.assertEqual(effective.stage.effort, "high")
        self.assertEqual(effective.stage.model_source, "user")
        self.assertEqual(effective.stage.effort_source, "user")
        self.assertEqual(effective.sparring.model, "gpt-5.6-terra")
        self.assertEqual(effective.sparring.effort, "xhigh")

    def test_a_cli_override_beats_the_user_preference(self):
        effective = resolve_agent_configs(
            parse_project_config(LEGACY_CONFIG),
            stage=RoleOverrides(model="sonnet", effort="low"),
            preferences=PREFERRED,
        )
        self.assertEqual(effective.stage.model, "sonnet")
        self.assertEqual(effective.stage.model_source, "cli")
        self.assertEqual(effective.stage.effort, "low")
        self.assertEqual(effective.stage.effort_source, "cli")
        # ... and does not leak into the other role.
        self.assertEqual(effective.sparring.model, "gpt-5.6-terra")
        self.assertEqual(effective.sparring.effort, "xhigh")

    def test_the_two_roles_are_independent(self):
        preferences = UserPreferences(
            path=Path("unused"),
            entries={
                (ROLE_STAGE, "claude-cli"): RolePreference(model="opus"),
                (ROLE_SPARRING, "codex-cli"): RolePreference(effort="max"),
            },
        )
        effective = resolve_agent_configs(parse_project_config(LEGACY_CONFIG), preferences=preferences)
        self.assertEqual(effective.stage.model, "opus")
        self.assertIsNone(effective.stage.effort)
        self.assertIsNone(effective.sparring.model)
        self.assertEqual(effective.sparring.effort, "max")

    def test_an_effort_the_provider_does_not_accept_is_a_config_error(self):
        # "ultra" is a real Codex level and not a Claude one; the engine
        # must not quietly translate it into the nearest Claude level.
        preferences = UserPreferences(
            path=Path("unused"), entries={(ROLE_STAGE, "claude-cli"): RolePreference(effort="ultra")}
        )
        config = parse_project_config('project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\n')
        with self.assertRaises(AgentConfigError) as ctx:
            resolve_role_config(ROLE_STAGE, config, preferences=preferences)
        message = str(ctx.exception)
        self.assertIn("your stage agent effort preference for claude-cli", message)
        self.assertIn("'ultra'", message)
        self.assertIn("claude-cli", message)
        self.assertIn("low, medium, high, xhigh, max", message)

    def test_the_same_level_is_accepted_for_the_provider_that_has_it(self):
        preferences = UserPreferences(
            path=Path("unused"), entries={(ROLE_SPARRING, "codex-cli"): RolePreference(effort="ultra")}
        )
        config = parse_project_config('project = "d"\n\n[agents.sparring]\nprovider = "codex-cli"\n')
        self.assertEqual(
            resolve_role_config(ROLE_SPARRING, config, preferences=preferences).effort, "ultra"
        )

    def test_effort_is_validated_against_the_effective_provider_not_the_configured_one(self):
        # A CLI override can switch the provider mid-resolution; the effort
        # -- however it arrived -- must be judged against the provider that
        # will actually run, not the one project.toml names.
        config = parse_project_config('project = "d"\n\n[agents.sparring]\nprovider = "codex-cli"\n')
        with self.assertRaises(AgentConfigError):
            resolve_role_config(
                ROLE_SPARRING, config, RoleOverrides(provider="claude-cli", effort="ultra")
            )

    def test_a_preference_saved_for_one_provider_is_never_applied_after_a_provider_switch(self):
        # The whole point of keying preferences by (role, provider): moving
        # the sparring role to a hypothetical provider must not inherit
        # codex-cli's own stored effort.
        config = parse_project_config('project = "d"\n\n[agents.sparring]\nprovider = "codex-cli"\n')
        resolved = resolve_role_config(
            ROLE_SPARRING, config, RoleOverrides(provider="codex-cli"), preferences=PREFERRED
        )
        self.assertEqual(resolved.effort, "xhigh")
        self.assertEqual(resolved.effort_source, "user")

    def test_a_provider_that_cannot_serve_the_role_is_refused(self):
        with self.assertRaises(AgentConfigError) as ctx:
            resolve_role_config(ROLE_STAGE, None, RoleOverrides(provider="codex-cli"))
        self.assertIn("unsupported stage agent provider 'codex-cli'", str(ctx.exception))

    def test_an_unknown_provider_lists_the_known_ones(self):
        with self.assertRaises(AgentConfigError) as ctx:
            resolve_role_config(ROLE_STAGE, None, RoleOverrides(provider="gpt-cli"))
        self.assertIn("unknown provider 'gpt-cli'", str(ctx.exception))
        self.assertIn("claude-cli", str(ctx.exception))


class ClaudeArgvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def _argv(self, **kwargs) -> list[str]:
        captured: dict[str, list[str]] = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            return _fake_completed('{"session_id": "s", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, **kwargs)
        adapter.start("hello")
        return captured["args"]

    def test_nothing_configured_passes_no_model_and_no_effort_flag(self):
        argv = self._argv()
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_a_resolved_effort_becomes_the_exact_claude_flag_pair(self):
        argv = self._argv(model="opus", effort="xhigh")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_resume_carries_the_same_model_and_effort_as_a_fresh_turn(self):
        captured: dict[str, list[str]] = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            return _fake_completed('{"session_id": "s-1", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root, runner=runner, model="opus", effort="high"
        )
        adapter.resume("s-1", "again")
        argv = captured["args"]
        self.assertEqual(argv[argv.index("--resume") + 1], "s-1")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_an_unsupported_effort_is_refused_before_any_process_starts(self):
        def runner(args, cwd, timeout_seconds, on_line=None):  # pragma: no cover
            raise AssertionError("the provider must not be launched")

        with self.assertRaises(ProviderError) as ctx:
            ClaudeCliAdapter(repo_root=self.repo_root, runner=runner, effort="ultra")
        self.assertIn("claude-cli", str(ctx.exception))
        self.assertIn("low, medium, high, xhigh, max", str(ctx.exception))


class CodexArgvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def _argv(self, **kwargs) -> list[str]:
        captured: dict[str, list[str]] = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            output = Path(args[args.index("-o") + 1])
            output.write_text('{"action": "READY"}', encoding="utf-8")
            return _fake_completed('{"type": "thread.started", "thread_id": "t-1"}\n')

        adapter = CodexCliAdapter(repo_root=self.repo_root, runner=runner, **kwargs)
        adapter.start("hello")
        return captured["args"]

    def test_nothing_configured_passes_no_model_and_no_effort_override(self):
        argv = self._argv()
        self.assertNotIn("--model", argv)
        self.assertNotIn("-m", argv)
        self.assertFalse([a for a in argv if a.startswith("model_reasoning_effort")])

    def test_a_resolved_effort_becomes_the_exact_codex_config_override(self):
        argv = self._argv(model="gpt-5.6-terra", effort="high")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', argv)
        # It is a "-c" override, not a flag Codex does not have.
        self.assertEqual(argv[argv.index('model_reasoning_effort="high"') - 1], "-c")
        self.assertNotIn("--effort", argv)

    def test_the_read_only_sandbox_override_is_still_present_alongside_it(self):
        argv = self._argv(effort="max")
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertIn('model_reasoning_effort="max"', argv)

    def test_resume_carries_the_effort_override_too(self):
        captured: dict[str, list[str]] = {}

        def runner(args, cwd, timeout_seconds, on_line=None):
            captured["args"] = args
            Path(args[args.index("-o") + 1]).write_text('{"action": "READY"}', encoding="utf-8")
            return _fake_completed('{"type": "thread.started", "thread_id": "t-1"}\n')

        CodexCliAdapter(repo_root=self.repo_root, runner=runner, effort="low").resume(
            "t-1", "again"
        )
        argv = captured["args"]
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "t-1"])
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertIn('sandbox_mode="read-only"', argv)

    def test_an_unsupported_effort_is_refused_before_any_process_starts(self):
        def runner(args, cwd, timeout_seconds, on_line=None):  # pragma: no cover
            raise AssertionError("the provider must not be launched")

        with self.assertRaises(ProviderError) as ctx:
            CodexCliAdapter(repo_root=self.repo_root, runner=runner, effort="turbo")
        self.assertIn("codex-cli", str(ctx.exception))


class LoopAdapterFactoryTests(unittest.TestCase):
    """The factory run-loop, run-plan, resume-plan and the independent-review
    stage all build their adapters from."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()

    def _args(self, **overrides) -> argparse.Namespace:
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

    def _write(self, content: str) -> None:
        (self.sparring_dir / "project.toml").write_text(content, encoding="utf-8")

    def test_project_config_reaches_both_adapters_on_the_right_role(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )
        stage_adapter, sparring_adapter = _build_loop_adapters(
            self._args(), self.sparring_dir, self.repo
        )
        self.assertEqual(stage_adapter.model, "opus")
        self.assertEqual(stage_adapter.effort, "high")
        self.assertEqual(sparring_adapter.model, "gpt-5.6-terra")
        self.assertEqual(sparring_adapter.effort, "xhigh")

    def test_cli_overrides_beat_the_user_preference_per_role(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )
        stage_adapter, sparring_adapter = _build_loop_adapters(
            self._args(stage_model="sonnet", sparring_effort="minimal"),
            self.sparring_dir,
            self.repo,
        )
        self.assertEqual(stage_adapter.model, "sonnet")
        self.assertEqual(stage_adapter.effort, "high", "untouched by a model override")
        self.assertEqual(sparring_adapter.effort, "minimal")
        self.assertEqual(sparring_adapter.model, "gpt-5.6-terra")

    def test_a_legacy_provider_only_project_gets_provider_defaults(self):
        self._write(LEGACY_CONFIG)
        stage_adapter, sparring_adapter = _build_loop_adapters(
            self._args(), self.sparring_dir, self.repo
        )
        for adapter in (stage_adapter, sparring_adapter):
            self.assertIsNone(adapter.model)
            self.assertIsNone(adapter.effort)

    def test_an_unsupported_effort_stops_the_run_before_an_adapter_exists(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(stage_effort="ultra")
        with self.assertRaises(AgentConfigError):
            _build_loop_adapters(self._args(), self.sparring_dir, self.repo)

    def test_the_factory_re_reads_the_file_so_an_edit_affects_the_next_stage_only(self):
        # Live-run semantics: nothing caches the resolved configuration for
        # the lifetime of a run, and nothing reaches into an adapter that
        # already exists.
        self._write(LEGACY_CONFIG)
        _write_preferences(stage_model="opus")
        first, _ = _build_loop_adapters(self._args(), self.sparring_dir, self.repo)
        _write_preferences(stage_model="sonnet")
        second, _ = _build_loop_adapters(self._args(), self.sparring_dir, self.repo)
        self.assertEqual(first.model, "opus", "the adapter already built is unchanged")
        self.assertEqual(second.model, "sonnet")


class ShowConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()

    def _main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def _write(self, content: str) -> None:
        (self.sparring_dir / "project.toml").write_text(content, encoding="utf-8")

    def test_json_reports_the_resolved_values_and_where_each_came_from(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )
        code, out, err = self._main("show-config", "--json", "--stage-model", "sonnet")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["stage"]["provider"], "claude-cli")
        self.assertEqual(payload["stage"]["provider_display_name"], "Claude")
        self.assertEqual(payload["stage"]["model"], "sonnet")
        self.assertEqual(payload["stage"]["model_source"], "cli")
        self.assertEqual(payload["stage"]["effort"], "high")
        self.assertEqual(payload["stage"]["effort_source"], "user")
        self.assertEqual(payload["sparring"]["provider_display_name"], "Codex")
        self.assertEqual(payload["sparring"]["model_source"], "user")
        self.assertTrue(payload["config_exists"])
        self.assertTrue(Path(payload["config_path"]).is_absolute())
        self.assertIsNone(payload["error"])

    def test_an_unset_model_is_reported_as_null_with_a_provider_default_source(self):
        self._write(LEGACY_CONFIG)
        code, out, _ = self._main("show-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIsNone(payload["stage"]["model"])
        self.assertEqual(payload["stage"]["model_source"], "provider-default")
        self.assertIsNone(payload["stage"]["effort"])
        self.assertEqual(payload["stage"]["effort_source"], "provider-default")

    def test_a_missing_project_toml_still_reports_the_engine_defaults(self):
        code, out, _ = self._main("show-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["config_exists"])
        self.assertIsNone(payload["project"])
        self.assertEqual(payload["stage"]["provider"], "claude-cli")
        self.assertEqual(payload["stage"]["provider_source"], "engine-default")

    def test_an_invalid_configuration_exits_nonzero_with_a_json_error(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(stage_effort="ultra")
        code, out, _ = self._main("show-config", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("ultra", payload["error"])
        self.assertNotIn("stage", payload, "no resolved config is invented for a broken file")

    def test_it_reports_no_environment_or_secret_material(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )
        _, out, _ = self._main("show-config", "--json")
        payload = json.loads(out)
        # Configuration only. Setup problems (or why they could not be
        # determined) and the user-preference file's own path/existence are
        # also configuration facts, never environment or credentials.
        self.assertTrue({"config_path", "config_exists", "project", "error"} <= set(payload))
        self.assertLessEqual(
            set(payload) - {"stage", "sparring"},
            {
                "config_path",
                "config_exists",
                "project",
                "error",
                "setup_problems",
                "setup_error",
                "user_config_path",
                "user_config_exists",
            },
        )
        for role in ("stage", "sparring"):
            self.assertNotIn("env", payload[role])
            self.assertNotIn("executable", payload[role])

    def test_check_config_reports_the_effective_agents_too(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(stage_model="opus", stage_effort="high")
        code, out, _ = self._main("check-config", "--repo-root", str(self.repo))
        # The repo is not a git repository here, so the workflow-state probe
        # is the only thing that can fail; the agent lines must still print.
        self.assertIn("stage agent: claude-cli (project)", out)
        self.assertIn("model 'opus' (user)", out)
        self.assertIn("effort 'high' (user)", out)
        del code

    def test_check_config_fails_on_an_effort_the_provider_cannot_honour(self):
        self._write(LEGACY_CONFIG)
        _write_preferences(stage_effort="ultra")
        code, _, err = self._main("check-config", "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("invalid agent configuration", err)
        self.assertIn("ultra", err)


class InitConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "my-project"
        self.sparring_dir = self.repo / ".sparring"

    def _main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_a_minimal_file_that_the_engine_can_read_back(self):
        code, out, _ = self._main("init-config")
        self.assertEqual(code, 0)
        path = Path(out.strip())
        self.assertEqual(path, self.sparring_dir / "project.toml")
        config = parse_project_config(path.read_text(encoding="utf-8"))
        self.assertEqual(config.project, "my-project")
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")

    def test_the_template_invents_no_model_or_effort(self):
        self._main("init-config")
        config = parse_project_config(
            (self.sparring_dir / "project.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(config.obsolete_agent_settings, ())

    def test_it_refuses_to_overwrite_an_existing_file(self):
        self.sparring_dir.mkdir(parents=True)
        (self.sparring_dir / "project.toml").write_text(FULL_CONFIG, encoding="utf-8")
        code, _, err = self._main("init-config")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(
            (self.sparring_dir / "project.toml").read_text(encoding="utf-8"), FULL_CONFIG
        )

    def test_exist_ok_reports_the_path_of_the_file_that_is_already_there(self):
        self.sparring_dir.mkdir(parents=True)
        (self.sparring_dir / "project.toml").write_text(FULL_CONFIG, encoding="utf-8")
        code, out, _ = self._main("init-config", "--exist-ok")
        self.assertEqual(code, 0)
        self.assertEqual(Path(out.strip()), self.sparring_dir / "project.toml")


class RecordingAdapters:
    """Patches the adapter classes the CLI constructs, so a whole command can
    be run and then asked what configuration actually reached a provider."""

    def __init__(self, stage_adapter, sparring_adapter):
        self.stage_kwargs: list[dict] = []
        self.sparring_kwargs: list[dict] = []
        self.stage_adapter = stage_adapter
        self.sparring_adapter = sparring_adapter

    @contextlib.contextmanager
    def patched(self):
        def make_stage(**kwargs):
            self.stage_kwargs.append(kwargs)
            return self.stage_adapter

        def make_sparring(**kwargs):
            self.sparring_kwargs.append(kwargs)
            return self.sparring_adapter

        with mock.patch("agent_sparring.cli.ClaudeCliAdapter", make_stage), mock.patch(
            "agent_sparring.cli.CodexCliAdapter", make_sparring
        ):
            yield self


class _Refusing:
    """An adapter that fails its turn immediately.

    These tests are about what the CLI *configured*, which is fixed by the
    time the adapter exists; letting the turn itself fail keeps them from
    depending on any provider output.
    """

    provider_id = "refusing"
    supports_resume = True

    def start(self, prompt):
        raise ProviderError("no provider is available in this test")

    def resume(self, session_id, prompt):
        raise ProviderError("no provider is available in this test")


class SingleRoleCommandResolutionTests(unittest.TestCase):
    """run-stage and run-sparring read the same project.toml and the same
    user preferences the loop does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        for command in (
            ["init", "-q", "-b", "feature/x"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "base"],
            check=True,
            capture_output=True,
        )
        self.sparring_dir = self.repo / ".sparring"
        self._main("new-stage", "stage-1")
        (self.sparring_dir / "project.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )

    def _main(self, *argv: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["--sparring-dir", str(self.sparring_dir), *argv])

    def _run(self, *argv: str) -> RecordingAdapters:
        # The adapters are constructed before any turn is attempted, so the
        # recorded kwargs are the whole point; whether the fake turn then
        # succeeds is irrelevant here.
        recorder = RecordingAdapters(_Refusing(), _Refusing())
        with recorder.patched():
            self._main(*argv)
        return recorder

    def test_run_stage_uses_the_user_stage_model_and_effort(self):
        recorder = self._run(
            "run-stage", "stage-1", "--repo-root", str(self.repo),
            "--expected-branch", "feature/x",
        )
        self.assertEqual(recorder.stage_kwargs[0]["model"], "opus")
        self.assertEqual(recorder.stage_kwargs[0]["effort"], "high")

    def test_run_stage_cli_overrides_beat_the_project(self):
        recorder = self._run(
            "run-stage", "stage-1", "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--model", "sonnet", "--effort", "max",
        )
        self.assertEqual(recorder.stage_kwargs[0]["model"], "sonnet")
        self.assertEqual(recorder.stage_kwargs[0]["effort"], "max")

    def test_run_sparring_uses_the_user_sparring_model_and_effort(self):
        # The untracked project.toml is not implementation output; ignore it
        # so the reviewer's unrelated-untracked-file refusal does not apply.
        (self.repo / ".git" / "info" / "exclude").write_text(
            ".sparring/project.toml\n", encoding="utf-8"
        )
        recorder = self._run(
            "run-sparring", "stage-1", "--repo-root", str(self.repo),
            "--expected-branch", "feature/x",
        )
        self.assertEqual(recorder.sparring_kwargs[0]["model"], "gpt-5.6-terra")
        self.assertEqual(recorder.sparring_kwargs[0]["effort"], "xhigh")

    def test_run_stage_refuses_an_unsupported_effort_without_building_an_adapter(self):
        _write_preferences(stage_effort="ultra")
        recorder = RecordingAdapters(_Refusing(), _Refusing())
        err = io.StringIO()
        with recorder.patched(), contextlib.redirect_stdout(io.StringIO()), (
            contextlib.redirect_stderr(err)
        ):
            code = main(
                [
                    "--sparring-dir", str(self.sparring_dir), "run-stage", "stage-1",
                    "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(recorder.stage_kwargs, [], "no adapter, so no provider process")
        self.assertIn("ultra", err.getvalue())


class PlanCommandResolutionTests(_PlanRepoTestCase):
    """run-plan and resume-plan resolve exactly as run-stage/run-sparring do."""

    def setUp(self):
        super().setUp()
        self.sparring_dir.mkdir(exist_ok=True)
        (self.sparring_dir / "project.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
        _run_git(self.repo, "add", ".sparring/project.toml")
        _run_git(self.repo, "commit", "-q", "-m", "project config")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        _write_preferences(
            stage_model="opus", stage_effort="high", sparring_model="gpt-5.6-terra", sparring_effort="xhigh"
        )

    def _main(self, recorder: RecordingAdapters, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with recorder.patched(), contextlib.redirect_stdout(io.StringIO()), (
            contextlib.redirect_stderr(err)
        ):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, err.getvalue()

    def _recorder(self, verdicts) -> RecordingAdapters:
        return RecordingAdapters(_StageAdapter(self.repo), _SparringAdapter(verdicts))

    def test_run_plan_gives_each_role_its_own_configuration(self):
        recorder = self._recorder([NEEDS_YOU])
        code, err = self._main(
            recorder, "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(recorder.stage_kwargs[0]["model"], "opus")
        self.assertEqual(recorder.stage_kwargs[0]["effort"], "high")
        self.assertEqual(recorder.sparring_kwargs[0]["model"], "gpt-5.6-terra")
        self.assertEqual(recorder.sparring_kwargs[0]["effort"], "xhigh")

    def test_resume_plan_resolves_the_same_way_and_keeps_the_provider_sessions(self):
        first = self._recorder([NEEDS_YOU])
        self._main(
            first, "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x",
        )
        second = self._recorder([READY, READY])
        code, err = self._main(
            second, "resume-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--evidence", "checked",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(second.stage_kwargs[0]["model"], "opus")
        self.assertEqual(second.sparring_kwargs[0]["effort"], "xhigh")
        # The resumed turn is a resume of the provider's own session, not a
        # fresh one: configuration changed nothing about session continuity.
        self.assertEqual(
            [sid for sid, _ in second.sparring_adapter.resume_calls], ["spar-1"]
        )

    def test_a_cli_override_reaches_every_stage_of_a_plan_run(self):
        recorder = self._recorder([READY, READY])
        code, err = self._main(
            recorder, "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--stage-model", "sonnet",
            "--sparring-effort", "minimal",
        )
        self.assertEqual(code, 0, err)
        self.assertGreater(len(recorder.stage_kwargs), 1, "more than one stage ran")
        self.assertTrue(all(kw["model"] == "sonnet" for kw in recorder.stage_kwargs))
        self.assertTrue(all(kw["effort"] == "minimal" for kw in recorder.sparring_kwargs))

    def test_an_unsupported_effort_stops_run_plan_before_any_stage_state_exists(self):
        _write_preferences(sparring_effort="turbo")
        recorder = self._recorder([READY])
        code, err = self._main(
            recorder, "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x",
        )
        self.assertEqual(code, 1)
        self.assertIn("turbo", err)
        self.assertEqual(recorder.stage_kwargs, [])
        self.assertFalse(self.state_path.exists(), "no run state was recorded")


class IndependentReviewResolutionTests(_ReviewRepoTestCase):
    """The independent reviewer is the sparring role, so it takes the
    sparring role's configuration -- not the stage agent's."""

    def setUp(self):
        super().setUp()
        self.sparring_dir.mkdir(exist_ok=True)
        (self.sparring_dir / "project.toml").write_text(LEGACY_CONFIG, encoding="utf-8")
        _run_git(self.repo, "add", ".sparring/project.toml")
        _run_git(self.repo, "commit", "-q", "-m", "project config")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        _write_preferences(sparring_model="gpt-5.6-terra", sparring_effort="xhigh")

    def test_the_reviewer_is_configured_from_the_sparring_role(self):
        recorder = RecordingAdapters(
            _StageAdapter(self.repo, commit=True), _SparringAdapter([READY, READY])
        )
        err = io.StringIO()
        with recorder.patched(), contextlib.redirect_stdout(io.StringIO()), (
            contextlib.redirect_stderr(err)
        ):
            code = main(
                [
                    "--sparring-dir", str(self.sparring_dir), "run-plan",
                    "--manifest", str(self.manifest_path), "--repo-root", str(self.repo),
                    "--expected-branch", "feature/x",
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        self.assertIs(
            self.stage(REVIEW_STAGE).read_state().mode, StageMode.INDEPENDENT_REVIEW
        )
        # Two adapter pairs were built (one per stage); every sparring one --
        # including the review stage's reviewer -- carries the sparring
        # role's configuration, and none of them carries the stage agent's.
        self.assertEqual(len(recorder.sparring_kwargs), 2)
        for kwargs in recorder.sparring_kwargs:
            self.assertEqual(kwargs["model"], "gpt-5.6-terra")
            self.assertEqual(kwargs["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
