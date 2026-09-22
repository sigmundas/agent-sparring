"""Review-only stages: explicit mode, one reviewer, no manufactured commit.

The behaviour these tests hold is a set of refusals as much as a lifecycle.
A stage is a review only because something said so explicitly; a reviewer
is fresh and is never an implementation session; a defect it finds does not
quietly become implementation work; and completing the stage over an already
accepted candidate does not weaken -- or reach -- the freeze an
implementation stage still goes through.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.acceptance import (
    AcceptanceError,
    StaleCandidateError,
    accept_reviewed_candidate,
)
from agent_sparring.manifest import ManifestError, manifest_digest, parse_manifest
from agent_sparring.plan import (
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    plan_key,
    run_state_path,
    resume_plan,
    start_plan,
)
from agent_sparring.prompt_capture import prompts_dir
from agent_sparring.review import ReviewError, describe_candidate_set, enter_review
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import (
    CandidateRepository,
    Stage,
    StageMode,
    StageState,
    StageStatus,
)
from test_manifest import PLAN_LABEL, _stage_entry, manifest_payload
from test_plan import (  # noqa: E402  (shared scripted adapters and verdicts)
    NEEDS_YOU,
    READY,
    SEND_BACK,
    _fixed,
    _run_git,
    _SparringAdapter,
    _StageAdapter,
    _head_sha,
)

REVIEW_STAGE = "stage-5-independent-final-review"
BUILD_STAGE = "stage-4-editor-and-guarded-editing"


def _review_manifest(*, mode: str | None = "independent_review") -> dict:
    """A two-stage plan: one ordinary stage, then the review of it."""

    review = _stage_entry(
        REVIEW_STAGE,
        "Stage 5",
        "Independent final review",
        "A fresh reviewer verifies the frozen candidate and every gate.",
    )
    if mode is not None:
        review["mode"] = mode
    return manifest_payload(
        [
            _stage_entry(BUILD_STAGE, "Stage 4", "Editor and guarded editing", "Wire it up."),
            review,
        ]
    )


# -- the declaration itself --------------------------------------------------


class StageModeDeclarationTests(unittest.TestCase):
    def test_mode_defaults_to_implementation_and_stays_out_of_state_json(self):
        # Every state.json written before modes existed must read back as the
        # mode it in fact ran under, and a fresh implementation stage's file
        # must stay byte-identical to what it always was.
        self.assertIs(StageState().mode, StageMode.IMPLEMENTATION)
        self.assertNotIn("mode", StageState().to_dict())
        self.assertIs(StageState.from_dict({"status": "working"}).mode, StageMode.IMPLEMENTATION)

    def test_review_mode_round_trips_through_state_json(self):
        state = StageState(mode=StageMode.INDEPENDENT_REVIEW)
        self.assertEqual(state.to_dict()["mode"], "independent_review")
        self.assertIs(StageState.from_dict(state.to_dict()).mode, StageMode.INDEPENDENT_REVIEW)

    def test_unknown_mode_is_refused_rather_than_defaulted(self):
        # Defaulting would silently run the implementation lifecycle for a
        # stage whose recorded mode asked for something else.
        for bad in ({"mode": "review"}, {"mode": 5}):
            with self.assertRaises(Exception, msg=bad):
                StageState.from_dict({"status": "working", **bad})

    def test_manifest_mode_is_optional_validated_and_never_inferred(self):
        default = parse_manifest(json.dumps(_review_manifest(mode=None)))
        self.assertEqual(
            [stage.mode for stage in default.stages],
            [StageMode.IMPLEMENTATION, StageMode.IMPLEMENTATION],
        )
        # ...even though the stage is titled "Independent final review":
        # nothing derives the mode from a title or from a brief's prose.
        self.assertIn("Independent final review", default.stages[1].title)
        self.assertFalse(default.stages[1].review_only)

        declared = parse_manifest(json.dumps(_review_manifest()))
        self.assertTrue(declared.stages[1].review_only)
        self.assertFalse(declared.stages[0].review_only)

        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(json.dumps(_review_manifest(mode="review-only")))
        self.assertIn("mode", str(ctx.exception))

    def test_digest_is_unchanged_by_the_default_mode_and_changed_by_a_declared_one(self):
        # A manifest written before modes existed executes the same thing and
        # must keep resuming its recorded run...
        implicit = manifest_digest(parse_manifest(json.dumps(_review_manifest(mode=None))))
        explicit = manifest_digest(
            parse_manifest(json.dumps(_review_manifest(mode="implementation")))
        )
        self.assertEqual(implicit, explicit)
        # ...and declaring a review changes what runs, in both directions.
        review = manifest_digest(parse_manifest(json.dumps(_review_manifest())))
        self.assertNotEqual(review, implicit)


# -- the lifecycle -----------------------------------------------------------


class _ReviewRepoTestCase(unittest.TestCase):
    """A repo with a real bare remote whose Stage 4 is already accepted, so
    Stage 5 has an accepted candidate set to review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )
        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text(
            ".sparring/stages/\n.sparring/plans/\n__pycache__/\n", encoding="utf-8"
        )
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.manifest_path = root / "manifest.json"
        self.write_manifest()
        self.run_key = plan_key(PLAN_LABEL)
        self.state_path = run_state_path(self.sparring_dir, self.run_key)

    def write_manifest(self, *, mode: str | None = "independent_review") -> None:
        self.manifest_path.write_text(
            json.dumps(_review_manifest(mode=mode), indent=2) + "\n", encoding="utf-8"
        )

    def source(self):
        from agent_sparring.manifest import load_manifest_source

        return load_manifest_source(self.manifest_path)

    def stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def plan_state(self) -> PlanRunState:
        return PlanRunState.load(self.state_path)

    def start(self, stage_adapter, sparring_adapter, **kwargs):
        return start_plan(
            self.source(),
            self.sparring_dir,
            self.repo,
            _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x",
            run_key=kwargs.pop("run_key", self.run_key),
            **kwargs,
        )

    def resume(self, stage_adapter, sparring_adapter, **kwargs):
        return resume_plan(
            self.source(),
            self.sparring_dir,
            self.repo,
            _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x",
            run_key=kwargs.pop("run_key", self.run_key),
            **kwargs,
        )


