"""Typed, engine-owned mutation of ``.sparring/project.toml``.

These tests are about what the file looks like afterwards, not only about
what the command returned. A mutation command that reports success while
leaving behind a file no run can load, or one that quietly drops a setting
the person chose, is the failure mode worth spending tests on -- so every
refusal here also asserts that the bytes on disk are unchanged, and every
acceptance asserts that the unrelated parts of the file survived and that
``show-config`` agrees with what was just written.
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
from agent_sparring.config_edit import EDITABLE_FIELDS, RoleEdit, apply_role_edit

# A file with settings this feature must never touch: a comment a person
# wrote, a command table, a sparring default, and the other role.
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


class ModelMutationTests(_ConfigDirTestCase):
    """(1)-(4): set and clear each role's model."""

    def test_setting_the_stage_model_records_it_and_resolves_to_it(self):
        self.write(LEGACY)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet"))
        self.assertTrue(outcome.changed)
        self.assertEqual(self.config().stage_agent_model, "sonnet")
        self.assertEqual(outcome.resolved.model, "sonnet")
        self.assertEqual(outcome.resolved.model_source, "project")

    def test_clearing_the_stage_model_removes_the_key_rather_than_emptying_it(self):
        self.write(HAND_WRITTEN)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(clear_model=True))
        self.assertIsNone(self.config().stage_agent_model)
        # An empty string would be a *stated* preference that config.py
        # refuses to parse; the key has to be gone.
        self.assertNotIn('model = ""', self.text())
        self.assertIsNone(outcome.resolved.model)
        self.assertEqual(outcome.resolved.model_source, "provider-default")

    def test_setting_the_sparring_model_records_it(self):
        self.write(LEGACY)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(model="gpt-5.6-terra"))
        self.assertEqual(self.config().sparring_agent_model, "gpt-5.6-terra")

    def test_clearing_the_sparring_model_removes_the_key(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(clear_model=True))
        self.assertIsNone(self.config().sparring_agent_model)

    def test_a_model_name_is_not_validated_against_any_engine_list(self):
        # Both CLIs take free-form names and gain new ones without an engine
        # release; refusing an unrecognised one here would be the engine
        # claiming knowledge it does not have.
        self.write(LEGACY)
        apply_role_edit(
            self.sparring_dir, ROLE_STAGE, RoleEdit(model="some-model-nobody-has-heard-of-7")
        )
        self.assertEqual(self.config().stage_agent_model, "some-model-nobody-has-heard-of-7")

    def test_an_empty_model_is_refused_rather_than_read_as_clear_it(self):
        self.write(LEGACY)
        before = self.text()
        with self.assertRaises(AgentConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="   "))
        self.assertIn("clear the override", str(caught.exception))
        self.assertEqual(self.text(), before)

    def test_setting_and_clearing_the_same_field_is_a_contradiction(self):
        with self.assertRaises(AgentConfigError):
            RoleEdit(model="opus", clear_model=True)
        with self.assertRaises(AgentConfigError):
            RoleEdit(effort="high", clear_effort=True)


