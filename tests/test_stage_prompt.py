import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.stage import Stage
from agent_sparring.stage_prompt import build_stage_prompt


class StagePromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_fresh_prompt_includes_brief_but_no_send_back_section(self):
        self.stage.write_notes("")
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )
        prompt = build_stage_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertIn("Build the widget.", prompt)
        self.assertNotIn("SEND_BACK", prompt)
        self.assertIn("Scope reminder", prompt)

    def test_resume_with_no_recorded_send_back_uses_fallback_text(self):
        prompt = build_stage_prompt(self.stage, self.sparring_dir, resume=True)
        self.assertIn("Latest sparring feedback", prompt)
        self.assertIn("no SEND_BACK feedback recorded", prompt)

    def test_resume_includes_recorded_send_back_body(self):
        self.stage.write_sparring(
            "# Sparring: stage-1\n\n"
            "## SEND BACK TO STAGE\n\n"
            "Add a regression test for the empty-input case.\n\n"
            "## NEEDS YOU\n\n(none)\n"
        )
        prompt = build_stage_prompt(self.stage, self.sparring_dir, resume=True)
        self.assertIn("Add a regression test for the empty-input case.", prompt)

    def test_project_markdown_is_included_when_present(self):
        self.sparring_dir.mkdir(parents=True, exist_ok=True)
        (self.sparring_dir / "PROJECT.md").write_text(
            "Use pytest, not unittest, for new tests.", encoding="utf-8"
        )
        prompt = build_stage_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertIn("Use pytest, not unittest, for new tests.", prompt)

    def test_project_markdown_absent_is_silently_skipped(self):
        prompt = build_stage_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertNotIn("Project context", prompt)


if __name__ == "__main__":
    unittest.main()
