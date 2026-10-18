"""The one path every engine Git invocation goes through.

The engine runs ``git`` with the user's own credentials, outside any agent
sandbox. An agent that can write the repository can therefore try to make
the *engine* run something for it: a hook, a ``core.fsmonitor`` program, a
textconv or an external diff, merge or filter driver, an ``include``d
configuration file, or a credential helper, SSH command or URL rewrite that
sends the engine's next push somewhere else. :func:`run` is how the engine
calls Git so that none of that can happen:

- **hooks** never run: ``core.hooksPath`` is pointed at ``/dev/null`` for
  every invocation (command-line configuration outranks every file and is
  passed on to any Git process Git itself starts);
- **fsmonitor** never runs: ``core.fsmonitor=false``;
- **textconv and external diff drivers** never run: every ``diff``,
  ``show`` and ``log`` gets ``--no-ext-diff --no-textconv``;
- **filter drivers** never run: every filter any configuration defines is
  overridden to no command and not required;
- **merge drivers** never run: a merge-like command refuses while any
  configuration defines one (an override cannot make one inert);
- **included configuration** is never loaded: any ``include.*`` or
  ``includeIf.*`` key, in any scope, is a refusal *before* the command runs;
- background ``gc``/maintenance, which would start further Git processes,
  is off.

Git's own location and configuration-injection environment variables
(``GIT_DIR``, ``GIT_CONFIG_PARAMETERS``, ``GIT_EXTERNAL_DIFF`` ...) are
removed, so the engine always operates on the repository it names.

Changed configuration is a refusal, not something to follow
----------------------------------------------------------------

Inside a managed run (see :func:`trusted_config`) the repository's own
security-relevant configuration -- credential, SSH, URL rewriting, remote,
protocol, HTTP, signing, hook, fsmonitor and driver keys -- is compared, on
every invocation inside that repository, with the snapshot captured when
the run started. Any difference is a :class:`GitConfigRefusal` naming the
key. Configuration that was already there at run start is the trusted
snapshot and keeps working exactly as before (a credential helper the
repository already used still authenticates the engine's push). User-level
and system configuration are the user's own and are not compared.

Editing ``.gitattributes`` is deliberately *not* a change: an attribute
only selects a driver, and a driver runs only if configuration defines it.

Remote operations go further than a comparison: they never read the
repository's configuration at all. :func:`run_remote` runs them in a
throwaway bare repository that borrows the objects and is configured from
the trusted snapshot's transport keys only (see :func:`isolated_git_dir`),
so configuration written between a check and the invocation cannot reach
them either. Local operations do read the repository's configuration;
what they could execute is neutralized as above, and a change is refused
before each one. A concurrent writer racing a single local invocation is
outside what this module can stop; confining the agents is.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence


class GitContextError(RuntimeError):
    """Raised when git context cannot be gathered for a repo/revision.

    Defined here, and re-exported by :mod:`agent_sparring.git_context`, so
    that a configuration refusal raised by the one engine Git path is caught
    by every existing ``except GitContextError`` handler.
    """


class GitConfigRefusal(GitContextError):
    """A typed refusal to run Git because of configuration.

    ``code`` is a stable machine-readable reason; ``key`` names the exact
    configuration key (as Git spells it) the refusal is about.
    """

    def __init__(self, code: str, key: str, message: str) -> None:
        super().__init__(f"{message} [{code}: {key}]")
        self.code = code
        self.key = key


#: Every invocation, before anything a file could say.
BASE_OVERRIDES: tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "gc.auto=0",
    "maintenance.auto=false",
)

#: Subcommands whose output can be routed through textconv or an external
#: diff driver; each gets ``--no-ext-diff --no-textconv``.
_DIFF_COMMANDS = frozenset({"diff", "show", "log"})

#: Subcommands that can invoke a merge driver.
_MERGE_COMMANDS = frozenset(
    {"merge", "merge-tree", "cherry-pick", "revert", "rebase", "pull", "am", "stash",
     "merge-file", "merge-index", "merge-recursive", "merge-ort"}
)

_STRIPPED_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_ATTR_SOURCE",
    }
)

_REPO_SCOPES = frozenset({"local", "worktree"})


def _environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _STRIPPED_ENV
        and not name.startswith("GIT_CONFIG_KEY_")
        and not name.startswith("GIT_CONFIG_VALUE_")
    }
    if extra:
        env.update(extra)
    return env


def _overrides(pairs: Sequence[str]) -> list[str]:
    argv: list[str] = []
    for pair in pairs:
        argv += ["-c", pair]
    return argv


@dataclass(frozen=True)
class ConfigEntry:
    scope: str
    key: str  # section and variable lower-cased by Git; subsection as written
    value: str | None


def config_entries(cwd: Path) -> tuple[ConfigEntry, ...] | None:
    """Every configuration entry Git would read in ``cwd``, with its scope.

    ``None`` when Git cannot list configuration there at all (``cwd`` is not
    a directory, say) -- the real command would then fail the same way.
    Listing configuration only reads files; it runs nothing.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *_overrides(BASE_OVERRIDES), "config", "--list", "--null",
             "--show-scope"],
            capture_output=True,
            check=False,
            env=_environment(None),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    tokens = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: list[ConfigEntry] = []
    for i in range(0, len(tokens) - 1, 2):
        scope, pair = tokens[i], tokens[i + 1]
        key, sep, value = pair.partition("\n")
        entries.append(ConfigEntry(scope=scope, key=key, value=value if sep else None))
    return tuple(entries)