class EffortMutationTests(_ConfigDirTestCase):
    """(5)-(9): each provider's own levels, set and cleared; invalid refused."""

    def test_a_valid_claude_level_is_accepted_for_the_stage_role(self):
        self.write(LEGACY)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(effort="xhigh"))
        self.assertEqual(self.config().stage_agent_effort, "xhigh")
        self.assertEqual(outcome.resolved.effort, "xhigh")

    def test_clearing_the_stage_effort_returns_it_to_the_provider_default(self):
        self.write(HAND_WRITTEN)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(clear_effort=True))
        self.assertIsNone(self.config().stage_agent_effort)
        self.assertIsNone(outcome.resolved.effort)
        self.assertEqual(outcome.resolved.effort_source, "provider-default")

    def test_a_codex_only_level_is_accepted_for_the_sparring_role(self):
        self.write(LEGACY)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(effort="ultra"))
        self.assertEqual(self.config().sparring_agent_effort, "ultra")

    def test_clearing_the_sparring_effort_removes_the_key(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(clear_effort=True))
        self.assertIsNone(self.config().sparring_agent_effort)

    def test_a_level_the_resolved_provider_rejects_does_not_reach_the_file(self):
        # "ultra" is a real level -- for Codex. Claude does not have it, and
        # its CLI would merely warn and run at its default, so the refusal
        # has to happen here.
        self.write(LEGACY)
        before = self.text()
        with self.assertRaises(AgentConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(effort="ultra"))
        self.assertIn("ultra", str(caught.exception))
        self.assertIn("claude-cli", str(caught.exception))
        self.assertEqual(self.text(), before, "a refused edit writes nothing at all")

    def test_a_nonsense_level_is_refused_for_either_role(self):
        self.write(LEGACY)
        before = self.text()
        for role in (ROLE_STAGE, ROLE_SPARRING):
            with self.assertRaises(AgentConfigError):
                apply_role_edit(self.sparring_dir, role, RoleEdit(effort="enormous"))
        self.assertEqual(self.text(), before)

    def test_every_level_the_engine_advertises_is_actually_settable(self):
        # The vocabulary show-config hands a UI and the vocabulary set-config
        # accepts have to be the same one, or the UI offers options that fail.
        for role in (ROLE_STAGE, ROLE_SPARRING):
            for cap in providers_for_role(role):
                for level in cap.effort_levels:
                    with self.subTest(role=role, level=level):
                        self.write(LEGACY)
                        apply_role_edit(self.sparring_dir, role, RoleEdit(effort=level))
                        config = self.config()
                        actual = (
                            config.stage_agent_effort
                            if role == ROLE_STAGE
                            else config.sparring_agent_effort
                        )
                        self.assertEqual(actual, level)


class PreservationTests(_ConfigDirTestCase):
    """(10)-(13): the rest of the file, and the other role, survive intact."""

    def test_unrelated_settings_and_comments_survive_a_mutation(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet"))
        text = self.text()
        self.assertIn("# Notes I keep at the top of this file.", text)
        self.assertIn('test = "pytest -q"   # trailing comment', text)
        self.assertIn('default_mode = "adversarial"', text)
        config = self.config()
        self.assertEqual(config.commands["test"], "pytest -q")
        self.assertEqual(config.default_sparring_mode, "adversarial")
        self.assertEqual(config.repo_root, ".")

    def test_a_stage_mutation_leaves_the_sparring_role_exactly_as_it_was(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet", effort="max"))
        config = self.config()
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        self.assertEqual(config.sparring_agent_model, "gpt-5.6-terra")
        self.assertEqual(config.sparring_agent_effort, "xhigh")

    def test_a_sparring_mutation_leaves_the_stage_role_exactly_as_it_was(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(clear_model=True))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.stage_agent_model, "opus")
        self.assertEqual(config.stage_agent_effort, "high")

    def test_the_file_still_parses_after_a_series_of_mutations(self):
        self.write(HAND_WRITTEN)
        for edit in (
            RoleEdit(model="sonnet"),
            RoleEdit(clear_model=True),
            RoleEdit(effort="low"),
            RoleEdit(clear_effort=True),
            RoleEdit(model="opus", effort="max"),
        ):
            apply_role_edit(self.sparring_dir, ROLE_STAGE, edit)
            parse_project_config(self.text(), source=str(self.path))
        config = self.config()
        self.assertEqual(config.stage_agent_model, "opus")
        self.assertEqual(config.stage_agent_effort, "max")

    def test_an_edit_that_changes_nothing_does_not_rewrite_the_file(self):
        self.write(HAND_WRITTEN)
        before = self.path.stat().st_mtime_ns
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.text(), HAND_WRITTEN)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_a_repeated_clear_is_idempotent(self):
        # What a double-clicked control sends. The second one must not be an
        # error and must not churn the file.
        self.write(HAND_WRITTEN)
        first = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(clear_effort=True))
        second = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(clear_effort=True))
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertIsNone(self.config().stage_agent_effort)

    def test_no_temporary_files_are_left_beside_the_config(self):
        self.write(HAND_WRITTEN)
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet"))
        self.assertEqual(
            sorted(entry.name for entry in self.sparring_dir.iterdir()), ["project.toml"]
        )


