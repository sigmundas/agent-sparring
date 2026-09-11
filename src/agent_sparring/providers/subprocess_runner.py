"""The shared streaming subprocess runner used by the provider adapters.

Both CLI adapters used to call ``subprocess.run(capture_output=True)`` and
only looked at stdout after the child had exited. This runner keeps the
same contract for the *final* result -- a ``CompletedProcess`` with full
``stdout``/``stderr`` text and the return code, or ``TimeoutExpired``/
``OSError`` exactly where ``subprocess.run`` would raise them -- while also
handing every stdout line to an ``on_line`` callback as soon as it arrives,
so an adapter can translate provider events into observational telemetry
(see :mod:`agent_sparring.activity`) while the provider is still running.

Portability constraints (deliberate):

- ``Popen`` with an argument array, never a shell;
- stderr is drained by a daemon thread, so a chatty child cannot deadlock
  on a full stderr pipe while the main thread reads stdout;
- the timeout is a plain ``threading.Timer`` that terminates, then kills
  the child; after the stdout stream ends the child is always waited on
  (reaped) and, if the timer fired, ``subprocess.TimeoutExpired`` is raised
  with whatever output was collected;
- no ``preexec_fn``, no signal handling, no ``selectors``: nothing here is
  Unix-only.

``on_line`` is observational: an exception raised by it is swallowed and
the remaining lines still go to the callback (unless it keeps failing, in
which case its failures keep being swallowed) -- a telemetry bug must never
turn a successful provider turn into a failed one.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import IO, Callable

LineSink = Callable[[str], None]


def _drain(stream: IO[str], chunks: list[str]) -> None:
    try:
        chunks.append(stream.read())
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _stop(proc: "subprocess.Popen[str]", fired: threading.Event) -> None:
    fired.set()
    try:
        proc.terminate()
    except Exception:
        pass
    # Give the child a moment to exit on terminate; escalate to kill.
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_streaming(
    args: list[str],
    cwd: Path,
    timeout_seconds: float | None,
    on_line: LineSink | None = None,
    *,
    stdin_devnull: bool = False,
) -> "subprocess.CompletedProcess[str]":
    """Run ``args`` in ``cwd``; stream stdout lines to ``on_line``; return the
    completed process with full captured output.

    Raises :class:`OSError` if the executable cannot be launched and
    :class:`subprocess.TimeoutExpired` if ``timeout_seconds`` elapses (the
    child is terminated/killed and reaped first).
    """

    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    assert proc.stdout is not None and proc.stderr is not None

    stderr_chunks: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_chunks), daemon=True
    )
    stderr_thread.start()

    timed_out = threading.Event()
    timer: threading.Timer | None = None
    if timeout_seconds is not None:
        timer = threading.Timer(timeout_seconds, _stop, args=(proc, timed_out))
        timer.daemon = True
        timer.start()

    stdout_parts: list[str] = []
    try:
        for line in proc.stdout:
            stdout_parts.append(line)
            if on_line is not None:
                try:
                    on_line(line.rstrip("\r\n"))
                except Exception:
                    # Observational callback: never lets a telemetry bug
                    # break the provider invocation.
                    pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
        if timer is not None:
            timer.cancel()
        stderr_thread.join()

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_chunks)

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(
            args, timeout_seconds or 0.0, output=stdout, stderr=stderr
        )

    return subprocess.CompletedProcess(
        args=args, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


__all__ = ["LineSink", "run_streaming"]
