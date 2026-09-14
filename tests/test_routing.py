import unittest

import conftest_path  # noqa: F401

from agent_sparring.human_gate import HumanCheck, HumanGate
from agent_sparring.routing import (
    NEEDS_YOU_REASON_CATEGORIES,
    RoutingAction,
    RoutingResult,
    RoutingResultError,
)

# Every NEEDS_YOU carries one of these: the structured list of what a human
# must finish before the stage can be READY (see agent_sparring.human_gate).
GATE = HumanGate(
    category="PRODUCT_PREFERENCE",
    title="A product choice blocks this stage",
    checks=(
        HumanCheck(
            id="pick-flow",
            instruction="Choose between UX flow A and UX flow B.",
            pass_criteria="One flow is chosen and recorded.",
        ),
    ),
)


class RoutingActionTests(unittest.TestCase):
    def test_all_four_actions_exist(self):
        self.assertEqual(
            {a.value for a in RoutingAction},
            {"SEND_BACK", "READY", "NEEDS_YOU", "ESCALATE"},
        )

    def test_unknown_action_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingAction.from_str("CHANGES_REQUESTED")


class NeedsYouReasonTests(unittest.TestCase):
    def test_recommended_categories_documented(self):
        # These are documentation conventions only, not an enforced closed
        # set — see the module-level comment on NEEDS_YOU_REASON_CATEGORIES.
        self.assertEqual(
            set(NEEDS_YOU_REASON_CATEGORIES),
            {
                "product_preference",
                "ui_visual_check",
                "device_manual_check",
                "external_condition",
                "scope_expansion",
            },
        )

    def test_needs_you_accepts_reason_outside_recommended_categories(self):
        # The router only cares that the action is NEEDS_YOU, not which
        # reason category (if any) was chosen.
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="need a call on something unlisted",
            needs_you_reason="something_unlisted",
            human_gate=GATE,
        )
        self.assertEqual(result.needs_you_reason, "something_unlisted")

    def test_needs_you_reason_must_be_string_if_present(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="pick one of two UX flows",
                needs_you_reason=123,
                human_gate=GATE,
            )


class RoutingResultConstructionTests(unittest.TestCase):
    def test_send_back(self):
        result = RoutingResult(action=RoutingAction.SEND_BACK, summary="missed a guard clause")
        self.assertEqual(result.action, RoutingAction.SEND_BACK)
        self.assertIsNone(result.needs_you_reason)

    def test_ready(self):
        result = RoutingResult(action=RoutingAction.READY, summary="no outstanding issues")
        self.assertEqual(result.action, RoutingAction.READY)

    def test_escalate(self):
        result = RoutingResult(action=RoutingAction.ESCALATE, summary="needs stronger sparring")
        self.assertEqual(result.action, RoutingAction.ESCALATE)

    def test_needs_you_without_reason_is_allowed(self):
        # needs_you_reason is optional lightweight metadata, not required
        # for NEEDS_YOU to be a legal action.
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="pick one of two UX flows",
            human_gate=GATE,
        )
        self.assertIsNone(result.needs_you_reason)

    def test_needs_you_with_reason(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="pick one of two UX flows",
            needs_you_reason="product_preference",
            human_gate=GATE,
        )
        self.assertEqual(result.needs_you_reason, "product_preference")

    def test_empty_summary_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.READY, summary="   ")

    def test_non_string_summary_refused_cleanly(self):
        # Regression: a malformed summary must raise RoutingResultError, not
        # an AttributeError from calling .strip() on a non-string.
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.READY, summary=123)

    def test_direct_construction_non_mapping_details_refused_cleanly(self):
        # Regression: a malformed details value passed directly (not via
        # from_dict) must raise RoutingResultError here, not surface later
        # as a confusing AttributeError from render_sparring treating it
        # as a mapping.
        for bad_details in ([], "", 0):
            with self.assertRaises(RoutingResultError):
                RoutingResult(action=RoutingAction.READY, summary="x", details=bad_details)


