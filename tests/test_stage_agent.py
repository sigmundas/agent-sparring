import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.concurrency import worktree_lock
from agent_sparring.providers import ProviderError, StageAgentResult
from agent_sparring.stage import Stage
from agent_sparring.stage_agent import StageAgentRunError, run_stage_agent


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class _FakeAdapter:
    def __init__(self, *, start_result=None, resume_result=None, raise_on_resume=None):
        self.start_calls = []
        self.resume_calls = []
        self._start_result = start_result
        self._resume_result = resume_result
        self._raise_on_resume = raise_on_resume

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        return self._start_result

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        if self._raise_on_resume is not None:
            raise self._raise_on_resume
        return self._resume_result


class _CommittingAdapter:
    """A fake provider that makes a real commit during its turn."""

    def __init__(self, repo: Path, *, filename: str, session_id: str = "sess-1"):
        self.repo = repo
        self.filename = filename
        self.session_id = session_id
        self.start_calls = []
        self.resume_calls = []

    def _commit(self) -> None:
        (self.repo / self.filename).write_text("added by stage agent\n", encoding="utf-8")
        _run_git(self.repo, "add", self.filename)
        _run_git(self.repo, "commit", "-q", "-m", f"stage agent: add {self.filename}")

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        self._commit()
        return StageAgentResult(session_id=self.session_id, text="did it", is_error=False)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        self._commit()
        return StageAgentResult(session_id=self.session_id, text="did more", is_error=False)


class _BranchSwitchingAdapter:
    """A fake provider that checks out a different branch during its turn."""

    def __init__(self, repo: Path, target_branch: str, *, session_id: str = "sess-1"):
        self.repo = repo
        self.target_branch = target_branch
        self.session_id = session_id
        self.start_calls = []
        self.resume_calls = []

    def start(self, prompt: str) -> StageAgentResult:
        self.start_calls.append(prompt)
        _run_git(self.repo, "checkout", "-q", self.target_branch)
        return StageAgentResult(session_id=self.session_id, text="switched", is_error=False)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        self.resume_calls.append((session_id, prompt))
        _run_git(self.repo, "checkout", "-q", self.target_branch)
        return StageAgentResult(session_id=self.session_id, text="switched", is_error=False)


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


class StageAgentRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nDo the thing.\n", encoding="utf-8"
        )

    def test_fresh_run_starts_and_records_session_id(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        run_result = run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(adapter.resume_calls, [])
        self.assertFalse(run_result.resumed)
        self.assertEqual(run_result.result.session_id, "sess-1")
        self.assertIn("Do the thing.", run_result.prompt)
        self.assertIn("feature/x", run_result.prompt)

        self.assertEqual(self.stage.read_state().implementation_session_id, "sess-1")

    def test_second_run_resumes_recorded_session(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False),
            resume_result=StageAgentResult(session_id="sess-1", text="ok again", is_error=False),
        )
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        run_result = run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertTrue(run_result.resumed)
        self.assertEqual(len(adapter.resume_calls), 1)
        resumed_session_id, _prompt = adapter.resume_calls[0]
        self.assertEqual(resumed_session_id, "sess-1")

    def test_provider_error_on_resume_is_wrapped(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False),
            raise_on_resume=ProviderError("boom"),
        )
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )

    def test_refuses_to_run_on_main(self):
        _run_git(self.repo, "checkout", "-q", "main")
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )
        self.assertEqual(adapter.start_calls, [])

    def test_expected_branch_mismatch_refuses_without_invoking_provider(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/other"
            )
        self.assertEqual(adapter.start_calls, [])

    def test_missing_expected_branch_is_refused_up_front(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        for bad in (None, "", "   "):
            with self.assertRaises(StageAgentRunError):
                run_stage_agent(
                    self.stage, self.sparring_dir, self.repo, adapter, expected_branch=bad
                )
        self.assertEqual(adapter.start_calls, [])

    def test_resume_returning_a_different_session_id_is_refused(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False),
            resume_result=StageAgentResult(session_id="sess-DIFFERENT", text="?", is_error=False),
        )
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )

        # The recorded identity must not have been silently replaced.
        self.assertEqual(self.stage.read_state().implementation_session_id, "sess-1")

    def test_successful_run_regenerates_handoff_from_real_git_context(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(
                session_id="sess-1", text="Implemented the widget.", is_error=False
            )
        )
        run_result = run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        handoff_on_disk = self.stage.read_handoff()
        self.assertEqual(handoff_on_disk, run_result.handoff)
        self.assertIn("Implemented the widget.", handoff_on_disk)
        self.assertIn("feature/x", handoff_on_disk)
        self.assertIn("(not recorded)", handoff_on_disk)  # test/build evidence: never invented

    def test_two_stages_targeting_the_same_worktree_cannot_both_hold_ownership(self):
        other_stage = Stage.resolve(self.sparring_dir, "stage-2").create()
        (other_stage.directory / "brief.md").write_text(
            "# Stage brief: stage-2\n\n## Goal\n\nDo another thing.\n", encoding="utf-8"
        )
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-2", text="ok", is_error=False)
        )

        with worktree_lock(self.repo):
            with self.assertRaises(StageAgentRunError):
                run_stage_agent(
                    other_stage,
                    self.sparring_dir,
                    self.repo,
                    adapter,
                    expected_branch="feature/x",
                )
        self.assertEqual(adapter.start_calls, [])

    def test_first_run_captures_base_sha_and_handoff_shows_accumulated_changes(self):
        pre_run_sha = _head_sha(self.repo)
        adapter = _CommittingAdapter(self.repo, filename="widget.txt")

        run_result = run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(self.stage.read_state().base_sha, pre_run_sha)
        post_run_sha = _head_sha(self.repo)
        self.assertNotEqual(pre_run_sha, post_run_sha)

        handoff = run_result.handoff
        self.assertIn(pre_run_sha, handoff)
        self.assertIn(post_run_sha, handoff)
        self.assertIn("widget.txt", handoff)

    def test_resumed_run_does_not_move_base_sha_forward(self):
        adapter = _CommittingAdapter(self.repo, filename="widget.txt", session_id="sess-1")
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )
        first_base = self.stage.read_state().base_sha
        self.assertIsNotNone(first_base)

        resume_adapter = _CommittingAdapter(self.repo, filename="widget2.txt", session_id="sess-1")
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, resume_adapter, expected_branch="feature/x"
        )

        self.assertEqual(self.stage.read_state().base_sha, first_base)

    def test_provider_switching_branches_during_start_is_refused(self):
        adapter = _BranchSwitchingAdapter(self.repo, "main")
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )
        # Refused before being treated as a success: no session id recorded.
        self.assertIsNone(self.stage.read_state().implementation_session_id)

    def test_provider_switching_branches_during_resume_is_refused(self):
        good_adapter = _CommittingAdapter(self.repo, filename="ok.txt", session_id="sess-1")
        run_stage_agent(
            self.stage, self.sparring_dir, self.repo, good_adapter, expected_branch="feature/x"
        )

        switching_adapter = _BranchSwitchingAdapter(self.repo, "main", session_id="sess-1")
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(
                self.stage,
                self.sparring_dir,
                self.repo,
                switching_adapter,
                expected_branch="feature/x",
            )
        # The identity recorded by the prior successful run must be untouched.
        self.assertEqual(self.stage.read_state().implementation_session_id, "sess-1")


if __name__ == "__main__":
    unittest.main()
