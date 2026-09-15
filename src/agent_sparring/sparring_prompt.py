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

from agent_sparring.config import load_project_markdown
from agent_sparring.handoff import human_evidence_section
from agent_sparring.stage import HUMAN_EVIDENCE_HEADING, Stage, StageError

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
     "human_gate": <the structured blocking human checks -- REQUIRED when
                   action is NEEDS_YOU, and null for every other action.
                   See "The human gate" below for its shape and, more
                   importantly, for what may and may not go in it>}

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

If, after applying that rule, nothing is left, then this is not NEEDS_YOU:
choose the action the review itself warrants and record the rest as
``deferred``.

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
files" is a deferred check, not a human decision, and must never appear in
``human_gate``."""


def build_sparring_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str | None = None,
    finalization: str | None = None,
) -> str:
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
    """

    parts = [f"# Sparring: {stage.stage_id}", ""]

    if expected_branch:
        parts += [
            "## Branch",
            "",
            f"The candidate lives on branch `{expected_branch}`.",
            "",
        ]

    parts += ["## Stage brief", "", stage.read_brief().strip()]

    project_context = load_project_markdown(sparring_dir)
    if project_context and project_context.strip():
        parts += ["", "## Project context", "", project_context.strip()]

    try:
        handoff_text = stage.read_handoff().strip()
    except StageError:
        handoff_text = ""
    parts += ["", "## Handoff", "", handoff_text or "(no handoff.md available)"]

    evidence = human_evidence_section(stage)
    if evidence:
        parts += [
            "",
            HUMAN_EVIDENCE_HEADING,
            "",
            evidence,
            "",
            "This is the human's own answer to your latest NEEDS_YOU gate, or their "
            "recorded manual-check results, read live from the stage's notes.md. It is "
            "current as of this turn and supersedes any copy of it inside the handoff "
            "above. Judge the candidate with it: if it satisfies what you asked for, say "
            "so and route accordingly rather than asking for it again.",
        ]

    if finalization and finalization.strip():
        parts += [
            "",
            "## Finalization",
            "",
            finalization.strip(),
        ]

    if resume:
        try:
            sparring_text = stage.read_sparring().strip()
        except StageError:
            sparring_text = ""
        parts += [
            "",
            "## Your previous sparring exchange",
            "",
            sparring_text or "(no sparring.md available)",
        ]

    parts += ["", _VERDICT_INSTRUCTIONS]

    return "\n".join(parts).rstrip() + "\n"


__all__ = ["build_sparring_prompt"]
