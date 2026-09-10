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


class NeedsYouReason(str, Enum):
    """Broad human-interruption reason categories.

    These categories are informational only; they must not become workflow
    states in their own right.
    """

    PRODUCT_PREFERENCE = "product_preference"
    UI_VISUAL_CHECK = "ui_visual_check"
    DEVICE_MANUAL_CHECK = "device_manual_check"
    EXTERNAL_CONDITION = "external_condition"
    SCOPE_EXPANSION = "scope_expansion"

    @classmethod
    def from_str(cls, value: str) -> "NeedsYouReason":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise RoutingResultError(
                f"unknown NEEDS_YOU reason {value!r}; expected one of: {valid}"
            ) from exc


@dataclass(frozen=True)
class RoutingResult:
    """A tiny, machine-readable sparring routing outcome.

    ``needs_you_reason`` is only meaningful (and required) when
    ``action`` is ``NEEDS_YOU``. Detailed findings/tests/checks are not
    encoded here; they belong in the human-readable sparring.md.
    """

    action: RoutingAction
    summary: str
    needs_you_reason: NeedsYouReason | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, RoutingAction):
            raise RoutingResultError(f"action must be a RoutingAction, got {self.action!r}")
        if not self.summary or not self.summary.strip():
            raise RoutingResultError("summary must be a non-empty string")
        if self.action is RoutingAction.NEEDS_YOU:
            if self.needs_you_reason is None:
                raise RoutingResultError(
                    "NEEDS_YOU requires a needs_you_reason"
                )
        elif self.needs_you_reason is not None:
            raise RoutingResultError(
                "needs_you_reason is only valid when action is NEEDS_YOU"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action.value,
            "summary": self.summary,
        }
        if self.needs_you_reason is not None:
            payload["needs_you_reason"] = self.needs_you_reason.value
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

        reason_raw = payload.get("needs_you_reason")
        reason = NeedsYouReason.from_str(str(reason_raw)) if reason_raw is not None else None

        details = payload.get("details") or {}
        if not isinstance(details, Mapping):
            raise RoutingResultError("routing result 'details' must be an object")

        return cls(
            action=action,
            summary=summary,
            needs_you_reason=reason,
            details=dict(details),
        )
