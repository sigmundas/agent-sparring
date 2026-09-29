"""The person's own model/effort preferences: shared, keyed, atomic, honest.

Provider stays a project decision (``.sparring/project.toml``); model and
effort are the person's own preference, in one engine-owned file outside
every repository (:mod:`agent_sparring.user_config`), keyed by role *and*
resolved provider so a value chosen for one provider is never applied to
another. These tests exercise that file end to end: across repositories
that share a provider, kept separate across roles and providers that do
not, falling back to the provider's own default when nothing is set,
ignoring an obsolete project.toml value, never dirtying the working tree,
the full CLI/env/user/provider-default precedence stack, live re-resolution
after an edit, ``show-config --json``'s introspection of it, validation
before anything is written, and the file's own atomic-write guarantees.
``model-choices`` (a UI's suggestion list for a role's provider) is
included here too, as the other place a person's model choice is read from.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.agent_config import ROLE_SPARRING, ROLE_STAGE, RoleOverrides, resolve_role_config
from agent_sparring.cli import main
from agent_sparring.config import parse_project_config
from agent_sparring.model_choices import model_choices
from agent_sparring.user_config import (
    RolePreference,
    UserConfigError,
    UserPreferences,
    load_user_preferences,
    render_user_preferences,
    update_user_preferences,
    user_config_path,
)


def _cli_in(sparring_dir: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--sparring-dir", str(sparring_dir), *argv])
    return code, out.getvalue(), err.getvalue()


def _sparring_dir(root: Path, *, stage_provider="claude-cli", sparring_provider="codex-cli") -> Path:
    sparring_dir = root / ".sparring"
    sparring_dir.mkdir(parents=True)
    (sparring_dir / "project.toml").write_text(
        f'project = "demo"\n\n[agents.stage]\nprovider = "{stage_provider}"\n\n'
        f'[agents.sparring]\nprovider = "{sparring_provider}"\n',
        encoding="utf-8",
    )
    return sparring_dir


class CrossRepoPreferenceTests(unittest.TestCase):
    """(1)-(4): the preference file is not scoped to any repository, and is
    still keyed strictly by role and resolved provider."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_a = _sparring_dir(Path(self._tmp.name) / "repo-a")
        self.repo_b = _sparring_dir(Path(self._tmp.name) / "repo-b")

    def test_a_stage_claude_cli_preference_set_in_one_repo_is_effective_in_another(self):
        code, _, _ = _cli_in(
            self.repo_a, "set-config", "stage", "--model", "claude-opus-5-5", "--effort", "high"
        )
        self.assertEqual(code, 0)
        _, out, _ = _cli_in(self.repo_b, "show-config", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["stage"]["model"], "claude-opus-5-5")
        self.assertEqual(payload["stage"]["model_source"], "user")
        self.assertEqual(payload["stage"]["effort"], "high")
        self.assertEqual(payload["stage"]["effort_source"], "user")

    def test_a_sparring_codex_cli_preference_set_in_one_repo_is_effective_in_another(self):
        code, _, _ = _cli_in(
            self.repo_a, "set-config", "sparring", "--model", "gpt-5.6-terra", "--effort", "xhigh"
        )
        self.assertEqual(code, 0)
        _, out, _ = _cli_in(self.repo_b, "show-config", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["sparring"]["model"], "gpt-5.6-terra")
        self.assertEqual(payload["sparring"]["model_source"], "user")
        self.assertEqual(payload["sparring"]["effort"], "xhigh")

    def test_stage_and_sparring_preferences_stay_separate_at_the_for_role_level(self):
        # Today claude-cli is stage-only and codex-cli is sparring-only, so
        # resolution itself cannot exercise "both roles on the same
        # provider" -- this pins that for_role() would keep them apart if
        # it could happen, by asking each role for the other's provider.
        preferences = UserPreferences(
            path=user_config_path(),
            entries={
                (ROLE_STAGE, "claude-cli"): RolePreference(model="claude-opus-5-5"),
                (ROLE_SPARRING, "codex-cli"): RolePreference(model="gpt-5.6-terra"),
            },
        )
        self.assertEqual(preferences.for_role(ROLE_STAGE, "codex-cli"), RolePreference())
        self.assertEqual(preferences.for_role(ROLE_SPARRING, "claude-cli"), RolePreference())
        self.assertEqual(preferences.for_role(ROLE_STAGE, "claude-cli").model, "claude-opus-5-5")
        self.assertEqual(preferences.for_role(ROLE_SPARRING, "codex-cli").model, "gpt-5.6-terra")

    def test_a_stage_preference_never_leaks_into_sparring_resolution(self):
        _cli_in(self.repo_a, "set-config", "stage", "--model", "claude-opus-5-5")
        _, out, _ = _cli_in(self.repo_a, "show-config", "--json")
        payload = json.loads(out)
        self.assertIsNone(payload["sparring"]["model"])
        self.assertEqual(payload["sparring"]["model_source"], "provider-default")

    def test_a_preference_saved_for_a_different_provider_is_never_used(self):
        # Written directly under codex-cli for the stage role, which never
        # runs codex-cli here: proves the *read* side, not just set-config's
        # own write-time provider resolution, is keyed by provider.
        update_user_preferences(
            user_config_path(),
            lambda current: current.with_edit(ROLE_STAGE, "codex-cli", "model", "gpt-5.6-terra"),
        )
        _, out, _ = _cli_in(self.repo_a, "show-config", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["stage"]["provider"], "claude-cli")
        self.assertIsNone(
            payload["stage"]["model"], "a codex-cli preference must not apply to claude-cli"
        )
        self.assertEqual(payload["stage"]["model_source"], "provider-default")


