"""Adversarial coverage of ``resume-plan --evidence --accept-advanced-head``.

The paused attempt here is every kind of uncommitted work at once: an
unstaged tracked edit, a staged edit, a path staged and then edited again,
and an untracked file. A refusal must leave notes.md, the stage's
state.json and the plan run state byte-for-byte as they were.
"""

import contextlib
import dataclasses
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import plan as plan_module
from agent_sparring.deferred_gate import CheckOutcome
from agent_sparring.next_turn import NEXT_TURN_STAGE, NextTurnError, record_next_turn
from agent_sparring.plan import (
    DeferredAnswer,
    PlanError,
    PlanRunError,
    PlanRunState,
    resume_plan,
)
from agent_sparring.sessions import SessionError
from agent_sparring.providers import ProviderUnavailable
from agent_sparring.stage import SiblingPin, StageStatus
from test_plan import (
    NEEDS_YOU,
    READY,
    S1,
    _PlanRepoTestCase,
    _SparringAdapter,
    _StageAdapter,
    _head_sha,
    _run_git,
)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


class AcceptAdvancedHeadTests(_PlanRepoTestCase):
    def setUp(self):
        super().setUp()
        for name in ("tracked.txt", "staged.txt", "both.txt", "other.txt", "tool.sh", "gone.txt"):
            (self.repo / name).write_text(f"{name} base\n", encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "fixtures")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")

    def _write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def _pause(self, verdicts=(NEEDS_YOU, NEEDS_YOU)):
        stage_adapter = _StageAdapter(self.repo)
        original_turn = stage_adapter._turn

        def dirty_turn(session_id):
            self._write("tracked.txt", "attempt unstaged\n")
            self._write("staged.txt", "attempt staged\n")
            _run_git(self.repo, "add", "staged.txt")
            self._write("both.txt", "attempt staged\n")
            _run_git(self.repo, "add", "both.txt")
            self._write("both.txt", "attempt staged then edited\n")
            self._write("wip.txt", "attempt untracked\n")
            return original_turn(session_id)

        stage_adapter._turn = dirty_turn
        sparring_adapter = _SparringAdapter(list(verdicts))
        self._start(stage_adapter, sparring_adapter)
        pinned = self._stage(S1).read_state().next_turn_candidate.head_sha
        self.assertEqual(pinned, _head_sha(self.repo))
        return stage_adapter, sparring_adapter, pinned

    def _land(self, message: str, *paths: str, push: bool = True) -> str:
        # Commits only ``paths`` beneath the attempt; the index keeps the
        # attempt's staged work for every other path.
        _run_git(self.repo, "commit", "-q", "-m", message, "--", *paths)
        if push:
            _run_git(self.repo, "push", "-q", "origin", "feature/x")
        return _head_sha(self.repo)

    def _land_other(self) -> str:
        self._write("other.txt", "prerequisite\n")
        return self._land("prerequisite", "other.txt")

    def _snapshot(self):
        stage_dir = self._stage(S1).directory
        files = (
            stage_dir / "notes.md",
            stage_dir / "state.json",
            stage_dir / "activity.jsonl",
            self.state_path,
        )
        return tuple(path.read_bytes() if path.exists() else None for path in files)

    def _refused(self, adapters, expect: str, **kwargs) -> str:
        before = self._snapshot()
        with self.assertRaises(PlanError) as ctx:
            self._resume(*adapters, **kwargs)
        self.assertIn(expect, str(ctx.exception))
        self.assertEqual(self._snapshot(), before, "a refusal changed recorded state")
        return str(ctx.exception)

    def _accepted(self, adapters, head: str, **kwargs):
        self._resume(*adapters, evidence="prerequisite landed", accept_advanced_head=head, **kwargs)
        state = self._stage(S1).read_state()
        self.assertEqual(state.next_turn_candidate.head_sha, _head_sha(self.repo))
        self.assertIn("Accepted branch advance", self._stage(S1).read_notes())
        # The attempt is untouched, staged and unstaged alike.
        self.assertEqual((self.repo / "tracked.txt").read_text(), "attempt unstaged\n")
        self.assertEqual((self.repo / "both.txt").read_text(), "attempt staged then edited\n")
        self.assertEqual((self.repo / "wip.txt").read_text(), "attempt untracked\n")
        staged = set(_git_out(self.repo, "diff", "--cached", "--name-only").split())
        self.assertEqual(staged, {"staged.txt", "both.txt"})
        self.assertEqual(adapters[0].resume_calls, [])
        return state

    # -- succeeds -------------------------------------------------------

    def test_unrelated_prerequisite_beneath_mixed_attempt_is_accepted(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        self._accepted((stage_adapter, sparring_adapter), advanced)

    def test_abbreviated_sha_is_accepted_and_repins_the_full_sha(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        state = self._accepted((stage_adapter, sparring_adapter), advanced[:10])
        self.assertEqual(state.next_turn_candidate.head_sha, advanced)

    def test_several_prerequisites_adding_deleting_and_changing_mode_are_accepted(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        self._write("new.txt", "added by prerequisite\n")
        _run_git(self.repo, "add", "new.txt")
        self._land("add a file", "new.txt", push=False)
        _run_git(self.repo, "rm", "-q", "gone.txt")
        self._land("delete a file", "gone.txt", push=False)
        (self.repo / "tool.sh").chmod(0o755)
        advanced = self._land("make executable", "tool.sh")
        self.assertIn("100755", _git_out(self.repo, "ls-files", "-s", "tool.sh"))
        self._accepted((stage_adapter, sparring_adapter), advanced)

    # -- the named commit -----------------------------------------------

    def test_revision_expressions_and_hex_named_refs_are_not_a_named_commit(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        adapters = (stage_adapter, sparring_adapter)
        for name in ("HEAD", "feature/x", f"{advanced}~0", advanced.upper()):
            self._refused(
                adapters, "is not a commit SHA", evidence="done", accept_advanced_head=name
            )
        # A ref whose name looks like a SHA prefix resolves to HEAD by name.
        decoy = "abcdef1" if not advanced.startswith("abcdef1") else "bcdef12"
        _run_git(self.repo, "tag", decoy, advanced)
        self._refused(adapters, "not a prefix of", evidence="done", accept_advanced_head=decoy)
        self._refused(
            adapters, "could not resolve", evidence="done", accept_advanced_head="0" * 40
        )

    def test_a_commit_other_than_head_is_refused(self):
        stage_adapter, sparring_adapter, pinned = self._pause()
        first = self._land_other()
        self._write("other.txt", "second\n")
        self._land("second prerequisite", "other.txt")
        adapters = (stage_adapter, sparring_adapter)
        for sha in (first, pinned):
            self._refused(
                adapters, "not the current HEAD", evidence="done", accept_advanced_head=sha
            )

    def test_rewritten_history_is_refused(self):
        stage_adapter, sparring_adapter, pinned = self._pause()
        # Replace the pinned commit by an amended one: same parent, new id.
        self._write("other.txt", "prerequisite\n")
        _run_git(self.repo, "commit", "-q", "--amend", "-m", "rewritten", "--", "other.txt")
        rewritten = _head_sha(self.repo)
        self.assertNotEqual(rewritten, pinned)
        self._refused(
            (stage_adapter, sparring_adapter),
            "is not an ancestor",
            evidence="done",
            accept_advanced_head=rewritten,
        )

    def test_a_moved_sibling_is_refused(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        stage = self._stage(S1)
        state = stage.read_state()
        state.next_turn_candidate = dataclasses.replace(
            state.next_turn_candidate, repositories=(SiblingPin(name="lib", head_sha="1" * 40),)
        )
        stage.write_state(state)
        advanced = self._land_other()
        self._refused(
            (stage_adapter, sparring_adapter),
            "sibling repository moved",
            evidence="done",
            accept_advanced_head=advanced,
        )

    # -- the attempt must be unchanged ----------------------------------

    def test_any_change_to_the_attempt_is_refused(self):
        edits = {
            "unstaged tracked": lambda: self._write("tracked.txt", "edited\n"),
            "staged tracked": lambda: (
                self._write("staged.txt", "edited\n"),
                _run_git(self.repo, "add", "staged.txt"),
            ),
            "staged+unstaged worktree side": lambda: self._write("both.txt", "edited\n"),
            "untracked added": lambda: self._write("extra.txt", "new\n"),
            "untracked removed": lambda: (self.repo / "wip.txt").unlink(),
            "untracked modified": lambda: self._write("wip.txt", "edited\n"),
        }
        for label, edit in edits.items():
            with self.subTest(label):
                self.tearDown_and_reset()
                stage_adapter, sparring_adapter, _ = self._pause()
                advanced = self._land_other()
                edit()
                self._refused(
                    (stage_adapter, sparring_adapter),
                    "--accept-advanced-head",
                    evidence="done",
                    accept_advanced_head=advanced,
                )

    def tearDown_and_reset(self):
        # Each subTest needs its own repository and run.
        self._tmp.cleanup()
        self.setUp()

    def test_a_prerequisite_touching_an_attempt_path_is_refused(self):
        for path in ("tracked.txt", "staged.txt", "both.txt"):
            with self.subTest(path):
                self.tearDown_and_reset()
                stage_adapter, sparring_adapter, _ = self._pause()
                attempt = (self.repo / path).read_text()
                self._write(path, "prerequisite\n")
                advanced = self._land("prerequisite", path)
                self._write(path, attempt)
                if path != "tracked.txt":
                    _run_git(self.repo, "add", path)
                if path == "both.txt":
                    self._write(path, "attempt staged then edited\n")
                self._refused(
                    (stage_adapter, sparring_adapter),
                    "uncommitted attempt content",
                    evidence="done",
                    accept_advanced_head=advanced,
                )

    def test_a_commit_that_absorbs_attempt_work_is_refused(self):
        # Committing the attempt's own change is not a prerequisite landing
        # beneath it: the candidate would silently change shape.
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land("absorb attempt", "tracked.txt")
        self._refused(
            (stage_adapter, sparring_adapter),
            "something else changed as well",
            evidence="done",
            accept_advanced_head=advanced,
        )

    # -- gate context and flag combinations -----------------------------

    def test_without_evidence_or_with_next_turn_is_refused(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        adapters = (stage_adapter, sparring_adapter)
        self._refused(adapters, "pass --evidence as well", accept_advanced_head=advanced)
        self._refused(
            adapters, "pass --evidence as well", evidence="  ", accept_advanced_head=advanced
        )
        self._refused(
            adapters,
            "--next-turn",
            evidence="done",
            next_turn="sparring",
            accept_advanced_head=advanced,
        )

    def test_a_refused_flag_leaves_push_permission_and_run_state_unwritten(self):
        stage_adapter, sparring_adapter, pinned = self._pause()
        self._land_other()
        self._refused(
            (stage_adapter, sparring_adapter),
            "not the current HEAD",
            evidence="done",
            accept_advanced_head=pinned,
            allow_push_for_run=True,
        )

    def test_a_stage_not_waiting_for_review_is_refused(self):
        for label, mutate in (
            ("next_turn=stage", lambda stage: record_next_turn(stage, NEXT_TURN_STAGE)),
            ("ACCEPTED", lambda stage: _set_status(stage, StageStatus.ACCEPTED)),
        ):
            with self.subTest(label):
                # Each from a fresh pause, so neither relies on the other.
                self.tearDown_and_reset()
                stage_adapter, sparring_adapter, _ = self._pause()
                advanced = self._land_other()
                stage = self._stage(S1)
                mutate(stage)
                self.assertEqual(stage.read_state().next_turn == "sparring", label == "ACCEPTED")
                self._refused(
                    (stage_adapter, sparring_adapter),
                    "has none",
                    evidence="done",
                    accept_advanced_head=advanced,
                )

    def test_evidence_without_the_flag_is_refused_exactly_as_before(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        self._land_other()
        message = self._refused(
            (stage_adapter, sparring_adapter), "refusing to record evidence", evidence="done"
        )
        self.assertTrue(message.endswith("(--next-turn cannot re-pin a recorded candidate.)"))
        self.assertNotIn("accept-advanced-head", message)

    # -- replay and partial failure -------------------------------------

    def test_replay_after_a_successful_repin_is_refused_and_changes_nothing(self):
        stage_adapter, sparring_adapter, _ = self._pause(verdicts=(NEEDS_YOU, NEEDS_YOU))
        advanced = self._land_other()
        self._accepted((stage_adapter, sparring_adapter), advanced)
        self._refused(
            (stage_adapter, sparring_adapter),
            "still holds its pinned candidate",
            evidence="prerequisite landed",
            accept_advanced_head=advanced,
        )

    def test_a_failure_at_any_write_puts_everything_back(self):
        # The re-pin, the note and the run state land together or not at
        # all, even with a push grant recorded first in the same resume.
        real_repin = plan_module.repin_after_authorized_advance
        proofs = []

        def second_proof_refuses(*args):
            proofs.append(args)
            if len(proofs) == 2:
                raise NextTurnError("the repository changed between the two proofs")
            return real_repin(*args)

        failures = {
            "re-pin write": ("record_next_turn", OSError("disk full")),
            "note write": ("record_human_evidence", OSError("disk full")),
            "evidence_pending digest": ("_sparring_digest", OSError("disk full")),
            "second proof": ("repin_after_authorized_advance", second_proof_refuses),
        }
        for label, (name, effect) in failures.items():
            with self.subTest(label):
                self.tearDown_and_reset()
                proofs.clear()
                stage_adapter, sparring_adapter, _ = self._pause()
                advanced = self._land_other()
                before = self._snapshot()
                with mock.patch.object(plan_module, name, side_effect=effect):
                    with self.assertRaises((OSError, PlanError)):
                        self._resume(
                            stage_adapter, sparring_adapter, evidence="done",
                            accept_advanced_head=advanced, allow_push_for_run=True,
                        )
                self.assertEqual(self._snapshot(), before)
                if label == "second proof":
                    self.assertEqual(len(proofs), 2)
                # Nothing is half-applied, so the same command then succeeds.
                self._accepted((stage_adapter, sparring_adapter), advanced)

    def test_combined_deferred_answers_are_refused_before_anything_is_written(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        self._refused(
            (stage_adapter, sparring_adapter),
            "--deferred-result",
            evidence="done",
            accept_advanced_head=advanced,
            allow_push_for_run=True,
            deferred_results=(DeferredAnswer(check_ref="nope:nope", outcome=CheckOutcome.PASS),),
        )

    # -- index-only and range-wide checks -------------------------------

    def test_an_index_only_change_to_the_attempt_is_refused(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        # Restage both.txt with different bytes, then put the working tree
        # back: the candidate content is identical, what was staged is not.
        self._write("both.txt", "staged differently\n")
        _run_git(self.repo, "add", "both.txt")
        self._write("both.txt", "attempt staged then edited\n")
        self._refused(
            (stage_adapter, sparring_adapter),
            "what the attempt staged differs",
            evidence="done",
            accept_advanced_head=advanced,
        )

    def test_a_pin_without_index_evidence_is_accepted_only_with_nothing_staged(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        stage = self._stage(S1)
        state = stage.read_state()
        state.next_turn_candidate = dataclasses.replace(
            state.next_turn_candidate, index_digest=None
        )
        stage.write_state(state)
        advanced = self._land_other()
        adapters = (stage_adapter, sparring_adapter)
        self._refused(
            adapters, "cannot be shown unchanged", evidence="done", accept_advanced_head=advanced
        )
        _run_git(self.repo, "restore", "--staged", "staged.txt", "both.txt")
        self._resume(*adapters, evidence="done", accept_advanced_head=advanced)
        self.assertEqual(stage.read_state().next_turn_candidate.head_sha, advanced)

    def test_a_range_that_changes_then_restores_an_attempt_path_is_refused(self):
        stage_adapter, sparring_adapter, _ = self._pause()
        attempt = (self.repo / "tracked.txt").read_text()
        self._write("tracked.txt", "prerequisite\n")
        self._land("touch an attempt path", "tracked.txt", push=False)
        self._write("tracked.txt", "tracked.txt base\n")
        self._land("restore it", "tracked.txt", push=False)
        advanced = self._land_other()
        self._write("tracked.txt", attempt)
        self._refused(
            (stage_adapter, sparring_adapter),
            "uncommitted attempt content (tracked.txt)",
            evidence="done",
            accept_advanced_head=advanced,
        )

    def test_a_pending_review_that_raised_no_gate_is_refused(self):
        # Implementation done and the review pinned, but the reviewer never
        # ruled (here: its provider failed), so there is no NEEDS_YOU.
        stage_adapter = _StageAdapter(self.repo)
        original_turn = stage_adapter._turn

        def dirty_turn(session_id):
            self._write("tracked.txt", "attempt unstaged\n")
            return original_turn(session_id)

        stage_adapter._turn = dirty_turn

        class _Down(_SparringAdapter):
            def start(self, prompt):
                raise ProviderUnavailable("usage limit reached")

        with self.assertRaises(PlanRunError):
            self._start(stage_adapter, _Down([]))
        state = self._stage(S1).read_state()
        self.assertEqual(state.next_turn, "sparring")
        advanced = self._land_other()
        self._refused(
            (stage_adapter, _SparringAdapter([NEEDS_YOU])),
            "not stopped on one",
            evidence="anything",
            accept_advanced_head=advanced,
        )

    def test_a_run_state_save_that_fails_midway_is_put_back(self):
        # A real write failure: the evidence_pending save truncates the run
        # state to garbage and then raises.
        stage_adapter, sparring_adapter, _ = self._pause()
        advanced = self._land_other()
        before = self._snapshot()
        real_save = PlanRunState.save

        def torn_save(run_state, path):
            if run_state.evidence_pending is not None:
                Path(path).write_bytes(b"{ torn")
                raise OSError("disk full")
            return real_save(run_state, path)

        with mock.patch.object(PlanRunState, "save", torn_save):
            with self.assertRaises(OSError):
                self._resume(
                    stage_adapter, sparring_adapter, evidence="done",
                    accept_advanced_head=advanced, allow_push_for_run=True,
                )
        self.assertEqual(self._snapshot(), before)
        self._accepted((stage_adapter, sparring_adapter), advanced)

    def test_refusals_before_the_first_provider_turn_put_everything_back(self):
        refusals = {
            "unknown stop-after-stage": dict(stop_after_stage="no-such-stage"),
            "fresh session refused": dict(
                fresh_roles=("sparring",),
                patch=("agent_sparring.plan.start_fresh_sessions", SessionError("refused")),
            ),
            "loop candidate check": dict(
                patch=(
                    "agent_sparring.loop.verify_candidate",
                    NextTurnError("candidate drifted"),
                ),
            ),
            "adapter construction": dict(factory_error=RuntimeError("no provider binary")),
        }
        for label, options in refusals.items():
            with self.subTest(label):
                self.tearDown_and_reset()
                stage_adapter, sparring_adapter, _ = self._pause()
                advanced = self._land_other()
                before = self._snapshot()
                turns = len(sparring_adapter.start_calls) + len(sparring_adapter.resume_calls)
                options = dict(options)
                target = options.pop("patch", None)
                factory_error = options.pop("factory_error", None)
                with contextlib.ExitStack() as stack:
                    if target is not None:
                        stack.enter_context(mock.patch(target[0], side_effect=target[1]))
                    with self.assertRaises(Exception):
                        if factory_error is not None:
                            resume_plan(
                                self.plan_path, self.sparring_dir, self.repo,
                                mock.Mock(side_effect=factory_error),
                                expected_branch="feature/x", evidence="done",
                                accept_advanced_head=advanced, allow_push_for_run=True,
                            )
                        else:
                            self._resume(
                                stage_adapter, sparring_adapter, evidence="done",
                                accept_advanced_head=advanced, allow_push_for_run=True,
                                **options,
                            )
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(
                    len(sparring_adapter.start_calls) + len(sparring_adapter.resume_calls), turns
                )
                self._accepted((stage_adapter, sparring_adapter), advanced)

    def test_once_the_reviewer_starts_what_was_recorded_is_kept(self):
        stage_adapter, sparring_adapter, _ = self._pause(verdicts=(NEEDS_YOU,))
        advanced = self._land_other()

        class _FailsMidTurn(_SparringAdapter):
            def resume(self, session_id, prompt):
                self.resume_calls.append((session_id, prompt))
                raise ProviderUnavailable("usage limit reached")

        reviewer = _FailsMidTurn([])
        with self.assertRaises(PlanRunError):
            self._resume(
                stage_adapter, reviewer, evidence="prerequisite landed",
                accept_advanced_head=advanced,
            )
        self.assertEqual(len(reviewer.resume_calls), 1)
        self.assertEqual(self._stage(S1).read_state().next_turn_candidate.head_sha, advanced)
        self.assertIn("Accepted branch advance", self._stage(S1).read_notes())
        self.assertIsNotNone(self._plan_state().evidence_pending)

def _set_status(stage, status):
    state = stage.read_state()
    state.status = status
    stage.write_state(state)


if __name__ == "__main__":
    unittest.main()


from test_independent_review import REVIEW_STAGE, _ReviewRepoTestCase  # noqa: E402
from test_manifest import _ManifestRepoTestCase  # noqa: E402

MANIFEST_STAGE = "stage-3c-cloud-schema"


class AcceptAdvancedHeadReviewOnlyTests(_ReviewRepoTestCase):
    def test_a_review_only_stage_is_refused_with_nothing_changed(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        self.start(stage_adapter, _SparringAdapter([READY, NEEDS_YOU]))
        stage_dir = self.stage(REVIEW_STAGE).directory
        (self.repo / "prereq.txt").write_text("prerequisite\n", encoding="utf-8")
        _run_git(self.repo, "add", "prereq.txt")
        _run_git(self.repo, "commit", "-q", "-m", "prerequisite")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        files = (self.state_path, stage_dir / "state.json", stage_dir / "notes.md")
        before = [path.read_bytes() if path.exists() else None for path in files]

        with self.assertRaises(PlanError) as ctx:
            self.resume(
                stage_adapter, _SparringAdapter([READY]), evidence="done",
                accept_advanced_head=_head_sha(self.repo),
            )
        self.assertIn("has none", str(ctx.exception))
        self.assertEqual([path.read_bytes() if path.exists() else None for path in files], before)


class AcceptAdvancedHeadManifestTests(_ManifestRepoTestCase):
    def test_an_intake_manifest_run_repins_like_a_markdown_one(self):
        stage_adapter = _StageAdapter(self.repo)
        original_turn = stage_adapter._turn

        def dirty_turn(session_id):
            (self.repo / "wip.txt").write_text("attempt\n", encoding="utf-8")
            return original_turn(session_id)

        stage_adapter._turn = dirty_turn
        sparring_adapter = _SparringAdapter([NEEDS_YOU, NEEDS_YOU])
        self._start(stage_adapter, sparring_adapter)
        pinned = self._stage(MANIFEST_STAGE).read_state().next_turn_candidate.head_sha
        (self.repo / "prereq.txt").write_text("prerequisite\n", encoding="utf-8")
        _run_git(self.repo, "add", "prereq.txt")
        _run_git(self.repo, "commit", "-q", "-m", "prerequisite")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        advanced = _head_sha(self.repo)

        with self.assertRaises(PlanError) as ctx:
            self._resume(stage_adapter, sparring_adapter, evidence="done")
        self.assertIn("(--next-turn cannot re-pin a recorded candidate.)", str(ctx.exception))
        self._resume(
            stage_adapter, sparring_adapter, evidence="done", accept_advanced_head=advanced
        )

        state = self._stage(MANIFEST_STAGE).read_state()
        self.assertEqual(state.next_turn_candidate.head_sha, advanced)
        self.assertIn(f"from HEAD {pinned} to {advanced}", self._stage(MANIFEST_STAGE).read_notes())
        self.assertEqual(stage_adapter.resume_calls, [])
