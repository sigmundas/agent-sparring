"""Stage instances belong to the managed run instance that made them.

The failure this file is written from, reproduced end to end: one plan was
run to completion on a feature branch, a *different* follow-up plan was then
started on the same branch and in the same worktree, and because both plans
generated the stage ids ``stage-1-...``/``stage-2-...`` the second plan found
the first plan's ACCEPTED stages, adopted them and completed as a no-op
without running anything.

The invariant these tests hold is that execution-stage identity is
``(run instance, stage)``: a stage recorded as owned by one managed run is
never the stage instance of another, whatever the two runs call their stages,
and no flag says otherwise. That is deliberately per *run*, not per plan
document, because a plan document is the input to a run and can be executed
more than once -- so a second run of the same document is new work too, with
stages of its own. Adoption stays what it is for -- a hand-driven, *unowned*
sequence deliberately taken into a managed run -- and earlier runs' history
stays on disk and intact throughout.
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
    find_runs,
    legacy_run_key,
    new_run_key,
    plan_key,
    run_state_path,
    start_plan,
)
from agent_sparring.stage import Stage, StageState, StageStatus
from test_plan import (  # noqa: E402  (shared scripted adapters and verdicts)
    NEEDS_YOU,
    READY,
    _SparringAdapter,
    _StageAdapter,
    _fixed,
    _run_git,
)

PLAN_A = "docs/plans/first.md"
PLAN_B = "docs/plans/follow-up.md"

# What the bug needed: two runs whose stage sections generate the very same
# ids. A follow-up plan numbered Stage 1..2 again is an ordinary workflow, so
# this is the collision the identity model has to survive rather than a
# malformed input.
COLLIDING = ("stage-1-foundation", "stage-2-transport")


def _brief(stage_id: str, plan_label: str) -> str:
    return f"# Stage brief: {stage_id}\n\nFrom plan `{plan_label}`.\n"


def namespaced(run_key: str) -> tuple[str, ...]:
    """The stage ids a managed run of two stages proposes for itself.

    The same convention the VS Code extension emits into its manifests and
    the engine's own Markdown parser uses: the owning run's key, then the
    stage. It is what keeps two runs' stage instances apart without anyone
    renumbering a plan.
    """

    return tuple(f"{run_key}-{stage_id}" for stage_id in COLLIDING)


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
    """One real repo, one branch, and several runs driven over it."""

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
        # runs commit successive files rather than re-writing the first
        # run's and finding nothing to commit.
        self.stage_adapter = _StageAdapter(self.repo, commit=True)

    # -- driving ---------------------------------------------------------

    def manifest_for(self, plan_label: str, stage_ids=COLLIDING) -> Path:
        path = self.manifests / f"{'-'.join(stage_ids)[:80]}.json"
        path.write_text(json.dumps(manifest_payload(plan_label, stage_ids), indent=2) + "\n", encoding="utf-8")
        return path

    def start(self, plan_label: str, *, run_key=None, stage_ids=COLLIDING, verdicts=None, adopt=False, report=None):
        """Start a run of ``plan_label``; returns its result and how many
        fresh implementation sessions this run started."""

        before = len(self.stage_adapter.start_calls)
        result = start_plan(
            load_manifest_source(self.manifest_for(plan_label, stage_ids)),
            self.sparring_dir,
            self.repo,
            _fixed(self.stage_adapter, _SparringAdapter(list(verdicts or [READY, READY]))),
            expected_branch="feature/x",
            run_key=run_key,
            adopt=adopt,
            report=report or (lambda message: None),
        )
        return result, len(self.stage_adapter.start_calls) - before

    # -- reading back ----------------------------------------------------

    def stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def state_of(self, stage_id: str) -> StageState:
        return self.stage(stage_id).read_state()

    def runs_of(self, plan_label: str):
        return find_runs(self.sparring_dir, plan_label)

    def sole_run(self, plan_label: str) -> PlanRunState:
        recorded = self.runs_of(plan_label)
        self.assertEqual(len(recorded), 1, f"expected one recorded run of {plan_label}")
        return recorded[0].state

    def complete_plan_a(self, run_key=None):
        """A run of plan A, to completion: both stages accepted and owned."""

        key = run_key or new_run_key(PLAN_A)
        result, _ = self.start(PLAN_A, run_key=key)
        self.assertIs(result.status, PlanRunStatus.COMPLETE, "the fixture itself must be sound")
        for stage_id in COLLIDING:
            self.assertIs(self.state_of(stage_id).status, StageStatus.ACCEPTED)
            self.assertEqual(self.state_of(stage_id).run, key)
        return key


class NewPlanAfterCompletedPlanTests(_TwoPlanRepoTestCase):
    """The reported failure: a follow-up plan on the same branch."""

    def test_a_follow_up_plan_with_its_own_stage_ids_runs_as_new_work(self):
        # The acceptance criterion. Same repository, same branch, same
        # worktree, Stage 1..2 numbered again -- and this is genuinely new
        # work, not an answer already given.
        self.complete_plan_a()
        key_b = new_run_key(PLAN_B)
        fresh = namespaced(key_b)

        reported: list[str] = []
        result, started = self.start(PLAN_B, run_key=key_b, stage_ids=fresh, report=reported.append)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], list(fresh))
        self.assertEqual(result.run, key_b, "the result says which run it is about")
        # Its stages really executed rather than being walked past.
        self.assertEqual(started, 2)
        self.assertFalse(
            [line for line in reported if "adopting" in line],
            "nothing was adopted: these are new stage instances",
        )
        # Fresh sessions of its own, and ownership recorded from creation.
        for stage_id in fresh:
            state = self.state_of(stage_id)
            self.assertEqual(state.run, key_b)
            self.assertIsNotNone(state.implementation_session_id)

    def test_plan_as_history_is_still_intact_and_still_plan_as(self):
        key_a = self.complete_plan_a()
        key_b = new_run_key(PLAN_B)
        fresh = namespaced(key_b)[:1]
        self.start(PLAN_B, run_key=key_b, stage_ids=fresh, verdicts=[READY])

        self.assertIs(self.sole_run(PLAN_A).status, PlanRunStatus.COMPLETE)
        for stage_id in COLLIDING:
            state = self.state_of(stage_id)
            self.assertIs(state.status, StageStatus.ACCEPTED)
            self.assertIsNotNone(state.candidate_sha)
            self.assertEqual(state.run, key_a)
            self.assertEqual(self.stage(stage_id).read_brief(), _brief(stage_id, PLAN_A))
        # Two managed runs, side by side, neither standing in for the other.
        self.assertEqual(self.sole_run(PLAN_B).plan, PLAN_B)
        self.assertEqual(self.sole_run(PLAN_B).run, key_b)

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
        self.assertEqual(self.runs_of(PLAN_B), ())

    def test_adopt_does_not_let_one_run_take_another_runs_accepted_stages(self):
        # --adopt means "these stages were executed independently and I want
        # this run to adopt them". It has never meant "a directory with this
        # generated id exists, so reuse it", and it must not mean "another
        # managed run's accepted work is now mine".
        key_a = self.complete_plan_a()

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_B, adopt=True)
        message = str(ctx.exception)
        self.assertIn("belong to another managed plan run", message)
        self.assertIn(key_a, message)
        for stage_id in COLLIDING:
            self.assertIn(stage_id, message)
        # Refused before anything was recorded or rewritten.
        self.assertEqual(self.runs_of(PLAN_B), ())
        for stage_id in COLLIDING:
            self.assertEqual(self.state_of(stage_id).run, key_a)
            self.assertIs(self.state_of(stage_id).status, StageStatus.ACCEPTED)

    def test_an_accidental_completed_no_op_run_does_not_block_the_next_plan(self):
        # The state the original bug already left behind on a real branch: a
        # second plan-run file recorded as complete over the first plan's
        # stages. Nothing has to be edited out of `.sparring` for a genuinely
        # fresh plan to start from the same branch afterwards.
        self.complete_plan_a()
        accidental = run_state_path(self.sparring_dir, legacy_run_key(PLAN_B))
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
        key = new_run_key(third)
        stage_ids = namespaced(key)[:1]
        result, _ = self.start(third, run_key=key, stage_ids=stage_ids, verdicts=[READY])

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.state_of(stage_ids[0]).run, key)
        # And the accidental record is still there, untouched, as history.
        self.assertIs(self.sole_run(PLAN_B).status, PlanRunStatus.COMPLETE)


class SamePlanTwiceTests(_TwoPlanRepoTestCase):
    """The same plan document, executed twice. Both are real runs."""

    def test_running_the_same_document_again_is_a_second_run_not_a_refusal(self):
        # Same repository, same worktree, same branch, the same exact plan
        # file with the same Stage 1..2 headings, and the first run complete.
        # This is the case that used to be refused outright, because the plan
        # document's key *was* the run's identity.
        key_a = self.complete_plan_a()
        key_b = new_run_key(PLAN_A)
        self.assertNotEqual(key_a, key_b)
        fresh = namespaced(key_b)

        result, started = self.start(PLAN_A, run_key=key_b, stage_ids=fresh)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(result.run, key_b)
        # New Stage-agent sessions, not a walk past run A's acceptances.
        self.assertEqual(started, 2)
        for stage_id in fresh:
            self.assertEqual(self.state_of(stage_id).run, key_b)
            self.assertIsNotNone(self.state_of(stage_id).implementation_session_id)
        # Two runs of one document, each with its own state file.
        recorded = {run.key for run in self.runs_of(PLAN_A)}
        self.assertEqual(recorded, {key_a, key_b})

    def test_run_as_accepted_stages_are_untouched_by_the_second_run(self):
        key_a = self.complete_plan_a()
        key_b = new_run_key(PLAN_A)
        before = {stage_id: self.state_of(stage_id) for stage_id in COLLIDING}

        self.start(PLAN_A, run_key=key_b, stage_ids=namespaced(key_b))

        for stage_id, was in before.items():
            self.assertEqual(self.state_of(stage_id), was, "run A's history is not rewritten")
            self.assertEqual(was.run, key_a)

    def test_a_second_run_may_not_reuse_the_first_runs_stage_ids(self):
        # The residual the run-instance identity closes. Ownership is by run,
        # so even the *same document's* earlier accepted stages cannot be
        # inherited -- with or without --adopt.
        self.complete_plan_a()

        for adopt in (False, True):
            with self.subTest(adopt=adopt):
                with self.assertRaises(PlanError) as ctx:
                    self.start(PLAN_A, run_key=new_run_key(PLAN_A), adopt=adopt)
                self.assertIn("belong to another managed plan run", str(ctx.exception))

    def test_an_open_run_of_the_same_document_refuses_a_second_one(self):
        # Managed-run exclusivity, which is a different question from
        # identity: two *live* runs of one plan in one worktree would compete
        # for the same candidate. A complete run never refuses; a paused or
        # running one does, and says how to continue it.
        key_a = new_run_key(PLAN_A)
        paused, _ = self.start(PLAN_A, run_key=key_a, verdicts=[READY, NEEDS_YOU])
        self.assertIs(paused.status, PlanRunStatus.PAUSED)

        key_b = new_run_key(PLAN_A)
        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_A, run_key=key_b, stage_ids=namespaced(key_b))
        message = str(ctx.exception)
        self.assertIn("still open", message)
        self.assertIn(key_a, message)
        self.assertIn("resume-plan --run-key", message)
        self.assertEqual({run.key for run in self.runs_of(PLAN_A)}, {key_a})


class DeliberateAdoptionTests(_TwoPlanRepoTestCase):
    """Adoption keeps working for what it is actually for."""

    def _hand_driven(self, stage_id: str, plan_label: str) -> Stage:
        """A stage created outside any managed run, as `new-stage` leaves it."""

        stage = self.stage(stage_id).create(brief=_brief(stage_id, plan_label))
        self.assertIsNone(stage.read_state().run, "a hand-driven stage is owned by no run")
        return stage

    def test_an_unowned_hand_driven_sequence_is_still_adoptable(self):
        for stage_id in COLLIDING:
            self._hand_driven(stage_id, PLAN_A)

        reported: list[str] = []
        key = new_run_key(PLAN_A)
        result, _ = self.start(PLAN_A, run_key=key, adopt=True, report=reported.append)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(len([line for line in reported if line.startswith("adopting ")]), 2)
        # Adoption is what recorded the ownership, so the sequence is now
        # this run's and a later run cannot take it in turn.
        for stage_id in COLLIDING:
            self.assertEqual(self.state_of(stage_id).run, key)

    def test_adopting_records_ownership_so_a_second_run_cannot_adopt_the_same_stages(self):
        for stage_id in COLLIDING:
            self._hand_driven(stage_id, PLAN_A)
        self.start(PLAN_A, run_key=new_run_key(PLAN_A), adopt=True)

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
        key = new_run_key(PLAN_A)
        self.start(PLAN_A, run_key=key)
        for stage_id in COLLIDING:
            payload = json.loads((self.stage(stage_id).directory / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run"], key)

    def test_a_state_json_written_before_ownership_reads_back_unowned(self):
        # Backward compatibility, both ways: a state.json written before
        # ownership existed omits the field and must read back as unowned (so
        # adoption still works on it), and an unowned state must still
        # serialize without it (so a hand-driven stage's file is
        # byte-identical to before).
        legacy = {
            "status": "accepted",
            "implementation_session_id": "impl-1",
            "sparring_session_id": "spar-1",
            "base_sha": "a" * 40,
            "candidate_sha": "b" * 40,
        }
        state = StageState.from_dict(legacy)
        self.assertIsNone(state.run)
        self.assertNotIn("run", state.to_dict())
        self.assertEqual(state.to_dict(), legacy)

    def test_the_older_plan_spelling_is_read_as_that_plans_legacy_run(self):
        # A stage owned by the first shape of this field, which recorded a
        # *plan* key. That named the plan document's only execution, so it is
        # read as that document's legacy run instance -- and a different run
        # of the same document therefore cannot claim the stage.
        owned = {"status": "accepted", "plan": plan_key(PLAN_A)}
        state = StageState.from_dict(owned)
        self.assertEqual(state.run, legacy_run_key(PLAN_A))

    def test_a_non_string_owner_field_is_refused_rather_than_coerced(self):
        from agent_sparring.stage import StageError

        with self.assertRaises(StageError):
            StageState.from_dict({"status": "working", "run": 7})


class LegacyRunTests(_TwoPlanRepoTestCase):
    """A run recorded before run instances existed stays a run."""

    def test_a_legacy_state_file_is_found_as_that_plans_run(self):
        # `plans/<plan key>.json` with no `run` field: exactly what every
        # recorded run on disk looks like today. It must be discoverable as a
        # run of its plan, and carry the key its stages already record.
        path = run_state_path(self.sparring_dir, legacy_run_key(PLAN_A))
        path.parent.mkdir(parents=True, exist_ok=True)
        PlanRunState(
            plan=PLAN_A,
            plan_digest="sha256:whatever",
            expected_branch="feature/x",
            current_stage_index=0,
            current_stage=COLLIDING[0],
            status=PlanRunStatus.PAUSED,
            source="manifest",
        ).save(path)

        recorded = self.runs_of(PLAN_A)
        self.assertEqual([run.key for run in recorded], [legacy_run_key(PLAN_A)])
        self.assertTrue(recorded[0].open)
        # And it is still the file it was: no `run` key was invented for it.
        self.assertNotIn("run", json.loads(path.read_text(encoding="utf-8")))

    def test_a_legacy_run_owns_the_stages_that_record_its_plan_key(self):
        # The migration case in one assertion: a stage carrying the older
        # `plan` spelling belongs to the legacy run, so a fresh run of the
        # same document is refused rather than inheriting it.
        self.stage(COLLIDING[0]).create(brief=_brief(COLLIDING[0], PLAN_A))
        state = self.state_of(COLLIDING[0])
        state.run = legacy_run_key(PLAN_A)
        self.stage(COLLIDING[0]).write_state(state)

        with self.assertRaises(PlanError) as ctx:
            self.start(PLAN_A, run_key=new_run_key(PLAN_A), adopt=True)
        self.assertIn(legacy_run_key(PLAN_A), str(ctx.exception))
