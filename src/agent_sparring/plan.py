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

An answer of ``Blocked`` -- the person reporting they could not perform the
check -- resolves nothing, and a reviewer with no implementation defect to
send back cannot make such a stage READY under its own rules. Left alone
that is a treadmill: the same gate, re-issued every turn, each round costing
a person real time. Two things break it. The reviewer's next prompt carries
the engine's tally of what has already been answered and the routes out of a
Blocked check (see
:func:`~agent_sparring.sparring_prompt.gate_answer_tally_section`), the
usual one being READY with the check moved to ``deferred_human_gate``, where
the run's ledger still refuses to let the plan complete until it is
answered. And :func:`_note_repeat_asking` stops the run rather than pause it
again when a gate is wholly checks a person has already declined
:data:`_MAX_ANSWERED_ASKINGS` times.

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
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent_sparring.acceptance import (
    AcceptanceError,
    accept_candidate,
    accept_reviewed_candidate,
    freeze_candidate,
)
from agent_sparring.activity import ActivityEmitter
from agent_sparring.config import VisualReviewConfig
from agent_sparring.plan_obligations import (
    PlanObligationError,
    carry as carry_plan_obligations,
    incomplete_slices,
    origin_of,
    owed as owed_plan_obligations,
    slice_context,
    sync as sync_plan_obligations,
)
from agent_sparring.deferred_gate import (
    CHECKPOINT_PLAN_COMPLETION,
    DEFERRED_VERIFICATION_REQUIRED,
    CheckResult,
    DeferredAnswer,
    DeferredGateError,
    DeferredHumanGate,
    DeferredObligation,
    DeferredVerificationRequired,
    before_stage_checkpoint,
)
from agent_sparring.finalization import FinalizationError, pending_finalization
from agent_sparring.gate_answers import (
    is_repeat_asking,
    parse_gate_answers,
    repeat_asking_count,
    stalled_checks,
)
from agent_sparring.git_context import GitContextError, is_ignored, resolve_commit
from agent_sparring.loop import (
    DEFAULT_MAX_SEND_BACK_CYCLES,
    CandidateRefused,
    LoopError,
    LoopResult,
    run_unattended_loop,
)
from agent_sparring.next_turn import (
    NO_TURN_OWED,
    RESUME_ACCEPT,
    NextTurnError,
    check_next_turn_choice,
    resolve_finalization,
    resolve_legacy_ready,
    record_next_turn,
    repin_after_authorized_advance,
    resolve_resume_turn,
    verify_candidate,
)
from agent_sparring.manifest import ManifestError, ManifestPlanSource, load_manifest_source
from agent_sparring.intake_approval import (
    SOURCE_KIND as INTAKE_SOURCE_KIND,
    is_envelope,
    load_intake_manifest,
    refuse_intake_identities,
)
from agent_sparring.plan_model import ManifestGate, PlanSource, PlannedStage, digest_planned_stages
from agent_sparring.push_gate import (
    PushAuthorization,
    PushError,
    PushRequired,
    authorization_for_candidate,
    authorization_for_run,
    ensure_candidate_pushed,
)
from agent_sparring.providers import (
    ProviderSessionUnresumable,
    SparringAgentAdapter,
    StageAgentAdapter,
    recoverable_provider_failure,
)
from agent_sparring.sessions import (
    ROLE_SPARRING,
    ROLE_STAGE,
    SessionError,
    current_session_id,
    generations,
    start_fresh_sessions,
)
from agent_sparring.sparring_agent import SparringAgentRunError
from agent_sparring.stage_agent import StageAgentRunError
from agent_sparring.review import (
    ReviewError,
    ReviewResult,
    declare_stage_mode,
    enter_review,
    run_independent_review,
)
from agent_sparring.human_gate import HumanCheck, HumanGate, new_gate_instance_id
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import RecordedOutcome, read_recorded_outcome
from agent_sparring.stage import (
    HUMAN_EVIDENCE_HEADING,
    NEXT_TURN_FINALIZATION,
    NEXT_TURN_SPARRING,
    NEXT_TURN_STAGE,
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


#: :attr:`ProviderPause.kind` values.
PAUSE_SESSION_UNRESUMABLE = "session-unresumable"
PAUSE_PROVIDER_UNAVAILABLE = "provider-unavailable"


class ProviderPause(PlanRunError):
    """The run paused because a provider turn failed in a way a person can
    recover from: the role's recorded conversation can no longer be resumed
    (:class:`~agent_sparring.providers.ProviderSessionUnresumable`), or the
    provider declined the turn for quota / rate-limit reasons
    (:class:`~agent_sparring.providers.ProviderUnavailable`).

    Like every :class:`PlanRunError`, the run is already persisted as paused
    and the stage neither accepted nor advanced. Nothing was retried, no
    session was discarded and the candidate was not touched: choosing a
    fresh session (or waiting) is the person's decision, never the engine's.
    """

    def __init__(
        self,
        message: str,
        *,
        stage_id: str,
        role: str,
        kind: str,
        has_session: bool,
        run: str = "",
    ) -> None:
        super().__init__(message)
        #: The run instance key, for the retry command.
        self.run = run
        self.stage_id = stage_id
        #: ``stage`` or ``sparring``: whose turn failed.
        self.role = role
        #: :data:`PAUSE_SESSION_UNRESUMABLE` or :data:`PAUSE_PROVIDER_UNAVAILABLE`.
        self.kind = kind
        #: Whether the role has a conversation a fresh session could
        #: replace; a role whose first session never started has none.
        self.has_session = has_session


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
    #: This run instance's own key (see :func:`new_run_key`): which execution
    #: of :attr:`plan` this is, and the namespace its stage instances are
    #: owned by. Recorded here as well as in the file name so a resume knows
    #: its identity from its content -- a run that had to learn its own key
    #: from where it happened to be filed could be given another run's
    #: stages by a rename.
    #:
    #: ``None`` for every run recorded before run instances existed, and
    #: read as :func:`legacy_run_key` of its plan: at the time, a plan
    #: document had exactly one execution and its key named it, so that is
    #: not a guess about the file, it is what the file means.
    run: str | None = None
    #: What a person has allowed this run to push, and for exactly what (see
    #: :mod:`agent_sparring.push_gate`). ``None`` -- including for every run
    #: recorded before push authorization existed -- means no authorization:
    #: a verified candidate that is not on its intended remote branch stops
    #: the run and asks.
    push_authorization: PushAuthorization | None = None
    #: Why this run is stopped, when the reason is a typed one the runner
    #: itself recorded rather than a reviewer's verdict:
    #: :class:`~agent_sparring.push_gate.PushRequired`, or
    #: :class:`~agent_sparring.deferred_gate.DeferredVerificationRequired`.
    #: Cleared whenever the run enters a stage for execution, so it can never
    #: describe an older pause than the one the run is actually in.
    awaiting: PushRequired | DeferredVerificationRequired | None = None
    #: The obligation ledger: human verification a reviewer deferred rather
    #: than stopped for (see :mod:`agent_sparring.deferred_gate`).
    #:
    #: It lives here, and not in a stage's ``state.json`` or in
    #: ``sparring.md``, because of what has to be true of it. It must outlive
    #: the stage that raised it, since the whole point is that later stages
    #: run first; it must survive SEND_BACK cycles, which rewrite
    #: ``sparring.md`` wholesale; and the thing that must refuse to finish
    #: while an entry is unresolved is the plan run, which is exactly what
    #: this file is the state of. Empty for every run recorded before
    #: deferral existed, which is also the correct reading of "this run owes
    #: nothing".
    deferred_human_checks: tuple[DeferredObligation, ...] = field(default_factory=tuple)
    #: Gate instances this run handed on to its plan's ledger when its slice
    #: of an intake ended before the plan did (see
    #: :mod:`agent_sparring.plan_obligations`): owed by the plan, answered at
    #: the plan's completion, no longer this run's. Provenance only; omitted
    #: while empty so every other run's file is unchanged.
    carried_deferred: tuple[str, ...] = field(default_factory=tuple)
    #: Human evidence recorded by ``resume-plan --evidence`` whose reviewer
    #: turn has not produced a verdict yet: ``{"stage": <stage id>,
    #: "sparring_digest": <sha256 of the sparring.md it answers>}``. A resume
    #: re-enters that evidence turn while the stage's sparring.md is still
    #: exactly the gate it answered, so a provider failure during the
    #: evidence review neither loses the answer nor falls back to the old
    #: pause. Any new verdict rewrites sparring.md and so retires it.
    #: Omitted while ``None``.
    evidence_pending: dict[str, str] | None = None
    #: Why the last provider failure paused this run, for clients to show:
    #: ``{"kind", "role", "stage_id", "has_session", "recorded_at"}``,
    #: written only by :func:`_fail_or_pause_for_provider` in the save that
    #: pauses. Descriptive only -- nothing reads it to route a resume;
    #: ``next_turn``, gates and candidate checks remain the authority.
    #: Replaced by any other pause or failure, cleared when the run next
    #: starts running. Omitted while ``None``.
    provider_pause: dict[str, Any] | None = None
    #: In memory only: the :attr:`provider_pause` this process cleared on
    #: leaving the pause, restored if the resume is then refused before any
    #: provider turn -- a refused resume leaves the recorded reason as it was.
    left_provider_pause: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    #: Whether this run executes in an engine-managed worktree (see
    #: :mod:`agent_sparring.managed_run`). Omitted while ``False``, and
    #: absent from older states, which read as unmanaged.
    managed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["push_authorization"] = (
            self.push_authorization.to_dict() if self.push_authorization is not None else None
        )
        payload["awaiting"] = self.awaiting.to_dict() if self.awaiting is not None else None
        payload["deferred_human_checks"] = [
            obligation.to_dict() for obligation in self.deferred_human_checks
        ]
        if self.carried_deferred:
            payload["carried_deferred"] = list(self.carried_deferred)
        else:
            payload.pop("carried_deferred", None)
        # Omitted rather than written as null, so a run recorded before run
        # instances existed stays exactly the file it was through every
        # resume that rewrites it. Its key is then its file name, which is
        # the plan key it was always filed under.
        if self.run is None:
            payload.pop("run", None)
        if self.evidence_pending is None:
            payload.pop("evidence_pending", None)
        if self.provider_pause is None:
            payload.pop("provider_pause", None)
        payload.pop("left_provider_pause", None)
        if not self.managed:
            payload.pop("managed", None)
        return payload

    # -- the obligation ledger -------------------------------------------

    def obligation(self, instance_id: str) -> DeferredObligation | None:
        for entry in self.deferred_human_checks:
            if entry.instance_id == instance_id:
                return entry
        return None

    @property
    def unresolved_deferred(self) -> tuple[DeferredObligation, ...]:
        """Every obligation a person has not finished answering, in the order
        they were raised. The plan may not complete while this is non-empty,
        and that is the whole of the completion rule."""

        return tuple(entry for entry in self.deferred_human_checks if not entry.resolved)

    @property
    def promoted_deferred(self) -> tuple[DeferredObligation, ...]:
        """Unresolved obligations a later reviewer said can wait no longer."""

        return tuple(entry for entry in self.unresolved_deferred if entry.promoted)

    def record_deferred(self, obligation: DeferredObligation) -> bool:
        """Add ``obligation`` to the ledger unless its asking is already
        there. Returns whether it was added.

        Keyed on the gate instance, which is what makes re-entering a stage
        idempotent: a resumed run that re-reads the same recorded verdict
        must not raise the same obligation twice, and a reviewer that writes
        a genuinely new deferral gets a new instance and so a new entry.
        """

        if self.obligation(obligation.instance_id) is not None:
            return False
        self.deferred_human_checks = self.deferred_human_checks + (obligation,)
        return True

    def replace_obligation(self, obligation: DeferredObligation) -> None:
        """Substitute the ledger entry with the same gate instance."""

        self.deferred_human_checks = tuple(
            obligation if entry.instance_id == obligation.instance_id else entry
            for entry in self.deferred_human_checks
        )

    def withdraw_obligation(self, instance_id: str) -> DeferredObligation | None:
        """Drop the ledger entry for ``instance_id``, returning it.

        Used when the stage that raised the obligation is reopened for
        repair: the asking was a question about a candidate that is about to
        be replaced, and a question about a candidate that no longer exists
        can neither be answered truthfully nor left to block completion
        forever. If it still applies to the repaired candidate, the review
        that ends the reopened stage raises it again -- with a new gate
        instance, because it is a new asking about new work.

        Nothing is lost by this. Every answer the obligation ever held was
        written to the raising stage's ``notes.md`` when it was recorded, and
        that file is never rewritten; the ledger holds what is true now, the
        same rule :meth:`DeferredObligation.with_result` follows.
        """

        found = self.obligation(instance_id)
        if found is None:
            return None
        self.deferred_human_checks = tuple(
            entry for entry in self.deferred_human_checks if entry.instance_id != instance_id
        )
        return found

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanRunState":
        try:
            authorization = payload.get("push_authorization")
            awaiting = payload.get("awaiting")
            raw_deferred = payload.get("deferred_human_checks") or []
            if not isinstance(raw_deferred, (list, tuple)):
                raise PlanError("malformed plan-run state: deferred_human_checks must be an array")
            return cls(
                plan=str(payload["plan"]),
                plan_digest=str(payload["plan_digest"]),
                expected_branch=str(payload["expected_branch"]),
                current_stage_index=int(payload["current_stage_index"]),
                current_stage=str(payload["current_stage"]),
                status=PlanRunStatus(str(payload["status"])),
                source=str(payload.get("source", "markdown")),
                run=(str(payload["run"]) if payload.get("run") else None),
                push_authorization=(
                    PushAuthorization.from_dict(authorization) if authorization else None
                ),
                awaiting=_awaiting_from_dict(awaiting),
                deferred_human_checks=tuple(
                    DeferredObligation.from_dict(entry) for entry in raw_deferred
                ),
                carried_deferred=tuple(str(entry) for entry in payload.get("carried_deferred") or ()),
                evidence_pending=(
                    {str(k): str(v) for k, v in payload["evidence_pending"].items()}
                    if isinstance(payload.get("evidence_pending"), dict)
                    else None
                ),
                provider_pause=(
                    dict(payload["provider_pause"])
                    if isinstance(payload.get("provider_pause"), dict)
                    else None
                ),
                managed=_managed_flag(payload.get("managed", False)),
            )
        except (PushError, DeferredGateError) as exc:
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


def _managed_flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise PlanError(f"malformed plan-run state: managed must be true or false, not {value!r}")
    return value


def _awaiting_from_dict(
    payload: Any,
) -> "PushRequired | DeferredVerificationRequired | None":
    """The typed pause a recorded run is stopped on.

    Dispatched on ``kind`` and on nothing else, so adding a reason is adding
    a value rather than changing a format. An unknown kind is a state file
    written by a newer engine; refusing it is right, because continuing
    would mean resuming a run whose reason for being stopped this version
    does not understand.
    """

    if not payload:
        return None
    kind = payload.get("kind") if isinstance(payload, Mapping) else None
    if kind == DEFERRED_VERIFICATION_REQUIRED:
        return DeferredVerificationRequired.from_dict(payload)
    return PushRequired.from_dict(payload)


def plan_label(plan_path: Path, repo_root: Path) -> str:
    """How the plan is named in state and output: repo-relative when it lives
    inside the repository, otherwise its absolute path."""

    resolved = plan_path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def new_run_key(label: str) -> str:
    """Mint an identity for one execution of a plan document.

    The plan document's key, so the run is still recognisable at a glance
    and everything belonging to one plan sorts together, plus eight hex
    digits that are this *execution*: ``follow-up-7c1e42a9-3b4d0f16``.

    Random rather than a counter, for the reason
    :func:`~agent_sparring.human_gate.new_gate_instance_id` gives: the only
    place a counter could be derived from is the very directory a person is
    free to prune, so it would restart -- and a restarted counter is worse
    than no identity, because run B would inherit run A's stage
    directories, which is exactly the accident this identity exists to
    prevent. Nothing orders runs by their key; ``plans/*.json`` records when
    each one started what it started.

    A plan document is the *input* to a run, not the run. Running the same
    document again is ordinary work (a second attempt, a rerun after the
    code moved on), and it gets a run of its own.
    """

    return f"{plan_key(label)}-{uuid.uuid4().hex[:8]}"


def legacy_run_key(label: str) -> str:
    """The run key of a run recorded before run instances existed.

    Such a run's state lives at ``plans/<plan key>.json`` and its stages
    record ``<plan key>`` as their owner, because at the time a plan
    document had exactly one execution and its key named it. Reading that as
    the key of that document's one legacy run instance keeps every existing
    run readable, keeps its stages owned by it, and needs nothing rewritten
    on disk.
    """

    return plan_key(label)


def run_state_path(sparring_dir: Path, run_key: str) -> Path:
    """Where one run instance's state lives.

    Still ``.sparring/plans/``, deliberately: that directory is what every
    project's ``.gitignore`` already names (see
    :func:`plan_state_not_ignored_message`), and a second one would make
    every existing project fail a check it has already satisfied. What
    changed is only the file name -- a run key rather than a plan key -- so
    two runs of one document are two files instead of one.
    """

    return Path(sparring_dir) / PLANS_DIRNAME / f"{run_key}.json"


@dataclass(frozen=True)
class RecordedRun:
    """One run instance found on disk, with the key it is filed under."""

    key: str
    path: Path
    state: PlanRunState

    @property
    def open(self) -> bool:
        return self.state.status is not PlanRunStatus.COMPLETE


def find_runs(sparring_dir: Path, label: str) -> tuple[RecordedRun, ...]:
    """Every recorded run of the plan document ``label``, oldest file first.

    Found by reading the states rather than by computing a file name, which
    is what makes one pass cover both spellings: a run instance's file is
    named by its run key, a run recorded before run instances existed is
    named by its plan key, and both say which plan they execute in their
    own ``plan`` field. Unparseable files are skipped -- this answers "which
    runs of this plan exist", and a file that cannot be read is not an
    answer to it; whoever addresses that run directly still fails on it
    with its own message.
    """

    plans = Path(sparring_dir) / PLANS_DIRNAME
    try:
        entries = sorted(plans.glob("*.json"))
    except OSError:
        return ()
    found: list[RecordedRun] = []
    for path in entries:
        try:
            state = PlanRunState.load(path)
        except PlanError:
            continue
        if state.plan != label:
            continue
        found.append(RecordedRun(key=state.run or path.stem, path=path, state=state))
    return tuple(found)


# -- plan sources ------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownPlanSource:
    """A :class:`~agent_sparring.plan_model.PlanSource` over a reviewed
    Markdown plan: the original ``## Stage <n> — <title>`` convention,
    unchanged.

    ``digest`` is still :func:`plan_digest` over the parsed stage numbers,
    titles and sections, so a run recorded before manifests existed
    re-validates against exactly the same value. Note what that means for
    ``namespace``: the digest is over what the plan *says*, never over the
    ids its stages were given, so re-namespacing a source cannot invalidate
    a recorded run.
    """

    path: Path
    label: str
    parsed: tuple[PlanStage, ...]
    kind: str = "markdown"
    #: What the parsed stage ids are prefixed with -- the owning run's key
    #: for a managed run, and carried as a field so it survives
    #: :meth:`reload`. ``None`` produces the bare ``stage-<n>-<slug>`` ids,
    #: which is what a caller that is not a managed run gets.
    namespace: str | None = None

    def digest(self) -> str:
        return plan_digest(self.parsed)

    def in_namespace(self, namespace: str) -> "MarkdownPlanSource":
        """The same plan, with its stage ids minted under ``namespace``.

        Used by :func:`start_plan` and :func:`resume_plan` once the run's
        own key is known, which is the only moment it can be: a fresh run's
        key does not exist when the CLI reads the document, and a resume's
        belongs to the recorded run rather than to the file.
        """

        if namespace == self.namespace:
            return self
        # Re-derived from the stages already parsed rather than by re-reading
        # the document: the validated content must not be able to change
        # between being checked and being run.
        return replace(
            self,
            parsed=tuple(
                replace(stage, stage_id=_stage_id_for(stage.number, stage.title, namespace))
                for stage in self.parsed
            ),
            namespace=namespace,
        )

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
        return load_markdown_source(self.path, self.label, namespace=self.namespace)

    def describe(self) -> str:
        return f"plan {self.label}"


def load_markdown_source(
    plan_path: Path, label: str, *, namespace: str | None = None
) -> MarkdownPlanSource:
    """Read and validate a reviewed Markdown plan.

    ``namespace`` prefixes the generated stage ids. It defaults to
    :func:`legacy_run_key` -- the plan's own key -- which is exactly what
    every run recorded before run instances existed used, so re-reading such
    a run's plan reproduces its stage ids unchanged. A managed run replaces
    it with its own key (:meth:`MarkdownPlanSource.in_namespace`) as soon as
    that key is known.
    """

    try:
        text = Path(plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read plan {plan_path}: {exc}") from exc
    return markdown_source_from_text(plan_path, label, text, namespace=namespace)


def markdown_source_from_text(
    plan_path: Path, label: str, text: str, *, namespace: str | None = None
) -> MarkdownPlanSource:
    """:func:`load_markdown_source` over ``text`` already read from
    ``plan_path``, so a caller that checked that text runs exactly it."""

    prefix = namespace if namespace is not None else legacy_run_key(label)
    return MarkdownPlanSource(
        path=Path(plan_path),
        label=label,
        parsed=parse_plan(text, plan_key=prefix),
        namespace=prefix,
    )


def load_plan_source(path: Path, repo_root: Path, *, manifest: bool = False) -> PlanSource:
    """Read ``path`` as the plan input the runner will execute.

    ``manifest`` picks the execution-manifest reader; otherwise the file is
    read as a reviewed Markdown plan. Both are validated in full here,
    before any run state is written or any provider is invoked.
    """

    if manifest:
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise PlanError(f"cannot read manifest {path}: {exc}") from exc
        try:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if is_envelope(payload):
                # Verified and parsed from these same bytes: what was
                # checked is what runs.
                return load_intake_manifest(Path(path), raw)
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
    #: Which run instance of ``plan`` this result is about (see
    #: :func:`new_run_key`). Empty only for a result built by a caller that
    #: predates run instances; every result the runner produces carries it,
    #: because a resume hint that named the document but not the run would
    #: be ambiguous the moment a document has been executed twice.
    run: str = ""
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
    #:
    #: The same field also carries
    #: :class:`~agent_sparring.deferred_gate.DeferredVerificationRequired`,
    #: for the run that has nothing left to implement but still owes a
    #: person the verification an earlier reviewer deferred. It is the same
    #: kind of fact -- the engine stopped on a typed reason of its own, not
    #: on a verdict -- so a consumer switches on ``kind`` rather than on
    #: which field is populated.
    awaiting: PushRequired | DeferredVerificationRequired | None = None
    #: The obligations a :class:`DeferredVerificationRequired` pause is
    #: about, in full, so a caller can render them without re-reading the
    #: run state. Empty otherwise.
    deferred: tuple[DeferredObligation, ...] = field(default_factory=tuple)


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
    run_key: str | None = None,
    adopt: bool = False,
    allow_push_for_run: bool = False,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    visual_review: VisualReviewConfig | None = None,
    stop_after_stage: str | None = None,
    report: Reporter = lambda message: None,
    managed: bool = False,
) -> PlanRunResult:
    """Validate the whole plan, record position at stage 1, and run.

    ``plan`` is either a :class:`~agent_sparring.plan_model.PlanSource` or a
    path to a reviewed Markdown plan (the original form).

    ``make_adapters`` (an :data:`AdapterFactory`) is called once per stage
    entered for execution, with that stage, and returns the stage and
    sparring adapters to drive it with.

    ``run_key`` is this execution's identity (see :func:`new_run_key`), and
    one is minted when none is given. A caller supplies it when it needs to
    know the run's identity before the run exists -- the VS Code extension
    does, because it names the stage ids in the manifest it hands over and
    tracks the terminal it launched. Every call starts a *new* run: running
    the same plan document again is ordinary work and gets a run of its own,
    with stage instances of its own, while the earlier run stays on disk
    exactly as it is.

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

    What ``adopt`` never does is take over a stage some *other* managed run
    owns. Stage instances are identified by ``(run instance, stage)``,
    recorded as :attr:`~agent_sparring.stage.StageState.run`, so a follow-up
    plan started on the same branch -- even one whose sections are numbered
    Stage 1..3 again -- and a second run of the very same document both get
    stage instances of their own, while the earlier run's accepted history
    stays the earlier run's. See :func:`_refuse_foreign_stages`.

    Refuses (:class:`PlanError`, before any provider is invoked) if the plan
    is malformed, if a run of this plan is already *open* -- running or
    paused -- because two live managed runs of one document in one worktree
    would compete for the same candidate (finish or resume that one first),
    if one of its stages belongs to another run, if a stage directory exists
    that adoption does not allow, or if the run-state location is not
    git-ignored. A *complete* run never refuses a fresh one.
    """

    if not expected_branch or not expected_branch.strip():
        raise PlanError("expected_branch is required for a plan run")
    from agent_sparring.managed_run import ManagedRunError, refuse_unmanaged_here

    if not managed:
        # One managed worktree belongs to one managed run.
        try:
            refuse_unmanaged_here(Path(repo_root))
        except ManagedRunError as exc:
            raise PlanError(str(exc)) from exc

    source = _coerce_source(plan, repo_root)
    bind_fresh_run = getattr(source, "bind_fresh_run", None)
    if callable(bind_fresh_run):
        # An approved intake slice runs as the run it was approved as, from
        # the repository state it was approved against -- checked here,
        # before anything is recorded or run.
        try:
            run_key = bind_fresh_run(Path(repo_root), run_key=run_key, expected_branch=expected_branch)
        except ManifestError as exc:
            raise PlanError(str(exc)) from exc
    label = source.label
    # Open runs are looked for before the run's own identity is minted, so a
    # refusal costs nothing and leaves nothing behind.
    live = [run for run in find_runs(sparring_dir, label) if run.open]
    if live:
        listed = "\n  - ".join(
            f"{run.key} (status {run.state.status.value}, current stage "
            f"{run.state.current_stage!r}) at {run.path}"
            for run in live
        )
        raise PlanError(
            f"refusing a fresh run of {label}: {len(live)} run(s) of it are still open:\n"
            f"  - {listed}\n"
            "Two live managed runs of one plan in one worktree would compete for the same "
            "candidate, so finish or resume that one first (resume-plan --run-key <key>). "
            "A *complete* run never blocks a fresh one; this is only about runs still in "
            "flight."
        )

    key = run_key or new_run_key(label)
    if isinstance(source, MarkdownPlanSource):
        # Stage ids are namespaced by the run that owns them, which is why
        # this cannot happen when the document is read: the key is this
        # run's, not the document's.
        source = source.in_namespace(key)
    stages = source.stages()
    if source.kind != INTAKE_SOURCE_KIND:
        try:
            refuse_intake_identities(
                sparring_dir, run_key=key, stage_ids=[stage.stage_id for stage in stages]
            )
        except ManifestError as exc:
            raise PlanError(f"refusing a fresh run of {label}: {exc}") from exc
    state_path = run_state_path(sparring_dir, key)
    if state_path.is_file():
        raise PlanError(
            f"refusing a fresh run of {label}: run key {key} is already recorded at "
            f"{state_path}. Run keys are minted per execution, so this means one was "
            "supplied that has been used before; leave it out and a fresh one is minted."
        )

    owner = _RunOwner(key=key, label=label)
    leftovers = [
        stage for stage in stages if Stage.resolve(sparring_dir, stage.stage_id).directory.exists()
    ]
    # Ownership first, and it is not a thing --adopt can answer: a stage
    # another managed run owns is that run's stage instance, whatever its
    # directory happens to be called.
    _refuse_foreign_stages(sparring_dir, leftovers, owner=owner)
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
    adopted: tuple[str, ...] = ()
    if leftovers:
        adopted = _check_adoption(sparring_dir, stages, leftovers, report=report)
    _require_state_ignored(repo_root, state_path)
    if managed:
        _require_managed_record(repo_root, key, expected_branch)

    state = PlanRunState(
        plan=label,
        plan_digest=source.digest(),
        expected_branch=expected_branch,
        current_stage_index=0,
        current_stage=stages[0].stage_id,
        status=PlanRunStatus.RUNNING,
        source=source.kind,
        run=key,
        managed=managed,
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
    # Claimed only now that the run exists: a refusal above must not leave a
    # stage marked as owned by a run that was never recorded.
    for stage_id in adopted:
        _claim_stage(sparring_dir, stage_id, owner=owner, report=report)
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
        visual_review=visual_review,
        report=report,
        stop_after_stage=stop_after_stage,
    )


def _require_managed_record(repo_root: Path, run_key: str, expected_branch: str) -> None:
    from agent_sparring.managed_run import ManagedRunError, require_state_matches

    try:
        require_state_matches(Path(repo_root), run_key, expected_branch=expected_branch)
    except ManagedRunError as exc:
        raise PlanError(str(exc)) from exc


@dataclass(frozen=True)
class _RunOwner:
    """The managed run instance that owns the stage instances it drives.

    ``key`` is this run's key, and it is what is recorded in each stage's
    ``state.json`` (see :attr:`~agent_sparring.stage.StageState.run`);
    ``label`` is only ever used to say which plan document a refusal is
    about, and is never the identity -- that was the whole defect: one
    document can be executed more than once.
    """

    key: str
    label: str

    @classmethod
    def of(cls, state: PlanRunState) -> "_RunOwner":
        """The owner a recorded run is. A run with no key of its own is one
        recorded before run instances existed, and its key is its plan's
        (see :func:`legacy_run_key`) -- which is what its stages already
        record as their owner, so nothing has to be rewritten."""

        return cls(key=state.run or legacy_run_key(state.plan), label=state.plan)


def _recorded_owner(sparring_dir: Path, stage_id: str) -> str | None:
    """The plan key recorded as owning ``stage_id``, or None.

    None both for a stage that records no owner and for one whose state
    cannot be read: this answers "is it *provably* another run's stage", and
    an unreadable state proves nothing. The ordinary adoption and brief
    checks still see that stage and refuse it on their own terms.
    """

    try:
        return Stage.resolve(sparring_dir, stage_id).read_state().run
    except StageError:
        return None


def _refuse_foreign_stages(
    sparring_dir: Path,
    planned: Sequence[PlannedStage],
    *,
    owner: _RunOwner,
) -> None:
    """Refuse if any of ``planned`` is a stage another run instance owns.

    This is the invariant that makes execution-stage identity
    ``(run instance, stage)``: an accepted stage of run A is never the stage
    instance of run B, however alike their generated stage ids are. Two
    ordinary workflows depend on it. A follow-up plan in the same worktree
    numbered Stage 1..3 again gets its own stage instances rather than the
    previous plan's; and so does a second run of the *same* document, which
    is why the owner recorded is a run key and not a plan key.

    Deliberately unconditional. ``--adopt`` says "these stages were executed
    independently and I want this run to adopt them", which is true of
    hand-driven stages that no run owns; it does not say "take another
    run's accepted work as my own", and no flag here does. The way out is
    for this run to use stage ids of its own (the run-key-prefixed ones
    every managed run now generates), which needs nothing removed from
    disk.
    """

    conflicts = [
        (stage.stage_id, recorded)
        for stage in planned
        for recorded in (_recorded_owner(sparring_dir, stage.stage_id),)
        if recorded is not None and recorded != owner.key
    ]
    if not conflicts:
        return
    listed = "\n  - ".join(
        f"{stage_id} is owned by managed run {recorded}" for stage_id, recorded in conflicts
    )
    raise PlanError(
        f"refusing to run {owner.label} over {len(conflicts)} stage(s) that belong to another "
        f"managed plan run:\n  - {listed}\n"
        "Those are that run's stage instances, not this run's, and an accepted stage of "
        "one run never answers for another run's stage of the same name. This run needs "
        "stage ids of its own; nothing has to be deleted, and that run's history stays "
        "exactly as it is."
    )


def _claim_stage(
    sparring_dir: Path, stage_id: str, *, owner: _RunOwner, report: Reporter
) -> None:
    """Record ``owner`` as this stage's owning run, if nothing else has.

    Monotone: an already-recorded owner is never rewritten (a foreign one
    has already been refused by :func:`_refuse_foreign_stages` before
    anything gets here), so ownership cannot drift and an accepted stage
    cannot be re-pointed at a different run. A state that cannot be read is
    left alone; whoever needs to read it will fail with its own message
    rather than this one.
    """

    stage = Stage.resolve(sparring_dir, stage_id)
    try:
        state = stage.read_state()
    except StageError:
        return
    if state.run is not None:
        return
    state.run = owner.key
    stage.write_state(state)
    report(f"stage {stage_id} is now recorded as owned by managed run {owner.key} of {owner.label}")


def _check_adoption(
    sparring_dir: Path,
    stages: tuple[PlannedStage, ...],
    existing: list[PlannedStage],
    *,
    report: Reporter,
) -> tuple[str, ...]:
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

    A stage owned by *another* managed run never reaches here: it is refused
    before this by :func:`_refuse_foreign_stages`, whatever its status. So
    what this decides about is only ever an unowned stage (the hand-driven
    sequence adoption exists for) or one this same plan already owns.

    Reports every adoption, including what is being inherited, so nothing is
    taken over quietly. Returns the ids actually adopted, for the caller to
    claim once the run is recorded.
    """

    known = {stage.stage_id for stage in existing}
    problems: list[str] = []
    adopted: list[str] = []
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
            adopted.append(planned.stage_id)
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
        adopted.append(planned.stage_id)

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
    return tuple(adopted)


def _run_to_resume(sparring_dir: Path, label: str, run_key: str | None) -> RecordedRun:
    """Which recorded run of ``label`` a resume means, or refuse.

    Named explicitly, it must be that run, and a complete one is refused by
    name -- "resume this" answered by resuming a different run would be the
    same silent substitution this whole identity model exists to stop.
    Unnamed, the plan's single open run is it; several open runs are an
    ambiguity a person resolves, and it is spelled out with the keys to
    resolve it with.
    """

    runs = find_runs(sparring_dir, label)
    if run_key is not None:
        for run in runs:
            if run.key == run_key:
                if not run.open:
                    raise PlanError(
                        f"plan run {run_key} of {label} is already complete; nothing to "
                        "resume. Run Plan starts a new run of the same document."
                    )
                return run
        raise PlanError(
            f"no run {run_key} of {label} is recorded in {Path(sparring_dir) / PLANS_DIRNAME}"
            + (f"; recorded runs are {', '.join(run.key for run in runs)}" if runs else "")
        )
    open_runs = [run for run in runs if run.open]
    if len(open_runs) == 1:
        return open_runs[0]
    if not open_runs:
        if not runs:
            raise PlanError(f"no plan run is recorded for {label}; start one with run-plan")
        keys = ", ".join(run.key for run in runs)
        which = f"the recorded run {keys} of {label} is" if len(runs) == 1 else f"every recorded run of {label} ({keys}) is"
        raise PlanError(
            f"{which} already complete; nothing to resume. Run Plan starts a new run of "
            "the same document, which does not disturb it."
        )
    listed = ", ".join(f"{run.key} ({run.state.status.value})" for run in open_runs)
    raise PlanError(
        f"{len(open_runs)} runs of {label} are open ({listed}); say which one with "
        "--run-key <key>"
    )


def resume_plan(
    plan: "Path | str | PlanSource",
    sparring_dir: Path,
    repo_root: Path,
    make_adapters: AdapterFactory,
    *,
    expected_branch: str,
    run_key: str | None = None,
    evidence: str | None = None,
    deferred_results: tuple[DeferredAnswer, ...] = (),
    allow_push_candidate: str | None = None,
    allow_push_for_run: bool = False,
    accept_advanced_head: str | None = None,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    visual_review: VisualReviewConfig | None = None,
    stop_after_stage: str | None = None,
    next_turn: str | None = None,
    fresh_roles: tuple[str, ...] = (),
    fresh_reason: str | None = None,
    report: Reporter = lambda message: None,
) -> PlanRunResult:
    """Continue a recorded plan run at its current stage.

    ``plan`` is the same source (or Markdown plan path) the run was started
    with; resuming with a different *kind* of source is refused.

    ``run_key`` names *which* run of that document to continue, since one
    document may have been executed more than once. Left out, the plan's
    one open run is resumed; if several are open the refusal names them and
    asks, and a document whose only runs are complete has nothing to
    resume. Naming a complete run is refused by name rather than silently
    treated as the open one.

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

    An ordinary resume obeys the current stage's recorded ``next_turn``
    (see :mod:`agent_sparring.next_turn`): ``sparring`` reviews the recorded
    candidate directly, with no implementation turn. State written before
    that marker existed is derived once from engine records; when that is
    ambiguous the resume is refused, and ``next_turn`` (``stage`` |
    ``sparring``) is the caller's explicit choice, recorded as ``manual``.
    That choice is refused, with nothing recorded, whenever a marker is
    already recorded or derivation has an unambiguous answer, and never
    answers a recorded human gate or reopens a recorded READY. It is
    validated before any plan-run or stage state changes, independently of
    ``fresh_roles``.
    Evidence, a recorded human gate and a pending finalization keep their
    existing precedence over the marker. Evidence answering a gate raised
    over a recorded candidate (``next_turn = sparring``) is refused, before
    it is recorded, when the repository no longer holds that candidate.

    ``fresh_roles`` (``stage`` / ``sparring``) discards those roles'
    conversations for the stage this resume enters and continues it in new
    ones (see :func:`agent_sparring.sessions.start_fresh_sessions`), with
    ``fresh_reason`` recorded. Applied only where an agent turn is about to
    run; refused, with nothing changed, where none is owed.

    A provider turn that fails as unresumable or unavailable raises
    :class:`ProviderPause` (a :class:`PlanRunError`) naming the role, so the
    caller can print the exact retry command.

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
    recorded = _run_to_resume(sparring_dir, label, run_key)
    state_path = recorded.path
    state = recorded.state
    owner = _RunOwner.of(state)

    if state.expected_branch != expected_branch:
        raise PlanError(
            f"plan run {owner.key} of {label} was started for branch "
            f"{state.expected_branch!r}, not {expected_branch!r}; refusing to resume on a "
            "different branch"
        )
    if state.managed:
        _require_managed_record(repo_root, owner.key, expected_branch)
    if state.source != source.kind:
        raise PlanError(
            f"plan run {owner.key} of {label} was started from a {state.source} plan input, "
            f"not a {source.kind} one; refusing to resume the same run from a different kind "
            "of input, which would describe different execution content"
        )
    verify_run_context = getattr(source, "verify_run_context", None)
    if callable(verify_run_context):
        # A sealed intake run continues only from the worktree it was
        # approved in, on its branch. Not against the starting commit: the
        # run's own accepted work has moved HEAD since.
        try:
            verify_run_context(Path(repo_root), expected_branch=expected_branch, fresh=False)
        except ManifestError as exc:
            raise PlanError(str(exc)) from exc
    if isinstance(source, MarkdownPlanSource):
        # The stage ids this resume works on are the *recorded run's*, never
        # the ones the document would generate on its own. That is the whole
        # of what makes a rerun of the same document a different run: its
        # stages are namespaced by whichever run owns them.
        source = source.in_namespace(owner.key)
    stages = _verify_source_unchanged(source, state)
    if not 0 <= state.current_stage_index < len(stages):
        raise PlanError(
            f"recorded stage index {state.current_stage_index} is out of range for "
            f"{len(stages)} stage(s)"
        )
    # The same invariant as a fresh run's, checked here too because a resume
    # rebuilds its manifest from a living plan document: a rebuild that
    # drifted onto another run's stage ids must refuse rather than execute
    # over that run's stages.
    _refuse_foreign_stages(sparring_dir, stages, owner=owner)
    _require_state_ignored(repo_root, state_path)
    if next_turn is not None and evidence is not None and evidence.strip():
        if next_turn != NEXT_TURN_SPARRING:
            raise PlanError(
                "evidence is judged by the reviewer, so it can only be combined with "
                f"--next-turn {NEXT_TURN_SPARRING} (for ambiguous legacy state only), not "
                f"--next-turn {next_turn}"
            )
    for role in fresh_roles:
        if role not in (ROLE_STAGE, ROLE_SPARRING):
            raise PlanError(f"unknown fresh-session role {role!r}")
    if (
        evidence is not None
        and evidence.strip()
        and isinstance(state.awaiting, DeferredVerificationRequired)
        and state.awaiting.reason == DeferredVerificationRequired.BEFORE_STAGE
    ):
        # The run is stopped between two stages on a plan-declared gate: no
        # stage review is waiting for evidence, and free-text evidence must
        # never be read as the gate's answer.
        raise PlanError(
            f"plan run {owner.key} is stopped on a plan-declared gate before the next stage; "
            "--evidence answers a stage's review, not a gate. Answer the gate with "
            "--deferred-result <instance>:<gate id>=pass once it is actually satisfied "
            f"(gate instances: {', '.join(state.awaiting.instance_ids)})"
        )

    if next_turn is not None:
        # Validated before any plan-level or stage state changes, so a refused
        # choice leaves both exactly as they were.
        _check_next_turn_choice(
            sparring_dir,
            repo_root,
            stages[state.current_stage_index],
            next_turn,
            evidence=bool(evidence and evidence.strip()),
        )
    _grant_push_authorization(
        state,
        state_path,
        Path(repo_root),
        allow_push_candidate=allow_push_candidate,
        allow_push_for_run=allow_push_for_run,
        report=report,
    )

    # Before anything runs, and before the ordinary evidence path: these
    # answer the run's own checkpoint rather than a stage's review, and a
    # refusal here must stop the resume rather than leave half of it applied.
    _answer_deferred(
        state,
        state_path,
        sparring_dir,
        deferred_results,
        report=report,
        context=slice_context(source) if deferred_results else None,
    )

    if accept_advanced_head is not None and not (evidence and evidence.strip()):
        raise PlanError(
            "--accept-advanced-head is recorded with the human evidence that authorizes "
            "it; pass --evidence as well"
        )

    sparrer_first = False
    if evidence is not None and evidence.strip():
        current = stages[state.current_stage_index]
        stage = _ensure_stage(sparring_dir, current, owner=owner, report=report)
        try:
            current_state = stage.read_state()
        except StageError as exc:
            raise PlanError(f"cannot read the state of {current.stage_id!r}: {exc}") from exc
        if next_turn is not None:
            # Evidence plus an explicit sparring turn, for ambiguous legacy
            # state only: recorded as a manual marker over the current
            # candidate. A recorded marker is never overridden or re-pinned;
            # refused here before the evidence is recorded.
            if current.review_only or current_state.status is StageStatus.ACCEPTED or not (
                generations(current_state, ROLE_STAGE)
            ):
                raise PlanError(
                    f"stage {current.stage_id!r} has no implementation candidate awaiting "
                    "review, so there is no candidate to re-pin with --next-turn sparring"
                )
            try:
                resolve_resume_turn(repo_root, stage, choice=next_turn)
                current_state = stage.read_state()
            except NextTurnError as exc:
                raise PlanError(str(exc)) from exc
            report(
                f"stage {current.stage_id}: pinned the current candidate for review "
                "(next turn = sparring, recorded as manual)"
            )
            next_turn = None
        if (
            not current.review_only
            and current_state.status is not StageStatus.ACCEPTED
            and current_state.next_turn == NEXT_TURN_SPARRING
        ):
            # The gate this evidence answers was raised over the recorded
            # candidate; the reviewer must judge the answer against exactly
            # that content. Checked before the evidence is recorded, so a
            # refusal leaves notes.md as it was and the same command can be
            # repeated once the candidate is restored. (The loop checks
            # again immediately before the reviewer starts.)
            try:
                verify_candidate(repo_root, stage, current_state)
            except NextTurnError as exc:
                if accept_advanced_head is None:
                    raise PlanError(
                        f"refusing to record evidence for stage {current.stage_id!r}: {exc} "
                        "(--next-turn cannot re-pin a recorded candidate.)"
                    ) from exc
                # The person named the exact commit the branch advanced to
                # while the stage waited on them; re-pin only if the attempt
                # is provably unchanged beneath it. Checked before anything
                # is written, so a refusal leaves the stage as it was.
                pinned_head = current_state.next_turn_candidate.head_sha
                try:
                    repinned = repin_after_authorized_advance(
                        repo_root, stage, current_state, accept_advanced_head
                    )
                except NextTurnError as repin_exc:
                    raise PlanError(
                        f"refusing to record evidence for stage {current.stage_id!r}: "
                        f"--accept-advanced-head {accept_advanced_head}: {repin_exc}"
                    ) from repin_exc
                evidence = (
                    f"{evidence.rstrip()}\n\nAccepted branch advance: the pending review "
                    f"was re-pinned from HEAD {pinned_head} to {repinned.head_sha} "
                    "(--accept-advanced-head). The stage's uncommitted work was verified "
                    "unchanged; the only difference is the commits in that range."
                )
                record_human_evidence(stage, evidence)
                record_next_turn(
                    stage, NEXT_TURN_SPARRING, candidate=repinned, source="manual"
                )
                current_state = stage.read_state()
                report(
                    f"stage {current.stage_id}: re-pinned the pending review from "
                    f"{pinned_head} to {repinned.head_sha}"
                )
                evidence = None
            else:
                if accept_advanced_head is not None:
                    raise PlanError(
                        f"--accept-advanced-head: stage {current.stage_id!r} still holds "
                        "its pinned candidate; there is no branch advance to accept"
                    )
        elif accept_advanced_head is not None:
            raise PlanError(
                f"--accept-advanced-head applies only to a stage whose pending review no "
                f"longer matches the repository; {current.stage_id!r} has none"
            )
        if evidence is not None:
            record_human_evidence(stage, evidence)
        report(f"recorded human evidence in {stage.directory / 'notes.md'}")
        # Observational only: that evidence was recorded, never what it says.
        _plan_emitter(stage).emit("plan.evidence_recorded")
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
            # Any implementation conversation, including one a pending
            # fresh generation has just replaced: the candidate exists.
            sparrer_first = current_state.status is not StageStatus.ACCEPTED and bool(
                generations(current_state, ROLE_STAGE)
            )
            if sparrer_first:
                report(
                    f"stage {current.stage_id}: resuming the sparrer against the unchanged "
                    "candidate with this evidence; the stage agent is not started"
                )
        if sparrer_first:
            # Until a reviewer turn answers it (see _evidence_awaiting_review).
            state.evidence_pending = {
                "stage": stage.stage_id,
                "sparring_digest": _sparring_digest(stage),
            }
            state.save(state_path)

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
        visual_review=visual_review,
        report=report,
        sparrer_first=sparrer_first,
        stop_after_stage=stop_after_stage,
        next_turn_choice=next_turn,
        fresh_roles=tuple(fresh_roles),
        fresh_reason=fresh_reason,
    )


