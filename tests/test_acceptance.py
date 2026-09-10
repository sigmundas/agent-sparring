import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.acceptance import (
    AcceptanceError,
    StaleCandidateError,
    accept_candidate,
    freeze_candidate,
)
from agent_sparring.concurrency import worktree_lock
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import Stage, StageStatus


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _head_sha(repo: Path) -> str:
    return _git_out(repo, "rev-parse", "HEAD")


def _commit_count(repo: Path) -> int:
    return int(_git_out(repo, "rev-list", "--count", "HEAD"))


class AcceptanceTests(unittest.TestCase):
    """A real repo with a real (bare, local) remote: the pushed/reachable
    check is proven the same way it is in production, not stubbed."""

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
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        self.base_sha = _head_sha(self.repo)

        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = _head_sha(self.repo)

        # The .sparring directory lives inside the repo, as it does in a
        # real project, and is deliberately left untracked here so the
        # workflow-artifact carve-out is exercised by every test below.
        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        state = self.stage.read_state()
        state.base_sha = self.base_sha
        self.stage.write_state(state)

    def _commit_more(self, filename: str = "extra.txt", *, push: bool = True) -> str:
        (self.repo / filename).write_text("more\n", encoding="utf-8")
        _run_git(self.repo, "add", filename)
        _run_git(self.repo, "commit", "-q", "-m", f"add {filename}")
        if push:
            _run_git(self.repo, "push", "-q", "origin", "feature/x")
        return _head_sha(self.repo)

    def _freeze(self):
        return freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )

    def _accept(self):
        return accept_candidate(self.stage, self.repo, expected_branch="feature/x")

    # -- freeze ---------------------------------------------------------

    def test_pushed_clean_head_can_be_frozen(self):
        result = self._freeze()
        self.assertEqual(result.candidate_sha, self.candidate_sha)
        self.assertEqual(result.branch, "feature/x")
        self.assertIn(self.candidate_sha, result.push_detail)

    def test_freeze_records_exact_full_sha_and_frozen_status(self):
        self._freeze()
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, self.candidate_sha)
        self.assertEqual(len(state.candidate_sha), 40)
        # The candidate is the exact commit, not an abbreviation, and the
        # rest of the stage's recorded state is untouched.
        self.assertEqual(state.base_sha, self.base_sha)

        persisted = json.loads(
            (self.stage.directory / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["candidate_sha"], self.candidate_sha)
        self.assertEqual(persisted["status"], "frozen")

    def test_freeze_creates_no_commit(self):
        before_head, before_count = _head_sha(self.repo), _commit_count(self.repo)
        self._freeze()
        self.assertEqual(_head_sha(self.repo), before_head)
        self.assertEqual(_commit_count(self.repo), before_count)

    def test_unpushed_candidate_cannot_be_frozen(self):
        unpushed = self._commit_more("unpushed.txt", push=False)
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn(unpushed, str(ctx.exception))
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.WORKING)
        self.assertIsNone(state.candidate_sha)

    def test_dirty_working_tree_cannot_be_frozen(self):
        (self.repo / "impl.txt").write_text("uncommitted edit\n", encoding="utf-8")
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("impl.txt", str(ctx.exception))
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.WORKING)
        self.assertIsNone(state.candidate_sha)

    def test_untracked_file_outside_sparring_dir_cannot_be_frozen(self):
        (self.repo / "stray.txt").write_text("unrepresented\n", encoding="utf-8")
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("stray.txt", str(ctx.exception))
        self.assertEqual(self.stage.read_state().status, StageStatus.WORKING)

    def test_workflow_artifacts_under_sparring_dir_do_not_block_freeze(self):
        # setUp already left .sparring/ untracked; freezing must still work,
        # since this tool rewrites those files on every stage/sparring turn.
        result = self._freeze()
        self.assertEqual(result.candidate_sha, self.candidate_sha)
        self.assertTrue(
            any(path.startswith(".sparring/") for path in result.ignored_dirty_paths),
            result.ignored_dirty_paths,
        )

    def test_freeze_refuses_wrong_branch(self):
        with self.assertRaises(AcceptanceError):
            freeze_candidate(
                self.stage, self.sparring_dir, self.repo, expected_branch="feature/other"
            )
        self.assertEqual(self.stage.read_state().status, StageStatus.WORKING)

    def test_freeze_requires_a_branch(self):
        with self.assertRaises(AcceptanceError):
            freeze_candidate(self.stage, self.sparring_dir, self.repo, expected_branch="")

    def test_freeze_fails_cleanly_on_missing_state(self):
        (self.stage.directory / "state.json").unlink()
        with self.assertRaises(AcceptanceError):
            self._freeze()

    def test_freeze_fails_cleanly_on_malformed_state(self):
        (self.stage.directory / "state.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(AcceptanceError):
            self._freeze()

    # -- acceptance -----------------------------------------------------

    def test_exact_frozen_candidate_can_be_accepted(self):
        self._freeze()
        result = self._accept()
        self.assertEqual(result.candidate_sha, self.candidate_sha)
        self.assertEqual(result.branch, "feature/x")

    def test_accepted_state_retains_the_accepted_candidate_sha(self):
        self._freeze()
        self._accept()
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.ACCEPTED)
        self.assertEqual(state.candidate_sha, self.candidate_sha)

    def test_acceptance_creates_no_commit(self):
        self._freeze()
        before_head, before_count = _head_sha(self.repo), _commit_count(self.repo)
        self._accept()
        self.assertEqual(_head_sha(self.repo), before_head)
        self.assertEqual(_commit_count(self.repo), before_count)

    def test_accept_without_freeze_is_refused(self):
        with self.assertRaises(AcceptanceError):
            self._accept()
        self.assertEqual(self.stage.read_state().status, StageStatus.WORKING)

    def test_ready_sparring_outcome_is_not_automatically_accepted(self):
        # A READY verdict recorded in sparring.md changes no lifecycle
        # state at all: acceptance stays an explicit, separate operation.
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.READY, summary="no open issues"),
            findings="checked the diff",
        )
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.WORKING)
        self.assertIsNone(state.candidate_sha)
        self.assertIn("READY", self.stage.read_sparring())

        # Even after freezing, READY alone has not accepted anything.
        self._freeze()
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

    def test_already_accepted_stage_cannot_be_re_accepted_or_re_frozen(self):
        self._freeze()
        self._accept()
        with self.assertRaises(AcceptanceError):
            self._accept()
        with self.assertRaises(AcceptanceError):
            self._freeze()
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.ACCEPTED)
        self.assertEqual(state.candidate_sha, self.candidate_sha)

    def test_accept_refuses_wrong_branch(self):
        self._freeze()
        _run_git(self.repo, "checkout", "-q", "-b", "feature/other")
        with self.assertRaises(AcceptanceError):
            self._accept()
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

    def test_accept_fails_cleanly_on_missing_state(self):
        self._freeze()
        (self.stage.directory / "state.json").unlink()
        with self.assertRaises(AcceptanceError):
            self._accept()

    def test_accept_fails_cleanly_on_malformed_candidate_sha(self):
        self._freeze()
        (self.stage.directory / "state.json").write_text(
            json.dumps({"status": "frozen", "candidate_sha": "HEAD"}), encoding="utf-8"
        )
        with self.assertRaises(AcceptanceError):
            self._accept()
        # Still not accepted, and the malformed value was not "fixed" by
        # guessing at what was meant.
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, "HEAD")

    def test_accept_fails_cleanly_on_frozen_state_with_no_candidate_sha(self):
        (self.stage.directory / "state.json").write_text(
            json.dumps({"status": "frozen"}), encoding="utf-8"
        )
        with self.assertRaises(AcceptanceError):
            self._accept()
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

    # -- stale candidates -----------------------------------------------

    def test_changing_head_after_freeze_makes_acceptance_stale(self):
        self._freeze()
        moved_sha = self._commit_more()
        self.assertNotEqual(moved_sha, self.candidate_sha)

        with self.assertRaises(StaleCandidateError) as ctx:
            self._accept()
        message = str(ctx.exception)
        self.assertIn(self.candidate_sha, message)
        self.assertIn(moved_sha, message)

    def test_stale_acceptance_never_substitutes_the_new_head(self):
        self._freeze()
        moved_sha = self._commit_more()
        with self.assertRaises(StaleCandidateError):
            self._accept()

        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, self.candidate_sha)
        self.assertNotEqual(state.candidate_sha, moved_sha)

    def test_stale_acceptance_creates_no_commit(self):
        self._freeze()
        self._commit_more()
        before_head, before_count = _head_sha(self.repo), _commit_count(self.repo)
        with self.assertRaises(StaleCandidateError):
            self._accept()
        self.assertEqual(_head_sha(self.repo), before_head)
        self.assertEqual(_commit_count(self.repo), before_count)

    def test_stale_candidate_can_be_re_frozen_and_then_accepted(self):
        self._freeze()
        moved_sha = self._commit_more()
        with self.assertRaises(StaleCandidateError):
            self._accept()

        # The changed candidate is presented again explicitly (in practice
        # after another sparring pass), and only then can be accepted.
        refreeze = self._freeze()
        self.assertEqual(refreeze.candidate_sha, moved_sha)
        result = self._accept()
        self.assertEqual(result.candidate_sha, moved_sha)
        self.assertEqual(self.stage.read_state().status, StageStatus.ACCEPTED)

    # -- same-SHA reconsideration ---------------------------------------

    def test_same_sha_may_be_reconsidered_after_evidence_changes(self):
        # SHA A is frozen and sparred; the sparrer needs a manual check.
        first = self._freeze()
        record_sparring(
            self.stage,
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="run the device check",
                needs_you_reason="device_manual_check",
            ),
            findings="cannot verify on-device behavior from here",
        )
        # Sparring by itself accepted nothing: the stage is still FROZEN.
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

        head_before_evidence = _head_sha(self.repo)
        commits_before_evidence = _commit_count(self.repo)

        # The human performs the check and records the evidence: notes and
        # a fresh sparring exchange change, the code does not.
        self.stage.write_notes(
            "# Notes: stage-1\n\n## Evidence\n\nDevice check performed, passed.\n"
        )
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.READY, summary="device check evidence accepted"),
            findings="same commit, manual check now satisfied",
        )

        # No dummy commit was needed to create a new sparring exchange.
        self.assertEqual(_head_sha(self.repo), head_before_evidence)
        self.assertEqual(_commit_count(self.repo), commits_before_evidence)

        # The identical SHA is still the frozen candidate, and its earlier
        # non-READY exchange does not disqualify it.
        second = self._freeze()
        self.assertEqual(second.candidate_sha, first.candidate_sha)
        result = self._accept()
        self.assertEqual(result.candidate_sha, first.candidate_sha)
        self.assertEqual(self.stage.read_state().candidate_sha, self.candidate_sha)

    def test_same_sha_reconsideration_requires_no_dummy_commit(self):
        before_count = _commit_count(self.repo)
        self._freeze()
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.SEND_BACK, summary="add a test"),
            findings="missing regression",
        )
        # Nothing about acceptance forces a new commit to re-present the
        # same candidate: freeze is re-runnable on the identical SHA.
        self._freeze()
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.READY, summary="satisfied"),
            findings="reconsidered on the same commit",
        )
        self._accept()
        self.assertEqual(_commit_count(self.repo), before_count)
        self.assertEqual(self.stage.read_state().candidate_sha, self.candidate_sha)


