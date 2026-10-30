"""Plan L Stage 4: legacy pauses migrate safely (L-D1, L-D2, I-L3).

What these tests hold, one sentence each:

- a legacy ``push_authorization_required`` pause meeting every L-D1
  condition converts on a plain ``resume-plan``: the stage is frozen and
  accepted locally at exactly the reviewed commit, the ordinary post-READY
  sequence runs (a deferral reaches the ledger; the next stage's
  ``gates_before`` stops the run), ``legacy_push_pause_converted`` is
  recorded, and nothing is pushed or run;
- a pause failing any condition -- a promoted obligation, a moved ``HEAD``,
  a non-READY verdict, a dirty tree, a moved sibling, an edited plan, an
  unanswered human check -- stays paused, names the failed condition, and
  leaves the engine's state byte-identical, with no agent turn;
- a recorded incident pause (``unauthorized_remote_movement``,
  ``pushed_candidate_unauthorized``) stays until a person answers it; the
  answer clears only that pause and accepts nothing, and a pause raised
  after READY then goes through the same conditions.

Every negative test has its positive control: the same pause without the
damage converts, or the same damage with the conditions disabled in the
test only (``mock.patch``) is accepted.

Real disposable repositories and bare remotes throughout; no network, no
provider (scripted adapters).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import plan as plan_module
from agent_sparring.deferred_gate import DeferredVerificationRequired
from agent_sparring.human_gate import HumanGate
from agent_sparring.plan import (
    LEGACY_PUSH_PAUSE_CONVERTED,
    PlanError,
    PlanRunStatus,
    acknowledge_remote_movement,
    authorize_pushed_candidate,
    record_human_evidence,
)
from agent_sparring.push_gate import PUSH_AUTHORIZATION_REQUIRED, PushRequired
from agent_sparring.remote_pin import (
    MOVEMENT_ACKNOWLEDGED,
    PUSHED_CANDIDATE_UNAUTHORIZED,
    UNAUTHORIZED_REMOTE_MOVEMENT,
    PushedCandidateUnauthorized,
    UnauthorizedRemoteMovement,
)
from agent_sparring.deferred_gate import DEFERRED_GATE_MARKER
from agent_sparring.human_gate import HUMAN_GATE_MARKER
from agent_sparring.sparring_exchange import _human_gate_lines, read_recorded_outcome
from agent_sparring.stage import STATE_FILENAME, StageStatus

from git_recorder import GitRecorder
from test_deferred_human_verification import READY_WITH_DEFERRAL, _obligation
from test_managed_run import _git
from test_manifest import _ManifestRepoTestCase, _stage_entry, manifest_payload
from test_manifest_gates import S3C, S3D, gated_payload
from test_multi_repo_candidates import _make_repo
from test_plan import NEEDS_YOU, READY, S1, _SparringAdapter, _StageAdapter, _head_sha, _run_git, human_gate
from test_push_authorization import _Implementer, _RemoteAwareTestCase


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _interrupted_at_freeze(start) -> None:
    """Run ``start()`` until the reviewer's READY, stopping just before the
    acceptance gate -- where an older engine raised its push pause."""

    with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
        try:
            start()
        except KeyboardInterrupt:
            return
    raise AssertionError("the run did not reach the acceptance gate")


def _corrupt_after(text: str, marker: str) -> str:
    """Make the JSON fence after ``marker`` unparseable, keeping the marker."""

    at = text.index(marker)
    start = text.index("```json", at) + len("```json")
    return text[:start] + "\n{not json" + text[start:]


def _drop_key(payload, key: str) -> None:
    if isinstance(payload, dict):
        payload.pop(key, None)
        for value in payload.values():
            _drop_key(value, key)
    elif isinstance(payload, list):
        for value in payload:
            _drop_key(value, key)


def _push_pause(stage_id: str, sha: str) -> PushRequired:
    return PushRequired(
        kind=PUSH_AUTHORIZATION_REQUIRED, stage_id=stage_id, candidate_sha=sha, branch="feature/x",
        remote="origin", remote_branch="feature/x",
    )


class _LegacyCase(_RemoteAwareTestCase):
    """A Markdown-plan run an older engine paused after its reviewer's READY."""

    def setUp(self):
        super().setUp()
        self.reports: list[str] = []
        self.stage_adapter = _Implementer(self.repo)
        self.sparring_adapter = _SparringAdapter([])

    def _ready_not_accepted(self, verdict: str = READY) -> str:
        _interrupted_at_freeze(lambda: self._start(_Implementer(self.repo), _SparringAdapter([verdict])))
        return _head_sha(self.repo)

    def _pause(self, awaiting) -> None:
        state = self._plan_state()
        state.status = PlanRunStatus.PAUSED
        state.awaiting = awaiting
        state.save(self.state_path)

    def _legacy_push_pause(self, verdict: str = READY) -> str:
        sha = self._ready_not_accepted(verdict)
        self._pause(_push_pause(S1, sha))
        return sha

    def _resume_plain(self, **kwargs):
        return self._resume(
            self.stage_adapter, self.sparring_adapter, stop_after_stage=S1, report=self.reports.append, **kwargs
        )

    def _engine_state(self):
        return _files(self.sparring_dir), _git(self.repo, "for-each-ref")

    def _assert_no_turn(self):
        self.assertEqual(self.stage_adapter.turns, 0)
        self.assertEqual(self.sparring_adapter.start_calls + self.sparring_adapter.resume_calls, [])

    def _assert_converted(self, result, sha: str, pause: str = PUSH_AUTHORIZATION_REQUIRED):
        self.assertIsNone(result.awaiting, self.reports)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S1], self.reports)
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, sha)
        (converted,) = self._plan_state().legacy_push_pause_converted
        self.assertEqual((converted["stage"], converted["sha"], converted["pause"]), (S1, sha, pause))
        self.assertTrue(converted["at"])
        self.assertTrue(any(LEGACY_PUSH_PAUSE_CONVERTED in line for line in self.reports), self.reports)
        self._assert_no_turn()
        self.assertNothingPushed()

    def _assert_stays(self, result, condition: int):
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.accepted, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        self.assertTrue(
            any(f"condition {condition} (" in line for line in self.reports), (condition, self.reports)
        )
        self.assertEqual(self._plan_state().legacy_push_pause_converted, ())
        self._assert_no_turn()
        self.assertNothingPushed()


