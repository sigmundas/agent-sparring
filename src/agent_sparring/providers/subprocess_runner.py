"""The shared streaming subprocess runner used by the provider adapters.

Both CLI adapters used to call ``subprocess.run(capture_output=True)`` and
only looked at stdout after the child had exited. This runner keeps the
same contract for the *final* result -- a ``CompletedProcess`` with the
``stdout``/``stderr`` text received and the return code, or
``TimeoutExpired``/``OSError`` exactly where ``subprocess.run`` would raise
them -- while also handing every stdout line to an ``on_line`` callback as
soon as it arrives, so an adapter can translate provider events into
observational telemetry (see :mod:`agent_sparring.activity`) while the
provider is still running.

The runner is governed by the lifetime of the provider process it launched,
not by pipe EOF. A provider may spawn descendants that inherit its stdout/
stderr handles and outlive it (a background helper, a stray server); with a
plain "read until EOF" loop such a descendant would keep the runner waiting
indefinitely after the provider had already produced its result and
exited. Here two daemon pump threads copy raw bytes from the pipes into a
queue, and the main thread consumes that queue while polling the provider.
Once the provider has exited, only a short bounded final drain follows
(:data:`FINAL_DRAIN_SECONDS`) to collect bytes the provider wrote just
before exiting; the pumps are then left to die with the interpreter if a
descendant still holds the pipe open. Bytes written *by* such a descendant
after the provider exited are not part of the provider's stream and are
not waited for.

Portability constraints (deliberate):

- ``Popen`` with an argument array, never a shell;
- reading via ``os.read`` on the pipe descriptors from plain threads, which
  returns whatever is available on every platform (no ``selectors``,
  signals, process groups or ``preexec_fn``);
- the timeout is a ``threading.Timer`` that terminates, then kills, the
  provider; the provider is always reaped (``poll``/``wait``) and, if the
  timer fired, ``subprocess.TimeoutExpired`` is raised with whatever output
  was collected.

``on_line`` is observational: an exception raised by it is swallowed and
the remaining lines still go to the callback -- a telemetry bug must never
turn a successful provider turn into a failed one.
"""

from __future__ import annotations

import codecs
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

LineSink = Callable[[str], None]

# How long, after the provider has exited, to keep collecting bytes that
# were already written to its pipes. Long enough for the pumps to deliver
# a result line flushed immediately before exit; short enough that a
# descendant holding the pipe open never stalls orchestration.
FINAL_DRAIN_SECONDS = 1.0
_POLL_SECONDS = 0.05
_READ_SIZE = 65536

_OUT = "out"
_ERR = "err"


def _pump(fd: int, kind: str, sink: "queue.Queue[tuple[str, bytes | None]]") -> None:
    """Copy raw bytes from ``fd`` into ``sink`` until EOF or error, then
    post a ``(kind, None)`` sentinel. Runs in a daemon thread and may block
    forever in ``os.read`` if a descendant keeps the pipe open; that is
    harmless because nothing joins it."""

    try:
        while True:
            data = os.read(fd, _READ_SIZE)
            if not data:
                break
            sink.put((kind, data))
    except OSError:
        pass
    finally:
        sink.put((kind, None))


def _terminate(proc: "subprocess.Popen[bytes]") -> None:
    """Ask the provider to stop, then insist, then reap it.

    Graceful first (``terminate``), forceful only if that is ignored, and
    the child is always waited for afterwards so no zombie is left behind.
    """

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def _stop(proc: "subprocess.Popen[bytes]", fired: threading.Event) -> None:
    fired.set()
    _terminate(proc)


