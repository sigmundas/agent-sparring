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

from agent_sparring.artifact_ownership import ownership_section
from agent_sparring.config import CONTEXT_FILENAME, load_project_markdown
from agent_sparring.handoff import human_evidence_section
from agent_sparring.prompt_sections import (
    ROLE_STAGE,
    AssembledPrompt,
    PromptSection,
    section,
    stage_turn_kind,
)
from agent_sparring.stage import (
    BRIEF_FILENAME,
    HUMAN_EVIDENCE_HEADING,
    NOTES_FILENAME,
    SPARRING_FILENAME,
    Stage,
    StageError,
)


def _stage_file(stage: Stage, filename: str) -> str:
    """A stage artifact's path relative to the ``.sparring`` directory.

    This is what a reader needs in order to open the file the section came
    from, and it stays correct wherever the project's sparring directory
    happens to live.
    """

    return f"stages/{stage.stage_id}/{filename}"


def _finalization_section(expected_branch: str) -> list[str]:
    """The bounded commit/push turn's instruction.

    Deliberately the narrowest turn this package ever asks for: the work is
    already implemented, already reviewed, and (for a human-gated stage)
    already manually verified, and the only thing missing is the commit the
    acceptance gate can freeze. The engine compares the committed content
    against the reviewed tree path by path afterwards (see
    :mod:`agent_sparring.finalization`), so the limits below are not an
    honour system -- but saying them is what lets an agent comply instead of
    tripping over them.
    """

    return [
        "## Finalize this candidate",
        "",
        "This stage's implementation is complete and the sparrer has accepted "
        "it. The reviewed work is still sitting uncommitted in the working "
        "tree, which is why this turn exists: the acceptance gate only ever "
        "freezes an exact commit. Turn this exact working tree into that "
        "commit, and change nothing about it.",
        "",
        f"- Commit the stage's work on `{expected_branch}` and push "
        f"`{expected_branch}`.",
        "- Report the exact committed SHA.",
        "- Run only the checks you need in order to commit safely.",
        "- Do not re-implement, refactor, rename, reformat, reword or "
        "otherwise improve anything -- not the code, not the tests, not the "
        "documentation.",
        "- If a check fails, or you believe a real code change is needed, "
        "make no change: say what you found and stop. That is a useful "
        "turn, and the run will handle it.",
        "",
        "The engine compares what you commit against the tree that was "
        "reviewed, path by path. Any content you change invalidates the "
        "verification this stage already passed -- including any manual "
        "check a human performed on it -- and the run will stop without "
        "accepting rather than carry that verification forward onto "
        "different work.",
    ]


