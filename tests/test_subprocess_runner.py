import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.providers.subprocess_runner import run_streaming


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class RunStreamingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name)

    def test_lines_reach_the_callback_before_the_child_exits(self):
        # The child prints one line, then blocks until a sentinel file
        # appears. The callback, run while the child is blocked, creates
        # the sentinel -- so the line can only have arrived live, not after
        # exit (without the callback the child would never finish).
        sentinel = self.cwd / "go"
        code = (
            "import sys, time, os\n"
            "print('first', flush=True)\n"
            f"while not os.path.exists({str(sentinel)!r}): time.sleep(0.01)\n"
            "print('second', flush=True)\n"
        )
        seen: list[tuple[str, float]] = []

        def on_line(line: str) -> None:
            seen.append((line, time.monotonic()))
            if line == "first":
                sentinel.write_text("", encoding="utf-8")

        result = run_streaming(_python(code), self.cwd, 10, on_line)

        self.assertEqual([line for line, _ in seen], ["first", "second"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "first\nsecond\n")

    def test_large_stderr_does_not_deadlock_and_is_captured(self):
        # Far more than any pipe buffer, written to stderr while stdout is
        # what the main thread reads line by line.
        code = (
            "import sys\n"
            "sys.stderr.write('e' * (4 * 1024 * 1024))\n"
            "sys.stderr.flush()\n"
            "print('done')\n"
        )
        result = run_streaming(_python(code), self.cwd, 30, None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "done\n")
        self.assertEqual(len(result.stderr), 4 * 1024 * 1024)

    def test_timeout_kills_and_reaps_the_child_then_raises(self):
        code = "import time\nprint('alive', flush=True)\ntime.sleep(60)\n"
        lines: list[str] = []
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            run_streaming(_python(code), self.cwd, 0.5, lines.append)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 20)
        self.assertEqual(lines, ["alive"])
        # Output collected so far travels with the exception, as with
        # subprocess.run.
        self.assertEqual(ctx.exception.output, "alive\n")

    def test_callback_exception_does_not_break_the_invocation(self):
        code = "print('a')\nprint('b')\nprint('c')\n"
        seen: list[str] = []

        def on_line(line: str) -> None:
            seen.append(line)
            raise RuntimeError("telemetry bug")

        result = run_streaming(_python(code), self.cwd, 10, on_line)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(result.stdout, "a\nb\nc\n")

    def test_missing_executable_raises_oserror(self):
        with self.assertRaises(OSError):
            run_streaming([str(self.cwd / "no-such-binary")], self.cwd, 5, None)

    def test_nonzero_exit_and_stderr_are_reported(self):
        code = "import sys\nsys.stderr.write('bad thing\\n')\nsys.exit(3)\n"
        result = run_streaming(_python(code), self.cwd, 10, None)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "bad thing\n")

    def test_stdin_devnull_gives_the_child_an_empty_stdin(self):
        code = "import sys\nprint(repr(sys.stdin.read()))\n"
        result = run_streaming(_python(code), self.cwd, 10, None, stdin_devnull=True)
        self.assertEqual(result.stdout.strip(), "''")


if __name__ == "__main__":
    unittest.main()
