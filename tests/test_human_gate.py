"""The structured human gate, and the Stage 3D pattern it exists for.

The regression this file is built around is real. Reviewing Stage 3D of the
reported-statistics plan, the sparrer said three true things in one breath:

1. a pre-activation desktop must be tested against a feed containing a
   snapshot v2 payload -- that genuinely blocks the stage;
2. applying the migration in production remains the release owner's action
   *after* acceptance;
3. the rollout gates stay closed until a later release decision.

Mined out of prose, that reads as three human checks and asks somebody to
"pass" two things acceptance does not depend on. Structurally, it is one
check plus two pieces of recorded prose -- which is exactly what the gate
contract produces.
"""

import json
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.human_gate import (
    HUMAN_GATE_CATEGORIES,
    HUMAN_GATE_MARKER,
    HumanCheck,
    HumanGate,
    HumanGateError,
    parse_human_gate,
)
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_agent import _parse_verdict
from agent_sparring.sparring_exchange import render_sparring
from agent_sparring.stage import Stage

# What the reviewer actually returns for the Stage 3D situation described
# above: one blocking check; the deployment and rollout statements live in
# findings/deferred, where they belong.
STAGE_3D_VERDICT = {
    "action": "NEEDS_YOU",
    "summary": "One pre-activation compatibility test is required before this stage is READY.",
    "needs_you_reason": "DEVICE/MANUAL CHECK -- a pre-activation desktop build against a v2 feed",
    "findings": (
        "Snapshot v2 emission, the version-aware comparison and preserve-or-reject on "
        "every transfer path all check out against the diff, and the reader accepts v2 "
        "before anything emits it. What I cannot verify from here is the compatibility "
        "claim itself: a desktop build with the feature not yet activated must be pointed "
        "at a feed that already contains a v2 payload. Separately, and after acceptance: "
        "applying the migration in production remains the release owner's action, and the "
        "two rollout gates stay closed until a later release decision. Neither of those "
        "blocks this stage."
    ),
    "deferred": (
        "Production deployment of the migration, and the rollout/activation decision, "
        "both belong to the release owner after this stage is accepted."
    ),
    "human_gate": {
        "category": "DEVICE_MANUAL_CHECK",
        "title": "A pre-activation desktop must survive a feed containing snapshot v2",
        "checks": [
            {
                "id": "pre-activation-desktop-v2-feed",
                "instruction": (
                    "Run a desktop build with the enhanced-content feature switched off, "
                    "sync it against an account whose feed already contains a snapshot v2 "
                    "reference, and open the reference library."
                ),
                "pass_criteria": (
                    "The library loads, the v2 reference appears with its legacy values, "
                    "and no error or data loss is reported in the sync log."
                ),
                "source": (
                    "docs/plans/active/2026-09-10-reported-statistics-and-range-semantics.md"
                    " > Stage 3D — Snapshot v2 and attachment/export/import transport"
                ),
            }
        ],
    },
}


class HumanGateModelTests(unittest.TestCase):
    def test_categories_are_closed_and_include_an_escape_hatch(self):
        self.assertIn("OTHER", HUMAN_GATE_CATEGORIES)
        self.assertIn("DEVICE_MANUAL_CHECK", HUMAN_GATE_CATEGORIES)

    def test_category_spelling_is_normalised(self):
        for spelling in ("device_manual_check", "DEVICE-MANUAL-CHECK", "device manual check"):
            gate = HumanGate.from_dict(
                {
                    "category": spelling,
                    "title": "t",
                    "checks": [{"id": "a", "instruction": "i", "pass_criteria": "p"}],
                }
            )
            self.assertEqual(gate.category, "DEVICE_MANUAL_CHECK")

    def test_every_check_needs_an_instruction_and_pass_criteria(self):
        for missing in ("id", "instruction", "pass_criteria"):
            check = {"id": "a", "instruction": "i", "pass_criteria": "p"}
            check.pop(missing)
            with self.assertRaises(HumanGateError, msg=missing):
                HumanGate.from_dict({"category": "OTHER", "title": "t", "checks": [check]})

    def test_source_is_optional(self):
        gate = HumanGate.from_dict(
            {
                "category": "OTHER",
                "title": "t",
                "checks": [{"id": "a", "instruction": "i", "pass_criteria": "p"}],
            }
        )
        self.assertIsNone(gate.checks[0].source)

    def test_check_ids_must_be_unique(self):
        with self.assertRaises(HumanGateError) as ctx:
            HumanGate.from_dict(
                {
                    "category": "OTHER",
                    "title": "t",
                    "checks": [
                        {"id": "a", "instruction": "one", "pass_criteria": "p"},
                        {"id": "a", "instruction": "two", "pass_criteria": "p"},
                    ],
                }
            )
        self.assertIn("unique", str(ctx.exception))

    def test_json_round_trip(self):
        gate = HumanGate(
            category="UI_VISUAL_CHECK",
            title="t",
            checks=(HumanCheck(id="a", instruction="i", pass_criteria="p", source="plan.md"),),
        )
        self.assertEqual(parse_human_gate(gate.to_json()), gate)


