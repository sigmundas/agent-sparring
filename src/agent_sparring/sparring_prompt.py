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
from agent_sparring.stage import Stage, StageError

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
                   null if nothing is deferred>"}

``summary`` is a short routing headline, not the whole story: put the real
detail -- what you inspected, what you found, why it matters -- in
``findings``. Both fields are shown to the human and, on the next
resumed stage-agent turn, to the same stage agent.

- SEND_BACK: a bounded implementation issue for the same stage agent to fix.
- READY: no unresolved implementation issue requiring another stage-agent pass.
- NEEDS_YOU: a concrete human choice/check/action is required. Start
  ``needs_you_reason`` with whichever of these standard categories fits, so
  the human can see at a glance what kind of attention is wanted:

      PRODUCT/PREFERENCE     choose between valid behaviors
      UI/VISUAL CHECK        look at layout, contrast, appearance
      DEVICE/MANUAL CHECK    run it on real hardware, offline, a real session
      EXTERNAL CONDITION     wait on a store/deploy/third-party state
      SCOPE EXPANSION        approve work beyond this stage's scope

  Then give the short reason. If none of the five fits, say so plainly and
  describe the category rather than forcing one.
- ESCALATE: this deserves a stronger/different sparring environment (e.g.
  GPT web chat) rather than being decided inside this automatic exchange.

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
files" is a deferred check, not a human decision."""


def build_sparring_prompt(
    stage: Stage,
    sparring_dir: Path,
    *,
    resume: bool,
    expected_branch: str | None = None,
) -> str:
    """Assemble the bounded prompt for one sparring-agent turn.

    Always includes the stage brief, (if present) PROJECT.md, and the
    current ``handoff.md`` content. When ``resume`` is true (a same-context
    follow-up after a SEND_BACK correction cycle), also includes the full
    current ``sparring.md`` content -- this sparrer's own previous exchange
    -- so it can see what it previously asked for.
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
