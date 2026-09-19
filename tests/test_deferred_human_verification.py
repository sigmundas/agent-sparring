"""Deferred human verification: the reviewer's timing judgement, and the
obligations the engine keeps whatever the reviewer decided.

The split under test is the whole point of the feature:

    NEEDS_YOU + human_gate            stop now (unchanged by any of this)
    READY     + deferred_human_gate   accept the stage, keep the obligation

So these tests are mostly about what the *engine* refuses to forget, not
about which checks a reviewer should defer -- that is the reviewer's call and
nothing here second-guesses it.
"""

import json
import unittest

import conftest_path  # noqa: F401

from agent_sparring.deferred_gate import (
    CHECKPOINT_PLAN_COMPLETION,
    CheckOutcome,
    CheckResult,
    DeferredAnswer,
    DeferredGateError,
    DeferredHumanGate,
    DeferredObligation,
    DeferredVerificationRequired,
    ObligationStatus,
)
from agent_sparring.human_gate import HumanGate, new_gate_instance_id
from agent_sparring.plan import PlanError, PlanRunState, PlanRunStatus
from agent_sparring.routing import RoutingAction, RoutingResult, RoutingResultError
from agent_sparring.sparring_exchange import read_recorded_outcome, record_sparring_result
from agent_sparring.stage import Stage, StageStatus

from test_plan import (  # noqa: E402  -- shared plan-run fixtures
    KEY,
    NEEDS_YOU,
    READY,
    S1,
    S2,
    _PlanRepoTestCase,
    _SparringAdapter,
    _StageAdapter,
    human_gate,
)


# ---------------------------------------------------------------- fixtures


def deferred_gate(
    *,
    category: str = "UI_VISUAL_CHECK",
    checks: list[dict] | None = None,
    rationale: str = (
        "Later stages consume the comparison model, not its pixel layout, and a failed "
        "readability check would need only a local UI adjustment."
    ),
    checkpoint: str = CHECKPOINT_PLAN_COMPLETION,
) -> dict:
    return {
        "category": category,
        "title": "Check comparison readability in the real UI",
        "checks": checks
        or [
            {
                "id": "resize-readability",
                "instruction": "Open the summary dialog and resize it from 1400px to 700px.",
                "pass_criteria": "Labels stay legible and nothing clips. Fail if text overlaps.",
                "source": None,
            }
        ],
        "rationale": rationale,
        "checkpoint": checkpoint,
    }


def verdict(
    action: str,
    summary: str,
    *,
    reason: str | None = None,
    gate: dict | None = None,
    deferred: dict | None = None,
    promote: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": reason,
            "findings": f"findings: {summary}",
            "deferred": None,
            "human_gate": gate,
            "deferred_human_gate": deferred,
            "promote_deferred": promote or [],
        }
    )


READY_WITH_DEFERRAL = verdict("READY", "looks good", deferred=deferred_gate())


def _obligation(stage_id: str = "stage-1", check_id: str = "resize-readability"):
    payload = deferred_gate(
        checks=[
            {
                "id": check_id,
                "instruction": "Resize and look.",
                "pass_criteria": "Readable.",
                "source": None,
            }
        ]
    )
    gate = DeferredHumanGate.from_dict(payload).asked_again(new_gate_instance_id())
    return DeferredObligation.from_gate(stage_id, gate)


# ---------------------------------------------------------------- the contract


