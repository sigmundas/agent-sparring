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


def finish_status(repo_root: Path, run_key: str, *, allow_merge_commit: bool = False) -> dict[str, Any]:
    """``{"git": ..., "finish": ...}`` for ``run_key``; never raises for an
    unknown, malformed or unmanaged run -- that is the ``unmanaged`` check."""

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
    if present:
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
