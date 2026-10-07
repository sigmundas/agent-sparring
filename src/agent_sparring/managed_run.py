"""Managed run worktrees: the engine-owned record, creation and resume.

A *managed* run executes in a disposable branch + worktree the engine
creates for it, so the person's own checkout is never switched, reset or
written to. Ownership comes only from the record kept here, in the
repository's common git directory
(``<git-common-dir>/agent-sparring/worktrees/<run-key>.json``) where every
worktree of the repository sees it and no checkout owns it -- never from a
directory name, a branch name or ``git worktree list``.

Fields other than ``events`` never change after creation. Lifecycle is the
append-only ``events`` list and is derived from it, never stored twice.
This module covers creation, resume lookup and the read-only listing; merge
and cleanup are not implemented here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
RECORDS_SUBDIR = Path("agent-sparring") / "worktrees"
INPUT_KINDS = ("markdown", "manifest")
EVENTS = (
    "created",
    "merged",
    "target_pushed",
    "state_archived",
    "worktree_removed",
    "branch_deleted",
    "remote_branch_deleted",
    "finished",
)
_RUN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "run_key",
        "plan_label",
        "input",
        "worktree_path",
        "branch",
        "target_branch",
        "base_sha",
        "remote",
        "created_at",
        "created_by",
        "events",
    }
)
#: Optional in schema v1 (absent means ``.sparring``): the project
#: directory relative to the worktree, always contained in it.
_OPTIONAL_KEYS = frozenset({"project_dir"})
DEFAULT_PROJECT_DIR = ".sparring"


def valid_project_dir(value: Any) -> bool:
    """A relative path strictly inside the worktree: no root, no ``..``."""

    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return bool(path.parts) and not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


class ManagedRunError(RuntimeError):
    """A managed-run refusal: a stable ``code`` and a human sentence.
    Raised before anything is changed unless the message says otherwise."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} [{code}]")
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- git ---------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)


def _git_out(cwd: Path, *args: str, code: str = "git_failed") -> str:
    result = _git(cwd, *args)
    if result.returncode != 0:
        raise ManagedRunError(code, result.stderr.strip() or f"git {' '.join(args)} failed in {cwd}")
    return result.stdout.strip()


def _git_out_raw(cwd: Path, *args: str, code: str = "git_failed") -> str:
    """Like :func:`_git_out`, but stdout unstripped (NUL-delimited output)."""

    result = _git(cwd, *args)
    if result.returncode != 0:
        raise ManagedRunError(code, result.stderr.strip() or f"git {' '.join(args)} failed in {cwd}")
    return result.stdout


def git_common_dir(repo_root: Path) -> Path:
    common = Path(_git_out(repo_root, "rev-parse", "--git-common-dir", code="not_a_repository"))
    if not common.is_absolute():
        common = Path(repo_root) / common
    return common.resolve()


def worktree_top(path: Path) -> Path:
    return Path(_git_out(path, "rev-parse", "--show-toplevel", code="not_a_repository")).resolve()


def worktree_list(repo_root: Path) -> list[dict[str, str | None]]:
    """``git worktree list --porcelain`` as ``[{path, head, branch}]``,
    main worktree first; ``branch`` is the short name or ``None``."""

    raw = _git_out(repo_root, "worktree", "list", "--porcelain")
    entries: list[dict[str, str | None]] = []
    current: dict[str, str | None] | None = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            current = {"path": str(Path(line[len("worktree "):]).resolve()), "head": None, "branch": None}
            entries.append(current)
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif current is not None and line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return entries


def branch_exists(repo_root: Path, branch: str) -> bool:
    return _git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def branch_tip(repo_root: Path, branch: str) -> str:
    if not branch_exists(repo_root, branch):
        raise ManagedRunError("target_branch_missing", f"target branch {branch!r} does not exist (refs/heads/{branch})")
    return _git_out(repo_root, "rev-parse", f"refs/heads/{branch}^{{commit}}")


