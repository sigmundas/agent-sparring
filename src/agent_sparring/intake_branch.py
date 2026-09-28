"""Put an intake slice on a feature branch: before approval, or once after it.

An implementation slice never runs on a protected branch -- the branch guard
refuses every unattended stage-agent turn there
(:mod:`agent_sparring.branch_guard`). So a slice is approved on the branch
its primary repository has checked out *at approval*, and approval refuses a
protected one (:func:`~agent_sparring.intake.approve_plan`). This module is
what a person (or the VS Code extension) uses to get there without knowing
that policy:

- :func:`slice_branch_status` says, as data, whether the slice needs a
  feature branch and what the engine can do about it (``action``), with the
  engine's own sentence for technical details.
- :func:`create_slice_branch` checks out a new branch at the current commit
  (``git switch -c``, which keeps the worktree exactly as it is), and, when
  the slice was already approved on a protected branch, moves that approval
  to it (below).
- :func:`move_slice_approval` is the one recovery for an approval sealed on
  a protected branch before approvals refused one. ``approval.json`` is never
  rewritten; the engine writes :data:`~agent_sparring.intake_approval.
  BRANCH_MOVE_FILENAME` beside it, once, bound to the approval's exact bytes,
  and every later reader applies it (:func:`~agent_sparring.intake_approval.
  read_approval`). It is not a branch-drift override. It refuses unless:

  - the approved branch is protected and the slice runs an implementation
    agent -- an approval the engine could never execute;
  - the repository is the approved worktree (same canonical path and git
    directory), now on a new, non-protected branch;
  - HEAD is the approved starting commit, and the worktree is no dirtier
    than it was at approval (its dirty paths are a subset of those the
    approval recorded) -- so the new branch holds exactly what was approved;
  - every other repository the approval recorded is still where it was;
  - no provider turn has happened for any of the slice's stages (no base
    commit, candidate or session recorded, and no turn in their activity);
  - its run, if one was started, is this slice's sealed run, paused before
    its first stage and holding no push authorization for the old branch.

  The run's recorded branch is then updated to match, which is what lets
  ``resume-plan`` continue it; the move record says what happened and why.
  Earlier slices and their accepted candidates are not read for writing.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_sparring.branch_guard import is_protected_branch
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.git_context import GitContextError, current_branch, repository_identity
from agent_sparring.intake import (
    IntakeError,
    RunSlice,
    feature_branch_needed,
    parse_interpretation,
    slice_runs_agent,
    slice_stage_names,
)
from agent_sparring.intake_approval import (
    APPROVAL_FILENAME,
    BRANCH_MOVE_FILENAME,
    BRANCH_MOVE_VERSION,
    MANIFEST_FILENAME,
    RUNS_DIRNAME,
    SOURCE_KIND,
    IntakeApproval,
    IntakeApprovalError,
    load_intake_manifest,
    read_approval,
    write_exclusive,
)
from agent_sparring.plan import PlanError, PlanRunState, PlanRunStatus, run_state_path
from agent_sparring.sparring_agent import repo_fingerprint
from agent_sparring.stage import Stage, StageError, StageStatus

#: Check out a new feature branch here (and move the approval, if there is one).
ACTION_CREATE = "create-branch"
#: The worktree is already on a feature branch; move the approval to it.
ACTION_MOVE = "move-approval"

#: Activity events that mean a provider was given a turn for the stage.
_PROVIDER_TURN_EVENTS = frozenset({"turn.started", "sparring.started", "dialogue.started"})


@dataclass(frozen=True)
class SliceBranch:
    """Whether a slice needs a feature branch, and what the engine can do."""

    run_id: str
    #: The slice's plan stages as a person names them (``Stage 1B``).
    stages: str
    repository: str
    repo_root: Path
    runs_agent: bool
    current_branch: str
    #: The branch its approval runs on (after any recorded move), if approved.
    approved_branch: str | None
    #: The protected branch a recorded move took the approval off, if any.
    moved_from: str | None
    #: :data:`ACTION_CREATE`, :data:`ACTION_MOVE`, or ``None`` when nothing is needed or possible.
    action: str | None
    #: A branch name to offer for :data:`ACTION_CREATE`.
    suggested_branch: str | None
    #: The engine's sentence for why a branch is needed (technical details).
    problem: str | None
    #: Why the engine cannot fix it, when a branch is needed but no action is safe.
    blocked: str | None

    @property
    def needs_branch(self) -> bool:
        return self.problem is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stages": self.stages,
            "repository": self.repository,
            "repo_root": str(self.repo_root),
            "runs_agent": self.runs_agent,
            "current_branch": self.current_branch,
            "approved_branch": self.approved_branch,
            "moved_from": self.moved_from,
            "needs_branch": self.needs_branch,
            "action": self.action,
            "suggested_branch": self.suggested_branch,
            "problem": self.problem,
            "blocked": self.blocked,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _run_dir(intake_dir: Path, run_id: str) -> Path:
    return Path(intake_dir) / RUNS_DIRNAME / (_slug(run_id) or "run")


def _slice(intake_dir: Path, run_id: str) -> tuple[RunSlice, str]:
    """The run slice and the intake's plan label, from the intake's own files."""

    try:
        record = json.loads((Path(intake_dir) / "intake.json").read_text(encoding="utf-8"))
        interpretation = parse_interpretation((Path(intake_dir) / "interpretation.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeError(f"cannot read intake {intake_dir}: {exc}") from exc
    for run in interpretation.runs:
        if run.id == run_id:
            return run, str(record.get("plan_label") or "")
    raise IntakeError(f"no run slice {run_id!r}; this intake has {sorted(r.id for r in interpretation.runs)}")


def suggested_branch(plan_label: str, run: RunSlice) -> str:
    """``feature/<plan>-stage-<labels>``: the plan's file name without its
    date or ``.md``, then the stages (``feature/taxonomy-v3-stage-1b``)."""

    name = re.sub(r"\.md$", "", plan_label.replace("\\", "/").rsplit("/", 1)[-1])
    plan = _slug(re.sub(r"^\d{4}-\d{2}-\d{2}-", "", name)) or "plan"
    stages = "-".join(_slug(stage.label) for stage in run.stages) or _slug(run.id)
    return f"feature/{plan}-stage-{stages}"


def _approval(intake_dir: Path, run_id: str) -> IntakeApproval | None:
    path = _run_dir(intake_dir, run_id) / APPROVAL_FILENAME
    if not path.exists():
        return None
    try:
        return read_approval(path)
    except IntakeApprovalError as exc:
        raise IntakeError(f"run slice {run_id!r} has an approval that cannot be used: {exc}") from exc


def slice_branch_status(intake_dir: Path, *, run_id: str, repo_root: Path, sparring_dir: Path) -> SliceBranch:
    """What the slice needs before it can run, as far as its branch goes.

    Read-only. ``repo_root`` is the slice's primary repository.
    """

    run, plan_label = _slice(intake_dir, run_id)
    repo_root = Path(repo_root).resolve()
    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise IntakeError(f"cannot read the branch of {repo_root}: {exc}") from exc
    approval = _approval(intake_dir, run_id)
    runs_agent = slice_runs_agent(run)
    approved = approval.expected_branch if approval else None
    on = approved if approved is not None else branch
    problem: str | None = None
    if runs_agent and is_protected_branch(on):
        problem = (
            feature_branch_needed(run, repo_root, on)
            if approval is None
            else (
                f"{slice_stage_names(run)} was approved on protected branch {on!r}, where no "
                "stage agent runs unattended, so run-plan refuses it before any provider turn. "
                "Moving the approval to a feature branch at the approved commit fixes it "
                "(sparring slice-branch --create <branch>)"
            )
        )
    action: str | None = None
    blocked: str | None = None
    if problem is not None:
        if approval is None:
            action = ACTION_CREATE
        else:
            refusals = _move_refusals(approval, run, intake_dir, repo_root, Path(sparring_dir), target=None)
            if refusals:
                blocked = "; ".join(refusals)
            else:
                action = ACTION_CREATE if is_protected_branch(branch) else ACTION_MOVE
    return SliceBranch(
        run_id=run_id,
        stages=slice_stage_names(run),
        repository=run.primary_repository,
        repo_root=repo_root,
        runs_agent=runs_agent,
        current_branch=branch,
        approved_branch=approved,
        moved_from=str(approval.move["from_branch"]) if approval and approval.move else None,
        action=action,
        suggested_branch=suggested_branch(plan_label, run) if action == ACTION_CREATE else None,
        problem=problem,
        blocked=blocked,
    )


def _no_provider_turn(sparring_dir: Path, stage_id: str, run_key: str) -> str | None:
    """Why ``stage_id`` has had a provider turn (or cannot be shown not to), or ``None``."""

    try:
        stage = Stage.resolve(sparring_dir, stage_id)
    except StageError as exc:
        return f"cannot resolve stage {stage_id}: {exc}"
    if not stage.directory.exists():
        return None
    try:
        state = stage.read_state()
    except StageError as exc:
        return f"cannot read stage {stage_id}: {exc}"
    if state.run not in (None, run_key):
        return f"stage {stage_id} belongs to another run ({state.run})"
    recorded = {
        "a base commit": state.base_sha,
        "a candidate": state.candidate_sha,
        "an implementation session": state.implementation_session_id,
        "a sparring session": state.sparring_session_id,
    }
    found = [what for what, value in recorded.items() if value]
    if found or state.status is not StageStatus.WORKING:
        return f"stage {stage_id} has already run ({', '.join(found) or state.status.value} recorded)"
    try:
        activity = stage.activity_path()
        lines = activity.read_text(encoding="utf-8").splitlines() if activity.exists() else []
    except OSError as exc:
        return f"cannot read the activity of stage {stage_id}: {exc}"
    for line in lines:
        try:
            event = json.loads(line).get("event")
        except (json.JSONDecodeError, AttributeError):
            return f"stage {stage_id} has an unreadable activity record"
        if event in _PROVIDER_TURN_EVENTS:
            return f"stage {stage_id} has already had a provider turn ({event})"
    return None


def _run_refusal(sparring_dir: Path, approval: IntakeApproval, digest: str | None) -> str | None:
    path = run_state_path(sparring_dir, approval.run_key)
    if not path.is_file():
        return None
    try:
        state = PlanRunState.load(path)
    except PlanError as exc:
        return f"cannot read its run at {path}: {exc}"
    if state.run != approval.run_key or state.source != SOURCE_KIND or (digest is not None and state.plan_digest != digest):
        return f"the run at {path} is not this slice's sealed run"
    if state.status is not PlanRunStatus.PAUSED:
        return f"its run {approval.run_key} is {state.status.value}, not paused before its first stage"
    if state.current_stage_index != 0:
        return f"its run {approval.run_key} is past its first stage"
    if state.push_authorization is not None:
        return f"its run {approval.run_key} records a push authorization for {state.expected_branch!r}"
    if state.expected_branch not in (approval.approved_branch, approval.expected_branch):
        return f"its run {approval.run_key} was started for {state.expected_branch!r}"
    return None


def _move_refusals(
    approval: IntakeApproval,
    run: RunSlice,
    intake_dir: Path,
    repo_root: Path,
    sparring_dir: Path,
    *,
    target: str | None,
    digest: str | None = None,
) -> list[str]:
    """Every reason the approval cannot be moved; empty when it can.

    ``target`` is the branch it would move to (the current one); ``None``
    checks everything that does not depend on which branch that is.
    """

    refusals: list[str] = []
    approved = approval.approved_branch
    if approval.move:
        return [f"it was already moved from {approved!r} to {approval.expected_branch!r}"]
    if not (slice_runs_agent(run) and is_protected_branch(approved)):
        return [f"it was approved on {approved!r}, where it can run; an approved branch is not changed"]
    starting = approval.payload["starting_snapshot"]
    try:
        identity = repository_identity(repo_root)
        branch, head, dirty = repo_fingerprint(repo_root)
    except GitContextError as exc:
        return [f"cannot inspect {repo_root}: {exc}"]
    if identity != (starting["path"], starting["git_common_dir"]):
        return [f"{repo_root} is not the worktree it was approved in ({starting['path']})"]
    if target is not None:
        if branch != target:
            refusals.append(f"{repo_root} is on {branch!r}, not {target!r}")
        if is_protected_branch(branch):
            refusals.append(f"{repo_root} is still on protected branch {branch!r}")
    if head != starting["head"]:
        refusals.append(f"HEAD is {head[:12]}, not the approved commit {starting['head'][:12]}")
    primary = str(approval.payload["primary_repository"])
    allowed = set(approval.payload["repositories"]["at_approval"].get(primary, {}).get("dirty_paths") or [])
    extra = sorted(set(dirty) - allowed)
    if extra:
        refusals.append(f"the worktree has changes it did not have when approved: {', '.join(extra)}")
    for name, seen in sorted(approval.payload["repositories"]["at_approval"].items()):
        if name == primary:
            continue
        try:
            now = (repository_identity(Path(seen["path"])), *repo_fingerprint(Path(seen["path"]))[:2])
        except GitContextError as exc:
            refusals.append(f"{name}: cannot inspect {seen['path']}: {exc}")
            continue
        if now != ((seen["path"], seen["git_common_dir"]), seen["branch"], seen["head"]):
            refusals.append(f"{name} moved since approval ({seen['branch']}@{seen['head'][:12]})")
    for stage_id in approval.payload.get("stage_ids") or []:
        turned = _no_provider_turn(sparring_dir, str(stage_id), approval.run_key)
        if turned:
            refusals.append(turned)
    run_problem = _run_refusal(sparring_dir, approval, digest)
    if run_problem:
        refusals.append(run_problem)
    return refusals


@dataclass(frozen=True)
class BranchMove:
    path: Path
    from_branch: str
    to_branch: str
    created: bool
    #: The run whose recorded branch was updated, if one had been started.
    run_state: Path | None


def move_slice_approval(
    intake_dir: Path,
    *,
    run_id: str,
    repo_root: Path,
    sparring_dir: Path,
    now: Callable[[], datetime] = _utc_now,
) -> BranchMove:
    """Move a slice approved on a protected branch to the branch now checked
    out, at the same commit; see the module docstring for every condition.
    Repeating a completed move returns it (and finishes updating the run)."""

    intake_dir = Path(intake_dir)
    repo_root = Path(repo_root)
    sparring_dir = Path(sparring_dir)
    run, _ = _slice(intake_dir, run_id)
    manifest_path = _run_dir(intake_dir, run_id) / MANIFEST_FILENAME
    try:
        source = load_intake_manifest(manifest_path)
    except IntakeApprovalError as exc:
        raise IntakeError(f"run slice {run_id!r} has no usable approval: {exc}") from exc
    approval = source.approval
    move_path = approval.path.parent / BRANCH_MOVE_FILENAME
    try:
        with worktree_lock(repo_root):
            branch = current_branch(repo_root)
            if approval.move:
                if approval.expected_branch != branch:
                    raise IntakeError(
                        f"run slice {run_id!r} was already moved from {approval.approved_branch!r} to "
                        f"{approval.expected_branch!r}; a slice is moved once"
                    )
                return BranchMove(move_path, approval.approved_branch, branch, False, _update_run(sparring_dir, approval, branch))
            refusals = _move_refusals(approval, run, intake_dir, repo_root, sparring_dir, target=branch, digest=source.digest())
            if refusals:
                raise IntakeError(
                    f"cannot move {slice_stage_names(run)} off {approval.approved_branch!r}:\n- "
                    + "\n- ".join(refusals)
                )
            state_path = run_state_path(sparring_dir, approval.run_key)
            record = {
                "version": BRANCH_MOVE_VERSION,
                "run_id": run_id,
                "run_key": approval.run_key,
                "approval_sha256": approval.sha256,
                "from_branch": approval.approved_branch,
                "to_branch": branch,
                "head": approval.payload["starting_snapshot"]["head"],
                "moved_at": now().isoformat(),
                "reason": (
                    f"approved on protected branch {approval.approved_branch!r}, where no "
                    "implementation agent runs; moved to a feature branch at the approved commit "
                    "before any provider turn"
                ),
                "run_state": (
                    {"path": str(state_path), "status": PlanRunState.load(state_path).status.value}
                    if state_path.is_file()
                    else None
                ),
            }
            if not write_exclusive(move_path, json.dumps(record, indent=2) + "\n"):
                raise IntakeError(f"{move_path} was written by another process; look at it and try again")
            moved = read_approval(approval.path)
            return BranchMove(move_path, approval.approved_branch, branch, True, _update_run(sparring_dir, moved, branch))
    except WorktreeLockError as exc:
        raise IntakeError(f"{repo_root} is busy: {exc}") from exc
    except GitContextError as exc:
        raise IntakeError(f"cannot inspect {repo_root}: {exc}") from exc


def _update_run(sparring_dir: Path, approval: IntakeApproval, branch: str) -> Path | None:
    """Point the slice's paused run at the moved branch (idempotently)."""

    path = run_state_path(sparring_dir, approval.run_key)
    if not path.is_file():
        return None
    state = PlanRunState.load(path)
    if state.expected_branch != branch:
        state.expected_branch = branch
        state.save(path)
    return path


