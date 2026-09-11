import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.providers.subprocess_runner import FINAL_DRAIN_SECONDS, run_streaming


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

    def _kill_pid_file(self, pid_file: Path) -> None:
        """Best-effort cleanup of a descendant whose pid the fake provider
        recorded, so the test never leaks it."""

        if not pid_file.is_file():
            return
        pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        if pid <= 0:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    @staticmethod
    def _alive(pid: int) -> bool:
        if os.name != "posix":  # pragma: no cover - liveness probe is POSIX-only
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def test_returns_promptly_when_a_descendant_keeps_the_pipes_open(self):
        # The fake provider spawns a descendant that inherits BOTH stdout
        # and stderr, prints a valid result, and exits. The descendant
        # sleeps far longer than any acceptable runner return time, so a
        # runner that waited for pipe EOF would hang until it ended.
        pid_file = self.cwd / "descendant.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "print('{\"type\": \"result\", \"ok\": true}', flush=True)\n"
            "sys.stderr.write('provider stderr\\n'); sys.stderr.flush()\n"
        )
        self.addCleanup(self._kill_pid_file, pid_file)
        lines: list[str] = []

        started = time.monotonic()
        result = run_streaming(_python(code), self.cwd, 20, lines.append)
        elapsed = time.monotonic() - started

        # Returned soon after the provider exited: bounded by the final
        # drain window plus slack, nowhere near the descendant's lifetime.
        self.assertLess(elapsed, FINAL_DRAIN_SECONDS + 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ['{"type": "result", "ok": true}'])
        self.assertEqual(result.stdout, '{"type": "result", "ok": true}\n')
        self.assertEqual(result.stderr, "provider stderr\n")
        # The descendant really was still alive when the runner returned.
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(self._alive(pid))

    def test_timeout_is_enforced_even_when_a_descendant_holds_the_pipes(self):
        pid_file = self.cwd / "descendant2.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "print('working', flush=True)\n"
            "time.sleep(60)\n"
        )
        self.addCleanup(self._kill_pid_file, pid_file)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_streaming(_python(code), self.cwd, 0.5, None)
        self.assertLess(time.monotonic() - started, 20)

    def test_trailing_partial_line_is_delivered(self):
        code = "import sys\nsys.stdout.write('no newline at end')\n"
        lines: list[str] = []
        result = run_streaming(_python(code), self.cwd, 10, lines.append)
        self.assertEqual(lines, ["no newline at end"])
        self.assertEqual(result.stdout, "no newline at end")

    def test_crlf_lines_are_stripped_for_the_callback_but_kept_in_stdout(self):
        code = "import sys\nsys.stdout.buffer.write(b'a\\r\\nb\\r\\n')\n"
        lines: list[str] = []
        result = run_streaming(_python(code), self.cwd, 10, lines.append)
        self.assertEqual(lines, ["a", "b"])
        self.assertEqual(result.stdout, "a\r\nb\r\n")


if __name__ == "__main__":
    unittest.main()