def _check_next_turn_choice(
    sparring_dir: Path,
    repo_root: Path,
    planned: PlannedStage,
    choice: str,
    *,
    evidence: bool,
) -> None:
    """Refuse (:class:`PlanError`) an explicit next turn the stage this resume
    enters cannot take, without creating or changing anything."""

    try:
        stage = Stage.resolve(sparring_dir, planned.stage_id)
        if not stage.exists():
            raise PlanError(
                f"stage {planned.stage_id!r} has not run yet: its first turn is the "
                f"implementation turn, so --next-turn {choice} is "
                + (
                    "unnecessary"
                    if choice == NEXT_TURN_STAGE
                    else "wrong (there is no candidate to review)"
                )
                + ". Nothing was changed; resume without --next-turn."
            )
        if planned.review_only:
            raise PlanError(
                f"stage {planned.stage_id!r} is review-only: it has no stage agent and no turn "
                f"to choose, so --next-turn {choice} is refused. Nothing was changed."
            )
        if stage.read_state().status is StageStatus.ACCEPTED:
            raise PlanError(
                f"stage {planned.stage_id!r} is already ACCEPTED and no agent turn is owed, so "
                f"--next-turn {choice} is refused. Nothing was changed."
            )
        waiting = None if evidence else _recorded_pause(stage)
        if waiting is not None:
            raise PlanError(
                f"stage {planned.stage_id!r} is waiting for a person ({waiting.action.value}); "
                f"--next-turn {choice} does not answer it. Nothing was changed; answer it with "
                "--evidence (a fresh reviewer session may accompany the evidence)."
            )
        check_next_turn_choice(repo_root, stage, choice)
    except (StageError, NextTurnError) as exc:
        raise PlanError(str(exc)) from exc


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
    if not any(run.open for run in find_runs(sparring_dir, label)):
        return exc if isinstance(exc, PlanError) else PlanError(str(exc))
    return PlanError(
        f"{label} can no longer be read as the plan this run started against ({exc}); "
        "a managed run's plan must not be edited while it is running. Restore it as it "
        "was, or start a new run of the document as it now is, which leaves the stopped "
        "run untouched."
    )


