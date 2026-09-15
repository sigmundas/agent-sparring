"""The captured prompt is the prompt that was sent.

The point of every test here is that a reader of a captured prompt is
reading what a provider actually received, not a plausible reconstruction of
it. So the central assertions compare the bytes on disk against the bytes a
fake adapter was handed, rather than against another call to the builder --
comparing two builders would only prove the builders agree with themselves.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.loop import run_unattended_loop
from agent_sparring.prompt_capture import (
    INDEX_FILENAME,
    is_prompt_artifact,
    prompts_dir,
)
from agent_sparring.prompt_sections import (
    ORIGIN_ENGINE,
    ORIGIN_FILE,
    TURN_EVIDENCE_REVIEW,
    TURN_FINALIZATION,
    TURN_ORIGINAL,
    TURN_RESUME,
)
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.sparring_agent import run_sparring_agent
from agent_sparring.sparring_prompt import assemble_sparring_prompt
from agent_sparring.stage import Stage
from agent_sparring.stage_agent import run_stage_agent
from agent_sparring.stage_prompt import assemble_stage_prompt


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _verdict_text(action: str, summary: str) -> str:
    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": None,
            "findings": summary,
            "deferred": None,
            "human_gate": None,
        }
    )


class _RecordingStageAdapter:
    """Records every prompt string it is handed, verbatim."""

    def __init__(self, *, session_id: str = "impl-sess"):
        self.session_id = session_id
        self.prompts: list[str] = []

    def start(self, prompt: str) -> StageAgentResult:
        self.prompts.append(prompt)
        return StageAgentResult(session_id=self.session_id, text="did it", is_error=False)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.prompts.append(prompt)
        return StageAgentResult(session_id=self.session_id, text="did more", is_error=False)


class _RecordingSparringAdapter:
    def __init__(self, verdicts: list[str], *, session_id: str = "spar-sess"):
        self.session_id = session_id
        self._verdicts = list(verdicts)
        self._index = 0
        self.prompts: list[str] = []

    def _next(self) -> str:
        text = self._verdicts[min(self._index, len(self._verdicts) - 1)]
        self._index += 1
        return text

    def start(self, prompt: str) -> SparringAgentResult:
        self.prompts.append(prompt)
        return SparringAgentResult(session_id=self.session_id, text=self._next(), is_error=False)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.prompts.append(prompt)
        return SparringAgentResult(session_id=self.session_id, text=self._next(), is_error=False)


class _CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nDo the thing.\n", encoding="utf-8"
        )

    @property
    def captures(self) -> list[Path]:
        directory = prompts_dir(self.stage.directory)
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.iterdir() if p.suffix == ".md")

    def index_entries(self) -> list[dict]:
        path = prompts_dir(self.stage.directory) / INDEX_FILENAME
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class CaptureFidelityTests(_CaptureTestCase):
    def test_the_captured_file_is_byte_identical_to_what_the_adapter_received(self):
        """The whole contract, stated once.

        Not "the capture matches a fresh build of the prompt" -- that would
        pass even if both were wrong in the same way. The adapter is the
        provider's stand-in, and what it was handed is by definition what
        was sent.
        """

        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(adapter.prompts), 1)
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.captures[0].read_text(encoding="utf-8"), adapter.prompts[0])

    def test_the_sparrers_captured_file_is_byte_identical_too(self):
        self.stage.write_handoff("# Handoff: stage-1\n\nCandidate commit abc123.\n")
        adapter = _RecordingSparringAdapter([_verdict_text("READY", "fine")])
        run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.captures[0].read_text(encoding="utf-8"), adapter.prompts[0])

    def test_a_resumed_turn_captures_the_prompt_that_resume_was_given(self):
        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )
        self.stage.write_sparring("# Sparring: stage-1\n\n## SEND BACK TO STAGE\n\nFix the edge.\n")
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(adapter.prompts), 2)
        self.assertEqual(len(self.captures), 2)
        self.assertEqual(self.captures[1].read_text(encoding="utf-8"), adapter.prompts[1])
        # And the resumed turn really did carry the new sparring exchange,
        # which is the thing a re-render after the fact would get wrong.
        self.assertIn("Fix the edge.", adapter.prompts[1])
        self.assertNotIn("Fix the edge.", adapter.prompts[0])


class ImmutabilityTests(_CaptureTestCase):
    def test_a_later_turn_never_rewrites_an_earlier_capture(self):
        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )
        first = self.captures[0]
        first_bytes = first.read_bytes()

        self.stage.write_sparring("# Sparring: stage-1\n\n## SEND BACK TO STAGE\n\nAgain.\n")
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(self.captures), 2)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertNotEqual(self.captures[1].read_bytes(), first_bytes)

    def test_sequence_numbers_are_shared_across_roles_and_never_reused(self):
        self.stage.write_handoff("# Handoff: stage-1\n\nCandidate.\n")
        stage_adapter = _RecordingStageAdapter()
        sparring_adapter = _RecordingSparringAdapter([_verdict_text("READY", "fine")])
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, stage_adapter, expected_branch="feature/x"
        )
        run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, sparring_adapter, expected_branch="feature/x"
        )

        names = [p.name for p in self.captures]
        self.assertEqual(names, ["0001-stage-original.md", "0002-sparrer-original.md"])

    def test_an_existing_capture_is_stepped_over_rather_than_replaced(self):
        """A file already occupying the next sequence number is never opened
        for writing -- the capture takes the following number instead."""

        directory = prompts_dir(self.stage.directory)
        directory.mkdir(parents=True, exist_ok=True)
        squatter = directory / "0001-stage-original.md"
        squatter.write_text("not mine\n", encoding="utf-8")

        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(squatter.read_text(encoding="utf-8"), "not mine\n")
        self.assertIn("0002-stage-original.md", [p.name for p in self.captures])


class SectionTests(_CaptureTestCase):
    def test_the_sections_slice_the_captured_prompt_exactly(self):
        """Every recorded span addresses its own section's text in the
        captured file, so a reader slices the real bytes instead of
        re-parsing them for headings."""

        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        text = self.captures[0].read_text(encoding="utf-8")
        entry = self.index_entries()[0]
        assembled = assemble_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        self.assertEqual(len(entry["sections"]), len(assembled.sections))
        for recorded, part in zip(entry["sections"], assembled.sections):
            self.assertEqual(text[recorded["start"] : recorded["end"]], part.text)

    def test_the_joined_sections_are_the_whole_prompt(self):
        assembled = assemble_stage_prompt(
            self.stage,
            self.sparring_dir,
            resume=False,
            expected_branch="feature/x",
            self_check=True,
        )
        text = assembled.text
        covered = "".join(text[start:end] for start, end in assembled.spans())
        # Only the separators between sections and the final newline are not
        # part of any section.
        self.assertEqual(len(text) - len(covered), 2 * (len(assembled.sections) - 1) + 1)
        self.assertTrue(text.endswith("\n"))

    def test_engine_framing_is_recorded_as_engine_authored(self):
        """The sections that are this engine's own words say so.

        This is what makes a stage whose brief describes a review while the
        framing around it demands an implementation legible at a glance,
        instead of the two being indistinguishable prose.
        """

        assembled = assemble_stage_prompt(
            self.stage,
            self.sparring_dir,
            resume=False,
            expected_branch="feature/x",
            self_check=True,
        )
        by_heading = {part.heading: part for part in assembled.sections}

        self.assertEqual(by_heading["Scope reminder"].origin, ORIGIN_ENGINE)
        self.assertIsNone(by_heading["Scope reminder"].source)
        self.assertEqual(by_heading["Self-check"].origin, ORIGIN_ENGINE)
        self.assertEqual(by_heading["Branch"].origin, ORIGIN_ENGINE)

        self.assertEqual(by_heading["Stage brief"].origin, ORIGIN_FILE)
        self.assertEqual(by_heading["Stage brief"].source, "stages/stage-1/brief.md")

    def test_project_context_is_sourced_to_the_project_file(self):
        (self.sparring_dir / "PROJECT.md").write_text("# Project\n\nKnowledge.\n", encoding="utf-8")
        assembled = assemble_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        by_heading = {part.heading: part for part in assembled.sections}
        self.assertEqual(by_heading["Project context"].source, "PROJECT.md")

    def test_the_instruction_about_human_evidence_is_not_attributed_to_notes(self):
        """notes.md holds the human's words; the sentence telling an agent
        what to do with them is the engine's, and is recorded separately so
        the file is never credited with text it does not contain."""

        self.stage.append_note("## Human evidence", "I checked it on the device. It renders.")
        assembled = assemble_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        by_heading = {part.heading: part for part in assembled.sections}

        self.assertEqual(by_heading["Human evidence"].source, "stages/stage-1/notes.md")
        self.assertIn("It renders.", by_heading["Human evidence"].text)
        self.assertEqual(by_heading["How to use the human evidence"].origin, ORIGIN_ENGINE)
        self.assertIsNone(by_heading["How to use the human evidence"].source)


class TurnKindTests(_CaptureTestCase):
    def test_stage_turn_kinds(self):
        original = assemble_stage_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x"
        )
        resumed = assemble_stage_prompt(
            self.stage, self.sparring_dir, resume=True, expected_branch="feature/x"
        )
        finalizing = assemble_stage_prompt(
            self.stage,
            self.sparring_dir,
            resume=True,
            expected_branch="feature/x",
            finalize_only=True,
        )

        self.assertEqual(original.turn_kind, TURN_ORIGINAL)
        self.assertEqual(resumed.turn_kind, TURN_RESUME)
        self.assertEqual(finalizing.turn_kind, TURN_FINALIZATION)

    def test_a_sparring_turn_answering_a_human_is_labelled_as_such(self):
        assembled = assemble_sparring_prompt(
            self.stage,
            self.sparring_dir,
            resume=True,
            expected_branch="feature/x",
            evidence_first=True,
        )
        self.assertEqual(assembled.turn_kind, TURN_EVIDENCE_REVIEW)

    def test_the_loop_labels_its_evidence_resume_turn(self):
        """End to end: the loop is the only thing that knows this sparring
        turn is answering a human, and what it knows reaches the capture."""

        self.stage.write_handoff("# Handoff: stage-1\n\nCandidate.\n")
        self.stage.append_note("## Human evidence", "Checked on hardware; it works.")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")

        stage_adapter = _RecordingStageAdapter()
        sparring_adapter = _RecordingSparringAdapter([_verdict_text("READY", "evidence accepted")])
        run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
            start_with="sparring",
        )

        entries = self.index_entries()
        self.assertEqual(entries[0]["role"], "sparrer")
        self.assertEqual(entries[0]["turn_kind"], TURN_EVIDENCE_REVIEW)
        self.assertEqual(self.captures[0].name, "0001-sparrer-evidence_review.md")


class IndexTests(_CaptureTestCase):
    def test_the_index_describes_each_capture(self):
        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        entry = self.index_entries()[0]
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["role"], "stage")
        self.assertEqual(entry["stage_id"], "stage-1")
        self.assertEqual(entry["turn_kind"], TURN_ORIGINAL)
        self.assertIs(entry["resumed"], False)
        self.assertEqual(entry["expected_branch"], "feature/x")
        self.assertEqual(entry["file"], self.captures[0].name)
        self.assertEqual(entry["chars"], len(adapter.prompts[0]))

    def test_a_lost_index_does_not_cause_an_overwrite(self):
        """The sequence is read from the directory, so the index is a
        convenience and never the authority on what already exists."""

        adapter = _RecordingStageAdapter()
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )
        first_bytes = self.captures[0].read_bytes()
        (prompts_dir(self.stage.directory) / INDEX_FILENAME).unlink()

        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(self.captures), 2)
        self.assertEqual(self.captures[0].read_bytes(), first_bytes)


class NeverFatalTests(_CaptureTestCase):
    def test_a_turn_still_runs_when_the_capture_cannot_be_written(self):
        """A read-only stage directory costs the inspector, not the run."""

        directory = prompts_dir(self.stage.directory)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o500)
        self.addCleanup(directory.chmod, 0o700)

        adapter = _RecordingStageAdapter()
        result = run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(adapter.prompts), 1)
        self.assertFalse(result.result.is_error)
        self.assertEqual(self.captures, [])


class ArtifactRecognitionTests(unittest.TestCase):
    def test_captured_names_are_recognised(self):
        self.assertTrue(is_prompt_artifact("0001-stage-original.md"))
        self.assertTrue(is_prompt_artifact("0042-sparrer-evidence_review.md"))
        self.assertTrue(is_prompt_artifact("0003-stage-finalization.md"))
        self.assertTrue(is_prompt_artifact(INDEX_FILENAME))

    def test_anything_else_is_not(self):
        """A stray file in prompts/ must still block a freeze, so this
        recognises exact shapes rather than a directory."""

        for name in (
            "secrets.env",
            "notes.md",
            "1-stage-original.md",
            "0001-other-original.md",
            "0001-stage-original.txt",
            "0001-stage-Original.md",
            "../escape.md",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_prompt_artifact(name))


if __name__ == "__main__":
    unittest.main()
