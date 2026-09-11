import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import main


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class CliHandoffAndSparringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        self.sparring_dir = self.repo / ".sparring"

    def test_handoff_then_record_sparring_end_to_end(self):
        exit_code = main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        self.assertEqual(exit_code, 0)

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = self.sparring_dir / "stages" / "stage-1" / "handoff.md"
        self.assertTrue(handoff_path.is_file())
        self.assertIn("did the thing", handoff_path.read_text(encoding="utf-8"))

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "record-sparring",
                "stage-1",
                "--action",
                "SEND_BACK",
                "--summary",
                "fix the widget",
            ]
        )
        self.assertEqual(exit_code, 0)
        sparring_path = self.sparring_dir / "stages" / "stage-1" / "sparring.md"
        self.assertIn("fix the widget", sparring_path.read_text(encoding="utf-8"))

    def test_handoff_honors_configured_repo_root_relative_to_project(self):
        # .sparring lives in a plain project directory; the actual git repo
        # is a sibling directory, reachable only through [repo].root in
        # project.toml. Without --repo-root, the CLI must resolve that
        # configured root relative to the project root (parent of
        # .sparring), not the process CWD.
        project_dir = Path(self._tmp.name) / "project"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = "../repo"\n', encoding="utf-8"
        )

        exit_code = main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])
        self.assertEqual(exit_code, 0)

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = sparring_dir / "stages" / "stage-1" / "handoff.md"
        content = handoff_path.read_text(encoding="utf-8")
        self.assertIn("`main`", content)
        self.assertIn("did the thing", content)

    def test_handoff_explicit_repo_root_overrides_configured_root(self):
        project_dir = Path(self._tmp.name) / "project2"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = "../nonexistent"\n', encoding="utf-8"
        )
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_handoff_missing_project_toml_uses_default_repo_root(self):
        # No project.toml at all: the default repo root (parent of
        # .sparring) must be used, and must not require --repo-root.
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)
        handoff_path = self.sparring_dir / "stages" / "stage-1" / "handoff.md"
        self.assertIn("`main`", handoff_path.read_text(encoding="utf-8"))

    def test_handoff_malformed_toml_fails_cleanly(self):
        # project.toml exists but is not valid TOML: must fail cleanly
        # (no traceback), and must NOT silently fall back to the default
        # repo root as if project.toml were absent.
        project_dir = Path(self._tmp.name) / "project3"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text("this is [ not valid toml", encoding="utf-8")
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_handoff_invalid_repo_root_type_fails_cleanly(self):
        # [repo].root has an invalid type: must fail cleanly, not silently
        # pick the default repo root nor raise an unhandled traceback.
        project_dir = Path(self._tmp.name) / "project4"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[repo]\nroot = 123\n', encoding="utf-8"
        )
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_handoff_explicit_repo_root_overrides_malformed_toml(self):
        # --repo-root must short-circuit before project.toml is even read,
        # so it works even when project.toml cannot be parsed at all.
        project_dir = Path(self._tmp.name) / "project5"
        project_dir.mkdir()
        sparring_dir = project_dir / ".sparring"
        sparring_dir.mkdir()
        (sparring_dir / "project.toml").write_text("this is [ not valid toml", encoding="utf-8")
        main(["--sparring-dir", str(sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(sparring_dir),
                "handoff",
                "stage-1",
                "--claims",
                "did the thing",
                "--repo-root",
                str(self.repo),
                "--no-check-pushed",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_handoff_missing_stage_fails(self):
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "handoff",
                "no-such-stage",
                "--claims",
                "x",
                "--repo-root",
                str(self.repo),
            ]
        )
        self.assertEqual(exit_code, 1)


class CliRunStageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.sparring_dir = self.repo / ".sparring"

    def test_dry_run_prints_prompt_without_invoking_any_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        (self.sparring_dir / "stages" / "stage-1" / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_run_stage_missing_stage_fails(self):
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "no-such-stage",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_stage_refuses_on_main_before_invoking_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        _run_git(self.repo, "checkout", "-q", "main")

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--claude-executable",
                "/nonexistent/claude-should-not-be-invoked",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_stage_unsupported_provider_fails_cleanly(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-stage",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--provider",
                "codex",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_stage_without_expected_branch_is_rejected_by_argparse(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        with self.assertRaises(SystemExit) as ctx:
            main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "run-stage",
                    "stage-1",
                    "--repo-root",
                    str(self.repo),
                    "--dry-run",
                ]
            )
        self.assertNotEqual(ctx.exception.code, 0)

    def test_run_stage_configured_claude_cli_provider_id_is_accepted(self):
        # The documented project.toml provider id ("claude-cli") must be
        # accepted, not rejected as "unsupported provider" -- proves the
        # documented provider vocabulary (see the canonical plan) matches
        # what the CLI actually accepts.
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        (self.sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[agents.stage]\nprovider = "claude-cli"\n', encoding="utf-8"
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "run-stage",
                    "stage-1",
                    "--repo-root",
                    str(self.repo),
                    "--expected-branch",
                    "feature/x",
                    "--claude-executable",
                    "/nonexistent/claude-should-not-exist",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertNotIn("unsupported stage agent provider", stderr.getvalue())
        self.assertIn("could not launch", stderr.getvalue())


class CliRunSparringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.sparring_dir = self.repo / ".sparring"

    def test_dry_run_prints_prompt_without_invoking_any_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        (self.sparring_dir / "stages" / "stage-1" / "brief.md").write_text(
            "# Stage brief: stage-1\n\n## Goal\n\nBuild the widget.\n", encoding="utf-8"
        )

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-sparring",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_run_sparring_missing_stage_fails(self):
        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-sparring",
                "no-such-stage",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--dry-run",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_sparring_unsupported_provider_fails_cleanly(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-sparring",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--provider",
                "claude-cli",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_sparring_refuses_on_detached_head_before_recording_anything(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        _run_git(self.repo, "checkout", "-q", "--detach", "HEAD")

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-sparring",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/x",
                "--codex-executable",
                "/nonexistent/codex-should-not-be-invoked",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_sparring_without_expected_branch_is_rejected_by_argparse(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        with self.assertRaises(SystemExit) as ctx:
            main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "run-sparring",
                    "stage-1",
                    "--repo-root",
                    str(self.repo),
                    "--dry-run",
                ]
            )
        self.assertNotEqual(ctx.exception.code, 0)

    def test_run_sparring_refuses_wrong_branch_before_invoking_provider(self):
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])

        exit_code = main(
            [
                "--sparring-dir",
                str(self.sparring_dir),
                "run-sparring",
                "stage-1",
                "--repo-root",
                str(self.repo),
                "--expected-branch",
                "feature/other",
                "--codex-executable",
                "/nonexistent/codex-should-not-be-invoked",
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_run_sparring_configured_codex_cli_provider_id_is_accepted(self):
        # The documented project.toml provider id ("codex-cli") must be
        # accepted, not rejected as "unsupported provider" -- proves the
        # documented provider vocabulary (see the canonical plan) matches
        # what the CLI actually accepts.
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        (self.sparring_dir / "project.toml").write_text(
            'project = "x"\n\n[agents.sparring]\nprovider = "codex-cli"\n', encoding="utf-8"
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    "run-sparring",
                    "stage-1",
                    "--repo-root",
                    str(self.repo),
                    "--expected-branch",
                    "feature/x",
                    "--codex-executable",
                    "/nonexistent/codex-should-not-be-invoked",
                ]
            )
        self.assertEqual(exit_code, 1)
        # It must have gotten past provider resolution and attempted to
        # actually launch the (nonexistent) executable, not been rejected
        # as an unsupported provider id.
        self.assertNotIn("unsupported sparring agent provider", stderr.getvalue())
        self.assertIn("could not launch", stderr.getvalue())


class CliAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.candidate_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        self.sparring_dir = self.repo / ".sparring"
        with contextlib.redirect_stdout(io.StringIO()):
            created = main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        self.assertEqual(created, 0)

    def _invoke(self, command: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--sparring-dir",
                    str(self.sparring_dir),
                    command,
                    "stage-1",
                    "--repo-root",
                    str(self.repo),
                    "--expected-branch",
                    "feature/x",
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_freeze_then_accept_end_to_end(self):
        exit_code, stdout, _stderr = self._invoke("freeze-candidate")
        self.assertEqual(exit_code, 0)
        self.assertIn(self.candidate_sha, stdout)

        exit_code, stdout, _stderr = self._invoke("accept-candidate")
        self.assertEqual(exit_code, 0)
        self.assertIn(self.candidate_sha, stdout)

    def test_accept_without_freeze_exits_nonzero(self):
        exit_code, _stdout, stderr = self._invoke("accept-candidate")
        self.assertEqual(exit_code, 1)
        self.assertIn("could not accept candidate", stderr)

    def test_accept_after_head_moves_exits_nonzero_as_stale(self):
        self.assertEqual(self._invoke("freeze-candidate")[0], 0)
        (self.repo / "extra.txt").write_text("more\n", encoding="utf-8")
        _run_git(self.repo, "add", "extra.txt")
        _run_git(self.repo, "commit", "-q", "-m", "moved on")

        exit_code, _stdout, stderr = self._invoke("accept-candidate")
        self.assertEqual(exit_code, 1)
        self.assertIn("stale", stderr)


if __name__ == "__main__":
    unittest.main()


class CliRunLoopActivityStreamTests(unittest.TestCase):
    """End to end through the real CLI, the real streaming runner and both
    real adapters, against fake ``claude``/``codex`` executables (small
    scripts printing canned structured output) -- no provider quota."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir(parents=True)
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.sparring_dir = self.repo / ".sparring"
        main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1"])
        self.stage_dir = self.sparring_dir / "stages" / "stage-1"

        self.bin = root / "bin"
        self.bin.mkdir()
        self.claude = self._script(
            "fake-claude",
            """
import json, sys, time
sid = "impl-1"
lines = [
    {"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                              "input": {"file_path": "f.txt", "old_string": "OLD-LEAK",
                                        "new_string": "NEW-LEAK"}}]}},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                              "input": {"command": "pytest -q CMD-LEAK"}}]}},
    {"type": "user", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                              "content": "OUTPUT-LEAK 3 passed"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "implemented",
     "session_id": sid, "num_turns": 3},
]
for line in lines:
    print(json.dumps(line), flush=True)
    time.sleep(0.01)
