"""Managed-run git status and finish eligibility (read-only).

:func:`finish_status` is the one function both ``sparring runs --json`` and
``sparring finish-run --dry-run`` report from, so the listing and the dry
run can never disagree. It never writes: every git command here reads, and
the merge simulation uses ``git merge-tree --write-tree``, which writes only
unreferenced objects, and ``git status`` runs with ``--no-optional-locks`` so
it never refreshes a checkout's index. Acquiring and releasing the worktree lock touches only
the lock file outside the repository.

Eligibility is advice, never a token: a finish that executes re-runs every
check itself.

Both are local (Plan L): no remote is read, and a finish merges into the
local target branch of every participating repository and contacts none.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from agent_sparring import engine_git, managed_run, trusted_snapshots
from agent_sparring.capabilities import (
    REMOTE_FINISH_REMOVED,
    delete_remote_branch_removed,
    push_target_removed,
)
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord
from agent_sparring.run_history import (
    RUN_HISTORY_REWRITTEN,
    RunHistoryError,
    describe_repository,
    run_accepted,
)
from agent_sparring.trusted_snapshots import TRUSTED_CONFIG_MISSING

#: The ``finish`` object of ``runs --json`` / ``finish-run --dry-run``; its shape is unchanged.
FINISH_SCHEMA_VERSION = 1
#: The ``finish-run`` execution report. ``plan_removal`` and ``remote_branch``
#: are additive fields within v1 (clients ignore unknown fields).
FINISH_REPORT_SCHEMA_VERSION = 1
MERGE_MODES = ("already_merged", "fast_forward", "merge_commit")


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, code: str, ok: bool, detail: str) -> bool:
        self.items.append({"code": code, "ok": bool(ok), "detail": detail})
        return ok

    @property
    def all_ok(self) -> bool:
        return all(item["ok"] for item in self.items)


def _empty_git() -> dict[str, Any]:
    return {
        "head": None,
        "branch_tip": None,
        "clean": None,
        "final_candidate": None,
        "candidate_pushed": None,
        "target_tip": None,
        "target_contains_candidate": None,
        "target_checked_out_at": None,
        "siblings": [],
    }


def _finish(run_key: str, *, managed: bool, checks: _Checks, merge_mode: str | None = None,
            eligible_merge: bool = False, eligible_cleanup: bool = False,
            actions: list[str] | None = None, deleted_ignored: list[str] | None = None,
            kept: list[dict[str, str]] | None = None) -> dict[str, Any]:
    failed = [item["code"] for item in checks.items if not item["ok"]]
    if eligible_cleanup:
        summary = f"run {run_key} can be finished ({merge_mode})"
    elif eligible_merge:
        summary = f"run {run_key} can be merged ({merge_mode}) but not cleaned up" + (
            ": " + ", ".join(failed) if failed else ""
        )
    else:
        summary = f"run {run_key} cannot be finished: " + ", ".join(failed)
    return {
        "schema_version": FINISH_SCHEMA_VERSION,
        "run_key": run_key,
        "managed": managed,
        "eligible": {"merge": eligible_merge, "cleanup": eligible_cleanup},
        "merge_mode": merge_mode,
        "actions": actions or [],
        "checks": checks.items,
        "deleted_ignored_paths": deleted_ignored or [],
        "kept": kept or [],
        "summary": summary,
    }


def _rev(cwd: Path, ref: str) -> str | None:
    result = managed_run._git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _status(cwd: Path, *, all_untracked: bool = False) -> list[tuple[str, str]]:
    """``git status`` as ``[(XY, path)]`` without git's optional index
    refresh, so reporting never rewrites a checkout's index. Raises
    :class:`ManagedRunError` if git fails."""

    args = ["--no-optional-locks", "status", "--porcelain=v1", "-z"]
    if all_untracked:
        args.append("--untracked-files=all")
    tokens = managed_run._git_out_raw(cwd, *args).split("\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 3:
            continue
        entries.append((token[:2], token[3:]))
        if token[0] in ("R", "C"):
            i += 1  # the rename/copy source path
    return entries


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return managed_run._git(cwd, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _final_candidate(
    record: ManagedRunRecord, state: Any
) -> tuple[bool, str, str | None, tuple[Any, ...]]:
    """``(every stage accepted, detail, last accepted stage's candidate,
    each sibling's last accepted candidate)`` in plan order, read from the
    run's own stage states. A sibling is ``(its accepted
    :class:`~agent_sparring.stage.CandidateRepository`, its root resolved
    against the run's worktree)``."""

    from agent_sparring.plan import MarkdownPlanSource, load_plan_source
    from agent_sparring.stage import Stage, StageStatus

    worktree = Path(record.worktree_path)
    source = load_plan_source(Path(record.input_path), worktree, manifest=record.input_kind == "manifest")
    if isinstance(source, MarkdownPlanSource):
        source = source.in_namespace(record.run_key)
    if source.digest() != state.plan_digest:
        return False, f"{record.input_path} no longer matches the plan this run executed", None, ()
    candidate: str | None = None
    siblings: dict[tuple[str, str], Any] = {}
    unaccepted: list[str] = []
    for planned in source.stages():
        stage = Stage.resolve(record.sparring_dir, planned.stage_id)
        stage_state = stage.read_state() if stage.exists() else None
        if (
            stage_state is None
            or stage_state.status is not StageStatus.ACCEPTED
            or not stage_state.candidate_sha
            or stage_state.run != record.run_key
        ):
            unaccepted.append(planned.stage_id)
            continue
        candidate = stage_state.candidate_sha
        for repository in stage_state.repositories:
            if repository.candidate_sha:
                siblings.pop((repository.name, repository.path), None)
                siblings[(repository.name, repository.path)] = (
                    repository, (worktree / repository.path).resolve()
                )
    if unaccepted:
        return False, "not accepted by this run: " + ", ".join(unaccepted), candidate, ()
    return True, "every stage is accepted", candidate, tuple(siblings.values())


def _ignored(worktree: Path, *pathspec: str, directory: bool = False) -> list[str]:
    args = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"]
    if directory:
        args.append("--directory")
    result = managed_run._git(worktree, *args, "--", *pathspec)
    # git warns on stderr and still exits 0 when it cannot open a directory,
    # so a successful listing with any warning is incomplete, not an answer.
    if result.returncode != 0 or result.stderr.strip():
        raise ManagedRunError(
            "git_failed", f"git ls-files could not list every ignored file: {result.stderr.strip() or 'failed'}"
        )
    out = result.stdout
    return [entry.rstrip("/") for entry in out.split("\0") if entry]


def _project_state_files(record: ManagedRunRecord) -> list[str]:
    """Every ignored file under the record's project directory, relative to
    it -- the state worktree removal would delete, so all of it is archived
    (``stages/.archive/**`` and unreadable stages included). Raises
    :class:`ManagedRunError` if git cannot list it."""

    project = PurePosixPath(record.project_dir)
    files = []
    for entry in _ignored(Path(record.worktree_path), project.as_posix()):
        path = PurePosixPath(entry)
        if project in path.parents:
            files.append(path.relative_to(project).as_posix())
    return sorted(files)


def _untraversable(record: ManagedRunRecord) -> list[str]:
    """Directories under the project directory that cannot be listed: their
    contents are invisible to any listing, so they can never be proven archived."""

    root = record.sparring_dir
    failed: list[str] = []

    def onerror(exc: OSError) -> None:
        failed.append(Path(exc.filename).relative_to(root).as_posix() + "/" if exc.filename else str(exc))

    for directory, _, _ in os.walk(root, onerror=onerror):
        if not os.access(directory, os.R_OK | os.X_OK):
            rel = Path(directory).relative_to(root).as_posix() + "/"
            if rel not in failed:
                failed.append(rel)
    return sorted(set(failed))


def _unarchivable(record: ManagedRunRecord, files: list[str]) -> list[str]:
    root = record.sparring_dir
    return _untraversable(record) + [
        rel for rel in files
        if not (root / rel).is_symlink() and not ((root / rel).is_file() and os.access(root / rel, os.R_OK))
    ]


def _deleted_ignored(record: ManagedRunRecord) -> list[str]:
    """Every ignored path outside the archived project directory that
    worktree removal would delete. Raises :class:`ManagedRunError`."""

    worktree = Path(record.worktree_path)
    project = PurePosixPath(record.project_dir)
    deleted: list[str] = []
    for entry in _ignored(worktree, directory=True):
        path = PurePosixPath(entry)
        if path == project or project in path.parents:
            continue  # archived
        if path in project.parents:
            # An ignored ancestor of the project directory: list what it
            # holds outside the project directory file by file.
            deleted += [
                inner for inner in _ignored(worktree, entry)
                if project not in PurePosixPath(inner).parents
            ]
            continue
        deleted.append(entry)
    return sorted(deleted)


def _operations_in_progress(checkout: Path) -> list[str]:
    """Git operations a person has in progress in ``checkout``."""

    found: list[str] = []
    for name, what in _IN_PROGRESS:
        result = managed_run._git(checkout, "rev-parse", "--path-format=absolute", "--git-path", name)
        if result.returncode != 0:
            raise ManagedRunError("git_failed", result.stderr.strip() or f"git rev-parse --git-path {name} failed")
        if Path(result.stdout.strip()).exists() and what not in found:
            found.append(what)
    return found


_IN_PROGRESS = (
    ("MERGE_HEAD", "a merge"),
    ("CHERRY_PICK_HEAD", "a cherry-pick"),
    ("REVERT_HEAD", "a revert"),
    ("rebase-merge", "a rebase"),
    ("rebase-apply", "a rebase"),
    ("BISECT_LOG", "a bisect"),
)

# A finish is local (Plan L, I-L1): it merges into the local target branch of
# every participating repository -- the primary and each sibling the run
# accepted a candidate in -- after the run's accepted history is shown to be
# preserved in each (I-L2b), and cleans up locally. It contacts no remote:
# nothing is read from one, pushed to one or deleted on one. A record's
# ``remote`` and a legacy run's pin load inertly and are never read to
# finish. ``--push-target`` and a reviewed candidate whose ``[finish]
# delete_remote_branch`` is true refuse with ``remote_finish_removed`` (see
# :mod:`agent_sparring.capabilities`); publishing is Push to review.
#
# The report's ``kept`` is always empty and its ``remote_branch`` says the
# finish is local (``local_finish``), unless an older engine recorded a
# remote branch deletion for a run it finished.
LOCAL_FINISH = "local_finish"
LOCAL_FINISH_DETAIL = "finish-run is local: no remote branch is read, pushed or deleted"

# A sibling's target is the branch of the run's own target name in that
# sibling repository. A sibling's branch is the plan's declaration, never a
# managed one, so finish merges its accepted candidate and deletes nothing
# there.


# Checks that block only cleanup; a merge (``--merge-only``) may still run.
CLEANUP_CHECKS = frozenset({"unarchived_project_state"})


def finish_status(
    repo_root: Path, run_key: str, *, allow_merge_commit: bool = False, lock_held: bool = False
) -> dict[str, Any]:
    """``{"git": ..., "finish": ...}`` for ``run_key``; never raises for an
    unknown, malformed or unmanaged run -- that is the ``unmanaged`` check.

    ``lock_held``: the caller (a finish that executes) already holds the
    worktree lock, so ``runner_live`` holds by construction."""

    git = _empty_git()
    checks = _Checks()
    deleted_ignored: list[str] = []
    try:
        record = managed_run.read_record(repo_root, run_key)
    except (ManagedRunError, OSError) as exc:
        checks.add("unmanaged", False, f"no usable managed-run record for {run_key}: {exc}")
        return {"git": git, "finish": _finish(run_key, managed=False, checks=checks)}
    if record is None or record.created_by != "engine":
        checks.add("unmanaged", False, f"{run_key} has no engine-created managed-run record; it is never finished")
        return {"git": git, "finish": _finish(run_key, managed=False, checks=checks)}
    if not record.owns_git_state:
        checks.add(
            "unmanaged", False,
            f"{run_key} is {record.lifecycle}: its record proves no branch or worktree is the run's; it is never finished",
        )
        return {"git": git, "finish": _finish(run_key, managed=False, checks=checks)}
    checks.add("unmanaged", True, f"engine-created managed run on {record.branch}")

    worktree = Path(record.worktree_path)
    # -- the run --------------------------------------------------------------
    from agent_sparring.plan import PlanError, PlanRunState, PlanRunStatus

    state = None
    candidate: str | None = None
    siblings: tuple[Any, ...] = ()
    try:
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        state = PlanRunState.load(state_path) if state_path.is_file() else None
    except (PlanError, ManagedRunError) as exc:
        checks.add("run_not_complete", False, f"the run state cannot be read: {exc}")
    else:
        if state is None:
            checks.add("run_not_complete", False, "the run has no run state yet")
        elif state.status is not PlanRunStatus.COMPLETE:
            checks.add("run_not_complete", False, f"the run is {state.status.value}, not complete")
        else:
            try:
                accepted, detail, candidate, siblings = _final_candidate(record, state)
            except Exception as exc:  # noqa: BLE001 -- any unreadable input or stage is "not complete"
                accepted, detail = False, f"the run's stages cannot be read: {exc}"
            checks.add("run_not_complete", accepted, f"the run is complete; {detail}" if accepted else detail)
    pending = []
    if state is not None:
        if state.awaiting is not None:
            pending.append("the run is awaiting a person")
        if state.unresolved_deferred:
            pending.append(f"{len(state.unresolved_deferred)} deferred human check(s) unresolved")
        if state.evidence_pending is not None:
            pending.append("recorded evidence has not been reviewed")
    checks.add("human_gate_pending", not pending, "; ".join(pending) or "no human gate is pending")
    git["final_candidate"] = candidate

    # -- the worktree -----------------------------------------------------------
    present = worktree.is_dir()
    entries = managed_run.worktree_list(repo_root)
    entry = next((e for e in entries if e["path"] == str(worktree.resolve())), None) if present else None
    if present and lock_held:
        checks.add("runner_live", True, "this finish holds the worktree lock")
    elif present:
        try:
            with worktree_lock(worktree):
                live = None
        except WorktreeLockError as exc:
            live = str(exc)
        checks.add("runner_live", live is None, live or "no runner holds the worktree lock")
    else:
        checks.add("runner_live", True, "no worktree, so no runner")
    if entry is None:
        checks.add(
            "worktree_missing", False,
            f"{worktree} does not exist" if not present else f"{worktree} is not a registered worktree of this repository",
        )
    else:
        checks.add("worktree_missing", True, f"{worktree} is a registered worktree")
        checks.add(
            "branch_mismatch", entry["branch"] == record.branch,
            f"{record.branch} is checked out" if entry["branch"] == record.branch
            else f"{entry['branch'] or 'a detached HEAD'} is checked out, not {record.branch}",
        )
        try:
            dirty = _status(worktree, all_untracked=True)
        except ManagedRunError as exc:
            dirty, dirty_error = (), str(exc)
        else:
            dirty_error = None
        git["clean"] = not dirty and dirty_error is None
        checks.add(
            "worktree_dirty", git["clean"],
            dirty_error or ("the worktree is clean" if not dirty
                            else "uncommitted: " + ", ".join(path for _, path in dirty[:10])),
        )
        git["head"] = _rev(worktree, "HEAD")
        untraversable = _untraversable(record)
        try:
            if untraversable:
                unarchivable = untraversable
            else:
                unarchivable = _unarchivable(record, _project_state_files(record))
                deleted_ignored = _deleted_ignored(record)
        except (ManagedRunError, OSError) as exc:
            checks.add("unarchived_project_state", False, f"the worktree's ignored files cannot be listed: {exc}")
        else:
            checks.add(
                "unarchived_project_state", not unarchivable,
                f"every ignored file under {record.project_dir} can be archived" if not unarchivable
                else f"these files under {record.project_dir} cannot be archived, so removal would lose them: "
                + ", ".join(unarchivable[:10]),
            )
    git["branch_tip"] = _rev(repo_root, f"refs/heads/{record.branch}")

    agreed = candidate is not None and git["head"] == git["branch_tip"] == candidate
    checks.add(
        "candidate_mismatch", agreed,
        f"HEAD, {record.branch} and the final accepted candidate are {candidate}" if agreed
        else f"HEAD {git['head']}, branch tip {git['branch_tip']}, final accepted candidate {candidate} differ",
    )
    # I-L2b at finish: every candidate the run accepted here is an ancestor
    # of the one commit the finish integrates. Nothing remote is read.
    try:
        history = run_accepted(record.sparring_dir, record.run_key)
    except (RunHistoryError, ManagedRunError) as exc:
        history, history_error = (), f"the run's accepted history cannot be read: {exc}"
    else:
        history_error = None
    if candidate is not None:
        _history_check(checks, repo_root, history, history_error, None, candidate)

    others = [e["path"] for e in entries if e["branch"] == record.branch and e["path"] != str(worktree.resolve())]
    checks.add(
        "branch_in_use", not others,
        f"{record.branch} is also checked out at " + ", ".join(others) if others
        else f"no other worktree has {record.branch} checked out",
    )

    # -- the target ---------------------------------------------------------------
    target_tip = _rev(repo_root, f"refs/heads/{record.target_branch}")
    git["target_tip"] = target_tip
    target_at = next((e["path"] for e in entries if e["branch"] == record.target_branch), None)
    git["target_checked_out_at"] = target_at
    if target_at is not None:
        try:
            tracked = [path for code, path in _status(Path(target_at)) if code not in ("??", "!!")]
            target_error = None
        except ManagedRunError as exc:
            tracked, target_error = [], str(exc)
        checks.add(
            "target_checkout_dirty", not tracked and target_error is None,
            target_error or (f"{target_at} has staged or modified files: " + ", ".join(tracked[:10]) if tracked
                             else f"{target_at} has no staged or modified tracked files"),
        )
        try:
            operations = _operations_in_progress(Path(target_at))
        except ManagedRunError as exc:
            operations = [str(exc)]
        checks.add(
            "target_operation_in_progress", not operations,
            f"{target_at} has {', '.join(operations)} in progress" if operations
            else f"{target_at} has no merge, cherry-pick, revert, rebase or bisect in progress",
        )
    else:
        checks.add("target_checkout_dirty", True, f"{record.target_branch} is not checked out anywhere")

    merge_mode: str | None = None
    if target_tip is None:
        checks.add("target_advanced", False, f"target branch {record.target_branch} does not exist")
    elif candidate is not None and _rev(repo_root, candidate) is not None:
        git["target_contains_candidate"] = _is_ancestor(repo_root, candidate, target_tip)
        if git["target_contains_candidate"]:
            merge_mode = "already_merged"
        elif _is_ancestor(repo_root, target_tip, candidate):
            merge_mode = "fast_forward"
        else:
            merge_mode = "merge_commit"
        if merge_mode == "merge_commit":
            checks.add(
                "target_advanced", allow_merge_commit,
                f"{record.target_branch} moved past {record.base_sha[:12]} and is not an ancestor of the candidate; "
                + ("a merge commit is allowed" if allow_merge_commit else "only a merge commit can finish it (--allow-merge-commit)"),
            )
            simulated = managed_run._git(
                repo_root, "merge-tree", "--write-tree", "--no-messages", target_tip, candidate
            )
            if simulated.returncode == 0:
                checks.add("merge_conflict", True, "the merge has no conflicts")
            else:
                checks.add(
                    "merge_conflict", False,
                    "the merge conflicts" if simulated.returncode == 1
                    else f"the merge could not be simulated: {simulated.stderr.strip()}",
                )
        else:
            checks.add("target_advanced", True, f"{record.target_branch} has not diverged from the candidate")
            checks.add("merge_conflict", True, "no merge commit is needed")

    # -- the siblings ---------------------------------------------------------------
    snapshots = _snapshots_of(state)
    for repository, root in siblings:
        git["siblings"].append(_sibling_status(
            checks, record, repository, root, history, history_error,
            allow_merge_commit=allow_merge_commit, snapshots=snapshots,
        ))

    # -- no remote finish ---------------------------------------------------------
    policy = _finish_policy(repo_root, record, candidate)
    checks.add(
        REMOTE_FINISH_REMOVED, not policy["delete_remote_branch"],
        delete_remote_branch_removed(policy["source"]) if policy["delete_remote_branch"]
        else LOCAL_FINISH_DETAIL,
    )

    if not all(item["ok"] for item in checks.items if item["code"] not in CLEANUP_CHECKS):
        return {"git": git, "finish": _finish(
            record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
            deleted_ignored=deleted_ignored,
        )}

    # Every merge check passed: after the planned merge the target contains the candidate.
    cleanup = checks.all_ok and merge_mode in MERGE_MODES
    actions: list[str] = []
    if merge_mode == "fast_forward":
        actions.append(f"fast-forward {record.target_branch} to {candidate}")
    elif merge_mode == "merge_commit":
        actions.append(f"merge {candidate} into {record.target_branch} with a merge commit")
    for sibling in git["siblings"]:
        where = f"{record.target_branch} of sibling repository {sibling['name']!r}"
        if sibling["merge_mode"] == "fast_forward":
            actions.append(f"fast-forward {where} to {sibling['candidate']}")
        elif sibling["merge_mode"] == "merge_commit":
            actions.append(f"merge {sibling['candidate']} into {where} with a merge commit")
    if policy["remove_plan"]:
        actions.append(
            f"remove the plan {record.input_path} from {record.target_branch} in a commit of its own, "
            "only if its bytes match the run's start snapshot"
        )
    if cleanup:
        actions += [
            f"archive every ignored file under {record.project_dir} of run {record.run_key}",
            f"remove worktree {record.worktree_path}",
            f"delete local branch {record.branch}",
        ]
    return {"git": git, "finish": _finish(
        record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
        eligible_merge=True, eligible_cleanup=cleanup, actions=actions,
        deleted_ignored=deleted_ignored,
    )}


def _snapshots_of(state: Any) -> Any:
    """The trusted snapshots ``state`` holds, or ``None`` when it holds none
    that can be read (the finish's guard refuses that case on its own)."""

    if state is None:
        return None
    try:
        return trusted_snapshots.from_run_state(state.to_dict())
    except trusted_snapshots.SnapshotError:
        return None


def _history_check(checks: _Checks, root: Path, history: tuple[Any, ...], error: str | None,
                   repository: Any, candidate: str) -> bool:
    """Add one repository's ``run_history_rewritten`` check (I-L2b): every
    candidate the run accepted in it is an ancestor of (or equal to)
    ``candidate``, the commit the finish integrates there."""

    label = describe_repository(repository)
    if error is not None:
        return checks.add(RUN_HISTORY_REWRITTEN, False, f"{label}: {error}")
    key = (None, None) if repository is None else (repository.name, repository.path)
    for entry in history:
        if entry.key == key and not _is_ancestor(root, entry.sha, candidate):
            return checks.add(
                RUN_HISTORY_REWRITTEN, False,
                f"{label}: the candidate accepted by stage {entry.stage_id!r} ({entry.sha}) is not an "
                f"ancestor of {candidate}, the commit this finish would integrate; the run's accepted "
                "history was rewritten, so nothing is merged",
            )
    return checks.add(
        RUN_HISTORY_REWRITTEN, True,
        f"{label}: every candidate the run accepted is an ancestor of {candidate}",
    )


def _sibling_status(checks: _Checks, record: ManagedRunRecord, repository: Any, root: Path,
                    history: tuple[Any, ...], history_error: str | None, *,
                    allow_merge_commit: bool, snapshots: Any) -> dict[str, Any]:
    """Check one sibling repository's local merge of its accepted candidate
    into its own ``record.target_branch``, adding ``run_history_rewritten``
    and ``sibling_merge`` checks; returns its ``git["siblings"]`` entry."""

    candidate = repository.candidate_sha
    target_ref = f"refs/heads/{record.target_branch}"
    where = f"{describe_repository(repository)}"
    entry: dict[str, Any] = {
        "name": repository.name, "path": str(root), "candidate": candidate,
        "target_branch": record.target_branch, "target_tip": None,
        "target_checked_out_at": None, "merge_mode": None,
    }
    if snapshots is not None and snapshots.primary is not None and snapshots.for_sibling(root) is None:
        checks.add("sibling_merge", False, f"{where}: {TRUSTED_CONFIG_MISSING}: the run holds no "
                   "trusted configuration snapshot for it, so no Git command runs there")
        return entry
    if not (root / ".git").exists() or _rev(root, candidate) is None:
        checks.add("sibling_merge", False, f"{where}: its accepted candidate {candidate} is not present there")
        return entry
    _history_check(checks, root, history, history_error, repository, candidate)
    target_tip = _rev(root, target_ref)
    entry["target_tip"] = target_tip
    if target_tip is None:
        checks.add("sibling_merge", False, f"{where}: target branch {record.target_branch} does not exist")
        return entry
    try:
        worktrees = managed_run.worktree_list(root)
        target_at = next((e["path"] for e in worktrees if e["branch"] == record.target_branch), None)
        entry["target_checked_out_at"] = target_at
        problems: list[str] = []
        if target_at is not None:
            tracked = [path for code, path in _status(Path(target_at)) if code not in ("??", "!!")]
            if tracked:
                problems.append(f"{target_at} has staged or modified files: " + ", ".join(tracked[:10]))
            operations = _operations_in_progress(Path(target_at))
            if operations:
                problems.append(f"{target_at} has {', '.join(operations)} in progress")
    except ManagedRunError as exc:
        problems = [str(exc)]
    if _is_ancestor(root, candidate, target_tip):
        mode = "already_merged"
    elif _is_ancestor(root, target_tip, candidate):
        mode = "fast_forward"
    else:
        mode = "merge_commit"
        if not allow_merge_commit:
            problems.append(
                f"{record.target_branch} has diverged from {candidate}; only a merge commit can finish it "
                "(--allow-merge-commit)"
            )
        simulated = managed_run._git(root, "merge-tree", "--write-tree", "--no-messages", target_tip, candidate)
        if simulated.returncode != 0:
            problems.append("the merge conflicts" if simulated.returncode == 1
                            else f"the merge could not be simulated: {simulated.stderr.strip()}")
    entry["merge_mode"] = mode
    checks.add(
        "sibling_merge", not problems,
        f"{where}: " + ("; ".join(problems) if problems
                        else f"{record.target_branch} at {target_tip} can take {candidate} ({mode})"),
    )
    return entry


def runs_report(repo_root: Path) -> dict[str, Any]:
    """:func:`managed_run.runs_payload` with each run's ``git`` and ``finish``."""

    payload = managed_run.runs_payload(repo_root)
    for run in payload["runs"]:
        status = finish_status(repo_root, run["run_key"])
        run["git"] = status["git"]
        run["finish"] = status["finish"]
    return payload


# -- execution ------------------------------------------------------------------

FINISH_STEPS = (
    "merge",
    "remove_plan",
    "archive_state",
    "remove_worktree",
    "delete_branch",
    "finished",
)
# The step each recorded event proves done, for reporting a finished run.
# ``target_pushed`` and ``remote_branch_deleted`` are only ever an older
# engine's record of a run it finished, reported as history.
_EVENT_STEPS = (
    ("merged", "merge"),
    ("plan_removed", "remove_plan"),
    ("plan_removal_refused", "remove_plan"),
    ("target_pushed", "push_target"),
    ("state_archived", "archive_state"),
    ("worktree_removed", "remove_worktree"),
    ("branch_deleted", "delete_branch"),
    ("remote_branch_deleted", "delete_remote_branch"),
    ("finished", "finished"),
)


def _planned_steps(*, merge_only: bool, remove_plan: bool) -> list[str]:
    steps = ["merge"] + (["remove_plan"] if remove_plan else [])
    if not merge_only:
        steps += ["archive_state", "remove_worktree", "delete_branch", "finished"]
    return steps
ARCHIVE_SUBDIR = Path("agent-sparring") / "runs"


class _StepFailed(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


def _events(record: ManagedRunRecord, name: str) -> list[dict[str, Any]]:
    return [event for event in record.events if event["event"] == name]


def archive_dir(repo_root: Path, run_key: str) -> Path:
    """``<git-common-dir>/agent-sparring/runs/<run_key>``."""

    managed_run._check_run_key(run_key)
    return managed_run.git_common_dir(repo_root) / ARCHIVE_SUBDIR / run_key


def _digests(root: Path, files: list[str]) -> dict[str, str]:
    """``{relative path: sha256}`` of ``files`` under ``root``; a symlink by its target."""

    digests: dict[str, str] = {}
    for rel in files:
        path = root / rel
        if path.is_symlink():
            digests[rel] = "symlink:" + os.readlink(path)
        else:
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _tree_digests(root: Path) -> dict[str, str]:
    """:func:`_digests` of every file and symlink under ``root``."""

    files = [
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink() or path.is_file()
    ]
    return _digests(root, sorted(files))


def _unarchived(record: ManagedRunRecord, files: list[str]) -> _StepFailed:
    return _StepFailed(
        "archive_state",
        f"unarchived_project_state: these files under {record.project_dir} cannot be archived and "
        "are left in place: " + ", ".join(files[:10]),
    )


def _archive_state(repo_root: Path, record: ManagedRunRecord) -> tuple[Path, dict[str, str]]:
    """Copy every ignored file under the project directory -- the run state,
    every stage, ``stages/.archive/**``, unreadable stages and anything else
    removal would delete -- into the archive, verify the copy by digest and
    return it with its digests. An existing archive is kept only when it is
    exactly that copy; anything else refuses. Any failure refuses cleanup."""

    destination = archive_dir(repo_root, record.run_key)
    state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
    if not state_path.is_file():
        raise _StepFailed("archive_state", f"the run state {state_path} does not exist")
    untraversable = _untraversable(record)
    if untraversable:
        raise _unarchived(record, untraversable)
    try:
        files = _project_state_files(record)
    except ManagedRunError as exc:
        raise _StepFailed("archive_state", f"unarchived_project_state: {exc}") from exc
    unarchivable = _unarchivable(record, files)
    if unarchivable:
        raise _unarchived(record, unarchivable)
    staging = destination.parent / f".{record.run_key}.tmp-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
        for rel in files:
            source, copy = record.sparring_dir / rel, staging / rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), copy)
            else:
                shutil.copy2(source, copy)
        expected = _digests(record.sparring_dir, files)
        if _tree_digests(staging) != expected:
            raise _StepFailed("archive_state", "unarchived_project_state: the copy does not match the project state")
        if destination.exists():
            if _tree_digests(destination) != expected:
                raise _StepFailed(
                    "archive_state",
                    f"unarchived_project_state: {destination} already exists with different content; it is left in place",
                )
        else:
            os.replace(staging, destination)
            if _tree_digests(destination) != expected:
                raise _StepFailed(
                    "archive_state", f"unarchived_project_state: the archive at {destination} does not match the project state"
                )
    except OSError as exc:
        raise _StepFailed("archive_state", f"unarchived_project_state: {type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination, expected


def _verify_archived(record: ManagedRunRecord, archived: dict[str, str]) -> None:
    """Immediately before removal: every ignored file now under the project
    directory is in the archive with the same digest, else nothing is removed."""

    try:
        files = _project_state_files(record)
        untraversable = _untraversable(record)
        current = _digests(record.sparring_dir, files)
    except (OSError, ManagedRunError) as exc:
        raise _StepFailed("remove_worktree", f"unarchived_project_state: {exc}") from exc
    missing = untraversable + [rel for rel, digest in current.items() if archived.get(rel) != digest]
    if missing:
        raise _StepFailed(
            "remove_worktree",
            "unarchived_project_state: changed or new since archiving, so nothing is removed: " + ", ".join(missing[:10]),
        )


def _archive_readable(repo_root: Path, record: ManagedRunRecord) -> bool:
    from agent_sparring.plan import PlanRunState

    path = archive_dir(repo_root, record.run_key) / "plans" / f"{record.run_key}.json"
    try:
        PlanRunState.load(path)
    except Exception:  # noqa: BLE001 -- unreadable is "not archived"
        return False
    return True


def _worktree_gone(repo_root: Path, record: ManagedRunRecord) -> bool:
    path = str(Path(record.worktree_path).resolve())
    return not Path(record.worktree_path).exists() and all(
        entry["path"] != path for entry in managed_run.worktree_list(repo_root)
    )


def _candidate_config(repo_root: Path, record: ManagedRunRecord, candidate: str) -> tuple[Any, str]:
    """``(the reviewed candidate's own project config or None, detail)``: the
    configuration the finished run was reviewed with, never a checkout's copy."""

    from agent_sparring.config import CONFIG_FILENAME, ProjectConfigError, parse_project_config

    spec = f"{candidate}:{(Path(record.project_dir) / CONFIG_FILENAME).as_posix()}"
    result = managed_run._git_bytes(repo_root, "show", spec)
    if result.returncode != 0:
        return None, f"{spec} cannot be read"
    try:
        return parse_project_config(result.stdout, source=spec), spec
    except ProjectConfigError as exc:
        return None, str(exc)


def _finish_policy(repo_root: Path, record: ManagedRunRecord, candidate: str | None) -> dict[str, Any]:
    """The candidate's ``[finish]`` settings; both false when unreadable,
    with ``error`` saying why (reported as ``finish_config_unreadable``).
    ``delete_remote_branch`` is read only to refuse it
    (``remote_finish_removed``); ``source`` names the config it came from."""

    config, detail = _candidate_config(repo_root, record, candidate) if candidate else (None, "no candidate")
    return {
        "delete_remote_branch": bool(config is not None and config.finish_delete_remote_branch),
        "remove_plan": bool(config is not None and config.finish_remove_plan),
        "error": None if config is not None or not candidate else detail,
        "source": detail if config is not None else None,
    }


def _merge(record: ManagedRunRecord, git: dict[str, Any], merge_mode: str, repo_root: Path) -> dict[str, Any]:
    return _merge_into(
        repo_root, target_branch=record.target_branch, source_branch=record.branch,
        candidate=git["final_candidate"], old_tip=git["target_tip"],
        checkout=git["target_checked_out_at"], merge_mode=merge_mode,
    )


def _merge_into(repo_root: Path, *, target_branch: str, source_branch: str, candidate: str,
                old_tip: str, checkout: str | None, merge_mode: str) -> dict[str, Any]:
    """Merge ``candidate`` into ``target_branch`` of the repository at
    ``repo_root`` exactly as the checks saw it: in its checkout when one has
    it, else as a compare-and-swap of the ref. Local only."""

    target_ref = f"refs/heads/{target_branch}"
    now_at = next(
        (e["path"] for e in managed_run.worktree_list(repo_root) if e["branch"] == target_branch), None
    )
    if now_at != checkout:
        raise _StepFailed(
            "merge", f"target_moved: {target_branch} is now checked out at {now_at or 'no checkout'}, "
            f"not {checkout or 'no checkout'}; nothing was merged",
        )
    if checkout is not None:
        where = Path(checkout)
        # Immediately before writing: the person may have started an
        # operation, switched branch or committed since the checks ran.
        operations = _operations_in_progress(where)
        if operations:
            raise _StepFailed(
                "merge", f"target_operation_in_progress: {where} has {', '.join(operations)} in progress; "
                "it is left intact and nothing was merged",
            )
        head_ref = managed_run._git(where, "symbolic-ref", "-q", "HEAD").stdout.strip()
        head = _rev(where, "HEAD")
        if head_ref != target_ref or head != old_tip:
            raise _StepFailed(
                "merge", f"target_moved: {where} is at {head_ref or 'a detached HEAD'} {head}, "
                f"not {target_ref} {old_tip} as checked; nothing was merged",
            )
        _refuse_untracked_collisions(where, old_tip, candidate, merge_mode)
        if merge_mode == "fast_forward":
            result = managed_run._git(where, "merge", "--ff-only", "--no-overwrite-ignore", candidate)
        else:
            result = managed_run._git(where, "merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", candidate)
        if result.returncode != 0:
            # No operation was in progress before it, so a MERGE_HEAD now is
            # this merge's own -- never a merge the person started.
            if merge_mode == "merge_commit" and managed_run._git(
                where, "rev-parse", "--verify", "--quiet", "MERGE_HEAD"
            ).returncode == 0:
                managed_run._git(where, "merge", "--abort")
            raise _StepFailed("merge", f"git merge in {where} failed: {result.stderr.strip() or result.stdout.strip()}")
    else:
        new_tip = candidate
        if merge_mode == "merge_commit":
            tree = managed_run._git(repo_root, "merge-tree", "--write-tree", "--no-messages", old_tip, candidate)
            if tree.returncode != 0:
                raise _StepFailed("merge", "the merge conflicts" if tree.returncode == 1 else tree.stderr.strip())
            commit = managed_run._git(
                repo_root, "commit-tree", tree.stdout.split()[0], "-p", old_tip, "-p", candidate,
                "-m", f"Merge branch '{source_branch}' into {target_branch}",
            )
            if commit.returncode != 0:
                raise _StepFailed("merge", f"git commit-tree failed: {commit.stderr.strip()}")
            new_tip = commit.stdout.strip()
        result = managed_run._git(repo_root, "update-ref", target_ref, new_tip, old_tip)
        if result.returncode != 0:
            raise _StepFailed("merge", f"{target_ref} moved; nothing was merged ({result.stderr.strip()})")
    target_sha = _rev(repo_root, target_ref)
    if target_sha is None or not _is_ancestor(repo_root, candidate, target_sha):
        raise _StepFailed("merge", f"{target_branch} does not contain {candidate} after the merge")
    return {"mode": merge_mode, "target_sha": target_sha, "candidate": candidate}


def _refuse_untracked_collisions(checkout: Path, old_tip: str, candidate: str, merge_mode: str) -> None:
    """Refuse a merge that would write over an untracked or ignored file.

    ``--no-overwrite-ignore`` is passed too, but git's merge-commit path does
    not honour it for every backend; the user's files are never ours to
    replace, so check every path the merge would write ourselves."""

    new = candidate
    if merge_mode == "merge_commit":
        tree = managed_run._git(checkout, "merge-tree", "--write-tree", "--no-messages", old_tip, candidate)
        if tree.returncode != 0:
            raise _StepFailed("merge", "the merge conflicts" if tree.returncode == 1 else tree.stderr.strip())
        new = tree.stdout.split()[0]
    changed = managed_run._git_out_raw(checkout, "diff", "--name-only", "--no-renames", "-z", old_tip, new)
    tracked = set(managed_run._git_out_raw(checkout, "ls-tree", "-r", "-z", "--name-only", old_tip).split("\0"))
    collisions: list[str] = []
    for path in filter(None, changed.split("\0")):
        parts = Path(path).parts
        for depth in range(1, len(parts) + 1):
            prefix = Path(*parts[:depth])
            on_disk = checkout / prefix
            is_leaf = depth == len(parts)
            if not (on_disk.exists() or on_disk.is_symlink()):
                break
            if (is_leaf or not on_disk.is_dir() or on_disk.is_symlink()) and prefix.as_posix() not in tracked:
                collisions.append(prefix.as_posix())
                break
    if collisions:
        raise _StepFailed(
            "merge",
            f"the merge would overwrite untracked or ignored files in {checkout}: " + ", ".join(sorted(collisions)[:10]),
        )


def _outcome(code: str | None, detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, **extra}


def _report(run_key: str, completed: list[str], stopped_at: str | None, reason: str | None,
            planned: list[str], *, deleted_ignored: list[str] | None = None,
            kept: list[dict[str, str]] | None = None, plan_removal: dict[str, Any] | None = None,
            remote_branch: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": FINISH_REPORT_SCHEMA_VERSION,
        "run_key": run_key,
        "completed_steps": completed,
        "stopped_at": stopped_at,
        "reason": reason,
        "remaining": [step for step in planned if step not in completed],
        "deleted_ignored_paths": deleted_ignored or [],
        "kept": kept or [],
        "plan_removal": plan_removal or _outcome(None, "not reached"),
        "remote_branch": remote_branch or _outcome(None, "not reached"),
    }


def _owned(record: ManagedRunRecord | None) -> bool:
    return record is not None and record.created_by == "engine" and record.owns_git_state


def finish_run(
    repo_root: Path,
    run_key: str,
    *,
    merge_only: bool = False,
    allow_merge_commit: bool = False,
    push_target: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Execute a finish: ``(report, finish block when the checks refused)``.

    Local only (I-L1): ``push_target`` refuses with ``remote_finish_removed``
    before anything is read or changed (see :mod:`agent_sparring.capabilities`).

    Holds the worktree lock throughout, re-reads the record under it and
    re-runs every eligibility check itself. Each step is skipped when its
    event is recorded and still true, appends its event when done, and the
    first failure stops everything after it. The opt-in step
    (``remove_plan``) is planned once the merged candidate -- whose own
    config enables it -- is known."""

    planned = _planned_steps(merge_only=merge_only, remove_plan=False)
    completed: list[str] = []
    notes: dict[str, Any] = {
        "deleted_ignored": [], "kept": [], "plan_removal": None,
        "remote_branch": _outcome(LOCAL_FINISH, LOCAL_FINISH_DETAIL),
    }
    if push_target:
        return refused_push_target(run_key, merge_only=merge_only), None
    try:
        record = managed_run.read_record(repo_root, run_key)
    except (ManagedRunError, OSError) as exc:
        return _report(run_key, completed, "checks", f"unmanaged: {exc}", planned), None
    if not _owned(record):
        status = finish_status(repo_root, run_key)
        return _report(run_key, completed, "checks", status["finish"]["summary"], planned), status["finish"]

    def report(stopped_at: str | None, reason: str | None) -> dict[str, Any]:
        return _report(run_key, completed, stopped_at, reason, planned,
                       deleted_ignored=notes["deleted_ignored"], kept=notes["kept"],
                       plan_removal=notes["plan_removal"], remote_branch=notes["remote_branch"])

    try:
        with worktree_lock(Path(record.worktree_path)):
            # Only the record read under the lock is used from here on: a
            # finish or edit that landed before the lock is seen.
            try:
                locked = managed_run.read_record(repo_root, run_key)
            except (ManagedRunError, OSError) as exc:
                return report("checks", f"unmanaged: {exc}"), None
            if not _owned(locked) or locked.worktree_path != record.worktree_path:
                status = finish_status(repo_root, run_key, lock_held=True)
                return report("checks", status["finish"]["summary"]), status["finish"]
            if _events(locked, "finished"):
                # Report only what the record shows actually happened.
                recorded = {event["event"] for event in locked.events}
                completed[:] = [step for event, step in _EVENT_STEPS if event in recorded]
                planned[:] = list(completed)
                notes["plan_removal"] = _recorded_plan_removal(locked)
                notes["remote_branch"] = _recorded_remote_branch(locked)
                return report(None, "the run is already finished"), None
            # Every Git invocation of the finish, in the primary repository
            # and in each sibling, is held to that repository's trusted
            # configuration snapshot: a changed configuration is a typed
            # refusal, never followed.
            with _run_guard(repo_root, locked):
                return _finish_locked(repo_root, locked, planned, completed, notes, merge_only=merge_only,
                                      allow_merge_commit=allow_merge_commit)
    except WorktreeLockError as exc:
        return report("checks", f"runner_live: {exc}"), None
    except _StepFailed as exc:
        return report(exc.step, exc.reason), None
    except (ManagedRunError, OSError) as exc:
        # An operational failure (git, disk, permissions, a record write)
        # stops at the step in progress; later steps stay untouched.
        stopped = next((step for step in planned if step not in completed), None)
        return report(stopped, f"{type(exc).__name__}: {exc}"), None


def _run_guard(repo_root: Path, record: ManagedRunRecord):
    """The trusted-configuration guard of ``record``'s run, from the
    snapshots in its engine-written plan-run state (in the worktree, or in
    the archive once the worktree is gone): its own local-first snapshots,
    or for a run recorded before them the trusted snapshot its legacy pin
    carries -- the local configuration only; the pin's remote destination
    is never read (I-L1). Every engine Git command of the finish, in the
    primary repository and in each sibling, is held to its repository's.

    Fails closed, with the run-state integrity refusal
    (``trusted_config_missing``) before any Git invocation: a copy that
    exists but cannot be read is never taken to mean "no snapshot", and a
    run the engine's record outside the project directory says held one
    (its snapshot record, or a legacy pin record) whose snapshot is gone --
    state deleted, pin removed or damaged, or replaced with a legacy-shaped
    file -- refuses. Only a run whose engine record proves it predates both
    finishes under the hardened Git path alone.
    """

    roots: list[tuple[Path, str]] = []
    try:
        roots.append((record.sparring_dir, "checks"))
    except ManagedRunError:
        pass
    roots.append((archive_dir(repo_root, record.run_key), "archive_state"))
    unreadable: list[tuple[str, str]] = []
    for root, step in roots:
        path = Path(root) / "plans" / f"{record.run_key}.json"
        if not os.path.lexists(path):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("it is not a JSON object")
            snapshots = trusted_snapshots.from_run_state(payload)
        except (OSError, ValueError, trusted_snapshots.SnapshotError) as exc:
            unreadable.append((step, f"{path}: {exc}"))
            continue
        if snapshots.primary is not None:
            return _guarded(repo_root, record, snapshots)
    nothing = "refusing to finish without it -- nothing was merged or deleted by this finish"
    if unreadable:
        step, detail = unreadable[0]
        raise _StepFailed(
            step,
            f"{TRUSTED_CONFIG_MISSING}: the run state holding this run's trusted configuration "
            f"snapshot cannot be read ({detail}); {nothing}",
        )
    if record.pinned or trusted_snapshots.engine_recorded(repo_root, record.run_key):
        raise _StepFailed(
            "checks",
            f"{TRUSTED_CONFIG_MISSING}: the engine's record says this run held a trusted "
            "configuration snapshot (its own, or a legacy pin's), but none can be found in its "
            f"run state (removed, damaged or replaced); {nothing}",
        )
    # Readable without a snapshot, or absent, and the engine's record
    # agrees: a run recorded before pins and snapshots.
    return contextlib.nullcontext()


@contextlib.contextmanager
def _guarded(repo_root: Path, record: ManagedRunRecord, snapshots: Any):
    with contextlib.ExitStack() as stack:
        stack.enter_context(engine_git.trusted_config(
            [Path(repo_root), Path(record.worktree_path)], snapshots.primary
        ))
        for path, entries in snapshots.siblings.items():
            stack.enter_context(engine_git.trusted_config([Path(path)], entries))
        yield snapshots


def _recorded_plan_removal(record: ManagedRunRecord) -> dict[str, Any]:
    removed = _events(record, "plan_removed")
    if removed:
        detail = removed[-1]["detail"]
        return _outcome("plan_removed", f"removed {detail.get('path')}", commit=detail.get("commit"))
    refused = _events(record, "plan_removal_refused")
    if refused:
        detail = refused[-1]["detail"]
        return _outcome(detail.get("code"), f"recorded refusal (final): {detail.get('detail', '')}")
    return _outcome(None, "no plan removal is recorded")


def _recorded_remote_branch(record: ManagedRunRecord) -> dict[str, Any]:
    """What a finished run's record says of its remote branch: an older
    engine's recorded deletion, as history, else that the finish is local."""

    deleted = _events(record, "remote_branch_deleted")
    if deleted:
        detail = deleted[-1]["detail"]
        return _outcome(detail.get("code", "remote_branch_deleted"), detail.get("detail", "recorded"))
    return _outcome(LOCAL_FINISH, LOCAL_FINISH_DETAIL)


def refused_push_target(run_key: str, *, merge_only: bool = False) -> dict[str, Any]:
    """The ``finish-run`` report of ``--push-target``: refused with
    ``remote_finish_removed`` before anything is read or changed."""

    return _report(
        run_key, [], "checks", push_target_removed(), _planned_steps(merge_only=merge_only, remove_plan=False),
        remote_branch=_outcome(LOCAL_FINISH, LOCAL_FINISH_DETAIL),
    )


def _finish_locked(
    repo_root: Path, record: ManagedRunRecord, planned: list[str], completed: list[str],
    notes: dict[str, Any], *, merge_only: bool, allow_merge_commit: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_key = record.run_key

    def report(stopped_at: str | None, reason: str | None) -> dict[str, Any]:
        return _report(run_key, completed, stopped_at, reason, planned,
                       deleted_ignored=notes["deleted_ignored"], kept=notes["kept"],
                       plan_removal=notes["plan_removal"], remote_branch=notes["remote_branch"])

    def plan_steps(candidate: str) -> dict[str, bool]:
        policy = _finish_policy(repo_root, record, candidate)
        if policy["delete_remote_branch"]:
            raise _StepFailed("checks", delete_remote_branch_removed(policy["source"]))
        planned[:] = _planned_steps(merge_only=merge_only, remove_plan=policy["remove_plan"])
        unreadable = policy["error"] and _outcome(
            "finish_config_unreadable", f"the candidate's [finish] config cannot be read: {policy['error']}"
        )
        if not policy["remove_plan"]:
            notes["plan_removal"] = unreadable or _outcome(
                "plan_removal_disabled", "[finish] remove_plan is not enabled"
            )
        return policy

    removed = bool(_events(record, "worktree_removed"))
    if removed and not _worktree_gone(repo_root, record):
        raise _StepFailed("remove_worktree", f"{record.worktree_path} was recorded removed but exists again")
    if (
        not removed
        and _events(record, "merged")
        and _events(record, "state_archived")
        and _worktree_gone(repo_root, record)
        and _archive_readable(repo_root, record)
    ):
        # Removal succeeded but its event was never persisted (interrupted):
        # the record proves the merge and the archive, git proves the
        # worktree is gone, so record what is already true.
        record = managed_run.append_event(repo_root, run_key, "worktree_removed", {"reconciled": True})
        removed = True
    if not removed and not merge_only and managed_run.worktree_top(repo_root) == Path(record.worktree_path).resolve():
        return report(
            "checks", "finish-run is running inside the managed worktree it would remove; run it from another checkout",
        ), None

    if removed:
        # Every eligibility check is about the worktree, which is gone; what
        # remains needs only the candidate the recorded merge proved merged.
        merged = _events(record, "merged")
        candidate = merged[-1]["detail"].get("candidate") if merged else None
        if not candidate:
            raise _StepFailed("merge", "the record has no merged candidate")
        target_tip = _rev(repo_root, f"refs/heads/{record.target_branch}")
        if target_tip is None or not _is_ancestor(repo_root, candidate, target_tip):
            raise _StepFailed("merge", f"{record.target_branch} no longer contains the merged candidate {candidate}")
        for event in _events(record, "sibling_merged"):
            sibling = event["detail"]
            root, sha = Path(sibling["path"]), sibling["candidate"]
            tip = _rev(root, f"refs/heads/{record.target_branch}")
            if tip is None or not _is_ancestor(root, sha, tip):
                raise _StepFailed(
                    "merge", f"{record.target_branch} of sibling repository {sibling['name']!r} no longer "
                    f"contains the merged candidate {sha}",
                )
        policy = plan_steps(candidate)
        completed.append("merge")
        if policy["remove_plan"]:
            record = _remove_plan_step(repo_root, record, notes)
            completed.append("remove_plan")
        if merge_only:
            return report(None, "--merge-only: cleanup was not requested"), None
        if not _archive_readable(repo_root, record):
            raise _StepFailed("archive_state", "the archived run state is missing or unreadable")
        completed.append("archive_state")
    else:
        status = finish_status(repo_root, run_key, allow_merge_commit=allow_merge_commit, lock_held=True)
        finish, git = status["finish"], status["git"]
        if not finish["eligible"]["merge"] or (not merge_only and not finish["eligible"]["cleanup"]):
            return report("checks", finish["summary"]), finish
        candidate = git["final_candidate"]
        policy = plan_steps(candidate)
        # 1. merge, locally, in the primary repository and then in each sibling
        if finish["merge_mode"] == "already_merged":
            if not _events(record, "merged"):
                record = managed_run.append_event(repo_root, run_key, "merged", {
                    "mode": "already_merged", "target_sha": git["target_tip"], "candidate": candidate,
                })
        else:
            record = managed_run.append_event(
                repo_root, run_key, "merged", _merge(record, git, finish["merge_mode"], repo_root)
            )
        for sibling in git["siblings"]:
            record = _merge_sibling(repo_root, record, sibling)
        completed.append("merge")
        # 2. remove the plan (opt-in; refusals are reported, not stops)
        if policy["remove_plan"]:
            record = _remove_plan_step(repo_root, record, notes)
            completed.append("remove_plan")
        if merge_only:
            return report(None, "--merge-only: cleanup was not requested"), None
        # 3. archive every ignored file under the project directory
        destination, archived = _archive_state(repo_root, record)
        if not _archive_readable(repo_root, record):
            raise _StepFailed("archive_state", "unarchived_project_state: the archived run state is unreadable")
        if not _events(record, "state_archived"):
            record = managed_run.append_event(repo_root, run_key, "state_archived", {"path": str(destination)})
        completed.append("archive_state")
        # 4. remove worktree -- only what is archived or reported may go
        _verify_archived(record, archived)
        deleted_ignored = _deleted_ignored(record)
        result = managed_run._git(repo_root, "worktree", "remove", record.worktree_path)
        if result.returncode != 0 or not _worktree_gone(repo_root, record):
            raise _StepFailed("remove_worktree", f"git worktree remove failed: {result.stderr.strip()}")
        # Only paths this removal actually deleted are reported.
        notes["deleted_ignored"] = deleted_ignored
        record = managed_run.append_event(repo_root, run_key, "worktree_removed")
    completed.append("remove_worktree")

    # 5. delete local branch
    branch_ref = f"refs/heads/{record.branch}"
    tip = _rev(repo_root, branch_ref)
    if tip is not None:
        refused = _foreign_branch(record)
        if refused:
            raise _StepFailed("delete_branch", refused)
        target_tip = _rev(repo_root, f"refs/heads/{record.target_branch}")
        if target_tip is None or not _is_ancestor(repo_root, candidate, target_tip):
            raise _StepFailed("delete_branch", f"{record.target_branch} does not contain {candidate}")
        if tip != candidate:
            raise _StepFailed("delete_branch", f"{record.branch} is at {tip}, not the merged candidate {candidate}")
        checkout = next(
            (e["path"] for e in managed_run.worktree_list(repo_root) if e["branch"] == record.target_branch), None
        )
        if checkout is not None:
            result = managed_run._git(Path(checkout), "branch", "-d", record.branch)
        else:
            result = managed_run._git(repo_root, "update-ref", "-d", branch_ref, candidate)
        if result.returncode != 0:
            raise _StepFailed("delete_branch", f"the branch could not be deleted: {result.stderr.strip()}")
    if not _events(record, "branch_deleted"):
        record = managed_run.append_event(repo_root, run_key, "branch_deleted")
    completed.append("delete_branch")

    # 6. finished
    managed_run.append_event(repo_root, run_key, "finished")
    completed.append("finished")
    return report(None, None), None


def _merge_sibling(repo_root: Path, record: ManagedRunRecord, sibling: dict[str, Any]) -> ManagedRunRecord:
    """Merge one sibling's accepted candidate into its local target, as
    :func:`_sibling_status` checked it, and record ``sibling_merged``. Its
    branch is the plan's, not a managed one: nothing there is deleted."""

    detail = {"name": sibling["name"], "path": sibling["path"], "candidate": sibling["candidate"]}
    recorded = [event["detail"] for event in _events(record, "sibling_merged")]
    if sibling["merge_mode"] == "already_merged":
        if not any(entry.get("path") == sibling["path"] and entry.get("candidate") == sibling["candidate"]
                   for entry in recorded):
            record = managed_run.append_event(repo_root, record.run_key, "sibling_merged", {
                **detail, "mode": "already_merged", "target_sha": sibling["target_tip"],
            })
        return record
    merged = _merge_into(
        Path(sibling["path"]), target_branch=sibling["target_branch"], source_branch=record.branch,
        candidate=sibling["candidate"], old_tip=sibling["target_tip"],
        checkout=sibling["target_checked_out_at"], merge_mode=sibling["merge_mode"],
    )
    return managed_run.append_event(repo_root, record.run_key, "sibling_merged", {**detail, **merged})


def _foreign_branch(record: ManagedRunRecord) -> str | None:
    """Why ``record.branch`` is not one finish may delete, or ``None``: only
    a managed branch -- the run's own, or one in the engine's ``sparring/``
    namespace (a record created under the earlier slug naming) -- and never
    its target. The record lives in the git common dir, so this does not
    trust it to name the branch."""

    try:
        own = managed_run.managed_branch(record.run_key)
    except Exception as exc:  # an invalid run key cannot name a branch
        return f"run key {record.run_key!r} has no managed branch ({exc})"
    managed = record.branch == own or (
        record.branch.startswith("sparring/") and len(record.branch) > len("sparring/")
    )
    if not managed or record.branch == record.target_branch:
        return (
            f"the record names branch {record.branch!r}, not a managed branch (such as {own!r}) "
            "distinct from its target; refusing to delete it"
        )
    return None


# -- plan removal ------------------------------------------------------------------

#: Refusals reported in ``plan_removal`` that do not stop the finish.
PLAN_REMOVAL_REPORTED = (
    "plan_snapshot_missing", "plan_snapshot_mismatch", "plan_not_tracked", "plan_absent", "plan_changed",
    "target_checkout_dirty", "target_operation_in_progress", "git_identity_missing",
)


def _remove_plan_step(repo_root: Path, record: ManagedRunRecord, notes: dict[str, Any]) -> ManagedRunRecord:
    """Run ``remove_plan`` unless its outcome is recorded. A refusal is
    final: it is recorded as ``plan_removal_refused`` and every later resume
    reports it without retrying. Only an unexpected git failure
    (``plan_commit_failed``) stops the finish, and that is retried."""

    if _events(record, "plan_removed") or _events(record, "plan_removal_refused"):
        notes["plan_removal"] = _recorded_plan_removal(record)
        return record
    outcome = _remove_plan(repo_root, record)
    notes["plan_removal"] = outcome
    if outcome["code"] == "plan_commit_failed":
        raise _StepFailed("remove_plan", f"plan_commit_failed: {outcome['detail']}")
    if outcome["code"] == "plan_removed":
        record = managed_run.append_event(repo_root, record.run_key, "plan_removed", {
            "commit": outcome["commit"], "path": outcome["path"], "sha256": outcome["sha256"],
            **({"reconciled": True} if outcome.get("reconciled") else {}),
        })
    else:
        record = managed_run.append_event(repo_root, record.run_key, "plan_removal_refused", {
            "code": outcome["code"], "detail": outcome["detail"],
        })
    return record


def _plan_snapshot(repo_root: Path, record: ManagedRunRecord) -> tuple[bytes, str] | None:
    """The run's start snapshot, from its project state or -- once the
    worktree is gone -- its archive."""

    from agent_sparring.plan import read_plan_snapshot

    for root in (record.sparring_dir, archive_dir(repo_root, record.run_key)):
        snapshot = read_plan_snapshot(root, record.run_key)
        if snapshot is not None:
            return snapshot
    return None


def _git_bytes(cwd: Path, *args: str, env: dict[str, str] | None = None,
               stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    # The one engine Git path; ``env`` is applied over its sanitized
    # environment, so a deliberate GIT_INDEX_FILE here still takes effect.
    try:
        return engine_git.run(cwd, *args, text=False, input=stdin, env=env)
    except engine_git.GitConfigRefusal as exc:
        raise ManagedRunError(exc.code, str(exc)) from exc


def _git_identity(repo_root: Path) -> str | None:
    """Why the user's git identity is unusable, or ``None`` when it is set."""

    missing = []
    for env_name, key in (("GIT_AUTHOR_NAME", "user.name"), ("GIT_AUTHOR_EMAIL", "user.email")):
        if os.environ.get(env_name):
            continue
        if not managed_run._git(repo_root, "config", "--get", key).stdout.strip():
            missing.append(key)
    return ", ".join(missing) + " is not set" if missing else None


def _remove_plan(repo_root: Path, record: ManagedRunRecord) -> dict[str, Any]:
    """Commit the removal of the run's plan file on the target, only when
    the target's bytes are byte-for-byte the run's start snapshot."""

    snapshot = _plan_snapshot(repo_root, record)
    # 1. the repo-relative path: only an input the run read inside its worktree
    try:
        rel = Path(record.input_path).relative_to(record.worktree_path).as_posix()
    except ValueError:
        return _outcome("plan_not_tracked", f"{record.input_path} is not a repository file the run read; it is left")
    # 2. the snapshot
    recorded = managed_run.created_plan_sha256(record)
    if snapshot is None or recorded is None:
        what = "no plan snapshot" if snapshot is None else "no plan digest in its record"
        return _outcome("plan_snapshot_missing", f"run {record.run_key} has {what}; {rel} is left")
    data, digest = snapshot
    # Tamper evidence: the snapshot in the worktree must match the digest
    # recorded outside it when the run was created.
    if digest != recorded or hashlib.sha256(data).hexdigest() != recorded:
        return _outcome(
            "plan_snapshot_mismatch",
            f"the run's plan snapshot does not match the digest in its record; {rel} is left",
        )
    target_ref = f"refs/heads/{record.target_branch}"
    old = _rev(repo_root, target_ref)
    if old is None:
        return _outcome("plan_commit_failed", f"{target_ref} does not exist")
    # 3./4. tracked at the target tip, as a regular file
    listed = _git_bytes(repo_root, "ls-tree", "-z", old, "--", rel)
    if listed.returncode != 0:
        return _outcome("plan_commit_failed", f"git ls-tree failed: {listed.stderr.decode(errors='replace').strip()}")
    entry = listed.stdout.split(b"\0")[0].decode(errors="replace")
    if not entry:
        commit = _recorded_removal_commit(repo_root, record, old, rel, data)
        if commit is not None:
            return _outcome("plan_removed", f"{rel} was removed from {record.target_branch} in {commit}",
                            commit=commit, path=rel, sha256=digest, reconciled=True)
        return _outcome("plan_absent", f"{rel} is not in {record.target_branch} ({old}); nothing to remove")
    if entry.split(" ", 2)[:2] not in (["100644", "blob"], ["100755", "blob"]):
        return _outcome("plan_not_tracked", f"{rel} is not a tracked file in {record.target_branch}")
    # 5. byte-exact
    shown = _git_bytes(repo_root, "cat-file", "blob", f"{old}:{rel}")
    if shown.returncode != 0:
        return _outcome("plan_commit_failed", f"git cat-file failed: {shown.stderr.decode(errors='replace').strip()}")
    if hashlib.sha256(shown.stdout).hexdigest() != digest or shown.stdout != data:
        return _outcome(
            "plan_changed", f"{rel} in {record.target_branch} differs from the run's start snapshot; it is left"
        )
    # 7. identity
    identity = _git_identity(repo_root)
    if identity:
        return _outcome("git_identity_missing", f"git {identity}; {rel} is left")
    message = (
        f"sparring: remove finished plan {record.plan_label} (run {record.run_key})\n\n"
        f"Sparring-Run: {record.run_key}\n"
    )
    checkout = next(
        (e["path"] for e in managed_run.worktree_list(repo_root) if e["branch"] == record.target_branch), None
    )
    # 6. commit
    if checkout is not None:
        new, failure = _commit_removal_in_checkout(Path(checkout), rel, old, message)
    else:
        new, failure = _commit_removal_by_plumbing(repo_root, target_ref, rel, old, message)
    if failure is not None:
        return failure
    return _outcome("plan_removed", f"removed {rel} from {record.target_branch} in {new}",
                    commit=new, path=rel, sha256=digest)


def _recorded_removal_commit(repo_root: Path, record: ManagedRunRecord, tip: str, rel: str,
                             data: bytes) -> str | None:
    """This run's own removal commit on the target, when it was made but its
    ``plan_removed`` event was not recorded (an interrupted finish): the
    latest commit reachable from ``tip`` that carries the run's
    ``Sparring-Run`` trailer, deletes ``rel``, and whose parent held exactly
    the snapshot ``data`` there."""

    listed = managed_run._git(
        repo_root, "log", "--format=%H", "--fixed-strings", f"--grep=Sparring-Run: {record.run_key}",
        "--diff-filter=D", "--no-renames", tip, "--", rel,
    )
    for commit in listed.stdout.split() if listed.returncode == 0 else []:
        trailers = managed_run._git(repo_root, "log", "-1", "--format=%(trailers:key=Sparring-Run,valueonly)", commit)
        if record.run_key not in trailers.stdout.split():
            continue
        parent = _git_bytes(repo_root, "cat-file", "blob", f"{commit}^:{rel}")
        if parent.returncode == 0 and parent.stdout == data:
            return commit
    return None


def _commit_removal_in_checkout(checkout: Path, rel: str, old: str, message: str) -> tuple[str | None, dict | None]:
    """In the checkout that has the target: an empty index and an unmodified
    plan are required; unrelated unstaged edits are left alone, never stashed."""

    operations = _operations_in_progress(checkout)
    if operations:
        return None, _outcome(
            "target_operation_in_progress", f"{checkout} has {', '.join(operations)} in progress; the plan is left"
        )
    if _rev(checkout, "HEAD") != old:
        return None, _outcome("plan_commit_failed", f"{checkout} is not at {old}")
    staged = managed_run._git(checkout, "diff", "--cached", "--quiet")
    plan_changed = managed_run._git(checkout, "diff", "--quiet", "--", rel)
    if staged.returncode != 0 or plan_changed.returncode != 0 or not (checkout / rel).is_file():
        return None, _outcome(
            "target_checkout_dirty",
            f"{checkout} has staged changes or a modified {rel}; the plan is left",
        )
    removed = managed_run._git(checkout, "rm", "-q", "--", rel)
    if removed.returncode != 0:
        return None, _outcome("plan_commit_failed", f"git rm failed: {removed.stderr.strip()}")
    committed = managed_run._git(checkout, "commit", "-q", "--no-verify", "--only", "-m", message, "--", rel)
    new = _rev(checkout, "HEAD")
    if committed.returncode != 0 or new == old:
        # Put back exactly what was removed; the index was empty before.
        managed_run._git(checkout, "reset", "-q", "--", rel)
        managed_run._git(checkout, "checkout", "--", rel)
        return None, _outcome("plan_commit_failed", f"git commit failed: {committed.stderr.strip()}")
    return new, None


def _commit_removal_by_plumbing(repo_root: Path, target_ref: str, rel: str, old: str,
                                message: str) -> tuple[str | None, dict | None]:
    """No checkout has the target: build the commit from ``old``'s tree
    without the plan and compare-and-swap the target from ``old``."""

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        steps = (
            ("read-tree", old),
            ("update-index", "--index-info"),
            ("write-tree",),
        )
        tree = ""
        for args in steps:
            stdin = f"0 {'0' * 40}\t{rel}\n".encode() if args[0] == "update-index" else None
            result = _git_bytes(repo_root, *args, env=env, stdin=stdin)
            if result.returncode != 0:
                return None, _outcome(
                    "plan_commit_failed", f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}"
                )
            tree = result.stdout.decode().strip()
    commit = managed_run._git(repo_root, "commit-tree", tree, "-p", old, "-m", message)
    if commit.returncode != 0:
        return None, _outcome("plan_commit_failed", f"git commit-tree failed: {commit.stderr.strip()}")
    new = commit.stdout.strip()
    swapped = managed_run._git(repo_root, "update-ref", target_ref, new, old)
    if swapped.returncode != 0:
        return None, _outcome("plan_commit_failed", f"{target_ref} moved: {swapped.stderr.strip()}")
    return new, None


# -- prune ------------------------------------------------------------------------

PRUNE_SCHEMA_VERSION = 2
PRUNE_SUMMARY_SCHEMA_VERSION = 1
PRUNE_SUMMARY_NAME = "pruned.jsonl"


def prune_summary_path(repo_root: Path) -> Path:
    """``<git-common-dir>/agent-sparring/runs/pruned.jsonl``."""

    return managed_run.git_common_dir(repo_root) / ARCHIVE_SUBDIR / PRUNE_SUMMARY_NAME


def _legacy_items(repo_root: Path, records: dict[str, ManagedRunRecord]) -> list[dict[str, Any]]:
    """The read-only report as it has always been (schema 1 items)."""

    items: list[dict[str, Any]] = []
    for run_key, record in records.items():
        path = str(managed_run.record_path(repo_root, run_key))
        if record.lifecycle == "finished":
            items.append({
                "kind": "record", "run_key": run_key, "path": path, "reason": "finished",
                "detail": f"run {run_key} is finished: its worktree and branch were removed",
            })
        elif not Path(record.worktree_path).is_dir():
            items.append({
                "kind": "record", "run_key": run_key, "path": path, "reason": "worktree_missing",
                "detail": f"the worktree directory {record.worktree_path} of run {run_key} "
                          f"({record.lifecycle}) no longer exists",
            })
    archives = managed_run.git_common_dir(repo_root) / ARCHIVE_SUBDIR
    if archives.is_dir():
        for entry in sorted(archives.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or not managed_run._RUN_KEY_RE.match(entry.name):
                continue
            record = records.get(entry.name)
            if record is not None and record.lifecycle != "finished":
                continue  # an unfinished finish re-reads its archive on retry: retained
            state = "is finished" if record is not None else "has no record"
            items.append({
                "kind": "archive", "run_key": entry.name, "path": str(entry), "reason": "archived",
                "detail": f"archived project state of run {entry.name}, which {state}; "
                          "no unfinished run reads it",
            })
    return items


def prune_report(repo_root: Path) -> dict[str, Any]:
    """Managed-run state believed unused, each item with why. Never deletes.

    Only engine-recorded state is reported: records (finished, or whose
    worktree directory is gone) and finish archives. Nothing is inferred from
    directory or branch names, so no unrecorded worktree -- an engine snapshot
    among them -- is ever named here."""

    records = {record.run_key: record for record in managed_run.list_records(repo_root)}
    return {
        "schema_version": 1,
        "dry_run": True,
        "items": _legacy_items(repo_root, records),
        "engine_snapshots": [],
    }


def _parse_time(text: str | None) -> float | None:
    from datetime import datetime

    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _finished_at(record: ManagedRunRecord | None, archive: Path | None) -> tuple[float | None, str | None]:
    """When the run finished: its ``finished`` event, else the archive mtime."""

    from datetime import datetime, timezone

    if record is not None:
        finished = _events(record, "finished")
        if finished:
            at = finished[-1].get("at")
            stamp = _parse_time(at)
            if stamp is not None:
                return stamp, at
    if archive is not None and archive.is_dir():
        stamp = archive.stat().st_mtime
        return stamp, datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    return None, None


def _archive_digest(archive: Path) -> str | None:
    if not archive.is_dir():
        return None
    digests = _tree_digests(archive)
    joined = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(digests.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


def _summary_line(repo_root: Path, run_key: str, record: ManagedRunRecord | None, archive: Path,
                  finished_at: str | None, now: str) -> dict[str, Any]:
    from agent_sparring.plan import read_plan_snapshot

    snapshot = read_plan_snapshot(archive, run_key) if archive.is_dir() else None
    line: dict[str, Any] = {
        "schema_version": PRUNE_SUMMARY_SCHEMA_VERSION,
        "run_key": run_key,
        "plan_label": record.plan_label if record else None,
        "input_kind": record.input_kind if record else None,
        "target_branch": record.target_branch if record else None,
        "branch": record.branch if record else None,
        "remote": record.remote if record else None,
        "base_sha": record.base_sha if record else None,
        "final_candidate": None,
        "merge": None,
        "plan_sha256": snapshot[1] if snapshot else None,
        "created_at": record.created_at if record else None,
        "finished_at": finished_at,
        "pruned_at": now,
        "archive_digest": _archive_digest(archive),
    }
    if record is not None:
        merged = _events(record, "merged")
        if merged:
            detail = merged[-1]["detail"]
            line["final_candidate"] = detail.get("candidate")
            line["merge"] = {"mode": detail.get("mode"), "target_sha": detail.get("target_sha")}
        removed = _events(record, "plan_removed")
        if removed:
            line["plan_removed_commit"] = removed[-1]["detail"].get("commit")
        remote = _events(record, "remote_branch_deleted")
        if remote:
            line["remote_branch_code"] = remote[-1]["detail"].get("code", "remote_branch_deleted")
    return line


def _append_summaries(path: Path, lines: list[dict[str, Any]]) -> None:
    """Append and fsync one JSON line per run not already summarised.
    Raises :class:`OSError` (the caller then deletes nothing), or
    :class:`ManagedRunError` ``prune_summary_corrupt`` for an unreadable
    existing summary, which is never rewritten."""

    import json

    existing: set[str] = set()
    if path.is_file():
        try:
            for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if raw.strip():
                    existing.add(str(json.loads(raw)["run_key"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ManagedRunError(
                "prune_summary_corrupt",
                f"{path} cannot be read ({type(exc).__name__}: {exc}); nothing was deleted",
            ) from exc
    fresh = [line for line in lines if line["run_key"] not in existing]
    if not fresh:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with open(path, "a", encoding="utf-8") as handle:
        for line in fresh:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        # Make the new file's directory entry durable too.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def prune(repo_root: Path, *, dry_run: bool, older_than_days: float | None = None,
          keep: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Select finished runs' records and archives by age and/or recency and
    delete them -- or, with ``dry_run``, say what would go.

    Only records whose lifecycle is finished, and archives whose record is
    finished or missing, are eligible; everything else is ``kept`` or
    ``report_only``. Before anything is deleted, one summary line per run is
    appended to ``pruned.jsonl`` and fsynced; if that fails nothing is
    deleted. Deletions run under the records lock with the lifecycle re-read."""

    import time
    from datetime import datetime, timezone

    now = time.time() if now is None else now
    now_text = datetime.fromtimestamp(now, timezone.utc).isoformat()
    records = {record.run_key: record for record in managed_run.list_records(repo_root)}
    archives_root = managed_run.git_common_dir(repo_root) / ARCHIVE_SUBDIR
    archives: dict[str, Path] = {}
    if archives_root.is_dir():
        for entry in sorted(archives_root.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and managed_run._RUN_KEY_RE.match(entry.name):
                archives[entry.name] = entry

    # Candidates per run key: finished records and finished/orphan archives.
    runs: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for run_key in sorted(set(records) | set(archives)):
        record, archive = records.get(run_key), archives.get(run_key)
        finished = record is not None and record.lifecycle == "finished"
        if record is not None and not finished:
            if not Path(record.worktree_path).is_dir():
                items.append({
                    "kind": "record", "run_key": run_key, "path": str(managed_run.record_path(repo_root, run_key)),
                    "action": "report_only", "reason": "worktree_missing",
                    "detail": f"the worktree directory {record.worktree_path} of run {run_key} "
                              f"({record.lifecycle}) no longer exists; never pruned",
                })
            else:
                items.append({
                    "kind": "record", "run_key": run_key, "path": str(managed_run.record_path(repo_root, run_key)),
                    "action": "kept", "reason": "unfinished", "detail": f"run {run_key} is {record.lifecycle}",
                })
            continue
        stamp, stamp_text = _finished_at(record if finished else None, archive)
        runs[run_key] = {"record": record if finished else None, "archive": archive,
                         "stamp": stamp, "finished_at": stamp_text}

    order = sorted(runs, key=lambda key: (runs[key]["stamp"] or 0.0), reverse=True)
    within_keep = set(order[:keep]) if keep is not None else set()
    selected: list[str] = []
    for run_key in order:
        info = runs[run_key]
        reason = None
        if keep is not None and run_key in within_keep:
            reason = "within_keep"
        elif older_than_days is not None and (
            info["stamp"] is None or now - info["stamp"] < older_than_days * 86400
        ):
            reason = "too_recent"
        elif keep is None and older_than_days is None:
            reason = "no_selector"
        info["reason"] = reason
        if reason is None:
            selected.append(run_key)

    def paths(run_key: str) -> list[tuple[str, Path]]:
        info = runs[run_key]
        found = []
        if info["record"] is not None:
            found.append(("record", managed_run.record_path(repo_root, run_key)))
        if info["archive"] is not None:
            found.append(("archive", info["archive"]))
        return found

    summary = prune_summary_path(repo_root)
    deleting = not dry_run and bool(selected)
    if deleting:
        try:
            with _records_lock(repo_root):
                # Re-check under the lock: a run unfinished now is never touched.
                current = {record.run_key: record for record in managed_run.list_records(repo_root)}
                for run_key in list(selected):
                    record = current.get(run_key)
                    if record is not None and record.lifecycle != "finished":
                        selected.remove(run_key)
                        runs[run_key]["reason"] = "unfinished"
                lines = [
                    _summary_line(repo_root, key, current.get(key),
                                  runs[key]["archive"] or archive_dir(repo_root, key), runs[key]["finished_at"], now_text)
                    for key in selected
                ]
                try:
                    _append_summaries(summary, lines)
                except OSError as exc:
                    for run_key in selected:
                        runs[run_key]["reason"] = "delete_failed"
                        runs[run_key]["error"] = f"the prune summary could not be written: {exc}"
                    selected = []
                for run_key in selected:
                    for kind, path in paths(run_key):
                        try:
                            if kind == "archive":
                                if path.parent != archives_root or not managed_run._RUN_KEY_RE.match(path.name):
                                    raise OSError(f"{path} is not a run archive")
                                shutil.rmtree(path)
                            else:
                                path.unlink()
                        except OSError as exc:
                            runs[run_key]["reason"] = "delete_failed"
                            runs[run_key]["error"] = str(exc)
        except _PruneLockError as exc:
            raise ManagedRunError("prune_locked", str(exc)) from exc

    for run_key in order:
        info = runs[run_key]
        reason = info["reason"]
        for kind, path in paths(run_key):
            if reason is None:
                action, why = ("would_delete" if dry_run else "deleted"), "finished"
                detail = f"run {run_key} finished at {info['finished_at']}"
            elif reason == "delete_failed":
                action, why, detail = "kept", "delete_failed", info.get("error", "")
            else:
                action, why = "kept", reason
                detail = {
                    "within_keep": f"one of the {keep} most recently finished runs",
                    "too_recent": f"finished at {info['finished_at']}, within {older_than_days} day(s)",
                    "unfinished": f"run {run_key} is no longer finished",
                    "no_selector": "no selector given",
                }[reason]
            if kind == "archive" and info["record"] is None and reason is None:
                detail += " (its record is missing)"
            items.append({"kind": kind, "run_key": run_key, "path": str(path),
                          "action": action, "reason": why, "detail": detail})
    return {
        "schema_version": PRUNE_SCHEMA_VERSION,
        "dry_run": dry_run,
        "criteria": {"older_than_days": older_than_days, "keep": keep},
        "items": items,
        "summary_path": str(summary),
    }


class _PruneLockError(Exception):
    pass


def _records_lock(repo_root: Path):
    """An exclusive, non-blocking ``flock`` on ``<records dir>/.prune.lock``.
    Only prune takes it, so two prunes never run their deletions at once;
    nothing else (finish included) takes this lock."""

    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        directory = managed_run.records_dir(repo_root)
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / ".prune.lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise _PruneLockError(f"another prune holds {directory / '.prune.lock'}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    return held()