def _answer_deferred(
    state: PlanRunState,
    state_path: Path,
    sparring_dir: Path,
    answers: tuple[DeferredAnswer, ...],
    *,
    report: Reporter,
    context: Any = None,
) -> None:
    """Record a person's results for the deferred checks the run is stopped
    on, or refuse.

    Refused unless the run is *in fact* stopped on deferred verification and
    the check being answered is one of the askings that pause named. That
    refusal is the same rule ``--allow-push-candidate`` follows and exists
    for the same reason: a surface rendered from an older state must not be
    able to answer a question the run has since replaced, and an answer to
    an earlier asking of a check must never satisfy a later one.

    Each result is written in two places, both engine-owned, neither
    derived from the other: the run's ledger, which decides whether the plan
    may complete, and the *originating* stage's ``notes.md``, which is what
    both agents read on their next turn. The second is why provenance
    matters -- an obligation raised by stage 2 and answered after stage 7 is
    recorded against stage 2, where the review that raised it lives.
    """

    if not answers:
        return
    awaiting = state.awaiting
    if not isinstance(awaiting, DeferredVerificationRequired):
        raise PlanError(
            "this run is not stopped for deferred human verification, so there is nothing "
            "here to answer. Deferred results are only accepted at the checkpoint that "
            "asked for them; resume without --deferred-result."
        )
    asked = tuple(
        obligation
        for instance_id in awaiting.instance_ids
        if (obligation := state.obligation(instance_id)) is not None
    )
    # Every answer is resolved against an asking *before* any of them is
    # applied. Recording results one at a time and refusing half-way through
    # would leave the first answer's line in one stage's notes.md with no
    # ledger entry behind it -- a written result the plan does not know about
    # is worse than a refused resume.
    located = [_locate_deferred_check(asked, answer) for answer in answers]

    for answer, (obligation, check) in zip(answers, located):
        # Re-read: an earlier answer in this same call may have updated the
        # obligation this one also belongs to, and applying the stale copy
        # would drop that earlier result.
        current = state.obligation(obligation.instance_id) or obligation
        state.replace_obligation(
            current.with_result(
                CheckResult(check_id=check.id, outcome=answer.outcome, note=answer.note)
            )
        )
        # An obligation carried from an earlier slice was raised by a stage
        # of that slice's run, possibly in another repository: its notes.md
        # is where the answer is written.
        carried = origin_of(context, obligation.instance_id)
        origin_dir = Path(carried.origin_sparring_dir) if carried is not None else sparring_dir
        stage = Stage.resolve(origin_dir, obligation.stage_id)
        if stage.exists():
            record_human_evidence(stage, _deferred_evidence_line(obligation, check, answer))
        report(
            f"recorded {answer.outcome.word} for deferred check {check.id!r} of gate "
            f"instance {obligation.instance_id} (raised by {obligation.stage_id})"
        )
    state.save(state_path)
    if context is not None:
        sync_plan_obligations(context, state.deferred_human_checks, run=_RunOwner.of(state).key)


