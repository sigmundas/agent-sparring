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
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from agent_sparring import managed_run
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord

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


def _remote_contains(cwd: Path, remote: str, branch: str, candidate: str) -> tuple[bool, str]:
    """Is ``candidate`` reachable from ``refs/heads/<branch>`` on the record's
    ``remote`` -- bound to the record, never to mutable upstream config."""

    ref = f"refs/heads/{branch}"
    result = managed_run._git(cwd, "ls-remote", "--exit-code", remote, ref)
    if result.returncode != 0:
        return False, f"{remote} has no {ref} ({result.stderr.strip() or 'ls-remote found nothing'})"
    remote_sha = (result.stdout.split() or [""])[0]
    if remote_sha == candidate:
        return True, f"{remote}/{branch} is exactly {candidate}"
    if _rev(cwd, remote_sha) is None:
        return False, f"{remote}/{branch} is at {remote_sha}, which is not present locally; fetch and re-check"
    if _is_ancestor(cwd, candidate, remote_sha):
        return True, f"{candidate} is reachable from {remote}/{branch} ({remote_sha})"
    return False, f"{candidate} is not reachable from {remote}/{branch} ({remote_sha})"


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return managed_run._git(cwd, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _final_candidate(record: ManagedRunRecord, state: Any) -> tuple[bool, str, str | None]:
    """``(every stage accepted, detail, last accepted stage's candidate)``
    in plan order, read from the run's own stage states."""

    from agent_sparring.plan import MarkdownPlanSource, load_plan_source
    from agent_sparring.stage import Stage, StageStatus

    worktree = Path(record.worktree_path)
    source = load_plan_source(Path(record.input_path), worktree, manifest=record.input_kind == "manifest")
    if isinstance(source, MarkdownPlanSource):
        source = source.in_namespace(record.run_key)
    if source.digest() != state.plan_digest:
        return False, f"{record.input_path} no longer matches the plan this run executed", None
    candidate: str | None = None
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
    if unaccepted:
        return False, "not accepted by this run: " + ", ".join(unaccepted), candidate
    return True, "every stage is accepted", candidate


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

# Remote branch deletion policy. The remote managed branch is deleted only
# when the reviewed candidate's ``[finish] delete_remote_branch`` is true,
# and only as a compare-and-delete at the mutation itself:
# ``git push --force-with-lease=refs/heads/<b>:<candidate> <remote> --delete
# refs/heads/<b>`` -- the server refuses the delete unless the ref is
# exactly the merged candidate, so a concurrent push is never dropped. This
# is the single use of force in this module (a test pins it). Before the
# push the remote is read at its push URL -- an absent branch needs no
# delete, and a remote target not yet containing the candidate keeps the
# branch (``remote_target_missing_candidate``) -- but those reads only
# inform; the lease is the guard. The "target contains the candidate" read
# is point-in-time: the target may move after it, and only the branch
# deletion itself is lease-guarded. A remote with several push URLs is not
# deleted from (``remote_multiple_push_urls``: no per-URL lease), and a
# push-only endpoint whose ``ls-remote`` fails cannot use the option
# (``remote_unreachable``). A refused push is classified from its own
# porcelain status lines; it is deleted only when every line says so. Any refusal keeps the branch and stops the finish
# at ``delete_remote_branch``, unfinished, so a re-run resumes.
#
# When the policy is false the branch is kept and reported both with the
# precise ``remote_branch.code`` ``remote_delete_disabled`` and with the
# legacy ``kept`` entry ``keep_remote_branch``/``remote_delete_unavailable``:
# that code is a compatibility alias kept for one release and removed in
# the next.
REMOTE_DELETE_UNAVAILABLE = "[finish] delete_remote_branch is not enabled, so the remote branch is kept"
REMOTE_DELETE_LEASE = "--force-with-lease"
REMOTE_KEEP_CODES = (
    "remote_lease_mismatch", "remote_unreachable", "remote_delete_rejected", "remote_target_missing_candidate",
    "remote_target_unknown", "remote_multiple_push_urls",
)


def _kept_remote(repo_root: Path, record: ManagedRunRecord, candidate: str | None,
                 code: str | None = None, detail: str | None = None) -> list[dict[str, str]]:
    """The ``keep_remote_branch`` entry for a record with a remote.

    ``code`` names why the branch is still there: a refusal of the delete,
    or -- when ``None`` -- the policy: ``remote_delete_planned`` when the
    candidate's config enables deletion (it is still kept until finish
    deletes it), else the alias ``remote_delete_unavailable``."""

    if not record.remote:
        return []
    where = f"{record.remote}/{record.branch}"
    if code is None:
        if candidate and _finish_policy(repo_root, record, candidate)["delete_remote_branch"]:
            code = "remote_delete_planned"
            detail = f"kept until finish-run deletes it with a lease on {candidate}"
        else:
            code = "remote_delete_unavailable"
            detail = REMOTE_DELETE_UNAVAILABLE
    return [{"action": "keep_remote_branch", "code": code, "detail": f"{where}: {detail}"}]


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
                accepted, detail, candidate = _final_candidate(record, state)
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
    if record.remote:
        if candidate and present:
            pushed, detail = _remote_contains(worktree, record.remote, record.branch, candidate)
        else:
            pushed, detail = False, "no candidate in a present worktree to check against the remote"
        git["candidate_pushed"] = pushed
        checks.add("candidate_not_pushed", pushed, detail)
    else:
        checks.add("candidate_not_pushed", True, "the record has no remote")

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

    kept = _kept_remote(repo_root, record, candidate)
    if not all(item["ok"] for item in checks.items if item["code"] not in CLEANUP_CHECKS):
        return {"git": git, "finish": _finish(
            record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
            deleted_ignored=deleted_ignored, kept=kept,
        )}

    # Every merge check passed: after the planned merge the target contains the candidate.
    cleanup = checks.all_ok and merge_mode in MERGE_MODES
    actions: list[str] = []
    if merge_mode == "fast_forward":
        actions.append(f"fast-forward {record.target_branch} to {candidate}")
    elif merge_mode == "merge_commit":
        actions.append(f"merge {candidate} into {record.target_branch} with a merge commit")
    policy = _finish_policy(repo_root, record, candidate)
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
        if record.remote and policy["delete_remote_branch"]:
            actions.append(
                f"delete remote branch {record.remote}/{record.branch} only while it is exactly {candidate} (lease)"
            )
        else:
            actions += [f"keep remote branch {record.remote}/{record.branch} ({item['code']})" for item in kept]
    return {"git": git, "finish": _finish(
        record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
        eligible_merge=True, eligible_cleanup=cleanup, actions=actions,
        deleted_ignored=deleted_ignored, kept=kept,
    )}


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
    "push_target",
    "archive_state",
    "remove_worktree",
    "delete_branch",
    "delete_remote_branch",
    "finished",
)
# The step each recorded event proves done, for reporting a finished run.
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