def _section(key: str) -> str:
    return key.split(".", 1)[0].lower()


def _variable(key: str) -> str:
    return key.rsplit(".", 1)[-1].lower()


def _subsection(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) < 3:
        return None
    return key[len(parts[0]) + 1 : len(key) - len(parts[-1]) - 1]


def is_include(key: str) -> bool:
    return _section(key) in ("include", "includeif")


def is_watched(key: str) -> bool:
    """Is ``key`` repository configuration whose later change must refuse?

    Credential, transport and remote keys decide where the engine's remote
    operations go and with what; the rest decide what a local operation
    could execute. Branch tracking is checked separately, by its effective
    value, against the pinned target.
    """

    section, variable = _section(key), _variable(key)
    lowered = key.lower()
    if section in ("credential", "remote", "protocol", "http", "ssh", "gpg", "include",
                   "includeif", "filter"):
        return True
    if section == "core" and variable in (
        "sshcommand", "askpass", "gitproxy", "worktree", "hookspath", "fsmonitor",
        "attributesfile",
    ):
        return True
    if section == "url" and variable in ("insteadof", "pushinsteadof"):
        return True
    if section == "diff" and (lowered == "diff.external" or variable in ("command", "textconv")):
        return True
    if section == "merge" and variable == "driver":
        return True
    return False


def repository_snapshot(entries: Sequence[ConfigEntry]) -> tuple[tuple[str, str], ...]:
    """The watched repository-scoped entries, as ``(key, value)`` in the
    order Git reads them.

    Order is part of the configuration: a later single-valued key wins, and
    multi-valued keys such as ``credential.helper`` are consulted in order.
    Sorting would make reordering invisible.
    """

    return tuple(
        (entry.key, entry.value if entry.value is not None else "")
        for entry in entries
        if entry.scope in _REPO_SCOPES and is_watched(entry.key)
    )


