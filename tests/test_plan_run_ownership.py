"""Stage instances belong to the managed run that made them.

The failure this file is written from, reproduced end to end: one plan was
run to completion on a feature branch, a *different* follow-up plan was then
started on the same branch and in the same worktree, and because both plans
generated the stage ids ``stage-1-...``/``stage-2-...`` the second plan found
the first plan's ACCEPTED stages, adopted them and completed as a no-op
without running anything.

The invariant these tests hold is that execution-stage identity is
``(run, stage)``: a stage recorded as owned by one managed plan run is never
the stage instance of another, whatever the two plans call their stages, and
no flag says otherwise. Adoption stays what it is for -- a hand-driven,
*unowned* sequence deliberately taken into a managed run -- and the earlier
plan's history stays on disk and intact throughout.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.manifest import load_manifest_source
from agent_sparring.plan import (
    PlanError,
    PlanRunState,
    PlanRunStatus,
    plan_key,
    plan_state_path,
    start_plan,
)
from agent_sparring.stage import Stage, StageState, StageStatus
from test_plan import (  # noqa: E402  (shared scripted adapters and verdicts)
    READY,
    _SparringAdapter,
    _StageAdapter,
    _fixed,
    _run_git,
)

PLAN_A = "docs/plans/first.md"
PLAN_B = "docs/plans/follow-up.md"

# What the bug needed: two plans whose stage sections generate the very same
# ids. A follow-up plan numbered Stage 1..2 again is an ordinary workflow, so
# this is the collision the identity model has to survive rather than a
# malformed input.
COLLIDING = ("stage-1-foundation", "stage-2-transport")


def _brief(stage_id: str, plan_label: str) -> str:
    return f"# Stage brief: {stage_id}\n\nFrom plan `{plan_label}`.\n"


def manifest_payload(plan_label: str, stage_ids=COLLIDING) -> dict:
    return {
        "version": 1,
        "plan_label": plan_label,
        "source_digest": f"sha256:{plan_key(plan_label)}",
        "stages": [
            {
                "stage_id": stage_id,
                "label": f"Stage {position}",
                "title": stage_id,
                "brief": _brief(stage_id, plan_label),
            }
            for position, stage_id in enumerate(stage_ids, start=1)
        ],
    }


class _TwoPlanRepoTestCase(unittest.TestCase):
    """One real repo, one branch, and two plan documents run over it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(remote))
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.manifests = root / "manifests"
        self.manifests.mkdir()
        # One implementation adapter for the whole repository, so successive
        # plans commit successive files rather than re-writing the first
        # plan's and finding nothing to commit.
        self.stage_adapter = _StageAdapter(self.repo, commit=True)

    # -- driving ---------------------------------------------------------

    def manifest_for(self, plan_label: str, stage_ids=COLLIDING) -> Path:
        path = self.manifests / f"{plan_key(plan_label)}.json"
        path.write_text(json.dumps(manifest_payload(plan_label, stage_ids), indent=2) + "\n", encoding="utf-8")
        return path

    def start(self, plan_label: str, *, stage_ids=COLLIDING, verdicts=None, adopt=False, report=None):
        """Start ``plan_label``; returns its result and how many fresh
        implementation sessions this run started."""

        before = len(self.stage_adapter.start_calls)
        result = start_plan(
            load_manifest_source(self.manifest_for(plan_label, stage_ids)),
            self.sparring_dir,
            self.repo,
            _fixed(self.stage_adapter, _SparringAdapter(list(verdicts or [READY, READY]))),
            expected_branch="feature/x",
            adopt=adopt,
            report=report or (lambda message: None),
        )
        return result, len(self.stage_adapter.start_calls) - before

    # -- reading back ----------------------------------------------------

    def stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def state_of(self, stage_id: str) -> StageState:
        return self.stage(stage_id).read_state()

    def run_state(self, plan_label: str) -> PlanRunState:
        return PlanRunState.load(plan_state_path(self.sparring_dir, plan_label))

    def complete_plan_a(self):
        """Plan A, run to completion: both stages accepted and owned by A."""

        result, _ = self.start(PLAN_A)
        self.assertIs(result.status, PlanRunStatus.COMPLETE, "the fixture itself must be sound")
        for stage_id in COLLIDING:
            self.assertIs(self.state_of(stage_id).status, StageStatus.ACCEPTED)
            self.assertEqual(self.state_of(stage_id).plan, plan_key(PLAN_A))
        return result


