"""Rendering of human-readable sparring.md content from a routing outcome.

This is a straightforward renderer, not a workflow engine: no review-attempt
counters, immutable verdict ids, or amendment protocol. It only turns one
:class:`~agent_sparring.routing.RoutingResult` plus free-text discussion into
the SEND_BACK/NEEDS_YOU/ESCALATE/READY sections already sketched in
``templates.SPARRING_TEMPLATE``.
"""

from __future__ import annotations

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
        else:
            lines.append("(not applicable)")
        lines.append("")

    lines.append("## Deferred")
    lines.append("")
    deferred = result.details.get("deferred") if result.details else None
    lines.append(str(deferred).strip() if deferred else "(none recorded)")

    return "\n".join(lines).rstrip() + "\n"


def record_sparring(stage: Stage, result: RoutingResult, *, findings: str = "") -> str:
    """Render and write ``sparring.md`` for one routing outcome."""

    content = render_sparring(stage, result, findings=findings)
    stage.write_sparring(content)
    return content