def checked_out_branch(path: Path) -> str | None:
    result = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return result.stdout.strip() or None if result.returncode == 0 else None


def resolve_target(repo_root: Path, target_branch: str | None) -> tuple[str, str]:
    """``(target branch, its tip)``: the given branch, else the one checked
    out at ``repo_root``. Detached HEAD or a missing branch refuses."""

    if not target_branch:
        target_branch = checked_out_branch(repo_root)
        if not target_branch:
            raise ManagedRunError(
                "detached_head",
                f"HEAD is detached in {repo_root}; give --target-branch to say which branch the managed run starts from",
            )
    return target_branch, branch_tip(repo_root, target_branch)


def dirty_paths(path: Path) -> tuple[str, ...]:
    raw = _git_out(path, "status", "--porcelain", "--untracked-files=normal")
    return tuple(line[3:] for line in raw.splitlines() if line.strip())


# -- the record --------------------------------------------------------------


@dataclass(frozen=True)
class ManagedRunRecord:
    run_key: str
    plan_label: str
    input_kind: str
    input_path: str
    worktree_path: str
    branch: str
    target_branch: str
    base_sha: str
    remote: str | None
    created_at: str
    #: The project directory relative to the worktree (see :data:`_OPTIONAL_KEYS`).
    project_dir: str = DEFAULT_PROJECT_DIR
    created_by: str = "engine"
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def lifecycle(self) -> str:
        names = {event["event"] for event in self.events}
        for name in ("finished", "worktree_removed", "merged", "created"):
            if name in names:
                return name
        return "creating"

    @property
    def sparring_dir(self) -> Path:
        """The project directory inside the worktree; refuses one that
        would resolve outside it (a symlink, or a hand-edited record)."""

        worktree = Path(self.worktree_path)
        if not valid_project_dir(self.project_dir):
            raise ManagedRunError("record_malformed", f"project_dir {self.project_dir!r} is not inside the worktree")
        directory = worktree / self.project_dir
        if worktree.exists():
            try:
                directory.resolve().relative_to(worktree.resolve())
            except ValueError as exc:
                raise ManagedRunError(
                    "project_dir_outside_worktree",
                    f"{directory} resolves outside the managed worktree {worktree}",
                ) from exc
        return directory

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_key": self.run_key,
            "plan_label": self.plan_label,
            "input": {"kind": self.input_kind, "path": self.input_path},
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "target_branch": self.target_branch,
            "base_sha": self.base_sha,
            "remote": self.remote,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "project_dir": self.project_dir,
            "events": [dict(event) for event in self.events],
        }

    @classmethod
    def from_dict(cls, payload: Any, *, where: str = "managed-run record") -> "ManagedRunRecord":
        def bad(detail: str) -> ManagedRunError:
            return ManagedRunError("record_malformed", f"{where} is malformed: {detail}")

        if not isinstance(payload, Mapping):
            raise bad("not a JSON object")
        version = payload.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ManagedRunError(
                "record_schema_unknown",
                f"{where} has schema_version {version!r}; this engine reads only {SCHEMA_VERSION}",
            )
        keys = set(payload)
        missing, extra = sorted(_RECORD_KEYS - keys), sorted(keys - _RECORD_KEYS - _OPTIONAL_KEYS)
        if missing or extra:
            raise bad(f"missing {missing}, unexpected {extra}")

        def text(key: str) -> str:
            value = payload[key]
            if not isinstance(value, str) or not value:
                raise bad(f"{key} must be a non-empty string")
            return value

        source = payload["input"]
        if not isinstance(source, Mapping) or set(source) != {"kind", "path"}:
            raise bad("input must be {kind, path}")
        if source["kind"] not in INPUT_KINDS or not isinstance(source["path"], str) or not source["path"]:
            raise bad("input.kind must be markdown or manifest, with a path")
        if not Path(source["path"]).is_absolute():
            raise bad("input.path must be absolute")
        project_dir = payload.get("project_dir", DEFAULT_PROJECT_DIR)
        if not valid_project_dir(project_dir):
            raise bad("project_dir must be a relative path inside the worktree")
        remote = payload["remote"]
        if remote is not None and (not isinstance(remote, str) or not remote):
            raise bad("remote must be a string or null")
        run_key = text("run_key")
        if not _RUN_KEY_RE.match(run_key):
            raise bad(f"run_key {run_key!r} is not a valid run key")
        if not _SHA_RE.match(text("base_sha")):
            raise bad("base_sha must be a 40-hex commit")
        if not Path(text("worktree_path")).is_absolute():
            raise bad("worktree_path must be absolute")
        if text("created_by") != "engine":
            raise bad("created_by must be 'engine'")
        events = payload["events"]
        if not isinstance(events, list):
            raise bad("events must be an array")
        for event in events:
            if (
                not isinstance(event, Mapping)
                or set(event) != {"at", "event", "detail"}
                or event["event"] not in EVENTS
                or not isinstance(event["at"], str)
                or not isinstance(event["detail"], Mapping)
            ):
                raise bad(f"unreadable event {event!r}")
        return cls(
            run_key=run_key,
            plan_label=text("plan_label"),
            input_kind=source["kind"],
            input_path=source["path"],
            worktree_path=text("worktree_path"),
            branch=text("branch"),
            target_branch=text("target_branch"),
            base_sha=text("base_sha"),
            remote=remote,
            created_at=text("created_at"),
            project_dir=project_dir,
            created_by="engine",
            events=tuple(dict(event) for event in events),
        )


