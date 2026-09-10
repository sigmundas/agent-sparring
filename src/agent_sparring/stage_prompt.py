"""Bounded stage-agent prompt assembly.

Per the project plan, resuming a stage agent must automatically supply the
stage brief, project context, and latest sparring feedback, while keeping
the prompt bounded to the current stage. This module only assembles text;
it does not invoke any provider and does not interpret PROJECT.md's prose.

On resume, the full ``sparring.md`` content is embedded verbatim rather than
re-parsed for one specific heading. ``sparring.md`` is overwritten in full on
each sparring exchange (see :mod:`agent_sparring.sparring_exchange`), so its
current content *is* "the latest exchange" — including both the detailed
"## Finding / discussion" and the "## SEND BACK TO STAGE" instruction.
Embedding it whole avoids building another partial review-history parser.
"""

from __future__ import annotations

from pathlib import Path

from agent_sparring.config import load_project_markdown
from agent_sparring.stage import Stage, StageError


def build_stage_prompt(
    stage: Stage, sparring_dir: Path, *, resume: bool, expected_branch: str
) -> str:
    """Assemble the bounded prompt for one stage-agent turn.

    Always includes the branch the agent must stay on, the stage brief, and
    (if present) PROJECT.md. When ``resume`` is true (a same-session
    follow-up), also includes the full current ``sparring.md`` content.
    """

    parts = [
        f"# Stage: {stage.stage_id}",
        "",
        "## Branch",
        "",
        f"You are operating on branch `{expected_branch}`. Do not switch, "
        "rename, or create a different branch.",
        "",
        "## Stage brief",
        "",
        stage.read_brief().strip(),
    ]

    project_context = load_project_markdown(sparring_dir)
    if project_context and project_context.strip():
        parts += ["", "## Project context", "", project_context.strip()]

    if resume:
        try:
            sparring_text = stage.read_sparring().strip()
        except StageError:
            sparring_text = ""
        parts += [
            "",
            "## Latest sparring exchange",
            "",
            sparring_text or "(no sparring.md available)",
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
