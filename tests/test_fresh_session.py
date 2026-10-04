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
        from agent_sparring.sessions import note_session_id

        state = self.stage.read_state()
        if role == "stage":
            state.implementation_session_id = session_id
        else:
            state.sparring_session_id = session_id
        note_session_id(state, role, session_id)
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
        from agent_sparring.sessions import note_pin

        other = PinnedAgent(provider="fake-reviewer-b", model=None, model_source="cli",
                            effort=None, effort_source="provider-default")
        state.agents = {**(state.agents or {}), "sparring": other}
        note_pin(state, "sparring", other)
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


if __name__ == "__main__":
    unittest.main()
