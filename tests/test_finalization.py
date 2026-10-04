"""The human-gated stage that is committed only after the human passes.

The sequence these tests are built from is a real one, reported against a
managed plan run:

    the stage implementation intentionally left the worktree dirty (the
    project's own rule: a human-gated candidate stays uncommitted until
    manual verification passes);
    the sparrer returned NEEDS_YOU;
    the human performed every blocking check and submitted Pass;
    `resume-plan --evidence` correctly resumed the sparrer first;
    the sparrer returned READY -- with a summary that itself said "ready to
    return to the stage agent for commit and push";
    the engine went straight to freeze/accept, and freeze correctly refused,
    because the recorded SHA did not represent the dirty worktree.

The fix is not to weaken that refusal. It is that READY is not terminal
until the reviewed candidate is a commit: the loop routes one bounded
commit/push turn, proves the committed content is the content that was
reviewed, and has the sparrer review that exact SHA before the gate ever
runs. See :mod:`agent_sparring.finalization` and the "Finalization" section
of :mod:`agent_sparring.loop`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.finalization import (
    pending_finalization,
    read_commit_content,
    read_worktree_content,
)
from agent_sparring.loop import FinalizationRefused, LoopError, run_unattended_loop
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    plan_key,
    run_state_path,
    record_human_evidence,
    resume_plan,
    start_plan,
)
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.human_gate import HumanGate
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import FINALIZATION_HEADING, Stage, StageStatus

PLAN = """\
# Feature plan

## Stage 1 — Editor and guarded editing

Build the editor and leave it for a human to inspect.

## Stage 2 — Activation

