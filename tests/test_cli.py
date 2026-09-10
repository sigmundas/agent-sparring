import subprocess
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


if __name__ == "__main__":
    unittest.main()
