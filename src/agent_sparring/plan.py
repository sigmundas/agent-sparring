"""The plan-level unattended runner: one reviewed plan, several bounded stages.

:mod:`agent_sparring.loop` automates *one* stage (implementation <-> sparring
until READY/NEEDS_YOU/ESCALATE). This module is the thin outer loop that
walks a reviewed plan's stages in order without a human launching each one::

    run-plan
        stage 1: run_unattended_loop -> READY -> freeze_candidate -> accept_candidate
        stage 2: (fresh implementation + sparring sessions) ... READY -> freeze -> accept
        stage 3: ... NEEDS_YOU -> pause, tell the human what is required
    resume-plan --evidence "..."
        stage 3: evidence appended to notes.md, SAME stage resumes ... READY -> accept
        stage 4: ...
    plan complete

It is deliberately boring: deterministic composition of the already-tested
primitives (:func:`~agent_sparring.loop.run_unattended_loop`,
:func:`~agent_sparring.acceptance.freeze_candidate`,
:func:`~agent_sparring.acceptance.accept_candidate`, :class:`~agent_sparring.
stage.Stage`), plus one integer position persisted so a paused run can be
resumed. No dependency graph, no transition table, no AI interpretation of
the plan, no second state model: candidate identity, session identity and
acceptance status stay in each stage's own ``state.json``.

Plan convention
---------------

Stages are level-2 headings of the form ``## Stage <n> — <title>`` (an
en dash, hyphen or colon also works as the separator), numbered 1..N in
document order. Everything from that heading up to the next level-1 or
level-2 heading (fenced code blocks excluded) is the stage's section, and
becomes that stage's ``brief.md`` verbatim, under a two-line header naming
the plan and position. Stage ids are derived deterministically as
``stage-<n>-<slugified title>``. Anything that looks like a stage heading
but does not fit the convention, a gap or duplicate in the numbering, an
empty section, or a plan with no stages at all is refused before any
provider is invoked.

READY is still not acceptance: the plan runner is simply the explicit
orchestration step that invokes the existing hard gate. If freeze or accept
refuses, the run stops loudly and nothing is substituted or advanced.

Plan changes: the SHA-256 of the parsed stage sections (ids + text) is
recorded when a run starts and re-checked on resume; a plan whose stage
content changed is refused rather than silently run. Prose outside the
stage sections may change freely.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_sparring.acceptance import AcceptanceError, accept_candidate, freeze_candidate
from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.loop import (
    DEFAULT_MAX_SEND_BACK_CYCLES,
    LoopError,
    run_unattended_loop,
)
from agent_sparring.providers import SparringAgentAdapter, StageAgentAdapter
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.stage import (
    HUMAN_EVIDENCE_HEADING,
    Stage,
    StageError,
    StageStatus,
    validate_stage_id,
)

PLANS_DIRNAME = "plans"

# "## Stage" at the start of a level-2 heading is claimed by the convention:
# a line matching this prefix but not the full pattern is malformed, never
# silently treated as ordinary prose.
_STAGE_PREFIX_RE = re.compile(r"^##\s+stage\b", re.IGNORECASE)
_STAGE_HEADING_RE = re.compile(
    r"^##\s+stage\s+(?P<number>\d+)\s*[—–:-]\s*(?P<title>\S.*?)\s*$", re.IGNORECASE
)
_SECTION_BOUNDARY_RE = re.compile(r"^#{1,2}\s")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

Reporter = Callable[[str], None]


class PlanError(ValueError):
    """A malformed plan, or plan-run state that cannot be started/resumed."""


class PlanRunError(RuntimeError):
    """The run stopped on a provider/integrity/acceptance failure.

    The plan position has been persisted as paused before this is raised;
    the current stage was neither accepted nor advanced.
    """


# -- plan parsing -----------------------------------------------------------


@dataclass(frozen=True)
class PlanStage:
    number: int
    title: str
    stage_id: str
    section: str  # heading line plus body, verbatim


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _stage_id_for(number: int, title: str) -> str:
    base = f"stage-{number}"
    slug = _slug(title)
    stage_id = f"{base}-{slug}"[:128].rstrip("-") if slug else base
    return validate_stage_id(stage_id)


def parse_plan(text: str) -> tuple[PlanStage, ...]:
    """Split a reviewed plan into its explicit stages, or refuse.

    Raises :class:`PlanError` for: no stages; a heading that starts with
    ``## Stage`` but does not fit ``## Stage <n> — <title>``; numbering that
    is not exactly 1..N in document order (gap, duplicate, out of order);
    an empty stage section.
    """

    stages: list[PlanStage] = []
    heading_indices: list[int] = []
    lines = text.splitlines()

    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _STAGE_PREFIX_RE.match(line):
            if not _STAGE_HEADING_RE.match(line):
                raise PlanError(
                    f"line {index + 1} looks like a stage heading but does not follow "
                    f"the convention '## Stage <n> — <title>': {line!r}"
                )
            heading_indices.append(index)

    if not heading_indices:
        raise PlanError(
            "the plan declares no stages; mark each stage with a level-2 heading "
            "'## Stage <n> — <title>'"
        )

    for position, start in enumerate(heading_indices):
        match = _STAGE_HEADING_RE.match(lines[start])
        assert match is not None  # guaranteed by the scan above
        number = int(match.group("number"))
        title = match.group("title")
        expected = position + 1
        if number != expected:
            raise PlanError(
                f"stage numbering must be 1..N in document order; found 'Stage {number}' "
                f"(line {start + 1}) where 'Stage {expected}' was expected"
            )

        end = _section_end(lines, start + 1)
        section_lines = lines[start:end]
        if not "".join(section_lines[1:]).strip():
            raise PlanError(f"'Stage {number} — {title}' (line {start + 1}) has no content")

        stages.append(
            PlanStage(
                number=number,
                title=title,
                stage_id=_stage_id_for(number, title),
                section="\n".join(section_lines).rstrip() + "\n",
            )
        )

    ids = [stage.stage_id for stage in stages]
    if len(set(ids)) != len(ids):
        raise PlanError(f"stage ids are not unique: {ids}")
    return tuple(stages)


def _section_end(lines: list[str], start: int) -> int:
    in_fence = False
    for index in range(start, len(lines)):
        line = lines[index]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and _SECTION_BOUNDARY_RE.match(line):
            return index
    return len(lines)


def plan_digest(stages: tuple[PlanStage, ...]) -> str:
    """SHA-256 over the parsed stage ids and sections -- the content a run
    actually executes. Prose outside the stage sections does not count."""

    digest = hashlib.sha256()
    for stage in stages:
        digest.update(stage.stage_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(stage.section.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def render_brief(plan_label: str, stage: PlanStage, total: int) -> str:
    """The stage's ``brief.md``: the plan section verbatim, under a short
    header naming the plan and this stage's position in it."""

    return (
        f"# Stage brief: {stage.stage_id}\n\n"
        f"Stage {stage.number} of {total} from plan `{plan_label}`. Implement only "
        "this section; the other stages are separate.\n\n"
        f"{stage.section}"
    )