class Stage3DRegressionTests(unittest.TestCase):
    """Exactly one structured check for the Stage 3D pattern."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stage = Stage.resolve(Path(self._tmp.name) / ".sparring", "stage-3d").create()

    def test_the_verdict_yields_exactly_one_blocking_check(self):
        routing, findings = _parse_verdict(json.dumps(STAGE_3D_VERDICT))

        self.assertIs(routing.action, RoutingAction.NEEDS_YOU)
        gate = routing.human_gate
        assert gate is not None
        self.assertEqual(gate.category, "DEVICE_MANUAL_CHECK")
        self.assertEqual([check.id for check in gate.checks], ["pre-activation-desktop-v2-feed"])

        # The two post-acceptance statements are recorded, and are not checks.
        self.assertIn("release owner", findings)
        self.assertIn("rollout gates", findings)
        self.assertIn("release owner", routing.details["deferred"])
        instructions = " ".join(check.instruction for check in gate.checks).lower()
        self.assertNotIn("deploy", instructions)
        self.assertNotIn("rollout", instructions)

    def test_the_one_check_is_runnable_without_the_plan_open(self):
        routing, _ = _parse_verdict(json.dumps(STAGE_3D_VERDICT))
        assert routing.human_gate is not None
        check = routing.human_gate.checks[0]

        self.assertIn("feature switched off", check.instruction)
        self.assertIn("snapshot v2", check.instruction)
        self.assertIn("no error or data loss", check.pass_criteria)
        self.assertIn("Stage 3D", check.source or "")

    def test_sparring_md_carries_the_gate_as_parsable_json(self):
        routing, findings = _parse_verdict(json.dumps(STAGE_3D_VERDICT))
        content = render_sparring(self.stage, routing, findings=findings)

        self.assertIn(HUMAN_GATE_MARKER, content)
        block = content.split(HUMAN_GATE_MARKER, 1)[1].split("```json", 1)[1].split("```", 1)[0]
        gate = json.loads(block)
        self.assertEqual(len(gate["checks"]), 1)
        self.assertEqual(gate["checks"][0]["id"], "pre-activation-desktop-v2-feed")
        # The prose form is there too, generated from the same object.
        self.assertIn("Required before this stage can be READY", content)
        self.assertIn("Pass when: The library loads", content)

    def test_the_same_verdict_without_a_gate_is_refused(self):
        payload = dict(STAGE_3D_VERDICT, human_gate=None)
        with self.assertRaises(RoutingResultError) as ctx:
            _parse_verdict(json.dumps(payload))
        self.assertIn("human_gate", str(ctx.exception))

    def test_a_ready_verdict_may_not_smuggle_a_gate(self):
        payload = dict(STAGE_3D_VERDICT, action="READY")
        with self.assertRaises(RoutingResultError) as ctx:
            _parse_verdict(json.dumps(payload))
        self.assertIn("must be null", str(ctx.exception))

    def test_a_gate_listing_the_post_acceptance_work_is_still_structurally_valid(self):
        # The scope rule is a prompt rule, not a parser rule -- the parser
        # cannot know that "deploy the migration" is post-acceptance. This
        # test pins that boundary honestly: what the contract *guarantees* is
        # that whatever the reviewer lists is explicit and inspectable, not
        # inferred from a paragraph. See sparring_prompt for the rule itself.
        payload = json.loads(json.dumps(STAGE_3D_VERDICT))
        payload["human_gate"]["checks"].append(
            {
                "id": "deploy",
                "instruction": "Apply the migration in production.",
                "pass_criteria": "It applied.",
                "source": None,
            }
        )
        routing, _ = _parse_verdict(json.dumps(payload))
        assert routing.human_gate is not None
        self.assertEqual(len(routing.human_gate.checks), 2)


class RoutingIntegrationTests(unittest.TestCase):
    def test_construction_requires_the_gate_for_needs_you_only(self):
        gate = HumanGate(
            category="OTHER",
            title="t",
            checks=(HumanCheck(id="a", instruction="i", pass_criteria="p"),),
        )
        RoutingResult(action=RoutingAction.NEEDS_YOU, summary="s", human_gate=gate)
        RoutingResult(action=RoutingAction.READY, summary="s")
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.NEEDS_YOU, summary="s")


if __name__ == "__main__":
    unittest.main()
