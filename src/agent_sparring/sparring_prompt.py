"""Bounded sparring-agent prompt assembly.

Assembles the prompt for one sparring-agent turn: the stage brief,
project context, the current handoff (the sparrer's read-only window onto
the candidate -- git identity, changed files, claims, test evidence; see
:mod:`agent_sparring.handoff`), and on resume the previous sparring
exchange, plus an explicit structured-verdict instruction. This module only
assembles text; it does not invoke any provider and does not interpret
PROJECT.md's prose. Mirrors :mod:`agent_sparring.stage_prompt`'s shape for
the stage-agent side.
"""

from __future__ import annotations

from pathlib import Path

from agent_sparring.artifact_ownership import ownership_section
from agent_sparring.config import CONTEXT_FILENAME, load_project_markdown
from agent_sparring.deferred_gate import DeferredObligation
from agent_sparring.gate_answers import check_histories, parse_gate_answers
from agent_sparring.handoff import human_evidence_section
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring.prompt_sections import (
    ROLE_SPARRER,
    AssembledPrompt,
    PromptSection,
    section,
    sparring_turn_kind,
)
from agent_sparring.stage import (
    BRIEF_FILENAME,
    HANDOFF_FILENAME,
    HUMAN_EVIDENCE_HEADING,
    NOTES_FILENAME,
    SPARRING_FILENAME,
    Stage,
    StageError,
)
from agent_sparring.visual_review import VisualDelivery, visual_evidence_section


def _stage_file(stage: Stage, filename: str) -> str:
    """A stage artifact's path relative to the ``.sparring`` directory."""

    return f"stages/{stage.stage_id}/{filename}"


def pending_deferred_section(
    pending: tuple[DeferredObligation, ...],
) -> PromptSection | None:
    """The managed run's ledger of human verification still owed.

    Engine-authored, like the candidate-set statement an independent review
    gets: it is assembled from the plan-run state and is true as of this
    turn only, so it is passed in rather than read from a file. Absent for a
    standalone stage, which owes nothing to a plan run.

    It exists so a reviewer can do the one thing only a *later* reviewer can:
    notice that work it is looking at now depends on a check an earlier
    reviewer judged safe to defer, and say so.
    """

    if not pending:
        return None
    lines = [
        "## Human verification already owed",
        "",
        "Earlier reviews of this plan deferred these checks: a person still has to do "
        "them, the engine will not let the plan complete until they are answered, and "
        "no stage is currently stopped for them.",
        "",
    ]
    for obligation in pending:
        lines.append(
            f"- `{obligation.instance_id}` — {obligation.gate.category} — "
            f"{obligation.gate.title} (raised by {obligation.stage_id}"
            + (", already promoted to immediate" if obligation.promoted else "")
            + ")"
        )
        lines.append(f"  - Deferred because: {obligation.rationale}")
        for check in obligation.gate.checks:
            lines.append(f"  - `{check.id}`: {check.instruction}")
    lines += [
        "",
        "If the work you are reviewing now *depends* on one of these answers — a later "
        "stage builds on the behaviour it verifies, or continuing would be misleading "
        "without it — name its gate instance in `promote_deferred` and say why in "
        "`findings`. The run then stops for it instead of carrying it to the end. If "
        "none of them has become blocking, leave `promote_deferred` empty; they are "
        "already recorded and nothing here needs to restate them.",
    ]
    return section("Human verification already owed", lines)


def gate_answer_tally_section(stage: Stage) -> PromptSection | None:
    """The engine's own tally of what has been answered, per check id.

    The ``Human evidence`` section already carries the person's words. This
    carries the fact those words do not state and a reviewer reading one
    turn cannot see: **how many times it has now asked the same thing, and
    which of its checks the person has reported they cannot do.**

    That blind spot is a real failure, not a hypothetical. A reviewer with
    no implementation defect to send back, holding acceptance checks a
    person has twice answered ``Blocked``, has only NEEDS_YOU left under the
    routing rules -- so it re-issues the same gate, every turn, forever. It
    is not ignoring the answer; it cannot see that it is repeating itself.

    Engine-authored and computed fresh, like the deferred ledger above: the
    gate comes from the recorded verdict in sparring.md and the answers from
    notes.md, so neither the reviewer nor the handoff can restate it
    wrongly. Absent when this reviewer has no recorded gate, or when nothing
    has been answered against it.
    """

    recorded = read_recorded_outcome(stage)
    gate = recorded.human_gate if recorded is not None else None
    if gate is None or not gate.checks:
        return None
    try:
        notes = stage.read_notes()
    except (StageError, OSError):
        return None
    answers = parse_gate_answers(notes)
    histories = check_histories(answers)
    answered = [(check, histories.get(check.id)) for check in gate.checks]
    if not any(history is not None for _, history in answered):
        return None

    lines = [
        "## What has already been answered",
        "",
        "Your last gate asked "
        + (f"{len(gate.checks)} check(s)" if gate.instance_id is None else f"{len(gate.checks)} check(s) as gate `{gate.instance_id}`")
        + ". This is the engine's tally of what a person has recorded against "
        "those check ids, counted from notes.md rather than from anyone's "
        "summary of it:",
        "",
    ]
    for check, history in answered:
        if history is None:
            lines.append(f"- `{check.id}` — no answer recorded yet")
            continue
        asked = history.askings_answered
        times = "once" if asked == 1 else f"{asked} times"
        lines.append(
            f"- `{check.id}` — latest answer **{history.latest.outcome.word}**, "
            f"asked and answered {times}"
        )
    stalled = [history for _, history in answered if history is not None and history.stalled]
    lines += ["", _BLOCKED_GUIDANCE if stalled else _ANSWERED_GUIDANCE]
    return section("What has already been answered", lines)