""",
        )
        self.codex = self._script(
            "fake-codex",
            """
import json, sys, time, os
args = sys.argv[1:]
out = args[args.index("-o") + 1]
counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-calls")
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
action = "SEND_BACK" if n == 0 else "READY"
verdict = {"action": action, "summary": f"verdict {n}", "needs_you_reason": None,
           "findings": "FINDINGS-LEAK", "deferred": None}
lines = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "REASONING-LEAK"}},
    {"type": "item.started", "item": {"id": "c", "type": "command_execution",
                                      "command": "git status CMD-LEAK", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "c", "type": "command_execution",
                                        "command": "git status CMD-LEAK",
                                        "aggregated_output": "OUTPUT-LEAK", "exit_code": 0,
                                        "status": "completed"}},
    {"type": "item.completed", "item": {"id": "m", "type": "agent_message",
                                        "text": json.dumps(verdict)}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
    time.sleep(0.01)
open(out, "w").write(json.dumps(verdict))
""",
        )

    def _script(self, name: str, body: str) -> Path:
        script = self.bin / f"{name}.py"
        script.write_text(body.lstrip("\n"), encoding="utf-8")
        wrapper = self.bin / name
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def _events(self) -> list[dict]:
        path = self.stage_dir / "activity.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]

    def test_run_loop_streams_provider_and_lifecycle_events(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--sparring-dir", str(self.sparring_dir),
                    "run-loop", "stage-1",
                    "--repo-root", str(self.repo),
                    "--expected-branch", "feature/x",
                    "--claude-executable", str(self.claude),
                    "--codex-executable", str(self.codex),
                ]
            )
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("outcome=READY cycles=2 send_back_count=1", stderr.getvalue())

        state = json.loads((self.stage_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["implementation_session_id"], "impl-1")
        self.assertEqual(state["sparring_session_id"], "thread-1")

        names = [f"{e['actor']}:{e['event']}" for e in self._events()]
        expected_cycle = [
            "stage:turn.started",
            "stage:session.started",
            "stage:file.changed",
            "stage:command.started",
            "stage:command.finished",
            "stage:provider.result",
            "stage:turn.finished",
            "stage:handoff.ready",
            "sparrer:sparring.started",
            "sparrer:session.started",
            "sparrer:command.started",
            "sparrer:command.finished",
            "sparrer:provider.result",
            "sparrer:verdict",
        ]
        self.assertEqual(
            names,
            ["loop:loop.started"] + expected_cycle + ["loop:loop.send_back"]
            + expected_cycle + ["loop:loop.stopped"],
        )

        events = self._events()
        sessions = [e for e in events if e["event"] == "session.started"]
        self.assertEqual([s["provider"] for s in sessions],
                         ["claude-cli", "codex-cli", "claude-cli", "codex-cli"])
        self.assertEqual((sessions[0]["session_id"], sessions[0]["model"]),
                         ("impl-1", "fake-model"))
        self.assertNotIn("model", sessions[1])
        self.assertEqual([e["path"] for e in events if e["event"] == "file.changed"],
                         ["f.txt", "f.txt"])
        self.assertEqual([e["action"] for e in events if e["event"] == "verdict"],
                         ["SEND_BACK", "READY"])
        second_turn = [e for e in events if e["event"] == "turn.started"][1]
        self.assertEqual((second_turn["resumed"], second_turn["session_id"]), (True, "impl-1"))
        second_sparring = [e for e in events if e["event"] == "sparring.started"][1]
        self.assertEqual((second_sparring["resumed"], second_sparring["session_id"]),
                         (True, "thread-1"))

        text = (self.stage_dir / "activity.jsonl").read_text(encoding="utf-8")
        for leak in ("OLD-LEAK", "NEW-LEAK", "CMD-LEAK", "OUTPUT-LEAK", "REASONING-LEAK",
                     "FINDINGS-LEAK", "3 passed", "Do the thing", "Stage brief"):
            self.assertNotIn(leak, text)
