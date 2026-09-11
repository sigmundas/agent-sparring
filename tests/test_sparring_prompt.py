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

    def test_unrunnable_checks_are_directed_to_deferred_not_needs_you(self):
        """A check the read-only sparrer cannot execute must be routed to the
        ``deferred`` field, not treated as a human decision.

        Exercised by the Stage 7 pilot: the sparrer could not run `npm test`
        (its OS-enforced read-only sandbox refused the runner's temporary
        directories) or `npm run build`, and returned NEEDS_YOU for that
        reason alone -- while also filling in `deferred`, which contradicts
        itself. The candidate was in fact correct and complete. Without this
        guidance the loop stops for the human on essentially every stage
        whose brief asks for full-suite or build evidence.
        """

        self.stage.write_handoff("# Handoff: stage-1\n\nCandidate commit abc123.\n")
        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)

        self.assertIn("Checks you cannot run yourself", prompt)
        self.assertIn(
            "Being unable to reproduce a check yourself is NOT by itself a "
            "reason to choose NEEDS_YOU.",
            " ".join(prompt.split()),
        )
        self.assertIn("deferred", prompt)

    def test_needs_you_prompt_asks_for_a_standard_reason_category(self):
        """The prompt must ask for one of the plan's five standard human-break
        categories in ``needs_you_reason``.

        Exercised by the Stage 7 pilot: ``sparring_exchange`` renders that
        field under a literal "Reason category:" label and
        ``templates.NOTES_TEMPLATE`` tells the reader to name a category, but
        the prompt only ever asked for a free-text "short reason". Both real
        NEEDS_YOU verdicts in the pilot therefore printed prose under a label
        promising a category, and the standard categories never surfaced --
        the second was plainly DEVICE/MANUAL CHECK and never said so.

        Prompt-only by design: the category stays prose, and
        :mod:`agent_sparring.routing` deliberately does not model it as
        machine state.
        """

        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)

        for category in (
            "PRODUCT/PREFERENCE",
            "UI/VISUAL CHECK",
            "DEVICE/MANUAL CHECK",
            "EXTERNAL CONDITION",
            "SCOPE EXPANSION",
        ):
            self.assertIn(category, prompt)

    def test_needs_you_prompt_requires_runnable_manual_checks(self):
        """For UI/VISUAL and DEVICE/MANUAL checks the prompt must forbid bare
        scenario letters and ask for steps plus pass/fail criteria and/or the
        exact plan path and heading.

        Exercised by the Stage 7 pilot: the real DEVICE/MANUAL CHECK verdict
        named the required hardware QA only as plan scenario letters (A/B, E,
        H, I), which is not actionable for a human reading the routing result
        without the plan open.

        Prompt-only by design, like the reason category.
        """

        prompt = build_sparring_prompt(self.stage, self.sparring_dir, resume=False)
        flat = " ".join(prompt.split())

        self.assertIn("Never list only scenario letters", flat)
        self.assertIn("step-by-step instructions plus explicit pass/fail criteria", flat)
        self.assertIn("exact repo-relative plan path and heading", flat)

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
