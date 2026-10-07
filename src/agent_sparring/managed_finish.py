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

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_sparring import managed_run
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord

FINISH_SCHEMA_VERSION = 1
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
            actions: list[str] | None = None, deleted_ignored: list[str] | None = None) -> dict[str, Any]:
    failed = [item["code"] for item in checks.items if not item["ok"]]
    if failed:
        summary = f"run {run_key} cannot be finished: " + ", ".join(failed)
    elif eligible_cleanup:
        summary = f"run {run_key} can be finished ({merge_mode})"
    else:
        summary = f"run {run_key} can be merged ({merge_mode}) but not cleaned up"
    return {
        "schema_version": FINISH_SCHEMA_VERSION,
        "run_key": run_key,
        "managed": managed,
        "eligible": {"merge": eligible_merge, "cleanup": eligible_cleanup},
        "merge_mode": merge_mode,
        "actions": actions or [],
        "checks": checks.items,
        "deleted_ignored_paths": deleted_ignored or [],
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


def _ignored_top_level(worktree: Path, project_dir: str) -> list[str]:
    result = managed_run._git(
        worktree, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"
    )
    if result.returncode != 0:
        return []
    archived = Path(project_dir).parts[0]
    tops = {Path(entry).parts[0] for entry in result.stdout.split("\0") if entry}
    return sorted(top for top in tops if top != archived)


def finish_status(
    repo_root: Path, run_key: str, *, allow_merge_commit: bool = False, lock_held: bool = False
) -> dict[str, Any]:
    """``{"git": ..., "finish": ...}`` for ``run_key``; never raises for an
    unknown, malformed or unmanaged run -- that is the ``unmanaged`` check.

    ``lock_held``: the caller (a finish that executes) already holds the
    worktree lock, so ``runner_live`` holds by construction."""

    git = _empty_git()
    checks = _Checks()
    try:
        record = managed_run.read_record(repo_root, run_key)
    except ManagedRunError as exc:
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

    if not checks.all_ok:
        return {"git": git, "finish": _finish(
            record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
            deleted_ignored=_ignored_top_level(worktree, record.project_dir) if entry else [],
        )}

    # Every check passed: after the planned merge the target contains the candidate.
    actions: list[str] = []
    if merge_mode == "fast_forward":
        actions.append(f"fast-forward {record.target_branch} to {candidate}")
    elif merge_mode == "merge_commit":
        actions.append(f"merge {candidate} into {record.target_branch} with a merge commit")
    actions += [
        f"archive {record.project_dir} state of run {record.run_key}",
        f"remove worktree {record.worktree_path}",
        f"delete local branch {record.branch}",
    ]
    return {"git": git, "finish": _finish(
        record.run_key, managed=True, checks=checks, merge_mode=merge_mode,
        eligible_merge=True, eligible_cleanup=merge_mode in MERGE_MODES, actions=actions,
        deleted_ignored=_ignored_top_level(worktree, record.project_dir),
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
    "push_target",
    "archive_state",
    "remove_worktree",
    "delete_branch",
    "delete_remote_branch",
    "finished",
)
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


def _owned_stage_dirs(record: ManagedRunRecord) -> list[Path]:
    from agent_sparring.stage import STAGES_DIRNAME, Stage

    root = record.sparring_dir / STAGES_DIRNAME
    owned: list[Path] = []
    if not root.is_dir():
        return owned
    for directory in sorted(root.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            state = Stage.resolve(record.sparring_dir, directory.name).read_state()
        except Exception:  # noqa: BLE001 -- an unreadable stage is not provably this run's
            continue
        if state.run == record.run_key:
            owned.append(directory)
    return owned


def _tree_files(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root`` by relative path; symlinks by target."""

    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            files[relative] = b"symlink:" + os.readlink(path).encode("utf-8")
        elif path.is_file():
            files[relative] = path.read_bytes()
    return files


def _archive_state(repo_root: Path, record: ManagedRunRecord) -> Path:
    """Copy the run state and the stage directories this run owns into the
    archive, verify the copy, and return it. An existing archive is kept
    only when it is exactly that copy; anything else refuses."""

    from agent_sparring.stage import STAGES_DIRNAME

    destination = archive_dir(repo_root, record.run_key)
    state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
    if not state_path.is_file():
        raise _StepFailed("archive_state", f"the run state {state_path} does not exist")
    staging = destination.parent / f".{record.run_key}.tmp-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        (staging / "plans").mkdir(parents=True)
        shutil.copy2(state_path, staging / "plans" / state_path.name)
        for directory in _owned_stage_dirs(record):
            shutil.copytree(directory, staging / STAGES_DIRNAME / directory.name, symlinks=True)
        expected = _tree_files(staging)
        if destination.exists():
            if _tree_files(destination) != expected:
                raise _StepFailed(
                    "archive_state", f"{destination} already exists with different content; it is left in place"
                )
        else:
            os.replace(staging, destination)
            if _tree_files(destination) != expected:
                raise _StepFailed("archive_state", f"the archive at {destination} does not match the run state")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


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


def _delete_remote_policy(repo_root: Path, record: ManagedRunRecord, candidate: str) -> tuple[bool, str]:
    """The reviewed candidate's own ``[finish] delete_remote_branch``."""

    from agent_sparring.config import CONFIG_FILENAME, ProjectConfigError, parse_project_config

    spec = f"{candidate}:{(Path(record.project_dir) / CONFIG_FILENAME).as_posix()}"
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", spec], capture_output=True, check=False
    )
    if result.returncode != 0:
        return False, f"{spec} cannot be read"
    try:
        config = parse_project_config(result.stdout, source=spec)
    except ProjectConfigError as exc:
        return False, str(exc)
    return config.finish_delete_remote_branch, "[finish] delete_remote_branch"


def _merge(record: ManagedRunRecord, git: dict[str, Any], merge_mode: str, repo_root: Path) -> dict[str, Any]:
    candidate = git["final_candidate"]
    old_tip = git["target_tip"]
    target_ref = f"refs/heads/{record.target_branch}"
    checkout = git["target_checked_out_at"]
    if checkout is not None:
        where = Path(checkout)
        if merge_mode == "fast_forward":
            result = managed_run._git(where, "merge", "--ff-only", candidate)
        else:
            result = managed_run._git(where, "merge", "--no-ff", "--no-edit", candidate)
        if result.returncode != 0:
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


def _report(run_key: str, completed: list[str], stopped_at: str | None, reason: str | None,
            planned: list[str]) -> dict[str, Any]:
    return {
        "schema_version": FINISH_SCHEMA_VERSION,
        "run_key": run_key,
        "completed_steps": completed,
        "stopped_at": stopped_at,
        "reason": reason,
        "remaining": [step for step in planned if step not in completed],
    }


def finish_run(
    repo_root: Path,
    run_key: str,
    *,
    merge_only: bool = False,
    allow_merge_commit: bool = False,
    push_target: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Execute a finish: ``(report, finish block when the checks refused)``.

    Holds the worktree lock throughout and re-runs every eligibility check
    itself. Each step is skipped when its event is recorded and still true,
    appends its event when done, and the first failure stops everything
    after it."""

    planned = ["merge"] + (["push_target"] if push_target else [])
    if not merge_only:
        planned += ["archive_state", "remove_worktree", "delete_branch", "delete_remote_branch", "finished"]
    completed: list[str] = []
    try:
        record = managed_run.read_record(repo_root, run_key)
    except ManagedRunError as exc:
        return _report(run_key, completed, "checks", f"unmanaged: {exc}", planned), None
    if record is None or record.created_by != "engine" or not record.owns_git_state:
        status = finish_status(repo_root, run_key)
        return _report(run_key, completed, "checks", status["finish"]["summary"], planned), status["finish"]
    if _events(record, "finished"):
        return _report(run_key, list(planned), None, "the run is already finished", planned), None

    try:
        with worktree_lock(Path(record.worktree_path)):
            return _finish_locked(repo_root, record, planned, completed, merge_only=merge_only,
                                  allow_merge_commit=allow_merge_commit, push_target=push_target)
    except WorktreeLockError as exc:
        return _report(run_key, completed, "checks", f"runner_live: {exc}", planned), None
    except _StepFailed as exc:
        return _report(run_key, completed, exc.step, exc.reason, planned), None
    except ManagedRunError as exc:
        stopped = next((step for step in planned if step not in completed), None)
        return _report(run_key, completed, stopped, str(exc), planned), None


def _finish_locked(
    repo_root: Path, record: ManagedRunRecord, planned: list[str], completed: list[str], *,
    merge_only: bool, allow_merge_commit: bool, push_target: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_key = record.run_key
    removed = bool(_events(record, "worktree_removed"))
    if removed and not _worktree_gone(repo_root, record):
        raise _StepFailed("remove_worktree", f"{record.worktree_path} was recorded removed but exists again")
    if not removed and not merge_only and managed_run.worktree_top(repo_root) == Path(record.worktree_path).resolve():
        return _report(
            run_key, completed, "checks",
            "finish-run is running inside the managed worktree it would remove; run it from another checkout",
            planned,
        ), None

    if removed:
        # Every eligibility check is about the worktree, which is gone; what
        # remains needs only the candidate the recorded merge proved merged.
        merged = _events(record, "merged")
        candidate = merged[-1]["detail"].get("candidate") if merged else None
        if not candidate:
            raise _StepFailed("merge", "the record has no merged candidate")
        completed.append("merge")
        if push_target:
            record = _push_target(repo_root, record, candidate)
            completed.append("push_target")
        if merge_only:
            return _report(run_key, completed, None, "--merge-only: cleanup was not requested", planned), None
        completed.append("archive_state")
        if not _archive_readable(repo_root, record):
            raise _StepFailed("archive_state", "the archived run state is missing or unreadable")
    else:
        status = finish_status(repo_root, run_key, allow_merge_commit=allow_merge_commit, lock_held=True)
        finish, git = status["finish"], status["git"]
        if not finish["eligible"]["merge"] or (not merge_only and not finish["eligible"]["cleanup"]):
            return _report(run_key, completed, "checks", finish["summary"], planned), finish
        candidate = git["final_candidate"]
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
        # 2. push target
        if push_target:
            record = _push_target(repo_root, record, candidate)
            completed.append("push_target")
        if merge_only:
            return _report(run_key, completed, None, "--merge-only: cleanup was not requested", planned), None
        # 4. archive state
        destination = _archive_state(repo_root, record)
        if not _events(record, "state_archived"):
            record = managed_run.append_event(repo_root, run_key, "state_archived", {"path": str(destination)})
        completed.append("archive_state")
        # 5. remove worktree
        result = managed_run._git(repo_root, "worktree", "remove", record.worktree_path)
        if result.returncode != 0 or not _worktree_gone(repo_root, record):
            raise _StepFailed("remove_worktree", f"git worktree remove failed: {result.stderr.strip()}")
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

    # 7. delete remote branch -- policy and containment, otherwise kept
    if record.remote and not _events(record, "remote_branch_deleted"):
        allowed, _ = _delete_remote_policy(repo_root, record, candidate)
        if allowed and _remote_branch_deletable(repo_root, record, candidate):
            result = managed_run._git(repo_root, "push", record.remote, f":refs/heads/{record.branch}")
            if result.returncode != 0:
                raise _StepFailed("delete_remote_branch", f"the remote branch could not be deleted: {result.stderr.strip()}")
            record = managed_run.append_event(repo_root, run_key, "remote_branch_deleted", {"remote": record.remote})
            completed.append("delete_remote_branch")
    elif _events(record, "remote_branch_deleted"):
        completed.append("delete_remote_branch")

    # 8. finished
    managed_run.append_event(repo_root, run_key, "finished")
    completed.append("finished")
    planned = [step for step in planned if step in completed]  # a kept remote branch is not "remaining"
    return _report(run_key, completed, None, None, planned), None


def _push_target(repo_root: Path, record: ManagedRunRecord, candidate: str) -> ManagedRunRecord:
    """Push the target unless the remote target already contains the
    candidate; never forced, so a rejection stops the finish here."""

    if not record.remote:
        raise _StepFailed("push_target", "the record has no remote to push the target to")
    ok, remote_tip, error = _remote_sha(repo_root, record.remote, record.target_branch)
    if not ok:
        raise _StepFailed("push_target", error)
    if remote_tip is None or _rev(repo_root, remote_tip) is None or not _is_ancestor(repo_root, candidate, remote_tip):
        result = managed_run._git(
            repo_root, "-c", "push.followTags=false", "push", record.remote,
            f"refs/heads/{record.target_branch}:refs/heads/{record.target_branch}",
        )
        if result.returncode != 0:
            raise _StepFailed("push_target", f"the push was rejected: {result.stderr.strip()}")
    if not _events(record, "target_pushed"):
        record = managed_run.append_event(repo_root, record.run_key, "target_pushed", {"remote": record.remote})
    return record


def _remote_branch_deletable(repo_root: Path, record: ManagedRunRecord, candidate: str) -> bool:
    """The remote target contains the candidate and the remote branch is
    exactly the candidate -- otherwise the remote branch is kept."""

    ok, target_tip, _ = _remote_sha(repo_root, record.remote, record.target_branch)
    if not ok or target_tip is None or _rev(repo_root, target_tip) is None:
        return False
    if not _is_ancestor(repo_root, candidate, target_tip):
        return False
    ok, branch_tip, _ = _remote_sha(repo_root, record.remote, record.branch)
    return ok and branch_tip == candidate
