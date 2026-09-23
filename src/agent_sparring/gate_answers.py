"""What a person actually answered to an immediate human gate.

A NEEDS_YOU gate's answers reach the next turn as prose: a UI writes them
under ``## Human evidence`` in the stage's notes.md and the prompt embeds
that section verbatim (see :func:`agent_sparring.handoff.
human_evidence_section`). That was enough while the only question was "what
did the human say"; it is not enough for the question this module exists to
answer, which is **"has this exact check already been asked and found
impossible?"**

The failure it was written for: a reviewer asks three checks, the person
answers two of them ``Blocked`` with a structural reason ("I can test this
only once the candidate is frozen"), and the reviewer -- which has no
implementation defect to send back and cannot mark a stage READY over
unsatisfied acceptance checks -- issues the same gate again. And again.
Nothing in the engine noticed, because nothing in the engine had ever read
an answer as anything but a paragraph.

So the lines a UI writes are parsed back into results here. Deliberately
narrow:

- This is a **reader of prose, not a format**. A person may write anything
  under ``## Human evidence`` (``resume-plan --evidence`` takes free text),
  and anything this cannot parse is simply not a result. No line is ever
  rejected, and nothing is inferred from a line's wording -- only the
  explicit ``Pass``/``Fail``/``Blocked`` word a UI wrote is read.
- Results belong to a **gate instance**, never to a check id for all time,
  for the reason :mod:`agent_sparring.human_gate` gives: a reviewer may
  legitimately re-ask a check whose answer was insufficient, and keying on
  the id alone would let an old answer satisfy a new asking.
- Nothing here routes. It reports what was answered; the prompt tells the
  reviewer what that means, and the plan runner decides when a repetition
  has gone on long enough.

The line grammar is the one the VS Code panel writes (``renderHumanEvidence``
in ``humanChecks.ts``), and both suffixes are optional so every line ever
written still parses:

    - <Pass|Fail|Blocked> — <check text> [· check `<id>`] [· gate `<id>`]
      <optional indented note lines>
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_sparring.deferred_gate import CheckOutcome
from agent_sparring.human_gate import HumanGate
from agent_sparring.stage import HUMAN_EVIDENCE_HEADING

# The sub-heading under which a UI records freeform human feedback. Text
# there is never a check result, however its words read: that separation is
# the whole reason it has its own heading.
HUMAN_FEEDBACK_HEADING = "### Additional human feedback"

_RESULT_RE = re.compile(
    r"\A-\s+(Pass|Fail|Blocked)\s+—\s+(.+?)"
    r"(?:\s+·\s+check\s+`([^`]+)`)?"
    r"(?:\s+·\s+reviewer request)?"
    r"(?:\s+·\s+gate\s+`([^`]+)`)?\s*\Z"
)
_SECTION_END_RE = re.compile(r"\A#{1,2}\s")
_SUB_HEADING_RE = re.compile(r"\A(#{3,6})\s+(\S.*?)\s*\Z")
_FENCE_RE = re.compile(r"\A\s*(```|~~~)")
_FEEDBACK_RE = re.compile(r"\Aadditional human feedback\b", re.IGNORECASE)


@dataclass(frozen=True)
class GateAnswer:
    """One recorded outcome for one check of one asking."""

    outcome: CheckOutcome
    #: The check's own wording as the UI quoted it back.
    text: str
    #: The check's stable id, absent on a line that named none.
    check_id: str | None = None
    #: Which asking this answered, absent on a line written before gate
    #: instances existed or for a gate that had none.
    gate_instance_id: str | None = None
    #: Whatever the person typed beneath the line, joined by newlines.
    note: str | None = None


def parse_gate_answers(notes: str | None) -> tuple[GateAnswer, ...]:
    """Every check result recorded under ``## Human evidence``.

    In the order written, which is the order asked, so the last answer for a
    check id is the most recent one. Unparseable lines, paragraphs and
    anything under the freeform-feedback sub-heading are skipped rather than
    guessed at.
    """

    if not notes:
        return ()
    lines = notes.splitlines()
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == HUMAN_EVIDENCE_HEADING
        )
    except StopIteration:
        return ()

    answers: list[GateAnswer] = []
    pending: re.Match[str] | None = None
    note_lines: list[str] = []
    in_feedback = False
    in_fence = False

    def flush() -> None:
        nonlocal pending, note_lines
        if pending is not None:
            note = "\n".join(note_lines).strip()
            answers.append(
                GateAnswer(
                    outcome=CheckOutcome(pending.group(1).lower()),
                    text=pending.group(2).strip(),
                    check_id=pending.group(3),
                    gate_instance_id=pending.group(4),
                    note=note or None,
                )
            )
        pending = None
        note_lines = []

    for line in lines[start + 1 :]:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _SECTION_END_RE.match(line):
            break
        sub = _SUB_HEADING_RE.match(line)
        if sub:
            # A sub-heading ends whatever was above it and decides whether
            # what follows is feedback (never a result) or more results.
            flush()
            in_feedback = bool(_FEEDBACK_RE.match(sub.group(2)))
            continue
        if in_feedback:
            continue
        stripped = line.strip()
        match = _RESULT_RE.match(stripped)
        if match is not None:
            flush()
            pending = match
            continue
        if not stripped:
            continue
        if line.startswith((" ", "\t")) and pending is not None:
            note_lines.append(stripped)
            continue
        # An ordinary paragraph, or a bullet this cannot read: it ends the
        # result above it and is not itself one.
        flush()
    flush()
    return tuple(answers)


@dataclass(frozen=True)
class CheckHistory:
    """Every answer recorded for one check id, oldest first."""

    check_id: str
    answers: tuple[GateAnswer, ...]

    @property
    def latest(self) -> GateAnswer:
        return self.answers[-1]

    @property
    def askings_answered(self) -> int:
        """How many distinct askings of this check a person has answered.

        Counted by gate instance, because that is what an asking *is*. An
        answer recorded before gate instances existed carries none, and each
        such answer counts once: two of them are two answers, and treating
        them as one asking would undercount the very repetition this exists
        to notice.
        """

        seen: set[str] = set()
        unattributed = 0
        for answer in self.answers:
            if answer.gate_instance_id is None:
                unattributed += 1
            else:
                seen.add(answer.gate_instance_id)
        return len(seen) + unattributed

    @property
    def stalled(self) -> bool:
        """The person's latest word on this check is that they could not do it.

        ``Blocked`` is not a result (see
        :class:`~agent_sparring.deferred_gate.CheckOutcome`): it resolves
        nothing. A check that is stalled is therefore still owed *and* not
        answerable by asking again in the same words -- which is exactly the
        state a reviewer needs told, because from inside one turn it looks
        identical to a check nobody has got to yet.
        """

        return self.latest.outcome is CheckOutcome.BLOCKED


def check_histories(answers: tuple[GateAnswer, ...]) -> dict[str, CheckHistory]:
    """The answers grouped by check id; answers naming no check are dropped."""

    grouped: dict[str, list[GateAnswer]] = {}
    for answer in answers:
        if answer.check_id:
            grouped.setdefault(answer.check_id, []).append(answer)
    return {
        check_id: CheckHistory(check_id=check_id, answers=tuple(found))
        for check_id, found in grouped.items()
    }


def stalled_checks(
    gate: HumanGate, answers: tuple[GateAnswer, ...]
) -> tuple[CheckHistory, ...]:
    """The gate's checks whose latest recorded answer was ``Blocked``.

    In the gate's own order. These are the checks that asking again, as
    written, cannot resolve.
    """

    histories = check_histories(answers)
    return tuple(
        history
        for check in gate.checks
        if (history := histories.get(check.id)) is not None and history.stalled
    )


def is_repeat_asking(gate: HumanGate, answers: tuple[GateAnswer, ...]) -> bool:
    """Every check in this gate has already been answered ``Blocked``.

    Not "some": a gate that adds a check, or re-asks one the person failed
    rather than could not attempt, is a reviewer making progress and must
    not be treated as a loop. This is the narrow case where the whole gate
    is a question the person has already said they cannot answer.
    """

    if not gate.checks:
        return False
    return len(stalled_checks(gate, answers)) == len(gate.checks)


def repeat_asking_count(gate: HumanGate, answers: tuple[GateAnswer, ...]) -> int:
    """How many times this gate's checks have been asked *and* answered.

    The number of askings a person has already answered for the check with
    the longest history in the gate. Zero for a gate nobody has answered
    yet. The gate being counted is not itself included: it has no answer.
    """

    histories = check_histories(answers)
    return max(
        (
            history.askings_answered
            for check in gate.checks
            if (history := histories.get(check.id)) is not None
        ),
        default=0,
    )


__all__ = [
    "CheckHistory",
    "GateAnswer",
    "check_histories",
    "is_repeat_asking",
    "parse_gate_answers",
    "repeat_asking_count",
    "stalled_checks",
]
