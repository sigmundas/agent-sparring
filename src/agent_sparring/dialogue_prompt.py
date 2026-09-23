"""The prompt for one turn of the human's side conversation with the reviewer.

This is deliberately the shortest prompt the engine builds. Every other
prompt has to establish a context; this one is delivered *into* the
reviewer's existing thread (see :mod:`agent_sparring.dialogue`), where the
brief, the handoff, the candidate diff and the reviewer's own findings are
already present. Re-sending them would pay for the same tokens twice and
crowd out the review the person is asking about, so this prompt carries only
what the thread does not already have: the framing, optionally the one check
being asked about, and the question.

The framing does real work, because the last thing said in that thread was
an instruction to emit a JSON routing verdict. Without an explicit override
the reviewer will answer a plain question in verdict shape, and an answer
dressed as a verdict invites exactly the confusion this feature exists to
remove: a person reading JSON cannot tell an explanation from a decision.
:mod:`agent_sparring.dialogue` never parses the reply either, so the two
defences are independent.

Nothing here can change a verdict. The reviewer may say it was wrong, and
that is useful -- but a recorded verdict only ever changes by a real
sparring turn writing a new one, and this prompt says so plainly rather than
leaving the reviewer to guess whether answering counts as revising.
"""

from __future__ import annotations

from agent_sparring.human_gate import HumanCheck, HumanGate
from agent_sparring.prompt_sections import (
    ROLE_SPARRER,
    AssembledPrompt,
    PromptSection,
    section,
)
from agent_sparring.stage import Stage

#: The turn kind recorded for a captured dialogue prompt. Its own kind, not
#: a flavour of ``resume``: a reader scanning ``prompts/`` should be able to
#: tell a turn that could change the verdict from one that could not.
TURN_DIALOGUE = "dialogue"

_FRAMING = """## This is a side conversation, not a review turn

The person running this stage is asking you something directly. You are the \
same reviewer, in the same conversation, so answer from the review you \
actually did rather than re-deriving it.

Four things are different about this turn:

- **Answer in prose.** Do not emit a routing verdict, a JSON object, or \
anything shaped like one. Nothing parses your reply; it is shown to a \
person as you write it.
- **You are read-only, as always.** Inspect the repository as much as the \
question needs. Do not edit, stage, commit or push anything.
- **This turn cannot change the verdict.** The recorded verdict stands \
until a real sparring turn writes a new one. If the person shows you \
something that changes your mind, say so plainly and say what you would \
conclude instead -- that is useful, and it is how they learn a re-review is \
worth running. Do not claim to have revised anything.
- **You are not deciding.** The decision this gate asks for is theirs. Do \
not mark a check passed, approve on their behalf, or tell them what you \
need them to say.

Quote the concrete evidence you used -- files, line ranges, identifiers, \
records -- so they can check it themselves. Where the evidence does not \
settle something, say "I cannot establish that from the evidence" and say \
what would settle it. That is a better answer than a confident one you \
cannot support."""


def _check_section(check: HumanCheck) -> PromptSection:
    """The one check being asked about, quoted rather than summarised.

    Verbatim on purpose: this is the question the person has been asked to
    answer, and an engine that paraphrased it here would be putting a
    slightly different question to the reviewer than the one on the
    person's screen.
    """

    lines = [
        "## The check they are asking about",
        "",
        f"Check id: `{check.id}`",
        "",
        "Instruction, as you wrote it:",
        "",
        f"> {check.instruction}",
        "",
        "Pass criteria, as you wrote it:",
        "",
        f"> {check.pass_criteria}",
    ]
    if check.source:
        lines += ["", f"Source: {check.source}"]
    return section("The check they are asking about", lines)


def find_check(gate: HumanGate | None, check_id: str) -> HumanCheck | None:
    """The check ``check_id`` names in ``gate``, if it has one."""

    if gate is None:
        return None
    for check in gate.checks:
        if check.id == check_id:
            return check
    return None


def assemble_dialogue_prompt(
    stage: Stage,
    *,
    message: str,
    check: HumanCheck | None = None,
    expected_branch: str | None = None,
) -> AssembledPrompt:
    """Assemble one dialogue turn's prompt.

    ``check`` is the gate check the person opened the conversation from,
    already resolved by the caller (see :func:`find_check`); ``None`` is an
    ordinary question about the review as a whole.
    """

    parts: list[PromptSection] = [section("Side conversation", [_FRAMING])]
    if check is not None:
        parts.append(_check_section(check))
    parts.append(
        section("Their question", ["## Their question", "", message.strip()])
    )

    return AssembledPrompt(
        role=ROLE_SPARRER,
        stage_id=stage.stage_id,
        turn_kind=TURN_DIALOGUE,
        # Always true: a dialogue exists only as a continuation of a
        # reviewer thread that is already going.
        resumed=True,
        expected_branch=expected_branch,
        sections=tuple(parts),
    )


__all__ = ["TURN_DIALOGUE", "assemble_dialogue_prompt", "find_check"]