class NoPreferenceProviderDefaultTests(unittest.TestCase):
    """(5): with no preference file at all, every role falls back to the
    provider's own default -- read through the real loader, not a
    constructed object, so this exercises the isolated-absent-file path
    tests/conftest.py sets up."""

    def test_no_preference_file_resolves_to_provider_default(self):
        config = parse_project_config(
            'project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\n\n'
            '[agents.sparring]\nprovider = "codex-cli"\n'
        )
        self.assertFalse(user_config_path().exists())
        for role in (ROLE_STAGE, ROLE_SPARRING):
            resolved = resolve_role_config(role, config)
            self.assertIsNone(resolved.model)
            self.assertEqual(resolved.model_source, "provider-default")
            self.assertIsNone(resolved.effort)
            self.assertEqual(resolved.effort_source, "provider-default")


class ObsoleteProjectSettingIsNeverUsedTests(unittest.TestCase):
    """(6): an old project.toml model/effort key is diagnosed elsewhere (see
    tests/test_setup_check.py) but never resolved -- whether or not a real
    user preference exists."""

    def test_a_stale_project_model_is_ignored_when_a_preference_exists(self):
        config = parse_project_config(
            'project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\nmodel = "stale-model"\n'
        )
        preferences = UserPreferences(
            path=Path("unused"),
            entries={(ROLE_STAGE, "claude-cli"): RolePreference(model="claude-opus-5-5")},
        )
        resolved = resolve_role_config(ROLE_STAGE, config, preferences=preferences)
        self.assertEqual(resolved.model, "claude-opus-5-5")
        self.assertEqual(resolved.model_source, "user")

    def test_a_stale_project_model_is_ignored_with_no_preference_either(self):
        config = parse_project_config(
            'project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\nmodel = "stale-model"\n'
        )
        resolved = resolve_role_config(ROLE_STAGE, config, preferences=UserPreferences(path=Path("unused")))
        self.assertIsNone(resolved.model)
        self.assertEqual(resolved.model_source, "provider-default")