def records_dir(repo_root: Path) -> Path:
    return git_common_dir(repo_root) / RECORDS_SUBDIR


def _check_run_key(run_key: str) -> None:
    if not _RUN_KEY_RE.match(run_key):
        raise ManagedRunError("run_key_invalid", f"{run_key!r} is not a valid run key")


def record_path(repo_root: Path, run_key: str) -> Path:
    _check_run_key(run_key)
    return records_dir(repo_root) / f"{run_key}.json"


def _load(path: Path) -> ManagedRunRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRunError("record_unreadable", f"cannot read managed-run record {path}: {exc}") from exc
    record = ManagedRunRecord.from_dict(payload, where=f"managed-run record {path}")
    if record.run_key != path.stem:
        raise ManagedRunError(
            "record_malformed", f"managed-run record {path} names run key {record.run_key!r}, not {path.stem!r}"
        )
    return record


def read_record(repo_root: Path, run_key: str) -> ManagedRunRecord | None:
    path = record_path(repo_root, run_key)
    return _load(path) if path.is_file() else None


def list_records(repo_root: Path) -> tuple[ManagedRunRecord, ...]:
    """Every record of this repository, by run key. Unreadable records refuse."""

    directory = records_dir(repo_root)
    if not directory.is_dir():
        return ()
    return tuple(_load(path) for path in sorted(directory.glob("*.json")))


