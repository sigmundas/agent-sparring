"""The plan-level unattended runner: one plan, several bounded stages.

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

Two plan inputs, one runner
---------------------------

The runner takes a :class:`~agent_sparring.plan_model.PlanSource` and never
learns which kind it got:

- :class:`MarkdownPlanSource` -- the original convention. Stages are level-2
  headings of the form ``## Stage <n> — <title>`` (an en dash, hyphen or
  colon also works as the separator), numbered 1..N in document order.
  Everything from that heading up to the next level-1 or level-2 heading
  (fenced code blocks excluded) is the stage's section, and becomes that
  stage's ``brief.md`` verbatim, under a two-line header naming the plan and
  position. Stage ids are derived deterministically as ``<plan key>-stage-
  <n>-<slugified title>``, where the plan key (see :func:`plan_key`) is a
  short hash of the plan's path, so two plans with the same headings never
  share stage artifacts. Anything that looks like a stage heading but does
  not fit the convention, a gap or duplicate in the numbering, an empty
  section, or a plan with no stages at all is refused before any provider is
  invoked.
- :class:`~agent_sparring.manifest.ManifestPlanSource` -- an execution
  manifest (see :mod:`agent_sparring.manifest`) emitted by a caller that
  already interpreted the human document: explicit stage ids, labels like
  ``3A``/``3B``/``3C``, exact briefs, and the order. Numeric ``1..N`` labels
  are not required, because *order* comes from the manifest's array and
  labels are display only.

Both produce the same ordered :class:`~agent_sparring.plan_model.
PlannedStage` tuple and feed the same ``_drive``. There is one plan-state
model, one position, one set of transitions.

READY is still not acceptance: the plan runner is simply the explicit
orchestration step that invokes the existing hard gate. If freeze or accept
refuses, the run stops loudly and nothing is substituted or advanced. For a
cross-repository stage the gate freezes and re-verifies the *complete*
reviewed candidate set (see :class:`~agent_sparring.stage.
CandidateRepository`), so acceptance cannot claim a stage whose sibling
candidate moved.

Plan changes: the source's own SHA-256 over exactly what it executes
(Markdown: the parsed stage sections; manifest: the whole executable
content plus the source document's digest) is recorded when a run starts and
re-checked on resume, before accepting, and before advancing. Content that
changed is refused rather than silently run. For a Markdown plan, prose
outside the stage sections may still change freely.

Answering NEEDS_YOU
-------------------

``resume-plan --evidence`` records a human's answer in the stage's
``notes.md`` and then resumes the *sparrer* against the unchanged candidate,
not the stage agent (``start_with="sparring"``; see
:func:`~agent_sparring.loop.run_unattended_loop`). The human satisfied a
review gate; there is nothing to implement, and starting an implementation
turn to carry the message would move the candidate the reviewer is being
asked about. If the sparrer then says SEND_BACK there *is* implementation
work and the ordinary loop takes over from the stage agent; READY accepts
and advances; NEEDS_YOU and ESCALATE leave the plan paused.

That READY is where a human-gated stage differs from every other one, and
the loop -- not this module -- is what closes the difference. A stage whose
implementation was deliberately left uncommitted until a human verified it
reaches READY with the reviewed work still in the working tree and HEAD
still at the stage's base, so there is no commit for the gate to freeze.
:func:`~agent_sparring.loop.run_unattended_loop` therefore does not call a
READY terminal until the reviewed candidate is a commit: it routes one
bounded commit/push turn, verifies path by path that the committed content
is the content that was reviewed, and returns the verdict of the sparring
turn over that exact SHA. By the time a READY reaches the freeze below,
there is a real candidate and it holds the work the human actually
verified. See that module's "Finalization" section; nothing about the gate
here is relaxed for it.

Pushing a verified candidate
----------------------------

The acceptance gate only freezes a commit that is already reachable from its
intended remote branch, and that has always been the rule. What used to
happen when it was not -- the reviewer said READY over a committed but
unpushed candidate, the gate refused, and nothing in the workflow could get
past it -- is now a typed stop: :mod:`agent_sparring.push_gate` reports that
this exact candidate needs a person's permission to be pushed, the run pauses
with that recorded in its state, and a resume carrying the permission pushes
exactly that commit, re-proves reachability, and then runs the same
unchanged gate. Permission can cover one candidate or every later verified
candidate of this run; without it nothing is ever pushed. See that module for
the two scopes, what the push is allowed to be, and what it is not.

Adapters are built per planned stage
------------------------------------

The runner takes an :data:`AdapterFactory` -- ``make_adapters(stage) ->
(stage_adapter, sparring_adapter)`` -- and calls it once each time it enters
a stage for execution, so a caller can bind each stage's provider telemetry
(see :mod:`agent_sparring.activity`) to that stage's own ``activity.jsonl``
instead of the first stage's. This module never learns which providers the
factory builds. Provider session continuity does not depend on any adapter
*object* surviving: the session ids persisted in each stage's ``state.json``
are what ``run_stage_agent``/``run_sparring_agent`` pass to ``resume``.

Observational plan events
-------------------------

The runner mirrors its own decisions into the *current* stage's
``activity.jsonl`` under the actor ``plan`` (``plan.stage.entered``,
``plan.stage.accepted``, ``plan.paused``, ``plan.failed``,
``plan.completed``, ``plan.evidence_recorded``). That stream is telemetry
only: nothing here reads it back, and the run-state file plus each stage's
``state.json`` remain the only authority for position, acceptance, pause,
completion, evidence and sessions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_sparring.acceptance import (
    AcceptanceError,
    accept_candidate,
    accept_reviewed_candidate,
    freeze_candidate,
)
from agent_sparring.activity import ActivityEmitter
from agent_sparring.finalization import FinalizationError, pending_finalization
from agent_sparring.git_context import GitContextError, is_ignored, resolve_commit
from agent_sparring.loop import (
    DEFAULT_MAX_SEND_BACK_CYCLES,
    LoopError,
    run_unattended_loop,
)
from agent_sparring.manifest import ManifestError, ManifestPlanSource, load_manifest_source
from agent_sparring.plan_model import PlanSource, PlannedStage, digest_planned_stages
from agent_sparring.push_gate import (
    PushAuthorization,
    PushError,
    PushRequired,
    authorization_for_candidate,
    authorization_for_run,
    ensure_candidate_pushed,
)
from agent_sparring.providers import SparringAgentAdapter, StageAgentAdapter
from agent_sparring.review import (
    ReviewError,
    ReviewResult,
    declare_stage_mode,
    enter_review,
    run_independent_review,
)
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import RecordedOutcome, read_recorded_outcome
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

# Builds the (stage_adapter, sparring_adapter) pair for one planned stage.
# Called once per stage entered for execution; may return the same objects
# every time (a stage-agnostic caller, or a test) or fresh ones bound to the
# given stage's activity log. Session continuity never depends on which.
AdapterFactory = Callable[[Stage], tuple[StageAgentAdapter, SparringAgentAdapter]]


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


def plan_key(label: str) -> str:
    """A short, deterministic, collision-safe identity for one plan.

    Readable stem plus the first 8 hex digits of the label's SHA-256, e.g.
    ``foo-3f9a2c1b``. Two distinct plan paths never share a key even when
    their slugs coincide (``docs/plan.md`` vs ``docs-plan.md``). Used to name
    the run-state file and to namespace planned stage ids, so two plans that
    both contain "Stage 1 — Foundation" never share stage artifacts.
    """

    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    stem = _slug(Path(label).stem)[:32].rstrip("-")
    return f"{stem}-{digest}" if stem else digest


def _stage_id_for(number: int, title: str, plan_key: str | None) -> str:
    base = f"stage-{number}"
    if plan_key:
        base = f"{plan_key}-{base}"
    slug = _slug(title)
    stage_id = f"{base}-{slug}"[:128].rstrip("-") if slug else base
    return validate_stage_id(stage_id)


def parse_plan(text: str, *, plan_key: str | None = None) -> tuple[PlanStage, ...]:
    """Split a reviewed plan into its explicit stages, or refuse.

    ``plan_key`` (see :func:`plan_key`), when given, prefixes every stage id
    so stages of different plans live in different directories.

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
                stage_id=_stage_id_for(number, title, plan_key),
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
    """SHA-256 over the parsed stage numbers, titles and sections -- the
    content a run actually executes. Prose outside the stage sections does
    not count."""

    digest = hashlib.sha256()
    for stage in stages:
        for part in (str(stage.number), stage.title, stage.section):
            digest.update(part.encode("utf-8"))
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
    #: Which plan input this run executes, ``"markdown"`` or ``"manifest"``.
    #: Absent from state written before manifests existed, and read as
    #: ``"markdown"`` then; a resume with a different kind is refused, since
    #: the two describe different execution content for the same plan.
    source: str = "markdown"
    #: What a person has allowed this run to push, and for exactly what (see
    #: :mod:`agent_sparring.push_gate`). ``None`` -- including for every run
    #: recorded before push authorization existed -- means no authorization:
    #: a verified candidate that is not on its intended remote branch stops
    #: the run and asks.
    push_authorization: PushAuthorization | None = None
    #: Why this run is stopped, when the reason is a typed one the runner
    #: itself recorded rather than a reviewer's verdict. Today that is only
    #: :class:`~agent_sparring.push_gate.PushRequired`. Cleared whenever the
    #: run enters a stage for execution, so it can never describe an older
    #: pause than the one the run is actually in.
    awaiting: PushRequired | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["push_authorization"] = (
            self.push_authorization.to_dict() if self.push_authorization is not None else None
        )
        payload["awaiting"] = self.awaiting.to_dict() if self.awaiting is not None else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanRunState":
        try:
            authorization = payload.get("push_authorization")
            awaiting = payload.get("awaiting")
            return cls(
                plan=str(payload["plan"]),
                plan_digest=str(payload["plan_digest"]),
                expected_branch=str(payload["expected_branch"]),
                current_stage_index=int(payload["current_stage_index"]),
                current_stage=str(payload["current_stage"]),
                status=PlanRunStatus(str(payload["status"])),
                source=str(payload.get("source", "markdown")),
                push_authorization=(
                    PushAuthorization.from_dict(authorization) if authorization else None
                ),
                awaiting=PushRequired.from_dict(awaiting) if awaiting else None,
            )
        except PushError as exc:
            raise PlanError(f"malformed plan-run state: {exc}") from exc
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
    return Path(sparring_dir) / PLANS_DIRNAME / f"{plan_key(label)}.json"


