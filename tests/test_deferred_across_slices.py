"""Deferred checks owed by plan completion, when the plan runs as intake slices.

An intake splits one plan into run slices, each its own managed run and
often in its own repository. A check a reviewer deferred ``before plan
completion`` is owed by the *plan*: an early slice ending must not ask for it
(the thing it needs may not exist yet), later slices must be able to go on,
and the plan's last slice must not complete while it is still owed. These
tests drive the real prepare -> approve -> run-plan path across two
repositories with scripted providers.
"""

import json
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.deferred_gate import (
    CHECKPOINT_PLAN_COMPLETION,
    DeferredAnswer,
    DeferredVerificationRequired,
)
from agent_sparring.plan import PlanError, PlanRunState, PlanRunStatus, resume_plan, run_state_path
from agent_sparring.plan_obligations import load_ledger, slice_context
from agent_sparring.stage import Stage
from test_deferred_human_verification import READY_WITH_DEFERRAL
from test_intake_sealing import APP_BRANCH, _Committer, _Forbidden, _Sealed
from test_plan import READY, _SparringAdapter

WEB_BRANCH = "feature/web"
CHECK = "resize-readability"


class _TwoSliceCase(_Sealed):
    """``app`` (stages 0, 1A) then ``web`` (1B), which depends on it."""

    def setUp(self):
        super().setUp()
        self.intake = self.prepare()
        self.app = self.approve(self.intake.directory)
        # Stage 0's review defers a check to plan completion; 1A is clean.
        self.app_sparrer = _SparringAdapter([READY_WITH_DEFERRAL, READY, READY, READY])
        self.app_committer = _Committer(self.repo, APP_BRANCH)

    def run_app(self):
        return self.start(self.app, lambda s: (self.app_committer, self.app_sparrer))

    def app_state(self) -> PlanRunState:
        return PlanRunState.load(run_state_path(self.sparring_dir, self.app.run_key))

    def ledger(self):
        return load_ledger(slice_context(self.source(self.app)).ledger_path)

    def approve_web(self):
        return self.approve(self.intake.directory, run="web", confirmed_prerequisites=["release"])

    def web_source(self, web):
        return self.source(web, self.web)

    def run_web(self, web, sparrer=None):
        committer = _Committer(self.web, WEB_BRANCH)
        sparrer = sparrer or _SparringAdapter([READY] * 4)
        return self.start(web, lambda s: (committer, sparrer), repo=self.web, branch=WEB_BRANCH)

    def resume_web(self, web, **kwargs):
        return resume_plan(
            self.web_source(web), self.web / ".sparring", self.web, _Forbidden(self),
            expected_branch=WEB_BRANCH, **kwargs,
        )

    def web_state(self, web) -> PlanRunState:
        return PlanRunState.load(run_state_path(self.web / ".sparring", web.run_key))


class EarlySliceTests(_TwoSliceCase):
    def test_an_early_slice_completes_and_carries_the_check_to_the_plan(self):
        result = self.run_app()
        self.assertIs(result.status, PlanRunStatus.COMPLETE, "the slice's end is not the plan's end")
        state = self.app_state()
        self.assertEqual(state.deferred_human_checks, ())
        self.assertIsNone(state.awaiting)
        (entry,) = self.ledger()
        self.assertEqual(state.carried_deferred, (entry.instance_id,))
        self.assertEqual(entry.obligation.checkpoint, CHECKPOINT_PLAN_COMPLETION)
        self.assertEqual(entry.origin_slice, "app")
        self.assertEqual(Path(entry.origin_sparring_dir), self.sparring_dir.resolve())
        self.assertIsNone(entry.resolved_by, "carried, not answered or waived")
        self.assertEqual(entry.obligation.results, ())

    def test_later_slices_can_be_approved_and_start(self):
        self.run_app()
        web = self.approve_web()  # would refuse if app's run were not complete
        result = self.run_web(web)
        self.assertEqual([sid for sid, _ in result.accepted][-1:], [web_stage(web)])

    def test_the_obligation_survives_a_reload_of_every_file(self):
        self.run_app()
        (entry,) = self.ledger()
        path = slice_context(self.source(self.app)).ledger_path
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["obligations"][0]["obligation"]["gate"]["instance_id"], entry.instance_id)
        self.assertEqual(raw["plan"], self.source(self.app).label)
        # Nothing that lists intakes treats the ledger directory as one.
        self.assertFalse((path.parent / "intake.json").exists())


