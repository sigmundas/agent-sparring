import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import Stage
from agent_sparring.stage_prompt import build_stage_prompt


class StagePromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_fresh_prompt_includes_branch_and_brief_but_no_sparring_section(self):
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )
        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertIn("Build the widget.", prompt)
        self.assertIn("feature/x", prompt)
        self.assertIn("Do not switch", prompt)
        self.assertNotIn("Latest sparring exchange", prompt)
        self.assertIn("Scope reminder", prompt)

    def test_resume_with_untouched_sparring_template_still_includes_section(self):
        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=True, expected_branch="feature/x"
        )
        self.assertIn("Latest sparring exchange", prompt)

    def test_resume_includes_both_detailed_finding_and_send_back_correction(self):
        finding = (
            "The empty-input branch silently swallows a ValueError raised by "
            "the parser instead of surfacing it, which will hide real parse "
            "failures from the caller in production."
        )
        send_back = "Add a regression test for the empty-input case."
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.SEND_BACK, summary=send_back),
            findings=finding,
        )

        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=True, expected_branch="feature/x"
        )
        self.assertIn(finding, prompt)
        self.assertIn(send_back, prompt)

    def test_project_markdown_is_included_when_present(self):
        self.sparring_dir.mkdir(parents=True, exist_ok=True)
        (self.sparring_dir / "PROJECT.md").write_text(
            "Use pytest, not unittest, for new tests.", encoding="utf-8"
        )
        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertIn("Use pytest, not unittest, for new tests.", prompt)

    def test_project_markdown_absent_is_silently_skipped(self):
        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertNotIn("Project context", prompt)

    def test_self_check_false_omits_section(self):
        prompt = build_stage_prompt(
            self.stage,
            self.sparring_dir,
            resume=False,
            expected_branch="feature/x",
            self_check=False,
        )
        self.assertNotIn("Self-check", prompt)

    def test_self_check_defaults_to_false(self):
        prompt = build_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertNotIn("Self-check", prompt)

    def test_self_check_true_includes_section_with_all_checkpoints(self):
        prompt = build_stage_prompt(
            self.stage,
            self.sparring_dir,
            resume=False,
            expected_branch="feature/x",
            self_check=True,
        )
        self.assertIn("Self-check", prompt)
        self.assertIn("failure between steps", prompt)
        self.assertIn("resume/retry behavior", prompt)
        self.assertIn("stale or partially written state", prompt)
        self.assertIn("provider/runtime differences", prompt)
        self.assertIn("concurrency issues", prompt)
        self.assertIn("invariants can be bypassed", prompt)
        self.assertIn("exercise the real tool", prompt)


if __name__ == "__main__":
    unittest.main()
