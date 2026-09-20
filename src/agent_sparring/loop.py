"""The unattended stage<->sparring loop.

Per the project plan's "Orchestrator target": the orchestrator is a small
router, not another workflow engine. This module is that router. It owns
the decision of *where control goes next* between one stage-agent turn
(:func:`agent_sparring.stage_agent.run_stage_agent`) and one sparring-agent
turn (:func:`agent_sparring.sparring_agent.run_sparring_agent`); the agents
themselves decide *what* needs doing.

    stage agent
        v
    sparring agent
        v
    SEND_BACK -> resume the SAME stage-agent session, then the SAME
                 sparring-agent session, and try again
    READY     -> if the reviewed candidate is already a commit: stop; ready
                 for later acceptance (Stage 6, not here). If it is still
                 sitting in the working tree: one bounded commit/push turn,
                 then review that exact commit (see "Finalization" below)
    NEEDS_YOU -> stop; surface the routing result for a human
    ESCALATE  -> stop; surface the routing result for a stronger/manual
                 sparring environment

Finalization
------------

READY is the reviewer saying "no unresolved implementation or human-gate
issue remains". It is not "there is a pushed commit", and for a stage whose
implementation is deliberately left uncommitted until a human verifies it
those two are not the same thing: the reviewed work is in the working tree
and HEAD is still the stage's base. Returning READY there hands the
acceptance gate a candidate that does not exist, and the gate correctly
refuses -- which used to leave the run stuck with no way forward that did
not involve a person committing the candidate by hand.

So READY is terminal only once the reviewed candidate is a commit. When it
is not, this loop routes one more ordinary cycle, with both halves narrowed
to exactly what is missing:

- the stage-agent turn is asked to commit and push that exact tree and
  nothing else (``finalize_only``; see :func:`~agent_sparring.stage_prompt.
  build_stage_prompt`);
- :mod:`agent_sparring.finalization` then compares the committed content
  against the tree the sparrer reviewed, path by path. Anything still
  uncommitted, or any committed content that is not the reviewed content,
  stops the loop -- the human's verification covered the reviewed tree and
  must not be carried forward onto different work;
- the sparring-agent turn that follows reviews the exact committed SHA, as
  every sparring turn does, and its verdict is the one that counts.

That is one extra cycle of the primitives this module already has: no new
routing action, no new stage status, no second workflow engine. It is
bounded at one finalization per loop invocation -- a second READY still
holding an uncommitted candidate is a refusal, not another turn.

``start_with="finalization"`` enters at that same cycle directly, for a
stage that is *already* stopped there: the sparrer recorded READY, nothing
has run since, and the reviewed candidate is still uncommitted. A run left
in that state before this existed has no other honest way forward --
entering at the stage agent would spend an unbounded implementation turn on
a tree a human has already verified, and entering at the sparrer would only
re-derive the READY that is already recorded. The caller identifies that
state from the recorded verdict (see :func:`agent_sparring.plan.resume_plan`);
this module does the same one cycle, with the same path-by-path check.

This module never launches a second top-level implementation or sparring
agent on its own initiative -- it only starts/resumes the exact stage/
sparring sessions handed to it via ``stage_adapter``/``sparring_adapter``,
mirroring the plan's orchestration-ownership principle: "only the
orchestrator starts/resumes the stage agent and sparring agent." A fresh
stage (no recorded session ids in ``state.json``) gets a fresh
implementation session and a fresh sparring session; the same stage reuses
whatever session ids are already recorded, and that reuse is entirely
:func:`~agent_sparring.stage_agent.run_stage_agent`'s and
:func:`~agent_sparring.sparring_agent.run_sparring_agent`'s own
responsibility (this module does not duplicate their start-vs-resume
logic).

Any provider/integrity/config error from either turn (a
:class:`~agent_sparring.stage_agent.StageAgentRunError` or
:class:`~agent_sparring.sparring_agent.SparringAgentRunError`) stops the
loop immediately, wrapped in :class:`LoopError`. This module never invents
recovery (retry, rollback, silently continuing) for such a failure -- that
would contradict the branch-guard/worktree-lock/read-only-integrity checks
those functions already perform.

A stage-agent turn that itself raised no exception but whose provider
reported ``is_error=true`` in its own machine-readable output (e.g.
``ClaudeCliAdapter``'s parsed ``StageAgentResult.is_error``) is treated the
same way: the loop stops with :class:`LoopError` before ever invoking the
sparring agent, rather than sending a provider-reported failed
implementation turn to the sparrer as though it had succeeded. This
mirrors the standalone ``run-stage`` CLI command, which already treats
``is_error=true`` as failure. The provider-issued
``implementation_session_id`` that ``run_stage_agent`` already recorded in
``state.json`` before returning is left untouched (not discarded), so a
later, deliberate retry can resume the same provider context; no automatic
retry/recovery is attempted here.

A small, configurable runaway limit (``max_send_back_cycles``) bounds how
many SEND_BACK verdicts this loop will act on before refusing to continue
further, raising :class:`LoopRunawayError`. This is deliberately not a
workflow state machine: it is one integer counter, compared against one
configured limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_sparring.deferred_gate import DeferredObligation
from agent_sparring.finalization import (
    FinalizationError,
    PendingFinalization,
    describe_refusal,
    pending_finalization,
    record_refusal,
    verify_finalized,
)
from agent_sparring.providers import SparringAgentAdapter, StageAgentAdapter
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.stage import Stage
from agent_sparring.stage_agent import StageAgentRunError, run_stage_agent

# A small, deliberately conservative default: enough headroom for a normal
# correction cycle or two without risking an unattended run burning through
# many provider turns unnoticed. Projects that need a different limit
# configure it explicitly (see the CLI's --max-send-back-cycles); there is
# no per-project config file loading here -- this module stays generic.
DEFAULT_MAX_SEND_BACK_CYCLES = 5

# How many bounded commit/push cycles one loop invocation will route (see
# the module docstring's "Finalization"). One: an already-reviewed candidate
# needs exactly one turn to become a commit, and a second READY that still
# has nothing to freeze is a condition to report, not to retry.
_MAX_FINALIZATION_CYCLES = 1

# Only these three routing actions are ever terminal for this loop; SEND_BACK
# always continues the loop instead (see run_unattended_loop).
_TERMINAL_ACTIONS = (RoutingAction.READY, RoutingAction.NEEDS_YOU, RoutingAction.ESCALATE)


class LoopError(RuntimeError):
    """Raised when the unattended loop must stop due to a provider,
    repository-integrity, or configuration error from either the stage
    agent or the sparring agent.

    Wraps the underlying :class:`~agent_sparring.stage_agent.
    StageAgentRunError` or :class:`~agent_sparring.sparring_agent.
    SparringAgentRunError` rather than swallowing it; no recovery is
    attempted by this module.
    """


class LoopRunawayError(LoopError):
    """Raised when the configured SEND_BACK runaway limit is exceeded
    without the sparring agent reaching READY, NEEDS_YOU, or ESCALATE."""


class FinalizationRefused(LoopError):
    """Raised when the bounded commit/push turn did not produce the exact
    candidate that was reviewed -- work left uncommitted, or committed
    content that differs from the reviewed tree.

    Nothing is rolled back, reset or re-tried: the working tree, both
    sessions and every recorded artifact are left exactly as the turn left
    them, and the account of what diverged is also written to the stage's
    notes.md so it outlives the terminal it was reported in. Carrying a
    human's manual verification forward onto content they did not verify is
    the one outcome this refusal exists to prevent.
    """


@dataclass(frozen=True)
class LoopCycleRecord:
    """One executed stage-agent-turn + sparring-agent-turn pair.

    ``stage_ran`` is false for the single cycle produced by
    ``start_with="sparring"``: that cycle is one sparring turn against the
    unchanged candidate and no implementation turn happened, so
    ``stage_resumed`` says nothing.

    ``finalization`` is true for the bounded commit/push cycle described in
    the module docstring: its stage turn was asked only to commit and push
    an already-reviewed tree, and the engine verified the committed content
    against that tree before the sparring turn ran.
    """

    stage_resumed: bool
    sparring_resumed: bool
    routing: RoutingResult
    stage_ran: bool = True
    finalization: bool = False


@dataclass(frozen=True)
class LoopResult:
    """The unattended loop's outcome: always one of the three terminal
    routing actions (SEND_BACK never appears here -- it always continues
    the loop instead of ending it).
    """

    outcome: RoutingAction
    routing: RoutingResult
    send_back_count: int
    cycles: tuple[LoopCycleRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in _TERMINAL_ACTIONS:
            raise LoopError(
                f"unattended loop outcome must be one of "
                f"{[a.value for a in _TERMINAL_ACTIONS]}, got {self.outcome!r}"
            )


def run_unattended_loop(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    stage_adapter: StageAgentAdapter,
    sparring_adapter: SparringAgentAdapter,
    *,
    expected_branch: str,
    max_send_back_cycles: int = DEFAULT_MAX_SEND_BACK_CYCLES,
    self_check: bool = False,
    start_with: str = "stage",
    pending_deferred: tuple[DeferredObligation, ...] = (),
) -> LoopResult:
    """Drive the stage<->sparring loop until a terminal routing action.

    ``pending_deferred`` is the managed run's ledger of human verification
    already owed, passed straight through to every sparring turn of this
    loop (see :func:`~agent_sparring.sparring_agent.run_sparring_agent`).
    This module neither reads nor interprets it: it is context for the
    reviewer's judgement, and the plan runner owns the ledger itself.

    Each cycle runs exactly one stage-agent turn (fresh session on the
    stage's first cycle, resumed on every later cycle -- entirely decided
    by :func:`~agent_sparring.stage_agent.run_stage_agent` from
    ``state.json``, not by this function) followed by exactly one
    sparring-agent turn (same fresh-vs-resume rule, via
    :func:`~agent_sparring.sparring_agent.run_sparring_agent`).

    ``start_with="sparring"`` skips the implementation turn of the *first*
    cycle only, going straight to the sparrer. That is what a human
    answering a NEEDS_YOU gate needs: the code did not change, the evidence
    did, and the question is whether the reviewer now accepts the same
    candidate. Starting the stage agent to deliver an answer would spend an
    implementation turn with nothing to implement and would move the
    candidate. If that sparring turn says SEND_BACK there *is* implementation
    work, and the loop continues normally from the stage agent; every later
    cycle is an ordinary full cycle regardless.

    ``start_with="finalization"`` makes that first cycle the bounded
    commit/push cycle, for a stage already stopped at exactly that point
    (see the module docstring). It is refused up front, before any provider
    turn, if the stage's candidate is in fact already committed -- there
    would be nothing to commit, and running some other turn instead is not
    this function's decision to make. Every later cycle is again an ordinary
    full cycle.

    On ``SEND_BACK``, the loop counts the cycle against
    ``max_send_back_cycles`` and, if still within the limit, continues:
    the *same* stage-agent session and the *same* sparring-agent session
    are resumed next cycle (never fresh sessions -- see both run_* functions
    for how the recorded session ids in ``state.json`` make that automatic).
    On ``NEEDS_YOU`` or ``ESCALATE`` the loop stops and returns immediately
    without resuming the stage agent again. ``READY`` does the same, with
    one condition: the reviewed candidate has to be a commit. If the
    reviewed work is still sitting in the working tree -- the ordinary
    shape of a stage left uncommitted until a human verified it -- the loop
    routes one bounded commit/push cycle first and returns the verdict of
    the sparring turn that reviews the resulting commit. See the module
    docstring's "Finalization" for why that belongs here and what bounds it.

    Raises :class:`LoopRunawayError` if the number of SEND_BACK verdicts
    exceeds ``max_send_back_cycles`` (raised before running another cycle,
    not after silently running one more turn than configured). Raises
    :class:`LoopError` if either the stage-agent or sparring-agent turn
    itself fails (branch guard, worktree lock, provider error, read-only
    integrity violation, session-identity mismatch, or unparseable
    verdict), or if the stage agent's own turn completed without raising
    but its provider reported ``is_error=true`` -- in that last case the
    sparring agent is never invoked for that cycle. Raises
    :class:`FinalizationRefused` (a :class:`LoopError`) if a bounded
    commit/push turn left reviewed work uncommitted or committed content
    that is not the content that was reviewed. This module never invents
    recovery for any of these; it stops cleanly and lets the caller decide
    what to do next.
    """

    if max_send_back_cycles < 1:
        raise LoopError(
            f"max_send_back_cycles must be at least 1, got {max_send_back_cycles!r}"
        )
    if start_with not in ("stage", "sparring", "finalization"):
        raise LoopError(
            f"start_with must be 'stage', 'sparring' or 'finalization', got {start_with!r}"
        )

    cycle_records: list[LoopCycleRecord] = []
    send_back_count = 0
    finalization_count = 0
    # Consumed by the first cycle only; every later cycle is a full cycle.
    skip_stage_turn = start_with == "sparring"
    # Set when a cycle ended READY over an uncommitted candidate: the
    # reviewed content, captured before the next stage turn touches
    # anything, and what that turn will be held to.
    pending: PendingFinalization | None = None
    finalization_note: str | None = None

    if start_with == "finalization":
        try:
            pending = pending_finalization(repo_root, stage)
        except FinalizationError as exc:
            raise LoopError(
                f"cannot finalize stage {stage.stage_id!r}: its candidate content could "
                f"not be read: {exc}"
            ) from exc
        if pending is None:
            # The candidate is already a commit, so there is nothing to
            # finalize. Refusing beats silently running something else: the
            # caller asked for one specific turn on a specific premise, and
            # the premise does not hold.
            raise LoopError(
                f"stage {stage.stage_id!r} was entered for finalization, but its working "
                "tree holds no candidate content outside a commit; there is nothing to "
                "commit. Resume it normally instead."
            )
        finalization_count += 1

    # Observational telemetry only (see agent_sparring.activity): mirrors
    # the routing decisions taken below, never influences them.
    activity = stage.activity_log().bind("loop")
    activity.emit("loop.started", summary="sparring first" if skip_stage_turn else None)

    while True:
        cycle = len(cycle_records) + 1
        finalizing = pending is not None
        # Only the first cycle of an evidence resume is the turn that
        # answers a human; every later cycle is ordinary. Recorded for the
        # captured prompt's turn kind and nothing else.
        evidence_first = skip_stage_turn
        if skip_stage_turn:
            skip_stage_turn = False
            stage_run = None
            activity.emit(
                "loop.sparring_first",
                cycle=cycle,
                summary="resuming the sparrer against the unchanged candidate",
            )
        else:
            try:
                stage_run = run_stage_agent(
                    stage,
                    sparring_dir,
                    repo_root,
                    stage_adapter,
                    expected_branch=expected_branch,
                    self_check=self_check,
                    finalize_only=finalizing,
                )
            except StageAgentRunError as exc:
                activity.emit("loop.stopped", cycle=cycle, summary="stage-agent turn failed")
                raise LoopError(f"stage-agent turn failed: {exc}") from exc

        if stage_run is not None and stage_run.result.is_error:
            # The provider's own machine-readable output reported this turn
            # as failed (mirrors the standalone `run-stage` CLI command,
            # which already treats is_error=true as failure). An
            # unattended loop must not hand a provider-reported failed
            # implementation turn to the sparrer as though it succeeded.
            # run_stage_agent has already recorded the provider-issued
            # implementation_session_id in state.json before returning --
            # that is left untouched here (not discarded) so a later,
            # deliberate retry can resume the same provider context. No
            # automatic retry/recovery is attempted.
            activity.emit(
                "loop.stopped", cycle=cycle, summary="stage-agent turn reported is_error"
            )
            raise LoopError(
                f"stage-agent turn for stage {stage.stage_id!r} reported "
                f"is_error=true (session {stage_run.result.session_id!r}); "
                "refusing to send a failed implementation turn to the "
                "sparrer"
            )

        if pending is not None:
            # The commit turn is held to the tree it was given, before the
            # sparrer is asked to review its result: a turn that rewrote the
            # work must not reach a review that could bless it, and a human's
            # verification of the earlier content must not be carried
            # forward onto the new content. Nothing is rolled back.
            try:
                outcome = verify_finalized(repo_root, stage, pending)
            except FinalizationError as exc:
                activity.emit(
                    "loop.stopped", cycle=cycle, summary="finalization could not be verified"
                )
                raise LoopError(
                    f"could not verify the finalization turn for stage "
                    f"{stage.stage_id!r}: {exc}"
                ) from exc
            if not outcome.preserved:
                report = describe_refusal(stage, pending, outcome)
                record_refusal(stage, report)
                activity.emit(
                    "loop.finalization_refused",
                    cycle=cycle,
                    summary="the commit turn did not preserve the reviewed candidate",
                )
                raise FinalizationRefused(report)
            activity.emit(
                "loop.finalized",
                cycle=cycle,
                sha=outcome.candidate_sha,
                summary="the reviewed candidate is committed unchanged",
            )
            finalization_note = (
                f"The candidate under review, {outcome.candidate_sha}, is the commit of "
                "the working tree you reviewed in your previous turn. The engine compared "
                "the committed content against that tree path by path, and it is "
                f"identical across all {len(outcome.committed.entries)} path(s) that make "
                "up this candidate; the only files that changed are this stage's own "
                "workflow artifacts, which are outside candidate identity. That turn was "
                "asked to commit and push, and nothing else. Any human evidence recorded "
                "for this stage therefore still describes exactly this content."
            )
            pending = None

        try:
            sparring_run = run_sparring_agent(
                stage,
                sparring_dir,
                repo_root,
                sparring_adapter,
                expected_branch=expected_branch,
                finalization=finalization_note,
                evidence_first=evidence_first,
                pending_deferred=pending_deferred,
            )
        except SparringAgentRunError as exc:
            activity.emit("loop.stopped", cycle=cycle, summary="sparring-agent turn failed")
            raise LoopError(f"sparring-agent turn failed: {exc}") from exc
        finalization_note = None

        cycle_records.append(
            LoopCycleRecord(
                stage_resumed=stage_run.resumed if stage_run is not None else False,
                sparring_resumed=sparring_run.resumed,
                routing=sparring_run.routing,
                stage_ran=stage_run is not None,
                finalization=finalizing,
            )
        )

        action = sparring_run.routing.action
        if action == RoutingAction.SEND_BACK:
            send_back_count += 1
            if send_back_count > max_send_back_cycles:
                activity.emit("loop.runaway", cycle=cycle, action=action.value)
                raise LoopRunawayError(
                    f"exceeded the configured runaway limit of "
                    f"{max_send_back_cycles} SEND_BACK cycle(s) for stage "
                    f"{stage.stage_id!r} without reaching READY, NEEDS_YOU, "
                    "or ESCALATE; stopping cleanly rather than continuing "
                    "unbounded"
                )
            activity.emit(
                "loop.send_back",
                cycle=cycle,
                action=action.value,
                summary="resuming the same stage and sparring sessions",
            )
            continue

        if action == RoutingAction.READY:
            # READY is terminal only once the reviewed candidate is a commit
            # the acceptance gate could freeze (see "Finalization" above).
            try:
                pending = pending_finalization(repo_root, stage)
            except FinalizationError as exc:
                activity.emit(
                    "loop.stopped", cycle=cycle, summary="candidate content unreadable"
                )
                raise LoopError(
                    f"stage {stage.stage_id!r} reached READY but its candidate content "
                    f"could not be read: {exc}"
                ) from exc
            if pending is not None:
                if finalization_count >= _MAX_FINALIZATION_CYCLES:
                    activity.emit(
                        "loop.stopped",
                        cycle=cycle,
                        summary="still uncommitted after a finalization cycle",
                    )
                    raise FinalizationRefused(
                        f"stage {stage.stage_id!r} reached READY again with its reviewed "
                        "candidate still uncommitted, after a bounded commit/push turn had "
                        "already been routed for it; refusing to route another. Nothing was "
                        "accepted. Unrepresented working-tree changes: "
                        + ", ".join(pending.uncommitted_paths)
                    )
                finalization_count += 1
                activity.emit(
                    "loop.finalization_required",
                    cycle=cycle,
                    action=action.value,
                    summary="the reviewed candidate is not committed yet",
                )
                continue

        activity.emit("loop.stopped", cycle=cycle, action=action.value)
        return LoopResult(
            outcome=action,
            routing=sparring_run.routing,
            send_back_count=send_back_count,
            cycles=tuple(cycle_records),
        )


__all__ = [
    "DEFAULT_MAX_SEND_BACK_CYCLES",
    "FinalizationRefused",
    "LoopCycleRecord",
    "LoopError",
    "LoopResult",
    "LoopRunawayError",
    "run_unattended_loop",
]