Turn it on.
"""

KEY = plan_key("docs/plan.md")
S1 = f"{KEY}-stage-1-editor-and-guarded-editing"
S2 = f"{KEY}-stage-2-activation"

# The five blocking manual checks of the reported gate, in the structured
# shape the sparrer must produce alongside NEEDS_YOU.
GATE = {
    "category": "UI_VISUAL_CHECK",
    "title": "Five manual checks block this stage",
    "checks": [
        {
            "id": check_id,
            "instruction": f"Perform {check_id} by hand in the running app.",
            "pass_criteria": "It behaves as the plan describes.",
            "source": "docs/plan.md#stage-1",
        }
        for check_id in (
            "edit-swap-save-restart",
            "retraction-behaviour",
            "closed-gate-preservation",
            "library-manager-round-trip",
            "ui-accessibility-inspection",
        )
    ],
}


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _head(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD")


def _verdict(action: str, summary: str, *, gate: dict | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": "UI/VISUAL CHECK -- a human must look at it"
            if action == "NEEDS_YOU"
            else None,
            "findings": f"findings: {summary}",
            "deferred": None,
            "human_gate": gate if action == "NEEDS_YOU" else None,
        }
    )


NEEDS_YOU = _verdict("NEEDS_YOU", "five manual checks block this stage", gate=GATE)
READY_SUMMARY = (
    "Stage 1 is ready to return to the stage agent for commit and push; all "
    "blocking manual checks passed."
)
READY_ON_EVIDENCE = _verdict("READY", READY_SUMMARY)
READY_ON_COMMIT = _verdict("READY", "the committed candidate is the verified work")
READY = _verdict("READY", "looks good")

# The work the human verifies: application code, a test, and a translation,
# left uncommitted on purpose.
IMPLEMENTATION = {
    "ui/measurement_content_view.py": "class MeasurementContentView:\n    pass\n",
    "tests/test_measurement_content_view.py": "def test_view():\n    assert True\n",
    "i18n/app_nb_NO.ts": "<TS><context>reported statistics</context></TS>\n",
}


class _StageAdapter:
    """An implementation side that can be asked to behave the three ways
    that matter here: leave the reviewed work uncommitted, commit exactly
    it, or commit something else.

    ``mode`` on each turn is taken from ``script`` (1-indexed by call), so a
    test can say "turn 1 implements and leaves it dirty, turn 2 finalizes".
    """

    provider_id = "fake-stage"

    def __init__(self, repo: Path, script: list[str]):
        self.repo = repo
        self.script = list(script)
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self._calls = 0
        self._starts = 0

    def _turn(self, session_id: str, prompt: str) -> StageAgentResult:
        self._calls += 1
        self.prompts.append(prompt)
        mode = self.script[self._calls - 1] if self._calls <= len(self.script) else "noop"
        getattr(self, f"_mode_{mode}")()
        return StageAgentResult(session_id=session_id, text=f"turn {self._calls}", is_error=False)

    # -- modes ---------------------------------------------------------

    def _mode_implement_dirty(self) -> None:
        """Write the implementation and deliberately do not commit it."""

        for name, body in IMPLEMENTATION.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def _mode_commit(self) -> None:
        """Commit and push exactly what is in the worktree."""

        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "stage work")
        _run_git(self.repo, "push", "-q", "origin", "HEAD")

    def _mode_commit_and_alter(self) -> None:
        """"Improve" one reviewed file on the way past, then commit."""

        path = self.repo / "ui/measurement_content_view.py"
        path.write_text(
            "class MeasurementContentView:\n    '''tidied up by the commit turn'''\n",
            encoding="utf-8",
        )
        self._mode_commit()

    def _mode_commit_partially(self) -> None:
        """Commit one reviewed file and leave the rest in the worktree."""

        _run_git(self.repo, "add", "ui/measurement_content_view.py")
        _run_git(self.repo, "commit", "-q", "-m", "part of the stage work")
        _run_git(self.repo, "push", "-q", "origin", "HEAD")

    def _mode_noop(self) -> None:
        """A turn that reports success and changes nothing."""

    # -- adapter surface ----------------------------------------------

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        self._starts += 1
        return self._turn(f"impl-{self._starts}", prompt)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        return self._turn(session_id, prompt)


class _SparringAdapter:
    provider_id = "fake-sparrer"

    def __init__(self, verdicts: list[str]):
        self._verdicts = list(verdicts)
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self._starts = 0

    def _next(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.prompts.append(prompt)
        return SparringAgentResult(
            session_id=session_id, text=self._verdicts.pop(0), is_error=False
        )

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        self._starts += 1
        return self._next(f"spar-{self._starts}", prompt)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls.append((session_id, prompt))
        return self._next(session_id, prompt)


class _RepoCase(unittest.TestCase):
    """A repo with a real remote, a gitignored .sparring/, and a plan."""

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
            ".sparring/stages/\n.sparring/plans/\n", encoding="utf-8"
        )
        self.plan_path = self.repo / "docs" / "plan.md"
        self.plan_path.parent.mkdir()
        self.plan_path.write_text(PLAN, encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.run_key = plan_key("docs/plan.md")
        self.state_path = run_state_path(self.sparring_dir, self.run_key)

    def _adapters(self, stage_adapter, sparring_adapter):
        return lambda _stage: (stage_adapter, sparring_adapter)

    def _start(self, stage_adapter, sparring_adapter, **kwargs):
        return start_plan(
            self.plan_path,
            self.sparring_dir,
            self.repo,
            self._adapters(stage_adapter, sparring_adapter),
            expected_branch="feature/x",
            run_key=kwargs.pop("run_key", self.run_key),
            **kwargs,
        )

    def _resume(self, stage_adapter, sparring_adapter, **kwargs):
        return resume_plan(
            self.plan_path,
            self.sparring_dir,
            self.repo,
            self._adapters(stage_adapter, sparring_adapter),
            expected_branch="feature/x",
            **kwargs,
        )

    def _stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def _write_implementation(self) -> None:
        for name, body in IMPLEMENTATION.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")


class HumanGatedStageLifecycleTests(_RepoCase):
    """The reported sequence, end to end, through the managed plan run."""

    def test_the_verified_candidate_is_committed_then_accepted(self):
        base = _head(self.repo)
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit", "noop"])
        sparring_adapter = _SparringAdapter(
            [NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT, NEEDS_YOU]
        )

        # 1. Implementation leaves the worktree dirty on purpose; the
        #    sparrer asks for the five manual checks and the plan pauses.
        paused = self._start(stage_adapter, sparring_adapter)
        self.assertIs(paused.status, PlanRunStatus.PAUSED)
        self.assertEqual(paused.stage_id, S1)
        self.assertIs(paused.routing.action, RoutingAction.NEEDS_YOU)
        self.assertEqual(_head(self.repo), base)
        self.assertIsNot(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

        # 2. The human passes every check. Evidence goes to the sparrer,
        #    which agrees -- and says so in the words that exposed the bug.
        evidence = "\n".join(
            f"- {check['id']}: PASS" for check in GATE["checks"]
        )
        result = self._resume(stage_adapter, sparring_adapter, evidence=evidence)

        # 3. Acceptance happened, and on the commit that holds the verified
        #    work -- never on the pre-stage base the freeze was handed before.
        self.assertIs(result.status, PlanRunStatus.PAUSED)  # paused at stage 2, not failed
        self.assertEqual(dict(result.accepted).keys(), {S1})
        accepted_sha = dict(result.accepted)[S1]
        self.assertNotEqual(accepted_sha, base)
        self.assertEqual(accepted_sha, _head(self.repo))
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, accepted_sha)
        self.assertEqual(s1.base_sha, base)

        # 4. The accepted commit contains the work the human verified, and
        #    it is pushed -- the whole point of the invariant.
        committed = _run_git(self.repo, "show", "--name-only", "--format=", accepted_sha)
        for name in IMPLEMENTATION:
            self.assertIn(name, committed)
        self.assertEqual(
            _run_git(self.remote, "rev-parse", "refs/heads/feature/x"), accepted_sha
        )
        self.assertEqual(
            _run_git(self.repo, "status", "--porcelain", "--untracked-files=all"), ""
        )

        # 5. One commit/push turn, on the same implementation session, and
        #    the sparrer reviewed the committed candidate on its own session.
        #    (The second start call is stage 2's own fresh session.)
        self.assertEqual(len(stage_adapter.start_calls), 2)
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1"])
        self.assertEqual(s1.implementation_session_id, "impl-1")
        self.assertEqual(s1.sparring_session_id, "spar-1")
        self.assertEqual([sid for sid, _ in sparring_adapter.resume_calls], ["spar-1", "spar-1"])

        # 6. The human's evidence survived all of it.
        notes = self._stage(S1).read_notes()
        for check in GATE["checks"]:
            self.assertIn(f"- {check['id']}: PASS", notes)
        self.assertNotIn(FINALIZATION_HEADING, notes)

        # 7. The plan moved on to stage 2 and is waiting there, unrun.
        self.assertEqual(PlanRunState.load(self.state_path).current_stage, S2)

    def test_the_commit_turn_is_told_to_commit_and_not_to_implement(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit", "noop"])
        sparring_adapter = _SparringAdapter(
            [NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT, NEEDS_YOU]
        )
        self._start(stage_adapter, sparring_adapter)
        self._resume(stage_adapter, sparring_adapter, evidence="- every check: PASS")

        # Turn 1 implemented, turn 2 finalized, turn 3 belongs to stage 2.
        finalize_prompt = stage_adapter.prompts[1]
        self.assertIn("## Finalize this candidate", finalize_prompt)
        self.assertIn("Commit the stage's work on `feature/x`", finalize_prompt)
        self.assertIn("Report the exact committed SHA", finalize_prompt)
        self.assertIn("Do not re-implement", finalize_prompt)
        # The ordinary "act on the human's answer" instruction would invite
        # exactly the implementation work this turn must not do.
        self.assertIn("- every check: PASS", finalize_prompt)
        self.assertNotIn("If it calls for implementation changes, make them", finalize_prompt)
        self.assertNotIn("## Scope reminder", finalize_prompt)

        # And the sparrer reviewing the result is told what only the engine
        # can know: the commit is the reviewed tree, unchanged. Prompt 2 is
        # the review of the committed candidate; 0 and 1 precede the commit
        # and 3 belongs to stage 2.
        review_of_commit = sparring_adapter.prompts[2]
        self.assertIn("## Finalization", review_of_commit)
        self.assertIn(_head(self.repo), review_of_commit)
        self.assertIn("compared the committed content against that tree", review_of_commit)
        # No other turn is given one; there was nothing to say.
        for index in (0, 1, 3):
            self.assertNotIn("## Finalization", sparring_adapter.prompts[index])


class FinalizationRefusalTests(_RepoCase):
    """A commit turn may not change what the human verified."""

    def test_altering_the_verified_work_stops_the_run_without_accepting(self):
        base = _head(self.repo)
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit_and_alter"])
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT])
        self._start(stage_adapter, sparring_adapter)

        with self.assertRaises(PlanRunError) as ctx:
            self._resume(stage_adapter, sparring_adapter, evidence="- every check: PASS")

        message = str(ctx.exception)
        self.assertIn("ui/measurement_content_view.py", message)
        self.assertIn("reviewed and human-verified", message)

        # Nothing accepted, nothing advanced, and the sparrer was never
        # asked to bless the rewrite.
        s1 = self._stage(S1).read_state()
        self.assertIsNot(s1.status, StageStatus.ACCEPTED)
        self.assertIsNone(s1.candidate_sha)
        self.assertEqual(PlanRunState.load(self.state_path).current_stage, S1)
        self.assertEqual(len(sparring_adapter.prompts), 2)

        # The refusal outlives the terminal: notes.md keeps the account, and
        # says plainly that the recorded manual checks no longer cover it.
        notes = self._stage(S1).read_notes()
        self.assertIn(FINALIZATION_HEADING, notes)
        self.assertIn("ui/measurement_content_view.py", notes)
        self.assertIn("does not carry", notes)
        # The human's own evidence is untouched and still readable as its
        # own section, above the engine's note.
        self.assertIn("- every check: PASS", notes)
        self.assertLess(
            notes.index("- every check: PASS"), notes.index(FINALIZATION_HEADING)
        )
        # The commit itself is left exactly where the turn left it; nothing
        # is rolled back, only refused.
        self.assertNotEqual(_head(self.repo), base)

    def test_committing_only_part_of_the_verified_work_is_refused(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit_partially"])
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT])
        self._start(stage_adapter, sparring_adapter)

        with self.assertRaises(PlanRunError) as ctx:
            self._resume(stage_adapter, sparring_adapter, evidence="- every check: PASS")

        message = str(ctx.exception)
        self.assertIn("Still outside any commit", message)
        self.assertIn("tests/test_measurement_content_view.py", message)
        self.assertIsNot(self._stage(S1).read_state().status, StageStatus.ACCEPTED)

    def test_a_commit_turn_that_commits_nothing_is_refused_not_retried(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "noop"])
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT])
        self._start(stage_adapter, sparring_adapter)

        with self.assertRaises(PlanRunError):
            self._resume(stage_adapter, sparring_adapter, evidence="- every check: PASS")

        # Exactly one commit/push turn was routed, not a retry loop.
        self.assertEqual(len(stage_adapter.resume_calls), 1)
        self.assertIsNot(self._stage(S1).read_state().status, StageStatus.ACCEPTED)


class LoopFinalizationTests(_RepoCase):
    """The routing rule itself, at the loop's own boundary."""

    def setUp(self):
        super().setUp()
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        self.stage.write_brief("# Stage brief: stage-1\n\n## Goal\n\nDo the thing.\n")

    def _loop(self, stage_adapter, sparring_adapter, **kwargs):
        return run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            stage_adapter,
            sparring_adapter,
            expected_branch="feature/x",
            **kwargs,
        )

    def test_ready_over_a_clean_tree_is_terminal_with_no_extra_turn(self):
        stage_adapter = _StageAdapter(self.repo, ["noop"])
        sparring_adapter = _SparringAdapter([READY])

        result = self._loop(stage_adapter, sparring_adapter)

        self.assertIs(result.outcome, RoutingAction.READY)
        self.assertEqual(len(result.cycles), 1)
        self.assertFalse(result.cycles[0].finalization)
        self.assertEqual(stage_adapter.resume_calls, [])

    def test_ready_over_an_uncommitted_candidate_routes_one_commit_cycle(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit"])
        sparring_adapter = _SparringAdapter([READY, READY_ON_COMMIT])

        result = self._loop(stage_adapter, sparring_adapter)

        self.assertIs(result.outcome, RoutingAction.READY)
        self.assertEqual([cycle.finalization for cycle in result.cycles], [False, True])
        # The verdict returned is the one over the committed candidate.
        self.assertEqual(result.routing.summary, "the committed candidate is the verified work")
        self.assertEqual(
            _run_git(self.repo, "status", "--porcelain", "--untracked-files=all"), ""
        )

    def test_a_send_back_after_the_commit_cycle_still_returns_to_the_stage_agent(self):
        # The commit cycle is an ordinary cycle: if the sparrer finds real
        # work in the committed candidate, the loop continues as always.
        stage_adapter = _StageAdapter(self.repo, ["commit", "noop"])
        sparring_adapter = _SparringAdapter(
            [READY, _verdict("SEND_BACK", "the committed diff has a bug"), READY]
        )
        self._write_implementation()
        _run_git(self.repo, "add", "-A")

        result = self._loop(stage_adapter, sparring_adapter, start_with="sparring")

        self.assertIs(result.outcome, RoutingAction.READY)
        self.assertEqual(result.send_back_count, 1)
        self.assertEqual([cycle.finalization for cycle in result.cycles], [False, True, False])

    def test_still_uncommitted_after_the_commit_cycle_is_refused(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "noop"])
        sparring_adapter = _SparringAdapter([READY, READY])

        with self.assertRaises(FinalizationRefused) as ctx:
            self._loop(stage_adapter, sparring_adapter)

        self.assertIn("Still outside any commit", str(ctx.exception))
        self.assertIn("ui/measurement_content_view.py", str(ctx.exception))
        # One commit/push turn was routed, then it stopped.
        self.assertEqual(len(stage_adapter.resume_calls), 1)

    def test_a_sibling_stages_workflow_files_do_not_route_a_commit_cycle(self):
        # Another stage's bookkeeping is not this stage's implementation, so
        # it must not provoke an implementation turn -- even though the
        # freeze gate does (still) refuse a worktree holding it.
        other = Stage.resolve(self.sparring_dir, "stage-0").create()
        other.write_notes("# Notes: stage-0\n\nleft behind\n")
        (self.repo / ".gitignore").write_text("", encoding="utf-8")
        _run_git(self.repo, "add", ".gitignore")
        _run_git(self.repo, "commit", "-q", "-m", "track the sparring dir")

        stage_adapter = _StageAdapter(self.repo, ["noop"])
        result = self._loop(stage_adapter, _SparringAdapter([READY]))

        self.assertIs(result.outcome, RoutingAction.READY)
        self.assertEqual(len(result.cycles), 1)
        self.assertEqual(stage_adapter.resume_calls, [])


