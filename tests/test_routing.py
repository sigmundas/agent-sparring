import unittest

import conftest_path  # noqa: F401

from sparring_v2.routing import (
    NeedsYouReason,
    RoutingAction,
    RoutingResult,
    RoutingResultError,
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
    def test_all_categories_exist(self):
        self.assertEqual(
            {r.value for r in NeedsYouReason},
            {
                "product_preference",
                "ui_visual_check",
                "device_manual_check",
                "external_condition",
                "scope_expansion",
            },
        )

    def test_unknown_reason_refused(self):
        with self.assertRaises(RoutingResultError):
            NeedsYouReason.from_str("random_reason")


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

    def test_needs_you_requires_reason(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.NEEDS_YOU, summary="pick one of two UX flows")

    def test_needs_you_with_reason(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="pick one of two UX flows",
            needs_you_reason=NeedsYouReason.PRODUCT_PREFERENCE,
        )
        self.assertEqual(result.needs_you_reason, NeedsYouReason.PRODUCT_PREFERENCE)

    def test_reason_only_valid_for_needs_you(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(
                action=RoutingAction.READY,
                summary="all good",
                needs_you_reason=NeedsYouReason.SCOPE_EXPANSION,
            )

    def test_empty_summary_refused(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.READY, summary="   ")


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
            needs_you_reason=NeedsYouReason.DEVICE_MANUAL_CHECK,
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

    def test_schema_stays_tiny_no_finding_list_required(self):
        # Detailed findings belong in sparring.md, not this schema; the
        # dataclass must not require a findings/tests/checks field to
        # construct a valid result.
        result = RoutingResult(action=RoutingAction.READY, summary="looks good")
        payload = result.to_dict()
        self.assertEqual(set(payload.keys()), {"action", "summary"})


if __name__ == "__main__":
    unittest.main()
