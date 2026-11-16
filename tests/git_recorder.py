"""The reusable recorder that proves I-L1 (no remote contact).

:class:`GitRecorder` puts a ``git`` wrapper first on ``PATH`` that appends
every invocation's arguments to a log, then runs the real ``git``.
``engine_git`` calls ``git`` through ``PATH``, so every engine Git command
-- and every one the providers' fakes run -- is in the log. No remote
contact is proven from that log (:meth:`GitRecorder.remote_commands`),
never by mocking ``engine_git``.

The classification here is the test's own oracle, written independently
of :func:`agent_sparring.engine_git.remote_form`: a remote form the engine
forgot would still be caught here.

:func:`make_bare` makes the local bare remote a positive control needs:
its ``post-receive`` hook leaves a marker. :func:`marker_program` makes an
``upload-pack``/``receive-pack`` stand-in that leaves a marker and then
runs the real one, so a read (``fetch``, ``ls-remote``, ``clone`` ...)
that reaches the remote is visible too.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

REAL_GIT = shutil.which("git")

#: Subcommands that contact a remote in every form.
REMOTE_SUBCOMMANDS = frozenset(
    {"push", "fetch", "pull", "ls-remote", "clone", "send-pack", "fetch-pack", "request-pull"}
)
#: ``git remote <sub>`` forms that contact one.
REMOTE_REMOTE_SUBS = frozenset({"update", "show", "prune", "set-head"})
#: Global options whose value is the next argument.
VALUE_OPTIONS = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
     "--super-prefix", "--attr-source"}
)


def git(cwd: Path, *args: str) -> str:
    """Plain ``git`` (never the engine's), for fixtures and controls."""

    result = subprocess.run([REAL_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_bare(path: Path, marker: Path) -> Path:
    """A bare repository whose ``post-receive`` hook touches ``marker``."""

    git(path.parent, "init", "-q", "--bare", str(path))
    hook = path / "hooks" / "post-receive"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
    hook.chmod(0o755)
    return path


def marker_program(path: Path, marker: Path, real: str) -> Path:
    """An executable at ``path`` that touches ``marker``, then runs
    ``git <real>`` (``upload-pack`` or ``receive-pack``) with its arguments."""

    path.write_text(f'#!/bin/sh\ntouch "{marker}"\nexec "{REAL_GIT}" {real} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


class PlantedRemotes:
    """Remote configuration planted in a repository, every program a marker.

    - ``remote.origin.url`` at a decoy (``https://example.invalid/...``),
      which ``url.ssh://decoy.invalid/.insteadOf`` rewrites to ``ssh://``;
    - ``core.sshCommand`` and ``credential.helper`` at marker scripts;
    - a second remote, ``decoy``, a real local bare repository whose
      ``remote.decoy.uploadpack`` is a marker program, so a read of it runs.

    :meth:`positive_controls` shows each planted program does run under
    plain ``git`` in the same repository.
    """

    def __init__(self, repo: Path, root: Path, *, branch: str | None = None) -> None:
        self.repo = repo
        bin_dir = root / "planted-bin"
        bin_dir.mkdir(exist_ok=True)
        self.ssh_marker = root / "MARKER-sshcommand"
        self.helper_marker = root / "MARKER-credential-helper"
        self.upload_marker = root / "MARKER-decoy-upload-pack"
        ssh = bin_dir / "evil-ssh"
        ssh.write_text(f'#!/bin/sh\ntouch "{self.ssh_marker}"\nexit 255\n', encoding="utf-8")
        ssh.chmod(0o755)
        helper = bin_dir / "evil-helper"
        helper.write_text(f'#!/bin/sh\ntouch "{self.helper_marker}"\n', encoding="utf-8")
        helper.chmod(0o755)
        self.decoy = make_bare(root / "decoy.git", root / "MARKER-decoy-receive")
        git(self.repo, "push", "-q", str(self.decoy), "HEAD:refs/heads/decoy")
        upload = marker_program(bin_dir / "decoy-upload-pack", self.upload_marker, "upload-pack")
        for key, value in (
            ("remote.origin.url", "https://example.invalid/decoy.git"),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("url.ssh://decoy.invalid/.insteadOf", "https://example.invalid/"),
            ("core.sshCommand", str(ssh)),
            ("credential.helper", f"!{helper}"),
            ("remote.decoy.url", str(self.decoy)),
            ("remote.decoy.uploadpack", str(upload)),
        ):
            git(self.repo, "config", key, value)
        if branch:
            git(self.repo, "config", f"branch.{branch}.remote", "origin")
            git(self.repo, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")

    @property
    def markers(self) -> tuple[Path, ...]:
        return (self.ssh_marker, self.helper_marker, self.upload_marker)

    def ran(self) -> list[str]:
        return [marker.name for marker in self.markers if marker.exists()]

    def positive_controls(self) -> list[str]:
        """Run each planted program through plain ``git``; the markers that
        appeared (all three, when the planting works)."""

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for args, stdin in (
            (["ls-remote", "origin"], None),
            (["ls-remote", "decoy"], None),
            (["credential", "fill"], "protocol=https\nhost=example.invalid\n\n"),
        ):
            # ``git`` through PATH, so an active recorder logs these too.
            subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                           input=stdin, env=env, check=False, timeout=60)
        return self.ran()


def refs(bare: Path) -> str:
    """Every ref of ``bare``, to compare byte for byte."""

    return git(bare.parent, "--git-dir", str(bare), "for-each-ref")


def is_remote_command(line: str) -> bool:
    """Does one logged ``git`` invocation contact a remote?"""

    words, index = line.split(), 0
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in VALUE_OPTIONS else 1
    sub = words[index] if index < len(words) else ""
    rest = words[index + 1 :]
    # The nested subcommand of ``remote``/``submodule``/``maintenance``,
    # after any options (``remote -v show``, ``submodule --quiet update``).
    nxt = next((w for w in rest if not w.startswith("-")), "")
    return (
        sub in REMOTE_SUBCOMMANDS
        or (sub == "remote" and (nxt in REMOTE_REMOTE_SUBS or (nxt == "add" and "-f" in rest)))
        or (sub == "submodule" and nxt in ("update", "add", "foreach"))
        or (sub == "archive" and any(w.startswith("--remote") for w in rest))
        or (sub == "maintenance" and nxt == "run")
        or any("://" in w or "::" in w for w in words)
    )


class GitRecorder:
    """A ``git`` wrapper first on ``PATH`` that logs every invocation."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "recorder"
        self.bin.mkdir()
        self.log = root / "git-commands.log"
        wrapper = self.bin / "git"
        wrapper.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.log}"\nexec "{REAL_GIT}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    @contextlib.contextmanager
    def active(self):
        path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        with mock.patch.dict(os.environ, {"PATH": path}):
            yield self

    def lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def clear(self) -> None:
        self.log.unlink(missing_ok=True)

    def remote_commands(self) -> list[str]:
        return [line for line in self.lines() if is_remote_command(line)]