def _locate_deferred_check(
    asked: tuple[DeferredObligation, ...], answer: DeferredAnswer
) -> tuple[DeferredObligation, HumanCheck]:
    """Which obligation and which check one answer is about.

    A qualified ref names both outright. A bare check id is resolved only
    when exactly one of the askings in front of the person has a check by
    that name -- two askings of the same check id is precisely the situation
    the gate instance exists for, so guessing between them would undo it.
    """

    # Two readings of the same text, because a reviewer's check id is free
    # text and may itself contain a colon: the whole reference as a bare
    # check id, and the qualified ``<instance>:<check id>`` split. Both are
    # tried and the results unioned, so a colon in an id cannot make a check
    # unanswerable -- which would leave a plan that can never complete.
    matches: list[tuple[DeferredObligation, HumanCheck]] = []
    for obligation in asked:
        for check in obligation.gate.checks:
            bare = check.id == answer.check_ref
            qualified = (
                answer.instance_id == obligation.instance_id and check.id == answer.check_id
            )
            if (bare or qualified) and (obligation, check) not in matches:
                matches.append((obligation, check))
    if not matches:
        known = ", ".join(
            f"{obligation.instance_id}:{check.id}"
            for obligation in asked
            for check in obligation.gate.checks
        )
        raise PlanError(
            f"{answer.check_ref!r} is not one of the deferred checks this run is stopped "
            f"on. It is asking about: {known or '(nothing)'}"
        )
    if len(matches) > 1:
        qualified = ", ".join(
            f"{obligation.instance_id}:{check.id}" for obligation, check in matches
        )
        raise PlanError(
            f"check id {answer.check_id!r} belongs to more than one asking in this "
            f"checkpoint; say which one: {qualified}"
        )
    return matches[0]


