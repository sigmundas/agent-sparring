"""Generic single-repo git context gathering for handoff packets.

Shells out to plain ``git`` for one local repository. Contains no
Sporely-specific, multi-repo-workspace, or prompt-front-matter assumptions:
no repo-name registries, no GitHub URL building, no transcript digestion.
Subprocess/parse failures are raised as :class:`GitContextError` rather than
leaking raw ``CalledProcessError`` output or crashing.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def is_full_sha(value: object) -> bool:
    """Is ``value`` exactly a full 40-hex-character commit id?

    Says nothing about whether such a commit exists in any repository (use
    :func:`resolve_commit` for that) — only that the string is shaped like a
    full, unabbreviated SHA. Exposed so callers holding a *recorded* sha
    (e.g. an acceptance candidate read back from ``state.json``) can reject
    a malformed value with their own message instead of feeding garbage to
    git.
    """

    return isinstance(value, str) and _FULL_SHA_RE.match(value) is not None


class GitContextError(RuntimeError):
    """Raised when git context cannot be gathered for a repo/revision."""


@dataclass(frozen=True)
class ChangedFile:
    """One changed path. ``old_path`` is the rename/copy source (``status``
    starting with ``R``/``C``); ``None`` for every other status."""

    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class GitContext:
    """Structured git identity/status for one handoff.

    ``base_sha`` is ``None`` when no base was given (nothing to diff
    against yet). ``pushed``/``push_detail`` are ``None`` when the push
    check was skipped rather than run and failed.
    """

    branch: str
    candidate_sha: str
    base_sha: str | None = None
    changed_files: tuple[ChangedFile, ...] = field(default_factory=tuple)
    dirty_paths: tuple[str, ...] = field(default_factory=tuple)
    pushed: bool | None = None
    push_detail: str | None = None


def _run(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git(repo_root: Path, *args: str) -> str:
    result = _run(repo_root, *args)
    if result.returncode != 0:
        raise GitContextError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def current_branch(repo_root: Path) -> str:
    """The checked-out named branch.

    Raises :class:`GitContextError` on detached HEAD rather than guessing.
    """

    result = _run(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise GitContextError(
            f"HEAD is detached in {repo_root}; a named branch is required for handoff context"
        )
    return branch


def resolve_commit(repo_root: Path, rev: str, *, label: str = "revision") -> str:
    """Canonicalize any revision to a full 40-hex commit SHA that exists here."""

    result = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    sha = result.stdout.strip()
    if result.returncode != 0 or not _FULL_SHA_RE.match(sha):
        raise GitContextError(f"{label} {rev!r} does not resolve to a commit in {repo_root}")
    return sha


def _split_nul(raw: str) -> list[str]:
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    return tokens


def changed_files(repo_root: Path, base_sha: str, candidate_sha: str) -> tuple[ChangedFile, ...]:
    """Files changed between ``base_sha`` and ``candidate_sha`` (name-status).

    Uses NUL-delimited output (``-z``) so filenames containing spaces, tabs,
    or newlines survive intact, and passes ``-M`` so a rename reports both
    its source and destination path rather than collapsing to a plain
    delete+add pair.
    """

    result = _run(repo_root, "diff", "--name-status", "-M", "-z", f"{base_sha}..{candidate_sha}")
    if result.returncode != 0:
        raise GitContextError(result.stderr.strip() or "git diff --name-status failed")
    tokens = _split_nul(result.stdout)

    files: list[ChangedFile] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if i >= len(tokens):
            break
        if status[:1] in ("R", "C"):
            old_path = tokens[i]
            i += 1
            if i >= len(tokens):
                break
            new_path = tokens[i]
            i += 1
            files.append(ChangedFile(status=status, path=new_path, old_path=old_path))
        else:
            path = tokens[i]
            i += 1
            files.append(ChangedFile(status=status, path=path))
    return tuple(files)


@dataclass(frozen=True)
class DirtyEntry:
    """One working-tree status entry, with both sides of a rename kept
    separate rather than folded into one descriptive string. ``old_path`` is
    the rename/copy source (``status`` starting with ``R``/``C``); ``None``
    for every other status."""

    status: str
    path: str
    old_path: str | None = None


def _status_entries(repo_root: Path, *, all_untracked: bool = False) -> tuple[DirtyEntry, ...]:
    """Parse ``git status --porcelain=v1 -z`` into structured entries.

    ``all_untracked``, when true, adds ``--untracked-files=all`` so an
    untracked directory is reported file-by-file instead of collapsed into
    one ``?? dir/`` entry -- needed by any caller that must classify
    individual untracked files (e.g. the acceptance gate's workflow-artifact
    exemption) rather than merely list that something changed under a
    directory.

    NUL-delimited output preserves filenames containing spaces, tabs, or
    newlines. A rename/copy entry is followed by its own source-path token
    (no ``XY`` prefix); that token is consumed here so it is not
    misinterpreted as an unrelated entry.
    """

    args = ["status", "--porcelain=v1", "-z"]
    if all_untracked:
        args.append("--untracked-files=all")
    result = _run(repo_root, *args)
    if result.returncode != 0:
        raise GitContextError(result.stderr.strip() or "git status --porcelain failed")
    tokens = _split_nul(result.stdout)

    entries: list[DirtyEntry] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 3:
            if entry:
                entries.append(DirtyEntry(status=entry, path=entry))
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in ("R", "C") and i < len(tokens):
            old_path = tokens[i]
            i += 1
            entries.append(DirtyEntry(status=status, path=path, old_path=old_path))
        else:
            entries.append(DirtyEntry(status=status, path=path))
    return tuple(entries)


def dirty_entries(repo_root: Path, *, all_untracked: bool = False) -> tuple[DirtyEntry, ...]:
    """Working-tree status entries, each side of a rename kept separate.

    See :func:`dirty_paths` for the folded-string form used by handoff
    rendering; this structured form is for callers (the acceptance gate)
    that must classify the source and destination of a rename independently
    rather than treat the folded description as one opaque path.
    """

    return _status_entries(repo_root, all_untracked=all_untracked)


def dirty_paths(repo_root: Path) -> tuple[str, ...]:
    """Working-tree paths reported by ``git status --porcelain=v1 -z``.

    NUL-delimited output preserves filenames containing spaces, tabs, or
    newlines. A rename/copy entry is folded into one descriptive path so the
    caller still sees both sides of the rename.
    """

    paths: list[str] = []
    for entry in _status_entries(repo_root):
        if entry.old_path is not None:
            paths.append(f"{entry.path} (renamed from {entry.old_path})")
        else:
            paths.append(entry.path)
    return tuple(paths)


def _tracking_remote(repo_root: Path, branch: str) -> tuple[str, str]:
    remote = _run(repo_root, "config", "--get", f"branch.{branch}.remote").stdout.strip()
    merge_ref = _run(repo_root, "config", "--get", f"branch.{branch}.merge").stdout.strip()
    if not remote:
        remote = "origin"
    remote_branch = (
        merge_ref[len("refs/heads/") :] if merge_ref.startswith("refs/heads/") else branch
    )
    return remote, remote_branch


def verify_pushed(repo_root: Path, candidate_sha: str, branch: str) -> tuple[bool, str]:
    """Is ``candidate_sha`` reachable from ``branch``'s configured remote ref?

    Returns ``(pushed, human-readable detail)``. Mirrors the reachability
    logic (not the code) used by the Sporely V1 workflow scripts: the remote
    ref is looked up by its full name and reachability is proven locally via
    ``merge-base --is-ancestor``, rather than trusting any recorded flag.
    """

    remote, remote_branch = _tracking_remote(repo_root, branch)
    ref = f"refs/heads/{remote_branch}"
    result = _run(repo_root, "ls-remote", "--exit-code", remote, ref)
    if result.returncode != 0:
        return False, (
            f"{remote} has no {ref}; push the branch before presenting a candidate "
            f"({result.stderr.strip() or 'ls-remote failed'})"
        )
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    remote_sha = first_line.split()[0] if first_line else ""
    if not _FULL_SHA_RE.match(remote_sha):
        return False, f"{remote} {ref} did not report a usable commit id ({remote_sha!r})"
    if remote_sha == candidate_sha:
        return True, f"{remote}/{remote_branch} is exactly {candidate_sha}"
    if _run(repo_root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode != 0:
        return False, (
            f"{remote}/{remote_branch} is at {remote_sha}, which is not present locally; "
            f"fetch and re-run so reachability can be proven"
        )
    if _run(repo_root, "merge-base", "--is-ancestor", candidate_sha, remote_sha).returncode == 0:
        return True, f"{candidate_sha} is reachable from {remote}/{remote_branch} ({remote_sha})"
    return False, (
        f"{candidate_sha} is not reachable from {remote}/{remote_branch} ({remote_sha}); "
        f"push the branch before presenting a candidate"
    )


def diff_patch(repo_root: Path, base_sha: str, candidate_sha: str) -> str:
    """Raw unified diff text between ``base_sha`` and ``candidate_sha``.

    Returned byte-for-byte from git's stdout (only ``check=False`` is used,
    never ``.strip()``): a real patch's meaningful trailing whitespace on a
    changed line must survive for ``git apply``/``patch`` to work from it.
    """

    result = _run(repo_root, "diff", f"{base_sha}..{candidate_sha}")
    if result.returncode != 0:
        raise GitContextError(result.stderr.strip() or "git diff failed")
    return result.stdout


def gather_git_context(
    repo_root: Path,
    *,
    base_sha: str | None = None,
    candidate_sha: str | None = None,
    check_pushed: bool = True,
) -> GitContext:
    """Gather branch/commit/diff/status/push context for one repo.

    ``candidate_sha`` defaults to the current ``HEAD``. ``base_sha``, if
    given, is used to compute the changed-file list; if omitted, the
    changed-file list is empty (nothing recorded to diff against yet).
    """

    branch = current_branch(repo_root)
    resolved_candidate = resolve_commit(repo_root, candidate_sha or "HEAD", label="candidate_sha")
    resolved_base = resolve_commit(repo_root, base_sha, label="base_sha") if base_sha else None

    files = (
        changed_files(repo_root, resolved_base, resolved_candidate) if resolved_base else tuple()
    )
    dirty = dirty_paths(repo_root)

    pushed: bool | None = None
    push_detail: str | None = None
    if check_pushed:
        pushed, push_detail = verify_pushed(repo_root, resolved_candidate, branch)

    return GitContext(
        branch=branch,
        candidate_sha=resolved_candidate,
        base_sha=resolved_base,
        changed_files=files,
        dirty_paths=dirty,
        pushed=pushed,
        push_detail=push_detail,
    )
