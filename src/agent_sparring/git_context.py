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

from agent_sparring import engine_git
from agent_sparring.engine_git import GitConfigRefusal, GitContextError

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
    against yet).
    """

    branch: str
    candidate_sha: str
    base_sha: str | None = None
    changed_files: tuple[ChangedFile, ...] = field(default_factory=tuple)
    dirty_paths: tuple[str, ...] = field(default_factory=tuple)


def _run(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # The one engine Git path (see agent_sparring.engine_git): no hooks,
    # fsmonitor, drivers or included configuration, and -- inside a managed
    # run -- no configuration that changed since the run started.
    return engine_git.run(repo_root, *args)


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


def repository_identity(repo_root: Path) -> tuple[str, str]:
    """``(worktree top level, common git directory)``, both resolved.

    The top level says *which worktree* this is; the common directory says
    which repository it belongs to, and is shared by every linked worktree
    of one clone. Together they tell a path substitution -- another clone,
    another worktree, a symlinked alias -- from the repository that was
    actually inspected.
    """

    top = _git(repo_root, "rev-parse", "--show-toplevel")
    common = _git(repo_root, "rev-parse", "--git-common-dir")
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = Path(repo_root) / common_path
    return str(Path(top).resolve()), str(common_path.resolve())


def is_ignored(repo_root: Path, path: Path) -> bool:
    """Would git ignore ``path`` (which need not exist yet)?

    Exit status 0 from ``git check-ignore`` means ignored, 1 means not
    ignored; anything else is a real git failure and is raised.
    """

    result = _run(repo_root, "check-ignore", "-q", "--", str(path))
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitContextError(result.stderr.strip() or f"git check-ignore failed for {path}")


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


def tracking_remote(repo_root: Path, branch: str) -> tuple[str, str]:
    """The ``(remote, remote branch)`` ``branch`` is configured to track.

    ``origin`` and ``branch``'s own name are the fallbacks, which is what
    makes an unconfigured branch still have one *intended* remote ref rather
    than none. Public because the remote ref this resolves is the identity a
    legacy push authorization is bound to (:mod:`agent_sparring.push_gate`).
    It reads local configuration only.
    """

    remote = _run(repo_root, "config", "--get", f"branch.{branch}.remote").stdout.strip()
    merge_ref = _run(repo_root, "config", "--get", f"branch.{branch}.merge").stdout.strip()
    if not remote:
        remote = "origin"
    remote_branch = (
        merge_ref[len("refs/heads/") :] if merge_ref.startswith("refs/heads/") else branch
    )
    return remote, remote_branch


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """Is ``ancestor`` ``descendant`` or one of its ancestors? ``False`` when
    either commit is not present locally -- reachability that cannot be
    proven here is not assumed."""

    if ancestor == descendant:
        return True
    return _run(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


_SCP_LIKE_RE = re.compile(r"^[^/]+:")


def absolute_url(repo_root: Path, url: str) -> str:
    """``url`` as Git would resolve it when run in ``repo_root``, made
    independent of the working directory: a relative local path (such as
    ``../remote.git``) becomes absolute; URLs and ``host:path`` forms are
    returned unchanged."""

    if "://" in url or url.startswith("/") or _SCP_LIKE_RE.match(url):
        return url
    return str((Path(repo_root) / url).resolve())


def resolved_push_url(repo_root: Path, remote: str) -> str | None:
    """Where ``git push <remote>`` would actually write: ``remote.<r>.pushurl``
    or ``.url`` after Git's own ``insteadOf``/``pushInsteadOf`` rewriting,
    made absolute (see :func:`absolute_url`). ``None`` when the remote has
    no URL."""

    result = _run(repo_root, "remote", "get-url", "--push", remote)
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        return None
    return absolute_url(repo_root, url)


def config_values(repo_root: Path, key: str) -> tuple[str, ...]:
    """Every value of ``key`` (``git config --get-all``), in order."""

    result = _run(repo_root, "config", "--get-all", key)
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def config_value(repo_root: Path, key: str) -> str | None:
    """``git config --get key`` (the last value), or ``None`` when unset."""

    result = _run(repo_root, "config", "--get", key)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


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
) -> GitContext:
    """Gather branch/commit/diff/status context for one repo, from the
    local repository only (I-L1: no remote is ever read).

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

    return GitContext(
        branch=branch,
        candidate_sha=resolved_candidate,
        base_sha=resolved_base,
        changed_files=files,
        dirty_paths=dirty,
    )
