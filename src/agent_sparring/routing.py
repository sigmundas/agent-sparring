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

from agent_sparring.deferred_gate import DeferredGateError, DeferredHumanGate
from agent_sparring.human_gate import HumanGate, HumanGateError


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
    discussion are not encoded here; they belong in the human-readable
    sparring.md.

    ``human_gate`` is the one exception, and it is deliberately narrow: the
    concrete, runnable things a human must complete **before this stage may
    become READY** (see :mod:`agent_sparring.human_gate`). It is required
    when the action is NEEDS_YOU and must be absent otherwise — a stage sent
    back, ready, or escalated is not waiting on a human check list. Making
    that list structured is what lets a UI render it without mining prose;
    everything that is *not* a blocking human check stays prose.

    ``deferred_human_gate`` is the other half of that decision, and it is
    deliberately a *different field* rather than a flag on the first one (see
    :mod:`agent_sparring.deferred_gate`). It is legal only on READY, and it
    means: no implementation issue blocks this stage, a person still owes
    this verification, and the reviewer has judged that later work may
    continue first. Overloading NEEDS_YOU to mean both "stop now" and "maybe
    later" would make the one distinction every consumer has to act on
    unreadable, so the two are structurally separate and the immediate
    contract above is untouched.

    ``promote_deferred`` names gate instances of obligations already in the
    run's ledger that this reviewer has decided can wait no longer. It is
    allowed with any action, because it is an annotation on the ledger rather
    than a routing decision: the engine stops the run for a promoted
    obligation whatever this stage's own verdict turned out to be.
    """

    action: RoutingAction
    summary: str
    needs_you_reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    human_gate: HumanGate | None = None
    deferred_human_gate: DeferredHumanGate | None = None
    promote_deferred: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, RoutingAction):
            raise RoutingResultError(f"action must be a RoutingAction, got {self.action!r}")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise RoutingResultError("summary must be a non-empty string")
        if self.needs_you_reason is not None and not isinstance(self.needs_you_reason, str):
            raise RoutingResultError("needs_you_reason must be a string if present")
        if not isinstance(self.details, Mapping):
            raise RoutingResultError(f"details must be a mapping, got {self.details!r}")
        if self.human_gate is not None and not isinstance(self.human_gate, HumanGate):
            raise RoutingResultError(
                f"human_gate must be a HumanGate if present, got {self.human_gate!r}"
            )
        if self.action is RoutingAction.NEEDS_YOU:
            if self.human_gate is None:
                raise RoutingResultError(
                    "NEEDS_YOU requires a structured human_gate naming what a human must "
                    "do before this stage can become READY"
                )
        elif self.human_gate is not None:
            raise RoutingResultError(
                f"human_gate must be null for {self.action.value}; only NEEDS_YOU stops "
                "the stage on a human check list"
            )
        if self.deferred_human_gate is not None:
            if not isinstance(self.deferred_human_gate, DeferredHumanGate):
                raise RoutingResultError(
                    "deferred_human_gate must be a DeferredHumanGate if present, got "
                    f"{self.deferred_human_gate!r}"
                )
            if self.action is not RoutingAction.READY:
                raise RoutingResultError(
                    f"deferred_human_gate must be null for {self.action.value}; deferring a "
                    "human check is a statement that nothing about the implementation "
                    "blocks this stage, which only READY makes"
                )
        if not isinstance(self.promote_deferred, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in self.promote_deferred
        ):
            raise RoutingResultError(
                "promote_deferred must be a tuple of non-empty gate instance ids"
            )
        if len(set(self.promote_deferred)) != len(self.promote_deferred):
            raise RoutingResultError("promote_deferred must not repeat a gate instance id")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action.value,
            "summary": self.summary,
        }
        if self.needs_you_reason is not None:
            payload["needs_you_reason"] = self.needs_you_reason
        if self.details:
            payload["details"] = dict(self.details)
        if self.human_gate is not None:
            payload["human_gate"] = self.human_gate.to_dict()
        if self.deferred_human_gate is not None:
            payload["deferred_human_gate"] = self.deferred_human_gate.to_dict()
        if self.promote_deferred:
            payload["promote_deferred"] = list(self.promote_deferred)
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

        details = payload.get("details")
        if details is None:
            details = {}
        elif not isinstance(details, Mapping):
            raise RoutingResultError(
                f"routing result 'details' must be an object, got {details!r}"
            )

        raw_gate = payload.get("human_gate")
        gate: HumanGate | None = None
        if isinstance(raw_gate, HumanGate):
            gate = raw_gate
        elif raw_gate is not None:
            try:
                gate = HumanGate.from_dict(raw_gate)
            except HumanGateError as exc:
                raise RoutingResultError(str(exc)) from exc

        raw_deferred = payload.get("deferred_human_gate")
        deferred_gate: DeferredHumanGate | None = None
        if isinstance(raw_deferred, DeferredHumanGate):
            deferred_gate = raw_deferred
        elif raw_deferred is not None:
            try:
                deferred_gate = DeferredHumanGate.from_dict(raw_deferred)
            except DeferredGateError as exc:
                raise RoutingResultError(str(exc)) from exc

        raw_promote = payload.get("promote_deferred") or ()
        if isinstance(raw_promote, str) or not isinstance(raw_promote, (list, tuple)):
            raise RoutingResultError(
                f"routing result 'promote_deferred' must be an array, got {raw_promote!r}"
            )
        promote = tuple(str(value).strip() for value in raw_promote if str(value).strip())

        return cls(
            action=action,
            summary=summary,
            needs_you_reason=reason,
            details=dict(details),
            human_gate=gate,
            deferred_human_gate=deferred_gate,
            promote_deferred=promote,
        )