class NewPlanAfterCompletedPlanTests(_TwoPlanRepoTestCase):
    """The reported failure: a follow-up plan on the same branch."""

    def test_a_follow_up_plan_with_its_own_stage_ids_runs_as_new_work(self):
        # The acceptance criterion. Same repository, same branch, same
        # worktree, Stage 1..2 numbered again -- and this is genuinely new
        # work, not an answer already given.
        self.complete_plan_a()
        fresh = (f"{plan_key(PLAN_B)}-stage-1-foundation", f"{plan_key(PLAN_B)}-stage-2-transport")

        reported: list[str] = []
        result, started = self.start(PLAN_B, stage_ids=fresh, report=reported.append)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], list(fresh))
        # Its stages really executed rather than being walked past.
        self.assertEqual(started, 2)
        self.assertFalse(
            [line for line in reported if "adopting" in line],
            "nothing was adopted: these are new stage instances",
        )
        # Fresh sessions of its own, and ownership recorded from creation.
        for stage_id in fresh:
            state = self.state_of(stage_id)
            self.assertEqual(state.plan, plan_key(PLAN_B))
            self.assertIsNotNone(state.implementation_session_id)

    def test_plan_a_history_is_still_intact_and_still_plan_as(self):
        self.complete_plan_a()
        fresh = (f"{plan_key(PLAN_B)}-stage-1-foundation",)
        self.start(PLAN_B, stage_ids=fresh, verdicts=[READY])

        self.assertIs(self.run_state(PLAN_A).status, PlanRunStatus.COMPLETE)
        for stage_id in COLLIDING:
            state = self.state_of(stage_id)
            self.assertIs(state.status, StageStatus.ACCEPTED)
            self.assertIsNotNone(state.candidate_sha)
            self.assertEqual(state.plan, plan_key(PLAN_A))
            self.assertEqual(self.stage(stage_id).read_brief(), _brief(stage_id, PLAN_A))
        # Two managed runs, side by side, neither standing in for the other.
        self.assertEqual(self.run_state(PLAN_B).plan, PLAN_B)

    def test_colliding_stage_ids_refuse_rather_than_complete_as_a_no_op(self):
        # The exact reported symptom, held as a refusal. If a caller does
        # generate ids that collide with another run's stages, the answer is
        # an error naming them -- never a run that reports every stage
        # already ACCEPTED and completes having done nothing.
        self.complete_plan_a()

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_B)
        # Ownership answers first, and says the useful thing: not "these
        # directories are in the way" but "they are another run's stages".
        self.assertIn("belong to another managed plan run", str(ctx.exception))
        self.assertFalse(plan_state_path(self.sparring_dir, PLAN_B).exists())

    def test_adopt_does_not_let_one_plan_take_another_plans_accepted_stages(self):
        # --adopt means "these stages were executed independently and I want
        # this plan to adopt them". It has never meant "a directory with this
        # generated id exists, so reuse it", and it must not mean "another
        # managed run's accepted work is now mine".
        self.complete_plan_a()

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_B, adopt=True)
        message = str(ctx.exception)
        self.assertIn("belong to another managed plan run", message)
        self.assertIn(plan_key(PLAN_A), message)
        for stage_id in COLLIDING:
            self.assertIn(stage_id, message)
        # Refused before anything was recorded or rewritten.
        self.assertFalse(plan_state_path(self.sparring_dir, PLAN_B).exists())
        for stage_id in COLLIDING:
            self.assertEqual(self.state_of(stage_id).plan, plan_key(PLAN_A))
            self.assertIs(self.state_of(stage_id).status, StageStatus.ACCEPTED)

    def test_an_accidental_completed_no_op_run_does_not_block_the_next_plan(self):
        # The state this bug already left behind on a real branch: a second
        # plan-run file recorded as complete over the first plan's stages.
        # Nothing has to be edited out of `.sparring` for a genuinely fresh
        # plan to start from the same branch afterwards.
        self.complete_plan_a()
        accidental = plan_state_path(self.sparring_dir, PLAN_B)
        accidental.parent.mkdir(parents=True, exist_ok=True)
        PlanRunState(
            plan=PLAN_B,
            plan_digest="sha256:whatever",
            expected_branch="feature/x",
            current_stage_index=1,
            current_stage=COLLIDING[-1],
            status=PlanRunStatus.COMPLETE,
            source="manifest",
        ).save(accidental)

        third = "docs/plans/third.md"
        stage_ids = (f"{plan_key(third)}-stage-1-foundation",)
        result, _ = self.start(third, stage_ids=stage_ids, verdicts=[READY])

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.state_of(stage_ids[0]).plan, plan_key(third))
        # And the accidental record is still there, untouched, as history.
        self.assertIs(self.run_state(PLAN_B).status, PlanRunStatus.COMPLETE)