class IndependentReviewRunTests(_ReviewRepoTestCase):
    def test_review_stage_runs_a_reviewer_and_no_implementation_agent(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY, READY])

        result = self.start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        # Stage 4 implemented and was sparred; Stage 5 only reviewed.
        self.assertEqual(len(stage_adapter.start_calls), 1, "one implementation turn in total")
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual(len(sparring_adapter.start_calls), 2, "a fresh reviewer for Stage 5")

        review = self.stage(REVIEW_STAGE).read_state()
        build = self.stage(BUILD_STAGE).read_state()
        self.assertIs(review.mode, StageMode.INDEPENDENT_REVIEW)
        self.assertIs(build.mode, StageMode.IMPLEMENTATION)
        self.assertIsNone(review.implementation_session_id, "no implementation session exists")
        self.assertEqual(review.sparring_session_id, "spar-2", "a session of its own, not Stage 4's")
        self.assertNotEqual(review.sparring_session_id, build.sparring_session_id)

    def test_completion_keeps_the_reviewed_candidate_and_adds_no_commit(self):
        sparring_adapter = _SparringAdapter([READY, READY])
        self.start(_StageAdapter(self.repo, commit=True), sparring_adapter)

        build = self.stage(BUILD_STAGE).read_state()
        review = self.stage(REVIEW_STAGE).read_state()
        self.assertIs(review.status, StageStatus.ACCEPTED)
        # The stage's candidate *is* the candidate it reviewed: no new commit
        # was manufactured to satisfy the freeze machinery, and HEAD did not
        # move for it.
        self.assertEqual(review.candidate_sha, build.candidate_sha)
        self.assertEqual(review.base_sha, build.candidate_sha)
        self.assertEqual(_head_sha(self.repo), build.candidate_sha)

    def test_the_reviewer_prompt_is_a_review_prompt_with_the_accepted_shas(self):
        sparring_adapter = _SparringAdapter([READY, READY])
        self.start(_StageAdapter(self.repo, commit=True), sparring_adapter)

        prompt = sparring_adapter.start_calls[1]
        accepted = self.stage(BUILD_STAGE).read_state().candidate_sha
        self.assertIn("# Independent review:", prompt)
        self.assertIn("## Accepted candidate set", prompt)
        self.assertIn(accepted, prompt)
        self.assertIn("Stage 4 — Editor and guarded editing", prompt)
        self.assertIn("you are not\nthe agent that changes it", prompt)
        self.assertIn("no implementation agent has run for it and none will", prompt)
        # There is no handoff for a stage no implementation agent ran, so the
        # sparring prompt's handoff section must not appear at all.
        self.assertNotIn("## Handoff", prompt)
        self.assertNotIn("You are sparring on this stage's candidate", prompt)

    def test_the_capture_records_the_reviewer_role_and_its_turn_kind(self):
        sparring_adapter = _SparringAdapter([READY, READY])
        self.start(_StageAdapter(self.repo, commit=True), sparring_adapter)

        index = (prompts_dir(self.stage(REVIEW_STAGE).directory) / "index.jsonl").read_text(
            encoding="utf-8"
        )
        entries = [json.loads(line) for line in index.splitlines() if line.strip()]
        self.assertEqual([e["role"] for e in entries], ["reviewer"])
        self.assertEqual(entries[0]["turn_kind"], "original")
        self.assertFalse(entries[0]["resumed"])
        self.assertIn(
            "0001-reviewer-original.md",
            [p.name for p in prompts_dir(self.stage(REVIEW_STAGE).directory).iterdir()],
        )
        # The section table says which words came from the engine, which is
        # the point of recording origins: a stage brief asking for a review
        # inside an engine framing that asks for one too.
        headings = {section["heading"]: section["origin"] for section in entries[0]["sections"]}
        self.assertEqual(headings["Accepted candidate set"], "engine")
        self.assertEqual(headings["Stage brief"], "file")

    def test_a_defect_pauses_the_plan_without_starting_an_implementation_turn(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY, SEND_BACK])

        result = self.start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, REVIEW_STAGE)
        self.assertIs(result.routing.action, RoutingAction.SEND_BACK)
        # The one implementation turn that ran was Stage 4's. SEND_BACK here
        # is a defect report, not the start of a correction cycle.
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual(len(sparring_adapter.start_calls), 2)
        self.assertIs(self.stage(REVIEW_STAGE).read_state().status, StageStatus.WORKING)
        self.assertIs(self.plan_state().status, PlanRunStatus.PAUSED)

    def test_needs_you_is_answered_by_the_same_reviewer_and_then_completes(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY, NEEDS_YOU])
        paused = self.start(stage_adapter, sparring_adapter)
        self.assertIs(paused.routing.action, RoutingAction.NEEDS_YOU)
        self.assertIsNotNone(paused.routing.human_gate)

        reviewer_session = self.stage(REVIEW_STAGE).read_state().sparring_session_id
        answering = _SparringAdapter([READY])
        done = self.resume(
            stage_adapter,
            answering,
            evidence="Ran the pre-activation reader check on a real device; it passed.",
        )

        self.assertIs(done.status, PlanRunStatus.COMPLETE)
        # The same reviewer session judged the evidence -- not a fresh one,
        # and not an implementation turn sent to carry the message.
        self.assertEqual(answering.start_calls, [])
        self.assertEqual([sid for sid, _ in answering.resume_calls], [reviewer_session])
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertIn("Ran the pre-activation reader check", answering.resume_calls[0][1])
        self.assertIn("## Human evidence", answering.resume_calls[0][1])

        index = (prompts_dir(self.stage(REVIEW_STAGE).directory) / "index.jsonl").read_text(
            encoding="utf-8"
        )
        kinds = [json.loads(line)["turn_kind"] for line in index.splitlines() if line.strip()]
        self.assertEqual(kinds, ["original", "evidence_review"])

    def test_a_repository_that_moved_past_the_accepted_candidate_is_refused(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        self.start(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))
        accepted = self.stage(BUILD_STAGE).read_state().candidate_sha

        (self.repo / "later.txt").write_text("after the review began\n", encoding="utf-8")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        with self.assertRaises(PlanRunError) as ctx:
            self.resume(stage_adapter, _SparringAdapter([READY]), evidence="the check passed")
        message = str(ctx.exception)
        self.assertIn(accepted, message)
        self.assertIn("not at the accepted candidate", message)
        self.assertIs(self.stage(REVIEW_STAGE).read_state().status, StageStatus.WORKING)

    def test_an_unrepresented_working_tree_change_is_refused_before_the_reviewer_runs(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        self.start(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))

        (self.repo / "scratch.txt").write_text("not in any commit\n", encoding="utf-8")
        reviewer = _SparringAdapter([READY])
        with self.assertRaises(PlanRunError) as ctx:
            self.resume(stage_adapter, reviewer, evidence="the check passed")
        self.assertIn("scratch.txt", str(ctx.exception))
        self.assertEqual(reviewer.start_calls, [])
        self.assertEqual(reviewer.resume_calls, [], "the reviewer was never given control")


