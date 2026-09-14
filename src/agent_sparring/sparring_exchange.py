"""Rendering of human-readable sparring.md content from a routing outcome.

This is a straightforward renderer, not a workflow engine: no review-attempt
counters, immutable verdict ids, or amendment protocol. It only turns one
:class:`~agent_sparring.routing.RoutingResult` plus free-text discussion into
the SEND_BACK/NEEDS_YOU/ESCALATE/READY sections already sketched in
``templates.SPARRING_TEMPLATE``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_sparring.human_gate import HUMAN_GATE_MARKER, HumanGate, HumanGateError
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
        else:
            lines.append("(not applicable)")
        lines.append("")

    lines.append("## Deferred")
    lines.append("")
    deferred = result.details.get("deferred") if result.details else None
    lines.append(str(deferred).strip() if deferred else "(none recorded)")

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
    for position, check in enumerate(gate.checks, start=1):
        lines.append(f"{position}. {check.instruction}")
        lines.append(f"   - Pass when: {check.pass_criteria}")
        if check.source:
            lines.append(f"   - Defined in: {check.source}")
        lines.append(f"   - Check id: `{check.id}`")
    lines += ["", HUMAN_GATE_MARKER, "", "```json", gate.to_json(), "```"]
    return lines


def record_sparring(stage: Stage, result: RoutingResult, *, findings: str = "") -> str:
    """Render and write ``sparring.md`` for one routing outcome."""

    content = render_sparring(stage, result, findings=findings)
    stage.write_sparring(content)
    return content


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

    marker = text.find(HUMAN_GATE_MARKER)
    if marker < 0:
        return None
    after = text[marker + len(HUMAN_GATE_MARKER) :]
    opening = after.find("```json")
    if opening < 0:
        return None
    body = after[opening + len("```json") :]
    closing = body.find("```")
    if closing < 0:
        return None
    try:
        return HumanGate.from_dict(json.loads(body[:closing]))
    except (json.JSONDecodeError, HumanGateError, TypeError, AttributeError):
        return None