def _deferred_evidence_line(
    obligation: DeferredObligation, check: HumanCheck, answer: DeferredAnswer
) -> str:
    """The ``## Human evidence`` entry for one answered deferred check.

    Deliberately the same line shape a human-gate result is recorded in --
    outcome, the check's own wording, its id and the gate instance it
    answers -- so everything that already reads that section keeps working
    and nothing has to learn a second format. The extra sentence says this
    was a deferred obligation, because "answered five stages later" is a
    fact a reader of the stage's notes would otherwise have no way to see.
    """

    lines = [
        f"- {answer.outcome.word} — {check.instruction} · check `{check.id}` · gate "
        f"`{obligation.instance_id}`",
    ]
    if answer.note:
        for note_line in answer.note.splitlines():
            lines.append(f"  {note_line}")
    lines.append(
        f"  (deferred human verification raised by this stage's review and answered at the "
        f"plan's verification checkpoint; the reviewer deferred it because: "
        f"{obligation.rationale})"
    )
    return "\n".join(lines)


def record_human_evidence(stage: Stage, evidence: str) -> None:
    """Append ``evidence`` under ``## Human evidence`` in the stage's notes.md
    (creating the heading on first use). Prose only; nothing parses it."""

    stage.append_note(HUMAN_EVIDENCE_HEADING, evidence)


def _ensure_stage(
    sparring_dir: Path,
    planned: PlannedStage,
    *,
    owner: _RunOwner | None = None,
    report: Reporter = lambda message: None,
) -> Stage:
    """Create the planned stage (fresh state.json, so fresh sessions) with
    the plan's brief, or continue the existing one whose brief is exactly
    that. A differing brief is refused: the plan is the source of truth for
    what a planned stage is.

    ``owner`` is the plan key of the managed run entering this stage (see
    :attr:`~agent_sparring.stage.StageState.plan`). A stage created here is
    owned by that run from the moment it exists, and an existing unowned one
    is claimed as it is entered, so a later plan cannot mistake it for its
    own. None means no managed run is driving, which leaves the stage
    unowned exactly as before.

    An ACCEPTED stage is the one exception, and it is returned without the
    brief being compared. Acceptance is terminal: nothing will run for that
    stage, only the recorded candidate is read, and its brief is a record of
    how the work was actually briefed at the time. Demanding that it match
    today's plan text would refuse a completed sequence for a wording change
    that can no longer affect anything."""

    if owner is not None:
        _refuse_foreign_stages(sparring_dir, (planned,), owner=owner)
    try:
        stage = Stage.resolve(sparring_dir, planned.stage_id)
        if not stage.exists():
            stage.create(brief=planned.brief, run=owner.key if owner else None)
            return stage
        state = stage.read_state()
        if owner is not None and state.run is None:
            _claim_stage(sparring_dir, planned.stage_id, owner=owner, report=report)
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
            refusal=True,
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
            refusal=True,
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
    pending_deferred: tuple[DeferredObligation, ...] = (),
    fresh_roles: tuple[str, ...] = (),
    fresh_reason: str | None = None,
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
            refusal=True,
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

    _apply_fresh_sessions(
        state, state_path, activity, repo_root, stage, fresh_roles, fresh_reason, report=report
    )
    try:
        _stage_adapter, reviewer_adapter = make_adapters(stage)
    except Exception as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="adapter construction failed",
            refusal=True,
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} before any "
                f"provider turn: could not build the provider adapters: {exc}"
            ),
        ) from exc

    try:
        result = run_independent_review(
            stage,
            sparring_dir,
            repo_root,
            reviewer_adapter,
            expected_branch=state.expected_branch,
            subject=subject,
            evidence_first=evidence_first,
            pending_deferred=pending_deferred,
        )
    except ReviewError as exc:
        raise _fail_or_pause_for_provider(
            state,
            state_path,
            activity,
            stage,
            exc,
            why="independent review failed",
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} (not accepted, "
                f"not advanced): {exc}"
            ),
        ) from exc
    # A provider turn ran: no later stop can be a refusal of this resume.
    state.left_provider_pause = None
    return result


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


def _leave_provider_pause(state: PlanRunState) -> None:
    if state.provider_pause is not None:
        state.left_provider_pause = state.provider_pause
    state.provider_pause = None


def _kept_provider_pause(state: PlanRunState) -> dict[str, Any] | None:
    """The record a refused resume leaves in place: the one still recorded,
    or the one this resume cleared on entry while it is still about the
    stage being entered and no provider turn has run since."""

    if state.provider_pause is not None:
        return state.provider_pause
    left = state.left_provider_pause
    if left is not None and left.get("stage_id") == state.current_stage:
        return left
    return None


def _pause(
    state: PlanRunState, state_path: Path, *, provider_pause: dict[str, Any] | None = None
) -> None:
    """Persist the run as paused. ``provider_pause`` is the descriptive
    record of why, set only by :func:`_fail_or_pause_for_provider`; every
    other pause or failure clears whatever an earlier one left."""

    state.status = PlanRunStatus.PAUSED
    state.provider_pause = provider_pause
    state.save(state_path)


#: How many times a person may answer the *same* wholly-unperformable gate
#: before the run refuses to stop for it again. Two, so the reviewer gets one
#: full turn with the engine's tally in front of it (see
#: :func:`~agent_sparring.sparring_prompt.gate_answer_tally_section`) to
#: choose a different route, and the third asking stops the run instead of
#: the person.
_MAX_ANSWERED_ASKINGS = 2


def _note_repeat_asking(
    stage: Stage, activity: ActivityEmitter, *, report: Reporter
) -> str | None:
    """Notice a gate that asks only what a person has said they cannot do.

    A reviewer holding unsatisfied acceptance checks, with no implementation
    defect to send back, has only NEEDS_YOU left -- so it re-issues the same
    gate, the person answers ``Blocked`` again, and the run treadmills. Every
    iteration needs a human, so nothing spins on its own; what it costs is
    the person's time, repeatedly, with no route out visible from inside any
    single turn.

    Two different things, deliberately:

    - Any re-asked ``Blocked`` check is *reported*, because the person is
      being asked again for something they have already declined once and
      should be told the engine can see that.
    - The run only *stops* when the gate is wholly such checks and they have
      been answered :data:`_MAX_ANSWERED_ASKINGS` times. A gate that adds a
      check, or re-asks one the person failed rather than could not attempt,
      is a reviewer making progress, and stopping it would be stopping the
      work.

    Returns a message when the run must stop instead of pausing for the same
    thing again, and ``None`` otherwise.
    """

    recorded = read_recorded_outcome(stage)
    gate = recorded.human_gate if recorded is not None else None
    if gate is None:
        return None
    try:
        answers = parse_gate_answers(stage.read_notes())
    except (StageError, OSError):
        return None
    stalled = stalled_checks(gate, answers)
    if not stalled:
        return None

    answered = repeat_asking_count(gate, answers)
    named = ", ".join(f"`{history.check_id}`" for history in stalled)
    wholly = is_repeat_asking(gate, answers)
    activity.emit(
        "gate.repeated",
        summary=(
            f"asking {answered + 1} of the same blocked checks"
            if wholly
            else f"{len(stalled)} of {len(gate.checks)} checks already answered Blocked"
        ),
    )
    if not (wholly and answered >= _MAX_ANSWERED_ASKINGS):
        report(
            f"stage {stage.stage_id}: this gate re-asks {named}, which you have already "
            "answered Blocked. The reviewer now has that tally in its prompt, along with "
            "the routes out of it."
            + (
                " If it asks only these again, the run will stop rather than ask you a "
                "third time."
                if wholly
                else ""
            )
        )
        return None
    return (
        f"plan stopped at stage {stage.stage_id!r}: its reviewer has now asked the same "
        f"checks {answered + 1} times ({named}), and every earlier asking was answered "
        "Blocked -- the person reported they could not perform them. Asking again cannot "
        "resolve them, so the run stops here rather than pausing for the same question "
        "once more. Nothing was discarded: the verdict is recorded in sparring.md and the "
        "answers in notes.md. Three ways forward, all of which change what the next turn "
        "sees: make the checks performable (freeze the candidate, provide the environment) "
        "and answer them; answer them Fail with what you did observe, which is a result a "
        "reviewer can act on; or record evidence saying the checks must be deferred, which "
        "lets the reviewer accept the stage with the verification still owed in the run's "
        "ledger."
    )


def _reconcile_deferred(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    stage: Stage,
    routings: tuple[RoutingResult, ...] = (),
    *,
    report: Reporter,
) -> None:
    """Bring the run's obligation ledger up to date with one accepted stage.

    Called after every acceptance, and on every pass over an
    already-accepted stage. It is idempotent on the gate instance, so
    repeating it records nothing new -- which is what lets it be called from
    every one of those places rather than from the single one that happens
    to be convenient.

    It reads **two** sources, because neither alone is complete, and an
    obligation that never reaches the ledger is an obligation that cannot
    stop the plan completing -- the one property this whole feature is for:

    - ``routings``, every sparring verdict this stage's loop produced. The
      loop does not stop at the first READY: a READY over an uncommitted
      candidate routes one more commit-and-review cycle, whose sparring turn
      *overwrites* ``sparring.md``. A deferral raised by the first READY is
      then on disk nowhere, and only the cycle records still hold it;
    - the verdict actually recorded in ``sparring.md``, which covers every
      path where this process did not produce the verdict at all: a resume
      that only pushes an already-authorized candidate and runs no agent, a
      stage accepted by hand, and a run killed between ``accept_candidate``
      and this write.

    Reconciling after acceptance rather than at verdict time is deliberate:
    a stage that did not accept leaves no obligation behind, so a run that
    stopped at the gate and was later reset does not carry a deferral for
    work that was never accepted.
    """

    recorded = read_recorded_outcome(stage)
    sources: list[RoutingResult | RecordedOutcome] = [*routings]
    if recorded is not None:
        sources.append(recorded)

    raised: list[DeferredHumanGate] = []
    for source in sources:
        deferred = source.deferred_human_gate
        if deferred is None:
            continue
        if deferred.instance_id is None:
            # An obligation nothing could ever answer. Unreachable through
            # run_sparring_agent, which mints the instance before this is
            # read; a recorded file that lost it is read as no deferral
            # rather than as an unanswerable one.
            if isinstance(source, RecordedOutcome):
                continue
            raise _fail(
                state,
                state_path,
                activity,
                why="deferral without a gate instance",
                message=(
                    f"plan {state.plan} stopped at stage {stage.stage_id!r}: its review "
                    "deferred human verification without an engine-minted gate instance, "
                    "and an obligation nothing can answer must not be recorded"
                ),
            )
        _fold_deferral(raised, deferred)

    carried = _carry_blocked_checks(stage, raised)
    if carried is not None:
        _fold_deferral(raised, carried)

    changed = False
    for deferred in raised:
        if state.record_deferred(DeferredObligation.from_gate(stage.stage_id, deferred)):
            changed = True
            activity.emit(
                "plan.deferred_recorded",
                summary=f"{len(deferred.checks)} manual check(s) owed by {deferred.checkpoint}",
            )
            report(
                f"stage {stage.stage_id}: accepted, and {len(deferred.checks)} manual "
                f"{'check is' if len(deferred.checks) == 1 else 'checks are'} deferred "
                f"until plan completion — {deferred.title}. The reviewer's reason: "
                f"{deferred.rationale}"
            )

    # Promotions come only from verdicts this run produced. A recorded file
    # is re-read on every pass over an accepted stage, and re-applying an old
    # promotion from it would re-raise a checkpoint a person had already been
    # through.
    for routing in routings:
        for instance_id in routing.promote_deferred:
            obligation = state.obligation(instance_id)
            if obligation is None:
                # The reviewer named something this run does not owe. Said out
                # loud rather than silently dropped, and never fatal: a mistyped
                # id must not destroy an otherwise good accepted stage.
                report(
                    f"stage {stage.stage_id}: the reviewer asked to promote deferred gate "
                    f"instance {instance_id!r}, which is not in this run's ledger; ignoring it"
                )
                continue
            if obligation.resolved or obligation.promoted:
                continue
            state.replace_obligation(obligation.promote())
            changed = True
            activity.emit("plan.deferred_promoted", summary=obligation.describe)
            report(
                f"stage {stage.stage_id}: the reviewer promoted the deferred check "
                f"{obligation.gate.title!r} (raised by {obligation.stage_id}) to immediate; "
                "the run stops for it before going further"
            )
    if changed:
        state.save(state_path)


