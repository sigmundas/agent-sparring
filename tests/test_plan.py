import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.acceptance import StaleCandidateError
from agent_sparring.cli import main
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    parse_plan,
    plan_digest,
    plan_state_path,
    record_human_evidence,
    resume_plan,
    start_plan,
)
from agent_sparring.providers import ProviderError, SparringAgentResult, StageAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import Stage, StageStatus
from agent_sparring.stage_prompt import build_stage_prompt


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _verdict(action: str, summary: str, *, reason: str | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "summary": summary,
            "needs_you_reason": reason,
            "findings": f"findings: {summary}",
            "deferred": None,
        }
    )


READY = _verdict("READY", "looks good")
SEND_BACK = _verdict("SEND_BACK", "fix the guard")
NEEDS_YOU = _verdict("NEEDS_YOU", "check it on a device", reason="DEVICE/MANUAL CHECK -- Android")
ESCALATE = _verdict("ESCALATE", "needs a stronger sparrer")

PLAN = """\
# Feature plan

Some reviewed preamble that is not a stage.

## Stage 1 — Foundation

Lay the groundwork.

### Details

- add the module

## Stage 2 — Incremental rendering

Render incrementally.

## Notes

Trailing prose that belongs to no stage.
"""

THREE_STAGE_PLAN = PLAN.replace(
    "## Notes", "## Stage 3 — Prefetch and enrichment\n\nPrefetch things.\n\n## Notes"
)


class _StageAdapter:
    """Issues a distinct session id per fresh start (``impl-1``, ``impl-2``,
    ...) and echoes the resumed id back, so tests can tell fresh sessions
    from resumed ones. Can commit (and optionally push) a file on each turn,
    and can fail on a given 1-indexed call."""

    def __init__(self, repo: Path, *, commit: bool = False, push: bool = True, fail_at: int | None = None):
        self.repo = repo
        self.commit = commit
        self.push = push
        self.fail_at = fail_at
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self._calls = 0
        self._starts = 0

    def _turn(self, session_id: str) -> StageAgentResult:
        self._calls += 1
        if self.fail_at == self._calls:
            raise ProviderError("stage provider boom")
        if self.commit:
            path = self.repo / f"impl-{self._calls}.txt"
            path.write_text("implementation\n", encoding="utf-8")
            _run_git(self.repo, "add", path.name)
            _run_git(self.repo, "commit", "-q", "-m", f"turn {self._calls}")
            if self.push:
                _run_git(self.repo, "push", "-q", "origin", "feature/x")
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        self._starts += 1
        return self._turn(f"impl-{self._starts}")

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        return self._turn(session_id)


class _SparringAdapter:
    """Yields scripted verdicts in call order; a distinct session id per
    fresh start (``spar-1``, ``spar-2``, ...), resumed ids echoed back."""

    def __init__(self, verdicts: list[str]):
        self._verdicts = list(verdicts)
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self._starts = 0

    def _next(self, session_id: str) -> SparringAgentResult:
        text = self._verdicts.pop(0)
        return SparringAgentResult(session_id=session_id, text=text, is_error=False)

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        self._starts += 1
        return self._next(f"spar-{self._starts}")

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls.append((session_id, prompt))
        return self._next(session_id)