class RepositoryCleanlinessTests(unittest.TestCase):
    """(9): writing a preference never touches the consuming repository."""

    def test_writing_a_preference_never_changes_git_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
        sparring_dir = _sparring_dir(repo)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True, capture_output=True)

        def status() -> str:
            return subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        before = status()
        code, _, _ = _cli_in(sparring_dir, "set-config", "stage", "--model", "claude-opus-5-5")
        self.assertEqual(code, 0)
        self.assertTrue(user_config_path().is_file())
        self.assertFalse(
            str(user_config_path().resolve()).startswith(str(repo.resolve())),
            "the preference file must live outside the repository entirely",
        )
        self.assertEqual(before, status())


class PrecedenceStackTests(unittest.TestCase):
    """(10): CLI beats env beats the user preference beats the provider's
    own default, with the source reported at every step."""

    def test_the_full_precedence_stack(self):
        config = parse_project_config('project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\n')
        preferences = UserPreferences(
            path=Path("unused"), entries={(ROLE_STAGE, "claude-cli"): RolePreference(model="from-user")}
        )

        resolved = resolve_role_config(ROLE_STAGE, config, preferences=preferences)
        self.assertEqual((resolved.model, resolved.model_source), ("from-user", "user"))

        resolved = resolve_role_config(
            ROLE_STAGE, config, environ={"SPARRING_STAGE_MODEL": "from-env"}, preferences=preferences
        )
        self.assertEqual((resolved.model, resolved.model_source), ("from-env", "env"))

        resolved = resolve_role_config(
            ROLE_STAGE,
            config,
            RoleOverrides(model="from-cli"),
            environ={"SPARRING_STAGE_MODEL": "from-env"},
            preferences=preferences,
        )
        self.assertEqual((resolved.model, resolved.model_source), ("from-cli", "cli"))

        resolved = resolve_role_config(ROLE_STAGE, config, preferences=UserPreferences(path=Path("unused")))
        self.assertIsNone(resolved.model)
        self.assertEqual(resolved.model_source, "provider-default")


class LiveReResolutionTests(unittest.TestCase):
    """(11): an already-built ResolvedAgentConfig is a frozen snapshot; the
    next resolution call is what sees a preference file change."""

    def test_an_existing_resolution_is_unaffected_by_a_later_preference_edit(self):
        config = parse_project_config('project = "d"\n\n[agents.stage]\nprovider = "claude-cli"\n')
        update_user_preferences(
            user_config_path(), lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "first")
        )
        first = resolve_role_config(ROLE_STAGE, config)
        self.assertEqual(first.model, "first")

        update_user_preferences(
            user_config_path(), lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "second")
        )
        self.assertEqual(first.model, "first", "a resolution already returned does not change retroactively")

        second = resolve_role_config(ROLE_STAGE, config)
        self.assertEqual(second.model, "second", "the next resolution call sees the new preference")