def capture_snapshot(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """The trusted snapshot of ``repo_root``'s own security-relevant
    configuration, captured when a run starts (or is first pinned)."""

    entries = config_entries(Path(repo_root))
    if entries is None:
        raise GitContextError(f"cannot read the git configuration of {repo_root}")
    _refuse_includes(entries)
    return repository_snapshot(entries)


@dataclass(frozen=True)
class TrustedConfig:
    """The snapshot an active managed run holds Git invocations to.

    ``roots`` are the resolved directories the snapshot governs: an
    invocation whose working directory is inside one of them is compared;
    any other (a declared sibling repository, say) is not.
    """

    roots: tuple[str, ...]
    entries: tuple[tuple[str, str], ...]

    def governs(self, cwd: Path) -> bool:
        try:
            resolved = Path(cwd).resolve()
        except OSError:
            return False
        for root in self.roots:
            root_path = Path(root)
            if resolved == root_path or root_path in resolved.parents:
                return True
        return False


_TRUSTED: contextvars.ContextVar[TrustedConfig | None] = contextvars.ContextVar(
    "agent_sparring_trusted_git_config", default=None
)


@contextlib.contextmanager
def trusted_config(
    roots: Sequence[Path], entries: Sequence[tuple[str, str]]
) -> Iterator[TrustedConfig]:
    """Hold every engine Git invocation inside ``roots`` to ``entries``."""

    guard = TrustedConfig(
        roots=tuple(str(Path(root).resolve()) for root in roots),
        entries=tuple((str(k), str(v)) for k, v in entries),
    )
    token = _TRUSTED.set(guard)
    try:
        yield guard
    finally:
        _TRUSTED.reset(token)


def active_guard() -> TrustedConfig | None:
    return _TRUSTED.get()


def _refuse_includes(entries: Sequence[ConfigEntry]) -> None:
    for entry in entries:
        if is_include(entry.key):
            raise GitConfigRefusal(
                "included_config",
                entry.key,
                f"refusing to run git: {entry.scope} configuration sets {entry.key}, and the "
                "engine never loads included configuration. Remove the include (or inline what "
                "it provides) and retry",
            )


def compare_snapshot(
    entries: Sequence[ConfigEntry], snapshot: Sequence[tuple[str, str]]
) -> None:
    """Refuse, naming the key, if the repository's watched configuration is
    not exactly ``snapshot``."""

    current = repository_snapshot(entries)
    trusted = tuple((str(k), str(v)) for k, v in snapshot)
    if current == trusted:
        return

    def by_key(pairs: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for key, value in pairs:
            grouped.setdefault(key, []).append(value)
        return grouped

    now, then = by_key(current), by_key(trusted)
    changed = sorted(
        (key for key in set(now) | set(then) if now.get(key) != then.get(key)),
        key=lambda key: key.lower(),
    )
    if not changed:
        # Same values per key, but the keys' relative order moved: the
        # first key whose position differs is the one named.
        changed = [next(a for (a, _), (b, _) in zip(current, trusted) if a != b)]
    key = changed[0] if changed else "?"
    raise GitConfigRefusal(
        "repository_config_changed",
        key,
        "refusing to run git: the repository's configuration changed after this run started "
        f"({', '.join(changed)}); the engine never follows configuration it did not trust at "
        "run start. Restore it, or start a new run",
    )


def subcommand(args: Sequence[str]) -> tuple[int, str | None]:
    """The index and name of the Git subcommand in ``args`` (after any
    leading ``-c key=value`` pairs and other global options)."""

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return i, arg
    return len(args), None


def _driver_overrides(entries: Sequence[ConfigEntry]) -> list[str]:
    names: list[str] = []
    for entry in entries:
        if _section(entry.key) == "filter":
            name = _subsection(entry.key)
            if name is not None and name not in names:
                names.append(name)
    pairs: list[str] = []
    for name in names:
        pairs += [
            f"filter.{name}.clean=",
            f"filter.{name}.smudge=",
            f"filter.{name}.process=",
            f"filter.{name}.required=false",
        ]
    return pairs


def _refuse_merge_drivers(entries: Sequence[ConfigEntry], name: str) -> None:
    for entry in entries:
        if _section(entry.key) == "merge" and _variable(entry.key) == "driver":
            raise GitConfigRefusal(
                "merge_driver_configured",
                entry.key,
                f"refusing to run git {name}: {entry.scope} configuration defines the merge "
                f"driver {entry.key}, and the engine never runs merge drivers",
            )


def _checked(cwd: Path, args: Sequence[str]) -> tuple[tuple[ConfigEntry, ...] | None, list[str]]:
    """Validate the configuration ``cwd`` has right now and build the
    arguments after ``git -C <cwd>``: the neutralizing overrides, then
    ``args`` (with diff flags inserted). Raises :class:`GitConfigRefusal`."""

    entries = config_entries(Path(cwd))
    pairs = list(BASE_OVERRIDES)
    if entries is not None:
        _refuse_includes(entries)
        guard = _TRUSTED.get()
        if guard is not None and guard.governs(Path(cwd)):
            compare_snapshot(entries, guard.entries)
        pairs += _driver_overrides(entries)
    index, name = subcommand(args)
    rest = list(args)
    if name is not None:
        if name in _MERGE_COMMANDS and entries is not None:
            _refuse_merge_drivers(entries, name)
        if name in _DIFF_COMMANDS:
            rest = rest[: index + 1] + ["--no-ext-diff", "--no-textconv"] + rest[index + 1 :]
    return entries, [*_overrides(pairs), *rest]


def command(cwd: Path, args: Sequence[str]) -> list[str]:
    """The argv :func:`run` executes for ``args`` in ``cwd``, after every
    check (the isolated configuration of :func:`run` is supplied through
    the environment, not the argv). Raises :class:`GitConfigRefusal`."""

    _entries, tail = _checked(Path(cwd), args)
    return ["git", "-C", str(cwd), *tail]


#: Subcommands run against the repository's live configuration. Each only
#: reads or writes refs and configuration -- none can reach a hook (off),
#: fsmonitor (off), a diff, merge or filter driver, a pager or an editor --
#: and each depends on the real common directory: ``rev-parse
#: --git-common-dir`` locates the engine's own records, ``worktree``
#: records worktrees relative to it, ``config`` reads the live values the
#: retargeting checks compare, and ref deletion may rewrite packed-refs.
_LIVE_COMMANDS = frozenset(
    {
        "rev-parse", "symbolic-ref", "config", "cat-file", "merge-base", "for-each-ref",
        "show-ref", "update-ref", "check-ref-format", "branch", "worktree", "remote",
        "init", "version",
    }
)


def run(
    cwd: Path,
    *args: str,
    text: bool = True,
    input: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` the one way the engine runs Git.

    Returns the completed process (``check=False``: callers read the exit
    status, as before). Raises :class:`GitConfigRefusal` -- before anything
    runs -- when configuration makes the invocation unsafe.

    Every other local command runs against an isolated copy of exactly the
    repository configuration that was just validated (see
    :func:`isolated_config`), never against the live file: configuration
    written after the check -- a new filter or merge driver, an include --
    is not what the command reads.
    """

    entries, tail = _checked(Path(cwd), args)
    _, name = subcommand(args)
    guard = _TRUSTED.get()
    if name in REMOTE_COMMANDS and guard is not None and guard.governs(Path(cwd)):
        # Inside a run, a remote operation never reads the repository's own
        # (mutable) configuration at all: see run_remote.
        return run_remote(cwd, guard.entries, *args, text=text, input=input)
    argv = ["git", "-C", str(cwd), *tail]
    if name is None or name in _LIVE_COMMANDS or name in REMOTE_COMMANDS or entries is None:
        return subprocess.run(
            argv, capture_output=True, text=text, check=False, input=input, env=_environment(env)
        )
    with isolated_config(Path(cwd), entries) as isolation:
        if isolation is None:
            message = (
                f"fatal: {cwd} is not inside a git repository the engine can locate from the file "
                "system (a .git directory or file); nothing was run against configuration it has "
                "not validated\n"
            )
            return subprocess.CompletedProcess(
                argv, 128, "" if text else b"", message if text else message.encode()
            )
        merged = dict(env or {})
        merged.update(isolation)
        return subprocess.run(
            argv, capture_output=True, text=text, check=False, input=input,
            env=_environment(merged),
        )


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_config(
    entries: Sequence[ConfigEntry],
    *,
    scopes: frozenset[str] = _REPO_SCOPES,
    drop_sections: frozenset[str] = frozenset(),
) -> str:
    """The ``entries`` of ``scopes`` as a config file, in their order.

    One section header per entry keeps the order exact. Worktree-scoped
    entries are merged in, so ``extensions.worktreeConfig`` is dropped: the
    copy must be the only repository configuration the command reads.
    Sections named in ``drop_sections`` (lower case) are left out.
    """

    lines: list[str] = []
    for entry in entries:
        if entry.scope not in scopes or entry.key.lower() == "extensions.worktreeconfig":
            continue
        if _section(entry.key) in drop_sections:
            continue
        section = entry.key.split(".", 1)[0]
        variable = entry.key.rsplit(".", 1)[-1]
        sub = _subsection(entry.key)
        if sub is None:
            lines.append(f"[{section}]")
        else:
            lines.append(f'[{section} "{sub.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"]')
        lines.append(f"\t{variable}" if entry.value is None else f"\t{variable} = {_quote(entry.value)}")
    return "\n".join(lines) + "\n"


def _is_git_dir(path: Path) -> bool:
    return (path / "HEAD").is_file() and (
        (path / "commondir").is_file() or ((path / "objects").is_dir() and (path / "refs").is_dir())
    )


def _locate(cwd: Path) -> tuple[Path, Path, Path | None] | None:
    """``(git dir, common dir, work tree or None)`` for ``cwd``, found from
    the file system alone -- never from configuration, so nothing written to
    the configuration (``core.worktree``, say) can choose the directory a
    command then works on. ``None`` when ``cwd`` is in no repository.

    The work tree is the directory holding ``.git`` (a directory, or the
    ``gitdir:`` file of a linked worktree); the common directory is the git
    directory's ``commondir``, if it has one. A bare repository (``cwd`` is
    itself a git directory) has no work tree.
    """

    try:
        start = Path(cwd).resolve()
    except OSError:
        return None
    if _is_git_dir(start) and not (start / ".git").exists():
        return start, start, None
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            git_dir = dot_git
        elif dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            git_dir = Path(text[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = directory / git_dir
        else:
            continue
        git_dir = git_dir.resolve()
        common = git_dir
        commondir = git_dir / "commondir"
        if commondir.is_file():
            try:
                pointed = Path(commondir.read_text(encoding="utf-8").strip())
            except OSError:
                return None
            common = (pointed if pointed.is_absolute() else git_dir / pointed).resolve()
        return git_dir, common, directory
    return None


@contextlib.contextmanager
def isolated_config(
    cwd: Path, entries: Sequence[ConfigEntry]
) -> Iterator[dict[str, str] | None]:
    """The environment that makes one local command read ``entries`` -- the
    configuration just validated -- instead of the repository's live file.

    ``GIT_COMMON_DIR`` points at a fresh private directory whose ``config``
    is :func:`render_config` of ``entries`` and whose every other entry is a
    symlink into the real common directory, so objects, refs, reflogs and
    worktrees are the real ones; ``GIT_DIR`` stays the real (per-worktree)
    directory, so ``HEAD`` and the index are the real ones too.

    The work tree is the one Git itself would use, decided without
    re-reading configuration: ``core.worktree`` from ``entries`` -- the
    configuration just validated, and inside a run held to the run-start
    snapshot -- when it sets one (resolved, as Git resolves it, against the
    git directory), otherwise the directory holding ``.git``. A
    ``core.worktree`` written after validation is not in ``entries`` and so
    cannot choose the directory. A repository the file system does not
    locate is refused rather than run against its live configuration.
    """

    import shutil
    import tempfile

    located = _locate(Path(cwd))
    if located is None:
        # Nothing is run: the caller gets a failed command, as from Git.
        yield None
        return
    git_dir, common, top = located
    configured = [
        entry.value
        for entry in entries
        if entry.scope in _REPO_SCOPES and entry.key.lower() == "core.worktree" and entry.value
    ]
    if configured:
        chosen = Path(configured[-1])
        top = (chosen if chosen.is_absolute() else git_dir / chosen).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="agent-sparring-config-"))
    try:
        # Reflogs must land in the real logs directory, never in scratch.
        (common / "logs").mkdir(exist_ok=True)
        for child in common.iterdir():
            if child.name in ("config", "config.worktree"):
                continue
            (scratch / child.name).symlink_to(child)
        (scratch / "config").write_text(render_config(entries), encoding="utf-8")
        isolation = {"GIT_DIR": str(git_dir), "GIT_COMMON_DIR": str(scratch)}
        if top is not None:
            isolation["GIT_WORK_TREE"] = str(top)
        yield isolation
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


#: Subcommands that talk to a remote, and so may load credential, SSH and
#: URL-rewriting configuration.
REMOTE_COMMANDS = frozenset({"push", "ls-remote", "fetch", "pull"})


def is_transport(key: str) -> bool:
    """Configuration a remote operation is allowed to take from the trusted
    snapshot: how to authenticate and how to reach a host, nothing else.

    URL rewriting (``url.*.insteadOf``/``pushInsteadOf``) is deliberately
    not copied: the pinned destination is already the resolved URL, and a
    rewrite that applied to pushes but not to reads would let the engine
    write one repository and verify another.
    """

    section, variable = _section(key), _variable(key)
    if section in ("credential", "http", "ssh", "protocol"):
        return True
    return section == "core" and variable in ("sshcommand", "askpass", "gitproxy")


def _plain(argv: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(argv), capture_output=True, text=True, check=False, env=_environment(None), cwd=cwd
    )
    if result.returncode != 0:
        raise GitContextError(result.stderr.strip() or f"{' '.join(argv[:3])} failed")
    return result


def _objects_dir(repo_root: Path) -> Path:
    common = _plain(
        ["git", "-C", str(repo_root), *_overrides(BASE_OVERRIDES), "rev-parse", "--git-common-dir"]
    ).stdout.strip()
    path = Path(common)
    if not path.is_absolute():
        path = Path(repo_root) / path
    return path.resolve() / "objects"


@contextlib.contextmanager
def isolated_git_dir(
    repo_root: Path, trusted: Sequence[tuple[str, str]]
) -> Iterator[Path]:
    """A throwaway bare repository that sees ``repo_root``'s objects (through
    ``objects/info/alternates``) and whose only repository configuration is
    the transport subset of ``trusted``, written in its original order.

    Created by the engine, for one remote operation, in a fresh private
    temporary directory, and removed afterwards. Git run against it loads
    user-level and system configuration, this file, and nothing the
    repository holds -- so a credential helper, SSH command, URL rewrite or
    include written into the repository after the run started cannot reach
    the operation even if it lands between a check and the invocation. It
    has no work tree, no hooks (it is created without templates) and no
    attributes.
    """

    import shutil
    import tempfile

    objects = _objects_dir(Path(repo_root))
    scratch = Path(tempfile.mkdtemp(prefix="agent-sparring-remote-"))
    try:
        git_dir = scratch / "repo.git"
        _plain(["git", *_overrides(BASE_OVERRIDES), "init", "-q", "--bare", "--template=", str(git_dir)])
        alternates = git_dir / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(f"{objects}\n", encoding="utf-8")
        config = git_dir / "config"
        for key, value in trusted:
            if is_transport(key):
                _plain(["git", "config", "--file", str(config), "--add", key, value])
        yield git_dir
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_remote(
    repo_root: Path,
    trusted: Sequence[tuple[str, str]],
    *args: str,
    text: bool = True,
    input: str | bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run a remote operation (``push``, ``ls-remote`` ...) for
    ``repo_root`` against :func:`isolated_git_dir`.

    Remote names do not exist there: callers pass the pinned URL. The same
    neutralizing overrides as :func:`run` apply, and included configuration
    is refused here too.

    User-level and system configuration still supply authentication and
    transport settings -- that is what keeps existing setups working -- but
    through a copy: the command reads ``GIT_CONFIG_GLOBAL`` pointing at the
    exact entries just listed, minus every ``url.*`` rewrite, and no system
    file. The destination was resolved, rewrites and all, when the run was
    pinned; rewriting it again here (``A->B`` at pin time, then ``B->C`` on
    the push) could write one repository and read another.
    """

    with isolated_git_dir(Path(repo_root), trusted) as git_dir:
        prefix = ["--git-dir", str(git_dir)]
        try:
            listed = subprocess.run(
                ["git", *prefix, *_overrides(BASE_OVERRIDES), "config", "--list", "--null",
                 "--show-scope"],
                capture_output=True,
                check=False,
                env=_environment(None),
                cwd=git_dir,
            )
        except OSError as exc:
            raise GitContextError(f"cannot run git: {exc}") from exc
        if listed.returncode != 0:
            raise GitContextError(
                listed.stderr.decode("utf-8", errors="replace").strip()
                or "cannot list the configuration a remote operation would use"
            )
        tokens = listed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        entries = []
        for i in range(0, len(tokens) - 1, 2):
            key, sep, value = tokens[i + 1].partition("\n")
            entries.append(ConfigEntry(scope=tokens[i], key=key, value=value if sep else None))
        _refuse_includes(entries)
        user_config = git_dir.parent / "user.gitconfig"
        user_config.write_text(
            render_config(
                entries,
                scopes=frozenset({"system", "global"}),
                drop_sections=frozenset({"url"}),
            ),
            encoding="utf-8",
        )
        isolation = {"GIT_CONFIG_GLOBAL": str(user_config), "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(
            ["git", *prefix, *_overrides(BASE_OVERRIDES), *args],
            capture_output=True,
            text=text,
            check=False,
            input=input,
            env=_environment(isolation),
            cwd=git_dir,
        )


__all__ = [
    "BASE_OVERRIDES",
    "ConfigEntry",
    "GitConfigRefusal",
    "GitContextError",
    "TrustedConfig",
    "active_guard",
    "capture_snapshot",
    "command",
    "compare_snapshot",
    "config_entries",
    "REMOTE_COMMANDS",
    "is_include",
    "is_transport",
    "is_watched",
    "isolated_git_dir",
    "repository_snapshot",
    "run",
    "run_remote",
    "subcommand",
    "trusted_config",
]
