"""Reading a person's gate answers back as data, and noticing a repetition.

The failure these exist for, taken from a real run: a reviewer asked three
acceptance checks, the person answered two of them ``Blocked`` with a
structural reason ("I can test this once the candidate is frozen"), and the
reviewer -- no implementation defect to send back, unsatisfied acceptance
checks, so NEEDS_YOU the only legal action -- issued the same gate again. And
again. Nothing in the engine noticed, because an answer had only ever been a
paragraph.
"""

import unittest

import conftest_path  # noqa: F401

from agent_sparring.deferred_gate import CheckOutcome
from agent_sparring.gate_answers import (
    check_histories,
    is_repeat_asking,
    parse_gate_answers,
    repeat_asking_count,
    stalled_checks,
)
from agent_sparring.human_gate import HumanCheck, HumanGate


def gate(*check_ids: str) -> HumanGate:
    return HumanGate(
        category="OTHER",
        title="t",
        checks=tuple(
            HumanCheck(id=check_id, instruction="do it", pass_criteria="it worked")
            for check_id in check_ids
        ),
    )


def notes(*entries: str) -> str:
    return "\n".join(["# Notes", "", "## Human evidence", "", *entries, ""])


#: The exact shape the VS Code panel writes, including the indented note.
REAL_ROUND = notes(
    "2026-09-23 — manual verification recorded in VS Code against the checks of Plan:",
    "",
    "- Fail — Capture the raw debug response · check `incident-evidence` · gate `2635edf5`",
    "  Fail — hypothesis refuted, mechanism not established.",
    "- Blocked — Save and restart after a native selection · check `desktop-restart` · gate `2635edf5`",
    "  Can't test against the candidate: the stage is being returned to implementation.",
    "",
    "  Perform this against the revised candidate.",
)


class ParsingTests(unittest.TestCase):
    def test_a_real_recorded_round_parses_into_outcomes_ids_and_notes(self):
        answers = parse_gate_answers(REAL_ROUND)

        self.assertEqual(len(answers), 2)
        first, second = answers
        self.assertEqual(first.outcome, CheckOutcome.FAIL)
        self.assertEqual(first.check_id, "incident-evidence")
        self.assertEqual(first.gate_instance_id, "2635edf5")
        self.assertEqual(second.outcome, CheckOutcome.BLOCKED)
        self.assertEqual(second.check_id, "desktop-restart")
        # Both indented lines belong to the bullet above them, blank line
        # and all: a person's reason is usually more than one sentence.
        self.assertIn("Can't test against the candidate", second.note)
        self.assertIn("revised candidate", second.note)

    def test_the_dated_lead_in_and_ordinary_prose_are_not_results(self):
        # `resume-plan --evidence` takes free text. Anything this cannot read
        # as a result simply is not one; nothing is guessed from wording.
        answers = parse_gate_answers(
            notes(
                "2026-09-23 — manual verification recorded in VS Code:",
                "",
                "I could not test the desktop client at all today.",
                "- some other bullet entirely",
            )
        )

        self.assertEqual(answers, ())

    def test_freeform_feedback_is_never_a_check_result(self):
        # Its own sub-heading exists precisely so its words are not mined
        # for outcomes, however much they look like one.
        answers = parse_gate_answers(
            notes(
                "- Pass — a real result · check `real`",
                "",
                "### Additional human feedback",
                "",
                "- Blocked — this sentence is feedback, not a recorded outcome",
            )
        )

        self.assertEqual([a.check_id for a in answers], ["real"])

    def test_a_line_from_before_gate_instances_still_parses(self):
        answers = parse_gate_answers(notes("- Pass — did it · check `old-check`"))

        self.assertEqual(answers[0].check_id, "old-check")
        self.assertIsNone(answers[0].gate_instance_id)

    def test_a_reviewer_request_marker_does_not_hide_the_outcome(self):
        answers = parse_gate_answers(notes("- Fail — try it · reviewer request"))

        self.assertEqual(answers[0].outcome, CheckOutcome.FAIL)
        self.assertIsNone(answers[0].check_id)

    def test_a_fenced_block_is_not_read(self):
        answers = parse_gate_answers(
            notes("```", "- Pass — pasted log line · check `fenced`", "```")
        )

        self.assertEqual(answers, ())

    def test_no_section_and_no_notes_are_both_empty(self):
        self.assertEqual(parse_gate_answers(None), ())
        self.assertEqual(parse_gate_answers("# Notes\n\n## Deferred checks\n\nnone\n"), ())

    def test_a_later_heading_ends_the_section(self):
        answers = parse_gate_answers(
            notes("- Pass — inside · check `inside`")
            + "\n## Deferred checks\n\n- Blocked — outside · check `outside`\n"
        )

        self.assertEqual([a.check_id for a in answers], ["inside"])


