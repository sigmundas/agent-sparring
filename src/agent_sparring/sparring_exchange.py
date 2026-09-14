"""Rendering of human-readable sparring.md content from a routing outcome.

This is a straightforward renderer, not a workflow engine: no review-attempt
counters, immutable verdict ids, or amendment protocol. It only turns one
:class:`~agent_sparring.routing.RoutingResult` plus free-text discussion into
the SEND_BACK/NEEDS_YOU/ESCALATE/READY sections already sketched in
``templates.SPARRING_TEMPLATE``.
"""

from __future__ import annotations

from agent_sparring.human_gate import HUMAN_GATE_MARKER, HumanGate
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.stage import Stage

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