class DeliberateAdoptionTests(_TwoPlanRepoTestCase):
    """Adoption keeps working for what it is actually for."""

    def _hand_driven(self, stage_id: str, plan_label: str) -> Stage:
        """A stage created outside any managed run, as `new-stage` leaves it."""

        stage = self.stage(stage_id).create(brief=_brief(stage_id, plan_label))
        self.assertIsNone(stage.read_state().plan, "a hand-driven stage is owned by no run")
        return stage

    def test_an_unowned_hand_driven_sequence_is_still_adoptable(self):
        for stage_id in COLLIDING:
            self._hand_driven(stage_id, PLAN_A)

        reported: list[str] = []
        result, _ = self.start(PLAN_A, adopt=True, report=reported.append)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(len([line for line in reported if line.startswith("adopting ")]), 2)
        # Adoption is what recorded the ownership, so the sequence is now
        # this run's and a later plan cannot take it in turn.
        for stage_id in COLLIDING:
            self.assertEqual(self.state_of(stage_id).plan, plan_key(PLAN_A))

    def test_adopting_records_ownership_so_a_second_plan_cannot_adopt_the_same_stages(self):
        for stage_id in COLLIDING:
            self._hand_driven(stage_id, PLAN_A)
        self.start(PLAN_A, adopt=True)

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_B, adopt=True)
        self.assertIn("belong to another managed plan run", str(ctx.exception))

    def test_a_fresh_run_still_refuses_unowned_leftovers_without_adopt(self):
        # Unchanged: an unowned leftover is not silently inherited either.
        # That refusal is about fresh sessions, and it is what --adopt answers.
        self._hand_driven(COLLIDING[0], PLAN_A)

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_A)
        self.assertIn("--adopt", str(ctx.exception))


class OwnershipRecordTests(_TwoPlanRepoTestCase):
    """How ownership is written, and what a file without it means."""

    def test_a_managed_run_owns_every_stage_it_creates(self):
        self.start(PLAN_A)
        for stage_id in COLLIDING:
            payload = json.loads((self.stage(stage_id).directory / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["plan"], plan_key(PLAN_A))

    def test_a_state_json_written_before_ownership_reads_back_unowned(self):
        # Backward compatibility, both ways: every state.json on disk today
        # omits the field and must read back as unowned (so adoption still
        # works on it), and an unowned state must still serialize without it
        # (so a hand-driven stage's file is byte-identical to before).
        legacy = {
            "status": "accepted",
            "implementation_session_id": "impl-1",
            "sparring_session_id": "spar-1",
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
        }
        state = StageState.from_dict(legacy)
        self.assertIsNone(state.plan)
        self.assertNotIn("plan", state.to_dict())
        self.assertEqual(state.to_dict(), legacy)

    def test_a_non_string_plan_field_is_refused_rather_than_coerced(self):
        from agent_sparring.stage import StageError

        with self.assertRaises(StageError):
            StageState.from_dict({"status": "working", "plan": 7})


if __name__ == "__main__":
    unittest.main()
