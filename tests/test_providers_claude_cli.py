import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.providers import ProviderError
from agent_sparring.providers.claude_cli import ClaudeCliAdapter


def _fake_result(returncode: int, stdout: str, stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ClaudeCliAdapterStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_start_builds_expected_args_and_parses_session_id(self):
        captured = {}

        def runner(args, cwd, timeout_seconds):
            captured["args"] = args
            captured["cwd"] = cwd
            return _fake_result(0, '{"session_id": "abc-123", "result": "done", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.start("do the thing")

        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(result.text, "done")
        self.assertFalse(result.is_error)
        self.assertEqual(captured["cwd"], self.repo_root)
        self.assertEqual(
            captured["args"],
            [
                "claude",
                "-p",
                "do the thing",
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
            ],
        )

    def test_start_never_passes_resume_flag(self):
        captured = {}

        def runner(args, cwd, timeout_seconds):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "x", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        adapter.start("hello")
        self.assertNotIn("--resume", captured["args"])

    def test_model_and_extra_args_are_included(self):
        captured = {}

        def runner(args, cwd, timeout_seconds):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "x", "result": "", "is_error": false}')

        adapter = ClaudeCliAdapter(
            repo_root=self.repo_root,
            runner=runner,
            model="opus",
            extra_args=("--add-dir", "/tmp/extra"),
        )
        adapter.start("hello")
        self.assertIn("--model", captured["args"])
        self.assertIn("opus", captured["args"])
        self.assertIn("--add-dir", captured["args"])
        self.assertIn("/tmp/extra", captured["args"])


class ClaudeCliAdapterResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_resume_passes_session_id_and_returns_same_id(self):
        captured = {}

        def runner(args, cwd, timeout_seconds):
            captured["args"] = args
            return _fake_result(0, '{"session_id": "abc-123", "result": "ok", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.resume("abc-123", "keep going")

        self.assertEqual(result.session_id, "abc-123")
        self.assertIn("--resume", captured["args"])
        self.assertIn("abc-123", captured["args"])

    def test_resume_rejects_empty_session_id_without_invoking_runner(self):
        called = []

        def runner(args, cwd, timeout_seconds):
            called.append(True)
            return _fake_result(0, "{}")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.resume("", "keep going")
        self.assertEqual(called, [])

    def test_resume_of_unknown_session_raises_provider_error(self):
        # Verified against a real claude CLI: resuming an unknown session id
        # exits non-zero and prints a plain-text error, not JSON.
        def runner(args, cwd, timeout_seconds):
            return _fake_result(
                1, "No conversation found with session ID: 00000000-0000-0000-0000-000000000000"
            )

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError) as ctx:
            adapter.resume("00000000-0000-0000-0000-000000000000", "keep going")
        self.assertIn("No conversation found", str(ctx.exception))


class ClaudeCliAdapterParsingFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_non_json_output_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds):
            return _fake_result(0, "not json at all")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_missing_session_id_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds):
            return _fake_result(0, '{"result": "done", "is_error": false}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_json_array_output_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds):
            return _fake_result(0, "[1, 2, 3]")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_launch_failure_raises_provider_error(self):
        def runner(args, cwd, timeout_seconds):
            raise FileNotFoundError("no such executable")

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        with self.assertRaises(ProviderError):
            adapter.start("hi")

    def test_is_error_true_is_surfaced_not_raised(self):
        def runner(args, cwd, timeout_seconds):
            return _fake_result(0, '{"session_id": "x", "result": "oops", "is_error": true}')

        adapter = ClaudeCliAdapter(repo_root=self.repo_root, runner=runner)
        result = adapter.start("hi")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "oops")


if __name__ == "__main__":
    unittest.main()
