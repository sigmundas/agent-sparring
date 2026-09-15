"""Bounded prompt assembly for an independent reviewer.

A review-only stage (:attr:`~agent_sparring.stage.StageMode.
INDEPENDENT_REVIEW`) has no implementation turn, so the sparring prompt is
the wrong prompt for it in two concrete ways.

**There is no handoff.** ``handoff.md`` is a stage agent's account of what
it just built -- claims, test evidence, changed files -- and a review-only
stage never has one, because no stage agent ran. Including the untouched
template would show the reviewer a page of ``(Copy or summarize from
brief.md.)`` placeholders and imply an implementation turn happened. What
replaces it is the one thing only the engine can state: the exact accepted
candidate commits, per stage, across every repository, verified against the
repositories as they stand right now (see :mod:`agent_sparring.review`).

**There is nothing to send back to.** The sparring prompt's SEND_BACK means
"a bounded issue for the same stage agent to fix", and for this stage there
is no such agent: a defect means the plan stops and a person decides where
the fix belongs. The reviewer is told that plainly, because a reviewer who
believes a correction loop exists behind it will write for that loop -- and,
worse, a reviewer who thinks its job includes fixing what it finds will
start editing the very candidate it was brought in to judge.

Like the other two builders this module only assembles text: one ordered
list of sourced sections (see :mod:`agent_sparring.prompt_sections`), which
is both the exact bytes handed to the provider and the sectioned view an
inspector shows. It invokes no provider and interprets no PROJECT.md prose.
"""

from __future__ import annotations

from pathlib import Path

from agent_sparring.config import CONTEXT_FILENAME, load_project_markdown
from agent_sparring.handoff import human_evidence_section
from agent_sparring.prompt_sections import (
    ROLE_REVIEWER,
    AssembledPrompt,
    PromptSection,
    review_turn_kind,
    section,
)
from agent_sparring.stage import (
    BRIEF_FILENAME,
    HUMAN_EVIDENCE_HEADING,
    NOTES_FILENAME,
    SPARRING_FILENAME,
    Stage,
    StageError,
)

