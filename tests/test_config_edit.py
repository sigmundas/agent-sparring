"""Typed, engine-owned mutation of ``.sparring/project.toml``.

Provider is the only field this module edits: model and effort are the
person's own preferences now (:mod:`agent_sparring.user_config`), and a
project.toml already carrying them just carries obsolete keys that
``remove_obsolete_agent_settings``/``fix-config`` clean up separately (see
``tests/test_setup_check.py``). These tests are about what the file looks
like afterwards, not only about what the command returned. A mutation
command that reports success while leaving behind a file no run can load, or
one that quietly drops a setting the person chose, is the failure mode worth
spending tests on -- so every refusal here also asserts that the bytes on
disk are unchanged, and every acceptance asserts that the unrelated parts of
the file survived and that ``show-config`` agrees with what was just
written.
"""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.agent_config import (
    AgentConfigError,
    ROLE_SPARRING,
    ROLE_STAGE,
    providers_for_role,
)
from agent_sparring.cli import main
from agent_sparring.config import ProjectConfigError, parse_project_config
from agent_sparring.config_edit import RoleEdit, apply_role_edit
from agent_sparring.user_config import user_config_path

# A file with settings this feature must never touch: a comment a person
# wrote, a command table, a sparring default, and the other role. The
# model/effort keys under [agents.*] are obsolete (see config.py) and must
# survive a provider edit untouched -- only fix-config removes them.
HAND_WRITTEN = """\
# Notes I keep at the top of this file.
project = "demo"

[repo]
root = "."

[commands]
test = "pytest -q"   # trailing comment

[sparring]
default_mode = "adversarial"

[agents.stage]
provider = "claude-cli"
model = "opus"
effort = "high"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-terra"
effort = "xhigh"
"""

# Same as HAND_WRITTEN but with no provider stated for the stage role, so an
# edit to it is a genuine change rather than a no-op (today only one provider
# is implemented per role, so "change the existing value" is not otherwise
# reachable).
NO_STAGE_PROVIDER = """\
# Notes I keep at the top of this file.
project = "demo"

[repo]
root = "."

[commands]
test = "pytest -q"   # trailing comment

[sparring]
default_mode = "adversarial"

[agents.stage]
model = "opus"
effort = "high"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-terra"
effort = "xhigh"
"""

LEGACY = """\
project = "demo"

[agents.stage]
provider = "claude-cli"

[agents.sparring]
provider = "codex-cli"
"""


class _ConfigDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "demo"
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir(parents=True)
        self.path = self.sparring_dir / "project.toml"

    def write(self, content: str) -> None:
        self.path.write_text(content, encoding="utf-8")

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def config(self):
        return parse_project_config(self.text(), source=str(self.path))

    def cli(self, *argv: str) -> tuple[int, str, str]:
        """Run the CLI in-process. An argparse exit is reported as its code
        rather than propagated, so a test can assert on the refusal."""

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--sparring-dir", str(self.sparring_dir), *argv])
            except SystemExit as exit_code:
                code = int(exit_code.code or 0)
        return code, out.getvalue(), err.getvalue()


class PreservationTests(_ConfigDirTestCase):
    """The rest of the file, the other role, and obsolete model/effort keys
    this module never touches all survive a provider mutation intact."""

    def test_unrelated_settings_and_comments_survive_a_mutation(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        text = self.text()
        self.assertIn("# Notes I keep at the top of this file.", text)
        self.assertIn('test = "pytest -q"   # trailing comment', text)
        self.assertIn('default_mode = "adversarial"', text)
        config = self.config()
        self.assertEqual(config.commands["test"], "pytest -q")
        self.assertEqual(config.default_sparring_mode, "adversarial")
        self.assertEqual(config.repo_root, ".")
        # The obsolete model/effort keys this edit never touches are
        # exactly as they were: only fix-config removes them.
        self.assertIn('model = "opus"', text)
        self.assertIn('effort = "high"', text)

    def test_a_stage_mutation_leaves_the_sparring_role_exactly_as_it_was(self):
        self.write(NO_STAGE_PROVIDER)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        config = self.config()
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        sparring_obsolete = {
            setting.field: setting.value
            for setting in config.obsolete_agent_settings
            if setting.role == "sparring"
        }
        self.assertEqual(sparring_obsolete, {"model": "gpt-5.6-terra", "effort": "xhigh"})

    def test_a_sparring_mutation_leaves_the_stage_role_exactly_as_it_was(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(provider="codex-cli"))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        stage_obsolete = {
            setting.field: setting.value
            for setting in config.obsolete_agent_settings
            if setting.role == "stage"
        }
        self.assertEqual(stage_obsolete, {"model": "opus", "effort": "high"})

    def test_an_edit_that_changes_nothing_does_not_rewrite_the_file(self):
        self.write(HAND_WRITTEN)
        before = self.path.stat().st_mtime_ns
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.text(), HAND_WRITTEN)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_no_temporary_files_are_left_beside_the_config(self):
        self.write(NO_STAGE_PROVIDER)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertEqual(
            sorted(entry.name for entry in self.sparring_dir.iterdir()), ["project.toml"]
        )