class StuckRunRecoveryTests(_RepoCase):
    """A run left at the broken point recovers through its own commands.

    Runs that predate the finalization cycle stopped exactly here: the
    sparrer's READY on disk, the human's evidence in notes.md, the verified
    work uncommitted, and the acceptance gate refusing a candidate that was
    never committed. Nobody should have to commit that by hand.
    """

    def _leave_stuck(self, summary: str = READY_SUMMARY, action: str = "READY") -> Stage:
        """Reproduce that on-disk state: stage 1 implemented but uncommitted,
        the human's evidence recorded, and the verdict already written."""

        stage_adapter = _StageAdapter(self.repo, ["implement_dirty"])
        self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        stage = self._stage(S1)
        record_human_evidence(stage, "- every check: PASS")
        routing = RoutingAction.from_str(action)
        record_sparring(
            stage,
            RoutingResult(
                action=routing,
                summary=summary,
                human_gate=HumanGate.from_dict(GATE) if routing is RoutingAction.NEEDS_YOU else None,
            ),
            findings="the reviewed tree is still the uncommitted implementation",
        )
        return stage

    def test_resuming_enters_the_commit_cycle_and_accepts_the_verified_work(self):
        stage = self._leave_stuck()
        base = _head(self.repo)
        stage_adapter = _StageAdapter(self.repo, ["commit", "noop"])
        sparring_adapter = _SparringAdapter([READY_ON_COMMIT, NEEDS_YOU])
        output: list[str] = []

        result = self._resume(stage_adapter, sparring_adapter, report=output.append)

        # The stage agent was asked for the bounded commit turn, not an
        # implementation turn on a tree a human already verified.
        self.assertEqual(len(stage_adapter.resume_calls), 1)
        self.assertIn("## Finalize this candidate", stage_adapter.prompts[0])
        self.assertIn("- every check: PASS", stage_adapter.prompts[0])
        self.assertTrue(
            any("reviewed candidate is not committed" in line for line in output), output
        )

        accepted = dict(result.accepted)[S1]
        self.assertNotEqual(accepted, base)
        self.assertEqual(accepted, _head(self.repo))
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(
            _run_git(self.remote, "rev-parse", "refs/heads/feature/x"), accepted
        )
        # Both recorded sessions were reused, not replaced.
        self.assertEqual(stage.read_state().implementation_session_id, "impl-1")
        self.assertEqual(stage.read_state().sparring_session_id, "spar-1")

    def test_a_recorded_needs_you_still_keeps_its_pause(self):
        # Finalization must not reach past a gate a human has not answered,
        # however dirty the worktree is.
        self._leave_stuck(summary="a human must look at it", action="NEEDS_YOU")
        stage_adapter = _StageAdapter(self.repo, ["commit"])

        result = self._resume(stage_adapter, _SparringAdapter([]))

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIs(result.recorded.action, RoutingAction.NEEDS_YOU)
        self.assertEqual(stage_adapter.prompts, [])
        self.assertEqual(_run_git(self.repo, "rev-parse", "HEAD"), _head(self.repo))

    def test_a_recorded_ready_over_a_committed_candidate_resumes_normally(self):
        # Nothing to finalize: the reviewed content was committed by hand,
        # exactly as reviewed. The next_turn marker recorded what READY was
        # given over, so the reviewer rules on that commit -- no
        # implementation turn on human-verified work -- and acceptance
        # follows. The only implementation turn is stage 2's own.
        stage = self._leave_stuck()
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "committed by hand meanwhile")
        _run_git(self.repo, "push", "-q", "origin", "HEAD")

        stage_adapter = _StageAdapter(self.repo, ["noop", "noop"])
        result = self._resume(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))

        self.assertEqual(len(stage_adapter.prompts), 1)
        self.assertIn(S2, stage_adapter.prompts[0])
        self.assertNotIn("## Finalize this candidate", stage_adapter.prompts[0])
        self.assertIn("## Scope reminder", stage_adapter.prompts[0])
        self.assertEqual(dict(result.accepted)[S1], _head(self.repo))
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)

    def test_entering_for_finalization_with_nothing_to_commit_is_refused(self):
        # The loop is asked for one specific turn on one specific premise;
        # if the premise does not hold it says so instead of running
        # something else, and no provider is invoked.
        stage = Stage.resolve(self.sparring_dir, "stage-clean").create()
        stage_adapter = _StageAdapter(self.repo, [])
        sparring_adapter = _SparringAdapter([])

        with self.assertRaises(LoopError) as ctx:
            run_unattended_loop(
                stage,
                self.sparring_dir,
                self.repo,
                stage_adapter,
                sparring_adapter,
                expected_branch="feature/x",
                start_with="finalization",
            )

        self.assertIn("no candidate content outside a commit", str(ctx.exception))
        self.assertEqual(stage_adapter.prompts, [])

    def test_an_unknown_start_with_is_still_refused(self):
        stage = Stage.resolve(self.sparring_dir, "stage-clean").create()
        with self.assertRaises(LoopError) as ctx:
            run_unattended_loop(
                stage,
                self.sparring_dir,
                self.repo,
                _StageAdapter(self.repo, []),
                _SparringAdapter([]),
                expected_branch="feature/x",
                start_with="whatever",
            )
        self.assertIn("'stage', 'sparring' or 'finalization'", str(ctx.exception))


