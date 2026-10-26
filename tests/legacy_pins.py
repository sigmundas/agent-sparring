"""Legacy-shaped remote pins for fixtures.

Local-first (Plan L): the engine no longer pins, observes or reads a
remote, so ``remote_pin.pin_remote`` is gone. Tests that need the state a
pre-Plan L run left -- a :class:`~agent_sparring.remote_pin.RemotePin`
with its trusted snapshot and the remote ref's tip at pin time -- build it
here, the way ``pin_remote`` did. The tip is read by plain ``git
ls-remote`` in the test, never through the engine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_sparring import engine_git
from agent_sparring.git_context import config_values, resolved_push_url, tracking_remote
from agent_sparring.remote_pin import RemotePin, SiblingRemotePin, now

from git_recorder import REAL_GIT


def _tip(url: str, ref: str) -> str | None:
    result = subprocess.run(
        [REAL_GIT, "ls-remote", url, ref], capture_output=True, text=True, check=True
    )
    for line in result.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return None


def legacy_pin(repo_root: Path, branch: str, *, legacy: bool = False) -> RemotePin:
    """``branch``'s remote target as configured right now, pinned as a
    pre-Plan L run pinned it."""

    remote, remote_branch = tracking_remote(repo_root, branch)
    urls = config_values(repo_root, f"remote.{remote}.url")
    push_urls = config_values(repo_root, f"remote.{remote}.pushurl")
    url = urls[0] if urls else None
    push_url = push_urls[0] if push_urls else None
    ref = f"refs/heads/{remote_branch}"
    destination = resolved_push_url(repo_root, remote) if (url or push_url) else None
    return RemotePin(
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        url=url,
        push_url=push_url,
        start_tip=_tip(destination, ref) if destination else None,
        pinned_at=now(),
        legacy=legacy,
        trusted_config=engine_git.capture_snapshot(Path(repo_root)),
        resolved=destination,
    )


def legacy_sibling_pin(root: Path, branch: str) -> SiblingRemotePin:
    """A declared sibling pinned as a pre-Plan L run pinned it at start."""

    return SiblingRemotePin(path=str(Path(root).resolve()), pin=legacy_pin(Path(root), branch))
