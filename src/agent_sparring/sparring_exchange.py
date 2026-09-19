"""Rendering of human-readable sparring.md content from a routing outcome.

This is a straightforward renderer, not a workflow engine: no review-attempt
counters, verdict ids, or amendment protocol. It only turns one
:class:`~agent_sparring.routing.RoutingResult` plus free-text discussion into
the SEND_BACK/NEEDS_YOU/ESCALATE/READY sections already sketched in
``templates.SPARRING_TEMPLATE``.

The one identity minted here is a human gate's ``instance_id``, and it is
deliberately not any of the above: it identifies *this asking* of the gate so
a consumer can tell a human's answer to it from an answer to an earlier one.
It orders nothing and authorizes nothing. See
:mod:`agent_sparring.human_gate`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Callable

from agent_sparring.deferred_gate import (
    DEFERRED_GATE_MARKER,
    DeferredGateError,
    DeferredHumanGate,
)
from agent_sparring.human_gate import (
    HUMAN_GATE_MARKER,
    HumanGate,
    HumanGateError,
    new_gate_instance_id,
)
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.stage import Stage, StageError

_ACTION_HEADINGS = {
    RoutingAction.SEND_BACK: "## SEND BACK TO STAGE",
    RoutingAction.NEEDS_YOU: "## NEEDS YOU",
    RoutingAction.ESCALATE: "## ESCALATE",
    RoutingAction.READY: "## READY",
}


def render_sparring(stage: Stage, result: RoutingResult, *, findings: str = "") -> str:
    """Render ``sparring.md`` content for one routing outcome.

    Every action section is present; only the section matching
    ``result.action`` is filled in, the rest read "(not applicable)".
    """

    lines = [
        f"# Sparring: {stage.stage_id}",
        "",
        "## Finding / discussion",
        "",
        findings.strip() or "(none recorded)",
        "",
        "## Routing outcome",
        "",
        f"- Action: `{result.action.value}`",
        f"- Summary: {result.summary}",
    ]
    if result.needs_you_reason:
        lines.append(f"- Needs-you reason: {result.needs_you_reason}")
    lines.append("")

    for action, heading in _ACTION_HEADINGS.items():
        lines.append(heading)
        lines.append("")
        if action is result.action:
            body = result.summary
            if action is RoutingAction.NEEDS_YOU:
                reason = result.needs_you_reason or "(reason category not recorded)"
                body = f"{body}\n\nReason category: {reason}"
            lines.append(body)
            if action is RoutingAction.NEEDS_YOU and result.human_gate is not None:
                lines.append("")
                lines.extend(_human_gate_lines(result.human_gate))
            if action is RoutingAction.READY and result.deferred_human_gate is not None:
                lines.append("")
                lines.extend(_deferred_gate_lines(result.deferred_human_gate))
        else:
            lines.append("(not applicable)")
        lines.append("")

    lines.append("## Deferred")
    lines.append("")
    deferred = result.details.get("deferred") if result.details else None
    lines.append(str(deferred).strip() if deferred else "(none recorded)")

    if result.promote_deferred:
        # A ledger annotation, not a routing decision, so it is recorded
        # after the sections rather than inside one. The plan-run state is
        # the authority for what actually happened to those obligations.
        lines += ["", "## Promoted deferred verification", ""]
        for instance_id in result.promote_deferred:
            lines.append(f"- gate instance `{instance_id}` can wait no longer")

    return "\n".join(lines).rstrip() + "\n"


def _human_gate_lines(gate: HumanGate) -> list[str]:
    """The gate, twice: readable prose for a person, then the canonical JSON
    behind :data:`~agent_sparring.human_gate.HUMAN_GATE_MARKER` for anything
    that renders Pass/Fail/Blocked controls.

    The JSON is what a consumer must read; the prose above it is a
    convenience for whoever opens sparring.md directly, and is regenerated
    from the same object, so the two cannot drift. No ``###`` sub-heading is
    used, so the whole gate stays inside the ``## NEEDS YOU`` section for
    every section extractor in this package.
    """

    lines = [
        f"**Required before this stage can be READY** — {gate.category} — {gate.title}",
        "",
    ]
    if gate.instance_id is not None:
        # Shown so a person reading sparring.md, or diffing two of them, can
        # see for themselves that a repeated check is a *new* asking and not
        # the old one lingering. Nothing parses this line; the JSON is the
        # contract.
        lines += [f"Gate instance: `{gate.instance_id}` — answer this asking, not an earlier one.", ""]
    for position, check in enumerate(gate.checks, start=1):
        lines.append(f"{position}. {check.instruction}")
        lines.append(f"   - Pass when: {check.pass_criteria}")
        if check.source:
            lines.append(f"   - Defined in: {check.source}")
        lines.append(f"   - Check id: `{check.id}`")
    lines += ["", HUMAN_GATE_MARKER, "", "```json", gate.to_json(), "```"]
    return lines


def _deferred_gate_lines(deferred: DeferredHumanGate) -> list[str]:
    """The deferred gate, in the same two layers as an immediate one: prose
    for a person, then canonical JSON behind
    :data:`~agent_sparring.deferred_gate.DEFERRED_GATE_MARKER`.

    The prose says plainly that this stage is *not* waiting, and carries the
    reviewer's rationale, because a reader of ``sparring.md`` who cannot see
    why continuing was judged safe has no way to disagree with it.
    """

    lines = [
        f"**Human verification deferred, not waived** — {deferred.category} — "
        f"{deferred.title}",
        "",
        f"Owed by: {deferred.checkpoint}. Nothing about this stage's implementation is "
        "blocked on it, and the plan may not complete until it is answered.",
        "",
        f"Reviewer's rationale for deferring: {deferred.rationale}",
        "",
    ]
    if deferred.instance_id is not None:
        lines += [
            f"Gate instance: `{deferred.instance_id}` — answer this asking, not an earlier one.",
            "",
        ]
    for position, check in enumerate(deferred.checks, start=1):
        lines.append(f"{position}. {check.instruction}")
        lines.append(f"   - Pass when: {check.pass_criteria}")
        if check.source:
            lines.append(f"   - Defined in: {check.source}")
        lines.append(f"   - Check id: `{check.id}`")
    lines += [
        "",
        DEFERRED_GATE_MARKER,
        "",
        "```json",
        json.dumps(deferred.to_dict(), indent=2, ensure_ascii=False),
        "```",
    ]
    return lines


@dataclass(frozen=True)
class SparringRecord:
    """What was written, and the verdict as written.

    ``result`` is not the object the caller passed in: it is that object with
    the engine's freshly minted gate instance(s) substituted. Callers that
    have to act on a gate's identity -- the plan runner recording a deferred
    obligation in its ledger -- need the recorded form, and re-reading the
    file to get it would make the identity depend on a parse that is allowed
    to fail.
    """

    content: str
    result: RoutingResult


def record_sparring(
    stage: Stage,
    result: RoutingResult,
    *,
    findings: str = "",
    instance_id_factory: Callable[[], str] = new_gate_instance_id,
) -> str:
    """Render and write ``sparring.md`` for one routing outcome.

    This is the only place a human gate acquires its ``instance_id``, and it
    always acquires a fresh one — whatever the reviewing agent put in that
    field is discarded. Writing a verdict *is* the act of asking, so every
    write is a new asking, even when the reviewer restates a gate it has
    already issued word for word. That is the case the identity exists for:
    a check re-asked because its recorded answer was insufficient must be
    answerable again, and it cannot be if the answer is attributed to the
    check id alone. See :mod:`agent_sparring.human_gate`.

    Nothing about routing, lifecycle or acceptance reads the id; it only
    tells a consumer which asking a recorded answer belongs to.

    The text that was written. :func:`record_sparring_result` is the same
    call for a caller that also needs the verdict *as recorded*, with the
    minted identities in it.
    """

    return record_sparring_result(
        stage, result, findings=findings, instance_id_factory=instance_id_factory
    ).content


def record_sparring_result(
    stage: Stage,
    result: RoutingResult,
    *,
    findings: str = "",
    instance_id_factory: Callable[[], str] = new_gate_instance_id,
) -> SparringRecord:
    """:func:`record_sparring`, returning the recorded verdict as well.

    A deferred gate is minted here for exactly the same reason an immediate
    one is, and by exactly the same rule: writing the verdict *is* the act of
    asking, so every write is a new asking. A reviewer that restates a
    deferral it already made therefore creates a second obligation rather
    than silently re-identifying the first -- which is correct, because the
    engine cannot tell "I am repeating myself" from "I am asking a materially
    different question under the same words". What keeps that from
    accumulating duplicates is the plan runner, which carries one obligation
    per gate instance and never records the same instance twice.
    """

    if result.human_gate is not None:
        result = replace(result, human_gate=result.human_gate.asked_again(instance_id_factory()))
    if result.deferred_human_gate is not None:
        result = replace(
            result,
            deferred_human_gate=result.deferred_human_gate.asked_again(instance_id_factory()),
        )
    content = render_sparring(stage, result, findings=findings)
    stage.write_sparring(content)
    return SparringRecord(content=content, result=result)


@dataclass(frozen=True)
class RecordedOutcome:
    """The verdict a stage's ``sparring.md`` already holds.

    Not a :class:`~agent_sparring.routing.RoutingResult`: that type is the
    contract a *fresh* verdict must satisfy, and today's contract requires a
    structured ``human_gate`` for NEEDS_YOU. A file written before that
    requirement existed is still a true record of where the stage stopped,
    and refusing to read it would be refusing history. So this is the looser
    thing — what is on disk — and ``human_gate`` is simply absent when the
    file predates structured gates.
    """

    action: RoutingAction
    summary: str
    needs_you_reason: str | None = None
    human_gate: HumanGate | None = None
    #: The deferred obligation this verdict raised, if it raised one. Read
    #: back for display and for a runner reconciling its ledger against what
    #: is actually on disk; it never makes the stage await a human.
    deferred_human_gate: DeferredHumanGate | None = None

    @property
    def awaits_a_human(self) -> bool:
        """Did this stage stop for a person? NEEDS_YOU and ESCALATE both did:
        one asks for a check or a decision, the other for a review this pair
        of agents cannot give. Neither is answered by running an agent
        again."""

        return self.action in (RoutingAction.NEEDS_YOU, RoutingAction.ESCALATE)


def read_recorded_outcome(stage: Stage) -> RecordedOutcome | None:
    """Read back the verdict in ``stage``'s ``sparring.md``, or ``None``.

    The inverse of :func:`render_sparring`, and deliberately only of its
    ``## Routing outcome`` block and the canonical gate JSON -- the prose is
    for people. ``None`` means *nothing can be claimed*: no file, no routing
    block, an action this version does not know, or a gate that will not
    parse. Every caller treats that as "no recorded verdict" and carries on,
    because a half-read verdict must never decide whether an agent runs.
    """

    try:
        text = stage.read_sparring()
    except (StageError, OSError):
        return None
    if not text.strip():
        return None

    action: RoutingAction | None = None
    summary = ""
    reason: str | None = None
    for line in _routing_block(text):
        if line.startswith("- Action:"):
            value = line.split(":", 1)[1].strip().strip("`")
            try:
                action = RoutingAction.from_str(value)
            except RoutingResultError:
                return None
        elif line.startswith("- Summary:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("- Needs-you reason:"):
            reason = line.split(":", 1)[1].strip() or None
    if action is None:
        return None
    return RecordedOutcome(
        action=action,
        summary=summary,
        needs_you_reason=reason,
        human_gate=_recorded_gate(text),
        deferred_human_gate=_recorded_deferred_gate(text),
    )


def _routing_block(text: str) -> list[str]:
    """The lines under ``## Routing outcome``, up to the next heading."""

    lines = text.splitlines()
    try:
        start = lines.index("## Routing outcome") + 1
    except ValueError:
        return []
    block: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        block.append(line.strip())
    return block