class LegacyPushPauseConversionTests(_LegacyCase):
    def test_a_pause_meeting_every_condition_converts_and_is_accepted_locally(self):
        sha = self._legacy_push_pause()
        recorder = GitRecorder(self.repo.parent)
        with recorder.active():
            result = self._resume_plain()
        self._assert_converted(result, sha)
        self.assertEqual(recorder.remote_commands(), [])
        # The converted run carries on as any run does: the next stage runs
        # the ordinary implementation-first loop.
        result = self._resume(_Implementer(self.repo), _SparringAdapter([READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def _stays_paused(self, damage, condition: int):
        sha = self._legacy_push_pause()
        damage(sha)
        before = self._engine_state()
        result = self._resume_plain()
        self._assert_stays(result, condition)
        self.assertEqual(result.awaiting.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertEqual(self._engine_state(), before)
        return result

    def test_a_promoted_obligation_stays_paused(self):
        def promote(_sha):
            state = self._plan_state()
            state.record_deferred(_obligation(S1).promote())
            state.save(self.state_path)

        self._stays_paused(promote, 4)

    def test_a_moved_head_stays_paused(self):
        def advance(_sha):
            (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
            _run_git(self.repo, "add", "later.txt")
            _run_git(self.repo, "commit", "-q", "-m", "later")

        self._stays_paused(advance, 2)

    def test_a_non_ready_verdict_stays_paused(self):
        def send_back(_sha):
            stage = self._stage(S1)
            text = stage.read_sparring().replace("- Action: `READY`", "- Action: `SEND_BACK`")
            stage.write_sparring(text)

        self._stays_paused(send_back, 2)

    def test_a_dirty_tree_stays_paused(self):
        self._stays_paused(lambda _sha: (self.repo / "stray.txt").write_text("x\n", encoding="utf-8"), 3)

    def test_an_edited_plan_stays_paused(self):
        sha = self._legacy_push_pause()
        self.plan_path.write_text(
            self.plan_path.read_text(encoding="utf-8").replace("Lay the groundwork.", "Do something else."),
            encoding="utf-8",
        )
        before = self._engine_state()
        with self.assertRaises(PlanError) as ctx:
            self._resume_plain()
        self.assertIn("condition 3 (", str(ctx.exception))
        self.assertIn(PUSH_AUTHORIZATION_REQUIRED, str(ctx.exception))
        self.assertEqual(self._engine_state(), before)
        self.assertEqual(self._plan_state().awaiting.candidate_sha, sha)
        self._assert_no_turn()

    def _with_an_unanswered_check(self, _sha):
        stage = self._stage(S1)
        gate = HumanGate.from_dict(human_gate())
        stage.write_sparring(stage.read_sparring() + "\n" + "\n".join(_human_gate_lines(gate)) + "\n")

    def test_an_unanswered_human_check_stays_paused(self):
        self._stays_paused(self._with_an_unanswered_check, 6)

    def test_an_unreadable_human_gate_stays_paused(self):
        # read_recorded_outcome reads a malformed gate as absent; the
        # migration must not take that as "no checks".
        def corrupt(sha):
            self._with_an_unanswered_check(sha)
            stage = self._stage(S1)
            stage.write_sparring(_corrupt_after(stage.read_sparring(), HUMAN_GATE_MARKER))
            self.assertIsNone(read_recorded_outcome(stage).human_gate)

        self._stays_paused(corrupt, 6)

    def test_positive_control_the_same_check_answered_pass_converts(self):
        sha = self._legacy_push_pause()
        self._with_an_unanswered_check(sha)
        record_human_evidence(
            self._stage(S1),
            "- Pass — Install the debug build on a real Android device and open the widget. · check `android-device`",
        )
        self._assert_converted(self._resume_plain(), sha)

    def test_positive_control_with_the_conditions_disabled_a_promoted_obligation_would_be_accepted(self):
        # The same damage as test_a_promoted_obligation_stays_paused, with
        # the L-D1 checks disabled in this test only: the acceptance gate
        # alone would accept it, so the condition is what keeps the pause.
        sha = self._legacy_push_pause()
        state = self._plan_state()
        state.record_deferred(_obligation(S1).promote())
        state.save(self.state_path)
        with mock.patch.object(plan_module, "_legacy_ready_failures", return_value=[]):
            result = self._resume_plain()
        self.assertEqual(self._stage(S1).read_state().candidate_sha, sha)
        self.assertIs(self._stage(S1).read_state().status, StageStatus.ACCEPTED)
        # ... and the ordinary post-READY sequence then stops for it.
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)

    def test_the_retired_flags_never_convert(self):
        sha = self._legacy_push_pause()
        before = self._engine_state()
        for kwargs in ({"allow_push_candidate": sha}, {"allow_push_for_run": True}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}), self.assertRaises(PlanError):
                self._resume(self.stage_adapter, self.sparring_adapter, **kwargs)
        self.assertEqual(self._engine_state(), before)
        self._assert_no_turn()


class ConversionSequenceTests(_ManifestRepoTestCase):
    """The ordinary post-READY sequence runs after a conversion."""

    def setUp(self):
        super().setUp()
        self.reports: list[str] = []

    def _paused(self, verdict: str = READY) -> str:
        _interrupted_at_freeze(
            lambda: self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([verdict]))
        )
        sha = _head_sha(self.repo)
        state = self._plan_state()
        state.status = PlanRunStatus.PAUSED
        state.awaiting = _push_pause(S3C, sha)
        state.save(self.state_path)
        return sha

    def test_a_pause_followed_by_a_gate_converts_then_stops_at_the_gate(self):
        self.write_manifest(gated_payload())
        sha = self._paused()
        stage_adapter = _StageAdapter(self.repo, commit=True)
        result = self._resume(stage_adapter, _SparringAdapter([]), report=self.reports.append)
        self.assertEqual(self._stage(S3C).read_state().status, StageStatus.ACCEPTED)
        self.assertEqual(self._stage(S3C).read_state().candidate_sha, sha)
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.BEFORE_STAGE)
        self.assertFalse(self._stage(S3D).directory.exists())
        self.assertEqual(len(stage_adapter.start_calls) + len(stage_adapter.resume_calls), 0)
        self.assertEqual(self._plan_state().legacy_push_pause_converted[0]["sha"], sha)

    def test_a_recorded_deferral_converts_and_the_obligation_is_in_the_ledger(self):
        sha = self._paused(READY_WITH_DEFERRAL)
        result = self._resume(
            _StageAdapter(self.repo, commit=True), _SparringAdapter([]),
            stop_after_stage=S3C, report=self.reports.append,
        )
        self.assertEqual(self._stage(S3C).read_state().candidate_sha, sha)
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S3C])
        (owed,) = self._plan_state().deferred_human_checks
        self.assertEqual(owed.stage_id, S3C)
        self.assertFalse(owed.resolved)

    def _deferral_damaged(self, damage) -> None:
        self._paused(READY_WITH_DEFERRAL)
        stage = self._stage(S3C)
        stage.write_sparring(damage(stage.read_sparring()))
        before = _files(self.sparring_dir)
        result = self._resume(
            _StageAdapter(self.repo, commit=True), _SparringAdapter([]),
            stop_after_stage=S3C, report=self.reports.append,
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertTrue(any("condition 5 (" in line for line in self.reports), self.reports)
        self.assertIs(self._stage(S3C).read_state().status, StageStatus.WORKING)
        self.assertEqual(self._plan_state().deferred_human_checks, ())
        self.assertEqual(_files(self.sparring_dir), before)

    def test_an_unreadable_deferred_gate_stays_paused(self):
        # Positive control: test_a_recorded_deferral_converts_and_the_
        # obligation_is_in_the_ledger, the same verdict undamaged.
        def corrupt(text):
            damaged = _corrupt_after(text, DEFERRED_GATE_MARKER)
            return damaged

        self._deferral_damaged(corrupt)

    def test_a_deferred_gate_without_an_instance_stays_paused(self):
        def drop_instance(text):
            at = text.index(DEFERRED_GATE_MARKER)
            start = text.index("```json", at) + len("```json")
            end = text.index("```", start)
            payload = json.loads(text[start:end])
            _drop_key(payload, "instance_id")
            return text[:start] + "\n" + json.dumps(payload, indent=2) + "\n" + text[end:]

        self._deferral_damaged(drop_instance)


class SiblingConversionTests(_ManifestRepoTestCase):
    def setUp(self):
        super().setUp()
        self.reports: list[str] = []
        self.sibling = _make_repo(self.repo.parent, "sib", "feature/x")
        payload = manifest_payload([
            _stage_entry(S3C, "Stage 3C", "Cloud schema", "Change both.",
                         repositories=[{"name": "sib", "path": str(self.sibling), "branch": "feature/x",
                                        "candidate_sha": None}]),
            _stage_entry(S3D, "Stage 3D", "Snapshot v2", "Emit version 2."),
        ])
        self.write_manifest(payload)
        _interrupted_at_freeze(
            lambda: self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]))
        )
        self.sha = _head_sha(self.repo)
        state = self._plan_state()
        state.status = PlanRunStatus.PAUSED
        state.awaiting = _push_pause(S3C, self.sha)
        state.save(self.state_path)

    def _resume_plain(self):
        return self._resume(
            _StageAdapter(self.repo, commit=True), _SparringAdapter([]),
            stop_after_stage=S3C, report=self.reports.append,
        )

    def test_a_moved_sibling_stays_paused(self):
        (self.sibling / "moved.txt").write_text("moved\n", encoding="utf-8")
        _run_git(self.sibling, "add", "moved.txt")
        _run_git(self.sibling, "commit", "-q", "-m", "moved")
        before = _files(self.sparring_dir)
        result = self._resume_plain()
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.kind, PUSH_AUTHORIZATION_REQUIRED)
        self.assertTrue(any("condition 3 (" in line and "sib" in line for line in self.reports), self.reports)
        self.assertIs(self._stage(S3C).read_state().status, StageStatus.WORKING)
        self.assertEqual(_files(self.sparring_dir), before)

    def test_positive_control_the_unmoved_sibling_converts(self):
        result = self._resume_plain()
        s3c = self._stage(S3C).read_state()
        self.assertIs(s3c.status, StageStatus.ACCEPTED)
        self.assertEqual(s3c.repositories[0].candidate_sha, _head_sha(self.sibling))
        self.assertEqual([stage_id for stage_id, _ in result.accepted], [S3C])