_ANSWERED_GUIDANCE = (
    "Judge the candidate with these answers. Re-issuing a check the person "
    "has already answered is legitimate when the answer did not settle the "
    "question -- keep its id and say in `findings` what was insufficient -- "
    "but a check they have answered well is finished, and listing it again "
    "asks them to do work twice."
)

_BLOCKED_GUIDANCE = """\
At least one of those checks is marked **Blocked**, which is not a result:
the person is telling you they could not perform it. Asking it again in the
same words cannot produce a different answer, and doing so every turn is
how this run stops making progress while appearing to. Do not re-issue a
Blocked check unless you have changed what it asks, or something it
depended on has changed -- and if you do, say which, in `findings`.

When a Blocked check is the only thing between this candidate and READY,
these are your routes, and one of them applies:

- The block is about **timing or environment** -- the candidate is not
  frozen yet, the test backend does not exist yet, the device is not to
  hand -- and you have found no implementation defect. Choose READY and put
  those checks in `deferred_human_gate`, with a `rationale` saying what
  later work does and does not depend on them. The engine keeps the
  obligation in the run's durable state and refuses to report the plan
  complete until a person answers it: deferring is not waiving, and it is
  the designed way out of exactly this position. The one thing that rules
  it out is later work in this plan depending on the answer.
- The block means the work **cannot be verified because the implementation
  is wrong, incomplete or not yet frozen in a form anyone can test**. That
  is an implementation problem: SEND_BACK, and say what would make it
  testable.
- The check is genuinely one that must not be deferred (see "Deciding when
  a human check has to happen"), and later work depends on its answer. Then
  the run does stop here: keep it as NEEDS_YOU, and say plainly in
  `findings` that you are holding the run for a check the person has
  reported they cannot yet perform, and what has to change for them to be
  able to. Do not pad that gate with the checks they have already answered.
"""