class ShowConfigIntrospectionTests(unittest.TestCase):
    """(12): show-config --json reports the preference file's own path and
    existence, and each role's model/effort source."""

    def test_show_config_json_reports_the_user_config_path_and_existence(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sparring_dir = _sparring_dir(Path(tmp.name))

        code, out, _ = _cli_in(sparring_dir, "show-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["user_config_path"], str(user_config_path()))
        self.assertFalse(payload["user_config_exists"])
        self.assertEqual(payload["stage"]["model_source"], "provider-default")

        _cli_in(sparring_dir, "set-config", "stage", "--model", "claude-opus-5-5")
        code, out, _ = _cli_in(sparring_dir, "show-config", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["user_config_exists"])
        self.assertEqual(payload["stage"]["model_source"], "user")
        # The role not touched still reports its own honest source.
        self.assertEqual(payload["sparring"]["model_source"], "provider-default")


class ValidationBeforeWriteTests(unittest.TestCase):
    """(13): every invalid combination fails before anything is written, and
    a rejected CLI/env override still resolves fine for a one-off run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = _sparring_dir(Path(self._tmp.name))

    def _cli(self, *argv: str) -> tuple[int, str, str]:
        return _cli_in(self.sparring_dir, *argv)

    def test_a_bad_effort_for_the_resolved_provider_is_refused(self):
        code, _, err = self._cli("set-config", "stage", "--effort", "ultra")
        self.assertEqual(code, 1)
        self.assertIn("ultra", err)
        self.assertFalse(user_config_path().exists())

    def test_a_claude_alias_is_refused_as_a_saved_model(self):
        code, _, err = self._cli("set-config", "stage", "--model", "opus")
        self.assertEqual(code, 1)
        self.assertIn("alias", err)
        self.assertFalse(user_config_path().exists())

    def test_a_claude_alias_still_resolves_fine_as_a_cli_override(self):
        # Refused only as a *saved* preference; a one-off override may still
        # use it, since the provider CLI itself accepts aliases.
        code, out, err = self._cli("show-config", "--json", "--stage-model", "opus")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["stage"]["model"], "opus")
        self.assertEqual(payload["stage"]["model_source"], "cli")

    def test_a_claude_alias_still_resolves_fine_as_an_env_override(self):
        with mock.patch.dict(os.environ, {"SPARRING_STAGE_MODEL": "opus"}):
            code, out, err = self._cli("show-config", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["stage"]["model"], "opus")
        self.assertEqual(payload["stage"]["model_source"], "env")

    def test_an_unknown_for_provider_is_refused(self):
        code, _, err = self._cli(
            "set-config", "stage", "--model", "claude-opus-5-5", "--for-provider", "acme-cli"
        )
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", err)
        self.assertFalse(user_config_path().exists())

    def test_a_provider_not_implemented_for_the_role_is_refused_via_for_provider(self):
        code, _, err = self._cli(
            "set-config", "stage", "--model", "gpt-5.6-terra", "--for-provider", "codex-cli"
        )
        self.assertEqual(code, 1)
        self.assertIn("codex-cli", err)
        self.assertFalse(user_config_path().exists())


class AtomicWriteTests(unittest.TestCase):
    """(14): the file itself -- deterministic rendering, no stray temp
    files, an unchanged edit writes nothing, clearing the last preference
    removes the file, and a malformed/unknown-key file is a load error."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"

    def test_the_file_is_rendered_deterministically(self):
        preferences = UserPreferences(
            path=self.path,
            entries={
                (ROLE_SPARRING, "codex-cli"): RolePreference(model="gpt-5.6-terra"),
                (ROLE_STAGE, "claude-cli"): RolePreference(model="claude-opus-5-5", effort="high"),
            },
        )
        self.assertEqual(render_user_preferences(preferences), render_user_preferences(preferences))
        self.assertTrue(render_user_preferences(preferences).startswith("# Agent Sparring user preferences"))

    def _entries_excluding_lock(self) -> list[str]:
        # The POSIX writer lock file (".<name>.lock") is a deliberate,
        # permanent fixture used to serialise writers -- not a leftover
        # temp file; only *.tmp siblings (or anything else unexpected) are
        # the thing being asserted against here.
        return sorted(p.name for p in self.path.parent.iterdir() if p.name != f".{self.path.name}.lock")

    def test_no_temporary_files_are_left_behind_on_success(self):
        update_user_preferences(
            self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "claude-opus-5-5")
        )
        self.assertEqual(self._entries_excluding_lock(), [self.path.name])

    def test_no_temporary_file_is_left_when_the_replace_fails(self):
        with mock.patch("agent_sparring.user_config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(UserConfigError) as caught:
                update_user_preferences(
                    self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "x")
                )
        self.assertIn("disk full", str(caught.exception))
        self.assertFalse(self.path.exists())
        self.assertEqual(
            self._entries_excluding_lock(), [], "the temp file is cleaned up, not left behind"
        )

    def test_an_unchanged_edit_writes_nothing(self):
        update_user_preferences(
            self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "claude-opus-5-5")
        )
        before = self.path.stat().st_mtime_ns
        _, changed = update_user_preferences(
            self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "claude-opus-5-5")
        )
        self.assertFalse(changed)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_clearing_the_last_preference_deletes_the_file(self):
        update_user_preferences(
            self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", "claude-opus-5-5")
        )
        self.assertTrue(self.path.exists())
        _, changed = update_user_preferences(
            self.path, lambda c: c.with_edit(ROLE_STAGE, "claude-cli", "model", None)
        )
        self.assertTrue(changed)
        self.assertFalse(self.path.exists())

    def test_a_malformed_file_is_a_load_error(self):
        self.path.write_text("not = valid = toml = at = all\n", encoding="utf-8")
        with self.assertRaises(UserConfigError):
            load_user_preferences(self.path)

    def test_an_unknown_role_in_the_file_is_a_load_error(self):
        self.path.write_text('version = 1\n\n[agents.reviewer.claude-cli]\nmodel = "x"\n', encoding="utf-8")
        with self.assertRaises(UserConfigError) as caught:
            load_user_preferences(self.path)
        self.assertIn("reviewer", str(caught.exception))

    def test_an_unknown_field_in_the_file_is_a_load_error(self):
        self.path.write_text('version = 1\n\n[agents.stage.claude-cli]\nreasoning = "high"\n', encoding="utf-8")
        with self.assertRaises(UserConfigError) as caught:
            load_user_preferences(self.path)
        self.assertIn("reasoning", str(caught.exception))


