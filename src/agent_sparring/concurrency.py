"""Prevent two top-level implementation writers for the same stage.

Per the project plan's orchestration-ownership principle: there is at most
one top-level implementation writer for a stage/worktree at a time. This is
a simple PID-checked lock file, not a distributed lock service — a stage's
directory is already 1:1 with one project's working tree, so locking the
stage directory is enough.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_FILENAME = ".implementation.lock"


class StageLockError(RuntimeError):
    """Raised when a stage is already locked by another live process."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else; treat as alive.
        return True
    except OSError:
        return False
    return True


def _read_holder_pid(lock_path: Path) -> int:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return -1
    try:
        return int(raw)
    except ValueError:
        return -1


@contextmanager
def stage_lock(stage_directory: Path) -> Iterator[None]:
    """Hold an exclusive implementation lock for ``stage_directory``.

    Raises :class:`StageLockError` if another live process already holds it.
    A lock file left behind by a process that is no longer running (a stale
    lock) is reclaimed automatically rather than blocking forever.
    """

    stage_directory = Path(stage_directory)
    stage_directory.mkdir(parents=True, exist_ok=True)
    lock_path = stage_directory / LOCK_FILENAME

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder_pid = _read_holder_pid(lock_path)
            if _pid_alive(holder_pid):
                raise StageLockError(
                    f"stage {stage_directory} is already locked by live process "
                    f"{holder_pid} ({lock_path})"
                )
            # Stale lock from a dead process: reclaim it and retry once.
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            break

    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["StageLockError", "stage_lock", "LOCK_FILENAME"]