class ReviewSubjectTests(_ReviewRepoTestCase):
    def test_refuses_a_review_whose_predecessor_is_not_accepted(self):
        review = self.stage(REVIEW_STAGE).create(mode=StageMode.INDEPENDENT_REVIEW)
        build = self.stage(BUILD_STAGE).create()
        planned = self.source().stages()
        with self.assertRaises(ReviewError) as ctx:
            enter_review(
                review,
                self.repo,
                expected_branch="feature/x",
                preceding=((planned[0], build),),
            )
        self.assertIn("rather than accepted", str(ctx.exception))
        self.assertIsNone(review.read_state().base_sha, "nothing was pinned")

    def test_the_review_stages_own_sibling_declaration_is_part_of_what_it_reviews(self):
        # A plan whose review stage spans two repositories while its
        # implementation stages each touched one: the review stage's own
        # declaration is the only place that says so, and narrowing the
        # reviewed set to the earlier stages' declarations would drop it.
        sibling = self.repo.parent / "sibling"
        sibling.mkdir()
        _run_git(sibling, "init", "-q", "-b", "feature/web")
        _run_git(sibling, "config", "user.email", "test@example.com")
        _run_git(sibling, "config", "user.name", "Test")
        (sibling / "web.txt").write_text("coupled change\n", encoding="utf-8")
        _run_git(sibling, "add", ".")
        _run_git(sibling, "commit", "-q", "-m", "web candidate")
        bare = self.repo.parent / "sibling-remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
        )
        _run_git(sibling, "remote", "add", "origin", str(bare))
        _run_git(sibling, "push", "-q", "-u", "origin", "feature/web")
        sibling_head = _head_sha(sibling)

        head = _head_sha(self.repo)
        build = self.stage(BUILD_STAGE).create()
        build.write_state(
            StageState(status=StageStatus.ACCEPTED, base_sha=head, candidate_sha=head)
        )
        review = self.stage(REVIEW_STAGE).create(mode=StageMode.INDEPENDENT_REVIEW)
        review.write_state(
            StageState(
                mode=StageMode.INDEPENDENT_REVIEW,
                repositories=(
                    CandidateRepository(name="web", path="../sibling", branch="feature/web"),
                ),
            )
        )
        planned = self.source().stages()
        subject = enter_review(
            review, self.repo, expected_branch="feature/x", preceding=((planned[0], build),)
        )

        self.assertEqual([r.name for r in subject.repositories], ["web"])
        self.assertEqual(subject.repositories[0].candidate_sha, sibling_head)
        self.assertEqual(review.read_state().repositories[0].candidate_sha, sibling_head)
        described = describe_candidate_set(review, subject)
        self.assertIn(sibling_head, described)
        self.assertIn("feature/web", described)

    def test_refuses_a_review_with_nothing_before_it(self):
        review = self.stage(REVIEW_STAGE).create(mode=StageMode.INDEPENDENT_REVIEW)
        with self.assertRaises(ReviewError) as ctx:
            enter_review(review, self.repo, expected_branch="feature/x", preceding=())
        self.assertIn("nothing accepted for it to review", str(ctx.exception))