_REVIEW_INSTRUCTIONS = """\
## Your task

You are the independent reviewer for this stage. This stage is a review and
nothing else: no implementation agent has run for it and none will, and the
work you are looking at was implemented, reviewed and accepted by the
earlier stages of this plan.

Verify the accepted candidate set above against the repositories as they
actually are, and against every gate this stage's brief names. Read code,
read history, read the plan; check that what the earlier stages claim is
what the commits contain. Then produce exactly one structured routing
outcome.

You have no write access, and that is enforced by the operating system
rather than by convention. Do not attempt to modify anything -- not
application code, not tests, not documentation, not a scratch file to try
something out. **If you find a defect that needs a code change, you are not
the agent that changes it.** Say what is wrong, precisely enough to act on,
and route it; the plan stops there and a person decides where the fix
belongs. A reviewer that quietly fixed what it found would be reviewing its
own work, which is the one thing this stage exists to avoid.

Give your final message as a single JSON object matching this shape and
nothing else (no markdown fences, no extra prose around it):

    {"action": "READY" | "NEEDS_YOU" | "SEND_BACK" | "ESCALATE",
     "summary": "<one short paragraph -- the routing headline>",
     "needs_you_reason": "<one of the standard categories below, then a
                   short reason -- e.g. \\"DEVICE/MANUAL CHECK -- needs a real
                   Android device\\". null if action is not NEEDS_YOU>",
     "findings": "<the real technical explanation -- what you actually
                   inspected, what you found, and why it matters; this is
                   what makes the outcome useful, and for READY it is your
                   account of how the candidate set was verified>",
     "deferred": "<what is deferred, why, and when it becomes required, or
                   null if nothing is deferred>",
     "human_gate": <the structured blocking human checks -- REQUIRED when
                   action is NEEDS_YOU, and null for every other action.
                   See "The human gate" below>}

What each action means *here*:

- READY: the accepted candidate set holds up, every gate this stage's brief
  names has passed, and this stage's decision can be recorded. Nothing is
  merged by this: acceptance of this stage records that the review passed,
  and no branch is merged automatically at any point.
- NEEDS_YOU: a concrete human choice, check or action is required before
  this stage can be READY. This is the normal way an activation or
  pre-activation gate is satisfied: you ask, the human does it and records
  the result, and you -- the same reviewer, in this same session -- judge
  the answer and route again. Start ``needs_you_reason`` with whichever of
  these categories fits:

      PRODUCT/PREFERENCE     choose between valid behaviors
      UI/VISUAL CHECK        look at layout, contrast, appearance
      DEVICE/MANUAL CHECK    run it on real hardware, offline, a real session
      EXTERNAL CONDITION     wait on a store/deploy/third-party state
      SCOPE EXPANSION        approve work beyond this stage's scope

  If none of the five fits, say so plainly and describe the category rather
  than forcing one.
- SEND_BACK: you found a defect in the accepted work that requires a code
  change. There is no implementation agent behind this stage, so this does
  not start one and is not a correction cycle: the plan stops, unaccepted,
  and a person decides whether the fix belongs in a new stage, in a
  reopened earlier one, or elsewhere. Describe the defect in ``findings``
  well enough for that decision to be made -- what is wrong, where, what it
  breaks, and what you checked to establish it.
- ESCALATE: this deserves a stronger or different review environment rather
  than being decided inside this automatic exchange.

## The human gate

When, and only when, you choose NEEDS_YOU, fill in ``human_gate``:

    {"category": "PRODUCT_PREFERENCE" | "UI_VISUAL_CHECK" |
                 "DEVICE_MANUAL_CHECK" | "EXTERNAL_CONDITION" |
                 "SCOPE_EXPANSION" | "OTHER",
     "title": "<one line: why execution stopped>",
     "checks": [
       {"id": "<short stable slug, e.g. \\"pre-activation-desktop-v2-feed\\";
                keep the same id if you restate the same check later>",
        "instruction": "<what to actually do, runnable by someone who does
                not have the plan open -- concrete steps, not a scenario
                letter or name>",
        "pass_criteria": "<what counts as passing, and what counts as
                failing>",
        "source": "<exact repo-relative plan path and heading where the full
                test is defined, or null>"}
     ]}

``checks`` must be non-empty, and it is rendered directly as the human's
Pass / Fail / Blocked list. A check written as "run scenarios A, B and D" is
not runnable by someone who does not have the plan open, and is therefore
not an acceptable check: write the steps out, or name the exact plan path
and heading in ``source``, and preferably both.

**Include only work that must be completed before this stage may become
READY.** Nothing else belongs in ``human_gate``. Explicitly exclude, even
when you are right to mention them:

- production deployment or migration application after this stage;
- anything that is the release owner's decision rather than this stage's;
- rollout gates deliberately closed until a later release decision -- unless
  this stage's brief makes that decision part of *this* stage;
- monitoring, follow-up or clean-up planned for the future;
- checks you merely could not run yourself because your sandbox is
  read-only.

Say those things in ``findings`` and ``deferred``. They are real and worth
recording; they are not this stage's gate, and listing them as checks asks a
human to "pass" work this stage does not depend on.

## Checks you cannot run yourself

Your sandbox is read-only, enforced by the operating system. Commands that
need to write will fail with permission errors: builds that emit artifacts,
test runners that create temporary directories, anything installing
dependencies. Those failures describe your environment, not a defect in the
candidate. Your own runtime may also differ from the project's, so a result
you do obtain may not be the project's result.

Being unable to reproduce a check yourself is NOT by itself a reason to
choose NEEDS_YOU. Put it in ``deferred`` -- what is unverified, why, and
when it becomes required -- then choose the action the review itself
warrants, treating the earlier stages' recorded evidence as a claim you have
not independently confirmed and saying so in ``findings``. "Someone should
re-run the suite where it can write files" is a deferred check, not a human
decision, and must never appear in ``human_gate``."""