# -- plan sources ------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownPlanSource:
    """A :class:`~agent_sparring.plan_model.PlanSource` over a reviewed
    Markdown plan: the original ``## Stage <n> — <title>`` convention,
    unchanged.

    ``digest`` is still :func:`plan_digest` over the parsed stage numbers,
    titles and sections, so a run recorded before manifests existed
    re-validates against exactly the same value.
    """

    path: Path
    label: str
    parsed: tuple[PlanStage, ...]
    kind: str = "markdown"

    def digest(self) -> str:
        return plan_digest(self.parsed)

    def stages(self) -> tuple[PlannedStage, ...]:
        total = len(self.parsed)
        return tuple(
            PlannedStage(
                position=stage.number,
                label=str(stage.number),
                title=stage.title,
                stage_id=stage.stage_id,
                brief=render_brief(self.label, stage, total),
            )
            for stage in self.parsed
        )

    def reload(self) -> "MarkdownPlanSource":
        return load_markdown_source(self.path, self.label)

    def describe(self) -> str:
        return f"plan {self.label}"


def load_markdown_source(plan_path: Path, label: str) -> MarkdownPlanSource:
    try:
        text = Path(plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read plan {plan_path}: {exc}") from exc
    return MarkdownPlanSource(
        path=Path(plan_path), label=label, parsed=parse_plan(text, plan_key=plan_key(label))
    )


def load_plan_source(path: Path, repo_root: Path, *, manifest: bool = False) -> PlanSource:
    """Read ``path`` as the plan input the runner will execute.

    ``manifest`` picks the execution-manifest reader; otherwise the file is
    read as a reviewed Markdown plan. Both are validated in full here,
    before any run state is written or any provider is invoked.
    """

    if manifest:
        try:
            return load_manifest_source(Path(path))
        except ManifestError as exc:
            raise PlanError(str(exc)) from exc
    return load_markdown_source(Path(path), plan_label(Path(path), Path(repo_root)))


def _coerce_source(plan: "Path | str | PlanSource", repo_root: Path) -> PlanSource:
    """Accept either a ready plan source or a Markdown plan path.

    The path form is the original API and is what every existing caller and
    test passes; it means "read this as a reviewed Markdown plan".
    """

    if isinstance(plan, (str, Path)):
        return load_plan_source(Path(plan), repo_root, manifest=False)
    return plan


def describe_source(source: PlanSource) -> str:
    describe = getattr(source, "describe", None)
    return describe() if callable(describe) else f"plan {source.label}"


# -- running -----------------------------------------------------------------


@dataclass(frozen=True)
class PlanRunResult:
    """Where the run ended: COMPLETE (every stage accepted) or PAUSED on
    ``stage_id`` with the sparrer's NEEDS_YOU/ESCALATE ``routing`` -- or with
    ``recorded``, when the stage was already stopped for a human before this
    run reached it."""

    status: PlanRunStatus
    plan: str
    stage_id: str
    routing: RoutingResult | None = None
    accepted: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (stage_id, sha)
    #: Set instead of ``routing`` when the run stopped at a verdict that was
    #: *already on disk* rather than one it produced -- see
    #: :func:`_recorded_pause`. The two are never both set.
    recorded: RecordedOutcome | None = None
    #: Set instead of either when the run stopped on a typed reason of its
    #: own rather than on a reviewer's verdict: a verified candidate that
    #: needs push authorization (see :mod:`agent_sparring.push_gate`). The
    #: reviewer said READY; what is missing is a person's permission, not a
    #: review outcome, so this is not dressed up as one.
    awaiting: PushRequired | None = None


def _verify_source_unchanged(source: PlanSource, state: PlanRunState) -> tuple[PlannedStage, ...]:
    """Re-read the plan input from disk and require its digest to equal the
    one recorded when the run started. The one check used on resume, before
    accepting a READY stage, and before advancing -- so execution content
    edited (even committed) mid-run is caught, not executed.

    A plan input that can no longer be *read* at all is the same finding and
    is reported as such: both failures raise :class:`PlanError`, so a caller
    has one exception type to handle.
    """

    try:
        fresh = source.reload()
    except (PlanError, ManifestError) as exc:
        # The input on disk can no longer be read as the thing this run
        # started against. That is the same fact as a digest mismatch -- the
        # run's execution content changed -- but it arrives as a parse error,
        # which on its own reads like a malformed document rather than like a
        # run whose definition was edited underneath it. Appending an
        # implementation record whose own '## Stage 1' heading follows the
        # plan's 'Stage 1..7' is exactly this case, so say which situation the
        # reader is in before handing over the parser's detail.
        raise PlanError(
            f"{state.plan} can no longer be read as the plan this run started against "
            f"({exc}); a managed run's plan must not be edited while it is running. "
            "Restore it as it was, or deliberately start over (see run-plan's refusal "
            "message for what to remove)."
        ) from exc
    if fresh.digest() != state.plan_digest:
        raise PlanError(
            f"the executable content of {state.plan} has changed since this run started; "
            "refusing to continue against a different plan. Restore it as it was, or "
            "deliberately start over (see run-plan's refusal message for what to remove)."
        )
    return fresh.stages()


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
        raise PlanError(plan_state_not_ignored_message(repo_root, state_path))


def plan_state_not_ignored_message(repo_root: Path, state_path: Path) -> str:
    """The one refusal every project hits once, written so it can be fixed
    without reading the source.

    This check is deliberately loud and deliberately not skippable: a
    plan-run state file that git can see is written before the first
    provider turn and rewritten at every stage boundary, so *every*
    subsequent ``freeze-candidate`` would refuse the worktree as dirty --
    after spending provider turns. Failing here costs nothing; failing there
    costs a whole stage.
    """

    try:
        shown = Path(state_path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        shown = str(state_path)
    return (
        f"plan-run state {shown} is not ignored by git. The plan runner rewrites it at "
        "every stage boundary, so leaving it visible would make every freeze-candidate "
        "refuse the worktree as dirty part-way through a run.\n"
        f"Fix: add a line '.sparring/{PLANS_DIRNAME}/' to {repo_root}/.gitignore, next to "
        "the '.sparring/stages/' line the same workflow already needs. Both are workflow "
        "bookkeeping; '.sparring/project.toml' and '.sparring/PROJECT.md' stay tracked.\n"
        "Check it with: sparring check-config"
    )


def start_plan(
    plan: "Path | str | PlanSource",
    sparring_dir: Path,
    repo_root: Path,
    make_adapters: AdapterFactory,
    *,
    expected_branch: str,
    adopt: bool = False,
    allow_push_for_run: bool = False,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    stop_after_stage: str | None = None,
    report: Reporter = lambda message: None,
) -> PlanRunResult:
    """Validate the whole plan, record position at stage 1, and run.

    ``plan`` is either a :class:`~agent_sparring.plan_model.PlanSource` or a
    path to a reviewed Markdown plan (the original form).

    ``make_adapters`` (an :data:`AdapterFactory`) is called once per stage
    entered for execution, with that stage, and returns the stage and
    sparring adapters to drive it with.

    ``allow_push_for_run`` records, as part of creating the run, that this
    run may push the verified candidates it produces to their intended remote
    branch (see :mod:`agent_sparring.push_gate`). Without it the run has no
    push authorization at all and stops the first time a verified candidate
    is not already on the remote.

    ``adopt`` opts in to running a plan whose stages *already exist* on disk
    -- the migration case, where a sequence was driven stage by stage before
    it was ever managed. Without it, any pre-existing stage directory refuses
    the run, because a fresh run means fresh stages and inheriting an old
    session id or an old ACCEPTED status silently is exactly the accident
    worth refusing. With it, each existing stage is checked against
    :func:`_check_adoption`'s rules and every adoption is reported.

    Refuses (:class:`PlanError`, before any provider is invoked) if the plan
    is malformed, if a run for this plan is already recorded (use
    :func:`resume_plan`), if a stage directory exists that adoption does not
    allow, or if the run-state location is not git-ignored.
    """

    if not expected_branch or not expected_branch.strip():
        raise PlanError("expected_branch is required for a plan run")

    source = _coerce_source(plan, repo_root)
    label = source.label
    stages = source.stages()
    state_path = plan_state_path(sparring_dir, label)
    if state_path.is_file():
        existing = PlanRunState.load(state_path)
        raise PlanError(
            f"refusing a fresh run of {label}: a run is already recorded at {state_path} "
            f"(status {existing.status.value}, current stage {existing.current_stage!r}); "
            "use resume-plan to continue it. To genuinely start over, deliberately remove "
            "that file; nothing is deleted automatically."
        )

    leftovers = [
        stage for stage in stages if Stage.resolve(sparring_dir, stage.stage_id).directory.exists()
    ]
    if leftovers and not adopt:
        listed = ", ".join(
            str(Stage.resolve(sparring_dir, stage.stage_id).directory) for stage in leftovers
        )
        raise PlanError(
            f"refusing a fresh run of {label}: {len(leftovers)} of its stage directories "
            f"already exist ({listed}). A fresh run means fresh stages, so old session ids "
            "and an old ACCEPTED status are never inherited by accident. Either remove "
            "those directories deliberately, or pass --adopt to continue the existing "
            "sequence (each stage is then checked and reported, never silently inherited)."
        )
    if leftovers:
        _check_adoption(sparring_dir, stages, leftovers, report=report)
    _require_state_ignored(repo_root, state_path)

    state = PlanRunState(
        plan=label,
        plan_digest=source.digest(),
        expected_branch=expected_branch,
        current_stage_index=0,
        current_stage=stages[0].stage_id,
        status=PlanRunStatus.RUNNING,
        source=source.kind,
    )
    if allow_push_for_run:
        try:
            state.push_authorization = authorization_for_run(
                Path(repo_root), branch=expected_branch
            )
        except PushError as exc:
            raise PlanError(str(exc)) from exc
        report(f"push authorization recorded for this run: {state.push_authorization.describe}")
    state.save(state_path)
    report(
        f"{describe_source(source)}: {len(stages)} stage(s) from {source.kind}; "
        f"run state at {state_path}"
    )
    return _drive(
        source,
        stages,
        state,
        state_path,
        sparring_dir,
        repo_root,
        make_adapters,
        max_send_back_cycles=max_send_back_cycles,
        self_check=self_check,
        report=report,
        stop_after_stage=stop_after_stage,
    )


def _check_adoption(
    sparring_dir: Path,
    stages: tuple[PlannedStage, ...],
    existing: list[PlannedStage],
    *,
    report: Reporter,
) -> None:
    """Decide, per already-existing stage, whether this run may take it over.

    Three cases, and nothing in between is guessed:

    - **ACCEPTED** with a real candidate: adopt and later advance past it.
      Its brief is history and is not compared -- the work went through the
      hard gate already, and demanding that a stage accepted months ago be
      briefed byte-identically to today's manifest would refuse exactly the
      sequences worth adopting.
    - **Not accepted, brief identical to the source's**: adopt. This is the
      current stage of a sequence being taken over mid-flight; its recorded
      sessions and candidate continue as they are.
    - **Anything else** -- an unreadable state, a missing brief, or a brief
      that differs: refuse, naming the stage. A stage that has actually run
      against a different brief is not this plan's stage, and silently
      re-briefing it would throw away the context its sessions hold.

    Note what this does *not* require of a manifest. Once a stage has real
    execution history, its ``brief.md`` is the contract that was actually
    implemented and reviewed, and the plan section it was extracted from is
    free to move on -- typically because the plan was rewritten afterwards to
    record what was built. A caller emitting an adoption manifest is expected
    to carry such a stage's existing ``brief.md`` verbatim, and only brief
    stages that do *not* exist yet from the plan's current section. Then this
    check passes on the truth rather than on a coincidence, and neither the
    brief nor the plan document has to be rolled back to run the sequence.

    Reports every adoption, including what is being inherited, so nothing is
    taken over quietly.
    """

    known = {stage.stage_id for stage in existing}
    problems: list[str] = []
    for planned in stages:
        if planned.stage_id not in known:
            continue
        stage = Stage.resolve(sparring_dir, planned.stage_id)
        try:
            state = stage.read_state()
        except StageError as exc:
            problems.append(f"{planned.stage_id}: its state cannot be read ({exc})")
            continue
        if state.status is StageStatus.ACCEPTED:
            if not state.candidate_sha:
                problems.append(
                    f"{planned.stage_id}: recorded as ACCEPTED but with no candidate commit"
                )
                continue
            report(
                f"adopting {planned.display} [{planned.stage_id}]: already ACCEPTED at "
                f"{state.candidate_sha}; it will be advanced past, not re-run"
            )
            continue
        try:
            brief = stage.read_brief()
        except StageError as exc:
            problems.append(f"{planned.stage_id}: its brief.md cannot be read ({exc})")
            continue
        if brief != planned.brief:
            problems.append(
                f"{planned.stage_id}: status {state.status.value} and its brief.md differs "
                "from this plan's text for that stage"
            )
            continue
        inherited = ", ".join(
            part
            for part in (
                f"implementation session {state.implementation_session_id}"
                if state.implementation_session_id
                else "",
                f"sparring session {state.sparring_session_id}"
                if state.sparring_session_id
                else "",
                f"candidate {state.candidate_sha}" if state.candidate_sha else "",
            )
            if part
        )
        report(
            f"adopting {planned.display} [{planned.stage_id}]: status "
            f"{state.status.value}, brief matches; inheriting "
            f"{inherited or 'no recorded sessions or candidate'}"
        )

    if problems:
        listed = "\n  - ".join(problems)
        raise PlanError(
            "refusing to adopt the existing stages of this plan; the following do not "
            f"match it and would have to be guessed at:\n  - {listed}\n"
            "A stage that has already run is defined by its own brief.md, not by what the "
            "plan says today. Carry that brief verbatim in the plan input (a manifest can), "
            "or align the plan's text with it, or remove the stage directory deliberately -- "
            "rather than running a started stage against a different brief."
        )


def resume_plan(
    plan: "Path | str | PlanSource",
    sparring_dir: Path,
    repo_root: Path,
    make_adapters: AdapterFactory,
    *,
    expected_branch: str,
    evidence: str | None = None,
    allow_push_candidate: str | None = None,
    allow_push_for_run: bool = False,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    stop_after_stage: str | None = None,
    report: Reporter = lambda message: None,
) -> PlanRunResult:
    """Continue a recorded plan run at its current stage.

    ``plan`` is the same source (or Markdown plan path) the run was started
    with; resuming with a different *kind* of source is refused.

    ``make_adapters`` is the same per-stage :data:`AdapterFactory` as for
    :func:`start_plan`.

    ``evidence`` (a human's answer, manual-check result, external-condition
    result or scope approval -- one path for all of them) is appended to the
    current stage's ``notes.md`` under ``## Human evidence``, which is the
    one canonical place it lives: the sparring prompt reads that section
    live (see :func:`~agent_sparring.sparring_prompt.build_sparring_prompt`)
    and the stage prompt embeds it too, so no caller has to mirror it
    anywhere to be seen.

    The *same* stage then resumes with its recorded sessions -- and, when
    that stage has actually been implemented, it resumes at the **sparrer**,
    not the stage agent. Evidence answers a review gate; the candidate did
    not change, and an implementation turn asked to deliver a message would
    move the very commit the reviewer is ruling on. If the sparrer says
    SEND_BACK there is real work and the ordinary loop takes over from the
    stage agent. A stage with no implementation session yet has no candidate
    to spar, so it starts normally.

    ``allow_push_candidate`` is a person allowing this run to push *exactly*
    that commit, and it is refused unless the run is in fact stopped waiting
    for permission to push exactly that commit (see
    :mod:`agent_sparring.push_gate`). That refusal is the point: a stale
    surface offering the permission it was rendered from cannot authorize a
    candidate the run has since replaced. ``allow_push_for_run`` records the
    wider permission -- the verified candidates this run produces from now on
    -- and the two combine, so a person can allow the candidate in front of
    them and stop being asked in the same action. Either one is recorded in
    the run's own state before anything runs, so it survives a reload and a
    later resume; neither is ever inferred from evidence text.

    A current stage that is already ACCEPTED (by hand, after an ESCALATE
    sparred elsewhere, or by a run that stopped between accept and advance)
    is advanced past without running anything.

    ``stop_after_stage`` bounds this call to the named stage: once that
    stage is accepted and the position has advanced, the run pauses instead
    of entering the next one, and nothing is run, created or briefed for it.
    That is what lets a person take one stage at a time through a managed
    run -- finish and accept this stage, look at what was accepted, and
    decide about the next one separately -- without giving up the managed
    run's own position, digest and acceptance handling. The plan is left
    exactly as an ordinary pause leaves it, so the next ``resume-plan``
    continues normally. A stage id that is not in the plan is refused.

    Refuses (:class:`PlanError`) if no run is recorded, the run is complete,
    ``expected_branch`` differs from the recorded one, the source kind
    differs from the recorded one, or the plan's executable content no
    longer matches the digest recorded at start.
    """

    try:
        source = _coerce_source(plan, repo_root)
    except (PlanError, ManifestError) as exc:
        raise _unreadable_resume_input(plan, repo_root, sparring_dir, exc) from exc
    label = source.label
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
    if state.source != source.kind:
        raise PlanError(
            f"plan run for {label} was started from a {state.source} plan input, not a "
            f"{source.kind} one; refusing to resume the same run from a different kind of "
            "input, which would describe different execution content"
        )
    stages = _verify_source_unchanged(source, state)
    if not 0 <= state.current_stage_index < len(stages):
        raise PlanError(
            f"recorded stage index {state.current_stage_index} is out of range for "
            f"{len(stages)} stage(s)"
        )
    _require_state_ignored(repo_root, state_path)
    _grant_push_authorization(
        state,
        state_path,
        Path(repo_root),
        allow_push_candidate=allow_push_candidate,
        allow_push_for_run=allow_push_for_run,
        report=report,
    )

    sparrer_first = False
    if evidence is not None and evidence.strip():
        current = stages[state.current_stage_index]
        stage = _ensure_stage(sparring_dir, current)
        record_human_evidence(stage, evidence)
        report(f"recorded human evidence in {stage.directory / 'notes.md'}")
        # Observational only: that evidence was recorded, never what it says.
        _plan_emitter(stage).emit("plan.evidence_recorded")
        try:
            current_state = stage.read_state()
        except StageError as exc:
            raise PlanError(f"cannot read the state of {current.stage_id!r}: {exc}") from exc
        if current.review_only:
            # A review-only stage has no implementation session to gate on
            # and no implementation turn to skip: its whole lifecycle is the
            # reviewer, so evidence always goes straight to it. What this
            # flag still does there is keep the recorded NEEDS_YOU from
            # being adopted as a pause that nothing answers -- the human
            # just answered it.
            sparrer_first = current_state.status is not StageStatus.ACCEPTED
            if sparrer_first:
                report(
                    f"stage {current.stage_id}: this is a review-only stage; the same "
                    "independent reviewer judges this evidence against the unchanged "
                    "candidate set"
                )
        else:
            sparrer_first = (
                current_state.status is not StageStatus.ACCEPTED
                and current_state.implementation_session_id is not None
            )
            if sparrer_first:
                report(
                    f"stage {current.stage_id}: resuming the sparrer against the unchanged "
                    "candidate with this evidence; the stage agent is not started"
                )

    return _drive(
        source,
        stages,
        state,
        state_path,
        sparring_dir,
        repo_root,
        make_adapters,
        max_send_back_cycles=max_send_back_cycles,
        self_check=self_check,
        report=report,
        sparrer_first=sparrer_first,
        stop_after_stage=stop_after_stage,
    )


def _grant_push_authorization(
    state: PlanRunState,
    state_path: Path,
    repo_root: Path,
    *,
    allow_push_candidate: str | None,
    allow_push_for_run: bool,
    report: Reporter,
) -> None:
    """Record what a person just allowed this run to push, or refuse.

    A one-candidate permission is checked against the run's own recorded
    reason for being stopped, and against nothing else. Three refusals, all
    before anything runs:

    - the run is not waiting for push permission at all (so there is no
      candidate this could be about);
    - it is waiting for a *different* commit -- the surface the person acted
      on was showing a candidate this run has replaced;
    - the sha is not a full commit id, so what was authorized is ambiguous.

    A run-scoped permission needs no pending request: it is a person saying
    "don't ask me again for this run", which is a decision about the run
    rather than about one commit. Both are written to the run state
    immediately, so the permission is durable even if this call then fails
    for an unrelated reason.
    """

    if not allow_push_candidate and not allow_push_for_run:
        return

    awaiting = state.awaiting
    if allow_push_candidate:
        wanted = allow_push_candidate.strip().lower()
        if awaiting is None:
            raise PlanError(
                f"plan run for {state.plan} is not waiting for permission to push a "
                "candidate, so there is nothing to authorize for one"
            )
        if awaiting.candidate_sha.lower() != wanted:
            raise PlanError(
                f"plan run for {state.plan} is waiting for permission to push "
                f"{awaiting.candidate_sha} of stage {awaiting.stage_id!r}, not "
                f"{allow_push_candidate}; refusing to authorize a commit this run is not "
                "waiting on"
            )

    granted: PushAuthorization
    try:
        if not allow_push_for_run and awaiting is not None:
            granted = authorization_for_candidate(
                repo_root,
                stage_id=awaiting.stage_id,
                candidate_sha=awaiting.candidate_sha,
                branch=state.expected_branch,
            )
        else:
            granted = authorization_for_run(repo_root, branch=state.expected_branch)
    except PushError as exc:
        raise PlanError(str(exc)) from exc

    state.push_authorization = granted
    state.save(state_path)
    report(f"push authorization recorded: {granted.describe}")


def _ensure_candidate_pushed(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    repo_root: Path,
    stage: Stage,
    *,
    report: Reporter,
) -> PushRequired | None:
    """Put the verified candidate where the acceptance gate requires it, or
    say that a person has to allow that first.

    Returns the typed request when the run must stop for permission -- the
    run is already paused and the request recorded by the time it returns.
    Returns ``None`` when nothing was needed or the push succeeded and
    reachability was re-proven; a failure of either raises
    :class:`PlanRunError` with the run paused and the stage unaccepted.
    """

    try:
        outcome = ensure_candidate_pushed(
            repo_root,
            stage_id=stage.stage_id,
            expected_branch=state.expected_branch,
            authorization=state.push_authorization,
        )
    except PushError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="push failed",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r}: the reviewer said "
                f"READY but the candidate could not be put on its intended remote branch "
                f"({exc}); nothing was accepted"
            ),
        ) from exc

    if outcome.required is not None:
        state.awaiting = outcome.required
        _pause(state, state_path)
        activity.emit("plan.paused", summary="push authorization required")
        report(
            f"stage {stage.stage_id}: {outcome.required.describe}. The plan is paused until "
            "you allow that push; nothing was pushed and nothing was accepted."
        )
        return outcome.required

    state.awaiting = None
    if outcome.pushed:
        activity.emit("plan.candidate.pushed", sha=outcome.target.candidate_sha)
        report(
            f"stage {stage.stage_id}: pushed the verified candidate "
            f"{outcome.target.candidate_sha} to {outcome.target.remote}/"
            f"{outcome.target.remote_branch} as authorized ({outcome.detail})"
        )
    return None