class StopAfterStageTests(_RepoCase):
    """One stage at a time, without leaving the managed run."""

    def test_the_run_accepts_the_named_stage_and_enters_no_other(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit"])
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT])
        self._start(stage_adapter, sparring_adapter, stop_after_stage=S1)

        result = self._resume(
            stage_adapter,
            sparring_adapter,
            evidence="- every check: PASS",
            stop_after_stage=S1,
        )

        # Stage 1 went all the way through the gate...
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(dict(result.accepted).keys(), {S1})
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        # ...and stage 2 was neither run nor even created, so nothing is
        # briefed that a person did not ask for.
        self.assertEqual(result.stage_id, S2)
        self.assertFalse(Stage.resolve(self.sparring_dir, S2).exists())
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(sparring_adapter._verdicts, [])

        # The position advanced, so an ordinary resume continues normally.
        state = PlanRunState.load(self.state_path)
        self.assertEqual(state.current_stage, S2)
        self.assertIs(state.status, PlanRunStatus.PAUSED)

    def test_resuming_again_without_the_bound_runs_the_next_stage(self):
        stage_adapter = _StageAdapter(self.repo, ["implement_dirty", "commit"])
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY_ON_EVIDENCE, READY_ON_COMMIT])
        self._start(stage_adapter, sparring_adapter, stop_after_stage=S1)
        self._resume(
            stage_adapter, sparring_adapter, evidence="- every check: PASS", stop_after_stage=S1
        )

        next_stage = _StageAdapter(self.repo, ["noop"])
        result = self._resume(next_stage, _SparringAdapter([NEEDS_YOU]))

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, S2)
        self.assertTrue(Stage.resolve(self.sparring_dir, S2).exists())
        self.assertEqual(len(next_stage.start_calls), 1)

    def test_a_stage_id_that_is_not_in_the_plan_is_refused(self):
        # Silently ignoring it would give an unbounded run to a caller that
        # asked for a bounded one.
        with self.assertRaises(PlanError) as ctx:
            self._start(
                _StageAdapter(self.repo, []),
                _SparringAdapter([]),
                stop_after_stage="stage-does-not-exist",
            )
        self.assertIn("is not a stage of this plan", str(ctx.exception))