class SparrerContractTests(unittest.TestCase):
    """Scenario 10: what a reviewer may and may not say."""

    def test_ready_may_carry_a_deferred_gate(self):
        result = RoutingResult.from_dict(
            {"action": "READY", "summary": "ok", "deferred_human_gate": deferred_gate()}
        )
        self.assertIsNotNone(result.deferred_human_gate)
        self.assertEqual(result.deferred_human_gate.checkpoint, CHECKPOINT_PLAN_COMPLETION)

    def test_needs_you_still_requires_an_immediate_gate(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict({"action": "NEEDS_YOU", "summary": "ask"})

    def test_immediate_gate_is_still_illegal_on_ready(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict(
                {"action": "READY", "summary": "ok", "human_gate": human_gate()}
            )

    def test_deferred_gate_is_illegal_on_every_action_but_ready(self):
        for action in ("SEND_BACK", "ESCALATE"):
            with self.assertRaises(RoutingResultError, msg=action):
                RoutingResult.from_dict(
                    {"action": action, "summary": "x", "deferred_human_gate": deferred_gate()}
                )
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict(
                {
                    "action": "NEEDS_YOU",
                    "summary": "x",
                    "human_gate": human_gate(),
                    "deferred_human_gate": deferred_gate(),
                }
            )

    def test_a_deferral_without_a_rationale_is_rejected(self):
        for bad in ("", "   ", None):
            payload = deferred_gate()
            payload["rationale"] = bad
            with self.assertRaises(RoutingResultError, msg=repr(bad)):
                RoutingResult.from_dict(
                    {"action": "READY", "summary": "ok", "deferred_human_gate": payload}
                )

    def test_an_unimplemented_checkpoint_is_rejected_rather_than_ignored(self):
        payload = deferred_gate(checkpoint="before_stage:stage-4-prefetch")
        with self.assertRaises(RoutingResultError) as ctx:
            RoutingResult.from_dict(
                {"action": "READY", "summary": "ok", "deferred_human_gate": payload}
            )
        self.assertIn("checkpoint", str(ctx.exception))

    def test_malformed_deferred_structures_are_rejected(self):
        for payload in ({"category": "UI_VISUAL_CHECK"}, {"title": "x", "checks": []}, "prose", 7):
            with self.assertRaises(RoutingResultError, msg=repr(payload)):
                RoutingResult.from_dict(
                    {"action": "READY", "summary": "ok", "deferred_human_gate": payload}
                )

    def test_promote_deferred_must_be_a_list_of_distinct_ids(self):
        with self.assertRaises(RoutingResultError):
            RoutingResult.from_dict(
                {"action": "READY", "summary": "ok", "promote_deferred": "one-id"}
            )
        with self.assertRaises(RoutingResultError):
            RoutingResult(action=RoutingAction.READY, summary="ok", promote_deferred=("a", "a"))

    def test_a_verdict_with_neither_field_is_exactly_what_it_was(self):
        result = RoutingResult.from_dict({"action": "READY", "summary": "ok"})
        self.assertIsNone(result.deferred_human_gate)
        self.assertEqual(result.promote_deferred, ())
        self.assertEqual(result.to_dict(), {"action": "READY", "summary": "ok"})


class ObligationModelTests(unittest.TestCase):
    def test_status_is_derived_from_the_recorded_results(self):
        obligation = _obligation()
        self.assertIs(obligation.status, ObligationStatus.PENDING)
        passed = obligation.with_result(
            CheckResult(check_id="resize-readability", outcome=CheckOutcome.PASS)
        )
        self.assertIs(passed.status, ObligationStatus.PASSED)
        self.assertTrue(passed.resolved)
        failed = obligation.with_result(
            CheckResult(check_id="resize-readability", outcome=CheckOutcome.FAIL)
        )
        self.assertIs(failed.status, ObligationStatus.FAILED)
        self.assertFalse(failed.resolved)

    def test_blocked_resolves_nothing(self):
        obligation = _obligation().with_result(
            CheckResult(check_id="resize-readability", outcome=CheckOutcome.BLOCKED)
        )
        self.assertIs(obligation.status, ObligationStatus.PENDING)
        self.assertFalse(obligation.resolved)
        self.assertEqual([c.id for c in obligation.unanswered], ["resize-readability"])

    def test_promotion_keeps_the_same_asking(self):
        obligation = _obligation()
        promoted = obligation.promote()
        self.assertTrue(promoted.promoted)
        self.assertEqual(promoted.instance_id, obligation.instance_id)

    def test_an_obligation_round_trips_through_json(self):
        obligation = _obligation().with_result(
            CheckResult(check_id="resize-readability", outcome=CheckOutcome.FAIL, note="clipped")
        )
        again = DeferredObligation.from_dict(json.loads(json.dumps(obligation.to_dict())))
        self.assertEqual(again, obligation)

    def test_an_obligation_without_an_engine_minted_instance_is_refused(self):
        gate = HumanGate.from_dict(deferred_gate())
        with self.assertRaises(DeferredGateError):
            DeferredObligation(stage_id="stage-1", gate=gate, rationale="because")

    def test_an_answer_parses_its_three_parts(self):
        answer = DeferredAnswer.parse("resize-readability=pass=looked fine at 900=700px")
        self.assertEqual(answer.check_id, "resize-readability")
        self.assertIsNone(answer.instance_id)
        self.assertIs(answer.outcome, CheckOutcome.PASS)
        self.assertEqual(answer.note, "looked fine at 900=700px")
        qualified = DeferredAnswer.parse("abc123:resize-readability=fail")
        self.assertEqual(qualified.instance_id, "abc123")
        self.assertEqual(qualified.check_id, "resize-readability")
        for bad in ("", "no-outcome", "id=nonsense"):
            with self.assertRaises(DeferredGateError, msg=bad):
                DeferredAnswer.parse(bad)


class RecordedVerdictTests(unittest.TestCase):
    """Scenario 8's other half: identity is minted by the engine, per asking."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stage = Stage.resolve(Path(tmp.name) / ".sparring", "stage-1-x").create()

    def _ready_with_deferral(self) -> RoutingResult:
        return RoutingResult.from_dict(
            {"action": "READY", "summary": "ok", "deferred_human_gate": deferred_gate()}
        )

    def test_recording_mints_an_instance_and_returns_the_recorded_verdict(self):
        record = record_sparring_result(self.stage, self._ready_with_deferral())
        minted = record.result.deferred_human_gate.instance_id
        self.assertIsNotNone(minted)
        self.assertIn(minted, record.content)
        self.assertIn("Human verification deferred, not waived", record.content)
        self.assertIn("Reviewer's rationale for deferring:", record.content)

    def test_every_recording_is_a_new_asking(self):
        first = record_sparring_result(self.stage, self._ready_with_deferral())
        second = record_sparring_result(self.stage, self._ready_with_deferral())
        self.assertNotEqual(
            first.result.deferred_human_gate.instance_id,
            second.result.deferred_human_gate.instance_id,
        )

    def test_the_recorded_file_reads_back_as_ready_and_not_awaiting_a_human(self):
        record_sparring_result(self.stage, self._ready_with_deferral())
        outcome = read_recorded_outcome(self.stage)
        self.assertIs(outcome.action, RoutingAction.READY)
        self.assertFalse(outcome.awaits_a_human)
        self.assertIsNotNone(outcome.deferred_human_gate)
        self.assertEqual(
            outcome.deferred_human_gate.title, "Check comparison readability in the real UI"
        )

    def test_a_verdict_recorded_before_deferrals_existed_reads_back_unchanged(self):
        record_sparring_result(
            self.stage, RoutingResult(action=RoutingAction.READY, summary="plain")
        )
        outcome = read_recorded_outcome(self.stage)
        self.assertIs(outcome.action, RoutingAction.READY)
        self.assertIsNone(outcome.deferred_human_gate)


# ---------------------------------------------------------------- the run


class _DeferredRunCase(_PlanRepoTestCase):
    """One stage adapter for the whole test.

    Deliberately shared between the initial run and every resume: the
    adapter names the file it commits after its own call count, so two
    adapter objects in one repository would both try to commit the same
    unchanged path and git would (rightly) refuse.
    """

    def setUp(self):
        super().setUp()
        self.stage_adapter = _StageAdapter(self.repo, commit=True)

    def _turns(self) -> int:
        return len(self.stage_adapter.start_calls) + len(self.stage_adapter.resume_calls)


class DeferredRunTests(_DeferredRunCase):
    def test_immediate_gate_is_unchanged(self):
        """Scenario 1: NEEDS_YOU still stops the run where it always did."""

        result = self._start(self.stage_adapter, _SparringAdapter([NEEDS_YOU]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, S1)
        self.assertIs(result.routing.action, RoutingAction.NEEDS_YOU)
        self.assertIsNone(result.awaiting)
        self.assertEqual(self._plan_state().deferred_human_checks, ())
        self.assertIsNot(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

    def test_a_deferred_check_accepts_the_stage_and_lets_the_next_one_start(self):
        """Scenario 2: the run does not stop, and the ledger remembers."""

        sparring = _SparringAdapter([READY_WITH_DEFERRAL, READY])
        result = self._start(self.stage_adapter, sparring)

        # Stage 1 accepted, stage 2 ran without anybody being asked anything.
        self.assertEqual([sid for sid, _ in result.accepted], [S1, S2])
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(len(sparring.start_calls), 2)

        state = self._plan_state()
        self.assertEqual(len(state.deferred_human_checks), 1)
        owed = state.deferred_human_checks[0]
        self.assertEqual(owed.stage_id, S1)
        self.assertIn("pixel layout", owed.rationale)

        # ... and the plan did not complete on it.
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIs(state.status, PlanRunStatus.PAUSED)

    def test_the_plan_pauses_instead_of_completing(self):
        """Scenario 4."""

        result = self._start(
            self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, READY])
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        self.assertEqual(len(result.deferred), 1)
        self.assertEqual(
            result.awaiting.instance_ids, (result.deferred[0].instance_id,)
        )

    def test_the_obligation_survives_save_and_load(self):
        """Scenario 3: durability across a process boundary."""

        self._start(self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, READY]))
        reloaded = PlanRunState.load(self.state_path)
        self.assertEqual(len(reloaded.deferred_human_checks), 1)
        self.assertEqual(reloaded.deferred_human_checks[0].stage_id, S1)
        self.assertIsInstance(reloaded.awaiting, DeferredVerificationRequired)

        # A resume with nothing new to say re-derives exactly the same pause,
        # without re-running a single accepted stage.
        before = self._turns()
        sparring = _SparringAdapter([])
        again = self._resume(self.stage_adapter, sparring)
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(again.awaiting, DeferredVerificationRequired)
        self.assertEqual(self._turns(), before)
        self.assertEqual(sparring.start_calls, [])

    def test_a_pass_resolves_the_checkpoint_and_the_plan_completes(self):
        """Scenario 5."""

        self._start(self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, READY]))
        instance = self._plan_state().deferred_human_checks[0].instance_id
        before = self._turns()

        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([]),
            deferred_results=(
                DeferredAnswer.parse("resize-readability=pass=legible down to 700px"),
            ),
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self._turns(), before)  # nothing re-run
        state = self._plan_state()
        self.assertIsNone(state.awaiting)
        self.assertTrue(state.deferred_human_checks[0].resolved)

        # Provenance: the answer is written back to the stage that raised it,
        # in the same shape a human-gate answer is recorded in.
        notes = self._stage(S1).read_notes()
        self.assertIn("## Human evidence", notes)
        self.assertIn("- Pass — Open the summary dialog", notes)
        self.assertIn("· check `resize-readability`", notes)
        self.assertIn(f"· gate `{instance}`", notes)
        self.assertIn("legible down to 700px", notes)

    def test_a_fail_keeps_the_plan_unresolved_and_says_what_needs_correcting(self):
        """Scenario 6."""

        self._start(
            self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, READY])
        )
        reports: list[str] = []
        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([]),
            deferred_results=(
                DeferredAnswer.parse("resize-readability=fail=labels overlap under 800px"),
            ),
            report=reports.append,
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)
        obligation = self._plan_state().deferred_human_checks[0]
        self.assertIs(obligation.status, ObligationStatus.FAILED)

        text = "\n".join(reports)
        self.assertIn("FAILED", text)
        self.assertIn("labels overlap under 800px", text)
        self.assertIn("will not complete", text)
        self.assertIn("- Fail — Open the summary dialog", self._stage(S1).read_notes())

    def test_a_blocked_answer_does_not_complete_the_plan(self):
        self._start(
            self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, READY])
        )
        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([]),
            deferred_results=(DeferredAnswer.parse("resize-readability=blocked"),),
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertFalse(self._plan_state().deferred_human_checks[0].resolved)

    def test_checks_from_two_stages_accumulate_with_their_origins_intact(self):
        """Scenario 7."""

        second = verdict(
            "READY",
            "also fine",
            deferred=deferred_gate(
                category="DEVICE_MANUAL_CHECK",
                checks=[
                    {
                        "id": "android-smoke",
                        "instruction": "Open it once on a real Android device.",
                        "pass_criteria": "No crash.",
                        "source": None,
                    }
                ],
                rationale="No later stage reads this surface; a failure is a local fix.",
            ),
        )
        result = self._start(
            self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, second])
        )
        owed = self._plan_state().deferred_human_checks
        self.assertEqual([entry.stage_id for entry in owed], [S1, S2])
        self.assertEqual(len({entry.instance_id for entry in owed}), 2)
        self.assertEqual(len(result.deferred), 2)
        # One checkpoint, both obligations.
        self.assertEqual(len(result.awaiting.instance_ids), 2)

        state = self._plan_state()
        self._resume(
            self.stage_adapter,
            _SparringAdapter([]),
            deferred_results=(
                DeferredAnswer.parse("resize-readability=pass"),
                DeferredAnswer.parse("android-smoke=pass"),
            ),
        )
        self.assertIn("resize-readability", self._stage(S1).read_notes())
        self.assertNotIn("android-smoke", self._stage(S1).read_notes())
        self.assertIn("android-smoke", self._stage(S2).read_notes())

    def test_an_answer_to_an_earlier_asking_does_not_satisfy_a_new_one(self):
        """Scenario 8: stale evidence, and the qualified-ref escape hatch."""

        # The same reviewer defers the same check id twice, in two stages.
        again = verdict("READY", "same check again", deferred=deferred_gate())
        self._start(self.stage_adapter, _SparringAdapter([READY_WITH_DEFERRAL, again]))
        owed = self._plan_state().deferred_human_checks
        self.assertEqual(len(owed), 2)
        self.assertNotEqual(owed[0].instance_id, owed[1].instance_id)

        # A bare id is now ambiguous, and is refused rather than guessed.
        with self.assertRaises(PlanError) as ctx:
            self._resume(
                self.stage_adapter,
                _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse("resize-readability=pass"),),
            )
        self.assertIn("more than one asking", str(ctx.exception))

        # Answering the first asking leaves the second one owed.
        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([]),
            deferred_results=(
                DeferredAnswer.parse(f"{owed[0].instance_id}:resize-readability=pass"),
            ),
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        state = self._plan_state()
        self.assertTrue(state.deferred_human_checks[0].resolved)
        self.assertFalse(state.deferred_human_checks[1].resolved)

    def test_a_result_is_refused_when_the_run_is_not_at_the_checkpoint(self):
        self._start(self.stage_adapter, _SparringAdapter([NEEDS_YOU]))
        with self.assertRaises(PlanError) as ctx:
            self._resume(
                self.stage_adapter,
                _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse("resize-readability=pass"),),
            )
        self.assertIn("not stopped for deferred human verification", str(ctx.exception))

    def test_the_next_reviewer_is_shown_what_the_run_already_owes(self):
        """Promotion is only possible if a later reviewer can see the ledger."""

        sparring = _SparringAdapter([READY_WITH_DEFERRAL, READY])
        self._start(self.stage_adapter, sparring)
        instance = self._plan_state().deferred_human_checks[0].instance_id

        self.assertIn(
            "Human verification already owed",
            sparring.start_calls[1],
        )
        self.assertIn(instance, sparring.start_calls[1])

    def test_an_unknown_promotion_target_is_reported_and_ignored(self):
        promoting = verdict("READY", "fine", promote=["not-a-real-instance"])
        reports: list[str] = []
        result = self._start(
            self.stage_adapter, _SparringAdapter([promoting, READY]), report=reports.append
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIn("not in this run's ledger", "\n".join(reports))


class PromotionTests(_DeferredRunCase):
    """A three-stage run so a later reviewer has something to promote."""

    def setUp(self):
        super().setUp()
        from test_plan import THREE_STAGE_PLAN

        self.plan_path.write_text(THREE_STAGE_PLAN, encoding="utf-8")
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qam", "three stages"],
            check=True,
            capture_output=True,
        )

    def test_promotion_stops_the_run_before_the_next_stage(self):
        sparring = _SparringAdapter([READY_WITH_DEFERRAL, "PROMOTE", READY])

        # The second verdict has to name the instance the first one minted,
        # which only exists once the first turn has run.
        original_next = sparring._next

        def _next(session_id):
            if sparring._verdicts and sparring._verdicts[0] == "PROMOTE":
                owed = PlanRunState.load(self.state_path).deferred_human_checks
                sparring._verdicts[0] = verdict(
                    "READY",
                    "stage 3 will build on that behaviour",
                    promote=[owed[0].instance_id],
                )
            return original_next(session_id)

        sparring._next = _next

        result = self._start(self.stage_adapter, sparring)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PROMOTED)
        # Both earlier stages are accepted; the third was never entered.
        self.assertEqual(len(result.accepted), 2)
        self.assertFalse(self._stage(f"{KEY}-stage-3-prefetch-and-enrichment").exists())
        self.assertTrue(self._plan_state().deferred_human_checks[0].promoted)

        # Answering it lets the run continue into stage 3 and finish.
        done = self._resume(
            self.stage_adapter,
            _SparringAdapter([READY]),
            deferred_results=(DeferredAnswer.parse("resize-readability=pass"),),
        )
        self.assertIs(done.status, PlanRunStatus.COMPLETE)


class BackwardCompatibilityTests(_DeferredRunCase):
    """Scenario 9: a state file written before any of this still loads."""

    def test_state_without_the_field_loads_and_behaves_identically(self):
        self._start(self.stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("deferred_human_checks", payload)
        del payload["deferred_human_checks"]
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        state = PlanRunState.load(self.state_path)
        self.assertEqual(state.deferred_human_checks, ())
        self.assertEqual(state.unresolved_deferred, ())

        result = self._resume(self.stage_adapter, _SparringAdapter([READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_a_recorded_push_pause_still_loads_as_one(self):
        state = PlanRunState(
            plan="docs/plan.md",
            plan_digest="d",
            expected_branch="feature/x",
            current_stage_index=0,
            current_stage=S1,
            status=PlanRunStatus.PAUSED,
            awaiting=None,
        )
        payload = state.to_dict()
        payload["awaiting"] = {
            "kind": "push_authorization_required",
            "stage_id": S1,
            "candidate_sha": "a" * 40,
            "branch": "feature/x",
            "remote": "origin",
            "remote_branch": "feature/x",
        }
        again = PlanRunState.from_dict(payload)
        self.assertEqual(again.awaiting.kind, "push_authorization_required")


if __name__ == "__main__":
    unittest.main()
