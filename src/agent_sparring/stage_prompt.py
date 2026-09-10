"""Bounded stage-agent prompt assembly.

Per the project plan, resuming a stage agent must automatically supply the
stage brief, project context, and latest sparring feedback, while keeping
the prompt bounded to the current stage. This module only assembles text;
it does not invoke any provider and does not interpret PROJECT.md's prose.
"""

from __future__ import annotations

from pathlib import Path

from agent_sparring.config import load_project_markdown
from agent_sparring.stage import Stage, StageError

_SEND_BACK_HEADING = "## send back to stage"


def _extract_send_back(sparring_text: str) -> str | None:
    """Best-effort body of sparring.md's "## SEND BACK TO STAGE" section.

    Returns ``None`` if the heading is missing or its body is still the
    unedited template placeholder (parenthesized prose).
    """

    lines = sparring_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == _SEND_BACK_HEADING:
            start = index + 1
            break
    if start is None:
        return None

    body_lines: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body or body.startswith("("):
        return None
    return body


def build_stage_prompt(stage: Stage, sparring_dir: Path, *, resume: bool) -> str:
    """Assemble the bounded prompt for one stage-agent turn.

    Always includes the stage brief and, if present, PROJECT.md. When
    ``resume`` is true (a same-session SEND_BACK follow-up), also includes
    the latest recorded "## SEND BACK TO STAGE" feedback, if any.
    """

    parts = [f"# Stage: {stage.stage_id}", "", "## Stage brief", "", stage.read_brief().strip()]

    project_context = load_project_markdown(sparring_dir)
    if project_context and project_context.strip():
        parts += ["", "## Project context", "", project_context.strip()]

    if resume:
        try:
            sparring_text = stage.read_sparring()
        except StageError:
            sparring_text = ""
        send_back = _extract_send_back(sparring_text)
        parts += [
            "",
            "## Latest sparring feedback (SEND_BACK)",
            "",
            send_back or "(no SEND_BACK feedback recorded; resuming for another reason)",
        ]

    parts += [
        "",
        "## Scope reminder",
        "",
        "Stay within this stage's bounded goal above. Do not expand scope, "
        "start a new stage, or launch another top-level implementation or "
        "sparring agent.",
    ]

    return "\n".join(parts).rstrip() + "\n"


__all__ = ["build_stage_prompt"]