class ReviewCompletionTests(_ReviewRepoTestCase):
    def _entered_review(self) -> tuple[Stage, str]:
        """A review stage entered against an accepted Stage 4."""

        (self.repo / "impl.txt").write_text("work\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "stage 4")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        head = _head_sha(self.repo)

        build = self.stage(BUILD_STAGE).create()
        build.write_state(
            StageState(status=StageStatus.ACCEPTED, base_sha=head, candidate_sha=head)
        )
        review = self.stage(REVIEW_STAGE).create(mode=StageMode.INDEPENDENT_REVIEW)
        planned = self.source().stages()
        enter_review(
            review, self.repo, expected_branch="feature/x", preceding=((planned[0], build),)
        )
        return review, head

    def test_accepts_the_reviewed_commit_without_a_freeze_or_a_new_commit(self):
        review, head = self._entered_review()
        result = accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        self.assertEqual(result.candidate_sha, head)
        state = review.read_state()
        self.assertIs(state.status, StageStatus.ACCEPTED)
        self.assertEqual(state.candidate_sha, head)
        self.assertEqual(_head_sha(self.repo), head, "no commit was created")

    def test_refuses_to_complete_an_implementation_stage_through_the_review_door(self):
        # The review completion rule must not be a second, softer way past
        # the freeze an implementation stage goes through.
        stage = self.stage(BUILD_STAGE).create()
        stage.write_state(StageState(base_sha=_head_sha(self.repo)))
        with self.assertRaises(AcceptanceError) as ctx:
            accept_reviewed_candidate(stage, self.repo, expected_branch="feature/x")
        self.assertIn("not as an independent review", str(ctx.exception))
        self.assertIs(stage.read_state().status, StageStatus.WORKING)

    def test_refuses_a_review_stage_that_was_never_entered(self):
        review = self.stage(REVIEW_STAGE).create(mode=StageMode.INDEPENDENT_REVIEW)
        with self.assertRaises(AcceptanceError) as ctx:
            accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        self.assertIn("never entered through the review lifecycle", str(ctx.exception))

    def test_refuses_when_head_moved_off_the_reviewed_commit(self):
        review, head = self._entered_review()
        (self.repo / "after.txt").write_text("after\n", encoding="utf-8")
        _run_git(self.repo, "add", "after.txt")
        _run_git(self.repo, "commit", "-q", "-m", "after the review")
        with self.assertRaises(StaleCandidateError) as ctx:
            accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        self.assertIn(head, str(ctx.exception))
        state = review.read_state()
        self.assertIs(state.status, StageStatus.WORKING)
        self.assertEqual(state.base_sha, head, "the reviewed commit is left recorded as it was")

    def test_refuses_when_the_worktree_is_not_the_reviewed_commit(self):
        review, _head = self._entered_review()
        (self.repo / "impl.txt").write_text("edited after the review\n", encoding="utf-8")
        with self.assertRaises(AcceptanceError) as ctx:
            accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        self.assertIn("impl.txt", str(ctx.exception))
        self.assertIs(review.read_state().status, StageStatus.WORKING)

    def test_refuses_to_re_accept(self):
        review, _head = self._entered_review()
        accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        with self.assertRaises(AcceptanceError) as ctx:
            accept_reviewed_candidate(review, self.repo, expected_branch="feature/x")
        self.assertIn("already ACCEPTED", str(ctx.exception))


class WrongModeRefusalTests(_ReviewRepoTestCase):
    def test_a_stage_that_ran_under_the_other_mode_stops_the_run_and_names_the_recovery(self):
        # Start the run against a manifest that does not declare the mode, so
        # Stage 5 is entered through the implementation lifecycle...
        self.write_manifest(mode=None)
        stage_adapter = _StageAdapter(self.repo, commit=True)
        self.start(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))
        self.assertIs(self.stage(REVIEW_STAGE).read_state().mode, StageMode.IMPLEMENTATION)
        self.assertIsNotNone(self.stage(REVIEW_STAGE).read_state().implementation_session_id)

        # ...then declare it. The run must stop rather than adopt that
        # attempt's session into a review meant to be independent of it.
        self.write_manifest()
        state = self.plan_state()
        state.plan_digest = self.source().digest()
        state.save(self.state_path)

        reviewer = _SparringAdapter([READY])
        with self.assertRaises(PlanRunError) as ctx:
            self.resume(stage_adapter, reviewer)
        message = str(ctx.exception)
        self.assertIn("already ran as implementation", message)
        self.assertIn("sparring reset-stage", message)
        self.assertIn("--mode independent_review", message)
        self.assertEqual(reviewer.start_calls, [])
        self.assertEqual(reviewer.resume_calls, [])

    def test_an_empty_stage_is_simply_relabelled(self):
        # Nothing ran: no session, no candidate, no base. There is no history
        # to preserve, so recording the declared mode is only labelling.
        review = self.stage(REVIEW_STAGE).create()
        self.assertIs(review.read_state().mode, StageMode.IMPLEMENTATION)
        from agent_sparring.review import declare_stage_mode

        declare_stage_mode(review, self.source().stages()[1])
        self.assertIs(review.read_state().mode, StageMode.INDEPENDENT_REVIEW)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