def _valid_branch_name(repo_root: Path, name: str) -> bool:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", name], cwd=repo_root, capture_output=True, text=True
    )
    return result.returncode == 0 and result.stdout.strip() == name


def create_slice_branch(
    intake_dir: Path,
    *,
    run_id: str,
    repo_root: Path,
    sparring_dir: Path,
    branch: str,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[str, BranchMove | None]:
    """Check out new branch ``branch`` at the current commit for the slice, and
    move its approval there when it was approved on a protected branch.

    Only when :func:`slice_branch_status` offers :data:`ACTION_CREATE`; the
    new branch must be a valid, non-protected, not-yet-existing name.
    """

    repo_root = Path(repo_root)
    status = slice_branch_status(intake_dir, run_id=run_id, repo_root=repo_root, sparring_dir=sparring_dir)
    if status.action != ACTION_CREATE:
        raise IntakeError(
            status.blocked
            and f"{status.stages} cannot be moved to a feature branch: {status.blocked}"
            or f"{status.stages} does not need a new branch here (on {status.current_branch!r})"
        )
    name = branch.strip()
    if not name or is_protected_branch(name) or not _valid_branch_name(repo_root, name):
        raise IntakeError(f"{branch!r} is not a branch name a stage agent may run on")
    result = subprocess.run(["git", "switch", "-c", name], cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntakeError(f"git switch -c {name} failed: {(result.stderr or result.stdout).strip()}")
    if status.approved_branch is None:
        return name, None
    return name, move_slice_approval(intake_dir, run_id=run_id, repo_root=repo_root, sparring_dir=sparring_dir, now=now)


__all__ = [
    "ACTION_CREATE",
    "ACTION_MOVE",
    "BranchMove",
    "SliceBranch",
    "create_slice_branch",
    "move_slice_approval",
    "slice_branch_status",
    "suggested_branch",
]