def assemble_stage_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str,
    self_check: bool = False,
    finalize_only: bool = False,
) -> AssembledPrompt:
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

    ``finalize_only`` (default false) turns this into a commit/push turn for
    an already-reviewed candidate: it replaces the ordinary "act on the
    human's answer" and self-check instructions -- both of which invite
    implementation work -- with the bounded finalization instruction, since
    asking for a change and forbidding one in the same prompt would be
    incoherent. Like ``self_check`` it is never recorded as machine state;
    :mod:`agent_sparring.loop` decides when a turn is one of these, and
    :mod:`agent_sparring.finalization` is what actually holds the turn to it.

    Returns the prompt as ordered, sourced sections (see
    :mod:`agent_sparring.prompt_sections`);
    :func:`build_stage_prompt` is the flat-text view of the same result.
    """

    parts: list[PromptSection] = [
        section("Stage", [f"# Stage: {stage.stage_id}"]),
        section(
            "Branch",
            [
                "## Branch",
                "",
                f"You are operating on branch `{expected_branch}`. Do not switch, "
                "rename, or create a different branch.",
            ],
        ),
        section(
            "Stage brief",
            ["## Stage brief", "", stage.read_brief().strip()],
            source=_stage_file(stage, BRIEF_FILENAME),
        ),
    ]

    project_context = load_project_markdown(sparring_dir)
    if project_context and project_context.strip():
        parts.append(
            section(
                "Project context",
                ["## Project context", "", project_context.strip()],
                source=CONTEXT_FILENAME,
            )
        )

    # Before any of the turn's own instructions, and on every turn kind:
    # the project's own agent instructions may well tell an agent to record
    # its progress in the active plan, and this is the only party that knows
    # the plan is currently a run's execution definition. See
    # :mod:`agent_sparring.artifact_ownership`.
    #
    # Assembled as its own engine-authored section (no ``source``), like
    # every other block here, so the prompt inspector shows it by name and
    # attributes it to this engine rather than to a file on disk.
    parts.append(section("Agent Sparring artifacts", ownership_section(ROLE_STAGE)))

    if resume:
        try:
            sparring_text = stage.read_sparring().strip()
        except StageError:
            sparring_text = ""
        parts.append(
            section(
                "Latest sparring exchange",
                [
                    "## Latest sparring exchange",
                    "",
                    sparring_text or "(no sparring.md available)",
                ],
                source=_stage_file(stage, SPARRING_FILENAME),
            )
        )

    # A human's recorded answer/check result (notes.md "## Human evidence",
    # written by `resume-plan --evidence` or by hand). Shown whenever present
    # so the agent sees the answer next to the question it follows; absent
    # for a stage with no such notes, leaving existing prompts unchanged.
    evidence = human_evidence_section(stage)
    if evidence:
        # Two sections, not one: the evidence is the human's own words out of
        # notes.md, and the sentence after it is this engine's instruction
        # about them. Attributing that instruction to notes.md would be a
        # claim the file does not support.
        parts.append(
            section(
                "Human evidence",
                [HUMAN_EVIDENCE_HEADING, "", evidence],
                source=_stage_file(stage, NOTES_FILENAME),
            )
        )
        parts.append(
            section(
                "How to use the human evidence",
                [
                    (
                        "This is what the human verified about the very tree you are about "
                        "to commit, and it is the reason this turn must not change that tree."
                    )
                    if finalize_only
                    else (
                        "Treat this as the human's answer to the latest NEEDS_YOU question "
                        "or as recorded manual-check results. If it calls for implementation "
                        "changes, make them; if not, report that no code change is needed."
                    )
                ],
            )
        )

    if finalize_only:
        # No self-check and no scope reminder: both ask for implementation
        # judgement, which is exactly what this turn must not exercise.
        parts.append(
            section("Finalize this candidate", _finalization_section(expected_branch))
        )
    else:
        if self_check:
            parts.append(
                section(
                    "Self-check",
                    [
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
                    ],
                )
            )

        parts.append(
            section(
                "Scope reminder",
                [
                    "## Scope reminder",
                    "",
                    "Stay within this stage's bounded goal above. Do not expand scope, "
                    "start a new stage, or launch another top-level implementation or "
                    "sparring agent.",
                ],
            )
        )

    return AssembledPrompt(
        role=ROLE_STAGE,
        stage_id=stage.stage_id,
        turn_kind=stage_turn_kind(resume=resume, finalize_only=finalize_only),
        resumed=resume,
        expected_branch=expected_branch,
        sections=tuple(parts),
    )


def build_stage_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str,
    self_check: bool = False,
    finalize_only: bool = False,
) -> str:
    """The flat text of :func:`assemble_stage_prompt`.

    Kept as the prompt-building entry point for callers that only want the
    string. It is deliberately a one-line wrapper rather than a second
    implementation: one assembly path means the sections a reader is shown
    and the bytes a provider receives can never describe different prompts.
    """

    return assemble_stage_prompt(
        stage,
        sparring_dir,
        resume=resume,
        expected_branch=expected_branch,
        self_check=self_check,
        finalize_only=finalize_only,
    ).text


__all__ = ["assemble_stage_prompt", "build_stage_prompt"]