class NarrowedDirtyExemptionTests(unittest.TestCase):
    """Finding 1: the workflow-artifact exemption is an exact allowlist of
    this stage's own artifact files, never a subtree/prefix rule keyed off
    wherever the caller happened to put ``sparring_dir``."""

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
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = _head_sha(self.repo)

    def test_sparring_dir_equal_to_repo_root_cannot_hide_a_dirty_source_file(self):
        # sparring_dir == repo_root would make the old prefix-based
        # exemption compute an empty prefix, matching every path. The new
        # allowlist is derived from the stage's own directory instead, so
        # this must still block.
        stage = Stage.resolve(self.repo, "stage-1").create()
        (self.repo / "stray_source.py").write_text("x = 1\n", encoding="utf-8")

        with self.assertRaises(AcceptanceError) as ctx:
            freeze_candidate(stage, self.repo, self.repo, expected_branch="feature/x")
        self.assertIn("stray_source.py", str(ctx.exception))
        self.assertEqual(stage.read_state().status, StageStatus.WORKING)

    def test_non_workflow_file_underneath_sparring_dir_blocks_freeze(self):
        sparring_dir = self.repo / ".sparring"
        stage = Stage.resolve(sparring_dir, "stage-1").create()
        # A file dropped alongside the real artifacts, in the very same
        # stage directory, that is not one of the known artifact filenames.
        (stage.directory / "extra-not-a-workflow-file.txt").write_text(
            "not a recognized artifact\n", encoding="utf-8"
        )

        with self.assertRaises(AcceptanceError) as ctx:
            freeze_candidate(stage, sparring_dir, self.repo, expected_branch="feature/x")
        self.assertIn("extra-not-a-workflow-file.txt", str(ctx.exception))
        self.assertEqual(stage.read_state().status, StageStatus.WORKING)

    def test_rename_from_outside_sparring_dir_into_it_blocks_freeze(self):
        sparring_dir = self.repo / ".sparring"
        stage = Stage.resolve(sparring_dir, "stage-1").create()
        _run_git(self.repo, "add", ".sparring")
        _run_git(self.repo, "commit", "-q", "-m", "commit workflow artifacts")
        # Remove the template notes.md first so the destination path is free
        # for `git mv` (which refuses to overwrite an existing path); the
        # point under test is the rename itself, not this fixture detail.
        _run_git(self.repo, "rm", "-q", str(stage.directory / "notes.md"))
        _run_git(self.repo, "commit", "-q", "-m", "clear notes.md for the rename")

        # Rename a real, tracked source file into the stage's own directory
        # under a name that collides with a real artifact filename. The
        # destination alone looks like an exempt workflow file; the source
        # is a real, unrepresented change elsewhere that must still block.
        _run_git(self.repo, "mv", "impl.txt", str(stage.directory / "notes.md"))

        with self.assertRaises(AcceptanceError) as ctx:
            freeze_candidate(stage, sparring_dir, self.repo, expected_branch="feature/x")
        message = str(ctx.exception)
        self.assertIn("impl.txt", message)
        self.assertEqual(stage.read_state().status, StageStatus.WORKING)

    def test_project_toml_and_other_stage_dirs_are_not_exempt(self):
        sparring_dir = self.repo / ".sparring"
        stage = Stage.resolve(sparring_dir, "stage-1").create()
        (sparring_dir / "project.toml").write_text("project = \"x\"\n", encoding="utf-8")
        other_stage = Stage.resolve(sparring_dir, "stage-2").create()

        with self.assertRaises(AcceptanceError) as ctx:
            freeze_candidate(stage, sparring_dir, self.repo, expected_branch="feature/x")
        message = str(ctx.exception)
        self.assertIn("project.toml", message)
        # Another stage's own artifacts are not this stage's allowlist.
        self.assertIn(f"stages/{other_stage.stage_id}", message)
        self.assertEqual(stage.read_state().status, StageStatus.WORKING)

    def test_intended_stage_artifacts_still_do_not_block_freeze_or_same_sha_reconsideration(self):
        sparring_dir = self.repo / ".sparring"
        stage = Stage.resolve(sparring_dir, "stage-1").create()

        first = freeze_candidate(stage, sparring_dir, self.repo, expected_branch="feature/x")
        self.assertEqual(first.candidate_sha, self.candidate_sha)
        accept_candidate(stage, self.repo, expected_branch="feature/x")
        self.assertEqual(stage.read_state().status, StageStatus.ACCEPTED)