_VERDICT_INSTRUCTIONS = """\
## Your task

You are sparring on this stage's candidate: inspect real repository state
(read-only -- you have no write access and must not attempt to modify
anything), check the handoff's claims against it, discuss/challenge as
needed, and then produce exactly one structured routing outcome.

Give your final message as a single JSON object matching this shape and
nothing else (no markdown fences, no extra prose around it):

    {"action": "SEND_BACK" | "READY" | "NEEDS_YOU" | "ESCALATE",
     "summary": "<one short paragraph -- the routing headline>",
     "needs_you_reason": "<one of the standard categories below, then a
                   short reason -- e.g. \\"DEVICE/MANUAL CHECK -- needs a real
                   Android device\\". null if action is not NEEDS_YOU>",
     "findings": "<the real technical explanation -- what you actually
                   found, checked, and why; this is what makes SEND_BACK/
                   NEEDS_YOU/ESCALATE useful, and can also give a READY
                   rationale beyond the one-line summary>",
     "deferred": "<what is deferred, why, and when it becomes required, or
                   null if nothing is deferred>",
     "human_gate": <the structured human checks that must be done BEFORE
                   this stage can be READY -- REQUIRED when action is
                   NEEDS_YOU, and null for every other action. See "The
                   human gate" below for its shape and, more importantly,
                   for what may and may not go in it>,
     "deferred_human_gate": <structured human verification that is genuinely
                   owed but that you judge does not have to happen now.
                   Allowed ONLY with action READY, and null otherwise. See
                   "Deciding when a human check has to happen" below>,
     "promote_deferred": <array of gate instance ids from the "Human
                   verification already owed" section, if any, that you have
                   decided can wait no longer; [] otherwise>}

``summary`` is a short routing headline, not the whole story: put the real
detail -- what you inspected, what you found, why it matters -- in
``findings``. Both fields are shown to the human and, on the next
resumed stage-agent turn, to the same stage agent.

- SEND_BACK: a bounded implementation issue for the same stage agent to fix.
- READY: no unresolved implementation issue requiring another stage-agent pass.
- NEEDS_YOU: a concrete human choice/check/action is required before this
  stage can be READY. Start ``needs_you_reason`` with whichever of these
  standard categories fits, so the human can see at a glance what kind of
  attention is wanted:

      PRODUCT/PREFERENCE     choose between valid behaviors
      UI/VISUAL CHECK        look at layout, contrast, appearance
      DEVICE/MANUAL CHECK    run it on real hardware, offline, a real session
      EXTERNAL CONDITION     wait on a store/deploy/third-party state
      SCOPE EXPANSION        approve work beyond this stage's scope

  Then give the short reason. If none of the five fits, say so plainly and
  describe the category rather than forcing one.
- ESCALATE: this deserves a stronger/different sparring environment (e.g.
  GPT web chat) rather than being decided inside this automatic exchange.

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
Pass / Fail / Blocked list. So the scope rule matters more than the shape:

**Include only work that must be completed before this stage may become
READY.** Nothing else belongs in ``human_gate``.

Explicitly exclude, even when you are right to mention them:

- production deployment or migration application after acceptance;
- anything that is the release owner's decision rather than this stage's;
- rollout/activation gates that are deliberately closed until a later
  release decision;
- monitoring, follow-up or clean-up planned for the future;
- checks you merely could not run yourself because your sandbox is
  read-only.

Say those things in ``findings`` and ``deferred``. They are real and worth
recording; they are not this stage's gate, and listing them as checks asks a
human to "pass" work that acceptance does not depend on.

Verification a person genuinely owes, but which does not have to stop the
run, is a third thing again: see "Deciding when a human check has to happen"
below, and put it in ``deferred_human_gate``, not here.

If, after applying that rule, nothing is left, then this is not NEEDS_YOU:
choose the action the review itself warrants and record the rest as
``deferred``.

## Deciding when a human check has to happen

Some verification a person owes does not have to interrupt the run to be
worth doing. That timing decision is yours: the engine does not classify
checks, and there is no list of categories that are automatically one or the
other. A UI check can be immediate or deferred depending on what the rest of
the plan does with it.

Ask yourself, in this order:

1. Does the human's answer change what the *subsequent implementation*
   should be?
2. Would later work be expensive to redo, or actively misleading, if this
   check turned out to fail?
3. Is the check validating semantics and requirements, or mostly confirming
   a finished surface?
4. If it fails later, is the correction local and bounded?
5. Is there a concrete reason a person has to interrupt this run *now*?

Usually immediate (NEEDS_YOU + ``human_gate``):

- a product or preference choice that changes what is required;
- an ambiguity the next stage builds directly on;
- security, privacy or destructive approval;
- an external condition without which the implementation direction is
  unknown;
- a manual result that determines whether the subsequent work is correct.

Often deferrable (READY + ``deferred_human_gate``):

- visual polish and readability verification;
- final resizing or theme inspection;
- screenshots;
- device confirmation that later work does not depend on;
- verification whose failure would cause only a local, bounded fix;
- checks that are *more* useful against a more complete UI later.

These are heuristics for your judgement, not rules. Nothing in the engine
re-evaluates them.

Four things are never deferrable, whatever else is true, because continuing
would cross a real boundary rather than merely leave a question open: an
irreversible or destructive action awaiting approval; production deployment
or release authorization; a credential or security boundary; and anything
the plan itself states must be approved by a human before proceeding. Those
are NEEDS_YOU.

One thing that sounds like the fourth but is not: a check the *plan* defines
as part of this stage's acceptance is not thereby undeferrable. The plan
saying "verify this before the stage is accepted" is a statement about what
must be verified, not a boundary that continuing would cross, and the
deferred ledger still refuses to let the plan complete until it is answered.
So when such a check cannot be performed yet -- the person has answered it
Blocked because the candidate is not frozen or the environment does not
exist -- deferring it is available and is usually right. What rules it out
is later work in this plan depending on the answer, not where the check was
written down.

``deferred_human_gate`` has the same shape as ``human_gate``, plus two
fields:

    {"category": …, "title": …, "checks": [ … same as human_gate … ],
     "rationale": "<short: why continuing before this verification is low
                risk. One or two sentences, concrete about what later work
                does and does not depend on. Not an essay>",
     "checkpoint": "before_plan_completion"}

``rationale`` is required. A deferral without one is rejected: it is the
only record of *why* the run was allowed to continue, and the human, the
next reviewer and anyone debugging an unattended run all read it.

``checkpoint`` is ``"before_plan_completion"`` — the only value this version
accepts. The engine keeps the obligation in durable plan state and refuses
to report the plan complete until a person has answered it. Deferring is
therefore not waiving: the stage is accepted *and* the verification is still
owed, and both facts are recorded.

You may use ``deferred_human_gate`` only with READY. If an implementation
issue remains, that is SEND_BACK; if a person must answer before this stage
can be READY at all, that is NEEDS_YOU with ``human_gate``. Do not put the
same check in both.

You may inspect the repository (e.g. git log/diff/status, reading files)
but must not modify it.

## Checks you cannot run yourself

Your sandbox is read-only, and that is enforced by the operating system,
not by convention. Commands that need to write will fail with permission
errors: builds that emit artifacts, test runners that create temporary
directories, anything installing dependencies. Those failures describe your
environment, not a defect in the candidate. Your own runtime may also
differ from the project's (a different language/tool version), so a result
you do obtain may not be the project's result.

Being unable to reproduce a check yourself is NOT by itself a reason to
choose NEEDS_YOU. Instead:

- put it in ``deferred`` -- what is unverified, why you could not verify it,
  and when it becomes required;
- then choose the action the code review itself warrants, treating the
  stage agent's reported evidence as a claim you have not independently
  confirmed and saying so in ``findings``.

Reserve NEEDS_YOU for something a human must actually decide or do: a
product/preference choice between valid behaviors, a visual or UI judgement,
a device/manual check, an external condition to wait on, or a scope
expansion to approve. "Someone should re-run the suite where it can write
files" is prose for ``deferred``, not a human decision, and must never
appear in ``human_gate`` -- nor in ``deferred_human_gate``, which is for
verification only a person can perform, not for work you happened to be
unable to run."""