class MissingAndMalformedTests(_ConfigDirTestCase):
    """Absent is benign; unreadable is refused, never replaced."""

    def test_a_missing_project_toml_is_created_from_the_engine_template(self):
        self.path.unlink(missing_ok=True)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertTrue(outcome.created)
        self.assertTrue(outcome.changed)
        config = self.config()
        # The template's own content, not something this module invented.
        self.assertEqual(config.project, "demo")
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        # And the template invents no model or effort for either role.
        self.assertEqual(config.obsolete_agent_settings, ())

    def test_a_malformed_file_is_refused_and_left_byte_for_byte_alone(self):
        broken = 'project = "demo\n\n[agents.stage\n'
        self.write(broken)
        with self.assertRaises(ProjectConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertIn("not valid TOML", str(caught.exception))
        self.assertIn("will not overwrite", str(caught.exception))
        self.assertEqual(self.text(), broken)

    def test_an_existing_file_with_an_unknown_role_key_is_refused_not_repaired(self):
        # Editing one field of a file the parser rejects would "succeed" and
        # leave behind something no run can load.
        odd = 'project = "demo"\n\n[agents.stage]\nprovider = "claude-cli"\nreasoning = "high"\n'
        self.write(odd)
        with self.assertRaises(ProjectConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertIn("reasoning", str(caught.exception))
        self.assertEqual(self.text(), odd)

    def test_a_write_failure_leaves_the_original_config_intact(self):
        # The atomic write is the only step that touches the file, so a
        # failure in it must leave the previous content completely readable.
        self.write(NO_STAGE_PROVIDER)
        with mock.patch("agent_sparring.config_edit.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ProjectConfigError) as caught:
                apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual(self.text(), NO_STAGE_PROVIDER)
        self.assertIsNone(self.config().stage_agent_provider)
        self.assertEqual(
            sorted(entry.name for entry in self.sparring_dir.iterdir()),
            ["project.toml"],
            "the temporary file is cleaned up rather than left beside the real one",
        )

    def test_an_unreadable_directory_is_reported_as_a_configuration_error(self):
        self.write(NO_STAGE_PROVIDER)
        mode = self.sparring_dir.stat().st_mode
        os.chmod(self.sparring_dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.sparring_dir, mode)
        if os.access(self.sparring_dir, os.W_OK):
            self.skipTest("this filesystem or user ignores the directory write bit")
        with self.assertRaises(ProjectConfigError):
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertEqual(self.text(), NO_STAGE_PROVIDER)


class UnusualButValidTomlTests(_ConfigDirTestCase):
    """Shapes a person (or another tool) can legally have written.

    tomlkit hands back a different object for each of these -- a proxy for
    an out-of-order table, an inline table for a one-liner -- and the edit
    has to work on all of them rather than on the one shape the template
    happens to produce.
    """

    def test_an_out_of_order_agents_table_is_edited_in_place(self):
        self.write(
            'project = "demo"\n\n'
            '[agents.stage]\n\n'
            '[commands]\ntest = "pytest -q"\n\n'
            # Back to [agents] after another table: legal TOML, and the
            # shape tomlkit represents with a proxy rather than a Table.
            '[agents.sparring]\nprovider = "codex-cli"\n'
        )
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        self.assertEqual(config.commands["test"], "pytest -q")

    def test_an_inline_agents_table_is_edited_in_place(self):
        self.write(
            'project = "demo"\n'
            'agents = { stage = { }, sparring = { provider = "codex-cli" } }\n'
        )
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")

    def test_an_agents_value_that_is_not_a_table_is_refused(self):
        self.write('project = "demo"\nagents = "nonsense"\n')
        before = self.text()
        with self.assertRaises(ProjectConfigError):
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertEqual(self.text(), before)


class ProviderMutationTests(_ConfigDirTestCase):
    """Re-setting an already-configured provider is accepted, never orphans
    anything (there is nothing left to orphan: model/effort are not project
    settings any more), and an unimplemented/unknown one is refused."""

    def test_setting_the_provider_a_role_already_has_is_accepted(self):
        self.write(HAND_WRITTEN)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        self.assertEqual(outcome.resolved.provider, "claude-cli")
        self.assertFalse(outcome.changed, "already this value; nothing rewritten")

    def test_a_provider_not_implemented_for_the_role_is_refused(self):
        self.write(HAND_WRITTEN)
        before = self.text()
        with self.assertRaises(AgentConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="codex-cli"))
        message = str(caught.exception)
        self.assertIn("codex-cli", message)
        self.assertIn("only 'claude-cli'", message)
        self.assertEqual(self.text(), before)

    def test_an_unknown_provider_is_refused(self):
        self.write(HAND_WRITTEN)
        before = self.text()
        with self.assertRaises(AgentConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="acme-cli"))
        self.assertIn("unknown provider", str(caught.exception))
        self.assertEqual(self.text(), before)


class SetConfigCommandTests(_ConfigDirTestCase):
    """The CLI contract: two destinations (project.toml's provider, the
    user's own model/effort preference), validated up front, and agreeing
    with a fresh show-config."""

    def test_the_command_is_documented_in_help(self):
        code, out, _ = self.cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("set-config", out)

    def test_its_own_help_documents_every_operation(self):
        code, out, _ = self.cli("set-config", "--help")
        self.assertEqual(code, 0)
        for flag in (
            "--provider",
            "--model",
            "--model-default",
            "--effort",
            "--effort-default",
            "--for-provider",
        ):
            self.assertIn(flag, out)
        self.assertIn("stage", out)
        self.assertIn("sparring", out)
        # The two properties a person most needs to know before running it.
        self.assertIn("atomic", out)
        self.assertIn("next provider turn", out)

    def test_the_role_is_a_closed_choice(self):
        code, _, err = self.cli("set-config", "reviewer", "--model", "claude-opus-5-5")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_there_is_no_way_to_set_an_arbitrary_key(self):
        code, _, _ = self.cli("set-config", "stage", "--set", "repo.root=/elsewhere")
        self.assertEqual(code, 2, "an unknown flag is rejected by the parser")

    def test_setting_and_clearing_the_same_field_is_rejected_by_the_parser(self):
        code, _, err = self.cli("set-config", "stage", "--model", "claude-opus-5-5", "--model-default")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

    def test_an_invocation_that_asks_for_nothing_is_refused(self):
        self.write(LEGACY)
        before = self.text()
        code, _, err = self.cli("set-config", "stage")
        self.assertEqual(code, 1)
        self.assertIn("nothing to change", err)
        self.assertEqual(self.text(), before)

    def test_json_reports_the_effective_configuration_in_show_configs_shape(self):
        self.write(LEGACY)
        before = self.text()
        code, out, _ = self.cli(
            "set-config", "stage", "--model", "claude-opus-5-5", "--effort", "max", "--json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["created"])
        self.assertIsNone(payload["error"])
        # Model and effort are the person's preference now; project.toml
        # (the provider's own file) is not touched by this at all.
        self.assertEqual(self.text(), before)

        # The engine is authoritative: what set-config reports and what a
        # fresh show-config reports must be the same answer.
        _, shown, _ = self.cli("show-config", "--json")
        expected = json.loads(shown)
        self.assertEqual(
            {key: payload[key] for key in expected},
            expected,
            "set-config --json must agree with a fresh show-config",
        )
        self.assertEqual(payload["stage"]["model"], "claude-opus-5-5")
        self.assertEqual(payload["stage"]["model_source"], "user")
        self.assertEqual(payload["stage"]["effort"], "max")
        self.assertEqual(payload["stage"]["effort_source"], "user")

    def test_a_refused_mutation_reports_json_and_exits_nonzero(self):
        self.write(LEGACY)
        before = self.text()
        code, out, _ = self.cli("set-config", "stage", "--effort", "ultra", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("ultra", payload["error"])
        self.assertFalse(payload["changed"])
        self.assertEqual(self.text(), before)
        self.assertFalse(
            Path(payload["user_config_path"]).exists(), "a refused preference is never written"
        )

    def test_clearing_through_the_command_line_reaches_the_file(self):
        self.write(LEGACY)
        code, _, _ = self.cli(
            "set-config", "stage", "--model", "claude-opus-5-5", "--effort", "high"
        )
        self.assertEqual(code, 0)
        pref_path = user_config_path()
        self.assertTrue(pref_path.is_file())

        code, _, _ = self.cli("set-config", "stage", "--model-default", "--effort-default")
        self.assertEqual(code, 0)
        _, shown, _ = self.cli("show-config", "--json")
        payload = json.loads(shown)
        self.assertIsNone(payload["stage"]["model"])
        self.assertEqual(payload["stage"]["model_source"], "provider-default")
        self.assertIsNone(payload["stage"]["effort"])
        self.assertEqual(payload["stage"]["effort_source"], "provider-default")
        self.assertFalse(pref_path.exists(), "clearing the only preference removes the file")

    def test_it_says_when_the_file_already_said_this(self):
        self.write(LEGACY)
        self.cli("set-config", "stage", "--model", "claude-opus-5-5")
        code, _, err = self.cli("set-config", "stage", "--model", "claude-opus-5-5")
        self.assertEqual(code, 0)
        self.assertIn("nothing written", err)

    def test_it_creates_a_missing_file_and_says_so(self):
        self.path.unlink(missing_ok=True)
        code, _, err = self.cli("set-config", "stage", "--provider", "claude-cli")
        self.assertEqual(code, 0)
        self.assertIn("created", err)
        self.assertEqual(self.config().stage_agent_provider, "claude-cli")


class LegacyCompatibilityTests(_ConfigDirTestCase):
    """A config with no [agents] table at all still gains only what was
    asked for -- no provider is invented for the role left untouched."""

    def test_a_config_with_no_agents_table_at_all_gains_only_what_was_asked_for(self):
        self.write('project = "demo"\n\n[repo]\nroot = "."\n')
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli"))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        # The other role was not named, so it stays unstated and the engine
        # default applies -- writing one in would be inventing a preference.
        self.assertIsNone(config.sparring_agent_provider)
        self.assertEqual(config.repo_root, ".")


class ProviderChoiceIntrospectionTests(_ConfigDirTestCase):
    """What a UI is told about providers, so it never keeps its own list."""

    def test_show_config_reports_the_providers_implemented_for_each_role(self):
        self.write(LEGACY)
        code, out, _ = self.cli("show-config", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for role in ("stage", "sparring"):
            choices = payload[role]["provider_choices"]
            self.assertTrue(choices, "a role with no offered provider would be unusable")
            self.assertIn(payload[role]["provider"], [choice["provider"] for choice in choices])
            for choice in choices:
                self.assertEqual(
                    set(choice),
                    {
                        "provider",
                        "display_name",
                        "supports_model",
                        "effort_supported",
                        "effort_levels",
                    },
                )

    def test_each_offered_provider_is_one_that_role_would_actually_accept(self):
        self.write(LEGACY)
        for role in (ROLE_STAGE, ROLE_SPARRING):
            for cap in providers_for_role(role):
                with self.subTest(role=role, provider=cap.provider_id):
                    outcome = apply_role_edit(
                        self.sparring_dir, role, RoleEdit(provider=cap.provider_id)
                    )
                    self.assertEqual(outcome.resolved.provider, cap.provider_id)


if __name__ == "__main__":
    unittest.main()