def _carry_blocked_checks(
    stage: Stage, raised: list[DeferredHumanGate]
) -> DeferredHumanGate | None:
    """Keep a check a person could not perform from vanishing at acceptance.

    ``Blocked`` resolves nothing -- that is what it means, on both sides of
    the gate. So a check a person answered Blocked is still owed, and a
    stage accepting with it neither answered nor deferred would waive it
    silently. Nobody decided that; it is what falls out when a reviewer,
    told that deferring is the way past a Blocked check, chooses READY and
    then simply does not write the ``deferred_human_gate``. Observed on the
    first run after that guidance shipped: three checks, two of them
    answered Blocked twice, dropped by a READY that reasoned correctly about
    every one of them.

    The engine does not decide the reviewer's timing judgement here, and
    this is not that. It is bookkeeping, and it is strictly conservative:
    the obligation is kept, the plan cannot report complete until a person
    answers it, and the rationale says plainly that the engine carried it
    rather than pretending a reviewer weighed it.

    Only checks with a recorded ``Blocked`` answer, and only ones no
    deferral in ``raised`` already covers. A ``Fail`` is a result and a
    reviewer may accept a stage over one with its reasons; a check nobody
    answered at all may genuinely have been overtaken by the code. Neither
    is a person saying "I tried and could not", which is the one fact this
    refuses to lose.
    """

    recorded = read_recorded_outcome(stage)
    gate = recorded.human_gate if recorded is not None else None
    if gate is None or not gate.checks:
        return None
    try:
        answers = parse_gate_answers(stage.read_notes())
    except (StageError, OSError):
        return None
    already = {
        check.id for deferred in raised for check in deferred.checks
    }
    stalled = [
        history for history in stalled_checks(gate, answers) if history.check_id not in already
    ]
    if not stalled:
        return None

    by_id = {check.id: check for check in gate.checks}
    checks = tuple(by_id[history.check_id] for history in stalled)
    # Derived from what it carries, not minted fresh, because this runs on
    # every pass over an accepted stage: a new instance each time would
    # record the same obligation again under a new id, and ask a person the
    # same thing twice.
    digest = hashlib.sha256(
        "\n".join([stage.stage_id, *(check.id for check in checks)]).encode("utf-8")
    ).hexdigest()[:16]
    return DeferredHumanGate(
        gate=HumanGate(
            category=gate.category,
            title=f"Still owed from {gate.title}",
            checks=checks,
            instance_id=f"carried-{digest}",
        ),
        rationale=(
            "Carried forward by the engine, not weighed by a reviewer: a person reported "
            "they could not perform "
            + ("this check" if len(checks) == 1 else "these checks")
            + ", and the stage was accepted without answering or deferring "
            + ("it" if len(checks) == 1 else "them")
            + ". Blocked resolves nothing, so the verification is still owed and the plan "
            "may not complete until it is answered."
        ),
        checkpoint=CHECKPOINT_PLAN_COMPLETION,
    )


def _fold_deferral(raised: list[DeferredHumanGate], deferred: DeferredHumanGate) -> None:
    """Add ``deferred`` to ``raised``, or replace the same question already in
    it with this later asking of it.

    One stage's loop can write the same deferral twice. A READY over an
    uncommitted candidate routes a commit-and-review cycle, and the reviewer
    of that second turn is looking at the same work with the same reservation
    -- so it restates the deferral, and ``record_sparring`` mints it a fresh
    instance, because every write is a new asking and the engine cannot tell
    a restatement from a materially different question by looking at one
    verdict.

    Here it can tell, because it has both. Two deferrals asking the same
    questions are one deferral, and recording them as two obligations would
    ask a person the same thing twice under two ids -- and make the bare
    check id ambiguous, so neither could be answered without naming an
    instance. The *later* asking wins: it is the one whose instance is in
    ``sparring.md``, which is what every other reader of this stage sees, and
    it supplies the contents, so any rewording the reviewer settled on is the
    one a person reads.

    A reviewer that changes *which* checks it is asking for gets a second
    obligation. That is the same rule as everywhere else: same unresolved
    obligation, same instance; materially reissued, new instance.
    """

    for position, existing in enumerate(raised):
        if _same_question(existing, deferred):
            raised[position] = deferred
            return
    raised.append(deferred)


def _same_question(a: DeferredHumanGate, b: DeferredHumanGate) -> bool:
    """Are these two deferrals asking for the same checks?

    Compared by the reviewer's **check ids**, because that is what a check id
    is for: :mod:`agent_sparring.human_gate` defines it as stable across
    sparring turns for the same check, precisely so a recorded outcome
    survives the reviewer restating its gate. Comparing the instructions
    instead would miss the ordinary case this fold exists for -- a second
    generation of the same review re-emitting the same check id with one word
    changed -- and hand back the duplicate obligation.

    ``category`` and ``checkpoint`` are compared because they are closed
    values a reviewer chooses deliberately; ``title`` and ``rationale`` are
    not, because they are prose and drift between two generations of the same
    judgement. The later gate supplies all of it anyway, so the wording a
    person reads is the wording the reviewer settled on.
    """

    return (
        a.category == b.category
        and a.checkpoint == b.checkpoint
        and tuple(check.id for check in a.checks) == tuple(check.id for check in b.checks)
    )


def _loop_routings(result: "LoopResult | None") -> tuple[RoutingResult, ...]:
    """Every verdict a stage's loop produced, in order.

    Not just ``result.routing``: that is the *last* one, and a loop that
    finalized an uncommitted candidate produced an earlier READY whose
    ``sparring.md`` has since been overwritten.
    """

    if result is None:
        return ()
    return tuple(cycle.routing for cycle in result.cycles) or (result.routing,)


def _carry_to_plan(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    context: Any,
    sparring_dir: Path,
    pending_slices: tuple[str, ...],
    *,
    report: Reporter,
) -> None:
    """Hand this slice's unresolved obligations to the plan's ledger.

    Only obligations owed by plan completion move; one a reviewer promoted
    is due now and has already stopped the run before this point. Nothing is
    answered, waived or marked: the entries leave this run's ledger with
    their results and gate instances intact, and the plan's end asks for
    them.
    """

    carried = tuple(
        entry
        for entry in state.unresolved_deferred
        if entry.checkpoint == CHECKPOINT_PLAN_COMPLETION
        and not entry.promoted
        and not entry.manifest_gate
    )
    if not carried:
        return
    try:
        carry_plan_obligations(
            context, carried, origin_run=_RunOwner.of(state).key, origin_sparring_dir=sparring_dir
        )
    except (PlanObligationError, OSError) as exc:
        raise PlanError(
            f"could not carry {len(carried)} deferred check(s) to the plan ledger at "
            f"{context.ledger_path}: {exc}. The run stays where it is; nothing was carried"
        ) from exc
    ids = {entry.instance_id for entry in carried}
    state.deferred_human_checks = tuple(
        entry for entry in state.deferred_human_checks if entry.instance_id not in ids
    )
    state.carried_deferred = state.carried_deferred + tuple(
        entry.instance_id for entry in carried if entry.instance_id not in state.carried_deferred
    )
    state.awaiting = None
    state.save(state_path)
    for entry in carried:
        activity.emit(
            "plan.deferred_carried",
            summary=f"{entry.gate.title} carried to plan completion (owed after {', '.join(pending_slices)})",
        )
        report(
            f"deferred check {entry.gate.title!r} (raised by {entry.stage_id}, gate instance "
            f"{entry.instance_id}) is owed by the whole plan, not this run slice: carried to "
            f"{context.ledger_path}; it will be asked for when the plan's last slice "
            f"completes (still to complete: {', '.join(pending_slices)})"
        )


def _claim_from_plan(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    context: Any,
    *,
    report: Reporter,
) -> None:
    """At the plan's end, bring every carried, unresolved obligation into this
    run's ledger (same gate instance, same results), so the completion
    checkpoint below covers the whole plan."""

    try:
        carried = owed_plan_obligations(context)
    except PlanObligationError as exc:
        raise PlanError(f"cannot read the plan's deferred checks: {exc}") from exc
    claimed = [entry for entry in carried if state.record_deferred(entry.obligation)]
    if not claimed:
        return
    state.save(state_path)
    for entry in claimed:
        activity.emit("plan.deferred_claimed", summary=f"{entry.obligation.gate.title} (raised by {entry.origin_slice})")
        report(
            f"deferred check {entry.obligation.gate.title!r} raised by run slice "
            f"{entry.origin_slice} ({entry.obligation.stage_id}) is due now: this is the plan's "
            f"last slice"
        )


def _manifest_gate_category(gate: ManifestGate) -> str:
    return "EXTERNAL_CONDITION" if gate.kind == "external" else "OTHER"


def _owe_manifest_gates(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter | None,
    stage_id: str,
    gates: tuple[ManifestGate, ...],
    *,
    checkpoint: str,
    report: Reporter,
) -> tuple[DeferredObligation, ...]:
    """Mint one engine-owned obligation per plan-declared ``gates`` entry
    owed by ``checkpoint``, and return the ones still unresolved.

    Idempotent by (checkpoint, gate id): a resume that reaches the same
    gate again finds the obligation it minted -- and any answer recorded to
    it -- instead of asking anew. ``stage_id`` is the accepted stage the gate
    follows (or, before the run's first stage, the gated stage itself, whose
    answer then lives only in the run ledger); the answer is written to its
    ``notes.md`` when that exists. Every gate kind is
    owed the same way: nothing about a gate is ever taken as satisfied.
    """

    owed: list[DeferredObligation] = []
    minted = False
    for gate in gates:
        existing = next(
            (
                entry
                for entry in state.deferred_human_checks
                if entry.manifest_gate
                and entry.checkpoint == checkpoint
                and any(check.id == gate.id for check in entry.gate.checks)
            ),
            None,
        )
        if existing is None:
            existing = DeferredObligation(
                stage_id=stage_id,
                gate=HumanGate(
                    category=_manifest_gate_category(gate),
                    title=gate.title,
                    checks=(
                        HumanCheck(
                            id=gate.id,
                            instruction=(
                                f"Confirm the plan-declared {gate.kind} gate {gate.title!r} is "
                                f"satisfied. Why it exists: {gate.reason}"
                            ),
                            pass_criteria=(
                                "The gate is actually satisfied. Reaching this point in the run "
                                "does not satisfy it."
                            ),
                        ),
                    ),
                ).asked_again(new_gate_instance_id()),
                rationale=gate.reason,
                checkpoint=checkpoint,
                manifest_gate=True,
            )
            state.record_deferred(existing)
            minted = True
            if activity is not None:
                activity.emit("plan.gate_owed", summary=f"{gate.title} ({gate.kind}) at {checkpoint}")
            report(
                f"plan-declared {gate.kind} gate {gate.title!r} ({gate.id}) is owed at {checkpoint}: "
                f"gate instance {existing.instance_id}"
            )
        if not existing.resolved:
            owed.append(existing)
    if minted:
        state.save(state_path)
    return tuple(owed)