def _resuming_authorized_push(
    repo_root: Path, stage: Stage, awaiting: PushRequired | None
) -> PushRequired | None:
    """Is this stage the one the run stopped on for push permission, still at
    the same commit?

    When it is, there is nothing left for either agent to do: the reviewer's
    READY is on disk, the candidate is that exact commit, and the only step
    between it and the acceptance gate is the push the person has now allowed.
    Entering at the stage agent would spend an unbounded implementation turn
    on work that is already verified and would move the very commit that was
    authorized; entering at the sparrer would re-derive a verdict already
    recorded.

    Anything that does not line up exactly -- another stage, a verdict that
    is not READY or cannot be read, a ``HEAD`` that has moved since the run
    stopped -- yields ``None``, and the stage is entered the ordinary way.
    """

    if awaiting is None:
        return None
    if awaiting.stage_id != stage.stage_id:
        return None
    outcome = read_recorded_outcome(stage)
    if outcome is None or outcome.action is not RoutingAction.READY:
        return None
    try:
        head = resolve_commit(repo_root, "HEAD", label="HEAD")
    except GitContextError:
        return None
    return awaiting if head == awaiting.candidate_sha else None


def _unreadable_resume_input(
    plan: "Path | str | PlanSource",
    repo_root: Path,
    sparring_dir: Path,
    exc: Exception,
) -> PlanError:
    """The refusal for a resume whose plan input can no longer be read.

    A resume reads its input *before* it reads the run state, so a plan that
    no longer parses fails here rather than at the digest check -- and a bare
    parser error reads like a malformed document instead of like a run whose
    definition was edited underneath it. Appending an implementation record
    whose own ``## Stage 1`` heading follows the plan's ``Stage 1..N`` is
    exactly that case.

    Said only when a run for that plan is actually recorded, which is what
    makes the claim true: without one there is no run it "started against",
    and the parser's own error is the whole of the finding. The label is
    derived from the path rather than from the file's content, so it survives
    the content being unreadable; a manifest, whose label lives *inside* the
    file, cannot be identified that way and keeps the plain error.
    """

    if not isinstance(plan, (str, Path)):
        return exc if isinstance(exc, PlanError) else PlanError(str(exc))
    label = plan_label(Path(plan), Path(repo_root))
    if not plan_state_path(sparring_dir, label).is_file():
        return exc if isinstance(exc, PlanError) else PlanError(str(exc))
    return PlanError(
        f"{label} can no longer be read as the plan this run started against ({exc}); "
        "a managed run's plan must not be edited while it is running. Restore it as it "
        "was, or deliberately start over (see run-plan's refusal message for what to "
        "remove)."
    )