def _stage_file(stage: Stage, filename: str) -> str:
    """A stage artifact's path relative to the ``.sparring`` directory."""

    return f"stages/{stage.stage_id}/{filename}"


def assemble_review_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str,
    candidate_set: str,
    evidence_first: bool = False,
) -> AssembledPrompt:
    """Assemble the bounded prompt for one independent-review turn.

    ``candidate_set`` is the engine's factual statement of what is under
    review -- every accepted stage, its candidate commit, and every sibling
    repository's pinned commit, each verified against the repositories
    immediately before this turn (rendered by
    :func:`agent_sparring.review.describe_candidate_set`). It is
    engine-authored and passed in rather than read from a file, because no
    file holds it: it is assembled from the preceding stages' ``state.json``
    and from git, and it is true as of this turn only.

    On ``resume`` the reviewer's own previous exchange (``sparring.md``) is
    included, which is what makes a NEEDS_YOU gate answerable by the *same*
    reviewer: it sees what it asked for beside the human's answer.

    ``evidence_first`` records that this turn exists because a human just
    answered that gate. It changes no text -- the human-evidence section
    below is already read live from ``notes.md`` -- and only labels the
    captured prompt with what the caller knew and this module cannot infer.
    """

    parts: list[PromptSection] = [
        section("Independent review", [f"# Independent review: {stage.stage_id}"])
    ]

    parts.append(
        section(
            "Branch",
            [
                "## Branch",
                "",
                f"The accepted candidate under review lives on branch `{expected_branch}`.",
            ],
        )
    )

    parts.append(
        section(
            "Stage brief",
            ["## Stage brief", "", stage.read_brief().strip()],
            source=_stage_file(stage, BRIEF_FILENAME),
        )
    )

    project_context = load_project_markdown(sparring_dir)
    if project_context and project_context.strip():
        parts.append(
            section(
                "Project context",
                ["## Project context", "", project_context.strip()],
                source=CONTEXT_FILENAME,
            )
        )

    parts.append(
        section(
            "Accepted candidate set",
            ["## Accepted candidate set", "", candidate_set.strip()],
        )
    )

    evidence = human_evidence_section(stage)
    if evidence:
        # Split for the same reason as in the other two builders: the
        # evidence is the human's own words out of notes.md, the paragraph
        # after it is this engine's instruction about them, and only one of
        # those two came from a file.
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
                    "This is the human's own answer to your latest NEEDS_YOU gate, or their "
                    "recorded manual-check results, read live from this stage's notes.md. It "
                    "is current as of this turn. Judge the candidate set with it: if it "
                    "satisfies what you asked for, say so and route accordingly rather than "
                    "asking for it again. If it does not, say exactly what is still missing."
                ],
            )
        )

    if resume:
        try:
            previous = stage.read_sparring().strip()
        except StageError:
            previous = ""
        parts.append(
            section(
                "Your previous review exchange",
                [
                    "## Your previous review exchange",
                    "",
                    previous or "(no sparring.md available)",
                ],
                source=_stage_file(stage, SPARRING_FILENAME),
            )
        )

    parts.append(section("Your task", [_REVIEW_INSTRUCTIONS]))

    return AssembledPrompt(
        role=ROLE_REVIEWER,
        stage_id=stage.stage_id,
        turn_kind=review_turn_kind(resume=resume, evidence_first=evidence_first),
        resumed=resume,
        expected_branch=expected_branch,
        sections=tuple(parts),
    )


def build_review_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str,
    candidate_set: str,
    evidence_first: bool = False,
) -> str:
    """The flat text of :func:`assemble_review_prompt`."""

    return assemble_review_prompt(
        stage,
        sparring_dir,
        resume=resume,
        expected_branch=expected_branch,
        candidate_set=candidate_set,
        evidence_first=evidence_first,
    ).text


__all__ = ["assemble_review_prompt", "build_review_prompt"]