class MissingAndMalformedTests(_ConfigDirTestCase):
    """(14)-(15): absent is benign; unreadable is refused, never replaced."""

    def test_a_missing_project_toml_is_created_from_the_engine_template(self):
        self.path.unlink(missing_ok=True)
        outcome = apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(effort="high"))
        self.assertTrue(outcome.created)
        self.assertTrue(outcome.changed)
        config = self.config()
        # The template's own content, not something this module invented.
        self.assertEqual(config.project, "demo")
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")
        self.assertEqual(config.stage_agent_effort, "high")
        # And nothing was guessed for the role that was not being edited.
        self.assertIsNone(config.sparring_agent_model)
        self.assertIsNone(config.sparring_agent_effort)

    def test_a_malformed_file_is_refused_and_left_byte_for_byte_alone(self):
        broken = 'project = "demo\n\n[agents.stage\n'
        self.write(broken)
        with self.assertRaises(ProjectConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        self.assertIn("not valid TOML", str(caught.exception))
        self.assertIn("will not overwrite", str(caught.exception))
        self.assertEqual(self.text(), broken)

    def test_an_existing_file_with_an_unknown_role_key_is_refused_not_repaired(self):
        # Editing one field of a file the parser rejects would "succeed" and
        # leave behind something no run can load.
        odd = 'project = "demo"\n\n[agents.stage]\nprovider = "claude-cli"\nreasoning = "high"\n'
        self.write(odd)
        with self.assertRaises(ProjectConfigError) as caught:
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        self.assertIn("reasoning", str(caught.exception))
        self.assertEqual(self.text(), odd)

    def test_a_write_failure_leaves_the_original_config_intact(self):
        # (17) The atomic write is the only step that touches the file, so a
        # failure in it must leave the previous content completely readable.
        self.write(HAND_WRITTEN)
        with mock.patch("agent_sparring.config_edit.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ProjectConfigError) as caught:
                apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet"))
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual(self.text(), HAND_WRITTEN)
        self.assertEqual(self.config().stage_agent_model, "opus")
        self.assertEqual(
            sorted(entry.name for entry in self.sparring_dir.iterdir()),
            ["project.toml"],
            "the temporary file is cleaned up rather than left beside the real one",
        )

    def test_an_unreadable_directory_is_reported_as_a_configuration_error(self):
        self.write(HAND_WRITTEN)
        mode = self.sparring_dir.stat().st_mode
        os.chmod(self.sparring_dir, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.sparring_dir, mode)
        if os.access(self.sparring_dir, os.W_OK):
            self.skipTest("this filesystem or user ignores the directory write bit")
        with self.assertRaises(ProjectConfigError):
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="sonnet"))
        self.assertEqual(self.text(), HAND_WRITTEN)


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
            '[agents.stage]\nprovider = "claude-cli"\n\n'
            '[commands]\ntest = "pytest -q"\n\n'
            # Back to [agents] after another table: legal TOML, and the
            # shape tomlkit represents with a proxy rather than a Table.
            '[agents.sparring]\nprovider = "codex-cli"\n'
        )
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(effort="ultra"))
        config = self.config()
        self.assertEqual(config.sparring_agent_effort, "ultra")
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertEqual(config.commands["test"], "pytest -q")

    def test_an_inline_agents_table_is_edited_in_place(self):
        self.write(
            'project = "demo"\n'
            'agents = { stage = { provider = "claude-cli" }, '
            'sparring = { provider = "codex-cli" } }\n'
        )
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        config = self.config()
        self.assertEqual(config.stage_agent_model, "opus")
        self.assertEqual(config.sparring_agent_provider, "codex-cli")

    def test_an_agents_value_that_is_not_a_table_is_refused(self):
        self.write('project = "demo"\nagents = "nonsense"\n')
        before = self.text()
        with self.assertRaises(ProjectConfigError):
            apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        self.assertEqual(self.text(), before)


