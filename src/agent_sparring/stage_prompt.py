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
from agent_sparring.handoff import human_evidence_section
from agent_sparring.stage import HUMAN_EVIDENCE_HEADING, Stage, StageError


def build_stage_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str,
    self_check: bool = False,
) -> str:
    """Assemble the bounded prompt for one stage-agent turn.

    Always includes the branch the agent must stay on, the stage brief, and
    (if present) PROJECT.md. When ``resume`` is true (a same-session
    follow-up), also includes the full current ``sparring.md`` content.

    ``self_check`` (project config ``[stage] self_check = true``, default
    false) adds one prose section asking the implementation agent to
    inspect its own work before finishing its turn. This is deliberately
    lightweight: no new machine-readable workflow state, no checklist
    fields, no pass/fail gate. Independent sparring still runs afterward
    regardless of what this section asks for.
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

    # A human's recorded answer/check result (notes.md "## Human evidence",
    # written by `resume-plan --evidence` or by hand). Shown whenever present
    # so the agent sees the answer next to the question it follows; absent
    # for a stage with no such notes, leaving existing prompts unchanged.
    evidence = human_evidence_section(stage)
    if evidence:
        parts += [
            "",
            HUMAN_EVIDENCE_HEADING,
            "",
            evidence,
            "",
            "Treat this as the human's answer to the latest NEEDS_YOU question "
            "or as recorded manual-check results. If it calls for implementation "
            "changes, make them; if not, report that no code change is needed.",
        ]

    if self_check:
        parts += [
            "",
            "## Self-check",
            "",
            "Before finishing this turn, inspect your own implementation for:",
            "",
            "- failure between steps;",
            "- resume/retry behavior;",
            "- stale or partially written state;",
            "- provider/runtime differences;",
            "- concurrency issues;",
            "- ways important invariants can be bypassed.",
            "",
            "Where this stage depends on external-tool behavior, exercise the "
            "real tool when practical rather than assume its contract. Fix "
            "issues you find before handing off, and report what you checked "
            "or deliberately deferred. This is a self-check, not a substitute "
            "for independent sparring, which still runs afterward.",
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
