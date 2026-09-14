import json
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.human_gate import HUMAN_GATE_MARKER, HumanCheck, HumanGate
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring, render_sparring
from agent_sparring.stage import Stage


class SparringExchangeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(sparring_dir, "stage-1").create()

    def test_send_back_fills_only_its_section(self):
        result = RoutingResult(action=RoutingAction.SEND_BACK, summary="Fix the bug in foo()")
        content = render_sparring(self.stage, result, findings="foo() double-counts")
        self.assertIn("## SEND BACK TO STAGE", content)
        self.assertIn("Fix the bug in foo()", content)
        self.assertIn("foo() double-counts", content)
        for other in ("## NEEDS YOU", "## ESCALATE", "## READY"):
            section = content.split(other, 1)[1].split("##", 1)[0]
            self.assertIn("(not applicable)", section)

    def test_needs_you_includes_reason(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="Pick a color scheme",
            needs_you_reason="product_preference",
            human_gate=HumanGate(
                category="PRODUCT_PREFERENCE",
                title="A colour choice blocks this stage",
                checks=(
                    HumanCheck(
                        id="pick-scheme",
                        instruction="Open the settings screen and choose between the two schemes.",
                        pass_criteria="One scheme is chosen and recorded.",
                        source="docs/plan.md > Stage 2",
                    ),
                ),
            ),
        )
        content = render_sparring(self.stage, result)
        section = content.split("## NEEDS YOU", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Pick a color scheme", section)
        self.assertIn("product_preference", section)
        # Readable prose for a person...
        self.assertIn("Required before this stage can be READY", section)
        self.assertIn("Open the settings screen", section)
        self.assertIn("Pass when: One scheme is chosen", section)
        self.assertIn("Defined in: docs/plan.md > Stage 2", section)
        # ...and the canonical JSON any UI renders its controls from.
        self.assertIn(HUMAN_GATE_MARKER, section)
        block = section.split(HUMAN_GATE_MARKER, 1)[1].split("```json", 1)[1].split("```", 1)[0]
        gate = json.loads(block)
        self.assertEqual(gate["category"], "PRODUCT_PREFERENCE")
        self.assertEqual([check["id"] for check in gate["checks"]], ["pick-scheme"])

    def test_the_gate_stays_inside_its_own_section(self):
        # No '###' sub-heading: handoff.py's section extractor stops at any
        # line starting with '#', so a sub-heading would cut the gate out of
        # the handoff's "previous sparring findings".
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="one check",
            human_gate=HumanGate(
                category="OTHER",
                title="t",
                checks=(HumanCheck(id="a", instruction="do it", pass_criteria="done"),),
            ),
        )
        content = render_sparring(self.stage, result)
        section = content.split("## NEEDS YOU", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("\n#", section.replace("\n## ", ""))

    def test_escalate_fills_its_section(self):
        result = RoutingResult(action=RoutingAction.ESCALATE, summary="Needs GPT web review")
        content = render_sparring(self.stage, result)
        section = content.split("## ESCALATE", 1)[1].split("##", 1)[0]
        self.assertIn("Needs GPT web review", section)

    def test_ready_fills_its_section(self):
        result = RoutingResult(action=RoutingAction.READY, summary="No issues found")
        content = render_sparring(self.stage, result)
        section = content.split("## READY", 1)[1].split("##", 1)[0]
        self.assertIn("No issues found", section)

    def test_deferred_from_details(self):
        result = RoutingResult(
            action=RoutingAction.READY,
            summary="ready",
            details={"deferred": "Load test deferred until Stage 5"},
        )
        content = render_sparring(self.stage, result)
        self.assertIn("Load test deferred until Stage 5", content)

    def test_record_sparring_writes_file(self):
        result = RoutingResult(action=RoutingAction.READY, summary="ready")
        content = record_sparring(self.stage, result)
        self.assertEqual(self.stage.read_sparring(), content)


if __name__ == "__main__":
    unittest.main()
