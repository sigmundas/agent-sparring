import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.providers import ProviderError, SparringAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.stage import Stage


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _verdict_text(action: str, summary: str, needs_you_reason: str | None = None) -> str:
    return json.dumps(
        {"action": action, "summary": summary, "needs_you_reason": needs_you_reason}
    )


class _FakeAdapter:
    def __init__(self, *, start_result=None, resume_result=None, raise_on_resume=None):
        self.start_calls = []
        self.resume_calls = []
        self._start_result = start_result
        self._resume_result = resume_result
        self._raise_on_resume = raise_on_resume

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        return self._start_result

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls.append((session_id, prompt))
        if self._raise_on_resume is not None:
            raise self._raise_on_resume
        return self._resume_result


class _WritingAdapter:
    """A fake provider that writes to the repo during its turn -- exactly
    what a read-only sparrer must never do."""

    def __init__(self, repo: Path, *, filename: str, commit: bool, session_id: str = "sess-1"):
        self.repo = repo
        self.filename = filename
        self.commit = commit
        self.session_id = session_id
        self.start_calls = []

    def _write(self) -> None:
        (self.repo / self.filename).write_text("written by a sparrer\n", encoding="utf-8")
        if self.commit:
            _run_git(self.repo, "add", self.filename)
            _run_git(self.repo, "commit", "-q", "-m", f"sparrer: add {self.filename}")

    def start(self, prompt: str) -> SparringAgentResult:
        self.start_calls.append(prompt)
        self._write()
        return SparringAgentResult(
            session_id=self.session_id,
            text=_verdict_text("READY", "looks fine"),
            is_error=False,
        )

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self._write()
        return SparringAgentResult(
            session_id=self.session_id,
            text=_verdict_text("READY", "looks fine"),
            is_error=False,
        )


class SparringAgentRunTests(unittest.TestCase):
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

    def test_fresh_run_starts_and_records_session_id_and_verdict(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("READY", "all good"), is_error=False
            )
        )
        run_result = run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(adapter.resume_calls, [])
        self.assertFalse(run_result.resumed)
        self.assertEqual(run_result.routing.action, RoutingAction.READY)
        self.assertEqual(run_result.routing.summary, "all good")
        self.assertIn("Do the thing.", run_result.prompt)

        self.assertEqual(self.stage.read_state().sparring_session_id, "thread-1")
        self.assertEqual(self.stage.read_sparring(), run_result.sparring)
        self.assertIn("all good", self.stage.read_sparring())

    def test_second_run_resumes_recorded_session(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("SEND_BACK", "fix x"), is_error=False
            ),
            resume_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("READY", "fixed"), is_error=False
            ),
        )
        run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        run_result = run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        self.assertTrue(run_result.resumed)
        self.assertEqual(len(adapter.resume_calls), 1)
        resumed_session_id, prompt = adapter.resume_calls[0]
        self.assertEqual(resumed_session_id, "thread-1")
        self.assertIn("Your previous sparring exchange", prompt)
        self.assertEqual(run_result.routing.action, RoutingAction.READY)

    def test_provider_error_is_wrapped(self):
        adapter = _FakeAdapter(raise_on_resume=ProviderError("boom"))
        adapter.start_calls = []  # force resume path below
        # Seed a recorded session so this run takes the resume branch.
        state = self.stage.read_state()
        state.sparring_session_id = "thread-1"
        self.stage.write_state(state)

        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )

    def test_resume_returning_a_different_session_id_is_refused(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("SEND_BACK", "fix x"), is_error=False
            ),
            resume_result=SparringAgentResult(
                session_id="thread-DIFFERENT", text=_verdict_text("READY", "?"), is_error=False
            ),
        )
        run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
        )

        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )

        # The recorded identity must not have been silently replaced.
        self.assertEqual(self.stage.read_state().sparring_session_id, "thread-1")

    def test_malformed_verdict_json_is_refused_and_nothing_is_recorded(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text="not json at all", is_error=False
            )
        )
        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )
        self.assertIsNone(self.stage.read_state().sparring_session_id)

    def test_verdict_with_unknown_action_is_refused(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1",
                text=json.dumps({"action": "MAYBE", "summary": "?", "needs_you_reason": None}),
                is_error=False,
            )
        )
        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )

    def test_read_only_enforcement_refuses_a_provider_that_commits(self):
        adapter = _WritingAdapter(self.repo, filename="sneaky.txt", commit=True)
        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )
        # Refused before being treated as a success: no session id recorded,
        # sparring.md not overwritten with the disallowed provider's verdict.
        self.assertIsNone(self.stage.read_state().sparring_session_id)
        self.assertNotIn("looks fine", self.stage.read_sparring())

    def test_read_only_enforcement_refuses_a_provider_that_dirties_the_tree(self):
        adapter = _WritingAdapter(self.repo, filename="sneaky.txt", commit=False)
        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, adapter, expected_branch="feature/x"
            )
        self.assertIsNone(self.stage.read_state().sparring_session_id)

    def test_refuses_on_detached_head(self):
        _run_git(self.repo, "checkout", "-q", "--detach", "HEAD")
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("READY", "ok"), is_error=False
            )
        )
        with self.assertRaises(SparringAgentRunError):
            run_sparring_agent(self.stage, self.sparring_dir, self.repo, adapter)
        self.assertEqual(adapter.start_calls, [])

    def test_expected_branch_is_optional(self):
        adapter = _FakeAdapter(
            start_result=SparringAgentResult(
                session_id="thread-1", text=_verdict_text("READY", "ok"), is_error=False
            )
        )
        run_result = run_sparring_agent(self.stage, self.sparring_dir, self.repo, adapter)
        self.assertEqual(run_result.routing.action, RoutingAction.READY)


if __name__ == "__main__":
    unittest.main()