class _LineSplitter:
    """Decodes stdout bytes incrementally and yields complete lines."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def feed(self, data: bytes) -> list[str]:
        self._pending += self._decoder.decode(data)
        lines: list[str] = []
        while True:
            cut = self._pending.find("\n")
            if cut < 0:
                break
            lines.append(self._pending[: cut + 1])
            self._pending = self._pending[cut + 1 :]
        return lines

    def flush(self) -> str | None:
        self._pending += self._decoder.decode(b"", final=True)
        tail, self._pending = self._pending, ""
        return tail or None


def run_streaming(
    args: list[str],
    cwd: Path,
    timeout_seconds: float | None,
    on_line: LineSink | None = None,
    *,
    stdin_devnull: bool = False,
) -> "subprocess.CompletedProcess[str]":
    """Run ``args`` in ``cwd``; stream stdout lines to ``on_line``; return the
    completed process with the captured output.

    Returns promptly once the launched process has exited, even if a
    descendant still holds its stdout/stderr open (see module docstring).

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
    )
    assert proc.stdout is not None and proc.stderr is not None

    chunks: "queue.Queue[tuple[str, bytes | None]]" = queue.Queue()
    for stream, kind in ((proc.stdout, _OUT), (proc.stderr, _ERR)):
        threading.Thread(
            target=_pump, args=(stream.fileno(), kind, chunks), daemon=True
        ).start()

    timed_out = threading.Event()
    timer: threading.Timer | None = None
    if timeout_seconds is not None:
        timer = threading.Timer(timeout_seconds, _stop, args=(proc, timed_out))
        timer.daemon = True
        timer.start()

    stdout_parts: list[str] = []
    stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stderr_parts: list[str] = []
    splitter = _LineSplitter()
    eof = {_OUT: False, _ERR: False}

    def deliver(line: str) -> None:
        stdout_parts.append(line)
        if on_line is not None:
            try:
                on_line(line.rstrip("\r\n"))
            except Exception:
                # Observational callback: never lets a telemetry bug break
                # the provider invocation.
                pass

    def consume(item: tuple[str, bytes | None]) -> None:
        kind, data = item
        if data is None:
            eof[kind] = True
        elif kind == _OUT:
            for line in splitter.feed(data):
                deliver(line)
        else:
            stderr_parts.append(stderr_decoder.decode(data))

    # Set when the wait below is abandoned rather than completed: a Ctrl-C
    # (KeyboardInterrupt) above all. The distinction decides what the
    # cleanup may do, and it must be a BaseException, because
    # KeyboardInterrupt is not an Exception.
    abandoned = False
    try:
        # Phase 1: the provider is alive. Deliver output as it arrives and
        # watch for exit; if both pipes hit EOF first (the provider closed
        # its stdio but is still finishing), fall through to wait for it.
        while not (eof[_OUT] and eof[_ERR]):
            try:
                consume(chunks.get(timeout=_POLL_SECONDS))
            except queue.Empty:
                if proc.poll() is not None:
                    break

        if eof[_OUT] and eof[_ERR]:
            proc.wait()  # the timer still bounds this via terminate/kill
        else:
            # Phase 2: the provider has exited. Bounded final drain of what
            # it wrote before exiting; never wait for a descendant's EOF.
            deadline = time.monotonic() + FINAL_DRAIN_SECONDS
            while not (eof[_OUT] and eof[_ERR]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    consume(chunks.get(timeout=min(remaining, _POLL_SECONDS)))
                except queue.Empty:
                    continue
    except BaseException:
        # Interrupted (Ctrl-C) or otherwise unwinding. The provider is this
        # process's child and nothing else will ever reap it, so it must be
        # stopped here -- see the cleanup below, which used to wait for it.
        abandoned = True
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            if abandoned:
                # The unconditional `proc.wait()` this replaces is correct
                # only when the provider is on its way out. On the
                # interrupted path it is not: a Ctrl-C delivered to this
                # process alone (rather than to the terminal's whole
                # foreground group) leaves the provider running, and
                # waiting for it blocked the engine for ever -- the run
                # neither stopped nor continued, and it went on holding the
                # worktree lock. An interruption must end the turn, so the
                # provider is stopped and reaped instead.
                _terminate(proc)
            else:
                proc.wait()
        # Close only the pipes whose pumps have finished; a pump still
        # blocked on a descendant-held pipe keeps its descriptor and dies
        # with the interpreter.
        for stream, kind in ((proc.stdout, _OUT), (proc.stderr, _ERR)):
            if eof[kind]:
                try:
                    stream.close()
                except Exception:
                    pass

    tail = splitter.flush()
    if tail is not None:
        deliver(tail)
    stderr_parts.append(stderr_decoder.decode(b"", final=True))

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(
            args, timeout_seconds or 0.0, output=stdout, stderr=stderr
        )

    return subprocess.CompletedProcess(
        args=args, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


__all__ = ["FINAL_DRAIN_SECONDS", "LineSink", "run_streaming"]