class RoutingResultSerializationTests(unittest.TestCase):
    def test_round_trip_without_reason(self):
        result = RoutingResult(action=RoutingAction.SEND_BACK, summary="fix the guard", details={"file": "a.py"})
        payload = result.to_dict()
        self.assertEqual(payload["action"], "SEND_BACK")
        self.assertEqual(payload["details"], {"file": "a.py"})
        self.assertNotIn("needs_you_reason", payload)

        reloaded = RoutingResult.from_dict(payload)
        self.assertEqual(reloaded, result)

    def test_round_trip_with_reason(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="verify on device",
            needs_you_reason="device_manual_check",
            human_gate=GATE,
        )
        payload = result.to_dict()
        reloaded = RoutingResult.from_dict(payload)
        self.assertEqual(reloaded, result)

    def test_from_dict_missing_action_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict({"summary": "x"})

    def test_from_dict_unknown_action_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict({"action": "MAYBE", "summary": "x"})

    def test_from_dict_non_string_summary_refused_cleanly(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict({"action": "READY", "summary": 123})

    def test_from_dict_missing_details_defaults_to_empty(self):
        result = RoutingResult.from_dict({"action": "READY", "summary": "x"})
        self.assertEqual(result.details, {})

    def test_from_dict_none_details_defaults_to_empty(self):
        result = RoutingResult.from_dict({"action": "READY", "summary": "x", "details": None})
        self.assertEqual(result.details, {})

    def test_from_dict_falsey_non_mapping_details_refused(self):
        # Regression: a present-but-falsey non-mapping ([], "", 0) must be
        # rejected, not silently treated as {} by `payload.get(...) or {}`.
        for bad_details in ([], "", 0):
            with self.assertRaises(RoutingResultError):
                RoutingResult.from_dict(
                    {"action": "READY", "summary": "x", "details": bad_details}
                )

    def test_schema_stays_tiny_no_finding_list_required(self):
        # Detailed findings belong in sparring.md, not this schema; the
        # dataclass must not require a findings/tests/checks field to
        # construct a valid result.
        result = RoutingResult(action=RoutingAction.READY, summary="looks good")
        payload = result.to_dict()
        self.assertEqual(set(payload.keys()), {"action", "summary"})

    def test_human_gate_round_trips_structurally(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU, summary="verify on device", human_gate=GATE
        )
        payload = result.to_dict()
        self.assertEqual(payload["human_gate"]["category"], "PRODUCT_PREFERENCE")
        self.assertEqual(len(payload["human_gate"]["checks"]), 1)
        self.assertEqual(RoutingResult.from_dict(payload), result)


class HumanGateContractTests(unittest.TestCase):
    """NEEDS_YOU is the only action that stops on a human check list, and it
    must say what that list is."""

    def test_needs_you_without_a_gate_is_refused(self):
        with self.assertRaises(RoutingResultError) as ctx:
            RoutingResult(action=RoutingAction.NEEDS_YOU, summary="a human must look")
        self.assertIn("human_gate", str(ctx.exception))

    def test_other_actions_must_not_carry_a_gate(self):
        for action in (RoutingAction.READY, RoutingAction.SEND_BACK, RoutingAction.ESCALATE):
            with self.assertRaises(RoutingResultError, msg=action.value):
                RoutingResult(action=action, summary="x", human_gate=GATE)

    def test_from_dict_builds_the_gate_from_plain_json(self):
        result = RoutingResult.from_dict(
            {
                "action": "NEEDS_YOU",
                "summary": "one device check",
                "human_gate": {
                    "category": "DEVICE_MANUAL_CHECK",
                    "title": "A device check blocks this stage",
                    "checks": [
                        {
                            "id": "device",
                            "instruction": "Run it on hardware.",
                            "pass_criteria": "No crash.",
                            "source": "docs/plan.md > Stage 3D",
                        }
                    ],
                },
            }
        )
        assert result.human_gate is not None
        self.assertEqual(result.human_gate.checks[0].source, "docs/plan.md > Stage 3D")

    def test_gate_with_no_checks_is_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict(
                {
                    "action": "NEEDS_YOU",
                    "summary": "x",
                    "human_gate": {"category": "OTHER", "title": "t", "checks": []},
                }
            )

    def test_unknown_category_is_refused_rather_than_guessed(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict(
                {
                    "action": "NEEDS_YOU",
                    "summary": "x",
                    "human_gate": {
                        "category": "DEPLOYMENT",
                        "title": "t",
                        "checks": [
                            {"id": "a", "instruction": "i", "pass_criteria": "p", "source": None}
                        ],
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