def _planned_steps(*, merge_only: bool, push_target: bool, remove_plan: bool, delete_remote: bool) -> list[str]:
    steps = ["merge"] + (["remove_plan"] if remove_plan else []) + (["push_target"] if push_target else [])
    if not merge_only:
        steps += ["archive_state", "remove_worktree", "delete_branch"]
        steps += ["delete_remote_branch"] if delete_remote else []
        steps.append("finished")
    return steps
ARCHIVE_SUBDIR = Path("agent-sparring") / "runs"


class _StepFailed(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


def _events(record: ManagedRunRecord, name: str) -> list[dict[str, Any]]:
    return [event for event in record.events if event["event"] == name]


def _remote_sha(cwd: Path, remote: str, branch: str) -> tuple[bool, str | None, str]:
    """``(ls-remote succeeded, sha or None when absent, error)``."""

    result = managed_run._git(cwd, "ls-remote", remote, f"refs/heads/{branch}")
    if result.returncode != 0:
        return False, None, result.stderr.strip() or f"git ls-remote {remote} failed"
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return True, sha, ""
    return True, None, ""


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
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", spec], capture_output=True, check=False
    )
    if result.returncode != 0:
        return None, f"{spec} cannot be read"
    try:
        return parse_project_config(result.stdout, source=spec), spec
    except ProjectConfigError as exc:
        return None, str(exc)