def _stop_for_deferred(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter | None,
    obligations: tuple[DeferredObligation, ...],
    *,
    reason: str,
    stage_id: str,
    accepted: list[tuple[str, str]],
    report: Reporter,
) -> PlanRunResult:
    """Stop the run for verification a person already owes.

    One pause, however many obligations accumulated, and however many stages
    they came from: interrupting somebody four times for four checks that
    have been waiting anyway is worse than asking once with all four in
    front of them. Provenance is not lost by doing that -- every obligation
    still names the stage that raised it and the asking it belongs to.
    """

    state.awaiting = DeferredVerificationRequired.for_obligations(obligations, reason=reason)
    _pause(state, state_path)
    if activity is not None:
        activity.emit("plan.paused", summary=state.awaiting.describe)
    failed = [entry for entry in obligations if entry.failed]
    report(f"plan {state.plan}: {state.awaiting.describe}.")
    for obligation in obligations:
        report(
            f"  {obligation.gate.title} — raised by {obligation.stage_id}, gate instance "
            f"{obligation.instance_id}"
        )
        for check in obligation.unanswered:
            answered = obligation.result_for(check.id)
            state_word = (
                "recorded as Blocked — no verification result was obtained, so this is "
                "still owed"
                if answered is not None
                else "not answered yet"
            )
            report(f"    {check.id}: {state_word}")
        for result in obligation.failed:
            note = f" — {result.note}" if result.note else ""
            report(f"    {result.check_id}: FAILED{note}")
    if failed:
        # The recovery path, stated rather than implied. Nothing is rewound
        # automatically: the stage that raised the obligation is accepted,
        # and un-accepting it is a real integrity action that belongs to a
        # person, not to a plan runner reacting to a check result. What the
        # engine guarantees is that the plan does not finish, that the
        # failure is durable in this run's ledger, and that it is written
        # into the originating stage's notes.md where both agents read it.
        report(
            "A deferred check failed. This plan will not complete, and nothing is rewound: "
            "the stage that raised the check is accepted, and un-accepting accepted work is "
            "a person's decision, not a plan runner's reaction to a check result. The "
            "failure is recorded in that stage's notes.md, so its agents read it on their "
            "next turn. Decide where the correction belongs — usually a further stage — and "
            "re-answer the check with --deferred-result once the behaviour is right."
        )
    return PlanRunResult(
        status=PlanRunStatus.PAUSED,
        plan=state.plan,
        run=_RunOwner.of(state).key,
        stage_id=stage_id,
        awaiting=state.awaiting,
        deferred=obligations,
        accepted=tuple(accepted),
    )


def _plan_emitter(stage: Stage) -> ActivityEmitter:
    """The plan runner's observational emitter into ``stage``'s own
    ``activity.jsonl`` (see :mod:`agent_sparring.activity`). Write-only and
    never raising; every ``plan.*`` line below is a mirror of a decision
    already taken from the run-state file and ``state.json``, never an
    input to one."""

    return stage.activity_log().bind("plan")


def _sparring_digest(stage: Stage) -> str:
    return hashlib.sha256(stage.read_sparring().encode("utf-8")).hexdigest()


def _evidence_awaiting_review(state: PlanRunState, stage: Stage) -> bool:
    """Is recorded evidence for ``stage`` still waiting for the reviewer
    turn that answers it (see :attr:`PlanRunState.evidence_pending`)?"""

    pending = state.evidence_pending
    if not pending or pending.get("stage") != stage.stage_id:
        return False
    try:
        return pending.get("sparring_digest") == _sparring_digest(stage)
    except (OSError, StageError):
        return False


def _retire_pending_evidence(state: PlanRunState, state_path: Path) -> None:
    """A reviewer verdict was recorded: any pending evidence has had its turn."""

    if state.evidence_pending is not None:
        state.evidence_pending = None
        state.save(state_path)


def _failed_role(exc: BaseException) -> str:
    """Whose turn ``exc`` came from: the reviewer's when a sparring /
    independent-review turn is anywhere in its cause chain, else the stage
    agent's."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (SparringAgentRunError, ReviewError)):
            return ROLE_SPARRING
        if isinstance(current, StageAgentRunError):
            return ROLE_STAGE
        current = current.__cause__
    return ROLE_STAGE


def _fail_or_pause_for_provider(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    stage: Stage,
    exc: BaseException,
    *,
    why: str,
    message: str,
) -> PlanRunError:
    """:func:`_fail`, except that a turn failing as unresumable or
    unavailable becomes a typed :class:`ProviderPause` for the role whose
    turn it was. Either way the run is paused and nothing is retried,
    discarded or touched."""

    failure = recoverable_provider_failure(exc)
    if failure is None:
        # A candidate refusal before any turn is a refused resume, which
        # leaves the recorded provider pause as it was.
        return _fail(
            state, state_path, activity, why=why, message=message,
            refusal=isinstance(exc, CandidateRefused),
        )
    role = _failed_role(exc)
    try:
        has_session = current_session_id(stage.read_state(), role) is not None
    except StageError:
        has_session = False
    if isinstance(failure, ProviderSessionUnresumable):
        kind = PAUSE_SESSION_UNRESUMABLE
        explanation = (
            f"the provider can no longer resume the recorded {role} conversation; retrying "
            "it cannot succeed. Continue the same stage in a fresh session for that role"
        )
    else:
        kind = PAUSE_PROVIDER_UNAVAILABLE
        explanation = (
            "the provider declined the turn for quota / rate-limit reasons; retry when it is "
            "available again, or continue in a fresh session on another provider"
        )
    _pause(
        state,
        state_path,
        provider_pause={
            "kind": kind,
            "role": role,
            "stage_id": stage.stage_id,
            "has_session": has_session,
            "recorded_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        },
    )
    activity.emit("plan.paused", summary=f"{role} {kind}")
    return ProviderPause(
        f"{message}. Paused, not failed: {explanation}. Nothing was retried, no session was "
        "discarded and the candidate was not touched",
        stage_id=stage.stage_id,
        role=role,
        kind=kind,
        has_session=has_session,
        run=_RunOwner.of(state).key,
    )


def _apply_fresh_sessions(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    repo_root: Path,
    stage: Stage,
    roles: tuple[str, ...],
    reason: str | None,
    *,
    report: Reporter,
) -> None:
    """Start the fresh sessions a person asked for, immediately before the
    stage's agent turns, or stop the run with nothing changed."""

    if not roles:
        return
    try:
        opened = start_fresh_sessions(stage, roles, reason or "manual", repo_root=repo_root)
    except SessionError as exc:
        raise _fail(
            state,
            state_path,
            activity,
            why="fresh session refused",
            refusal=True,
            message=(
                f"plan {state.plan} stopped at stage {stage.stage_id!r} before any provider "
                f"turn: {exc}"
            ),
        ) from exc
    for role, pending in opened.items():
        report(
            f"stage {stage.stage_id}: starting a fresh {role} session (generation "
            f"{pending.generation}, {pending.start_reason}); same stage, candidate and "
            "artifacts, new provider conversation"
        )


def _refuse_unapplied(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    stage: Stage,
    *,
    fresh_roles: tuple[str, ...],
    next_turn_choice: str | None,
    why: str,
) -> None:
    """Refuse a fresh-session or next-turn request the stage this resume
    enters cannot honour, instead of silently ignoring it."""

    asked = [f"a fresh {role} session" for role in fresh_roles]
    if next_turn_choice is not None:
        asked.append(f"next turn = {next_turn_choice}")
    if not asked:
        return
    raise _fail(
        state,
        state_path,
        activity,
        why="resume request not applicable",
        refusal=True,
        message=(
            f"plan {state.plan} stopped at stage {stage.stage_id!r} before any provider turn: "
            f"{' and '.join(asked)} was asked for, but {why}. Nothing was changed"
        ),
    )