def record_human_evidence(stage: Stage, evidence: str) -> None:
    """Append ``evidence`` under ``## Human evidence`` in the stage's notes.md
    (creating the heading on first use). Prose only; nothing parses it."""

    stage.append_note(HUMAN_EVIDENCE_HEADING, evidence)


def _ensure_stage(sparring_dir: Path, planned: PlannedStage) -> Stage:
    """Create the planned stage (fresh state.json, so fresh sessions) with
    the plan's brief, or continue the existing one whose brief is exactly
    that. A differing brief is refused: the plan is the source of truth for
    what a planned stage is.

    An ACCEPTED stage is the one exception, and it is returned without the
    brief being compared. Acceptance is terminal: nothing will run for that
    stage, only the recorded candidate is read, and its brief is a record of
    how the work was actually briefed at the time. Demanding that it match
    today's plan text would refuse a completed sequence for a wording change
    that can no longer affect anything."""

    try:
        stage = Stage.resolve(sparring_dir, planned.stage_id)
        if not stage.exists():
            stage.create(brief=planned.brief)
            return stage
        state = stage.read_state()
        if state.status is StageStatus.ACCEPTED:
            return stage
        if stage.read_brief() != planned.brief:
            raise PlanError(
                f"stage {planned.stage_id!r} already exists at {stage.directory} with a "
                "brief.md that differs from this plan's text for that stage; refusing to "
                "run a planned stage against a different brief"
            )
    except StageError as exc:
        raise PlanError(str(exc)) from exc
    return stage