class HistoryTests(unittest.TestCase):
    def two_rounds(self) -> str:
        return notes(
            "- Blocked — do it · check `blocked-twice` · gate `first`",
            "  no environment",
            "- Fail — do it · check `failed-then` · gate `first`",
            "- Blocked — do it · check `blocked-twice` · gate `second`",
            "  still no environment",
            "- Pass — do it · check `failed-then` · gate `second`",
        )

    def test_askings_are_counted_by_gate_instance(self):
        histories = check_histories(parse_gate_answers(self.two_rounds()))

        self.assertEqual(histories["blocked-twice"].askings_answered, 2)
        self.assertTrue(histories["blocked-twice"].stalled)

    def test_the_latest_answer_is_the_one_that_counts(self):
        # Answered Fail, then Pass: the check is not stalled, and a reviewer
        # must not be told it is.
        histories = check_histories(parse_gate_answers(self.two_rounds()))

        self.assertEqual(histories["failed-then"].latest.outcome, CheckOutcome.PASS)
        self.assertFalse(histories["failed-then"].stalled)

    def test_answers_from_before_gate_instances_each_count_as_an_asking(self):
        # Two answers with nothing to attribute them to are two answers.
        # Collapsing them into one asking would undercount the repetition
        # this whole module exists to notice.
        histories = check_histories(
            parse_gate_answers(
                notes("- Blocked — do it · check `x`", "- Blocked — do it · check `x`")
            )
        )

        self.assertEqual(histories["x"].askings_answered, 2)

    def test_an_answer_naming_no_check_is_not_grouped(self):
        self.assertEqual(check_histories(parse_gate_answers(notes("- Pass — loose"))), {})


class RepetitionTests(unittest.TestCase):
    ALL_BLOCKED = notes(
        "- Blocked — a · check `a` · gate `first`",
        "- Blocked — b · check `b` · gate `first`",
    )

    def test_a_gate_of_only_blocked_checks_is_a_repeat_asking(self):
        answers = parse_gate_answers(self.ALL_BLOCKED)

        self.assertTrue(is_repeat_asking(gate("a", "b"), answers))
        self.assertEqual(repeat_asking_count(gate("a", "b"), answers), 1)

    def test_adding_one_unanswered_check_is_progress_not_a_repeat(self):
        # The narrowness is the point: a reviewer that asks something new,
        # or re-asks a check the person *failed* rather than could not
        # attempt, is working. Only a gate that is wholly a question already
        # declared unanswerable counts.
        answers = parse_gate_answers(self.ALL_BLOCKED)

        self.assertFalse(is_repeat_asking(gate("a", "b", "c"), answers))

    def test_a_failed_check_is_an_answer_and_does_not_stall(self):
        answers = parse_gate_answers(
            notes("- Blocked — a · check `a` · gate `first`", "- Fail — b · check `b` · gate `first`")
        )

        self.assertFalse(is_repeat_asking(gate("a", "b"), answers))
        self.assertEqual([h.check_id for h in stalled_checks(gate("a", "b"), answers)], ["a"])

    def test_an_unanswered_gate_is_not_a_repeat(self):
        self.assertFalse(is_repeat_asking(gate("a"), ()))
        self.assertEqual(repeat_asking_count(gate("a"), ()), 0)

    def test_an_empty_gate_is_never_a_repeat(self):
        self.assertFalse(is_repeat_asking(gate(), parse_gate_answers(self.ALL_BLOCKED)))

    def test_the_count_is_of_the_most_asked_check_in_the_gate(self):
        answers = parse_gate_answers(
            notes(
                "- Blocked — a · check `a` · gate `first`",
                "- Blocked — a · check `a` · gate `second`",
                "- Blocked — b · check `b` · gate `second`",
            )
        )

        self.assertEqual(repeat_asking_count(gate("a", "b"), answers), 2)


if __name__ == "__main__":
    unittest.main()
