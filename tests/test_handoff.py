import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.handoff import (
    HandoffInput,
    build_self_contained_packet,
    generate_handoff,
    render_thin_handoff,
)
from agent_sparring.git_context import gather_git_context
from agent_sparring.stage import Stage, StageState


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run(self.repo, "init", "-q", "-b", "main")
        _run(self.repo, "config", "user.email", "test@example.com")
        _run(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        _run(self.repo, "add", "a.txt")
        _run(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        (self.repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        _run(self.repo, "add", "a.txt")
        _run(self.repo, "commit", "-q", "-m", "candidate")
        self.candidate_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def _git_context(self, **kwargs):
        kwargs.setdefault("check_pushed", False)
        return gather_git_context(
            self.repo, base_sha=self.base_sha, candidate_sha=self.candidate_sha, **kwargs
        )

    def test_thin_handoff_contains_identity_and_no_diff(self):
        handoff_input = HandoffInput(
            stage_goal="Implement the thing.",
            claims="I implemented the thing and tests pass.",
            git=self._git_context(),
            test_evidence="pytest: 5 passed",
        )
        content = render_thin_handoff(self.stage, handoff_input)
        self.assertIn("stage-1", content)
        self.assertIn(self.base_sha, content)
        self.assertIn(self.candidate_sha, content)
        self.assertIn("a.txt", content)
        self.assertIn("pytest: 5 passed", content)
        self.assertNotIn("+one", content)  # no diff hunk lines
        self.assertNotIn("```diff", content)

    def test_self_contained_packet_embeds_diff(self):
        handoff_input = HandoffInput(
            stage_goal="Implement the thing.",
            claims="Done.",
            git=self._git_context(),
        )
        content = build_self_contained_packet(self.stage, handoff_input, self.repo)
        self.assertIn("```diff", content)
        self.assertIn("+two", content)

    def test_self_contained_without_base_reports_unavailable(self):
        git = gather_git_context(self.repo, candidate_sha=self.candidate_sha, check_pushed=False)
        handoff_input = HandoffInput(stage_goal="g", claims="c", git=git)
        content = build_self_contained_packet(self.stage, handoff_input, self.repo)
        self.assertIn("cannot compute a diff", content)

    def test_open_checks_pulled_from_notes_when_filled_in(self):
        self.stage.write_notes(
            "# Notes\n\n## Implementation notes\n\nsome notes\n\n"
            "## Deferred checks\n\nManual QA on staging.\n"
        )
        handoff_input = HandoffInput(stage_goal="g", claims="c", git=self._git_context())
        content = render_thin_handoff(self.stage, handoff_input)
        self.assertIn("Manual QA on staging.", content)

    def test_open_checks_placeholder_notes_reported_as_none(self):
        # Fresh stage.create() leaves notes.md as the unedited template.
        handoff_input = HandoffInput(stage_goal="g", claims="c", git=self._git_context())
        content = render_thin_handoff(self.stage, handoff_input)
        self.assertIn("(none recorded)", content)

    def test_previous_sparring_findings_pulled_when_filled_in(self):
        self.stage.write_sparring(
            "# Sparring\n\n## SEND BACK TO STAGE\n\nFix the off-by-one bug.\n"
        )
        handoff_input = HandoffInput(stage_goal="g", claims="c", git=self._git_context())
        content = render_thin_handoff(self.stage, handoff_input)
        self.assertIn("Fix the off-by-one bug.", content)

    def test_generate_handoff_writes_file_and_uses_stage_state(self):
        self.stage.write_state(
            StageState(base_sha=self.base_sha, candidate_sha=self.candidate_sha)
        )
        content = generate_handoff(
            self.stage,
            self.repo,
            stage_goal="Implement the thing.",
            claims="Done.",
            check_pushed=False,
        )
        self.assertEqual(self.stage.read_handoff(), content)
        self.assertIn(self.candidate_sha, content)


if __name__ == "__main__":
    unittest.main()