def _declare_repositories(stage: Stage, planned: PlannedStage) -> None:
    """Record this stage's declared sibling repositories in ``state.json``.

    Declaring them here, rather than passing them into the acceptance gate,
    keeps the gate's contract unchanged and makes the standalone
    ``freeze-candidate``/``accept-candidate`` commands behave identically:
    the complete reviewed candidate set is a property of the stage, not of
    who happens to be driving it. Only the declaration is written -- the
    pinned commits are the freeze's business (see
    :func:`agent_sparring.acceptance.freeze_candidate`).
    """

    declared = tuple(planned.repositories)
    if not declared:
        return
    try:
        state = stage.read_state()
    except StageError as exc:
        raise PlanError(str(exc)) from exc
    current = tuple(
        repository.__class__(
            name=repository.name,
            path=repository.path,
            branch=repository.branch,
            candidate_sha=None,
        )
        for repository in state.repositories
    )
    if current == declared:
        return
    # Re-declaring keeps any commit already pinned for a repository the plan
    # still declares identically; a changed declaration replaces it, because
    # a pin from a different declaration is not this stage's candidate set.
    pinned = {
        (repository.name, repository.path, repository.branch): repository.candidate_sha
        for repository in state.repositories
    }
    state.repositories = tuple(
        repository.__class__(
            name=repository.name,
            path=repository.path,
            branch=repository.branch,
            candidate_sha=repository.candidate_sha
            or pinned.get((repository.name, repository.path, repository.branch)),
        )
        for repository in declared
    )
    stage.write_state(state)


