import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.concurrency import LOCK_FILENAME
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
        run_result = run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)

        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(adapter.resume_calls, [])
        self.assertFalse(run_result.resumed)
        self.assertEqual(run_result.result.session_id, "sess-1")
        self.assertIn("Do the thing.", run_result.prompt)

        self.assertEqual(self.stage.read_state().implementation_session_id, "sess-1")

    def test_second_run_resumes_recorded_session(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False),
            resume_result=StageAgentResult(session_id="sess-1", text="ok again", is_error=False),
        )
        run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)

        run_result = run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)

        self.assertTrue(run_result.resumed)
        self.assertEqual(len(adapter.resume_calls), 1)
        resumed_session_id, _prompt = adapter.resume_calls[0]
        self.assertEqual(resumed_session_id, "sess-1")

    def test_provider_error_on_resume_is_wrapped(self):
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False),
            raise_on_resume=ProviderError("boom"),
        )
        run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)

        with self.assertRaises(StageAgentRunError):
            run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)

    def test_refuses_to_run_on_main(self):
        _run_git(self.repo, "checkout", "-q", "main")
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)
        self.assertEqual(adapter.start_calls, [])

    def test_refuses_when_stage_already_locked_by_live_process(self):
        (self.stage.directory / LOCK_FILENAME).write_text(str(os.getpid()), encoding="utf-8")
        adapter = _FakeAdapter(
            start_result=StageAgentResult(session_id="sess-1", text="ok", is_error=False)
        )
        with self.assertRaises(StageAgentRunError):
            run_stage_agent(self.stage, self.sparring_dir, self.repo, adapter)
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


if __name__ == "__main__":
    unittest.main()
