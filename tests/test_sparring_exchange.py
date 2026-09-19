import json
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.human_gate import (
    HUMAN_GATE_MARKER,
    HumanCheck,
    HumanGate,
    HumanGateError,
    new_gate_instance_id,
)
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import (
    read_recorded_outcome,
    record_sparring,
    render_sparring,
)
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


class GateInstanceIdentityTests(unittest.TestCase):
    """Recording a verdict is an *asking*, and every asking is identifiable.

    The bug these pin: a reviewer re-issues a check under the same id because
    the recorded answer was insufficient, and a consumer keying evidence on
    the check id treats the old answer as satisfying the new asking — leaving
    a check that can never be answered.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stage = Stage.resolve(Path(self._tmp.name) / ".sparring", "stage-1").create()

    def _gate(self, instance_id=None):
        return HumanGate(
            category="PRODUCT_PREFERENCE",
            title="Choose the batch-attachment acceptance scope",
            checks=(
                HumanCheck(
                    id="batch-attachment-scope",
                    instruction="Record one choice in text.",
                    pass_criteria="An explicit choice is recorded.",
                ),
            ),
            instance_id=instance_id,
        )

    def _needs_you(self, gate):
        return RoutingResult(
            action=RoutingAction.NEEDS_YOU, summary="Choose the scope", human_gate=gate
        )

    def _recorded_gate(self, content):
        block = content.split(HUMAN_GATE_MARKER, 1)[1].split("```json", 1)[1].split("```", 1)[0]
        return json.loads(block)

    def test_recording_mints_an_instance_id(self):
        content = record_sparring(self.stage, self._needs_you(self._gate()))
        payload = self._recorded_gate(content)
        self.assertTrue(payload["instance_id"], "a recorded gate says which asking it is")
        self.assertEqual(payload["checks"][0]["id"], "batch-attachment-scope")

    def test_the_same_gate_asked_twice_gets_two_identities(self):
        first = self._recorded_gate(record_sparring(self.stage, self._needs_you(self._gate())))
        second = self._recorded_gate(record_sparring(self.stage, self._needs_you(self._gate())))
        self.assertNotEqual(
            first["instance_id"],
            second["instance_id"],
            "re-issuing a check word for word is a new asking, not the old one",
        )
        self.assertEqual(first["checks"][0]["id"], second["checks"][0]["id"])

    def test_an_agent_cannot_pin_the_instance_id(self):
        # A reviewer restating itself has every incentive to repeat the id it
        # saw in its own prompt; the engine's is the only one that counts.
        content = record_sparring(self.stage, self._needs_you(self._gate("supplied-by-agent")))
        self.assertNotEqual(self._recorded_gate(content)["instance_id"], "supplied-by-agent")

    def test_the_recorded_id_is_stable_when_read_back(self):
        content = record_sparring(
            self.stage, self._needs_you(self._gate()), instance_id_factory=lambda: "gate-1"
        )
        self.assertEqual(self._recorded_gate(content)["instance_id"], "gate-1")
        outcome = read_recorded_outcome(self.stage)
        self.assertEqual(outcome.human_gate.instance_id, "gate-1")
        # Re-reading is not re-asking: nothing about opening the file changes
        # which asking a person is answering.
        self.assertEqual(read_recorded_outcome(self.stage).human_gate.instance_id, "gate-1")

    def test_the_prose_names_the_instance_too(self):
        content = record_sparring(
            self.stage, self._needs_you(self._gate()), instance_id_factory=lambda: "gate-1"
        )
        self.assertIn("Gate instance: `gate-1`", content)

    def test_a_gate_recorded_before_instances_existed_still_reads(self):
        # Exactly the shape on disk in every run that predates this change.
        legacy = json.dumps(
            {
                "category": "PRODUCT_PREFERENCE",
                "title": "t",
                "checks": [
                    {"id": "c", "instruction": "do it", "pass_criteria": "done", "source": None}
                ],
            },
            indent=2,
        )
        self.stage.write_sparring(
            "# Sparring: stage-1\n\n## Routing outcome\n\n"
            "- Action: `NEEDS_YOU`\n- Summary: s\n\n## NEEDS YOU\n\ns\n\n"
            f"{HUMAN_GATE_MARKER}\n\n```json\n{legacy}\n```\n"
        )
        outcome = read_recorded_outcome(self.stage)
        self.assertEqual(outcome.action, RoutingAction.NEEDS_YOU)
        self.assertIsNone(outcome.human_gate.instance_id, "absent, not invented")
        self.assertEqual(outcome.human_gate.checks[0].id, "c")

    def test_a_gate_without_an_instance_renders_as_it_always_did(self):
        content = render_sparring(self.stage, self._needs_you(self._gate()))
        self.assertNotIn("instance_id", content)
        self.assertNotIn("Gate instance:", content)

    def test_a_malformed_instance_id_reads_as_absent(self):
        # Not an error: this field is not the reviewing agent's to set, and
        # rejecting the verdict because an agent echoed a mangled id out of
        # the sparring.md it was shown would throw away a good verdict over a
        # field that is about to be overwritten anyway.
        for bad in ("has space", "back`tick", "", 7, {"a": 1}, "x" * 129):
            gate = HumanGate.from_dict(
                {
                    "category": "OTHER",
                    "title": "t",
                    "checks": [{"id": "c", "instruction": "i", "pass_criteria": "p"}],
                    "instance_id": bad,
                }
            )
            self.assertIsNone(gate.instance_id, f"{bad!r} is not an identity")

    def test_a_malformed_instance_id_is_still_refused_when_minting(self):
        # What the engine writes is held to the contract, because everything
        # downstream compares these strings for equality.
        for bad in ("has space", "back`tick", "", "x" * 129):
            with self.assertRaises(HumanGateError):
                self._gate().asked_again(bad)

    def test_minted_ids_are_usable_and_distinct(self):
        minted = {new_gate_instance_id() for _ in range(100)}
        self.assertEqual(len(minted), 100)
        for identifier in minted:
            HumanGate.from_dict(
                {
                    "category": "OTHER",
                    "title": "t",
                    "checks": [{"id": "c", "instruction": "i", "pass_criteria": "p"}],
                    "instance_id": identifier,
                }
            )


if __name__ == "__main__":
    unittest.main()