def _fail(
    state: PlanRunState,
    state_path: Path,
    activity: ActivityEmitter,
    *,
    why: str,
    message: str,
    refusal: bool = False,
) -> PlanRunError:
    """Pause the run, mirror ``plan.failed`` with a fixed phrase (``why`` is
    orchestration-authored; the exception text, which can carry provider
    output or paths, is never copied into telemetry), and build the
    :class:`PlanRunError` for the caller to raise. A ``refusal`` -- a resume
    request refused before any provider turn -- keeps the recorded
    ``provider_pause`` it found, even one already cleared on entry."""

    _pause(
        state,
        state_path,
        provider_pause=_kept_provider_pause(state) if refusal else None,
    )
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
    visual_review: VisualReviewConfig | None = None,
    sparrer_first: bool = False,
    stop_after_stage: str | None = None,
    next_turn_choice: str | None = None,
    fresh_roles: tuple[str, ...] = (),
    fresh_reason: str | None = None,
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
    pending_push = state.awaiting if isinstance(state.awaiting, PushRequired) else None
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
                run=_RunOwner.of(state).key,
                stage_id=plan_stage.stage_id,
                accepted=tuple(accepted),
            )
        if state.current_stage_index == 0 and plan_stage.gates_before:
            # Gates before the run's first stage: nothing precedes it to
            # wait after, so the run stops before creating anything. The
            # obligation names the gated stage; its answer lives in the run
            # ledger (the stage has no notes.md yet).
            gated = _owe_manifest_gates(
                state,
                state_path,
                last_activity,
                plan_stage.stage_id,
                plan_stage.gates_before,
                checkpoint=before_stage_checkpoint(plan_stage.stage_id),
                report=report,
            )
            if gated:
                return _stop_for_deferred(
                    state,
                    state_path,
                    last_activity,
                    gated,
                    reason=DeferredVerificationRequired.BEFORE_STAGE,
                    stage_id=plan_stage.stage_id,
                    accepted=accepted,
                    report=report,
                )
        try:
            stage = _ensure_stage(
                sparring_dir, plan_stage, owner=_RunOwner.of(state), report=report
            )
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
        # Like sparrer_first: a request about the stage this resume enters,
        # never about the stages after it.
        entering_fresh, fresh_roles = fresh_roles, ()
        if state.evidence_pending is not None:
            if not sparrer_first and _evidence_awaiting_review(state, stage):
                # The evidence turn a person already asked for failed before
                # its verdict (e.g. the reviewer's provider was unavailable):
                # retrying it, plainly or with a fresh reviewer, re-enters
                # that same turn over the same, re-verified candidate.
                sparrer_first = True
                report(
                    f"stage {stage.stage_id}: the human evidence recorded earlier has not "
                    "been reviewed yet; resuming the sparrer with it"
                )
            elif not sparrer_first:
                state.evidence_pending = None
                state.save(state_path)
        try:
            stage_state = stage.read_state()
        except StageError as exc:
            raise _fail(
                state,
                state_path,
                activity,
                why="stage state unreadable",
                refusal=True,
                message=f"plan {state.plan} stopped at {stage.stage_id!r}: {exc}",
            ) from exc

        _declare_mode(state, state_path, activity, stage, plan_stage)

        position = f"{plan_stage.position}/{total}"
        if stage_state.status is StageStatus.ACCEPTED:
            # Already through the hard gate (by hand, or by a run that stopped
            # between accept and advance): nothing to run, just move on.
            _refuse_unapplied(
                state,
                state_path,
                activity,
                stage,
                fresh_roles=entering_fresh,
                next_turn_choice=next_turn_choice,
                why="the stage is already ACCEPTED and no agent turn is owed",
            )
            activity.emit(
                "plan.stage.entered",
                summary=f"{plan_stage.display} ({position}); already accepted, advancing",
            )
            report(f"stage {position} {stage.stage_id}: already ACCEPTED at "
                   f"{stage_state.candidate_sha}; advancing")
            accepted.append((stage.stage_id, str(stage_state.candidate_sha)))
            # Leaving the pause without a turn still leaves it: the reason
            # must not survive into the advance or completion saves.
            _leave_provider_pause(state)
            # Nothing ran here, and something may still be owed: a stage
            # accepted by hand, or by a run killed between the acceptance
            # gate and the ledger write, carries its reviewer's deferral in
            # sparring.md and nowhere else. Idempotent on the gate instance.
            _reconcile_deferred(state, state_path, activity, stage, report=report)
        else:
            waiting = None if sparrer_first else _recorded_pause(stage)
            if waiting is not None:
                # Neither a new conversation nor a chosen next turn answers
                # a person's gate; refused rather than recorded for later.
                _refuse_unapplied(
                    state,
                    state_path,
                    activity,
                    stage,
                    fresh_roles=entering_fresh,
                    next_turn_choice=next_turn_choice,
                    why=(
                        f"the stage is waiting for a person ({waiting.action.value}); answer it "
                        "with --evidence (a fresh reviewer session may accompany the evidence)"
                    ),
                )
                # This stage already stopped for a person, and nothing here
                # answers that. Running the stage agent would spend a turn on
                # a question it cannot answer and move the very candidate the
                # reviewer ruled on; running the sparrer again would throw
                # away the recorded verdict and ask the human to produce the
                # gate a second time. So the run adopts the pause exactly as
                # it stands -- same sessions, same candidate, same
                # sparring.md -- and the way out is the way it always was:
                # answer it, with `resume-plan --evidence`. No provider turn
                # ran, so a recorded provider pause reason is kept too.
                _pause(state, state_path, provider_pause=_kept_provider_pause(state))
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
                    run=_RunOwner.of(state).key,
                    stage_id=stage.stage_id,
                    recorded=waiting,
                    accepted=tuple(accepted),
                )
            state.status = PlanRunStatus.RUNNING
            # A recorded typed stop describes the pause this run is leaving,
            # never the one it might reach next; it is cleared on entry so it
            # can only ever be re-recorded by the step that means it.
            state.awaiting = None
            # Likewise the provider failure that paused it: descriptive of
            # the pause being left, never read to choose what runs now.
            _leave_provider_pause(state)
            state.save(state_path)
            # The declared cross-repository candidate set belongs to the
            # stage, so the acceptance gate finds it wherever it is invoked
            # from. Written before any provider turn.
            _declare_repositories(stage, plan_stage)

            if plan_stage.review_only:
                _refuse_unapplied(
                    state,
                    state_path,
                    activity,
                    stage,
                    fresh_roles=tuple(role for role in entering_fresh if role == ROLE_STAGE),
                    next_turn_choice=next_turn_choice,
                    why="the stage is review-only: it has no stage agent and no turn to choose",
                )
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
                    pending_deferred=state.unresolved_deferred,
                    fresh_roles=entering_fresh,
                    fresh_reason=fresh_reason,
                    report=report,
                )
                sparrer_first = False  # only the stage this resume entered
                _retire_pending_evidence(state, state_path)
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
                    stuck = _note_repeat_asking(stage, activity, report=report)
                    if stuck is not None:
                        raise _fail(
                            state,
                            state_path,
                            activity,
                            why="gate re-asked what a person cannot do",
                            message=stuck,
                        )
                    return PlanRunResult(
                        status=PlanRunStatus.PAUSED,
                        plan=state.plan,
                        run=_RunOwner.of(state).key,
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
                _reconcile_deferred(
                    state,
                    state_path,
                    activity,
                    stage,
                    (review.routing,) if review.routing is not None else (),
                    report=report,
                )

            else:
                # A run stopped for push permission, resumed with it, and
                # still at the exact commit it stopped over, has nothing left
                # for either agent: the READY is recorded, the candidate is
                # that commit, and the only missing step is the push itself.
                # None on the authorized-push path below, where no agent
                # runs at all: there is no fresh verdict then, and the
                # obligations the recorded one raised were folded into the
                # ledger by the run that produced it.
                loop_result: LoopResult | None = None
                authorized_push = _resuming_authorized_push(repo_root, stage, pending_push)
                pending_push = None  # only the stage this resume entered
                # READY was recorded over a candidate that was already a
                # commit, and the run stopped before acceptance: the
                # next_turn marker says so, with the commit it was given
                # over. Only the push and acceptance gates are left, for
                # exactly that commit; a moved candidate is refused rather
                # than handed to either agent.
                ready_commit = False
                if (
                    authorized_push is None
                    and not sparrer_first
                    and stage_state.next_turn == NEXT_TURN_FINALIZATION
                    and stage_state.next_turn_candidate is not None
                    and stage_state.next_turn_candidate.kind == "commit"
                ):
                    try:
                        ready_commit = (
                            resolve_finalization(repo_root, stage, stage_state) == RESUME_ACCEPT
                        )
                    except NextTurnError as exc:
                        raise _fail(
                            state,
                            state_path,
                            activity,
                            why="next turn undetermined",
                            refusal=True,
                            message=(
                                f"plan {state.plan} stopped at stage {stage.stage_id!r} "
                                f"before any provider turn: {exc}"
                            ),
                        ) from exc
                if authorized_push is not None or ready_commit:
                    _refuse_unapplied(
                        state,
                        state_path,
                        activity,
                        stage,
                        fresh_roles=entering_fresh,
                        next_turn_choice=next_turn_choice,
                        why=(
                            "the reviewer already said READY over this commit; only the push "
                            "and acceptance gates are left and no agent runs"
                        ),
                    )
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
                elif ready_commit:
                    activity.emit(
                        "plan.stage.entered",
                        summary=f"{plan_stage.display} ({position}); completing a recorded READY",
                    )
                    report(
                        f"stage {position} {stage.stage_id}: the reviewer already said READY over "
                        f"{stage_state.next_turn_candidate.head_sha}, which is still the "
                        "candidate; running the push and acceptance gates for exactly that "
                        "commit. No agent runs."
                    )
                else:
                    if sparrer_first:
                        start_with = "sparring"
                        sparrer_first = False  # only the stage this resume entered
                    elif stage_state.next_turn is None and _awaiting_finalization(
                        state, state_path, activity, repo_root, stage
                    ):
                        # Stopped between the reviewer's READY and the commit that
                        # acceptance can freeze, in state written before the
                        # next_turn marker existed. With a marker, the marker's
                        # own reviewed candidate decides instead (below).
                        # Never suppressed by --next-turn: a chosen turn is
                        # refused here rather than reopening reviewed work.
                        _refuse_unapplied(
                            state,
                            state_path,
                            activity,
                            stage,
                            fresh_roles=(),
                            next_turn_choice=next_turn_choice,
                            why="the reviewer already said READY; the reviewed work is finalized",
                        )
                        start_with = "finalization"
                    else:
                        # The engine's own record of whose turn it is (see
                        # agent_sparring.next_turn): derived once for state
                        # written before it existed, or chosen explicitly.
                        choice, next_turn_choice = next_turn_choice, None
                        try:
                            marker = resolve_resume_turn(repo_root, stage, choice=choice)
                            if marker == NO_TURN_OWED:
                                # Legacy READY with nothing after it (an
                                # uncommitted one was routed to finalization
                                # above): review the commit, never implement.
                                marker = resolve_legacy_ready(repo_root, stage)
                            elif marker == NEXT_TURN_FINALIZATION:
                                # Matched against the candidate READY was given
                                # over: commit it, review its commit, or refuse --
                                # never an implementation turn.
                                marker = resolve_finalization(
                                    repo_root, stage, stage.read_state()
                                )
                                if marker == RESUME_ACCEPT:
                                    # Handled before this branch; never mapped
                                    # to an agent turn if it ever arrives here.
                                    raise NextTurnError(
                                        f"stage {stage.stage_id!r} is READY over its "
                                        "committed candidate; no agent turn is owed"
                                    )
                        except NextTurnError as exc:
                            raise _fail(
                                state,
                                state_path,
                                activity,
                                why="next turn undetermined",
                                refusal=True,
                                message=(
                                    f"plan {state.plan} stopped at stage {stage.stage_id!r} "
                                    f"before any provider turn: {exc}"
                                ),
                            ) from exc
                        start_with = {
                            NEXT_TURN_SPARRING: "next_turn",
                            NEXT_TURN_FINALIZATION: "finalization",
                        }.get(marker, "stage")
                    entering = {
                        "next_turn": "; reviewing the recorded candidate",
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
                            "next_turn": (
                                "an implementation turn already completed; reviewing its "
                                "recorded candidate without another implementation turn"
                            ),
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
                    #
                    # A requested fresh session is started here: after every
                    # refusal that could still stop this stage before a turn,
                    # and before the adapters resolve its configuration.
                    _apply_fresh_sessions(
                        state,
                        state_path,
                        activity,
                        repo_root,
                        stage,
                        entering_fresh,
                        fresh_reason,
                        report=report,
                    )
                    try:
                        stage_adapter, sparring_adapter = make_adapters(stage)
                    except Exception as exc:
                        raise _fail(
                            state,
                            state_path,
                            activity,
                            why="adapter construction failed",
                            refusal=True,
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
                            visual_review=visual_review,
                            start_with="sparring" if start_with == "next_turn" else start_with,
                            sparring_first_reason=(
                                "next_turn" if start_with == "next_turn" else "evidence"
                            ),
                            pending_deferred=state.unresolved_deferred,
                        )
                    except LoopError as exc:
                        raise _fail_or_pause_for_provider(
                            state,
                            state_path,
                            activity,
                            stage,
                            exc,
                            why="stage loop failed",
                            message=(
                                f"plan {state.plan} stopped at stage {stage.stage_id!r} (not "
                                f"accepted, not advanced): {exc}"
                            ),
                        ) from exc

                    # A provider turn ran: no later stop can be a refusal
                    # of this resume.
                    state.left_provider_pause = None
                    _retire_pending_evidence(state, state_path)
                    if loop_result.outcome is not RoutingAction.READY:
                        _pause(state, state_path)
                        activity.emit("plan.paused", action=loop_result.outcome.value)
                        report(
                            f"stage {stage.stage_id}: {loop_result.outcome.value}; plan paused"
                        )
                        stuck = _note_repeat_asking(stage, activity, report=report)
                        if stuck is not None:
                            raise _fail(
                                state,
                                state_path,
                                activity,
                                why="gate re-asked what a person cannot do",
                                message=stuck,
                            )
                        return PlanRunResult(
                            status=PlanRunStatus.PAUSED,
                            plan=state.plan,
                            run=_RunOwner.of(state).key,
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
                        run=_RunOwner.of(state).key,
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
                # The reviewer's own words for what this acceptance does and
                # does not claim. A stage accepted with a deferred obligation
                # is accepted *and* still owes verification, and the report
                # says both rather than letting "accepted" read as "verified".
                _reconcile_deferred(
                    state, state_path, activity, stage, _loop_routings(loop_result), report=report
                )

        # A later reviewer decided one of the run's standing obligations can
        # wait no longer. The stage it said that in is finished either way --
        # promotion is not a verdict on this stage -- so the run stops here,
        # before entering the next one, and asks.
        promoted = state.promoted_deferred
        if promoted:
            return _stop_for_deferred(
                state,
                state_path,
                activity,
                promoted,
                reason=DeferredVerificationRequired.PROMOTED,
                stage_id=stage.stage_id,
                accepted=accepted,
                report=report,
            )

        # A plan-declared gate stands between this stage and the next: the
        # run stops before the next stage is created, and only an explicit
        # pass answer lets it continue. Reaching the gate satisfies nothing.
        if state.current_stage_index + 1 < total:
            upcoming = stages[state.current_stage_index + 1]
            gated = _owe_manifest_gates(
                state,
                state_path,
                activity,
                stage.stage_id,
                upcoming.gates_before,
                checkpoint=before_stage_checkpoint(upcoming.stage_id),
                report=report,
            )
            if gated:
                return _stop_for_deferred(
                    state,
                    state_path,
                    activity,
                    gated,
                    reason=DeferredVerificationRequired.BEFORE_STAGE,
                    stage_id=stage.stage_id,
                    accepted=accepted,
                    report=report,
                )

        if state.current_stage_index + 1 >= total:
            # The run's last stage is finished. For a slice of an intake that
            # is not the plan's end: obligations owed by plan completion are
            # the plan's, not this slice's, so they are carried to the plan
            # ledger and this slice completes. For the slice that is the
            # plan's end, every carried obligation is claimed here, so the
            # checkpoint below covers the whole plan.
            slice_ctx = slice_context(source)
            if slice_ctx is not None:
                pending_slices = incomplete_slices(slice_ctx)
                if pending_slices:
                    _carry_to_plan(state, state_path, activity, slice_ctx, sparring_dir, pending_slices, report=report)
                else:
                    _claim_from_plan(state, state_path, activity, slice_ctx, report=report)
            # NON-NEGOTIABLE: every executable stage is finished, and the
            # plan still may not report completion while a person owes it
            # verification. The run stops instead, with one checkpoint
            # covering everything that accumulated, and completes only once
            # those are answered -- without re-running anything that is
            # already accepted, because this loop skips accepted stages.
            # The manifest's closeout gates are minted after the carry
            # above, so they stay this run's: a slice does not complete
            # past its own closeout gate.
            _owe_manifest_gates(
                state,
                state_path,
                activity,
                stage.stage_id,
                tuple(getattr(source, "completion_gates", ())),
                checkpoint=CHECKPOINT_PLAN_COMPLETION,
                report=report,
            )
            owed = state.unresolved_deferred
            if owed:
                return _stop_for_deferred(
                    state,
                    state_path,
                    activity,
                    owed,
                    reason=DeferredVerificationRequired.PLAN_COMPLETION,
                    stage_id=stage.stage_id,
                    accepted=accepted,
                    report=report,
                )
            # Nothing is owed any more, so the run is not stopped on
            # anything: leaving a stale reason on a completed run would say
            # it finished while waiting.
            state.awaiting = None
            state.status = PlanRunStatus.COMPLETE
            state.save(state_path)
            if slice_ctx is not None:
                # The plan's end: what this run claimed is answered, and the
                # plan ledger says so, with the run that answered it.
                sync_plan_obligations(slice_ctx, state.deferred_human_checks, run=_RunOwner.of(state).key)
            activity.emit("plan.completed", summary=f"{total} stage(s) accepted")
            report(f"plan {state.plan}: complete; {total} stage(s) accepted")
            return PlanRunResult(
                status=PlanRunStatus.COMPLETE,
                plan=state.plan,
                run=_RunOwner.of(state).key,
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
    "RecordedRun",
    "describe_source",
    "find_runs",
    "legacy_run_key",
    "load_markdown_source",
    "load_plan_source",
    "parse_plan",
    "plan_digest",
    "plan_key",
    "plan_label",
    "plan_state_not_ignored_message",
    "record_human_evidence",
    "new_run_key",
    "render_brief",
    "resume_plan",
    "run_state_path",
    "start_plan",
]