class AcceptanceDirtyRecheckTests(unittest.TestCase):
    """Finding 2: acceptance must not rely on HEAD alone -- a real,
    unrepresented worktree change made after a clean freeze must refuse
    acceptance even though HEAD has not moved."""

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
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = _head_sha(self.repo)

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_uncommitted_edit_to_a_real_file_after_clean_freeze_refuses_acceptance(self):
        freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )
        self.assertEqual(_head_sha(self.repo), self.candidate_sha)

        (self.repo / "impl.txt").write_text("uncommitted edit\n", encoding="utf-8")

        with self.assertRaises(AcceptanceError) as ctx:
            accept_candidate(self.stage, self.repo, expected_branch="feature/x")
        self.assertIn("impl.txt", str(ctx.exception))

        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, self.candidate_sha)
        # HEAD is unchanged -- this is not the stale-candidate path.
        self.assertEqual(_head_sha(self.repo), self.candidate_sha)

    def test_new_untracked_source_file_after_clean_freeze_refuses_acceptance(self):
        freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )
        (self.repo / "sneaky.py").write_text("y = 2\n", encoding="utf-8")

        with self.assertRaises(AcceptanceError) as ctx:
            accept_candidate(self.stage, self.repo, expected_branch="feature/x")
        self.assertIn("sneaky.py", str(ctx.exception))
        self.assertEqual(self.stage.read_state().status, StageStatus.FROZEN)

    def test_evidence_only_update_still_permits_acceptance(self):
        # The permitted case from the same-SHA reconsideration workflow:
        # only this stage's own notes.md/sparring.md change.
        freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )
        self.stage.write_notes("# Notes: stage-1\n\nDevice check performed, passed.\n")
        record_sparring(
            self.stage,
            RoutingResult(action=RoutingAction.READY, summary="evidence satisfied"),
            findings="same commit, manual check now satisfied",
        )

        result = accept_candidate(self.stage, self.repo, expected_branch="feature/x")
        self.assertEqual(result.candidate_sha, self.candidate_sha)
        self.assertEqual(self.stage.read_state().status, StageStatus.ACCEPTED)