class PlanEndTests(_TwoSliceCase):
    def setUp(self):
        super().setUp()
        self.run_app()
        self.web_approval = self.approve_web()
        (self.entry,) = self.ledger()

    def test_the_last_slice_claims_the_check_and_does_not_complete(self):
        result = self.run_web(self.web_approval)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        self.assertEqual(result.awaiting.instance_ids, (self.entry.instance_id,))
        state = self.web_state(self.web_approval)
        self.assertEqual([o.instance_id for o in state.deferred_human_checks], [self.entry.instance_id])
        self.assertIs(state.status, PlanRunStatus.PAUSED)

    def test_a_plain_resume_does_not_complete_it_either(self):
        self.run_web(self.web_approval)
        again = self.resume_web(self.web_approval)
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        self.assertEqual(len(self.web_state(self.web_approval).deferred_human_checks), 1, "claimed once, not twice")

    def test_cant_test_does_not_waive_it(self):
        self.run_web(self.web_approval)
        result = self.resume_web(
            self.web_approval, deferred_results=(DeferredAnswer.parse(f"{CHECK}=blocked=no build available"),)
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        (entry,) = self.ledger()
        self.assertIsNone(entry.resolved_by)
        self.assertEqual([r.outcome.value for r in entry.obligation.results], ["blocked"])

    def test_answering_it_completes_the_plan_once_and_writes_back_to_the_raising_stage(self):
        self.run_web(self.web_approval)
        result = self.resume_web(
            self.web_approval, deferred_results=(DeferredAnswer.parse(f"{CHECK}=pass=clean audit"),)
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        (entry,) = self.ledger()
        self.assertEqual(entry.resolved_by, self.web_approval.run_key)
        # The answer is written where the check was raised: the app
        # repository's stage, not the web run that asked for it.
        notes = Stage.resolve(self.sparring_dir, self.entry.obligation.stage_id).read_notes()
        self.assertIn("clean audit", notes)
        self.assertIn(f"gate `{self.entry.instance_id}`", notes)
        # Exactly once: the run is no longer at a checkpoint to answer.
        with self.assertRaisesRegex(PlanError, "already complete; nothing to resume"):
            self.resume_web(self.web_approval, deferred_results=(DeferredAnswer.parse(f"{CHECK}=pass=again"),))


class RecoveryTests(_TwoSliceCase):
    def test_a_slice_paused_by_the_old_rule_completes_on_resume_without_replay(self):
        # What an engine without slice awareness recorded: the early slice
        # stopped at "plan completion" for a check it cannot have answered.
        with mock.patch("agent_sparring.plan.slice_context", return_value=None):
            paused = self.run_app()
        self.assertIs(paused.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(paused.awaiting, DeferredVerificationRequired)
        instance = paused.awaiting.instance_ids[0]

        # Resumed with nothing answered and no provider available: it
        # completes, and the check moves to the plan unanswered.
        result = resume_plan(
            self.source(self.app), self.sparring_dir, self.repo, _Forbidden(self),
            expected_branch=APP_BRANCH,
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        (entry,) = self.ledger()
        self.assertEqual(entry.instance_id, instance, "the same asking, not a new one")
        self.assertIsNone(entry.resolved_by)
        self.approve_web()  # and the next slice may now be approved


class SingleRunPlanTests(_Sealed):
    def test_a_plan_that_is_one_slice_still_asks_at_its_end(self):
        """A one-slice intake is the whole plan: nothing to carry to."""

        intake = self.prepare()
        interpretation = json.loads((intake.directory / "interpretation.json").read_text())
        with mock.patch(
            "agent_sparring.plan_obligations._intake_run_ids", return_value=("app",)
        ):
            approval = self.approve(intake.directory)
            result = self.start(
                approval,
                lambda s: (_Committer(self.repo, APP_BRANCH), _SparringAdapter([READY_WITH_DEFERRAL, READY])),
            )
        self.assertIn("web", [run["id"] for run in interpretation["runs"]])
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)


def web_stage(web) -> str:
    return json.loads(web.manifest_path.read_text())["manifest"]["stages"][-1]["stage_id"]