# -- plan-run state ----------------------------------------------------------


class PlanRunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"


@dataclass
class PlanRunState:
    """The minimum needed to resume: which plan, which stage, and whether
    the plan has changed. Candidate SHAs, session ids and acceptance status
    are *not* duplicated here -- each stage's ``state.json`` owns those."""

    plan: str
    plan_digest: str
    expected_branch: str
    current_stage_index: int
    current_stage: str
    status: PlanRunStatus

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanRunState":
        try:
            return cls(
                plan=str(payload["plan"]),
                plan_digest=str(payload["plan_digest"]),
                expected_branch=str(payload["expected_branch"]),
                current_stage_index=int(payload["current_stage_index"]),
                current_stage=str(payload["current_stage"]),
                status=PlanRunStatus(str(payload["status"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanError(f"malformed plan-run state: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> "PlanRunState":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanError(f"cannot read plan-run state at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise PlanError(f"plan-run state at {path} must be a JSON object")
        return cls.from_dict(payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def plan_label(plan_path: Path, repo_root: Path) -> str:
    """How the plan is named in state and output: repo-relative when it lives
    inside the repository, otherwise its absolute path."""

    resolved = plan_path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def plan_state_path(sparring_dir: Path, label: str) -> Path:
    return Path(sparring_dir) / PLANS_DIRNAME / f"{_slug(label) or 'plan'}.json"


# -- running -----------------------------------------------------------------


@dataclass(frozen=True)
class PlanRunResult:
    """Where the run ended: COMPLETE (every stage accepted) or PAUSED on
    ``stage_id`` with the sparrer's NEEDS_YOU/ESCALATE ``routing``."""

    status: PlanRunStatus
    plan: str
    stage_id: str
    routing: RoutingResult | None = None
    accepted: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (stage_id, sha)


def _read_plan(plan_path: Path) -> tuple[PlanStage, ...]:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read plan {plan_path}: {exc}") from exc
    return parse_plan(text)


def _require_state_ignored(repo_root: Path, state_path: Path) -> None:
    """The plan-run state is workflow bookkeeping, like ``.sparring/stages/``;
    if git can see it, every freeze after the first state write would refuse
    the worktree as dirty. Catch that up front, before any provider turn."""

    try:
        state_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return  # outside the repository: git cannot see it
    try:
        ignored = is_ignored(repo_root, state_path)
    except GitContextError as exc:
        raise PlanError(str(exc)) from exc
    if not ignored:
        raise PlanError(
            f"plan-run state {state_path} would be visible to git, which would make "
            "every freeze-candidate refuse the worktree as dirty; add "
            f"'.sparring/{PLANS_DIRNAME}/' to .gitignore (alongside '.sparring/stages/')"
        )


def start_plan(
    plan_path: Path,
    sparring_dir: Path,
    repo_root: Path,
    stage_adapter: StageAgentAdapter,
    sparring_adapter: SparringAgentAdapter,
    *,
    expected_branch: str,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    report: Reporter = lambda message: None,
) -> PlanRunResult:
    """Validate the whole plan, record position at stage 1, and run.

    Refuses (:class:`PlanError`, before any provider is invoked) if the plan
    is malformed, if a run for this plan is already recorded (use
    :func:`resume_plan`), or if the run-state location is not git-ignored.
    """

    if not expected_branch or not expected_branch.strip():
        raise PlanError("expected_branch is required for a plan run")

    stages = _read_plan(plan_path)
    label = plan_label(plan_path, repo_root)
    state_path = plan_state_path(sparring_dir, label)
    if state_path.is_file():
        existing = PlanRunState.load(state_path)
        raise PlanError(
            f"a plan run for {label} is already recorded at {state_path} "
            f"(status {existing.status.value}, current stage {existing.current_stage!r}); "
            "use resume-plan to continue it, or delete that file to abandon it"
        )
    _require_state_ignored(repo_root, state_path)

    state = PlanRunState(
        plan=label,
        plan_digest=plan_digest(stages),
        expected_branch=expected_branch,
        current_stage_index=0,
        current_stage=stages[0].stage_id,
        status=PlanRunStatus.RUNNING,
    )
    state.save(state_path)
    report(f"plan {label}: {len(stages)} stage(s); run state at {state_path}")
    return _drive(
        stages,
        state,
        state_path,
        sparring_dir,
        repo_root,
        stage_adapter,
        sparring_adapter,
        max_send_back_cycles=max_send_back_cycles,
        self_check=self_check,
        report=report,
    )


def resume_plan(
    plan_path: Path,
    sparring_dir: Path,
    repo_root: Path,
    stage_adapter: StageAgentAdapter,
    sparring_adapter: SparringAgentAdapter,
    *,
    expected_branch: str,
    evidence: str | None = None,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    report: Reporter = lambda message: None,
) -> PlanRunResult:
    """Continue a recorded plan run at its current stage.

    ``evidence`` (a human's answer, manual-check result, external-condition
    result or scope approval -- one path for all of them) is appended to the
    current stage's ``notes.md`` under ``## Human evidence``; the existing
    handoff/prompt plumbing shows it to both agents. The *same* stage then
    resumes with its recorded sessions: no new stage is created for an
    answer, and with no code change the same SHA is simply sparred again.

    A current stage that is already ACCEPTED (by hand, after an ESCALATE
    sparred elsewhere, or by a run that stopped between accept and advance)
    is advanced past without running anything.

    Refuses (:class:`PlanError`) if no run is recorded, the run is complete,
    ``expected_branch`` differs from the recorded one, or the plan's stage
    content no longer matches the digest recorded at start.
    """

    stages = _read_plan(plan_path)
    label = plan_label(plan_path, repo_root)
    state_path = plan_state_path(sparring_dir, label)
    if not state_path.is_file():
        raise PlanError(f"no plan run is recorded for {label}; start one with run-plan")
    state = PlanRunState.load(state_path)

    if state.status is PlanRunStatus.COMPLETE:
        raise PlanError(f"plan run for {label} is already complete; nothing to resume")
    if state.expected_branch != expected_branch:
        raise PlanError(
            f"plan run for {label} was started for branch {state.expected_branch!r}, "
            f"not {expected_branch!r}; refusing to resume on a different branch"
        )
    if plan_digest(stages) != state.plan_digest:
        raise PlanError(
            f"the stage content of {label} has changed since this run started; refusing "
            "to resume against a different plan. Restore the stage sections as they "
            f"were, or abandon this run (delete {state_path}) and start a new one."
        )
    if not 0 <= state.current_stage_index < len(stages):
        raise PlanError(
            f"recorded stage index {state.current_stage_index} is out of range for "
            f"{len(stages)} stage(s)"
        )
    _require_state_ignored(repo_root, state_path)

    if evidence is not None and evidence.strip():
        current = stages[state.current_stage_index]
        stage = _ensure_stage(sparring_dir, label, current, len(stages))
        record_human_evidence(stage, evidence)
        report(f"recorded human evidence in {stage.directory / 'notes.md'}")

    return _drive(
        stages,
        state,
        state_path,
        sparring_dir,
        repo_root,
        stage_adapter,
        sparring_adapter,
        max_send_back_cycles=max_send_back_cycles,
        self_check=self_check,
        report=report,
    )


def record_human_evidence(stage: Stage, evidence: str) -> None:
    """Append ``evidence`` under ``## Human evidence`` in the stage's notes.md
    (creating the heading on first use). Prose only; nothing parses it."""

    entry = evidence.strip()
    notes = stage.read_notes().rstrip("\n")
    has_heading = any(line.strip() == HUMAN_EVIDENCE_HEADING for line in notes.splitlines())
    if not has_heading:
        notes += f"\n\n{HUMAN_EVIDENCE_HEADING}"
    stage.write_notes(f"{notes}\n\n{entry}\n")


def _ensure_stage(sparring_dir: Path, label: str, plan_stage: PlanStage, total: int) -> Stage:
    """Create the planned stage (fresh state.json, so fresh sessions) with
    the plan section as its brief, or reuse an existing one whose brief is
    exactly that section. A differing brief is refused: the reviewed plan
    is the source of truth for what a planned stage is."""

    brief = render_brief(label, plan_stage, total)
    try:
        stage = Stage.resolve(sparring_dir, plan_stage.stage_id)
        if not stage.exists():
            stage.create()
            stage.write_brief(brief)
        elif stage.read_brief() != brief:
            raise PlanError(
                f"stage {plan_stage.stage_id!r} already exists at {stage.directory} with a "
                "brief.md that differs from this plan's section; refusing to run a planned "
                "stage against a different brief"
            )
    except StageError as exc:
        raise PlanError(str(exc)) from exc
    return stage


def _pause(state: PlanRunState, state_path: Path) -> None:
    state.status = PlanRunStatus.PAUSED
    state.save(state_path)


def _drive(
    stages: tuple[PlanStage, ...],
    state: PlanRunState,
    state_path: Path,
    sparring_dir: Path,
    repo_root: Path,
    stage_adapter: StageAgentAdapter,
    sparring_adapter: SparringAgentAdapter,
    *,
    max_send_back_cycles: int,
    self_check: bool,
    report: Reporter,
) -> PlanRunResult:
    total = len(stages)
    accepted: list[tuple[str, str]] = []

    while True:
        plan_stage = stages[state.current_stage_index]
        stage = _ensure_stage(sparring_dir, state.plan, plan_stage, total)
        try:
            stage_state = stage.read_state()
        except StageError as exc:
            _pause(state, state_path)
            raise PlanRunError(f"plan {state.plan} stopped at {stage.stage_id!r}: {exc}") from exc

        if stage_state.status is StageStatus.ACCEPTED:
            # Already through the hard gate (by hand, or by a run that stopped
            # between accept and advance): nothing to run, just move on.
            report(f"stage {plan_stage.number}/{total} {stage.stage_id}: already ACCEPTED at "
                   f"{stage_state.candidate_sha}; advancing")
            accepted.append((stage.stage_id, str(stage_state.candidate_sha)))
        else:
            state.status = PlanRunStatus.RUNNING
            state.save(state_path)
            report(f"stage {plan_stage.number}/{total} {stage.stage_id}: running the "
                   "implementation <-> sparring loop")
            try:
                loop_result = run_unattended_loop(
                    stage,
                    sparring_dir,
                    repo_root,
                    stage_adapter,
                    sparring_adapter,
                    expected_branch=state.expected_branch,
                    max_send_back_cycles=max_send_back_cycles,
                    self_check=self_check,
                )
            except LoopError as exc:
                _pause(state, state_path)
                raise PlanRunError(
                    f"plan {state.plan} stopped at stage {stage.stage_id!r} (not accepted, "
                    f"not advanced): {exc}"
                ) from exc

            if loop_result.outcome is not RoutingAction.READY:
                _pause(state, state_path)
                report(f"stage {stage.stage_id}: {loop_result.outcome.value}; plan paused")
                return PlanRunResult(
                    status=PlanRunStatus.PAUSED,
                    plan=state.plan,
                    stage_id=stage.stage_id,
                    routing=loop_result.routing,
                    accepted=tuple(accepted),
                )

            # READY: invoke the existing hard gate on the exact pushed SHA.
            # Any refusal stops the plan; nothing else is ever substituted.
            try:
                frozen = freeze_candidate(
                    stage, sparring_dir, repo_root, expected_branch=state.expected_branch
                )
                result = accept_candidate(stage, repo_root, expected_branch=state.expected_branch)
            except AcceptanceError as exc:
                _pause(state, state_path)
                raise PlanRunError(
                    f"plan {state.plan} stopped at stage {stage.stage_id!r}: the sparrer said "
                    f"READY but the acceptance gate refused ({exc}); the plan did not advance"
                ) from exc
            report(f"stage {stage.stage_id}: READY; frozen and accepted {result.candidate_sha} "
                   f"({frozen.push_detail})")
            accepted.append((stage.stage_id, result.candidate_sha))

        if state.current_stage_index + 1 >= total:
            state.status = PlanRunStatus.COMPLETE
            state.save(state_path)
            report(f"plan {state.plan}: complete; {total} stage(s) accepted")
            return PlanRunResult(
                status=PlanRunStatus.COMPLETE,
                plan=state.plan,
                stage_id=stage.stage_id,
                accepted=tuple(accepted),
            )

        state.current_stage_index += 1
        state.current_stage = stages[state.current_stage_index].stage_id
        state.save(state_path)


__all__ = [
    "PLANS_DIRNAME",
    "PlanError",
    "PlanRunError",
    "PlanRunResult",
    "PlanRunState",
    "PlanRunStatus",
    "PlanStage",
    "parse_plan",
    "plan_digest",
    "plan_label",
    "plan_state_path",
    "record_human_evidence",
    "render_brief",
    "resume_plan",
    "start_plan",
]