def _stop_index(stages: tuple[PlannedStage, ...], stop_after_stage: str | None) -> int | None:
    """Where ``stop_after_stage`` sits in the plan, or ``None`` for no bound.

    An id that is not in this plan is refused rather than ignored: a caller
    that meant to bound a run and mistyped the stage would otherwise get an
    unbounded one, which is the opposite of what it asked for.
    """

    if stop_after_stage is None:
        return None
    wanted = stop_after_stage.strip()
    for index, planned in enumerate(stages):
        if planned.stage_id == wanted:
            return index
    known = ", ".join(planned.stage_id for planned in stages)
    raise PlanError(
        f"stop-after stage {wanted!r} is not a stage of this plan; it has: {known}"
    )


def _awaiting_finalization(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    repo_root: Path,
    stage: Stage,
) -> bool:
    """Is this stage stopped exactly between READY and the commit?

    True when the sparrer's recorded verdict is READY *and* the reviewed
    candidate is still in the working tree rather than in a commit. That is
    a real, reachable resting place: it is where every run stopped before
    the loop's finalization cycle existed (the reviewer said READY, the
    acceptance gate refused a candidate that was never committed, and the
    run stopped with the verified work still uncommitted), and it is where
    any process killed between the READY and the commit stops today.

    Entering such a stage at the stage agent would spend an unbounded
    implementation turn on a tree a human has already verified; entering at
    the sparrer would only re-derive a READY that is already on disk. So the
    run enters at the commit/push cycle instead, and
    :mod:`agent_sparring.finalization` holds that turn to the reviewed
    content exactly as it does when the READY happens inside the loop.

    A verdict that cannot be read yields False -- the same rule the rest of
    this module follows, that a half-read verdict never decides whether an
    agent runs. Candidate content that cannot be read is a broken
    repository, not an ambiguous state, and stops the run.
    """

    outcome = read_recorded_outcome(stage)
    if outcome is None or outcome.action is not RoutingAction.READY:
        return False
    try:
        return pending_finalization(repo_root, stage) is not None
    except FinalizationError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="candidate content unreadable",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r}: its recorded "
                f"verdict is READY, but its candidate content could not be read to tell "
                f"whether the reviewed work is committed: {exc}"
            ),
        ) from exc


def _declare_mode(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    stage: Stage,
    planned: PlannedStage,
) -> None:
    """Record what this stage runs as, before anything about where it is.

    A stage's mode is settled before its status is even consulted, because
    the mode decides *which agent runs* and the status only says how far
    along that has got. A stage that already ran under a different mode than
    the plan declares stops the run here with the supported recovery named
    (see :func:`agent_sparring.review.declare_stage_mode`), rather than
    somewhere further in where an implementation session or a stage agent's
    leftovers would already have been adopted into a review.
    """

    try:
        declare_stage_mode(stage, planned)
    except ReviewError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="stage mode mismatch",
            message=f"plan {state.plan} stopped at stage {stage.stage_id!r}: {exc}",
        ) from exc


def _run_review(
    stages: tuple[PlannedStage, ...],
    state: PlanRunState,
    state_path: Path,
    sparring_dir: Path,
    repo_root: Path,
    make_adapters: AdapterFactory,
    planned: PlannedStage,
    stage: Stage,
    activity: ActivityEmitter,
    *,
    position: str,
    evidence_first: bool,
    report: Reporter,
) -> ReviewResult:
    """Enter a review-only stage and run its one independent-review turn.

    Pins and verifies the accepted candidate set first (see
    :func:`agent_sparring.review.enter_review`), so a reviewer is never
    given control over a repository that is not at the work it is being
    asked about. Every refusal pauses the run and stops; nothing is
    substituted and no stage is advanced.

    The adapter pair is built for this stage exactly as for an
    implementation stage, and the sparring half of it is the independent
    reviewer. The stage half is deliberately not used: a review-only stage
    has no implementation turn, so the one adapter that could write to the
    repository is never handed a prompt.
    """

    preceding = tuple(
        (earlier, Stage.resolve(sparring_dir, earlier.stage_id))
        for earlier in stages[: state.current_stage_index]
    )
    try:
        subject = enter_review(
            stage,
            repo_root,
            expected_branch=state.expected_branch,
            preceding=preceding,
        )
    except (ReviewError, StageError) as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="review subject unusable",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} before any "
                f"provider turn: {exc}"
            ),
        ) from exc

    activity.emit(
        "plan.stage.entered",
        summary=f"{planned.display} ({position}); independent review only",
    )
    report(
        f"stage {position} {stage.stage_id}: independent review only; no implementation "
        f"agent runs. Reviewing {subject.candidate_sha} on {subject.branch}, the "
        f"candidate accepted by {subject.accepted[-1].display}"
        + (
            f", plus {len(subject.repositories)} sibling candidate(s)"
            if subject.repositories
            else ""
        )
    )

    try:
        _stage_adapter, reviewer_adapter = make_adapters(stage)
    except Exception as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="adapter construction failed",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} before any "
                f"provider turn: could not build the provider adapters: {exc}"
            ),
        ) from exc

    try:
        return run_independent_review(
            stage,
            sparring_dir,
            repo_root,
            reviewer_adapter,
            expected_branch=state.expected_branch,
            subject=subject,
            evidence_first=evidence_first,
        )
    except ReviewError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="independent review failed",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} (not accepted, "
                f"not advanced): {exc}"
            ),
        ) from exc


def _recorded_pause(stage: Stage) -> RecordedOutcome | None:
    """The verdict this stage is already stopped at, when it is one only a
    person can answer (NEEDS_YOU or ESCALATE); otherwise ``None``.

    This is what lets a sequence be adopted mid-pause. A stage that was
    driven by hand up to a NEEDS_YOU holds everything that matters -- the
    candidate, both sessions, and the reviewer's own gate in ``sparring.md``
    -- and the human's next move is unchanged by the run becoming managed.
    Re-entering it would either re-implement or re-review, and both would
    destroy that. A recorded SEND_BACK is the opposite case: there *is*
    implementation work, so the loop takes it. READY is left to the loop too;
    the sparrer confirms it on its own terms before the hard gate runs.

    An unreadable or unrecognised ``sparring.md`` yields ``None``: a pause is
    only preserved on a verdict that could actually be read.
    """

    outcome = read_recorded_outcome(stage)
    return outcome if outcome is not None and outcome.awaits_a_human else None


def _pause(state: PlanRunState, state_path: Path) -> None:
    state.status = PlanRunStatus.PAUSED
    state.save(state_path)


def _plan_emitter(stage: Stage) -> ActivityEmitter:
    """The plan runner's observational emitter into ``stage``'s own
    ``activity.jsonl`` (see :mod:`agent_sparring.activity`). Write-only and
    never raising; every ``plan.*`` line below is a mirror of a decision
    already taken from the run-state file and ``state.json``, never an
    input to one."""

    return stage.activity_log().bind("plan")


