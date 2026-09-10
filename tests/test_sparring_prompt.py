import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import Stage
from agent_sparring.sparring_prompt import build_sparring_prompt


class SparringPromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )

    def test_fresh_prompt_includes_brief_handoff_and_verdict_instructions(self):
        self.stage.write_handoff("# Handoff: stage-1\n\nCandidate commit abc123.\n")
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)

        self.assertIn("Build the widget.", prompt)
        self.assertIn("Candidate commit abc123.", prompt)
        self.assertIn("SEND_BACK", prompt)
        self.assertIn("READY", prompt)
        self.assertIn("NEEDS_YOU", prompt)
        self.assertIn("ESCALATE", prompt)
        self.assertIn("read-only", prompt)
        self.assertNotIn("Your previous sparring exchange", prompt)

    def test_resume_includes_previous_sparring_exchange(self):
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.SEND_BACK, summary="fix the widget"),
            findings="the widget silently drops errors",
        )
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=True)

        self.assertIn("Your previous sparring exchange", prompt)
        self.assertIn("fix the widget", prompt)
        self.assertIn("the widget silently drops errors", prompt)

    def test_expected_branch_is_included_when_given(self):
        prompt = build_sparring_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertIn("feature/x", prompt)

    def test_expected_branch_omitted_when_not_given(self):
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertNotIn("## Branch", prompt)

    def test_missing_handoff_is_reported_not_silently_skipped(self):
        (self.stage.directory / "handoff.md").unlink()
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertIn("(no handoff.md available)", prompt)

    def test_project_markdown_is_included_when_present(self):
        self.sparring_dir.mkdir(parents=True, exist_ok=True)
        (self.sparring_dir / "PROJECT.md").write_text(
            "Use pytest, not unittest, for new tests.", encoding="utf-8"
        )
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertIn("Use pytest, not unittest, for new tests.", prompt)


if __name__ == "__main__":
    unittest.main()