class ProviderMutationTests(_ConfigDirTestCase):
    """(18): a provider change never silently discards an existing value."""

    def test_setting_the_provider_a_role_already_has_is_accepted(self):
        self.write(HAND_WRITTEN)
        outcome = apply_role_edit(
            self.sparring_dir, ROLE_STAGE, RoleEdit(provider="claude-cli", effort="max")
        )
        self.assertEqual(outcome.resolved.provider, "claude-cli")
        self.assertEqual(self.config().stage_agent_effort, "max")

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

    def test_a_provider_change_that_orphans_an_existing_effort_says_so(self):
        # Constructed against the capability table rather than hard-coded, so
        # this keeps testing the real behaviour as providers change: find a
        # role with more than one provider whose levels differ.
        role, source, target, orphan = _orphaning_case()
        if role is None:
            self.skipTest("no role currently has two providers with differing effort levels")
        self.write(
            f'project = "demo"\n\n[agents.{role}]\n'
            f'provider = "{source}"\neffort = "{orphan}"\n'
        )
        before = self.text()
        with self.assertRaises(AgentConfigError) as caught:
            apply_role_edit(self.sparring_dir, role, RoleEdit(provider=target))
        message = str(caught.exception)
        self.assertIn(orphan, message)
        self.assertIn("will not discard it for you", message)
        self.assertEqual(self.text(), before)

    def test_the_same_change_is_accepted_when_the_effort_is_supplied_with_it(self):
        role, source, target, orphan = _orphaning_case()
        if role is None:
            self.skipTest("no role currently has two providers with differing effort levels")
        self.write(
            f'project = "demo"\n\n[agents.{role}]\n'
            f'provider = "{source}"\neffort = "{orphan}"\n'
        )
        replacement = next(
            cap for cap in providers_for_role(role) if cap.provider_id == target
        ).effort_levels[0]
        outcome = apply_role_edit(
            self.sparring_dir, role, RoleEdit(provider=target, effort=replacement)
        )
        self.assertEqual(outcome.resolved.provider, target)
        self.assertEqual(outcome.resolved.effort, replacement)


def _orphaning_case() -> tuple[str | None, str, str, str]:
    """A (role, from, to, level) where moving provider orphans the level.

    Today no role has two providers, so this finds nothing and the tests
    that need it skip. It is written against the capability table rather
    than against today's table so that it starts exercising the behaviour
    the moment a second provider is implemented for a role.
    """

    for role in (ROLE_STAGE, ROLE_SPARRING):
        caps = providers_for_role(role)
        for source in caps:
            for target in caps:
                if source is target:
                    continue
                orphans = set(source.effort_levels) - set(target.effort_levels)
                if orphans:
                    return role, source.provider_id, target.provider_id, sorted(orphans)[0]
    return None, "", "", ""


