"""The structured human gate carried by a NEEDS_YOU sparring verdict.

NEEDS_YOU used to be prose: the sparrer wrote what a human had to do inside
``findings``, and every consumer that wanted to *show* those checks had to
mine sentences out of Markdown. That guessed. A reviewer who mentioned a
deployment step, a rollout decision and one real device test in the same
paragraph produced three "checks", only one of which actually blocked the
stage.

So the gate is machine-readable, and small:

    category   which kind of human attention is wanted (a closed set)
    title      one line saying why execution stopped
    checks     one or more concrete, runnable items, each with a stable id,
               an instruction, explicit pass criteria and an optional
               pointer to where the full test is defined

The rule that makes the list meaningful is a *scope* rule, enforced by the
prompt rather than by code (see :mod:`agent_sparring.sparring_prompt`): a
check belongs here only if it must be completed **before this stage may
become READY**. Post-acceptance deployment, release-owner actions, rollout
decisions that come after acceptance, future monitoring, and checks the
read-only reviewer merely could not run itself are not gates -- they belong
in ``deferred``/``findings``, which stay rich human-readable prose.

This module owns the model and its validation only. Rendering into
``sparring.md`` lives in :mod:`agent_sparring.sparring_exchange`; the
routing envelope that carries it is
:class:`~agent_sparring.routing.RoutingResult`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

# The marker that precedes the canonical JSON block in sparring.md. An HTML
# comment so it is invisible in every Markdown renderer, and a version so a
# later shape change is detectable rather than silently misparsed.
HUMAN_GATE_MARKER = "<!-- human-gate:v1 -->"

# Which kind of human attention a gate asks for. Closed, so a consumer can
# present it without interpreting prose; OTHER exists so a reviewer facing
# something genuinely different says so instead of forcing a bad fit.
HUMAN_GATE_CATEGORIES = (
    "PRODUCT_PREFERENCE",
    "UI_VISUAL_CHECK",
    "DEVICE_MANUAL_CHECK",
    "EXTERNAL_CONDITION",
    "SCOPE_EXPANSION",
    "OTHER",
)

_MAX_ID_LENGTH = 128


class HumanGateError(ValueError):
    """Raised for a malformed or out-of-contract human gate."""


@dataclass(frozen=True)
class HumanCheck:
    """One thing a human must actually do before the stage can be READY.

    ``id`` is stable across sparring turns for the same check, so a recorded
    Pass/Fail/Blocked outcome survives the reviewer restating its gate.
    ``instruction`` is runnable by someone who does not have the plan open;
    ``pass_criteria`` says what counts as passing; ``source`` optionally
    points at the plan heading or path where the full test is defined.
    """

    id: str
    instruction: str
    pass_criteria: str
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "instruction": self.instruction,
            "pass_criteria": self.pass_criteria,
        }
        payload["source"] = self.source
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "HumanCheck":
        if not isinstance(payload, Mapping):
            raise HumanGateError(f"a human-gate check must be an object, got {payload!r}")
        identifier = _required_text(payload, "id", "check")
        if len(identifier) > _MAX_ID_LENGTH:
            raise HumanGateError(
                f"human-gate check id is longer than {_MAX_ID_LENGTH} characters: {identifier!r}"
            )
        source = payload.get("source")
        if source is not None and not isinstance(source, str):
            raise HumanGateError("human-gate check 'source' must be a string or null")
        return cls(
            id=identifier,
            instruction=_required_text(payload, "instruction", "check"),
            pass_criteria=_required_text(payload, "pass_criteria", "check"),
            source=source.strip() if isinstance(source, str) and source.strip() else None,
        )


@dataclass(frozen=True)
class HumanGate:
    """Everything a human must complete before this stage may become READY."""

    category: str
    title: str
    checks: tuple[HumanCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "title": self.title,
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        """The canonical JSON block embedded in ``sparring.md``."""

        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: Any) -> "HumanGate":
        if not isinstance(payload, Mapping):
            raise HumanGateError(f"human_gate must be an object, got {payload!r}")
        category = _required_text(payload, "category", "human_gate").upper().replace("-", "_")
        category = category.replace(" ", "_").replace("/", "_")
        if category not in HUMAN_GATE_CATEGORIES:
            valid = ", ".join(HUMAN_GATE_CATEGORIES)
            raise HumanGateError(
                f"human_gate category {category!r} is not one of: {valid}"
            )
        title = _required_text(payload, "title", "human_gate")
        raw_checks = payload.get("checks")
        if not isinstance(raw_checks, (list, tuple)) or not raw_checks:
            raise HumanGateError(
                "human_gate must carry a non-empty 'checks' array; a NEEDS_YOU with "
                "nothing concrete for a human to do is not a gate"
            )
        checks = tuple(HumanCheck.from_dict(entry) for entry in raw_checks)
        seen: set[str] = set()
        for check in checks:
            if check.id in seen:
                raise HumanGateError(
                    f"human-gate check ids must be unique within a gate; {check.id!r} repeats"
                )
            seen.add(check.id)
        return cls(category=category, title=title, checks=checks)


def _required_text(payload: Mapping[str, Any], key: str, what: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HumanGateError(f"{what} field {key!r} must be a non-empty string")
    return value.strip()


def parse_human_gate(text: str) -> HumanGate:
    """Parse a human gate from its canonical JSON text."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HumanGateError(f"human_gate is not valid JSON: {exc}") from exc
    return HumanGate.from_dict(payload)


__all__ = [
    "HUMAN_GATE_CATEGORIES",
    "HUMAN_GATE_MARKER",
    "HumanCheck",
    "HumanGate",
    "HumanGateError",
    "parse_human_gate",
]