def _write_temp(directory: Path, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return Path(name)


def create_record(repo_root: Path, record: ManagedRunRecord) -> Path:
    """Write ``record`` atomically and exclusively: a run key is claimed once."""

    path = record_path(repo_root, record.run_key)
    temp = _write_temp(path.parent, record.to_dict())
    try:
        os.link(temp, path)
    except FileExistsError as exc:
        raise ManagedRunError(
            "record_exists", f"a managed-run record for {record.run_key} already exists at {path}"
        ) from exc
    finally:
        temp.unlink(missing_ok=True)
    return path


def append_event(repo_root: Path, run_key: str, event: str, detail: Mapping[str, Any] | None = None) -> ManagedRunRecord:
    if event not in EVENTS:
        raise ManagedRunError("event_unknown", f"unknown managed-run event {event!r}")
    path = record_path(repo_root, run_key)
    record = _load(path)
    updated = ManagedRunRecord(
        **{
            **record.__dict__,
            "events": record.events + ({"at": _now(), "event": event, "detail": dict(detail or {})},),
        }
    )
    temp = _write_temp(path.parent, updated.to_dict())
    os.replace(temp, path)
    return updated


def record_for_worktree(repo_root: Path, worktree: Path) -> ManagedRunRecord | None:
    """The record whose managed worktree is ``worktree``, if any."""

    target = str(Path(worktree).resolve())
    for record in list_records(repo_root):
        if record.worktree_path == target:
            return record
    return None


# -- creation ----------------------------------------------------------------


def committed_project(repo_root: Path, sparring_dir: Path, target: str, base_sha: str) -> tuple[Path, bytes]:
    """``(project dir relative to the worktree, its project.toml at base_sha)``.

    The managed worktree is checked out at ``base_sha``, so this committed
    file -- not the invoking checkout's copy -- is the configuration the run
    executes with. Refuses when the project directory is not inside the
    invoking worktree or its project.toml is not committed there."""

    top = worktree_top(repo_root)
    try:
        project_rel = Path(sparring_dir).resolve().relative_to(top)
        if not valid_project_dir(project_rel.as_posix()):
            raise ValueError(project_rel)
    except ValueError as exc:
        raise ManagedRunError(
            "project_outside_worktree",
            f"the project directory {sparring_dir} is not inside {top}; a managed worktree "
            "needs it at the same relative location",
        ) from exc
    config_rel = (project_rel / "project.toml").as_posix()
    shown = subprocess.run(
        ["git", "-C", str(top), "show", f"{base_sha}:{config_rel}"], capture_output=True, check=False
    )
    if shown.returncode != 0:
        raise ManagedRunError(
            "project_not_committed",
            f"{config_rel} is not committed at {target} ({base_sha}); a managed worktree must be a "
            "working project, so commit the project configuration first",
        )
    return project_rel, shown.stdout


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-") or "plan"


@dataclass(frozen=True)
class ManagedRunPlan:
    """Everything creation needs, checked before anything is written."""

    record: ManagedRunRecord
    uncommitted: tuple[str, ...]


def map_input(invoking_top: Path, base_sha: str, input_path: Path) -> tuple[Path, Path]:
    """``(path the run reads, path to read it at for preflight)``.

    An input inside the invoking checkout that is committed at ``base_sha``
    with identical bytes is read from the managed worktree at the same
    relative location; anything else is read where it is, read-only."""

    resolved = Path(input_path).resolve()
    try:
        relative = resolved.relative_to(invoking_top)
    except ValueError:
        return resolved, resolved
    shown = subprocess.run(
        ["git", "-C", str(invoking_top), "show", f"{base_sha}:{relative.as_posix()}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode == 0 and resolved.is_file() and shown.stdout == resolved.read_bytes():
        return relative, resolved  # relative: joined to the worktree once it is known
    return resolved, resolved


def plan_managed_run(
    repo_root: Path,
    sparring_dir: Path,
    *,
    source_label,
    input_kind: str,
    input_path: Path,
    target_branch: str | None,
    run_key: str | None,
    new_run_key,
    base_sha: str | None = None,
) -> ManagedRunPlan:
    """Preflight a managed run; writes nothing.

    ``source_label(read_path, mapped)`` reads and checks the input (``mapped``
    is relative when the run will read it inside the worktree) and returns
    the plan label the run will record; ``new_run_key(label)`` is the plan
    module's own."""

    invoking_top = worktree_top(repo_root)
    target, tip = resolve_target(Path(repo_root), target_branch)
    if base_sha is not None and base_sha != tip:
        raise ManagedRunError(
            "target_moved",
            f"target branch {target} is at {tip}, not {base_sha} as confirmed; run start-plan again",
        )
    project_rel, _ = committed_project(invoking_top, sparring_dir, target, tip)

    entries = worktree_list(invoking_top)
    main = Path(entries[0]["path"])  # type: ignore[arg-type]
    mapped, read_path = map_input(invoking_top, tip, input_path)
    if run_key is not None:
        _check_run_key(run_key)
    label = source_label(read_path, mapped)
    key = run_key or new_run_key(label)
    suffix = key.rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        suffix = re.sub(r"[^0-9a-f]", "", key)[-8:] or "run"
    branch = f"sparring/{_slug(Path(label).stem)}-{suffix}"
    worktree = (main.parent / f"{main.name}-sparring-{key}").resolve()
    input_abs = worktree / mapped if not mapped.is_absolute() else mapped

    if record_path(invoking_top, key).exists():
        raise ManagedRunError("record_exists", f"run key {key} already has a managed-run record; nothing was created")
    # A run key is one identity repository-wide: look in this project's state
    # directory in every registered worktree, not only the invoking one.
    state_dirs = {Path(sparring_dir).resolve() / "plans"} | {
        Path(str(entry["path"])) / project_rel / "plans" for entry in entries
    }
    for directory in sorted(state_dirs):
        if (directory / f"{key}.json").exists():
            raise ManagedRunError(
                "run_key_used",
                f"run key {key} is already used by the run state {directory / f'{key}.json'}; nothing was created",
            )
    if branch_exists(invoking_top, branch):
        raise ManagedRunError("branch_exists", f"branch {branch} already exists; nothing was created")
    if os.path.lexists(worktree) or any(entry["path"] == str(worktree) for entry in entries):
        raise ManagedRunError("path_exists", f"{worktree} already exists; nothing was created")

    remote_result = _git(invoking_top, "config", "--get", f"branch.{target}.remote")
    remote = remote_result.stdout.strip() or None
    if remote is None and _git(invoking_top, "remote", "get-url", "origin").returncode == 0:
        remote = "origin"
    record = ManagedRunRecord(
        run_key=key,
        plan_label=label,
        input_kind=input_kind,
        input_path=str(input_abs),
        worktree_path=str(worktree),
        branch=branch,
        target_branch=target,
        base_sha=tip,
        remote=remote,
        created_at=_now(),
        project_dir=project_rel.as_posix(),
    )
    return ManagedRunPlan(record=record, uncommitted=dirty_paths(invoking_top))


def create_managed_worktree(repo_root: Path, plan: ManagedRunPlan) -> ManagedRunRecord:
    """Record (exclusive), ``git worktree add -b``, then the ``created`` event.

    A failed ``worktree add`` that left neither branch nor path removes the
    record it just wrote; otherwise the record stays and the refusal says
    what exists."""

    record = plan.record
    path = create_record(repo_root, record)
    result = _git(
        Path(repo_root), "worktree", "add", "-b", record.branch, record.worktree_path, record.base_sha
    )
    if result.returncode != 0:
        branch_left = branch_exists(Path(repo_root), record.branch)
        path_left = os.path.lexists(record.worktree_path)
        if not branch_left and not path_left:
            path.unlink(missing_ok=True)
            raise ManagedRunError(
                "worktree_add_failed",
                f"git worktree add failed ({result.stderr.strip()}); nothing was left behind",
            )
        raise ManagedRunError(
            "worktree_add_failed",
            f"git worktree add failed ({result.stderr.strip()}); left in place: record {path}"
            + (f", branch {record.branch}" if branch_left else "")
            + (f", path {record.worktree_path}" if path_left else ""),
        )
    return append_event(repo_root, record.run_key, "created", {})


# -- resume ------------------------------------------------------------------


def verify_worktree(repo_root: Path, record: ManagedRunRecord) -> None:
    """The record's worktree exists, is registered for this repository and
    has the record's branch checked out."""

    path = Path(record.worktree_path)
    if not path.is_dir():
        raise ManagedRunError("worktree_missing", f"managed worktree {path} of run {record.run_key} does not exist")
    entry = next((entry for entry in worktree_list(repo_root) if entry["path"] == str(path.resolve())), None)
    if entry is None:
        raise ManagedRunError(
            "worktree_unregistered", f"{path} is not a registered worktree of this repository (git worktree list)"
        )
    if entry["branch"] != record.branch:
        raise ManagedRunError(
            "worktree_branch_mismatch",
            f"{path} has {entry['branch'] or 'a detached HEAD'} checked out, not the run's branch {record.branch}",
        )


def same_repository(a: Path, b: Path) -> bool:
    return git_common_dir(a) == git_common_dir(b)


def check_input_agrees(record: ManagedRunRecord, *, kind: str, path: Path, invoking_top: Path) -> None:
    """A plan path or manifest given with a resume must be the record's."""

    if kind != record.input_kind:
        raise ManagedRunError(
            "input_kind_mismatch",
            f"run {record.run_key} was started from a {record.input_kind} input, not a {kind} one",
        )
    given = Path(path).resolve()
    recorded = Path(record.input_path)
    if given == recorded:
        return
    # The same repo-relative file, named from another worktree of the repository.
    try:
        recorded_rel = recorded.relative_to(record.worktree_path)
    except ValueError:
        recorded_rel = None
    for top in (invoking_top, Path(record.worktree_path)):
        try:
            if recorded_rel is not None and given.relative_to(top) == recorded_rel:
                return
        except ValueError:
            continue
    raise ManagedRunError(
        "input_mismatch", f"run {record.run_key} runs {recorded}, not {given}; refusing to resume with another input"
    )


def require_state_matches(repo_root: Path, run_key: str, *, expected_branch: str) -> None:
    """Integrity: a run state saying ``managed: true`` must have a record
    whose worktree and branch are where the state was found."""

    record = read_record(repo_root, run_key)
    if record is None:
        raise ManagedRunError(
            "managed_record_missing", f"run {run_key} is recorded as managed but has no managed-run record"
        )
    top = str(worktree_top(repo_root))
    if record.worktree_path != top or record.branch != expected_branch:
        raise ManagedRunError(
            "managed_record_mismatch",
            f"run {run_key}'s record names {record.worktree_path} on {record.branch}, not {top} on {expected_branch}",
        )


def refuse_unmanaged_here(repo_root: Path) -> None:
    """An unmanaged run inside a managed worktree is refused."""

    try:
        top = worktree_top(repo_root)
        record = record_for_worktree(repo_root, top)
    except ManagedRunError as exc:
        if exc.code == "not_a_repository":
            return
        raise
    if record is not None:
        raise ManagedRunError(
            "managed_worktree",
            f"{top} is the managed worktree of run {record.run_key}; it belongs to that run only "
            f"(resume it with resume-plan --run-key {record.run_key})",
        )


# -- listing -----------------------------------------------------------------


def runs_payload(repo_root: Path) -> dict[str, Any]:
    runs = []
    for record in list_records(repo_root):
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        status = "missing"
        if state_path.is_file():
            try:
                status = str(json.loads(state_path.read_text(encoding="utf-8")).get("status") or "missing")
            except (OSError, json.JSONDecodeError, AttributeError):
                status = "unreadable"
        runs.append(
            {
                "run_key": record.run_key,
                "plan_label": record.plan_label,
                "managed": True,
                "worktree_path": record.worktree_path,
                "worktree_exists": Path(record.worktree_path).is_dir(),
                "branch": record.branch,
                "target_branch": record.target_branch,
                "base_sha": record.base_sha,
                "created_at": record.created_at,
                "lifecycle": record.lifecycle,
                "run_status": status,
            }
        )
    return {"schema_version": SCHEMA_VERSION, "runs": runs}