def _finish_policy(repo_root: Path, record: ManagedRunRecord, candidate: str | None) -> dict[str, Any]:
    """The candidate's ``[finish]`` opt-ins; both false when unreadable,
    with ``error`` saying why (reported as ``finish_config_unreadable``)."""

    config, detail = _candidate_config(repo_root, record, candidate) if candidate else (None, "no candidate")
    return {
        "delete_remote_branch": bool(config is not None and config.finish_delete_remote_branch),
        "remove_plan": bool(config is not None and config.finish_remove_plan),
        "error": None if config is not None or not candidate else detail,
    }


def _merge(record: ManagedRunRecord, git: dict[str, Any], merge_mode: str, repo_root: Path) -> dict[str, Any]:
    candidate = git["final_candidate"]
    old_tip = git["target_tip"]
    target_ref = f"refs/heads/{record.target_branch}"
    checkout = git["target_checked_out_at"]
    now_at = next(
        (e["path"] for e in managed_run.worktree_list(repo_root) if e["branch"] == record.target_branch), None
    )
    if now_at != checkout:
        raise _StepFailed(
            "merge", f"target_moved: {record.target_branch} is now checked out at {now_at or 'no checkout'}, "
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
                "-m", f"Merge branch '{record.branch}' into {record.target_branch}",
            )
            if commit.returncode != 0:
                raise _StepFailed("merge", f"git commit-tree failed: {commit.stderr.strip()}")
            new_tip = commit.stdout.strip()
        result = managed_run._git(repo_root, "update-ref", target_ref, new_tip, old_tip)
        if result.returncode != 0:
            raise _StepFailed("merge", f"{target_ref} moved; nothing was merged ({result.stderr.strip()})")
    target_sha = _rev(repo_root, target_ref)
    if target_sha is None or not _is_ancestor(repo_root, candidate, target_sha):
        raise _StepFailed("merge", f"{record.target_branch} does not contain {candidate} after the merge")
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

    Holds the worktree lock throughout, re-reads the record under it and
    re-runs every eligibility check itself. Each step is skipped when its
    event is recorded and still true, appends its event when done, and the
    first failure stops everything after it. The opt-in steps
    (``remove_plan``, ``delete_remote_branch``) are planned once the merged
    candidate -- whose own config enables them -- is known."""

    planned = _planned_steps(merge_only=merge_only, push_target=push_target, remove_plan=False, delete_remote=False)
    completed: list[str] = []
    notes: dict[str, Any] = {"deleted_ignored": [], "kept": [], "plan_removal": None, "remote_branch": None}
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
            # The remote branch is reported on every return, including
            # --merge-only, refusals and failures, until it is deleted.
            merged = _events(locked, "merged")
            merged_candidate = merged[-1]["detail"].get("candidate") if merged else None
            notes["kept"] = _kept_remote(repo_root, locked, merged_candidate)
            if _events(locked, "finished"):
                # Report only what the record shows actually happened.
                recorded = {event["event"] for event in locked.events}
                completed[:] = [step for event, step in _EVENT_STEPS if event in recorded]
                planned[:] = list(completed)
                notes["plan_removal"] = _recorded_plan_removal(locked)
                notes["remote_branch"] = _recorded_remote_branch(locked)
                if notes["remote_branch"]["code"] in ("remote_branch_deleted", "remote_branch_absent"):
                    notes["kept"] = []
                return report(None, "the run is already finished"), None
            return _finish_locked(repo_root, locked, planned, completed, notes, merge_only=merge_only,
                                  allow_merge_commit=allow_merge_commit, push_target=push_target)
    except WorktreeLockError as exc:
        return report("checks", f"runner_live: {exc}"), None
    except _StepFailed as exc:
        return report(exc.step, exc.reason), None
    except (ManagedRunError, OSError) as exc:
        # An operational failure (git, disk, permissions, a record write)
        # stops at the step in progress; later steps stay untouched.
        stopped = next((step for step in planned if step not in completed), None)
        return report(stopped, f"{type(exc).__name__}: {exc}"), None


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
    deleted = _events(record, "remote_branch_deleted")
    if deleted:
        detail = deleted[-1]["detail"]
        return _outcome(detail.get("code", "remote_branch_deleted"), detail.get("detail", "recorded"))
    if not record.remote:
        return _outcome("no_remote", "the record has no remote")
    return _outcome(None, "no remote branch deletion is recorded")


def _finish_locked(
    repo_root: Path, record: ManagedRunRecord, planned: list[str], completed: list[str],
    notes: dict[str, Any], *, merge_only: bool, allow_merge_commit: bool, push_target: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_key = record.run_key

    def report(stopped_at: str | None, reason: str | None) -> dict[str, Any]:
        return _report(run_key, completed, stopped_at, reason, planned,
                       deleted_ignored=notes["deleted_ignored"], kept=notes["kept"],
                       plan_removal=notes["plan_removal"], remote_branch=notes["remote_branch"])

    def plan_steps(candidate: str) -> dict[str, bool]:
        policy = _finish_policy(repo_root, record, candidate)
        planned[:] = _planned_steps(
            merge_only=merge_only, push_target=push_target, remove_plan=policy["remove_plan"],
            delete_remote=policy["delete_remote_branch"] and bool(record.remote),
        )
        unreadable = policy["error"] and _outcome(
            "finish_config_unreadable", f"the candidate's [finish] config cannot be read: {policy['error']}"
        )
        if not policy["remove_plan"]:
            notes["plan_removal"] = unreadable or _outcome(
                "plan_removal_disabled", "[finish] remove_plan is not enabled"
            )
        if not record.remote:
            notes["remote_branch"] = _outcome("no_remote", "the record has no remote; nothing to delete")
        elif not policy["delete_remote_branch"]:
            notes["remote_branch"] = unreadable or _outcome("remote_delete_disabled", REMOTE_DELETE_UNAVAILABLE)
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
        policy = plan_steps(candidate)
        completed.append("merge")
        if policy["remove_plan"]:
            record = _remove_plan_step(repo_root, record, notes)
            completed.append("remove_plan")
        if push_target:
            record = _push_target(repo_root, record, candidate)
            completed.append("push_target")
        if merge_only:
            return report(None, "--merge-only: cleanup was not requested"), None
        if not _archive_readable(repo_root, record):
            raise _StepFailed("archive_state", "the archived run state is missing or unreadable")
        completed.append("archive_state")
    else:
        status = finish_status(repo_root, run_key, allow_merge_commit=allow_merge_commit, lock_held=True)
        finish, git = status["finish"], status["git"]
        if git["final_candidate"]:
            notes["kept"] = _kept_remote(repo_root, record, git["final_candidate"])
        if not finish["eligible"]["merge"] or (not merge_only and not finish["eligible"]["cleanup"]):
            return report("checks", finish["summary"]), finish
        candidate = git["final_candidate"]
        policy = plan_steps(candidate)
        # 1. merge
        if finish["merge_mode"] == "already_merged":
            if not _events(record, "merged"):
                record = managed_run.append_event(repo_root, run_key, "merged", {
                    "mode": "already_merged", "target_sha": git["target_tip"], "candidate": candidate,
                })
        else:
            record = managed_run.append_event(
                repo_root, run_key, "merged", _merge(record, git, finish["merge_mode"], repo_root)
            )
        completed.append("merge")
        # 2. remove the plan (opt-in; refusals are reported, not stops)
        if policy["remove_plan"]:
            record = _remove_plan_step(repo_root, record, notes)
            completed.append("remove_plan")
        # 3. push target
        if push_target:
            record = _push_target(repo_root, record, candidate)
            completed.append("push_target")
        if merge_only:
            return report(None, "--merge-only: cleanup was not requested"), None
        # 4. archive every ignored file under the project directory
        destination, archived = _archive_state(repo_root, record)
        if not _archive_readable(repo_root, record):
            raise _StepFailed("archive_state", "unarchived_project_state: the archived run state is unreadable")
        if not _events(record, "state_archived"):
            record = managed_run.append_event(repo_root, run_key, "state_archived", {"path": str(destination)})
        completed.append("archive_state")
        # 5. remove worktree -- only what is archived or reported may go
        _verify_archived(record, archived)
        deleted_ignored = _deleted_ignored(record)
        result = managed_run._git(repo_root, "worktree", "remove", record.worktree_path)
        if result.returncode != 0 or not _worktree_gone(repo_root, record):
            raise _StepFailed("remove_worktree", f"git worktree remove failed: {result.stderr.strip()}")
        # Only paths this removal actually deleted are reported.
        notes["deleted_ignored"] = deleted_ignored
        record = managed_run.append_event(repo_root, run_key, "worktree_removed")
    completed.append("remove_worktree")

    # 6. delete local branch
    branch_ref = f"refs/heads/{record.branch}"
    tip = _rev(repo_root, branch_ref)
    if tip is not None:
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

    # 7. delete the remote branch (opt-in; compare-and-delete on the candidate)
    if "delete_remote_branch" in planned:
        merged = _events(record, "merged")
        merged_candidate = merged[-1]["detail"].get("candidate") if merged else None
        if not merged_candidate:
            raise _StepFailed("delete_remote_branch", "the record has no merged candidate")
        if _events(record, "remote_branch_deleted"):
            notes["remote_branch"] = _recorded_remote_branch(record)
        else:
            code, detail = _delete_remote_branch(repo_root, record, merged_candidate)
            notes["remote_branch"] = _outcome(code, detail)
            if code in REMOTE_KEEP_CODES:
                notes["kept"] = _kept_remote(repo_root, record, merged_candidate, code=code, detail=detail)
                raise _StepFailed("delete_remote_branch", f"{code}: {detail}")
            record = managed_run.append_event(
                repo_root, run_key, "remote_branch_deleted",
                {"code": code, "detail": detail, "remote": record.remote, "candidate": merged_candidate},
            )
        notes["kept"] = []
        completed.append("delete_remote_branch")

    # 8. finished
    managed_run.append_event(repo_root, run_key, "finished")
    completed.append("finished")
    return report(None, None), None


def _delete_remote_branch(repo_root: Path, record: ManagedRunRecord, candidate: str) -> tuple[str, str]:
    """Delete ``refs/heads/<branch>`` on the record's remote only while it is
    exactly ``candidate``: the lease is checked by the server at the delete
    itself. ``(code, detail)``.

    Before the push, the remote is read at its **push** URL (where the
    delete goes, after ``pushurl``/``pushInsteadOf``): an absent branch is
    success without a push, and a remote target that does not yet contain
    the candidate keeps the branch (``remote_target_missing_candidate``).
    Those reads inform only; the lease is the guard. A refused push is
    classified from its own ``--porcelain`` status line, never by reading
    the remote again, so a refusal is never reported as absent or deleted."""

    ref = f"refs/heads/{record.branch}"
    where = f"{record.remote}/{record.branch}"
    push_urls = managed_run._git(repo_root, "remote", "get-url", "--push", "--all", record.remote)
    urls = push_urls.stdout.split() if push_urls.returncode == 0 else []
    if len(urls) > 1:
        return "remote_multiple_push_urls", (
            f"{record.remote} has {len(urls)} push URLs ({', '.join(urls)}); a per-URL lease delete is not "
            f"supported, so nothing was read or pushed and {where} is kept"
        )
    url = urls[0] if urls else record.remote
    ok, branch_sha, error = _remote_sha(repo_root, url, record.branch)
    if not ok:
        return "remote_unreachable", f"{where} cannot be reached at {url} ({error}); the branch is kept"
    if branch_sha is None:
        return "remote_branch_absent", f"{where} does not exist at {url}; nothing to delete"
    ok, target_sha, error = _remote_sha(repo_root, url, record.target_branch)
    if not ok:
        return "remote_unreachable", f"{record.remote}/{record.target_branch} cannot be read at {url} ({error})"
    if target_sha is not None and _rev(repo_root, target_sha) is None:
        return "remote_target_unknown", (
            f"{record.remote}/{record.target_branch} at {url} is {target_sha}, which is not present locally; "
            f"fetch first, then re-run. {where} is kept"
        )
    if target_sha is None or not _is_ancestor(repo_root, candidate, target_sha):
        return "remote_target_missing_candidate", (
            f"{record.remote}/{record.target_branch} at {url} is {target_sha or 'absent'} and does not contain "
            f"the merged candidate {candidate}; push the target (--push-target) and re-run. {where} is kept"
        )
    result = managed_run._git(
        repo_root, "push", "--porcelain", f"{REMOTE_DELETE_LEASE}={ref}:{candidate}", record.remote, "--delete", ref,
    )
    lines = result.stdout.splitlines()
    sections = [line for line in lines if line.startswith("To ")]
    statuses = [line.split("\t") for line in lines if line.count("\t") >= 2 and f":{ref}" in line]
    error = (result.stderr.strip() or result.stdout.strip() or "git push --delete failed").splitlines()[-1]
    if not statuses:
        return "remote_unreachable", f"the delete of {where} did not reach the remote ({error}); the branch is kept"
    if result.returncode == 0 and all(status[0] == "-" for status in statuses):
        return "remote_branch_deleted", f"{where} was exactly {candidate} and is deleted"
    summary = "; ".join(status[2] for status in statuses if status[0] != "-") or error
    if len(sections) > 1 or len(statuses) > 1:
        return "remote_delete_rejected", (
            f"the delete of {where} went to {max(len(sections), len(statuses))} destinations and not every one "
            f"deleted it ({summary}); it is not recorded deleted"
        )
    if "stale info" in summary:
        return "remote_lease_mismatch", (
            f"{where} is no longer exactly the merged candidate {candidate}; the lease refused the delete "
            f"({summary}) and it is kept"
        )
    return "remote_delete_rejected", f"the remote refused deleting {where}: {summary}; the branch is kept"


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
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=False, input=stdin,
        env={**os.environ, **env} if env else None,
    )


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
    committed = managed_run._git(checkout, "commit", "-q", "--no-verify", "-m", message)
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


def _push_target(repo_root: Path, record: ManagedRunRecord, candidate: str) -> ManagedRunRecord:
    """Push the target unless the remote target already contains the
    candidate (and a recorded plan-removal commit); never forced, so a
    rejection stops the finish here."""

    if not record.remote:
        raise _StepFailed("push_target", "the record has no remote to push the target to")
    ok, remote_tip, error = _remote_sha(repo_root, record.remote, record.target_branch)
    if not ok:
        raise _StepFailed("push_target", error)
    removal = _events(record, "plan_removed")
    must_contain = [candidate] + ([removal[-1]["detail"]["commit"]] if removal else [])
    if (
        remote_tip is None
        or _rev(repo_root, remote_tip) is None
        or not all(_is_ancestor(repo_root, sha, remote_tip) for sha in must_contain)
    ):
        result = managed_run._git(
            repo_root, "-c", "push.followTags=false", "push", record.remote,
            f"refs/heads/{record.target_branch}:refs/heads/{record.target_branch}",
        )
        if result.returncode != 0:
            raise _StepFailed("push_target", f"the push was rejected: {result.stderr.strip()}")
    if not _events(record, "target_pushed"):
        record = managed_run.append_event(repo_root, record.run_key, "target_pushed", {"remote": record.remote})
    return record


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