def _fail(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    *,
    why: str,
    message: str,
) -> PlanRunError:
    """Pause the run, mirror ``plan.failed`` with a fixed phrase (``why`` is
    orchestration-authored; the exception text, which can carry provider
    output or paths, is never copied into telemetry), and build the
    :class:`PlanRunError` for the caller to raise."""

    _pause(state, state_path)
    activity.emit("plan.failed", summary=why)
    return PlanRunError(message)


def _require_plan_unchanged(
    source: PlanSource,
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    *,
    before: str,
) -> None:
    """Mid-run form of :func:`_verify_source_unchanged`: on changed (or now
    unreadable) execution content, pause and stop instead of doing
    ``before``."""

    try:
        _verify_source_unchanged(source, state)
    except PlanError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="reviewed plan changed",
            message=(
                f"plan {state.plan} stopped before {before} at stage "
                f"{state.current_stage!r}: {exc}"
            ),
        ) from exc


def _drive(
    source: PlanSource,
    stages: tuple[PlannedStage, ...],
    state: PlanRunState,
    state_path: Path,
    sparring_dir: Path,
    repo_root: Path,
    make_adapters: AdapterFactory,
    *,
    max_send_back_cycles: int,
    self_check: bool,
    report: Reporter,
    sparrer_first: bool = False,
    stop_after_stage: str | None = None,
) -> PlanRunResult:
    """Walk the plan's stages from the recorded position until it pauses,
    fails or completes.

    One loop for both plan inputs and for both entry modes: ``sparrer_first``
    only changes where the *first* stage entered starts (see
    :func:`resume_plan`), and is consumed immediately so every later stage
    runs the ordinary implementation-first loop.

    ``stop_after_stage`` bounds how far this call goes (see
    :func:`resume_plan`). It is checked before a stage is entered -- and so
    before :func:`_ensure_stage` would create its directory -- because
    creating a briefed stage is itself a deliberate step, not something a
    run that is stopping should leave behind.
    """

    total = len(stages)
    accepted: list[tuple[str, str]] = []
    stop_index = _stop_index(stages, stop_after_stage)
    # The typed reason this run was stopped, if it was. Consumed by the first
    # stage entered, exactly like ``sparrer_first``: it is a fact about where
    # the run stopped, not about every stage that follows.
    pending_push = state.awaiting
    # Only for the observational plan.paused event: the run state, not this,
    # is the authority for where a stopped run is positioned.
    last_activity: ActivityEmitter | None = None

    while True:
        plan_stage = stages[state.current_stage_index]
        if stop_index is not None and state.current_stage_index > stop_index:
            stopped_after = stages[stop_index].stage_id
            _pause(state, state_path)
            if last_activity is not None:
                last_activity.emit("plan.paused", summary=f"stopping after {stopped_after}")
            report(
                f"plan {state.plan}: stopping after {stopped_after} as asked. The run is "
                f"positioned at {plan_stage.stage_id} ({plan_stage.position}/{total}) and "
                "nothing was run, created or briefed for it."
            )
            return PlanRunResult(
                status=PlanRunStatus.PAUSED,
                plan=state.plan,
                stage_id=plan_stage.stage_id,
                accepted=tuple(accepted),
            )
        try:
            stage = _ensure_stage(sparring_dir, plan_stage)
        except PlanError:
            # The authoritative refusal is unchanged; only mirror it, if the
            # stage directory is even addressable.
            try:
                _plan_emitter(Stage.resolve(sparring_dir, plan_stage.stage_id)).emit(
                    "plan.failed", summary="planned stage unusable"
                )
            except StageError:
                pass
            raise
        activity = _plan_emitter(stage)
        last_activity = activity
        try:
            stage_state = stage.read_state()
        except StageError as exc:
            raise _fail(
                state,
                state_path,
                activity,
                why="stage state unreadable",
                message=f"plan {state.plan} stopped at {stage.stage_id!r}: {exc}",
            ) from exc

        _declare_mode(state, state_path, activity, stage, plan_stage)

        position = f"{plan_stage.position}/{total}"
        if stage_state.status is StageStatus.ACCEPTED:
            # Already through the hard gate (by hand, or by a run that stopped
            # between accept and advance): nothing to run, just move on.
            activity.emit(
                "plan.stage.entered",
                summary=f"{plan_stage.display} ({position}); already accepted, advancing",
            )
            report(f"stage {position} {stage.stage_id}: already ACCEPTED at "
                   f"{stage_state.candidate_sha}; advancing")
            accepted.append((stage.stage_id, str(stage_state.candidate_sha)))
        else:
            waiting = None if sparrer_first else _recorded_pause(stage)
            if waiting is not None:
                # This stage already stopped for a person, and nothing here
                # answers that. Running the stage agent would spend a turn on
                # a question it cannot answer and move the very candidate the
                # reviewer ruled on; running the sparrer again would throw
                # away the recorded verdict and ask the human to produce the
                # gate a second time. So the run adopts the pause exactly as
                # it stands -- same sessions, same candidate, same
                # sparring.md -- and the way out is the way it always was:
                # answer it, with `resume-plan --evidence`.
                _pause(state, state_path)
                activity.emit(
                    "plan.stage.entered",
                    summary=f"{plan_stage.display} ({position}); already awaiting a human",
                )
                activity.emit("plan.paused", summary=f"recorded {waiting.action.value}")
                report(
                    f"stage {position} {stage.stage_id}: already {waiting.action.value} and "
                    "waiting for you; keeping that pause. Nothing was run and the recorded "
                    "review is unchanged. Answer it with resume-plan --evidence."
                )
                return PlanRunResult(
                    status=PlanRunStatus.PAUSED,
                    plan=state.plan,
                    stage_id=stage.stage_id,
                    recorded=waiting,
                    accepted=tuple(accepted),
                )
            state.status = PlanRunStatus.RUNNING
            # A recorded typed stop describes the pause this run is leaving,
            # never the one it might reach next; it is cleared on entry so it
            # can only ever be re-recorded by the step that means it.
            state.awaiting = None
            state.save(state_path)
            # The declared cross-repository candidate set belongs to the
            # stage, so the acceptance gate finds it wherever it is invoked
            # from. Written before any provider turn.
            _declare_repositories(stage, plan_stage)

            if plan_stage.review_only:
                # No implementation turn exists for this stage, so none of
                # the implementation-first machinery below applies to it:
                # one fresh independent reviewer over the candidate set the
                # preceding stages already accepted (see
                # agent_sparring.review), and all four of its routing
                # actions are terminal.
                review = _run_review(
                    stages,
                    state,
                    state_path,
                    sparring_dir,
                    repo_root,
                    make_adapters,
                    plan_stage,
                    stage,
                    activity,
                    position=position,
                    evidence_first=sparrer_first,
                    report=report,
                )
                sparrer_first = False  # only the stage this resume entered
                if review.outcome is not RoutingAction.READY:
                    _pause(state, state_path)
                    activity.emit("plan.paused", action=review.outcome.value)
                    report(
                        f"stage {stage.stage_id}: {review.outcome.value}; plan paused"
                        + (
                            ". The independent reviewer found a defect that needs a code "
                            "change. This stage has no implementation agent behind it, so "
                            "nothing was changed and nothing was accepted: decide where "
                            "the fix belongs before continuing."
                            if review.defect
                            else ""
                        )
                    )
                    return PlanRunResult(
                        status=PlanRunStatus.PAUSED,
                        plan=state.plan,
                        stage_id=stage.stage_id,
                        routing=review.routing,
                        accepted=tuple(accepted),
                    )

                # READY: the same plan-unchanged check the implementation
                # path makes, then this stage's own completion rule -- no
                # freeze, because there is no new commit to freeze, and the
                # identity being completed over was pinned on entry.
                _require_plan_unchanged(
                    source,
                    state,
                    state_path,
                    activity,
                    before="completing the independent review",
                )
                try:
                    result = accept_reviewed_candidate(
                        stage, repo_root, expected_branch=state.expected_branch
                    )
                except AcceptanceError as exc:
                    raise _fail(
                        state,
                        state_path,
                        activity,
                        why="acceptance gate refused",
                        message=(
                            f"plan {state.plan} stopped at stage {stage.stage_id!r}: the "
                            f"independent reviewer said READY but completing the review "
                            f"was refused ({exc}); the plan did not advance"
                        ),
                    ) from exc
                report(
                    f"stage {stage.stage_id}: READY; independent review completed over "
                    f"{result.candidate_sha}, which stays the candidate it reviewed (this "
                    "stage adds no commit and merges nothing)"
                )
                for repository in result.repositories:
                    report(
                        f"stage {stage.stage_id}: reviewed sibling candidate "
                        f"{repository.name} {repository.branch} re-verified at "
                        f"{repository.candidate_sha}"
                    )
                accepted.append((stage.stage_id, result.candidate_sha))
                activity.emit("plan.stage.accepted", sha=result.candidate_sha)

            else:
                # A run stopped for push permission, resumed with it, and
                # still at the exact commit it stopped over, has nothing left
                # for either agent: the READY is recorded, the candidate is
                # that commit, and the only missing step is the push itself.
                authorized_push = _resuming_authorized_push(repo_root, stage, pending_push)
                pending_push = None  # only the stage this resume entered
                if authorized_push is not None:
                    # Both flags belong to the one stage this resume entered;
                    # neither may leak into the next stage, whose
                    # implementation turn must still run.
                    sparrer_first = False
                    activity.emit(
                        "plan.stage.entered",
                        summary=f"{plan_stage.display} ({position}); pushing the "
                        "authorized candidate",
                    )
                    report(
                        f"stage {position} {stage.stage_id}: the reviewer already said READY "
                        f"over {authorized_push.candidate_sha}; pushing exactly that commit "
                        "as authorized and then running the acceptance gate. No agent runs."
                    )
                else:
                    if sparrer_first:
                        start_with = "sparring"
                        sparrer_first = False  # only the stage this resume entered
                    elif _awaiting_finalization(state, state_path, activity, repo_root, stage):
                        # Stopped between the reviewer's READY and the commit that
                        # acceptance can freeze -- where a run that predates the
                        # finalization cycle was left, and where any process killed
                        # between those two points stops.
                        start_with = "finalization"
                    else:
                        start_with = "stage"
                    entering = {
                        "sparring": "; sparring first",
                        "finalization": "; finalizing the reviewed candidate",
                        "stage": "",
                    }[start_with]
                    activity.emit(
                        "plan.stage.entered",
                        summary=f"{plan_stage.display} ({position}){entering}",
                    )
                    report(
                        f"stage {position} {stage.stage_id}: "
                        + {
                            "sparring": "resuming the sparrer against the unchanged candidate",
                            "finalization": (
                                "the sparrer already said READY and the reviewed candidate is "
                                "not committed; committing and pushing exactly that work, then "
                                "reviewing the commit"
                            ),
                            "stage": "running the implementation <-> sparring loop",
                        }[start_with]
                    )
                    # Adapters are built for THIS stage, so a caller can bind their
                    # provider telemetry to this stage's activity.jsonl. Session
                    # continuity comes from state.json's recorded ids, which
                    # run_stage_agent/run_sparring_agent pass to resume(); it does
                    # not depend on the adapter objects from an earlier stage or an
                    # earlier process.
                    try:
                        stage_adapter, sparring_adapter = make_adapters(stage)
                    except Exception as exc:
                        raise _fail(
                            state,
                            state_path,
                            activity,
                            why="adapter construction failed",
                            message=(
                                f"plan {state.plan} stopped at stage {stage.stage_id!r} before "
                                f"any provider turn: could not build the provider adapters: "
                                f"{exc}"
                            ),
                        ) from exc
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
                            start_with=start_with,
                        )
                    except LoopError as exc:
                        raise _fail(
                            state,
                            state_path,
                            activity,
                            why="stage loop failed",
                            message=(
                                f"plan {state.plan} stopped at stage {stage.stage_id!r} (not "
                                f"accepted, not advanced): {exc}"
                            ),
                        ) from exc

                    if loop_result.outcome is not RoutingAction.READY:
                        _pause(state, state_path)
                        activity.emit("plan.paused", action=loop_result.outcome.value)
                        report(
                            f"stage {stage.stage_id}: {loop_result.outcome.value}; plan paused"
                        )
                        return PlanRunResult(
                            status=PlanRunStatus.PAUSED,
                            plan=state.plan,
                            stage_id=stage.stage_id,
                            routing=loop_result.routing,
                            accepted=tuple(accepted),
                        )

                # READY: first make sure the reviewed plan the sparrer judged
                # against is still the plan on disk -- an implementation turn
                # could have edited and committed a stage section -- then put
                # that exact commit where the gate requires it (only ever with
                # a person's permission), and then invoke the existing hard
                # gate. Any refusal stops the plan; nothing else is ever
                # substituted.
                _require_plan_unchanged(
                    source, state, state_path, activity, before="accepting the candidate"
                )
                required = _ensure_candidate_pushed(
                    state, state_path, activity, repo_root, stage, report=report
                )
                if required is not None:
                    return PlanRunResult(
                        status=PlanRunStatus.PAUSED,
                        plan=state.plan,
                        stage_id=stage.stage_id,
                        awaiting=required,
                        accepted=tuple(accepted),
                    )
                try:
                    frozen = freeze_candidate(
                        stage, sparring_dir, repo_root, expected_branch=state.expected_branch
                    )
                    result = accept_candidate(stage, repo_root, expected_branch=state.expected_branch)
                except AcceptanceError as exc:
                    raise _fail(
                        state,
                        state_path,
                        activity,
                        why="acceptance gate refused",
                        message=(
                            f"plan {state.plan} stopped at stage {stage.stage_id!r}: the sparrer "
                            f"said READY but the acceptance gate refused ({exc}); the plan did not "
                            "advance"
                        ),
                    ) from exc
                report(f"stage {stage.stage_id}: READY; frozen and accepted {result.candidate_sha} "
                       f"({frozen.push_detail})")
                for repository in result.repositories:
                    report(
                        f"stage {stage.stage_id}: sibling candidate {repository.name} "
                        f"{repository.branch} pinned and verified at {repository.candidate_sha}"
                    )
                accepted.append((stage.stage_id, result.candidate_sha))
                activity.emit("plan.stage.accepted", sha=result.candidate_sha)

        if state.current_stage_index + 1 >= total:
            state.status = PlanRunStatus.COMPLETE
            state.save(state_path)
            activity.emit("plan.completed", summary=f"{total} stage(s) accepted")
            report(f"plan {state.plan}: complete; {total} stage(s) accepted")
            return PlanRunResult(
                status=PlanRunStatus.COMPLETE,
                plan=state.plan,
                stage_id=stage.stage_id,
                accepted=tuple(accepted),
            )

        _require_plan_unchanged(
            source, state, state_path, activity, before="advancing to the next stage"
        )
        state.current_stage_index += 1
        state.current_stage = stages[state.current_stage_index].stage_id
        state.save(state_path)


__all__ = [
    "AdapterFactory",
    "PLANS_DIRNAME",
    "MarkdownPlanSource",
    "PlanError",
    "PlanRunError",
    "PlanRunResult",
    "PlanRunState",
    "PlanRunStatus",
    "PlanSource",
    "PlanStage",
    "PlannedStage",
    "describe_source",
    "load_markdown_source",
    "load_plan_source",
    "parse_plan",
    "plan_digest",
    "plan_key",
    "plan_label",
    "plan_state_not_ignored_message",
    "plan_state_path",
    "record_human_evidence",
    "render_brief",
    "resume_plan",
    "start_plan",
]
