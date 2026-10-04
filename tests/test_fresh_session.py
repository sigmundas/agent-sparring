"""next_turn authority, legacy derivation and fresh session generations.

The stuck run these exist for: SEND_BACK -> the implementation agent fixes
the candidate -> the reviewer starts and fails before a verdict. The engine
must remember that the fixed candidate is owed a review, and both an
ordinary resume and a fresh reviewer must review it directly instead of
spending another implementation turn.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.agent_config import AgentConfigError
from agent_sparring.git_context import GitContextError
from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.next_turn import (
    AmbiguousNextTurn,
    NextTurnError,
    capture_candidate,
    record_next_turn,
    resolve_resume_turn,
    verify_candidate,
)
from agent_sparring.plan import PlanRunError, PlanRunStatus
from agent_sparring.providers import ProviderError, SparringAgentResult
from agent_sparring.sessions import SessionError, generations, start_fresh_session
from agent_sparring.sparring_prompt import FRESH_REVIEWER_NOTICE
from agent_sparring.stage import CandidateRepository, PinnedAgent, Stage, StageState, StageStatus
from agent_sparring.usage import collect_stage_usage, render_stage_usage

from test_plan import (
    NEEDS_YOU,
    READY,
    S1,
    SEND_BACK,
    _PlanRepoTestCase,
    _SparringAdapter,
    _StageAdapter,
    _run_git,
)
from test_stage_agent_pin import PROVIDERS_ONLY, _loop_args, _set

FAIL = "<provider fails before a verdict>"


class _FailingSparringAdapter(_SparringAdapter):
    """Scripted verdicts, where ``FAIL`` raises ProviderError instead."""

    provider_id = "fake-reviewer-a"

    def _next(self, session_id):
        if self._verdicts and self._verdicts[0] == FAIL:
            self._verdicts.pop(0)
            raise ProviderError("reviewer crashed before a verdict")
        return super()._next(session_id)


class _OtherProviderSparringAdapter(_SparringAdapter):
    provider_id = "fake-reviewer-b"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _strip_marker(stage: Stage) -> None:
    """Make state.json look like it was written before next_turn existed."""

    path = stage.directory / "state.json"
    raw = json.loads(path.read_text())
    for key in ("next_turn", "next_turn_candidate", "next_turn_source"):
        raw.pop(key, None)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")


class _Stuck(_PlanRepoTestCase):
    def leave_stuck(self):
        """SEND_BACK, then a successful fix, then a reviewer that fails."""

        stage_adapter = _StageAdapter(self.repo, commit=True)
        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, _FailingSparringAdapter([SEND_BACK, FAIL]))
        stage = self._stage(S1)
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2)
        return stage, stage_adapter


class StuckRunTests(_Stuck):
    def test_a_failed_review_leaves_the_fixed_candidate_owed_a_review(self):
        stage, _ = self.leave_stuck()
        state = stage.read_state()
        self.assertEqual(state.next_turn, "sparring")
        self.assertEqual(state.next_turn_source, "engine")
        self.assertEqual(state.next_turn_candidate.head_sha, _head(self.repo))
        self.assertEqual(state.next_turn_candidate.kind, "commit")

    def test_ordinary_resume_reviews_that_candidate_without_an_implementation_turn(self):
        stage, stage_adapter = self.leave_stuck()
        fixed = _head(self.repo)
        sparring = _SparringAdapter([READY])

        result = self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2)
        # The same reviewer conversation, resumed.
        self.assertEqual([sid for sid, _ in sparring.resume_calls], ["spar-1"])
        self.assertEqual(sparring.start_calls, [])
        self.assertEqual(dict(result.accepted)[S1], fixed)
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)
        # Not labelled as a human-evidence review.
        index = (stage.directory / "prompts" / "index.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(index[-1])["turn_kind"], "resume")

    def test_a_fresh_reviewer_does_the_same_as_generation_two(self):
        stage, stage_adapter = self.leave_stuck()
        fixed = _head(self.repo)
        before = stage.read_state()

        start_fresh_session(stage, "sparring", "stuck", repo_root=self.repo)
        after_fresh = stage.read_state()
        self.assertEqual(after_fresh.next_turn, "sparring")  # never changed by a fresh session
        self.assertEqual(after_fresh.next_turn_candidate, before.next_turn_candidate)
        self.assertIsNone(after_fresh.sparring_session_id)

        sparring = _OtherProviderSparringAdapter([READY])
        result = self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2)
        self.assertEqual(len(sparring.start_calls), 1)
        self.assertEqual(sparring.resume_calls, [])
        prompt = sparring.start_calls[0]
        self.assertIn(FRESH_REVIEWER_NOTICE, prompt)
        self.assertIn("fix the guard", prompt)  # the prior unresolved finding
        self.assertIn(f"- Candidate commit: `{fixed}`", prompt)  # the current handoff
        self.assertEqual(dict(result.accepted)[S1], fixed)

        state = stage.read_state()
        gens = generations(state, "sparring")
        self.assertEqual([g.generation for g in gens], [1, 2])
        self.assertEqual(gens[0].session_id, "spar-1")
        self.assertEqual(gens[0].end_reason, "fresh:stuck")
        self.assertEqual(gens[1].session_id, "spar-1")  # the fake's first *start* id
        self.assertEqual(gens[1].start_reason, "fresh:stuck")
        self.assertEqual(state.sparring_session_id, gens[1].session_id)
        index = (stage.directory / "prompts" / "index.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(index[-1])["turn_kind"], "fresh")

    def test_a_drifted_head_or_worktree_is_refused_not_rerouted(self):
        for drift in ("commit", "worktree"):
            with self.subTest(drift=drift):
                self.setUp()
                stage, stage_adapter = self.leave_stuck()
                (self.repo / "drift.txt").write_text("not reviewed\n")
                if drift == "commit":
                    _run_git(self.repo, "add", "drift.txt")
                    _run_git(self.repo, "commit", "-q", "-m", "drift")
                sparring = _SparringAdapter([READY])
                with self.assertRaises(PlanRunError) as ctx:
                    self._resume(stage_adapter, sparring, stop_after_stage=S1)
                self.assertIn("waiting for review of", str(ctx.exception))
                self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
                self.assertEqual(
                    len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2
                )


class InverseTests(_PlanRepoTestCase):
    def leave_owed_implementation(self):
        """SEND_BACK, then an implementation turn that fails."""

        stage_adapter = _StageAdapter(self.repo, commit=True, fail_at=2)
        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, _SparringAdapter([SEND_BACK]))
        return self._stage(S1)

    def test_send_back_without_a_later_successful_turn_owes_the_stage(self):
        stage = self.leave_owed_implementation()
        self.assertEqual(stage.read_state().next_turn, "stage")

    def test_a_fresh_reviewer_does_not_skip_the_implementation_turn(self):
        stage = self.leave_owed_implementation()
        start_fresh_session(stage, "sparring", "new eyes", repo_root=self.repo)
        stage_adapter = _StageAdapter(self.repo, commit=True)
        stage_adapter._calls = 10  # commit files the earlier adapter did not
        sparring = _SparringAdapter([READY])

        self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1"])
        self.assertEqual(len(sparring.start_calls), 1)
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)

    def test_a_fresh_stage_agent_continues_with_the_send_back_findings(self):
        stage = self.leave_owed_implementation()
        start_fresh_session(stage, "stage", "context full", repo_root=self.repo)
        stage_adapter = _StageAdapter(self.repo, commit=True)
        stage_adapter._calls = 10  # commit files the earlier adapter did not
        sparring = _SparringAdapter([READY])

        self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual(stage_adapter.resume_calls, [])
        self.assertEqual(len(stage_adapter.start_calls), 1)
        prompt = stage_adapter.start_calls[0]
        self.assertIn("## Fresh session", prompt)
        self.assertIn("## Latest sparring exchange", prompt)
        self.assertIn("fix the guard", prompt)
        # The reviewer conversation is untouched by a stage-side fresh session.
        self.assertEqual([sid for sid, _ in sparring.resume_calls], ["spar-1"])
        gens = generations(stage.read_state(), "stage")
        self.assertEqual([(g.generation, g.session_id) for g in gens], [(1, "impl-1"), (2, "impl-1")])


class IntegrityTests(_Stuck):
    def test_a_fresh_session_changes_nothing_but_session_bookkeeping(self):
        stage, _ = self.leave_stuck()
        plan_before = self._plan_state()
        state_before = stage.read_state()
        files = ("brief.md", "notes.md", "handoff.md", "sparring.md", "activity.jsonl")
        texts = {name: (stage.directory / name).read_text() for name in files}
        prompts = sorted(p.name for p in (stage.directory / "prompts").iterdir())
        index = (stage.directory / "prompts" / "index.jsonl").read_text()
        head = _head(self.repo)

        start_fresh_session(stage, "sparring", "audit", repo_root=self.repo)
        start_fresh_session(stage, "stage", "audit", repo_root=self.repo)

        after = stage.read_state()
        for name in ("status", "base_sha", "candidate_sha", "repositories", "mode", "run",
                     "next_turn", "next_turn_candidate", "next_turn_source"):
            self.assertEqual(getattr(after, name), getattr(state_before, name), name)
        self.assertEqual(_head(self.repo), head)
        plan_after = self._plan_state()
        self.assertEqual(plan_after.plan_digest, plan_before.plan_digest)
        self.assertEqual(plan_after.current_stage_index, plan_before.current_stage_index)
        for name in files[:-1]:
            self.assertEqual((stage.directory / name).read_text(), texts[name], name)
        # Activity is append-only: history before the fresh session is intact.
        self.assertTrue((stage.directory / "activity.jsonl").read_text().startswith(texts["activity.jsonl"]))
        self.assertEqual(sorted(p.name for p in (stage.directory / "prompts").iterdir()), prompts)
        self.assertEqual((stage.directory / "prompts" / "index.jsonl").read_text(), index)

    def test_a_fresh_session_cannot_reach_acceptance_without_a_ready_review(self):
        stage, stage_adapter = self.leave_stuck()
        start_fresh_session(stage, "sparring", "audit", repo_root=self.repo)
        result = self._resume(stage_adapter, _SparringAdapter([NEEDS_YOU]), stop_after_stage=S1)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsNot(stage.read_state().status, StageStatus.ACCEPTED)

    def test_a_pending_fresh_session_is_not_started_twice_and_accepted_stages_refuse(self):
        stage, stage_adapter = self.leave_stuck()
        start_fresh_session(stage, "sparring", "one", repo_root=self.repo)
        with self.assertRaises(SessionError):
            start_fresh_session(stage, "sparring", "two", repo_root=self.repo)
        self._resume(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        with self.assertRaises(SessionError):
            start_fresh_session(stage, "stage", "late", repo_root=self.repo)


class LegacyDerivationTests(_Stuck):
    def test_unambiguous_sparring_is_derived_once_and_recorded(self):
        stage, stage_adapter = self.leave_stuck()
        _strip_marker(stage)
        self.assertIsNone(stage.read_state().next_turn)

        self.assertEqual(resolve_resume_turn(self.repo, stage), "sparring")
        state = stage.read_state()
        self.assertEqual((state.next_turn, state.next_turn_source), ("sparring", "derived"))
        self.assertEqual(state.next_turn_candidate.head_sha, _head(self.repo))

        sparring = _SparringAdapter([READY])
        self._resume(stage_adapter, sparring, stop_after_stage=S1)
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 2)

    def test_unambiguous_stage_after_send_back(self):
        stage_adapter = _StageAdapter(self.repo, commit=True, fail_at=2)
        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, _SparringAdapter([SEND_BACK]))
        stage = self._stage(S1)
        _strip_marker(stage)
        self.assertEqual(resolve_resume_turn(self.repo, stage), "stage")
        self.assertEqual(stage.read_state().next_turn_source, "derived")

    def test_head_moved_past_the_handoff_candidate_is_ambiguous_and_refused(self):
        stage, stage_adapter = self.leave_stuck()
        _strip_marker(stage)
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")

        with self.assertRaises(AmbiguousNextTurn) as ctx:
            resolve_resume_turn(self.repo, stage)
        self.assertIn("choose explicitly", str(ctx.exception))
        sparring = _SparringAdapter([READY])
        with self.assertRaises(PlanRunError):
            self._resume(stage_adapter, sparring, stop_after_stage=S1)
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertIsNone(stage.read_state().next_turn)

    def test_an_explicit_choice_is_recorded_as_manual_and_never_rederived(self):
        stage, stage_adapter = self.leave_stuck()
        _strip_marker(stage)
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")

        self.assertEqual(resolve_resume_turn(self.repo, stage, choice="sparring"), "sparring")
        state = stage.read_state()
        self.assertEqual((state.next_turn, state.next_turn_source), ("sparring", "manual"))
        self.assertEqual(state.next_turn_candidate.head_sha, _head(self.repo))
        # A later resume obeys it rather than deriving again.
        with mock.patch("agent_sparring.next_turn.derive_next_turn") as derive:
            self.assertEqual(resolve_resume_turn(self.repo, stage), "sparring")
        derive.assert_not_called()

    def test_resume_plan_accepts_the_explicit_choice(self):
        stage, stage_adapter = self.leave_stuck()
        _strip_marker(stage)
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

        sparring = _SparringAdapter([READY])
        self._resume(stage_adapter, sparring, stop_after_stage=S1, next_turn="stage")
        self.assertEqual(len(stage_adapter.resume_calls), 2)  # one more implementation turn
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)


class _LoopRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "t@example.com")
        _run_git(self.repo, "config", "user.name", "T")
        (self.repo / ".gitignore").write_text(".sparring/\n")
        (self.repo / "f.txt").write_text("hi\n")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "s1").create()


class TransactionalTests(_LoopRepo):
    def test_a_failure_recording_the_handoff_leaves_next_turn_at_stage(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring = _SparringAdapter([READY])
        with mock.patch(
            "agent_sparring.stage_agent.generate_handoff", side_effect=GitContextError("disk full")
        ):
            with self.assertRaises(LoopError):
                run_unattended_loop(
                    self.stage, self.sparring_dir, self.repo, stage_adapter, sparring,
                    expected_branch="feature/x",
                )
        self.assertEqual(self.stage.read_state().next_turn, "stage")
        self.assertEqual(sparring.start_calls, [])

    def test_a_failure_capturing_the_candidate_leaves_next_turn_at_stage(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring = _SparringAdapter([READY])
        with mock.patch(
            "agent_sparring.loop.capture_candidate", side_effect=NextTurnError("unreadable")
        ):
            with self.assertRaises(LoopError):
                run_unattended_loop(
                    self.stage, self.sparring_dir, self.repo, stage_adapter, sparring,
                    expected_branch="feature/x",
                )
        self.assertEqual(self.stage.read_state().next_turn, "stage")

    def test_a_provider_reported_failure_never_advances_the_marker(self):
        class _ErrorStage(_StageAdapter):
            def _turn(self, session_id):
                result = super()._turn(session_id)
                return type(result)(session_id=result.session_id, text="x", is_error=True)

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage, self.sparring_dir, self.repo, _ErrorStage(self.repo),
                _SparringAdapter([READY]), expected_branch="feature/x",
            )
        self.assertEqual(self.stage.read_state().next_turn, "stage")


class SiblingPinTests(_LoopRepo):
    def test_a_moved_sibling_is_a_candidate_mismatch(self):
        sibling = Path(self._tmp.name) / "sibling"
        sibling.mkdir()
        _run_git(sibling, "init", "-q", "-b", "main")
        _run_git(sibling, "config", "user.email", "t@example.com")
        _run_git(sibling, "config", "user.name", "T")
        (sibling / "s.txt").write_text("1\n")
        _run_git(sibling, "add", "-A")
        _run_git(sibling, "commit", "-q", "-m", "one")
        state = self.stage.read_state()
        state.repositories = (CandidateRepository(name="lib", path=str(sibling), branch="main"),)
        self.stage.write_state(state)

        candidate = capture_candidate(self.repo, self.stage, self.stage.read_state())
        record_next_turn(self.stage, "sparring", candidate=candidate)
        verify_candidate(self.repo, self.stage, self.stage.read_state())  # matches

        (sibling / "s.txt").write_text("2\n")
        _run_git(sibling, "commit", "-q", "-am", "two")
        with self.assertRaises(NextTurnError) as ctx:
            verify_candidate(self.repo, self.stage, self.stage.read_state())
        self.assertIn("sibling lib", str(ctx.exception))


class StateCompatibilityTests(unittest.TestCase):
    def test_absent_fields_round_trip_byte_identically(self):
        raw = {"status": "working", "implementation_session_id": "a", "sparring_session_id": None,
               "base_sha": None, "candidate_sha": None}
        self.assertEqual(StageState.from_dict(raw).to_dict(), raw)

    def test_a_legacy_session_is_generation_one(self):
        state = StageState(implementation_session_id="impl-1")
        gens = generations(state, "stage")
        self.assertEqual([(g.generation, g.session_id, g.start_reason) for g in gens],
                         [(1, "impl-1", "initial")])
        self.assertEqual(generations(state, "sparring"), [])
        self.assertNotIn("sessions", state.to_dict())


class ConfigurationPerSessionTests(unittest.TestCase):
    """Generation 1 keeps its pin; a fresh generation resolves anew."""

    def setUp(self):
        from test_stage_agent_pin import _git

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()
        (self.repo / ".gitignore").write_text(".sparring/stages/\n")
        (self.sparring_dir / "project.toml").write_text(PROVIDERS_ONLY)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "config")
        self.stage = Stage.resolve(self.sparring_dir, "s1").create()

    def build(self, **overrides):
        from agent_sparring.cli import _build_loop_adapters

        return _build_loop_adapters(
            _loop_args(**overrides), self.sparring_dir, self.repo, stage=self.stage
        )

    def _record_session(self, role: str, session_id: str) -> None:
        from agent_sparring.sessions import record_session_id

        state = self.stage.read_state()
        record_session_id(state, role, session_id)
        self.stage.write_state(state)

    def test_model_switch_takes_effect_only_in_a_fresh_session(self):
        _set("stage", "--model", "claude-opus-5-5")
        first, _ = self.build()
        self._record_session("stage", "impl-1")
        _set("stage", "--model", "claude-sonnet-5-5")

        again, _ = self.build()  # an ordinary resume
        self.assertEqual(again.model, "claude-opus-5-5")

        start_fresh_session(self.stage, "stage", "switch model", repo_root=self.repo)
        fresh, _ = self.build()
        self.assertEqual(fresh.model, "claude-sonnet-5-5")
        # Building the adapters pins nothing for the pending generation...
        self.assertNotIn("stage", self.stage.read_state().agents)
        # ...its first turn does (what run_stage_agent does before starting).
        from agent_sparring.sessions import pin_pending_generation

        state = self.stage.read_state()
        self.assertTrue(pin_pending_generation(state, "stage", fresh))
        self.stage.write_state(state)
        self._record_session("stage", "impl-2")
        # Later turns of the fresh session keep its own pin.
        _set("stage", "--model", "claude-haiku-4-5-20251001")
        self.assertEqual(self.build()[0].model, "claude-sonnet-5-5")

        gens = generations(self.stage.read_state(), "stage")
        self.assertEqual([g.agent.model for g in gens], ["claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual([g.session_id for g in gens], ["impl-1", "impl-2"])
        # The other role's pin is untouched.
        self.assertIn("sparring", self.stage.read_state().agents)

    def test_an_override_mid_session_names_a_fresh_session_as_the_way(self):
        _set("stage", "--model", "claude-opus-5-5")
        self.build()
        self._record_session("stage", "impl-1")
        with self.assertRaisesRegex(AgentConfigError, "fresh stage session"):
            self.build(stage_model="claude-sonnet-5-5")
        start_fresh_session(self.stage, "stage", "override", repo_root=self.repo)
        fresh, _ = self.build(stage_model="claude-sonnet-5-5")
        self.assertEqual(fresh.model, "claude-sonnet-5-5")

    def test_provider_switch_across_two_fake_providers(self):
        # Generation 1 ran with one provider; the fresh generation's pin is
        # whatever resolves at its first turn, recorded per generation.
        self.build()
        self._record_session("sparring", "spar-1")
        start_fresh_session(self.stage, "sparring", "switch provider", repo_root=self.repo)
        state = self.stage.read_state()
        from agent_sparring.sessions import record_pin

        other = PinnedAgent(provider="fake-reviewer-b", model=None, model_source="cli",
                            effort=None, effort_source="provider-default")
        state.agents = {**(state.agents or {}), "sparring": other}
        record_pin(state, "sparring", other)
        self.stage.write_state(state)
        gens = generations(self.stage.read_state(), "sparring")
        self.assertEqual([g.agent.provider for g in gens], ["codex-cli", "fake-reviewer-b"])
        # And the per-session lock now refuses the original provider for
        # this session rather than silently switching back.
        with self.assertRaisesRegex(AgentConfigError, "fresh sparring session"):
            self.build()


class UsageTests(_Stuck):
    def test_usage_reports_generation_boundaries_and_per_generation_totals(self):
        stage, stage_adapter = self.leave_stuck()
        log = stage.activity_log()
        log.emit("sparrer", "provider.usage", input_tokens=100, output_tokens=10, total_tokens=110)
        start_fresh_session(stage, "sparring", "stuck", repo_root=self.repo)
        log.emit("sparrer", "agents.resolved", role="sparring", provider="codex-cli",
                 requested_model="gpt-6", model_source="user")
        self._resume(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        log.emit("sparrer", "provider.usage", input_tokens=40, output_tokens=4, total_tokens=44)

        usage = collect_stage_usage(stage)
        gens = usage.generations["sparring"]
        self.assertEqual([g.generation for g in gens], [1, 2])
        self.assertEqual([g.total_tokens for g in gens], [110, 44])
        self.assertEqual(usage.role_totals("sparring")["total_tokens"], 154)
        self.assertEqual(usage.generations["stage"][0].generation, 1)

        text = render_stage_usage(usage)
        self.assertIn("sparring session generation 2 (fresh:stuck; codex-cli, model gpt-6", text)
        self.assertIn("all 2 generations: tokens   in 140  out 14  total 154", text)
        self.assertIn("sparrer#2", text)


class _DirtyStageAdapter(_StageAdapter):
    """Leaves its work uncommitted: the candidate is the working tree."""

    def _turn(self, session_id):
        self._calls += 1
        if self.fail_at == self._calls:
            raise ProviderError("stage provider boom")
        (self.repo / "work.txt").write_text(f"turn {self._calls}\n")
        from agent_sparring.providers import StageAgentResult

        return StageAgentResult(session_id=session_id, text="claims", is_error=False)


class _EarlyAnnouncingFailingReviewer(_SparringAdapter):
    """Announces its new thread id, then fails before a verdict -- what a
    real reviewer that crashes mid-turn does."""

    def __init__(self, verdicts):
        super().__init__(verdicts)
        self.on_session_observed = None

    def start(self, prompt):
        self.start_calls.append(prompt)
        self._starts += 1
        sid = f"spar-{self._starts}"
        if self.on_session_observed is not None:
            self.on_session_observed(sid)
        if self._verdicts and self._verdicts[0] == FAIL:
            self._verdicts.pop(0)
            raise ProviderError("reviewer crashed after announcing its thread")
        return self._next(sid)


class SendBackFindingTests(_LoopRepo):
    """Regressions for the first review of this stage."""

    def run_loop(self, stage_adapter, sparring, **kwargs):
        return run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, stage_adapter, sparring,
            expected_branch="feature/x", **kwargs,
        )

    def test_initial_generations_are_persisted_with_their_start(self):
        self.run_loop(_StageAdapter(self.repo), _SparringAdapter([READY]))
        state = self.stage.read_state()
        for role, sid in (("stage", "impl-1"), ("sparring", "spar-1")):
            with self.subTest(role=role):
                gen = state.sessions[role]
                self.assertEqual(len(gen), 1)
                self.assertEqual((gen[0].generation, gen[0].session_id), (1, sid))
                self.assertEqual(gen[0].start_reason, "initial")
                self.assertIsNotNone(gen[0].started_at)

    def test_a_fresh_reviewers_announced_session_is_recorded_before_it_fails(self):
        with self.assertRaises(LoopError):
            self.run_loop(_StageAdapter(self.repo), _SparringAdapter([SEND_BACK, SEND_BACK]),
                          max_send_back_cycles=1)
        start_fresh_session(self.stage, "sparring", "crashy", repo_root=self.repo)
        sparring_md = (self.stage.directory / "sparring.md").read_text()
        reviewer = _EarlyAnnouncingFailingReviewer([FAIL, READY])
        with self.assertRaises(LoopError):
            self.run_loop(_StageAdapter(self.repo), reviewer,
                          start_with="sparring", sparring_first_reason="next_turn")
        state = self.stage.read_state()
        self.assertEqual(state.sparring_session_id, "spar-1")
        self.assertEqual(state.sessions["sparring"][-1].session_id, "spar-1")
        self.assertEqual((self.stage.directory / "sparring.md").read_text(), sparring_md)
        # The next turn resumes that conversation instead of opening a third.
        self.run_loop(_StageAdapter(self.repo), reviewer,
                      start_with="sparring", sparring_first_reason="next_turn")
        self.assertEqual([sid for sid, _ in reviewer.resume_calls], ["spar-1"])
        self.assertEqual(len(reviewer.start_calls), 1)

    def test_legacy_uncommitted_candidate_is_never_derived_as_sparring(self):
        with self.assertRaises(LoopError):
            self.run_loop(_DirtyStageAdapter(self.repo), _FailingSparringAdapter([SEND_BACK, FAIL]))
        self.assertEqual(self.stage.read_state().next_turn_candidate.kind, "worktree")
        _strip_marker(self.stage)
        # Same dirty path, edited bytes: the handoff cannot tell these apart.
        (self.repo / "work.txt").write_text("edited after the turn\n")
        with self.assertRaises(AmbiguousNextTurn) as ctx:
            resolve_resume_turn(self.repo, self.stage)
        self.assertIn("uncommitted content", str(ctx.exception))

    def test_standalone_resume_honours_finalization_and_human_gates(self):
        from agent_sparring.next_turn import standalone_start_with
        from agent_sparring.routing import HumanGate, RoutingAction, RoutingResult
        from agent_sparring.sparring_exchange import record_sparring
        from test_plan import human_gate

        (self.repo / "work.txt").write_text("reviewed, uncommitted\n")
        record_sparring(self.stage, RoutingResult(action=RoutingAction.READY, summary="ok"))
        record_next_turn(self.stage, "finalization",
                         candidate=capture_candidate(self.repo, self.stage, self.stage.read_state()))
        self.assertEqual(standalone_start_with(self.repo, self.stage), "finalization")
        # The reviewed content changed before its commit: refused, never an
        # implementation turn.
        (self.repo / "work.txt").write_text("changed after READY\n")
        with self.assertRaises(AmbiguousNextTurn):
            standalone_start_with(self.repo, self.stage)
        # The reviewed content was committed but nothing verified it yet:
        # the reviewer rules on that commit.
        (self.repo / "work.txt").write_text("reviewed, uncommitted\n")
        _run_git(self.repo, "add", "work.txt")
        _run_git(self.repo, "commit", "-q", "-m", "finalized, unverified")
        self.assertEqual(standalone_start_with(self.repo, self.stage), "sparring")
        self.assertEqual(self.stage.read_state().next_turn_candidate.head_sha, _head(self.repo))

        candidate = capture_candidate(self.repo, self.stage, self.stage.read_state())
        record_next_turn(self.stage, "sparring", candidate=candidate)
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.NEEDS_YOU, summary="check it",
                          human_gate=HumanGate.from_dict(human_gate())),
        )
        with self.assertRaises(NextTurnError) as ctx:
            standalone_start_with(self.repo, self.stage)
        self.assertIn("waiting for a person", str(ctx.exception))

    def test_run_stage_pins_only_the_stage_role(self):
        from agent_sparring.agent_config import RoleOverrides, resolve_agent_configs
        from agent_sparring.cli import _stage_agents

        _stage_agents(self.stage, resolve_agent_configs(None, stage=RoleOverrides()),
                      record=("stage",))
        state = self.stage.read_state()
        self.assertEqual(set(state.agents), {"stage"})
        self.assertNotIn("sparring", state.sessions)
        with self.assertRaises(SessionError):
            start_fresh_session(self.stage, "sparring", "too early", repo_root=self.repo)

    def test_a_fresh_independent_reviewer_gets_the_fresh_context(self):
        from agent_sparring.review_prompt import assemble_review_prompt

        (self.stage.directory / "sparring.md").write_text("## Routing outcome\n\nold finding X\n")
        assembled = assemble_review_prompt(
            self.stage, self.sparring_dir, resume=False, expected_branch="feature/x",
            candidate_set="(set)", fresh=True,
        )
        self.assertEqual(assembled.turn_kind, "fresh")
        self.assertIn(FRESH_REVIEWER_NOTICE, assembled.text)
        self.assertIn("old finding X", assembled.text)


class SecondReviewFindingTests(_LoopRepo):
    def test_an_unreadable_declared_sibling_refuses_candidate_capture(self):
        state = self.stage.read_state()
        state.repositories = (
            CandidateRepository(name="lib", path=str(self.repo.parent / "missing"), branch="main"),
        )
        self.stage.write_state(state)
        with self.assertRaises(NextTurnError) as ctx:
            capture_candidate(self.repo, self.stage, self.stage.read_state())
        self.assertIn("sibling repository 'lib'", str(ctx.exception))

    def test_fresh_is_the_captured_turn_kind_whatever_else_the_turn_is(self):
        from agent_sparring.prompt_sections import (
            review_turn_kind,
            sparring_turn_kind,
            stage_turn_kind,
        )

        self.assertEqual(stage_turn_kind(resume=False, finalize_only=True, fresh=True), "fresh")
        for kwargs in ({"evidence_first": True}, {"finalization": True}):
            self.assertEqual(sparring_turn_kind(resume=False, fresh=True, **kwargs), "fresh")
        self.assertEqual(review_turn_kind(resume=False, evidence_first=True, fresh=True), "fresh")

    def test_a_pending_fresh_reviewer_is_not_pinned_until_its_first_turn(self):
        from agent_sparring.sessions import pin_pending_generation, record_session_id

        state = self.stage.read_state()
        state.agents = {"sparring": PinnedAgent("codex-cli", "B", "user", None, "provider-default")}
        record_session_id(state, "sparring", "spar-1")
        self.stage.write_state(state)
        start_fresh_session(self.stage, "sparring", "new eyes", repo_root=self.repo)

        # An owed implementation turn fails; the fresh reviewer never runs.
        class _Built:
            pending_pin = PinnedAgent("codex-cli", "B", "user", None, "provider-default")

        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage, self.sparring_dir, self.repo, _StageAdapter(self.repo, fail_at=1),
                _SparringAdapter([READY]), expected_branch="feature/x",
            )
        self.assertNotIn("sparring", self.stage.read_state().agents or {})
        # A later preference C is therefore free to apply at its first turn.
        later = _Built()
        later.pending_pin = PinnedAgent("codex-cli", "C", "user", None, "provider-default")
        state = self.stage.read_state()
        self.assertTrue(pin_pending_generation(state, "sparring", later))
        self.assertEqual(state.agents["sparring"].model, "C")
        self.assertEqual(state.sessions["sparring"][-1].agent.model, "C")
        self.assertEqual(state.sessions["sparring"][0].agent.model, "B")

    def test_a_fresh_reviewer_turn_writes_its_pin_before_the_provider_starts(self):
        from agent_sparring.sessions import record_session_id

        state = self.stage.read_state()
        record_session_id(state, "sparring", "spar-0")
        state.agents = {"sparring": PinnedAgent("codex-cli", "B", "user", None, "provider-default")}
        self.stage.write_state(state)
        start_fresh_session(self.stage, "sparring", "new eyes", repo_root=self.repo)
        reviewer = _FailingSparringAdapter([FAIL])
        reviewer.pending_pin = PinnedAgent("codex-cli", "C", "user", None, "provider-default")
        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage, self.sparring_dir, self.repo, _StageAdapter(self.repo), reviewer,
                expected_branch="feature/x",
            )
        self.assertEqual(self.stage.read_state().agents["sparring"].model, "C")


class FinalizationRecoveryTests(_LoopRepo):
    def test_interrupted_between_ready_and_its_commit_routes_finalization_not_implementation(self):
        from agent_sparring.loop import FinalizationRefused

        # READY over an uncommitted candidate, then the process stops before
        # the commit turn runs (simulated: the commit turn raises).
        with self.assertRaises(LoopError):
            run_unattended_loop(
                self.stage, self.sparring_dir, self.repo,
                _DirtyStageAdapter(self.repo, fail_at=2), _SparringAdapter([READY]),
                expected_branch="feature/x",
            )
        state = self.stage.read_state()
        self.assertEqual(state.next_turn, "finalization")
        self.assertEqual(state.next_turn_candidate.kind, "worktree")
        from agent_sparring.next_turn import standalone_start_with

        self.assertEqual(standalone_start_with(self.repo, self.stage), "finalization")
        self.assertIsNotNone(FinalizationRefused)


class ReadyOverCommitRecoveryTests(_PlanRepoTestCase):
    def leave_ready_unaccepted(self):
        """READY over a committed candidate, then the run stops before the
        acceptance gate completes."""

        from agent_sparring.acceptance import AcceptanceError

        stage_adapter = _StageAdapter(self.repo, commit=True)
        with mock.patch(
            "agent_sparring.plan.freeze_candidate", side_effect=AcceptanceError("stopped here")
        ):
            with self.assertRaises(PlanRunError):
                self._start(stage_adapter, _SparringAdapter([READY]), stop_after_stage=S1)
        stage = self._stage(S1)
        state = stage.read_state()
        self.assertEqual(state.next_turn, "finalization")
        self.assertEqual(state.next_turn_candidate.kind, "commit")
        self.assertEqual(state.next_turn_candidate.head_sha, _head(self.repo))
        return stage

    def test_resume_completes_acceptance_for_the_reviewed_commit_with_no_agent_turn(self):
        stage = self.leave_ready_unaccepted()
        reviewed = _head(self.repo)
        stage_adapter, sparring = _StageAdapter(self.repo, commit=True), _SparringAdapter([])

        result = self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertEqual(dict(result.accepted)[S1], reviewed)
        self.assertIs(stage.read_state().status, StageStatus.ACCEPTED)

    def test_a_moved_candidate_after_ready_is_refused_not_implemented(self):
        stage = self.leave_ready_unaccepted()
        (self.repo / "later.txt").write_text("later\n")
        _run_git(self.repo, "add", "later.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")
        stage_adapter, sparring = _StageAdapter(self.repo, commit=True), _SparringAdapter([])

        with self.assertRaises(PlanRunError) as ctx:
            self._resume(stage_adapter, sparring, stop_after_stage=S1)

        self.assertIn("Only that reviewed commit may be pushed and accepted", str(ctx.exception))
        self.assertEqual(stage_adapter.start_calls + stage_adapter.resume_calls, [])
        self.assertEqual(sparring.start_calls + sparring.resume_calls, [])
        self.assertIsNot(stage.read_state().status, StageStatus.ACCEPTED)

    def test_standalone_run_loop_owes_no_agent_turn_after_a_committed_ready(self):
        from agent_sparring.next_turn import standalone_start_with

        stage = self.leave_ready_unaccepted()
        with self.assertRaises(NextTurnError) as ctx:
            standalone_start_with(self.repo, stage)
        self.assertIn("no agent turn is owed", str(ctx.exception))


class CodexEarlySessionTests(unittest.TestCase):
    def test_the_codex_stream_announces_its_thread_without_telemetry(self):
        from agent_sparring.providers.codex_cli import _CodexStreamTranslator

        seen = []
        translator = _CodexStreamTranslator(None, Path("."), on_session=seen.append)
        translator.feed(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
        translator.feed(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
        self.assertEqual(seen, ["t-1"])


if __name__ == "__main__":
    unittest.main()
