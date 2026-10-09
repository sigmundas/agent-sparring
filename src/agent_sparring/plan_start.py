"""``sparring start-plan``: one confirmation from a human plan to a managed run.

Two routes, chosen by whether :mod:`agent_sparring.plan` parses the plan:

- **direct**: a plan of ``## Stage <n> — <title>`` sections runs exactly as
  ``run-plan <plan.md>`` runs it. No intake and no approval file.
- **intake**: anything else is prepared in compile mode (or a matching
  finished compile intake is reused), and its first unfinished run slice of
  this project is approved through :func:`~agent_sparring.intake.
  build_approval` / :func:`~agent_sparring.intake.seal_approval` -- the same
  code ``approve-plan`` uses -- and run from the sealed manifest.

Without ``--confirm`` nothing is written beyond a prepare's own intake: the
result is a status (``refused | needs_decision | ready``, see
:data:`STATUS_VERSION` and ``docs/intake.md``) and, when ready, a
``confirm_token``. The token is a SHA-256 of everything the confirmation
binds -- intake identity and digests, decisions, this slice's manifest
digest, branch, the repositories' current commits, the resolved models and
the push flag -- so ``--confirm TOKEN`` recomputes it from disk and refuses
on any difference. ``--confirm`` never prepares.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_sparring.branch_guard import BranchGuardError, ensure_branch_for_unattended_run
from agent_sparring.git_context import GitContextError
from agent_sparring.intake import (
    DISPOSITION_NEEDS_DECISION,
    MODE_COMPILE,
    VERDICT_CANNOT_INTERPRET,
    CompileContext,
    Finding,
    IntakeError,
    PendingApproval,
    SourcePlan,
    _completed_slice,
    _read_bytes,
    _read_json,
    _read_text,
    all_findings,
    build_approval,
    parse_interpretation,
    posed_decisions,
    previous_finished_intakes,
    read_decisions,
    recorded_answers,
    repository_snapshot,
    require_intake_ignored,
    sha256_text,
)
from agent_sparring.intake_approval import APPROVAL_FILENAME, RUNS_DIRNAME, sha256_bytes
from agent_sparring.plan import (
    PlanError,
    PlanRefusal,
    find_runs,
    markdown_source_from_text,
    plan_label,
    refuse_foreign_owners,
)
from agent_sparring.plan_model import foreign_stages
from agent_sparring.sparring_agent import repo_fingerprint

#: The ``start-plan --json`` status shape (documented in docs/intake.md).
STATUS_VERSION = 1
#: Bumped whenever the token's inputs change meaning.
TOKEN_VERSION = 2

STATUS_REFUSED = "refused"
STATUS_NEEDS_DECISION = "needs_decision"
STATUS_READY = "ready"

ROUTE_DIRECT = "direct"
ROUTE_INTAKE = "intake"

#: ``prepare(parent intake dir or None, answers, validate) -> new intake dir``;
#: ``validate(interpretation, findings)`` must run before anything is written.
Prepare = Callable[["Path | None", Mapping[str, str], Callable[[Any, Any], None]], Path]


class StartPlanError(RuntimeError):
    """start-plan refuses; the message says why and what to do."""


@dataclass(frozen=True)
class StartRequest:
    plan_path: Path
    repo_root: Path
    sparring_dir: Path
    expected_branch: str
    primary_repository: str
    context_repositories: Mapping[str, Path] = field(default_factory=dict)
    repository_branches: Mapping[str, str] = field(default_factory=dict)
    answers: Mapping[str, str] = field(default_factory=dict)
    #: ``{role: {provider, model, effort}}`` as the run would resolve them.
    models: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    allow_push_for_run: bool = False
    #: Every other option that changes what runs (permission mode, provider
    #: executables as given, the send-back limit): shown and bound as given.
    execution: Mapping[str, Any] = field(default_factory=dict)
    #: A managed run (direct route only): the engine creates the branch and
    #: worktree from ``target_branch`` (default: the one checked out), so the
    #: token binds the target and its tip instead of ``expected_branch``.
    managed: bool = False
    target_branch: str | None = None
    #: ``--repository NAME=PATH``: where each repository a managed plan's
    #: stages declare (``Repository: <name>``) is checked out.
    repositories: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class StartStatus:
    """The JSON status, plus what confirming needs (never serialized):
    the sealable approval (intake route) or the exact checked plan source
    (direct route), so what was checked is what runs."""

    payload: dict[str, Any]
    pending: PendingApproval | None = None
    source: Any = None

    @property
    def status(self) -> str:
        return str(self.payload["status"])

    @property
    def route(self) -> str | None:
        return self.payload["route"]

    @property
    def token(self) -> str | None:
        return self.payload["confirm_token"]


def confirm_token(fields: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``fields`` plus the token version."""

    canonical = json.dumps(
        {"token_version": TOKEN_VERSION, **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def repository_fingerprint(name: str, path: Path) -> dict[str, str]:
    """``{name, path, git_common_dir, branch, head}`` of a repository now."""

    snapshot = repository_snapshot(Path(path))
    return {"name": name, **{key: snapshot[key] for key in ("path", "git_common_dir", "branch", "head")}}


def _base(request: StartRequest) -> dict[str, Any]:
    return refused_payload(
        request.plan_path, request.repo_root, request.expected_branch, None, execution=request.execution
    )


def refused_payload(
    plan_path: Path,
    repo_root: Path,
    expected_branch: str,
    error: str | None,
    *,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A status with every schema key present: ``refused`` with ``error``.
    Also what the CLI reports when its arguments refuse before evaluating."""

    return {
        "schema_version": STATUS_VERSION,
        "status": STATUS_REFUSED,
        "route": None,
        "plan": {
            "path": str(Path(plan_path).resolve()),
            "label": plan_label(Path(plan_path), Path(repo_root)),
        },
        "expected_branch": expected_branch,
        "execution": dict(execution) if execution is not None else None,
        "intake": None,
        "slice": None,
        "later_slices": [],
        "decisions": [],
        "findings": [],
        "confirm_token": None,
        "error": error,
    }


def evaluate(request: StartRequest, *, prepare: Prepare | None) -> StartStatus:
    """The start-plan status for ``request``. ``prepare`` is ``None`` for
    ``--confirm``, which must find an existing intake and never prepares."""

    payload = _base(request)
    try:
        if request.repositories and not request.managed:
            raise StartPlanError(
                "--repository: a plan whose stages belong to other repositories runs only --managed "
                "[cross_repository_requires_managed]"
            )
        if request.managed:
            managed_preflight(request, payload)
        else:
            preflight(request, label=payload["plan"]["label"])
        try:
            text = Path(request.plan_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise StartPlanError(f"cannot read plan {request.plan_path}: {exc}") from exc
        try:
            direct = markdown_source_from_text(Path(request.plan_path), payload["plan"]["label"], text)
        except PlanRefusal:
            # A stage plan whose ownership or gate declaration is wrong is
            # refused as such, never re-read by intake.
            raise
        except PlanError:
            direct = None
        if direct is not None:
            if not request.managed:
                refuse_foreign_owners(direct, request.primary_repository)
            if request.managed and (foreign_stages(direct.stages(), request.primary_repository) or request.repositories):
                payload["repositories"] = [
                    {"name": binding.name, **binding.to_dict()} for binding in plan_bindings(request, direct)
                ]
            if request.answers:
                raise StartPlanError("--answer: this plan runs directly, and asks no decisions")
            if request.repository_branches:
                raise StartPlanError(
                    "--repository-branch: this plan runs directly, and a Markdown plan declares no "
                    "sibling repositories to put on a branch"
                )
            return _direct_status(request, payload, direct, text)
        if request.repositories:
            raise StartPlanError(
                "--repository: only a '## Stage <n>' plan run --managed declares stage repositories "
                "[repository_unknown]"
            )
        if request.managed:
            raise StartPlanError(
                "--managed runs only a plan that runs directly ('## Stage <n>' sections); the "
                "intake route with --managed is not supported yet"
            )
        # A slice declares only repositories given with --context-repository:
        # any other branch name is refused before a preparation writes anything.
        unknown = sorted(set(request.repository_branches) - set(request.context_repositories))
        if unknown:
            raise StartPlanError(
                f"--repository-branch names {unknown}, which is not a declared sibling repository; "
                f"the repositories given are {sorted(request.context_repositories) or 'none'}"
            )
        require_intake_ignored(Path(request.repo_root), Path(request.sparring_dir))
        intake_dir, reused = locate_intake(request, label=payload["plan"]["label"], prepare=prepare)
        return _intake_status(request, payload, intake_dir, reused=reused)
    except (StartPlanError, IntakeError, PlanError, GitContextError, BranchGuardError, OSError) as exc:
        payload["status"] = STATUS_REFUSED
        payload["confirm_token"] = None
        payload["error"] = str(exc)
        return StartStatus(payload=payload)


def preflight(request: StartRequest, *, label: str) -> None:
    """Branch, protected branch, clean tree and run ownership -- before any
    provider turn, through the existing guards. start-plan never checks out
    or creates a branch."""

    repo_root = Path(request.repo_root)
    try:
        ensure_branch_for_unattended_run(repo_root, expected_branch=request.expected_branch)
    except BranchGuardError as exc:
        raise StartPlanError(
            f"{exc}. start-plan never checks out or creates a branch: create a feature branch at "
            "this commit (git switch -c <branch>; for an approved intake slice, sparring "
            "slice-branch <intake> --run <slice> --create <branch>) and run start-plan with "
            "--expected-branch <branch>"
        ) from exc
    _, _, dirty = repo_fingerprint(repo_root)
    if dirty:
        raise StartPlanError(
            f"{repo_root} has uncommitted changes ({', '.join(dirty)}); a managed run starts from "
            "a clean tree. Commit, stash or remove them first"
        )
    live = [run for run in find_runs(Path(request.sparring_dir), label) if run.open]
    if live:
        listed = ", ".join(f"{run.key} ({run.state.status.value})" for run in live)
        raise StartPlanError(
            f"{label} already has an open run: {listed}. Finish or resume it first "
            "(sparring resume-plan … --run-key <key>)"
        )


def managed_preflight(request: StartRequest, payload: dict[str, Any]) -> None:
    """The managed checks before a token: target branch and its tip, the
    project committed there, no siblings. The invoking checkout need not be
    clean -- its uncommitted changes are reported as not part of the run."""

    from agent_sparring.managed_run import ManagedRunError, committed_project, dirty_paths, resolve_target

    if request.context_repositories:
        raise StartPlanError(
            "--context-repository: a managed run is single-repository for now [sibling_repositories]"
        )
    try:
        target, base_sha = resolve_target(Path(request.repo_root), request.target_branch)
        # The worktree must be a working project at base_sha, as run-plan
        # --managed requires: never a token for a start that would refuse.
        committed_project(Path(request.repo_root), Path(request.sparring_dir), target, base_sha)
        uncommitted = dirty_paths(Path(request.repo_root))
    except ManagedRunError as exc:
        raise StartPlanError(str(exc)) from exc
    payload["managed"] = {
        "target_branch": target,
        "base_sha": base_sha,
        "not_part_of_this_run": list(uncommitted),
    }


def plan_bindings(request: StartRequest, source) -> tuple[Any, ...]:
    """The repository bindings a managed cross-repository plan records:
    this project (``primary_repository``, at its target tip) and every
    ``--repository`` its stages declare. Refuses with the binding's code."""

    from agent_sparring.logical_plan import (
        LogicalPlanError,
        bind_repository,
        require_home_first,
        resolve_bindings,
        resolve_stages,
    )

    owners = {stage.owner or request.primary_repository for stage in source.stages()}
    try:
        require_home_first(resolve_stages(source.stages(), request.primary_repository), request.primary_repository)
        home, _, _ = bind_repository(
            request.primary_repository, Path(request.repo_root), target_branch=request.target_branch
        )
        return resolve_bindings(home, request.repositories, owners)
    except LogicalPlanError as exc:
        raise StartPlanError(str(exc)) from exc


# -- direct route -----------------------------------------------------------------


def _direct_status(request: StartRequest, payload: dict[str, Any], source, text: str) -> StartStatus:
    # ``source`` was parsed from ``text``: the digests below are of what runs.
    stages = source.stages()
    later: list[dict[str, Any]] = []
    if payload.get("repositories"):
        # A plan across repositories: this part is the first run of
        # consecutive stages one repository owns; the rest continue later.
        from agent_sparring.logical_plan import derive_slices, resolve_stages

        parts = derive_slices("pending", resolve_stages(stages, request.primary_repository))
        first = set(parts[0].stages)
        later = [
            {
                "run_id": None,
                "primary_repository": part.primary_repository,
                "stages": [stage.label for stage in stages if stage.stage_id in part.stages],
            }
            for part in parts[1:]
        ]
        stages = tuple(stage for stage in stages if stage.stage_id in first)
    payload["later_slices"] = later
    payload.update(
        route=ROUTE_DIRECT,
        status=STATUS_READY,
        slice={
            "run_id": None,
            "run_key": None,
            "manifest_version": None,
            "stages": [
                {
                    "stage_id": None,
                    "label": f"Stage {stage.label}",
                    "title": stage.title,
                    "mode": stage.mode.value,
                    "plan_stage_label": stage.label,
                    "gates_before": [
                        {"id": gate.id, "title": gate.title, "kind": gate.kind, "reason": gate.reason}
                        for gate in stage.gates_before
                    ],
                }
                for stage in stages
            ],
            "completion_gates": [],
        },
    )
    payload["confirm_token"] = confirm_token(
        {
            "route": ROUTE_DIRECT,
            "plan_label": source.label,
            "plan_digest": source.digest(),
            "source_digest": sha256_text(text),
            **(
                {
                    "managed": True,
                    "target_branch": payload["managed"]["target_branch"],
                    "base_sha": payload["managed"]["base_sha"],
                }
                if request.managed
                else {"expected_branch": request.expected_branch}
            ),
            # The primary and every repository supplied with the request:
            # whatever was given is bound, never silently ignored.
            "repositories": [
                repository_fingerprint(request.primary_repository, Path(request.repo_root)),
                *(
                    repository_fingerprint(name, Path(path))
                    for name, path in sorted(request.context_repositories.items())
                ),
            ],
            # Only when stages declare repositories: a single-repository
            # token is unchanged.
            **({"bound_repositories": payload["repositories"]} if payload.get("repositories") else {}),
            "models": dict(request.models),
            "allow_push_for_run": bool(request.allow_push_for_run),
            "execution": dict(request.execution),
        }
    )
    return StartStatus(payload=payload, source=source)


# -- continuing a plan across repositories ------------------------------------


@dataclass(frozen=True)
class ContinuationStatus:
    """``resume-plan --run-key <logical>`` when the plan's next part has no
    execution yet: where it would run and the token that authorizes it.
    ``payload`` is the ``--json`` shape; ``index`` the part to create."""

    payload: dict[str, Any]
    index: int | None = None

    @property
    def token(self) -> str | None:
        return self.payload["confirm_token"]


def _slice_payload(entry) -> dict[str, Any]:
    return {
        "index": entry.index,
        "run_key": entry.id,
        "repository": entry.repository,
        "stages": list(entry.stages),
        "lifecycle": entry.lifecycle,
        "run_status": entry.run_status,
        "integrated": entry.integrated,
        "complete": entry.complete,
        "proof": entry.proof,
    }


def continuation_status(
    home_common_dir: Path, record, *, target_branch: str | None, allow_push_for_run: bool
) -> ContinuationStatus:
    """The derived status of logical plan ``record`` and, when its next part
    has no execution and the part before it is integrated, the destination
    (repository, target branch and its tip) with a confirm token binding
    the repository's path, git common dir and committed ``project``, the
    target branch, ``base_sha``, the run key and the push flag. Identical
    from every worktree of every involved repository; writes nothing."""

    from agent_sparring.logical_plan import (
        LogicalPlanError,
        check_binding_now,
        derived_status,
        membership,
        next_slice,
        previous_integration,
    )
    from agent_sparring.managed_run import ManagedRunError, dirty_paths, read_record_in

    status = derived_status(record)
    titles = {stage.stage_id: stage for stage in record.stages}
    payload: dict[str, Any] = {
        "schema_version": STATUS_VERSION,
        "status": STATUS_REFUSED,
        "plan": {"label": record.plan_label, "logical_key": record.logical_key},
        "slices": [_slice_payload(entry) for entry in status.slices],
        "next": None,
        "allow_push_for_run": bool(allow_push_for_run),
        "confirm_token": None,
        "error": None,
        "code": None,
    }
    entry = next_slice(status)
    if entry is None:
        payload.update(status="complete")
        return ContinuationStatus(payload=payload)
    binding = record.binding(entry.repository)
    payload["next"] = {
        "index": entry.index,
        "run_key": entry.id,
        "repository": entry.repository,
        "path": binding.path,
        "git_common_dir": binding.git_common_dir,
        "project": binding.project,
        "target_branch": None,
        "base_sha": None,
        "exists": entry.lifecycle is not None,
        "stages": [
            {"stage_id": stage_id, "label": titles[stage_id].label, "title": titles[stage_id].title}
            for stage_id in entry.stages
        ],
        "not_part_of_this_run": [],
    }
    if entry.lifecycle is not None:
        # Only this plan's own execution of the part is resumed: a run that
        # merely uses the derived key is refused, never adopted.
        try:
            execution = read_record_in(Path(binding.git_common_dir), entry.id)
            mismatch = membership(record, entry.index, execution) if execution is not None else "it has no record"
        except ManagedRunError as exc:
            mismatch = f"its record is unreadable: {exc}"
        if mismatch is not None:
            payload.update(
                error=f"{entry.id} in {entry.repository} is not part {entry.index} of this plan: {mismatch}; "
                "nothing was changed [execution_not_in_plan]",
                code="execution_not_in_plan",
            )
            return ContinuationStatus(payload=payload, index=entry.index)
        payload.update(status="exists")
        return ContinuationStatus(payload=payload, index=entry.index)
    try:
        refusal = previous_integration(record, status, entry.index)
        if refusal is not None:
            raise LogicalPlanError("previous_not_integrated", refusal)
        target, base_sha = check_binding_now(binding, target_branch)
        uncommitted = dirty_paths(Path(binding.path))
    except ManagedRunError as exc:
        payload.update(error=str(exc), code=exc.code)
        return ContinuationStatus(payload=payload, index=entry.index)
    payload["next"].update(target_branch=target, base_sha=base_sha, not_part_of_this_run=list(uncommitted))
    payload.update(
        status=STATUS_READY,
        confirm_token=confirm_token(
            {
                "route": "continue",
                "logical_key": record.logical_key,
                "input_digest": record.input_digest,
                "index": entry.index,
                "run_key": entry.id,
                "stage_ids": list(entry.stages),
                "repository": entry.repository,
                "path": binding.path,
                "git_common_dir": binding.git_common_dir,
                "project": binding.project,
                "target_branch": target,
                "base_sha": base_sha,
                "allow_push_for_run": bool(allow_push_for_run),
            }
        ),
    )
    return ContinuationStatus(payload=payload, index=entry.index)


# -- intake route -----------------------------------------------------------------


def _resolved_contexts(request: StartRequest) -> dict[str, str]:
    return {name: str(Path(path).resolve()) for name, path in sorted(request.context_repositories.items())}


def _still_current(directory: Path, record: Mapping[str, Any]) -> bool:
    """Whether a finished intake still describes the repositories: one of
    its slices is approved already (the approval then governs), or every
    repository it inspected is where it saw it."""

    runs = directory / RUNS_DIRNAME
    if runs.is_dir() and any(runs.glob(f"*/{APPROVAL_FILENAME}")):
        return True
    for snapshot in (record.get("repositories") or {}).values():
        try:
            present = repository_snapshot(Path(str(snapshot.get("path"))))
        except IntakeError:
            return False
        keys = ("path", "git_common_dir", "branch", "head")
        if any(present[key] != snapshot.get(key) for key in keys):
            return False
    return True


def locate_intake(request: StartRequest, *, label: str, prepare: Prepare | None) -> tuple[Path, bool]:
    """``(intake dir, reused)``: the newest finished compile intake of this
    plan whose source digest, primary and context repositories and decision
    answers match and that still describes the repositories; else, unless
    ``prepare`` is ``None``, a new one (answering the newest matching intake
    that asks the remaining decisions, when there are answers)."""

    try:
        text = Path(request.plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise StartPlanError(f"cannot read plan {request.plan_path}: {exc}") from exc
    digest = sha256_text(text)
    contexts = _resolved_contexts(request)
    answers = dict(request.answers)
    candidates = [
        prior
        for prior in reversed(previous_finished_intakes(Path(request.sparring_dir), label))
        if prior.record.get("mode") == MODE_COMPILE
        and prior.record.get("source_digest") == digest
        and prior.record.get("primary_repository") == request.primary_repository
        and (prior.record.get("context_repositories") or {}) == contexts
    ]
    for prior in candidates:
        if recorded_answers(prior.record) == answers and _still_current(prior.directory, prior.record):
            return prior.directory, True

    def validate(interpretation, findings) -> None:
        # A refusal the prepared intake would end in is made before it is written.
        if interpretation.verdict == VERDICT_CANNOT_INTERPRET or any(f.blocking for f in findings):
            return  # needs_decision or refused on its own, never reaching the slice
        ours = [run for run in interpretation.runs if run.primary_repository == request.primary_repository]
        if ours:
            _check_repository_branches(request, ours[0])

    if prepare is None:
        raise StartPlanError(
            "--confirm never prepares, and no finished compile intake of this plan matches its "
            "source, repositories and answers. Run start-plan without --confirm first, and "
            "confirm the token it prints"
        )
    if not answers:
        return prepare(None, {}, validate), False
    for prior in candidates:
        given = recorded_answers(prior.record)
        if any(answers.get(key) != value for key, value in given.items()):
            continue
        remaining = set(answers) - set(given)
        if not remaining:
            continue
        try:
            interpretation = parse_interpretation(_read_text(prior.directory / "interpretation.json"), mode=MODE_COMPILE)
        except IntakeError:
            continue
        if remaining <= set(posed_decisions(interpretation)):
            return prepare(prior.directory, answers, validate), False
    raise StartPlanError(
        f"--answer {sorted(answers)}: no prepared intake of this plan asks these decisions. Run "
        "start-plan without --answer to see the questions it asks"
    )


def _finding(f: Finding) -> dict[str, Any]:
    return {
        "code": f.code,
        "severity": f.severity,
        "disposition": f.disposition,
        "origin": f.origin,
        "message": f.message,
        "stages": [s for s in f.stages if s],
    }


def _decision(f: Finding) -> dict[str, Any]:
    decision = f.decision
    assert decision is not None
    return {
        "id": decision.id,
        "question": decision.question,
        "why": decision.why,
        "options": [{"id": o.id, "label": o.label, "consequence": o.consequence} for o in decision.options],
        "stages": [s for s in f.stages if s],
        "finding": f.code,
    }


def _intake_status(request: StartRequest, payload: dict[str, Any], intake_dir: Path, *, reused: bool) -> StartStatus:
    record_raw = _read_bytes(intake_dir / "intake.json")
    record = _read_json(intake_dir / "intake.json")
    payload.update(
        route=ROUTE_INTAKE,
        intake={
            "id": record.get("intake_id"),
            "directory": str(intake_dir),
            "report": str(intake_dir / "report.md"),
            "mode": record.get("mode"),
            "reused": reused,
            "answers": recorded_answers(record),
        },
    )
    snapshot = _read_text(intake_dir / "source.md")
    source = SourcePlan(label=str(record.get("plan_label")), text=snapshot)
    interpretation = parse_interpretation(_read_text(intake_dir / "interpretation.json"), mode=MODE_COMPILE)
    findings = all_findings(
        interpretation, source, mode=MODE_COMPILE, compile_context=CompileContext.from_record(record)
    )
    payload["findings"] = [_finding(f) for f in findings]
    blocking = [f for f in findings if f.blocking]
    if blocking or interpretation.verdict == VERDICT_CANNOT_INTERPRET:
        asks = [f for f in blocking if f.disposition == DISPOSITION_NEEDS_DECISION and f.decision is not None]
        if asks and len(asks) == len(blocking) and interpretation.verdict != VERDICT_CANNOT_INTERPRET:
            payload["status"] = STATUS_NEEDS_DECISION
            payload["decisions"] = [_decision(f) for f in asks]
            return StartStatus(payload=payload)
        listed = "; ".join(f"{f.code}: {f.message}" for f in blocking) or "the intake agent could not interpret the plan"
        raise StartPlanError(
            f"the plan cannot run as prepared ({len(blocking)} blocking finding(s) a decision does "
            f"not resolve): {listed}. Edit the plan and run start-plan again; see "
            f"{intake_dir / 'report.md'}"
        )

    run = _first_unfinished_slice(interpretation, intake_dir, request.primary_repository)
    inspected = record.get("repositories") or {}
    siblings = sorted({name for stage in run.stages for name in stage.repositories})
    missing = [name for name in siblings if name not in request.context_repositories]
    if missing:
        raise StartPlanError(
            f"run slice {run.id!r} declares sibling repositories {missing}; give each with "
            "--context-repository NAME=PATH"
        )
    _check_repository_branches(request, run)
    branches = {
        name: request.repository_branches.get(name) or str((inspected.get(name) or {}).get("branch") or "")
        for name in siblings
    }
    pending = build_approval(
        intake_dir,
        run_id=run.id,
        repo_root=Path(request.repo_root),
        sparring_dir=Path(request.sparring_dir),
        primary_repository=request.primary_repository,
        expected_branch=request.expected_branch,
        # As given, exactly as approve-plan records --repository NAME=PATH.
        repositories={name: str(request.context_repositories[name]) for name in siblings},
        repository_branches=branches,
    )
    envelope = json.loads(pending.manifest_text)
    manifest = envelope["manifest"]
    contracts = record.get("stages") or {}
    payload["slice"] = {
        "run_id": run.id,
        "run_key": pending.run_key,
        "manifest_version": manifest.get("version"),
        "stages": [
            {
                "stage_id": entry["stage_id"],
                "label": entry["label"],
                "title": entry["title"],
                "mode": entry.get("mode", "implementation"),
                "plan_stage_label": (contracts.get(entry["stage_id"]) or {}).get("plan_stage_label"),
                "gates_before": list(entry.get("gates_before") or []),
            }
            for entry in pending.stages
        ],
        "completion_gates": list(manifest.get("completion_gates") or []),
    }
    payload["later_slices"] = [
        {
            "run_id": other.id,
            "primary_repository": other.primary_repository,
            "stages": [stage.label for stage in other.stages],
        }
        for other in interpretation.runs
        if other.id != run.id
    ]
    decisions = record.get("decisions") if isinstance(record.get("decisions"), Mapping) else None
    read_decisions(intake_dir, record)  # refuses an edited decisions.json
    payload["status"] = STATUS_READY
    payload["confirm_token"] = confirm_token(
        {
            "route": ROUTE_INTAKE,
            "intake_id": record.get("intake_id"),
            "intake_record_sha256": sha256_bytes(record_raw),
            "report_digest": record.get("report_digest"),
            "interpretation_digest": record.get("interpretation_digest"),
            "decisions_digest": decisions.get("digest") if decisions else None,
            "run_id": run.id,
            "run_key": pending.run_key,
            "manifest_digest": pending.manifest_digest,
            "source_digest": record.get("source_digest"),
            "expected_branch": pending.expected_branch,
            "repositories": [
                repository_fingerprint(request.primary_repository, Path(request.repo_root)),
                *(
                    repository_fingerprint(name, Path(request.context_repositories[name]))
                    for name in siblings
                ),
            ],
            "models": dict(request.models),
            "allow_push_for_run": bool(request.allow_push_for_run),
            "execution": dict(request.execution),
        }
    )
    return StartStatus(payload=payload, pending=pending)


def _check_repository_branches(request: StartRequest, run) -> None:
    """Refuses a ``--repository-branch`` for a repository ``run`` does not declare."""

    siblings = sorted({name for stage in run.stages for name in stage.repositories})
    undeclared = sorted(set(request.repository_branches) - set(siblings))
    if undeclared:
        raise StartPlanError(
            f"--repository-branch names {undeclared}, which run slice {run.id!r} does not declare; "
            f"its declared sibling repositories are {siblings or 'none'}"
        )


def _first_unfinished_slice(interpretation, intake_dir: Path, primary_repository: str):
    """The first run slice, in order, that runs in this project and is not
    proven complete. start-plan handles that one; the rest are listed."""

    ours = [run for run in interpretation.runs if run.primary_repository == primary_repository]
    if not ours:
        raise StartPlanError(
            f"no run slice of this plan runs in {primary_repository!r}; start it from the project "
            f"of {sorted({run.primary_repository for run in interpretation.runs})}"
        )
    for run in ours:
        try:
            _completed_slice(intake_dir, run.id)
        except IntakeError:
            return run
    raise StartPlanError(f"every run slice of this plan in {primary_repository!r} has completed")


__all__ = [
    "ROUTE_DIRECT",
    "ROUTE_INTAKE",
    "STATUS_NEEDS_DECISION",
    "STATUS_READY",
    "STATUS_REFUSED",
    "STATUS_VERSION",
    "StartPlanError",
    "StartRequest",
    "StartStatus",
    "TOKEN_VERSION",
    "ContinuationStatus",
    "confirm_token",
    "continuation_status",
    "evaluate",
    "plan_bindings",
    "locate_intake",
    "preflight",
    "refused_payload",
]
