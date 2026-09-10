"""Sparring routing outcome model.

This is deliberately a routing protocol, not a formal review/verdict system.
The sparrer produces one small structured result when it is ready to hand
control elsewhere; detailed findings belong in human-readable ``sparring.md``,
not in this schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RoutingResultError(ValueError):
    """Raised for an unknown/invalid routing action or malformed payload."""


class RoutingAction(str, Enum):
    """Where control should go next after a sparring pass."""

    SEND_BACK = "SEND_BACK"
    READY = "READY"
    NEEDS_YOU = "NEEDS_YOU"
    ESCALATE = "ESCALATE"

    @classmethod
    def from_str(cls, value: str) -> "RoutingAction":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise RoutingResultError(
                f"unknown routing action {value!r}; expected one of: {valid}"
            ) from exc


# Recommended human-break categories for NEEDS_YOU, per the project plan:
# product/preference, UI/visual check, device/manual check, external
# condition, scope expansion. These are documentation conventions for
# sparring.md prose, not a closed set enforced here — the router only cares
# that the action is NEEDS_YOU, not which category (if any) was chosen.
NEEDS_YOU_REASON_CATEGORIES = (
    "product_preference",
    "ui_visual_check",
    "device_manual_check",
    "external_condition",
    "scope_expansion",
)


@dataclass(frozen=True)
class RoutingResult:
    """A tiny, machine-readable sparring routing outcome.

    ``needs_you_reason`` is optional, freeform metadata (see
    ``NEEDS_YOU_REASON_CATEGORIES`` for suggested values) — it is never
    required or validated against a closed set. Detailed findings/tests/
    checks are not encoded here; they belong in the human-readable
    sparring.md.
    """

    action: RoutingAction
    summary: str
    needs_you_reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, RoutingAction):
            raise RoutingResultError(f"action must be a RoutingAction, got {self.action!r}")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise RoutingResultError("summary must be a non-empty string")
        if self.needs_you_reason is not None and not isinstance(self.needs_you_reason, str):
            raise RoutingResultError("needs_you_reason must be a string if present")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action.value,
            "summary": self.summary,
        }
        if self.needs_you_reason is not None:
            payload["needs_you_reason"] = self.needs_you_reason
        if self.details:
            payload["details"] = dict(self.details)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RoutingResult":
        if "action" not in payload:
            raise RoutingResultError("routing result is missing 'action'")
        action = RoutingAction.from_str(str(payload["action"]))

        if "summary" not in payload:
            raise RoutingResultError("routing result is missing 'summary'")
        summary = payload["summary"]

        reason = payload.get("needs_you_reason")

        details = payload.get("details") or {}
        if not isinstance(details, Mapping):
            raise RoutingResultError("routing result 'details' must be an object")

        return cls(
            action=action,
            summary=summary,
            needs_you_reason=reason,
            details=dict(details),
        )
