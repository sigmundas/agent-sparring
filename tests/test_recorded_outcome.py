"""Reading back the verdict a stage's ``sparring.md`` already holds.

The engine writes ``sparring.md`` and, until now, never read it. It has to
in one place: a sequence that was driven by hand up to a NEEDS_YOU can be
adopted into a managed run, and the run must recognise that the stage is
already stopped for a person instead of starting an agent on it. These tests
hold the reader's two properties -- it reads what the renderer writes, and it
says "nothing" rather than half-read anything.
"""

import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.human_gate import HumanCheck, HumanGate
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import read_recorded_outcome, record_sparring
from agent_sparring.stage import Stage

GATE = HumanGate(
    category="DEVICE_MANUAL_CHECK",
    title="A pre-activation desktop must accept a feed containing snapshot v2",
    checks=(
        HumanCheck(
            id="desktop-v2-feed",
            instruction="Point a pre-activation desktop at a feed containing a v2 row.",
            pass_criteria="The desktop consumes the feed and does not reject it.",
            source="Required regression matrix",
        ),
    ),
)


class RecordedOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-3d-snapshot-v2").create(
            brief="# Stage brief\n\nBody.\n"
        )

    def test_reads_back_exactly_what_the_renderer_wrote(self):
        record_sparring(
            self.stage,
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="Only the desktop compatibility gate remains.",
                needs_you_reason="DEVICE/MANUAL CHECK -- desktop",
                human_gate=GATE,
            ),
            findings="Long human-readable review prose.",
        )

        recorded = read_recorded_outcome(self.stage)

        assert recorded is not None
        self.assertIs(recorded.action, RoutingAction.NEEDS_YOU)
        self.assertEqual(recorded.summary, "Only the desktop compatibility gate remains.")
        self.assertEqual(recorded.needs_you_reason, "DEVICE/MANUAL CHECK -- desktop")
        self.assertEqual(recorded.human_gate, GATE)
        self.assertTrue(recorded.awaits_a_human)

    def test_reads_a_verdict_recorded_before_structured_gates_existed(self):
        # The real Stage 3D was reviewed before the gate was part of the
        # envelope. Its file is still a true record of where the stage
        # stopped, and refusing to read it would be refusing history.
        legacy = "\n".join(
            [
                "# Sparring: stage-3d-snapshot-v2",
                "",
                "## Finding / discussion",
                "",
                "Both repository candidates form a coherent implementation.",
                "",
                "## Routing outcome",
                "",
                "- Action: `NEEDS_YOU`",
                "- Summary: Only the real-desktop compatibility gate remains.",
                "- Needs-you reason: DEVICE/MANUAL CHECK -- desktop",
                "",
                "## NEEDS YOU",
                "",
                "Verify a pre-activation desktop can consume a feed containing v2.",
                "",
            ]
        )
        self.stage.write_sparring(legacy)

        recorded = read_recorded_outcome(self.stage)

        assert recorded is not None
        self.assertIs(recorded.action, RoutingAction.NEEDS_YOU)
        self.assertIsNone(recorded.human_gate)
        self.assertTrue(recorded.awaits_a_human)

    def test_send_back_and_ready_are_not_waiting_for_a_person(self):
        for action, waiting in ((RoutingAction.SEND_BACK, False), (RoutingAction.READY, False), (RoutingAction.ESCALATE, True)):
            with self.subTest(action=action):
                record_sparring(self.stage, RoutingResult(action=action, summary="s"))
                recorded = read_recorded_outcome(self.stage)
                assert recorded is not None
                self.assertIs(recorded.action, action)
                self.assertEqual(recorded.awaits_a_human, waiting)

    def test_says_nothing_rather_than_half_reading(self):
        # Every one of these must read as "no recorded verdict": a partial
        # answer here would decide whether an agent runs.
        self.assertIsNone(read_recorded_outcome(self.stage), "no sparring.md at all")

        self.stage.write_sparring("")
        self.assertIsNone(read_recorded_outcome(self.stage), "empty file")

        self.stage.write_sparring("# Sparring\n\nSomebody's notes, no routing block.\n")
        self.assertIsNone(read_recorded_outcome(self.stage), "no routing outcome")

        self.stage.write_sparring("## Routing outcome\n\n- Action: `MAYBE`\n- Summary: s\n")
        self.assertIsNone(read_recorded_outcome(self.stage), "an action this version does not know")

    def test_a_gate_that_will_not_parse_leaves_the_verdict_readable_without_one(self):
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.NEEDS_YOU, summary="s", human_gate=GATE),
        )
        broken = self.stage.read_sparring().replace('"checks"', '"chekcs"')
        self.stage.write_sparring(broken)

        recorded = read_recorded_outcome(self.stage)

        assert recorded is not None
        self.assertIs(recorded.action, RoutingAction.NEEDS_YOU)
        self.assertIsNone(recorded.human_gate, "the gate is dropped, the pause is not")
        self.assertTrue(recorded.awaits_a_human)


if __name__ == "__main__":
    unittest.main()