class SetConfigCommandTests(_ConfigDirTestCase):
    """(16), (19): the CLI contract, its help, and agreement with show-config."""

    def test_the_command_is_documented_in_help(self):
        code, out, _ = self.cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("set-config", out)

    def test_its_own_help_documents_every_operation(self):
        code, out, _ = self.cli("set-config", "--help")
        self.assertEqual(code, 0)
        for flag in ("--provider", "--model", "--model-default", "--effort", "--effort-default"):
            self.assertIn(flag, out)
        self.assertIn("stage", out)
        self.assertIn("sparring", out)
        # The two properties a person most needs to know before running it.
        self.assertIn("atomic", out)
        self.assertIn("next provider turn", out)

    def test_the_role_is_a_closed_choice(self):
        code, _, err = self.cli("set-config", "reviewer", "--model", "opus")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_there_is_no_way_to_set_an_arbitrary_key(self):
        code, _, _ = self.cli("set-config", "stage", "--set", "repo.root=/elsewhere")
        self.assertEqual(code, 2, "an unknown flag is rejected by the parser")

    def test_setting_and_clearing_the_same_field_is_rejected_by_the_parser(self):
        code, _, err = self.cli("set-config", "stage", "--model", "opus", "--model-default")
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
        code, out, _ = self.cli("set-config", "stage", "--model", "opus", "--effort", "max", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["created"])
        self.assertIsNone(payload["error"])

        # (16) The engine is authoritative: what set-config reports and what a
        # fresh show-config reports must be the same answer.
        _, shown, _ = self.cli("show-config", "--json")
        expected = json.loads(shown)
        self.assertEqual(
            {key: payload[key] for key in expected},
            expected,
            "set-config --json must agree with a fresh show-config",
        )
        self.assertEqual(payload["stage"]["model"], "opus")
        self.assertEqual(payload["stage"]["effort"], "max")

    def test_a_refused_mutation_reports_json_and_exits_nonzero(self):
        self.write(LEGACY)
        before = self.text()
        code, out, _ = self.cli("set-config", "stage", "--effort", "ultra", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("ultra", payload["error"])
        self.assertFalse(payload["changed"])
        self.assertEqual(self.text(), before)

    def test_clearing_through_the_command_line_reaches_the_file(self):
        self.write(HAND_WRITTEN)
        code, _, _ = self.cli("set-config", "stage", "--model-default", "--effort-default")
        self.assertEqual(code, 0)
        config = self.config()
        self.assertIsNone(config.stage_agent_model)
        self.assertIsNone(config.stage_agent_effort)

    def test_it_says_when_the_file_already_said_this(self):
        self.write(HAND_WRITTEN)
        code, _, err = self.cli("set-config", "stage", "--model", "opus")
        self.assertEqual(code, 0)
        self.assertIn("nothing written", err)

    def test_it_creates_a_missing_file_and_says_so(self):
        self.path.unlink(missing_ok=True)
        code, _, err = self.cli("set-config", "sparring", "--effort", "ultra")
        self.assertEqual(code, 0)
        self.assertIn("created", err)
        self.assertEqual(self.config().sparring_agent_effort, "ultra")


class LegacyCompatibilityTests(_ConfigDirTestCase):
    """(20): a config written before this feature keeps working."""

    def test_a_providers_only_config_is_read_and_edited_without_complaint(self):
        self.write(LEGACY)
        apply_role_edit(self.sparring_dir, ROLE_SPARRING, RoleEdit(effort="high"))
        config = self.config()
        self.assertEqual(config.stage_agent_provider, "claude-cli")
        self.assertIsNone(config.stage_agent_model)
        self.assertIsNone(config.stage_agent_effort)
        self.assertEqual(config.sparring_agent_effort, "high")

    def test_a_config_with_no_agents_table_at_all_gains_only_what_was_asked_for(self):
        self.write('project = "demo"\n\n[repo]\nroot = "."\n')
        apply_role_edit(self.sparring_dir, ROLE_STAGE, RoleEdit(model="opus"))
        config = self.config()
        self.assertEqual(config.stage_agent_model, "opus")
        # The provider was not named, so it stays unstated and the engine
        # default applies -- writing one in would be inventing a preference.
        self.assertIsNone(config.stage_agent_provider)
        self.assertIsNone(config.sparring_agent_provider)
        self.assertEqual(config.repo_root, ".")

    def test_the_editable_fields_are_exactly_the_parsed_schema(self):
        # If config.py ever learns a fourth [agents.<role>] key, this fails
        # rather than letting the two drift apart in silence.
        from agent_sparring.config import _AGENT_ROLE_KEYS

        self.assertEqual(set(EDITABLE_FIELDS), set(_AGENT_ROLE_KEYS))


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