class CandidateContentTests(_RepoCase):
    """The comparison the refusal rests on."""

    def setUp(self):
        super().setUp()
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_committing_the_worktree_verbatim_reads_as_the_same_content(self):
        self._write_implementation()
        reviewed = read_worktree_content(self.repo, self.stage)

        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "as reviewed")

        committed = read_commit_content(self.repo, self.stage, "HEAD")
        self.assertEqual(committed.digest, reviewed.digest)
        self.assertEqual(committed.diverged_from(reviewed), ())
        self.assertIn("ui/measurement_content_view.py", committed.paths)

    def test_a_one_character_change_names_exactly_that_path(self):
        self._write_implementation()
        reviewed = read_worktree_content(self.repo, self.stage)

        (self.repo / "ui/measurement_content_view.py").write_text(
            IMPLEMENTATION["ui/measurement_content_view.py"] + "\n", encoding="utf-8"
        )
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "tidied")

        committed = read_commit_content(self.repo, self.stage, "HEAD")
        self.assertEqual(
            committed.diverged_from(reviewed), ("ui/measurement_content_view.py",)
        )
        self.assertNotEqual(committed.digest, reviewed.digest)

    def test_an_executable_bit_is_content_too(self):
        self._write_implementation()
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "as reviewed")
        reviewed = read_commit_content(self.repo, self.stage, "HEAD")

        _run_git(self.repo, "update-index", "--chmod=+x", "ui/measurement_content_view.py")
        _run_git(self.repo, "commit", "-q", "-m", "chmod")

        committed = read_commit_content(self.repo, self.stage, "HEAD")
        self.assertEqual(
            committed.diverged_from(reviewed), ("ui/measurement_content_view.py",)
        )

    def test_workflow_artifacts_are_not_candidate_content(self):
        # .sparring/ is gitignored in this repo anyway; the point is that
        # rewriting the stage's own files -- which every turn does -- is not
        # a divergence even when they are tracked.
        (self.repo / ".gitignore").write_text("", encoding="utf-8")
        self._write_implementation()
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "everything, artifacts included")
        reviewed = read_commit_content(self.repo, self.stage, "HEAD")

        self.stage.write_handoff("# Handoff\n\nregenerated by the commit turn\n")
        self.stage.write_notes("# Notes\n\nrewritten\n")
        self.stage.activity_path().write_text('{"v":1}\n', encoding="utf-8")

        self.assertEqual(read_worktree_content(self.repo, self.stage).diverged_from(reviewed), ())
        self.assertIsNone(pending_finalization(self.repo, self.stage))

    def test_a_file_that_only_looks_like_a_workflow_artifact_is_content(self):
        # The exemption is exact filenames at an exact depth, never a
        # subtree: nothing under .sparring/ becomes invisible by living there.
        (self.repo / ".gitignore").write_text("", encoding="utf-8")
        for name in (
            ".sparring/stages/stage-1/secrets.py",
            ".sparring/stages/stage-1/nested/state.json",
            ".sparring/project.toml",
        ):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("real content\n", encoding="utf-8")

        pending = pending_finalization(self.repo, self.stage)
        self.assertIsNotNone(pending)
        self.assertEqual(
            set(pending.reviewed.paths) & {
                ".sparring/stages/stage-1/secrets.py",
                ".sparring/stages/stage-1/nested/state.json",
                ".sparring/project.toml",
            },
            {
                ".sparring/stages/stage-1/secrets.py",
                ".sparring/stages/stage-1/nested/state.json",
                ".sparring/project.toml",
            },
        )

    def test_reading_the_worktree_leaves_the_index_and_tree_alone(self):
        self._write_implementation()
        before_status = _run_git(self.repo, "status", "--porcelain", "--untracked-files=all")
        before_head = _head(self.repo)

        read_worktree_content(self.repo, self.stage)

        self.assertEqual(
            _run_git(self.repo, "status", "--porcelain", "--untracked-files=all"), before_status
        )
        self.assertEqual(_head(self.repo), before_head)


if __name__ == "__main__":
    unittest.main()
