"""Prevent two top-level implementation writers for the same Git worktree.

Per the project plan's orchestration-ownership principle, the invariant is
at most one top-level implementation writer per Git worktree — not per
stage. Two different stages targeting the same worktree must contend on the
same lock; different worktrees must not block each other.

This uses the kernel's own advisory file lock (``fcntl.flock``) rather than
a hand-rolled PID-file staleness check. A PID-file approach has a real race:
one contender can read a lock file, decide its holder is dead, unlink it,
and remove a lock another contender has *since* legitimately (re)acquired.
``flock`` has no such window — a lock is tied to an open file description,
and the kernel releases it automatically when that file descriptor is
closed (including on process death), so there is nothing to unlink and no
stale-lock case to detect.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_DIR_NAME = "agent-sparring-locks"


class WorktreeLockError(RuntimeError):
    """Raised when another live process already holds the worktree's lock."""


def _lock_path(repo_root: Path) -> Path:
    """A lock file path derived from ``repo_root``'s resolved identity.

    Every stage/caller working against the same worktree resolves to the
    same path, regardless of stage id; a different worktree resolves to a
    different path. Lives outside the repo itself (in the system temp
    directory) so it is never mistaken for project content.
    """

    resolved = str(Path(repo_root).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    lock_dir = Path(tempfile.gettempdir()) / LOCK_DIR_NAME
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{digest}.lock"


@contextmanager
def worktree_lock(repo_root: Path) -> Iterator[None]:
    """Hold an exclusive implementation lock for the worktree at ``repo_root``.

    Raises :class:`WorktreeLockError` immediately (non-blocking) if another
    live process already holds it.
    """

    path = _lock_path(repo_root)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise WorktreeLockError(
                    f"worktree {repo_root} is already locked by another live "
                    f"implementation process ({path})"
                ) from exc
            raise

        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("utf-8"))
        except OSError:
            pass  # best-effort diagnostic only; the flock itself is authoritative

        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


__all__ = ["WorktreeLockError", "worktree_lock"]