class ModelChoicesTests(unittest.TestCase):
    """A suggestion list for a role's provider, never a validation boundary:
    ``custom_allowed`` is always true. Claude's list is engine-known;
    Codex's own catalog is read from ``codex debug models``."""

    def test_claude_cli_choices_are_engine_known_and_custom_allowed(self):
        result = model_choices(ROLE_STAGE, "claude-cli")
        self.assertEqual(result.source, "engine-known")
        self.assertFalse(result.complete)
        payload = result.as_dict()
        self.assertTrue(payload["custom_allowed"])
        self.assertIn("claude-opus-5-5", [choice["model"] for choice in payload["choices"]])
        self.assertIsNone(payload["error"])

    def test_codex_cli_choices_come_from_a_fake_debug_models_executable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-terra",
                        "display_name": "GPT 5.6 Terra",
                        "priority": 1,
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                        "default_reasoning_level": "medium",
                    },
                    # Not "list": must be excluded, not merely deprioritised.
                    {"slug": "gpt-hidden", "visibility": "experimental"},
                ]
            }
        )
        script = root / "fake-codex.py"
        script.write_text(
            "import sys\n"
            f"payload = {catalog!r}\n"
            "if sys.argv[1:3] == ['debug', 'models']:\n"
            "    print(payload)\n"
            "else:\n"
            "    sys.exit(2)\n",
            encoding="utf-8",
        )
        wrapper = root / "fake-codex"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)

        result = model_choices(ROLE_SPARRING, "codex-cli", codex_executable=str(wrapper))
        self.assertEqual(result.source, "provider-catalog")
        self.assertFalse(result.complete)
        self.assertIsNone(result.error)
        self.assertEqual([choice.model for choice in result.choices], ["gpt-5.6-terra"])
        self.assertEqual(result.choices[0].effort_levels, ("medium", "high"))
        self.assertEqual(result.choices[0].default_effort, "medium")

    def test_a_failing_codex_executable_reports_an_error_not_an_empty_success(self):
        result = model_choices(
            ROLE_SPARRING, "codex-cli", codex_executable="definitely-not-a-real-executable-xyz"
        )
        self.assertEqual(result.source, "none")
        self.assertFalse(result.complete)
        self.assertEqual(result.choices, ())
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