def _recorded_gate(text: str) -> HumanGate | None:
    """The canonical JSON block behind :data:`HUMAN_GATE_MARKER`, if the file
    has one and it still parses."""

    body = _marked_json(text, HUMAN_GATE_MARKER)
    if body is None:
        return None
    try:
        return HumanGate.from_dict(json.loads(body))
    except (json.JSONDecodeError, HumanGateError, TypeError, AttributeError):
        return None


def _recorded_deferred_gate(text: str) -> DeferredHumanGate | None:
    """The canonical JSON block behind
    :data:`~agent_sparring.deferred_gate.DEFERRED_GATE_MARKER`, if the file
    has one and it still parses. A file written before deferrals existed has
    neither, and reads as ``None`` -- which is exactly right for it."""

    body = _marked_json(text, DEFERRED_GATE_MARKER)
    if body is None:
        return None
    try:
        return DeferredHumanGate.from_dict(json.loads(body))
    except (json.JSONDecodeError, DeferredGateError, TypeError, AttributeError):
        return None


def _marked_json(text: str, marker: str) -> str | None:
    """The body of the first ```` ```json ```` fence after ``marker``."""

    at = text.find(marker)
    if at < 0:
        return None
    after = text[at + len(marker) :]
    opening = after.find("```json")
    if opening < 0:
        return None
    body = after[opening + len("```json") :]
    closing = body.find("```")
    return None if closing < 0 else body[:closing]
