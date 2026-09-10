import unittest

import conftest_path  # noqa: F401

from agent_sparring.routing import (
    NEEDS_YOU_REASON_CATEGORIES,
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
        )
        self.assertEqual(result.needs_you_reason, "something_unlisted")

    def test_needs_you_reason_must_be_string_if_present(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="pick one of two UX flows",
                needs_you_reason=123,
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
        result = RoutingResult(action=RoutingAction.NEEDS_YOU, summary="pick one of two UX flows")
        self.assertIsNone(result.needs_you_reason)

    def test_needs_you_with_reason(self):
        result = RoutingResult(
            action=RoutingAction.NEEDS_YOU,
            summary="pick one of two UX flows",
            needs_you_reason="product_preference",
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

    def test_schema_stays_tiny_no_finding_list_required(self):
        # Detailed findings belong in sparring.md, not this schema; the
        # dataclass must not require a findings/tests/checks field to
        # construct a valid result.
        result = RoutingResult(action=RoutingAction.READY, summary="looks good")
        payload = result.to_dict()
        self.assertEqual(set(payload.keys()), {"action", "summary"})


if __name__ == "__main__":
    unittest.main()
