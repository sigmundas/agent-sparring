import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import conftest_path  # noqa: F401
from png_fixture import write_quadrants

from agent_sparring.config import ProjectConfigError, VisualReviewConfig, parse_project_config
from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.next_turn import capture_candidate
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import NEXT_TURN_SPARRING, Stage
from agent_sparring.visual_capture import (
    RECORD_FILENAME,
    VisualCaptureError,
    evidence_root,
    load_current_evidence,
    run_capture,
)

# A stand-in for a repository's screenshot command. It is deliberately a
# separate process driven only by the documented environment variables, so
# these tests exercise the same contract a Playwright or Qt script would.
CAPTURE_SCRIPT = textwrap.dedent(
    """
    import json, os, shutil, sys, time
    from pathlib import Path

    mode, source, calls = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    with calls.open("a") as log:
        log.write(os.environ["SPARRING_CANDIDATE_SHA"] + "\\n")
    out = Path(os.environ["SPARRING_EVIDENCE_DIR"])
    manifest_path = Path(os.environ["SPARRING_EVIDENCE_MANIFEST"])
    repo = Path(os.environ["SPARRING_REPO_ROOT"])

    def shot(path="desktop.png", status="captured", reference="docs/ref.png"):
        return {"id": "panel-desktop", "path": path,
                "viewport": {"width": 64, "height": 64},
                "status": status, "reference": reference}

    shots = [shot()]
    if mode == "exit":
        print("renderer crashed"); sys.exit(3)
    if mode == "sleep":
        time.sleep(30)
    if mode == "dirty":
        (repo / "f.txt").write_text("rewritten by capture\\n")
    if mode not in ("missing", "symlink", "inner-symlink"):
        shutil.copy(source / "good.png", out / "desktop.png")
    if mode == "corrupt":
        (out / "desktop.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\nnot really")
    if mode == "symlink":
        os.symlink(source / "good.png", out / "desktop.png")
    if mode == "inner-symlink":
        shutil.copy(source / "good.png", out / "other.png")
        os.symlink(out / "other.png", out / "desktop.png")
    if mode == "escape":
        shots = [shot(path="../desktop.png")]
    if mode == "all-failed":
        shots = [shot(path=None, status="failed")]
    if mode == "ignored-reference":
        shots = [shot(reference="build/ref.png")]
    if mode == "fifo":
        os.mkfifo(manifest_path)
    if mode not in ("no-manifest", "fifo"):
        manifest_path.write_text(json.dumps({"version": 1, "screenshots": shots}))
    """
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


class _StageAdapter:
    def __init__(self):
        self.calls = 0

    def start(self, prompt):
        self.calls += 1
        return StageAgentResult(session_id="impl", text="did it", is_error=False)

    def resume(self, session_id, prompt):
        self.calls += 1
        return StageAgentResult(session_id="impl", text="did more", is_error=False)


class _SparringAdapter:
    # Visual review refuses a reviewer that cannot be shown images.
    supports_image_input = True

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0
        self.images = []

    def _verdict(self):
        action = self.actions[self.calls]
        self.calls += 1
        text = json.dumps(
            {
                "action": action,
                "summary": action,
                "needs_you_reason": None,
                "findings": action,
                "deferred": None,
                "human_gate": None,
            }
        )
        return SparringAgentResult(session_id="spar", text=text, is_error=False)

    def start(self, prompt, *, images=()):
        self.images.append(tuple(images))
        return self._verdict()

    def resume(self, session_id, prompt, *, images=()):
        self.images.append(tuple(images))
        return self._verdict()


class VisualCaptureTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".sparring/stages/\nbuild/\n", encoding="utf-8")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        write_quadrants(self.repo / "docs" / "ref.png", dict(top_left="red", top_right="blue", bottom_left="green", bottom_right="yellow"), size=64)
        write_quadrants(self.repo / "build" / "ref.png", dict(top_left="red", top_right="red", bottom_left="red", bottom_right="red"), size=64)
        _git(self.repo, "add", ".gitignore", "f.txt", "docs/ref.png")
        _git(self.repo, "commit", "-q", "-m", "base")
        _git(self.repo, "checkout", "-q", "-b", "feature/x")

        # Outside the repository: the capture tool is not candidate content.
        self.tools = base / "tools"
        self.tools.mkdir()
        (self.tools / "capture.py").write_text(CAPTURE_SCRIPT, encoding="utf-8")
        write_quadrants(self.tools / "good.png", dict(top_left="red", top_right="blue", bottom_left="green", bottom_right="yellow"), size=64)
        self.calls = self.tools / "calls.log"

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text("# Stage brief\n\nDo it.\n", encoding="utf-8")

    def config(self, mode: str, *, timeout: int = 60) -> VisualReviewConfig:
        return VisualReviewConfig(
            command=(sys.executable, str(self.tools / "capture.py"), mode, str(self.tools), str(self.calls)),
            timeout_seconds=timeout,
        )

    def capture_count(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def candidate(self):
        return capture_candidate(self.repo, self.stage, self.stage.read_state())


class RunCaptureTests(VisualCaptureTestCase):
    def test_valid_capture_is_recorded_and_bound_to_the_candidate(self):
        before = self.candidate()
        status_before = _git(self.repo, "status", "--porcelain", "--untracked-files=all")

        record = run_capture(self.repo, self.stage, self.config("ok"))

        directory = record.directory(self.stage)
        screenshot = directory / "desktop.png"
        self.assertTrue(screenshot.is_file())
        self.assertEqual(record.candidate, before)
        self.assertEqual(record.binding.candidate_sha, _git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual(
            record.binding.screenshots,
            (("desktop.png", hashlib.sha256(screenshot.read_bytes()).hexdigest()),),
        )
        self.assertEqual(
            record.binding.references,
            (("docs/ref.png", hashlib.sha256((self.repo / "docs/ref.png").read_bytes()).hexdigest()),),
        )
        self.assertEqual(record.manifest.screenshots[0].viewport.width, 64)
        # Recorded on disk, and it verifies against the repository as it is.
        on_disk = json.loads((evidence_root(self.stage) / RECORD_FILENAME).read_text())
        self.assertEqual(on_disk["capture_id"], record.capture_id)
        self.assertEqual(load_current_evidence(self.repo, self.stage), record)
        # Screenshots neither dirty the repository nor move the candidate.
        self.assertEqual(
            _git(self.repo, "status", "--porcelain", "--untracked-files=all"), status_before
        )
        self.assertEqual(self.candidate(), before)

    def test_evidence_for_an_earlier_candidate_is_stale(self):
        run_capture(self.repo, self.stage, self.config("ok"))
        (self.repo / "f.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(VisualCaptureError, "stale"):
            load_current_evidence(self.repo, self.stage)
        _git(self.repo, "commit", "-q", "-am", "next candidate")
        with self.assertRaisesRegex(VisualCaptureError, "stale"):
            load_current_evidence(self.repo, self.stage)

    def test_a_screenshot_changed_after_capture_is_refused(self):
        record = run_capture(self.repo, self.stage, self.config("ok"))
        write_quadrants(record.directory(self.stage) / "desktop.png", dict(top_left="blue", top_right="blue", bottom_left="blue", bottom_right="blue"), size=64)
        with self.assertRaisesRegex(VisualCaptureError, "changed after it was bound"):
            load_current_evidence(self.repo, self.stage)

    def test_a_screenshot_replaced_by_a_link_after_capture_is_refused(self):
        record = run_capture(self.repo, self.stage, self.config("ok"))
        directory = record.directory(self.stage)
        (directory / "desktop.png").rename(directory / "moved.png")
        (directory / "desktop.png").symlink_to(directory / "moved.png")
        with self.assertRaisesRegex(VisualCaptureError, "is a link"):
            load_current_evidence(self.repo, self.stage)

    def test_a_capture_directory_moved_out_and_linked_back_is_refused(self):
        record = run_capture(self.repo, self.stage, self.config("ok"))
        directory = record.directory(self.stage)
        outside = self.tools / "moved-capture"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(VisualCaptureError, "not a directory the engine owns"):
            load_current_evidence(self.repo, self.stage)

    def test_an_evidence_root_moved_out_and_linked_back_is_refused(self):
        run_capture(self.repo, self.stage, self.config("ok"))
        root = evidence_root(self.stage)
        outside = self.tools / "moved-root"
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(VisualCaptureError, "is a link"):
            load_current_evidence(self.repo, self.stage)

    def test_a_new_capture_discards_the_previous_one_first(self):
        first = run_capture(self.repo, self.stage, self.config("ok"))
        with self.assertRaises(VisualCaptureError):
            run_capture(self.repo, self.stage, self.config("exit"))
        self.assertFalse(first.directory(self.stage).exists())
        self.assertFalse((evidence_root(self.stage) / RECORD_FILENAME).exists())
        with self.assertRaisesRegex(VisualCaptureError, "no current visual evidence"):
            load_current_evidence(self.repo, self.stage)

    def test_rejected_captures(self):
        cases = {
            "exit": "exited with status 3(.|\n)*renderer crashed",
            "no-manifest": "missing or not a regular file",
            "missing": "not a regular file",
            "corrupt": "invalid evidence",
            "symlink": "is a link",
            "inner-symlink": "is a link",
            "escape": "must stay inside its root",
            "all-failed": "no captured screenshot",
            "ignored-reference": "git-ignored, so it is not part of the candidate",
            "dirty": "changed the candidate",
            "fifo": "not a regular file",
        }
        for mode, pattern in cases.items():
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(VisualCaptureError, pattern):
                    run_capture(self.repo, self.stage, self.config(mode))
                self.assertFalse((evidence_root(self.stage) / RECORD_FILENAME).exists())
                _git(self.repo, "checkout", "-q", "--", "f.txt")

    def test_a_capture_that_overruns_its_timeout_is_killed(self):
        with self.assertRaisesRegex(VisualCaptureError, "did not finish within 1s"):
            run_capture(self.repo, self.stage, self.config("sleep", timeout=1))
        self.assertFalse((evidence_root(self.stage) / RECORD_FILENAME).exists())

    def test_an_unignored_evidence_directory_is_refused_before_capture(self):
        (self.repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        _git(self.repo, "commit", "-q", "-am", "stop ignoring stages")
        with self.assertRaisesRegex(VisualCaptureError, "not git-ignored"):
            run_capture(self.repo, self.stage, self.config("ok"))
        self.assertEqual(self.capture_count(), 0)

    def test_an_unknown_command_is_a_capture_failure(self):
        config = VisualReviewConfig(command=(str(self.tools / "no-such-tool"),))
        with self.assertRaisesRegex(VisualCaptureError, "could not start"):
            run_capture(self.repo, self.stage, config)


class LoopCaptureTests(VisualCaptureTestCase):
    def run_loop(self, sparring, config):
        return run_unattended_loop(
            self.stage,
            self.sparring_dir,
            self.repo,
            _StageAdapter(),
            sparring,
            expected_branch="feature/x",
            visual_review=config,
        )

    def test_capture_runs_before_every_review_including_after_send_back(self):
        sparring = _SparringAdapter(["SEND_BACK", "READY"])
        result = self.run_loop(sparring, self.config("ok"))
        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(self.capture_count(), 2)
        load_current_evidence(self.repo, self.stage)

    def test_capture_failure_stops_the_loop_before_the_reviewer(self):
        sparring = _SparringAdapter(["READY"])
        with self.assertRaisesRegex(LoopError, "visual capture .* failed"):
            self.run_loop(sparring, self.config("exit"))
        self.assertEqual(sparring.calls, 0)
        state = self.stage.read_state()
        # Still owed a review of the same candidate; no verdict was recorded.
        self.assertEqual(state.next_turn, NEXT_TURN_SPARRING)
        self.assertEqual(state.next_turn_candidate, self.candidate())

    def test_a_fifo_manifest_fails_promptly_without_starting_the_reviewer(self):
        sparring = _SparringAdapter(["READY"])
        started = time.monotonic()
        with self.assertRaisesRegex(LoopError, "not a regular file"):
            self.run_loop(sparring, self.config("fifo"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(sparring.calls, 0)

    def test_disabled_visual_review_captures_nothing(self):
        sparring = _SparringAdapter(["READY"])
        result = self.run_loop(sparring, None)
        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(self.capture_count(), 0)
        self.assertFalse(evidence_root(self.stage).exists())


class VisualReviewConfigTests(unittest.TestCase):
    BASE = 'project = "p"\n'

    def parse(self, table: str):
        return parse_project_config(self.BASE + table).visual_review

    def test_absent_or_disabled_is_none(self):
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse('[visual_review]\nenabled = false\ncommand = ["x"]\n'))
        self.assertIsNone(self.parse("[visual_review]\nenabled = false\n"))

    def test_enabled(self):
        config = self.parse(
            '[visual_review]\nenabled = true\ncommand = ["npm", "run", "shots"]\ntimeout_seconds = 90\n'
        )
        self.assertEqual(config, VisualReviewConfig(command=("npm", "run", "shots"), timeout_seconds=90))
        self.assertEqual(
            self.parse('[visual_review]\nenabled = true\ncommand = ["x"]\n').timeout_seconds, 300
        )

    def test_malformed(self):
        for table in (
            '[visual_review]\ncommand = ["x"]\n',
            "[visual_review]\nenabled = true\n",
            '[visual_review]\nenabled = true\ncommand = "npm run shots"\n',
            "[visual_review]\nenabled = true\ncommand = []\n",
            '[visual_review]\nenabled = true\ncommand = [""]\n',
            '[visual_review]\nenabled = true\ncommand = ["x"]\ntimeout_seconds = 0\n',
            '[visual_review]\nenabled = true\ncommand = ["x"]\ntimeout_seconds = 7200\n',
            '[visual_review]\nenabled = true\ncommand = ["x"]\ntimeout = 5\n',
            '[visual_review]\nenabled = "yes"\ncommand = ["x"]\n',
            'visual_review = "on"\n',
        ):
            with self.subTest(table=table):
                with self.assertRaises(ProjectConfigError):
                    self.parse(table)


if __name__ == "__main__":
    unittest.main()