class IncidentPauseTests(_LegacyCase):
    """L-D2: recorded incident pauses stay until a person answers them, and
    the answer is never acceptance (I-L3)."""

    def _movement(self, observed: str) -> UnauthorizedRemoteMovement:
        return UnauthorizedRemoteMovement(
            stage_id=S1, remote="origin", url=str(self.remote), ref="refs/heads/feature/x",
            expected_tip=self.remote_before, observed_tip=observed,
        )

    def test_a_movement_pause_stays_until_answered_and_the_answer_accepts_nothing(self):
        sha = self._ready_not_accepted()
        # As an older engine recorded it (the push it observed is not
        # repeated here: a run reads no remote, so only the record matters).
        self._pause(self._movement(sha))
        for _ in range(2):
            result = self._resume_plain()
            self.assertEqual(result.awaiting.kind, UNAUTHORIZED_REMOTE_MOVEMENT)
            self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)
        stage_before = (self._stage(S1).directory / STATE_FILENAME).read_bytes()

        acknowledge_remote_movement(self.sparring_dir, run_key=self.run_key, observed=sha, by="tester")

        # The answer clears only the pause: nothing is accepted by it.
        state = self._plan_state()
        self.assertIsNone(state.awaiting)
        self.assertEqual(state.legacy_push_pause_converted, ())
        self.assertEqual((self._stage(S1).directory / STATE_FILENAME).read_bytes(), stage_before)
        # Raised after READY, it then goes through the L-D1 conditions.
        result = self._resume_plain()
        self._assert_converted(result, sha, pause=MOVEMENT_ACKNOWLEDGED)

    def _acknowledged_after_ready(self, observed: str | None) -> str:
        """READY over a commit, then a recorded movement pause whose observed
        tip is ``observed`` (another commit, or ``None``: the ref was gone),
        acknowledged by a person."""

        sha = self._ready_not_accepted()
        movement = UnauthorizedRemoteMovement(
            stage_id=S1, remote="origin", url=str(self.remote), ref="refs/heads/feature/x",
            expected_tip=self.remote_before, observed_tip=observed, candidate_sha=sha,
        )
        self._pause(movement)
        acknowledge_remote_movement(
            self.sparring_dir, run_key=self.run_key, observed=observed or "absent", by="tester"
        )
        (answered,) = self._plan_state().answered_incidents
        self.assertEqual((answered["stage"], answered["candidate"], answered["after_ready"]), (S1, sha, "true"))
        return sha

    def test_an_answered_pause_after_ready_converts_whatever_tip_it_observed(self):
        for observed in ("other", None):
            with self.subTest(observed=observed):
                self.tearDown()
                self.setUp()
                tip = self.remote_before if observed == "other" else None
                sha = self._acknowledged_after_ready(tip)
                self.assertNotEqual(tip, sha)
                self._assert_converted(self._resume_plain(), sha, pause=MOVEMENT_ACKNOWLEDGED)

    def _with_unanswered_check_after_ack(self, observed: str | None) -> str:
        sha = self._acknowledged_after_ready(observed)
        stage = self._stage(S1)
        gate = HumanGate.from_dict(human_gate())
        stage.write_sparring(stage.read_sparring() + "\n" + "\n".join(_human_gate_lines(gate)) + "\n")
        return sha

    def test_a_failing_condition_after_an_answer_with_another_tip_leaves_the_stage_unaccepted(self):
        for observed in ("other", None):
            with self.subTest(observed=observed):
                self.tearDown()
                self.setUp()
                self._with_unanswered_check_after_ack(self.remote_before if observed else None)
                before = self._engine_state()
                result = self._resume_plain()
                self._assert_stays(result, 6)
                self.assertEqual(self._engine_state(), before)

    def test_positive_control_without_the_routing_the_ordinary_path_would_accept_it(self):
        # The same answered pause and unanswered check, with the post-READY
        # routing disabled in this test only: the ordinary recorded-READY path
        # accepts the stage, so the routing is what holds the conditions.
        sha = self._with_unanswered_check_after_ack(self.remote_before)
        with mock.patch.object(plan_module, "_cleared_incident_after_ready", return_value=None):
            self._resume_plain()
        s1 = self._stage(S1).read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertEqual(s1.candidate_sha, sha)

    def test_an_answered_pause_raised_mid_loop_resumes_the_stage_normally(self):
        # NEEDS_YOU recorded when the pause is answered: not after READY.
        self._start(_Implementer(self.repo), _SparringAdapter([NEEDS_YOU]), stop_after_stage=S1)
        sha = _head_sha(self.repo)
        self._pause(self._movement(sha))
        acknowledge_remote_movement(self.sparring_dir, run_key=self.run_key, observed=sha, by="tester")
        (answered,) = self._plan_state().answered_incidents
        self.assertEqual(answered["after_ready"], "false")
        result = self._resume_plain()
        # The stage's own recorded NEEDS_YOU is adopted, as on any resume.
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(self._plan_state().legacy_push_pause_converted, ())
        self.assertIs(self._stage(S1).read_state().status, StageStatus.WORKING)

    def test_an_answered_pause_after_ready_whose_conditions_fail_accepts_nothing(self):
        sha = self._ready_not_accepted()
        self._pause(PushedCandidateUnauthorized(
            stage_id=S1, candidate_sha=sha, remote="origin", url=str(self.remote),
            ref="refs/heads/feature/x", observed_tip=sha,
        ))
        result = self._resume_plain()
        self.assertEqual(result.awaiting.kind, PUSHED_CANDIDATE_UNAUTHORIZED)
        authorize_pushed_candidate(self.sparring_dir, run_key=self.run_key, candidate_sha=sha, by="tester")
        (self.repo / "stray.txt").write_text("x\n", encoding="utf-8")
        before = self._engine_state()
        result = self._resume_plain()
        self._assert_stays(result, 3)
        self.assertEqual(self._engine_state(), before)
        # Positive control: the tree cleaned, the same resume converts.
        (self.repo / "stray.txt").unlink()
        self._assert_converted(self._resume_plain(), sha, pause="authorized_without_push")
