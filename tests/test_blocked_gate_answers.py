"""What the reviewer is told about answers it has already had, and what the
run does when it asks for them anyway.

The behaviour under test is one loop, closed at three points: the reviewer
is shown a tally of what has been answered (so it can see it is repeating
itself), told what a ``Blocked`` answer leaves it able to do (so it has a
route that is not NEEDS_YOU), and the run refuses to stop a person for the
same unanswerable gate a third time.
"""

import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.activity import ActivityLog
from agent_sparring.human_gate import HumanCheck, HumanGate
from agent_sparring.plan import _note_repeat_asking, record_human_evidence
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring_result
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.stage import Stage


def gate(*check_ids: str) -> HumanGate:
    return HumanGate(
        category="DEVICE_MANUAL_CHECK",
        title="Verify it on real hardware",
        checks=tuple(
            HumanCheck(
                id=check_id,
                instruction=f"do {check_id} on a real device",
                pass_criteria="it behaves",
            )
            for check_id in check_ids
        ),
    )


class BlockedAnswerPromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def ask(self, *check_ids: str) -> str:
        """Record a NEEDS_YOU gate as the engine would, returning its instance id."""

        record = record_sparring_result(
            self.stage,
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="a person must check this",
                needs_you_reason="DEVICE/MANUAL CHECK -- needs real hardware",
                human_gate=gate(*check_ids),
            ),
            findings="nothing else is outstanding",
        )
        assert record.result.human_gate is not None
        return record.result.human_gate.instance_id

    def answer(self, instance_id: str, **outcomes: str) -> None:
        lines = ["2026-09-23 — manual verification recorded in VS Code:", ""]
        for check_id, outcome in outcomes.items():
            lines.append(
                f"- {outcome} — do {check_id} on a real device · check `{check_id}` "
                f"· gate `{instance_id}`"
            )
            lines.append("  No device is available until the candidate is frozen.")
        record_human_evidence(self.stage, "\n".join(lines))

    def prompt(self) -> str:
        return " ".join(
            build_sparring_prompt(self.stage, self.sparring_dir, resume=True).split()
        )

    def test_the_reviewer_is_shown_how_many_times_each_check_was_answered(self):
        first = self.ask("device-restart", "round-trip")
        self.answer(first, **{"device-restart": "Blocked", "round-trip": "Fail"})
        second = self.ask("device-restart", "round-trip")
        self.answer(second, **{"device-restart": "Blocked", "round-trip": "Fail"})
        self.ask("device-restart", "round-trip")

        prompt = self.prompt()

        self.assertIn("What has already been answered", prompt)
        self.assertIn("`device-restart` — latest answer **Blocked**, asked and answered 2 times", prompt)
        self.assertIn("`round-trip` — latest answer **Fail**, asked and answered 2 times", prompt)

    def test_a_blocked_answer_brings_the_routes_out_of_it(self):
        # The whole deadlock: no implementation defect, unsatisfied
        # acceptance checks, so NEEDS_YOU is the only action the rules allow
        # and the reviewer re-asks forever. It needs to be told, in the turn
        # where it can act on it, that deferring is available.
        instance = self.ask("device-restart")
        self.answer(instance, **{"device-restart": "Blocked"})
        self.ask("device-restart")

        prompt = self.prompt()

        self.assertIn("Blocked**, which is not a result", prompt)
        self.assertIn("Asking it again in the same words cannot produce a different answer", prompt)
        self.assertIn("Choose READY and put those checks in `deferred_human_gate`", prompt)
        self.assertIn("deferring is not waiving", prompt)

    def test_an_answered_gate_with_nothing_blocked_does_not_get_the_blocked_guidance(self):
        instance = self.ask("device-restart")
        self.answer(instance, **{"device-restart": "Pass"})
        self.ask("device-restart")

        prompt = self.prompt()

        self.assertIn("latest answer **Pass**", prompt)
        self.assertNotIn("which is not a result", prompt)
        self.assertIn("a check they have answered well is finished", prompt)

    def test_no_tally_before_anything_has_been_answered(self):
        self.ask("device-restart")

        self.assertNotIn("What has already been answered", self.prompt())

    def test_a_plan_defined_acceptance_check_is_not_thereby_undeferrable(self):
        # The rule the reviewer was reading as a prohibition. Deferring keeps
        # the obligation in the run's ledger, so nothing is waived and no
        # boundary is crossed by continuing.
        prompt = self.prompt()

        self.assertIn("is not thereby undeferrable", prompt)
        self.assertIn(
            "What rules it out is later work in this plan depending on the answer", prompt
        )


