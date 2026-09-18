import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import artifact_ownership
from agent_sparring.artifact_ownership import (
    ARTIFACTS,
    PROVIDER_WRITABLE,
    ROLE_SPARRER,
    ROLE_STAGE,
    ownership_section,
)
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.stage import Stage
from agent_sparring.stage_prompt import build_stage_prompt


class OwnershipDeclarationTests(unittest.TestCase):
    def test_no_managed_run_artifact_is_provider_writable(self):
        # The whole point of the declaration: there is no provider-write
        # surface, so gaining one must be a deliberate edit to this tuple
        # rather than a side effect of some new prompt sentence.
        self.assertEqual(PROVIDER_WRITABLE, ())
        for artifact in ARTIFACTS:
            self.assertFalse(
                artifact.provider_writable, f"{artifact.path} is declared provider-writable"
            )

    def test_notes_md_is_declared_engine_owned_not_an_agent_scratchpad(self):
        notes = [a for a in ARTIFACTS if a.path.endswith("notes.md")]
        self.assertEqual(len(notes), 1)
        self.assertIn("engine", notes[0].owner)
        self.assertFalse(notes[0].provider_writable)

    def test_the_source_plan_is_declared_immutable_for_a_run(self):
        self.assertIn("immutable", artifact_ownership.SOURCE_PLAN.lifetime)
        self.assertFalse(artifact_ownership.SOURCE_PLAN.provider_writable)

    def test_every_engine_artifact_lives_under_the_sparring_directory(self):
        # A relative sparring path or a repository document, never an
        # absolute path: the table is read next to a real project.
        for artifact in ARTIFACTS:
            self.assertFalse(artifact.path.startswith("/"), artifact.path)

    def test_an_unknown_role_is_refused_rather_than_given_a_default(self):
        with self.assertRaises(ValueError):
            ownership_section("implementer")

    def test_each_role_is_told_where_its_own_reporting_belongs(self):
        stage_text = "\n".join(ownership_section(ROLE_STAGE))
        sparrer_text = "\n".join(ownership_section(ROLE_SPARRER))
        self.assertIn("this turn's own reply", stage_text)
        self.assertIn("structured result this prompt asks you for", sparrer_text)


def _assert_plan_is_read_only(case: unittest.TestCase, prompt: str) -> None:
    """The restriction every managed prompt must carry."""

    case.assertIn("read-only input", prompt)
    case.assertIn("do not edit or annotate it", prompt)
    case.assertIn("do not append an implementation record", prompt)
    case.assertIn("do not mark stages complete in it, renumber stages", prompt)
    case.assertIn("is written by the engine", prompt)
    case.assertIn("No Agent Sparring artifact is yours to write", prompt)


class StagePromptOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def _prompt(self, **kwargs) -> str:
        return build_stage_prompt(
            self.stage, self.sparring_dir, expected_branch="feature/x", **kwargs
        )

    def test_original_prompt_tells_the_agent_not_to_edit_the_source_plan(self):
        prompt = self._prompt(resume=False)
        _assert_plan_is_read_only(self, prompt)
        self.assertIn("this turn's own reply", prompt)

    def test_resume_prompt_retains_the_restriction(self):
        _assert_plan_is_read_only(self, self._prompt(resume=True))

    def test_self_check_prompt_retains_the_restriction(self):
        _assert_plan_is_read_only(self, self._prompt(resume=False, self_check=True))

    def test_the_restriction_precedes_the_turn_instructions(self):
        prompt = self._prompt(resume=False)
        self.assertLess(prompt.index("read-only input"), prompt.index("## Scope reminder"))


class SparringPromptOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_sparring_prompt_treats_the_source_plan_as_read_only(self):
        prompt = build_sparring_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        _assert_plan_is_read_only(self, prompt)
        self.assertIn("structured result this prompt asks you for", prompt)

    def test_resumed_sparring_prompt_retains_the_restriction(self):
        _assert_plan_is_read_only(
            self,
            build_sparring_prompt(
                self.stage, self.sparring_dir, resume=True, expected_branch="feature/x"
            ),
        )

    def test_the_restriction_precedes_the_verdict_instructions(self):
        prompt = build_sparring_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertLess(prompt.index("read-only input"), prompt.index("## Your task"))


if __name__ == "__main__":
    unittest.main()