FRESH_REVIEWER_NOTICE = (
    "You are a fresh reviewer replacing an earlier reviewer conversation. Prior findings are "
    "historical evidence, not conclusions you must blindly accept. Re-check the candidate "
    "independently, but do not lose unresolved findings without explicitly resolving them."
)


def assemble_sparring_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str | None = None,
    finalization: str | None = None,
    evidence_first: bool = False,
    pending_deferred: tuple[DeferredObligation, ...] = (),
    fresh: bool = False,
    visual: VisualDelivery | None = None,
) -> AssembledPrompt:
    """Assemble the bounded prompt for one sparring-agent turn.

    Always includes the stage brief, (if present) PROJECT.md, the current
    ``handoff.md`` content, and -- when the stage has any -- the human
    evidence recorded in ``notes.md``. When ``resume`` is true (a same-context
    follow-up after a SEND_BACK correction cycle), also includes the full
    current ``sparring.md`` content -- this sparrer's own previous exchange
    -- so it can see what it previously asked for.

    The human-evidence section is read live from ``notes.md``, not from the
    handoff. ``handoff.md`` is only regenerated by a stage-agent turn, so
    after a NEEDS_YOU gate is answered the handoff's copy of the evidence is
    a snapshot from *before* the answer existed. Reading notes.md here is
    what makes ``resume-plan --evidence`` able to go straight back to the
    sparrer against the unchanged candidate: notes.md is the one canonical
    place a human's answer lives, and no caller has to mirror it anywhere
    else to be seen.

    ``finalization``, when given, is one factual statement from the engine
    about the commit turn that produced the candidate now under review: that
    the committed content is byte-for-byte the content of the tree this
    sparrer already reviewed (see :mod:`agent_sparring.finalization`). It is
    something only the engine can know and the sparrer cannot establish by
    reading the repository, and without it a sparrer looking at a
    just-committed candidate cannot tell a plain commit of reviewed work
    from a commit that quietly rewrote it. It is passed through verbatim by
    :func:`agent_sparring.sparring_agent.run_sparring_agent`, which never
    sends it unless the engine's own path-by-path comparison actually
    passed.

    ``evidence_first`` records that the loop entered at this sparrer against
    an unchanged candidate because a human just answered a NEEDS_YOU gate.
    It changes no text -- the human-evidence section below is already driven
    by notes.md -- and exists only so the captured turn is labelled with
    what the caller knew and this module could not infer.

    ``fresh`` marks the first turn of a fresh reviewer session (see
    :mod:`agent_sparring.sessions`): the full prompt, plus the previous
    sparring exchange as historical evidence and a notice that this reviewer
    replaces an earlier conversation. Its captured turn kind is ``fresh``.

    ``visual`` is the verified evidence attached to this turn (see
    :mod:`agent_sparring.visual_review`): it adds the engine's statement of
    which attached image is which, the written visual criteria and how to
    judge them. Only :func:`~agent_sparring.sparring_agent.run_sparring_agent`
    passes it, and only alongside the images themselves.

    Returns the prompt as ordered, sourced sections (see
    :mod:`agent_sparring.prompt_sections`);
    :func:`build_sparring_prompt` is the flat-text view of the same result.
    """

    parts: list[PromptSection] = [
        section("Sparring", [f"# Sparring: {stage.stage_id}"])
    ]

    if expected_branch:
        parts.append(
            section(
                "Branch",
                ["## Branch", "", f"The candidate lives on branch `{expected_branch}`."],
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

    # The same read-only artifact contract the implementation agent gets.
    # A sparrer's read-only sandbox is enforced where the provider supports
    # one, but the instruction is not redundant: it says which files are
    # engine-owned, which is something no sandbox communicates.
    parts.append(section("Agent Sparring artifacts", ownership_section(ROLE_SPARRER)))

    try:
        handoff_text = stage.read_handoff().strip()
    except StageError:
        handoff_text = ""
    parts.append(
        section(
            "Handoff",
            ["## Handoff", "", handoff_text or "(no handoff.md available)"],
            source=_stage_file(stage, HANDOFF_FILENAME),
        )
    )

    evidence = human_evidence_section(stage)
    if evidence:
        # Split for the same reason as in stage_prompt: the evidence is the
        # human's words out of notes.md, the sentence after it is this
        # engine's instruction about them, and only one of those two comes
        # from the file.
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
                    "recorded manual-check results, read live from the stage's notes.md. It is "
                    "current as of this turn and supersedes any copy of it inside the handoff "
                    "above. Judge the candidate with it: if it satisfies what you asked for, say "
                    "so and route accordingly rather than asking for it again. If it says they "
                    "could not do what you asked, that is an answer too, and the section below "
                    "says what to do with it."
                ],
            )
        )

    tally = gate_answer_tally_section(stage)
    if tally is not None:
        parts.append(tally)

    owed = pending_deferred_section(pending_deferred)
    if owed is not None:
        parts.append(owed)

    if finalization and finalization.strip():
        parts.append(
            section("Finalization", ["## Finalization", "", finalization.strip()])
        )

    if fresh:
        try:
            sparring_text = stage.read_sparring().strip()
        except StageError:
            sparring_text = ""
        parts.append(
            section(
                "Fresh reviewer",
                [
                    "## Fresh reviewer",
                    "",
                    FRESH_REVIEWER_NOTICE,
                ],
            )
        )
        parts.append(
            section(
                "Previous sparring exchange (historical evidence)",
                [
                    "## Previous sparring exchange (historical evidence)",
                    "",
                    sparring_text or "(no sparring.md available)",
                ],
                source=_stage_file(stage, SPARRING_FILENAME),
            )
        )
    elif resume:
        try:
            sparring_text = stage.read_sparring().strip()
        except StageError:
            sparring_text = ""
        parts.append(
            section(
                "Your previous sparring exchange",
                [
                    "## Your previous sparring exchange",
                    "",
                    sparring_text or "(no sparring.md available)",
                ],
                source=_stage_file(stage, SPARRING_FILENAME),
            )
        )

    if visual is not None:
        parts.append(visual_evidence_section(visual, stage))

    parts.append(section("Your task", [_VERDICT_INSTRUCTIONS]))

    return AssembledPrompt(
        role=ROLE_SPARRER,
        stage_id=stage.stage_id,
        turn_kind=sparring_turn_kind(
            resume=resume,
            evidence_first=evidence_first,
            finalization=bool(finalization and finalization.strip()),
            fresh=fresh,
        ),
        resumed=resume,
        expected_branch=expected_branch,
        sections=tuple(parts),
    )


def build_sparring_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str | None = None,
    finalization: str | None = None,
) -> str:
    """The flat text of :func:`assemble_sparring_prompt`.

    A one-line wrapper on purpose; see :func:`build_stage_prompt` for why
    there is deliberately only one assembly path.
    """

    return assemble_sparring_prompt(
        stage,
        sparring_dir,
        resume=resume,
        expected_branch=expected_branch,
        finalization=finalization,
    ).text


__all__ = ["assemble_sparring_prompt", "build_sparring_prompt", "pending_deferred_section"]