class RepeatAskingStopTests(unittest.TestCase):
    """The run's own limit on asking a person the same impossible thing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        self.activity_path = Path(self._tmp.name) / "activity.jsonl"
        self.activity = ActivityLog(self.activity_path).bind("plan")
        self.reported: list[str] = []

    ask = BlockedAnswerPromptTests.ask
    answer = BlockedAnswerPromptTests.answer

    def note(self) -> str | None:
        return _note_repeat_asking(self.stage, self.activity, report=self.reported.append)

    def test_the_first_repetition_warns_the_person_and_lets_the_run_pause(self):
        first = self.ask("device-restart")
        self.answer(first, **{"device-restart": "Blocked"})
        self.ask("device-restart")

        self.assertIsNone(self.note(), "one repetition is a warning, not a stop")
        self.assertIn("which you have already answered Blocked", " ".join(self.reported))
        self.assertIn("the run will stop rather than ask you a third time", " ".join(self.reported))

    def test_a_blocked_check_carried_inside_a_moving_gate_is_reported_not_stopped(self):
        # The shape the real run had: the reviewer reworks one check each
        # turn while re-listing two the person cannot do. That is progress
        # and must not be stopped -- but the person is still being asked
        # twice for the same impossible thing, so they are told.
        first = self.ask("incident-evidence", "device-restart")
        self.answer(first, **{"incident-evidence": "Fail", "device-restart": "Blocked"})
        second = self.ask("incident-evidence", "device-restart")
        self.answer(second, **{"incident-evidence": "Fail", "device-restart": "Blocked"})
        self.ask("incident-evidence", "device-restart")

        self.assertIsNone(self.note(), "the gate is still moving")
        reported = " ".join(self.reported)
        self.assertIn("`device-restart`", reported)
        self.assertNotIn("`incident-evidence`", reported, "that one is being worked on")
        self.assertNotIn("a third time", reported, "no stop is coming for a moving gate")

    def test_the_third_asking_stops_the_run_instead_of_the_person(self):
        first = self.ask("device-restart")
        self.answer(first, **{"device-restart": "Blocked"})
        second = self.ask("device-restart")
        self.answer(second, **{"device-restart": "Blocked"})
        self.ask("device-restart")

        message = self.note()

        self.assertIsNotNone(message)
        self.assertIn("asked the same checks 3 times", message)
        # It stops *and* says how to get moving again, naming routes that
        # actually change what the next turn sees.
        self.assertIn("freeze the candidate", message)
        self.assertIn("answer them Fail with what you did observe", message)
        self.assertIn("Nothing was discarded", message)

    def test_a_gate_that_asks_something_new_is_never_stopped(self):
        first = self.ask("device-restart")
        self.answer(first, **{"device-restart": "Blocked"})
        second = self.ask("device-restart")
        self.answer(second, **{"device-restart": "Blocked"})
        self.ask("device-restart", "something-new")

        self.assertIsNone(self.note(), "a reviewer asking something new is working")
        self.assertNotIn("a third time", " ".join(self.reported))

    def test_a_check_answered_fail_is_answered_and_may_be_asked_again(self):
        first = self.ask("device-restart")
        self.answer(first, **{"device-restart": "Fail"})
        second = self.ask("device-restart")
        self.answer(second, **{"device-restart": "Fail"})
        self.ask("device-restart")

        self.assertIsNone(self.note())

    def test_the_repetition_is_recorded_as_telemetry(self):
        first = self.ask("device-restart")
        self.answer(first, **{"device-restart": "Blocked"})
        self.ask("device-restart")
        self.note()

        logged = self.activity_path.read_text(encoding="utf-8")
        self.assertIn("gate.repeated", logged)
        self.assertIn("asking 2 of the same blocked checks", logged)

    def test_a_stage_with_no_recorded_gate_is_not_a_repetition(self):
        self.assertIsNone(self.note())


if __name__ == "__main__":
    unittest.main()