class AcceptanceWorktreeLockTests(unittest.TestCase):
    """Finding 3: freeze/accept serialize with the existing implementation
    worktree lock rather than running concurrently with a stage-agent turn
    on the same worktree."""

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
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "impl.txt").write_text("implementation\n", encoding="utf-8")
        _run_git(self.repo, "add", "impl.txt")
        _run_git(self.repo, "commit", "-q", "-m", "candidate")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = _head_sha(self.repo)

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()

    def test_freeze_refuses_while_worktree_lock_is_held_elsewhere(self):
        with worktree_lock(self.repo):
            with self.assertRaises(AcceptanceError):
                freeze_candidate(
                    self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
                )
        self.assertEqual(self.stage.read_state().status, StageStatus.WORKING)
        self.assertIsNone(self.stage.read_state().candidate_sha)

        # Once the lock is released, freezing proceeds normally -- this was
        # a contention refusal, not a permanent one.
        result = freeze_candidate(
            self.stage, self.sparring_dir, self.repo, expected_branch="feature/x"
        )
        self.assertEqual(result.candidate_sha, self.candidate_sha)

    def test_accept_refuses_while_worktree_lock_is_held_elsewhere(self):
        freeze_candidate(self.stage, self.sparring_dir, self.repo, expected_branch="feature/x")

        with worktree_lock(self.repo):
            with self.assertRaises(AcceptanceError):
                accept_candidate(self.stage, self.repo, expected_branch="feature/x")
        state = self.stage.read_state()
        self.assertEqual(state.status, StageStatus.FROZEN)
        self.assertEqual(state.candidate_sha, self.candidate_sha)

        result = accept_candidate(self.stage, self.repo, expected_branch="feature/x")
        self.assertEqual(result.candidate_sha, self.candidate_sha)


if __name__ == "__main__":
    unittest.main()