class PlanParsingTests(unittest.TestCase):
    def test_parses_stages_with_deterministic_ids_and_verbatim_sections(self):
        stages = parse_plan(PLAN)
        self.assertEqual([s.stage_id for s in stages], ["stage-1-foundation", "stage-2-incremental-rendering"])
        self.assertEqual(stages[0].number, 1)
        self.assertEqual(stages[0].title, "Foundation")
        self.assertTrue(stages[0].section.startswith("## Stage 1 — Foundation\n"))
        self.assertIn("### Details\n\n- add the module", stages[0].section)
        self.assertNotIn("Incremental", stages[0].section)
        self.assertNotIn("Trailing prose", stages[1].section)
        self.assertEqual(parse_plan(PLAN), stages)

    def test_accepts_hyphen_en_dash_and_colon_separators(self):
        for sep in ("-", "–", ":", "—"):
            stages = parse_plan(f"## Stage 1 {sep} Foo bar\n\nbody\n")
            self.assertEqual(stages[0].stage_id, "stage-1-foo-bar")

    def test_stage_headings_inside_code_fences_are_ignored(self):
        text = PLAN.replace(
            "Lay the groundwork.", "Lay the groundwork.\n\n```md\n## Stage 9 — not real\n```"
        )
        stages = parse_plan(text)
        self.assertEqual(len(stages), 2)
        self.assertIn("## Stage 9 — not real", stages[0].section)

    def test_refuses_plan_without_stages(self):
        with self.assertRaises(PlanError):
            parse_plan("# Plan\n\nJust prose.\n")

    def test_refuses_malformed_stage_heading(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan("## Stage one — Foo\n\nbody\n")
        self.assertIn("convention", str(ctx.exception))
        with self.assertRaises(PlanError):
            parse_plan("## Stage 1\n\nbody\n")

    def test_refuses_duplicate_gap_and_out_of_order_numbering(self):
        for bad in (
            "## Stage 1 — A\n\na\n\n## Stage 1 — B\n\nb\n",
            "## Stage 1 — A\n\na\n\n## Stage 3 — B\n\nb\n",
            "## Stage 2 — A\n\na\n\n## Stage 1 — B\n\nb\n",
            "## Stage 0 — A\n\na\n",
        ):
            with self.assertRaises(PlanError, msg=bad):
                parse_plan(bad)

    def test_refuses_empty_stage_section(self):
        with self.assertRaises(PlanError) as ctx:
            parse_plan("## Stage 1 — A\n\n## Stage 2 — B\n\nb\n")
        self.assertIn("no content", str(ctx.exception))

    def test_digest_ignores_prose_outside_stages_but_not_stage_edits(self):
        base = plan_digest(parse_plan(PLAN))
        self.assertEqual(plan_digest(parse_plan(PLAN.replace("reviewed preamble", "edited"))), base)
        self.assertNotEqual(plan_digest(parse_plan(PLAN.replace("Lay the groundwork", "Dig"))), base)


class _PlanRepoTestCase(unittest.TestCase):
    """A real repo with a real bare remote, the plan committed, and the
    workflow directories git-ignored as the README prescribes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True)

        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        self.plan_path = self.repo / "docs" / "plan.md"
        self.plan_path.parent.mkdir()
        self.plan_path.write_text(PLAN, encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.state_path = plan_state_path(self.sparring_dir, "docs/plan.md")

    def _start(self, stage_adapter, sparring_adapter, **kwargs):
        return start_plan(
            self.plan_path, self.sparring_dir, self.repo, stage_adapter, sparring_adapter,
            expected_branch="feature/x", **kwargs,
        )

    def _resume(self, stage_adapter, sparring_adapter, **kwargs):
        return resume_plan(
            self.plan_path, self.sparring_dir, self.repo, stage_adapter, sparring_adapter,
            expected_branch="feature/x", **kwargs,
        )

    def _stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def _plan_state(self) -> PlanRunState:
        return PlanRunState.load(self.state_path)


class PlanRunTests(_PlanRepoTestCase):
    def test_two_ready_stages_freeze_accept_and_complete_the_plan(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY, READY])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([sid for sid, _ in result.accepted], ["stage-1-foundation", "stage-2-incremental-rendering"])
        s1, s2 = self._stage("stage-1-foundation").read_state(), self._stage("stage-2-incremental-rendering").read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertIs(s2.status, StageStatus.ACCEPTED)
        self.assertNotEqual(s1.candidate_sha, s2.candidate_sha)
        self.assertEqual(s2.candidate_sha, _head_sha(self.repo))
        self.assertEqual(dict(result.accepted)["stage-1-foundation"], s1.candidate_sha)
        # Fresh implementation and sparring sessions per planned stage.
        self.assertEqual(len(stage_adapter.start_calls), 2)
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual((s1.implementation_session_id, s1.sparring_session_id), ("impl-1", "spar-1"))
        self.assertEqual((s2.implementation_session_id, s2.sparring_session_id), ("impl-2", "spar-2"))
        # The plan section is the stage brief.
        brief = self._stage("stage-2-incremental-rendering").read_brief()
        self.assertIn("Stage 2 of 2 from plan `docs/plan.md`", brief)
        self.assertIn("## Stage 2 — Incremental rendering\n\nRender incrementally.", brief)
        self.assertIn("## Stage 2 — Incremental rendering", stage_adapter.start_calls[1])
        self.assertNotIn("Lay the groundwork", stage_adapter.start_calls[1])
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.COMPLETE)
        self.assertEqual(state.current_stage, "stage-2-incremental-rendering")

    def test_send_back_stays_in_stage_one_sessions_then_stage_two_is_fresh(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([SEND_BACK, READY, READY])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(len(stage_adapter.start_calls), 2)
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1"])
        self.assertEqual(len(sparring_adapter.start_calls), 2)
        self.assertEqual([sid for sid, _ in sparring_adapter.resume_calls], ["spar-1"])
        s2 = self._stage("stage-2-incremental-rendering").read_state()
        self.assertEqual((s2.implementation_session_id, s2.sparring_session_id), ("impl-2", "spar-2"))

    def test_needs_you_pauses_without_accepting_or_creating_the_next_stage(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, "stage-1-foundation")
        self.assertIs(result.routing.action, RoutingAction.NEEDS_YOU)
        self.assertEqual(result.routing.summary, "check it on a device")
        self.assertIs(self._stage("stage-1-foundation").read_state().status, StageStatus.WORKING)
        self.assertFalse(self._stage("stage-2-incremental-rendering").exists())
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertEqual(len(sparring_adapter.start_calls), 1)
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        self.assertEqual((state.current_stage_index, state.current_stage), (0, "stage-1-foundation"))

    def test_resume_after_needs_you_adds_evidence_resumes_same_sessions_and_continues(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        self._start(stage_adapter, sparring_adapter)

        result = self._resume(stage_adapter, sparring_adapter, evidence="Tested on Pixel 7: resume works after 24h.")

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        stage1 = self._stage("stage-1-foundation")
        notes = stage1.read_notes()
        self.assertIn("## Human evidence\n\nTested on Pixel 7", notes)
        # The SAME stage resumed with the SAME provider sessions; no new
        # stage was created for the answer.
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1"])
        self.assertEqual([sid for sid, _ in sparring_adapter.resume_calls], ["spar-1"])
        self.assertEqual(len(stage_adapter.start_calls), 2)  # stage 1 once, stage 2 once
        s1 = stage1.read_state()
        self.assertEqual((s1.implementation_session_id, s1.sparring_session_id), ("impl-1", "spar-1"))
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        # Both agents were shown the evidence on the resumed turn.
        _, stage_prompt = stage_adapter.resume_calls[0]
        self.assertIn("## Human evidence", stage_prompt)
        self.assertIn("Tested on Pixel 7", stage_prompt)
        _, sparring_prompt = sparring_adapter.resume_calls[0]
        self.assertIn("Tested on Pixel 7", sparring_prompt)
        self.assertIn("Tested on Pixel 7", stage1.read_handoff())
        # Stage 2 then proceeded with fresh sessions and the plan completed.
        s2 = self._stage("stage-2-incremental-rendering").read_state()
        self.assertEqual((s2.implementation_session_id, s2.sparring_session_id), ("impl-2", "spar-2"))
        self.assertIs(s2.status, StageStatus.ACCEPTED)
        self.assertIs(self._plan_state().status, PlanRunStatus.COMPLETE)

    def test_same_sha_reconsidered_and_accepted_after_evidence_only(self):
        stage_adapter = _StageAdapter(self.repo)  # never commits
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        self._start(stage_adapter, sparring_adapter)
        sha_at_pause = _head_sha(self.repo)

        self._resume(stage_adapter, sparring_adapter, evidence="Checked; behaves as specified.")

        s1 = self._stage("stage-1-foundation").read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, sha_at_pause)
        self.assertEqual(_head_sha(self.repo), sha_at_pause)  # no dummy commit

    def test_evidence_appends_under_one_heading(self):
        stage = Stage.resolve(self.sparring_dir, "s").create()
        record_human_evidence(stage, "first answer")
        record_human_evidence(stage, "second answer\n")
        notes = stage.read_notes()
        self.assertEqual(notes.count("## Human evidence"), 1)
        self.assertIn("## Human evidence\n\nfirst answer\n\nsecond answer\n", notes)
        prompt = build_stage_prompt(stage, self.sparring_dir, resume=True, expected_branch="feature/x")
        self.assertIn("first answer\n\nsecond answer", prompt)

    def test_escalate_pauses_without_accepting_or_advancing(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([ESCALATE])

        result = self._start(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIs(result.routing.action, RoutingAction.ESCALATE)
        self.assertIs(self._stage("stage-1-foundation").read_state().status, StageStatus.WORKING)
        self.assertFalse(self._stage("stage-2-incremental-rendering").exists())
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_manual_acceptance_of_the_paused_stage_is_advanced_past_on_resume(self):
        # The ESCALATE path: sparred elsewhere, then accepted by hand through
        # the existing gate; resume-plan advances without re-running stage 1.
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([ESCALATE, READY])
        self._start(stage_adapter, sparring_adapter)
        from agent_sparring.acceptance import accept_candidate, freeze_candidate

        stage1 = self._stage("stage-1-foundation")
        freeze_candidate(stage1, self.sparring_dir, self.repo, expected_branch="feature/x")
        accept_candidate(stage1, self.repo, expected_branch="feature/x")

        result = self._resume(stage_adapter, sparring_adapter)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(stage_adapter.resume_calls, [])  # stage 1 was not re-run
        self.assertEqual(len(stage_adapter.start_calls), 2)
        self.assertIs(self._stage("stage-2-incremental-rendering").read_state().status, StageStatus.ACCEPTED)

    def test_freeze_refusal_stops_the_plan_without_advancing(self):
        stage_adapter = _StageAdapter(self.repo, commit=True, push=False)  # unpushed candidate
        sparring_adapter = _SparringAdapter([READY, READY])

        with self.assertRaises(PlanRunError) as ctx:
            self._start(stage_adapter, sparring_adapter)

        self.assertIn("acceptance gate refused", str(ctx.exception))
        s1 = self._stage("stage-1-foundation").read_state()
        self.assertIs(s1.status, StageStatus.WORKING)
        self.assertIsNone(s1.candidate_sha)
        self.assertFalse(self._stage("stage-2-incremental-rendering").exists())
        self.assertEqual(len(stage_adapter.start_calls), 1)
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        self.assertEqual(state.current_stage_index, 0)

    def test_accept_refusal_after_freeze_stops_the_plan_without_advancing(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([READY, READY])

        with mock.patch(
            "agent_sparring.plan.accept_candidate", side_effect=StaleCandidateError("moved on")
        ):
            with self.assertRaises(PlanRunError):
                self._start(stage_adapter, sparring_adapter)

        s1 = self._stage("stage-1-foundation").read_state()
        self.assertIs(s1.status, StageStatus.FROZEN)
        self.assertFalse(self._stage("stage-2-incremental-rendering").exists())
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_provider_failure_stops_the_plan_without_advancing(self):
        stage_adapter = _StageAdapter(self.repo, fail_at=1)
        sparring_adapter = _SparringAdapter([READY, READY])

        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, sparring_adapter)

        self.assertEqual(sparring_adapter.start_calls, [])
        self.assertFalse(self._stage("stage-2-incremental-rendering").exists())
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        self.assertEqual(state.current_stage, "stage-1-foundation")

    def test_runaway_limit_stops_the_plan(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([SEND_BACK] * 5)

        with self.assertRaises(PlanRunError) as ctx:
            self._start(stage_adapter, sparring_adapter, max_send_back_cycles=1)

        self.assertIn("runaway", str(ctx.exception))
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_malformed_plan_is_refused_before_any_provider_runs(self):
        self.plan_path.write_text("## Stage 1 — A\n\na\n\n## Stage 3 — B\n\nb\n", encoding="utf-8")
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([READY])

        with self.assertRaises(PlanError):
            self._start(stage_adapter, sparring_adapter)

        self.assertEqual(stage_adapter.start_calls, [])
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.sparring_dir / "stages").exists())

    def test_changed_plan_after_pause_is_refused_on_resume(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        self._start(stage_adapter, sparring_adapter)
        self.plan_path.write_text(PLAN.replace("Render incrementally.", "Render everything at once."), encoding="utf-8")

        with self.assertRaises(PlanError) as ctx:
            self._resume(stage_adapter, sparring_adapter, evidence="done")

        self.assertIn("changed since this run started", str(ctx.exception))
        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)
        self.assertNotIn("Human evidence", self._stage("stage-1-foundation").read_notes())

    def test_prose_edits_outside_stages_do_not_block_resume(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        self._start(stage_adapter, sparring_adapter)
        self.plan_path.write_text(PLAN.replace("reviewed preamble", "a typo fix"), encoding="utf-8")
        _run_git(self.repo, "commit", "-q", "-am", "typo fix outside any stage")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        result = self._resume(stage_adapter, sparring_adapter, evidence="done")

        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_start_refuses_when_a_run_is_already_recorded(self):
        stage_adapter = _StageAdapter(self.repo)
        self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        with self.assertRaises(PlanError) as ctx:
            self._start(stage_adapter, _SparringAdapter([READY]))
        self.assertIn("resume-plan", str(ctx.exception))
        self.assertEqual(len(stage_adapter.start_calls), 1)

    def test_resume_refuses_without_a_recorded_run_on_a_different_branch_or_when_complete(self):
        stage_adapter = _StageAdapter(self.repo)
        with self.assertRaises(PlanError):
            self._resume(stage_adapter, _SparringAdapter([READY]))

        self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        with self.assertRaises(PlanError) as ctx:
            resume_plan(
                self.plan_path, self.sparring_dir, self.repo, stage_adapter, _SparringAdapter([READY]),
                expected_branch="feature/other",
            )
        self.assertIn("different branch", str(ctx.exception))

        self._resume(stage_adapter, _SparringAdapter([READY, READY]))
        with self.assertRaises(PlanError) as ctx:
            self._resume(stage_adapter, _SparringAdapter([READY]))
        self.assertIn("already complete", str(ctx.exception))

    def test_existing_stage_with_a_different_brief_is_refused(self):
        stage = Stage.resolve(self.sparring_dir, "stage-1-foundation").create()
        stage.write_brief("# something else\n")
        stage_adapter = _StageAdapter(self.repo)
        with self.assertRaises(PlanError) as ctx:
            self._start(stage_adapter, _SparringAdapter([READY]))
        self.assertIn("differs", str(ctx.exception))
        self.assertEqual(stage_adapter.start_calls, [])

    def test_refuses_to_start_when_plan_state_would_be_visible_to_git(self):
        (self.repo / ".gitignore").write_text(".sparring/stages/\n", encoding="utf-8")
        _run_git(self.repo, "commit", "-q", "-am", "drop plans ignore")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        stage_adapter = _StageAdapter(self.repo)
        with self.assertRaises(PlanError) as ctx:
            self._start(stage_adapter, _SparringAdapter([READY]))
        self.assertIn(".gitignore", str(ctx.exception))
        self.assertEqual(stage_adapter.start_calls, [])
        self.assertFalse(self.state_path.exists())

    def test_three_stages_pause_on_the_third_and_resume_to_completion(self):
        self.plan_path.write_text(THREE_STAGE_PLAN, encoding="utf-8")
        _run_git(self.repo, "commit", "-q", "-am", "three stages")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([READY, READY, NEEDS_YOU, READY])

        result = self._start(stage_adapter, sparring_adapter)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, "stage-3-prefetch-and-enrichment")
        self.assertEqual(len(result.accepted), 2)
        self.assertEqual(self._plan_state().current_stage_index, 2)

        result = self._resume(stage_adapter, sparring_adapter, evidence="approved")
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-3"])
        self.assertEqual(len(stage_adapter.start_calls), 3)


class PlanCliTests(_PlanRepoTestCase):
    def _main(self, *argv: str, adapters=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        patch = (
            mock.patch("agent_sparring.cli._build_loop_adapters", return_value=adapters)
            if adapters is not None
            else contextlib.nullcontext()
        )
        with patch, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_run_plan_pauses_on_needs_you_and_prints_how_to_resume(self):
        adapters = (_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU]))
        code, out, err = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", adapters=adapters,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("plan paused: docs/plan.md", out)
        self.assertIn("action: NEEDS_YOU", out)
        self.assertIn("DEVICE/MANUAL CHECK -- Android", out)
        self.assertIn("findings: check it on a device", out)  # sparring.md findings
        self.assertIn(f"sparring resume-plan {self.plan_path}", out)
        self.assertIn("--evidence", out)
        self.assertIn("stage 1/2 stage-1-foundation", err)

    def test_resume_plan_completes_and_prints_accepted_shas(self):
        adapters = (_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU, READY, READY]))
        self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", adapters=adapters,
        )
        code, out, err = self._main(
            "resume-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--evidence", "checked on device", adapters=adapters,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("plan complete: docs/plan.md", out)
        self.assertIn(f"accepted stage-2-incremental-rendering at {_head_sha(self.repo)}", out)
        self.assertIn("recorded human evidence", err)

    def test_escalate_output_points_at_the_handoff_and_packet_commands(self):
        adapters = (_StageAdapter(self.repo), _SparringAdapter([ESCALATE]))
        code, out, _ = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", adapters=adapters,
        )
        self.assertEqual(code, 0)
        self.assertIn("action: ESCALATE", out)
        self.assertIn(str(self.sparring_dir / "stages" / "stage-1-foundation" / "handoff.md"), out)
        self.assertIn("--self-contained", out)
        self.assertIn("record-sparring stage-1-foundation", out)

    def test_malformed_plan_exits_nonzero_before_running(self):
        self.plan_path.write_text("# no stages here\n", encoding="utf-8")
        adapters = (_StageAdapter(self.repo), _SparringAdapter([READY]))
        code, _, err = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", adapters=adapters,
        )
        self.assertEqual(code, 1)
        self.assertIn("could not run plan", err)
        self.assertEqual(adapters[0].start_calls, [])


if __name__ == "__main__":
    unittest.main()
