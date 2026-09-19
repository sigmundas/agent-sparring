"""The structured human gate carried by a NEEDS_YOU sparring verdict.

NEEDS_YOU used to be prose: the sparrer wrote what a human had to do inside
``findings``, and every consumer that wanted to *show* those checks had to
mine sentences out of Markdown. That guessed. A reviewer who mentioned a
deployment step, a rollout decision and one real device test in the same
paragraph produced three "checks", only one of which actually blocked the
stage.

So the gate is machine-readable, and small:

    category     which kind of human attention is wanted (a closed set)
    title        one line saying why execution stopped
    checks       one or more concrete, runnable items, each with a stable id,
                 an instruction, explicit pass criteria and an optional
                 pointer to where the full test is defined
    instance_id  which *asking* this is -- see below

A check's ``id`` says **what question this is**; the gate's ``instance_id``
says **which turn asked it**. The two are different facts and conflating them
loses a real one.

A reviewer may re-issue a check it has already asked, under the same id, on
purpose: the recorded answer was insufficient, and asking a materially
different question under a new id would throw away the fact that it is the
same subject. A consumer that keys recorded evidence on the check id alone
then treats the earlier answer as satisfying the later asking, and the
re-issued check can never be answered at all. So evidence belongs to a
*gate instance*, not to a check id for all time.

``instance_id`` is minted **by the engine**, in
:func:`~agent_sparring.sparring_exchange.record_sparring`, at the moment a
verdict is written — never by the reviewing agent, which cannot be relied on
to vary it, and which has an obvious incentive not to when it is restating
itself. Whatever an agent puts in this field is discarded and replaced. It is
opaque: nothing orders it, parses it for meaning, or derives anything from
it. Two gates are the same asking exactly when their ``instance_id`` strings
are equal.

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
import re
import uuid
from dataclasses import dataclass, replace
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

# A gate instance id is written into JSON, into Markdown prose, and into the
# evidence lines a UI records in notes.md, and it is compared for equality by
# every consumer. Keeping it to an unambiguous, quote-free, whitespace-free
# alphabet means no consumer ever has to escape or normalise it to compare
# two of them.
_INSTANCE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_MAX_INSTANCE_ID_LENGTH = 128


def new_gate_instance_id() -> str:
    """Mint an identity for one asking of a gate.

    Random rather than a counter. A per-stage ordinal would be derivable only
    from the file about to be overwritten, so it would restart at 1 whenever a
    stage's ``sparring.md`` was reset or recreated -- and a *restarted*
    counter is worse than no identity at all, because evidence recorded
    against the first gate would silently match the first gate of the new
    sequence. Uniqueness is the whole requirement here; ordering is not, since
    nothing sorts gates and the recorded order already lives in ``notes.md``.
    """

    return uuid.uuid4().hex


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
    #: Which asking this is; ``None`` for a gate recorded before gate
    #: instances existed, and for a gate that has not been recorded yet.
    instance_id: str | None = None

    def asked_again(self, instance_id: str) -> "HumanGate":
        """This same gate, as a *new* asking.

        The one way an ``instance_id`` is set. Used by
        :func:`~agent_sparring.sparring_exchange.record_sparring`, which
        discards whatever the reviewing agent supplied.
        """

        return replace(self, instance_id=_valid_instance_id(instance_id))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": self.category,
            "title": self.title,
            "checks": [check.to_dict() for check in self.checks],
        }
        # Omitted rather than null when absent, so a gate recorded before
        # instances existed re-renders byte-identically and a reader can tell
        # "this engine did not mint one" from "this engine minted nothing".
        if self.instance_id is not None:
            payload["instance_id"] = self.instance_id
        return payload

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
        raw_instance = payload.get("instance_id")
        instance_id = None if raw_instance is None else _valid_instance_id(raw_instance)
        return cls(category=category, title=title, checks=checks, instance_id=instance_id)


def _valid_instance_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HumanGateError("human_gate 'instance_id' must be a non-empty string or absent")
    identifier = value.strip()
    if len(identifier) > _MAX_INSTANCE_ID_LENGTH:
        raise HumanGateError(
            f"human_gate instance_id is longer than {_MAX_INSTANCE_ID_LENGTH} characters"
        )
    if not _INSTANCE_ID_RE.match(identifier):
        raise HumanGateError(
            f"human_gate instance_id {identifier!r} must be alphanumerics, '.', '_' or '-'"
        )
    return identifier


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
    "new_gate_instance_id",
    "parse_human_gate",
]
