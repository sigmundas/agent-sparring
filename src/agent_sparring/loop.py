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
    READY     -> stop; ready for later acceptance (Stage 6, not here)
    NEEDS_YOU -> stop; surface the routing result for a human
    ESCALATE  -> stop; surface the routing result for a stronger/manual
                 sparring environment

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


@dataclass(frozen=True)
class LoopCycleRecord:
    """One executed stage-agent-turn + sparring-agent-turn pair."""

    stage_resumed: bool
    sparring_resumed: bool
    routing: RoutingResult


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
) -> LoopResult:
    """Drive the stage<->sparring loop until a terminal routing action.

    Each cycle runs exactly one stage-agent turn (fresh session on the
    stage's first cycle, resumed on every later cycle -- entirely decided
    by :func:`~agent_sparring.stage_agent.run_stage_agent` from
    ``state.json``, not by this function) followed by exactly one
    sparring-agent turn (same fresh-vs-resume rule, via
    :func:`~agent_sparring.sparring_agent.run_sparring_agent`).

    On ``SEND_BACK``, the loop counts the cycle against
    ``max_send_back_cycles`` and, if still within the limit, continues:
    the *same* stage-agent session and the *same* sparring-agent session
    are resumed next cycle (never fresh sessions -- see both run_* functions
    for how the recorded session ids in ``state.json`` make that automatic).
    On ``READY``, ``NEEDS_YOU``, or ``ESCALATE``, the loop stops and returns
    immediately without resuming the stage agent again.

    Raises :class:`LoopRunawayError` if the number of SEND_BACK verdicts
    exceeds ``max_send_back_cycles`` (raised before running another cycle,
    not after silently running one more turn than configured). Raises
    :class:`LoopError` if either the stage-agent or sparring-agent turn
    itself fails (branch guard, worktree lock, provider error, read-only
    integrity violation, session-identity mismatch, or unparseable
    verdict), or if the stage agent's own turn completed without raising
    but its provider reported ``is_error=true`` -- in that last case the
    sparring agent is never invoked for that cycle. This module never
    invents recovery for any of these; it stops cleanly and lets the
    caller decide what to do next.
    """

    if max_send_back_cycles < 1:
        raise LoopError(
            f"max_send_back_cycles must be at least 1, got {max_send_back_cycles!r}"
        )

    cycle_records: list[LoopCycleRecord] = []
    send_back_count = 0

    while True:
        try:
            stage_run = run_stage_agent(
                stage,
                sparring_dir,
                repo_root,
                stage_adapter,
                expected_branch=expected_branch,
                self_check=self_check,
            )
        except StageAgentRunError as exc:
            raise LoopError(f"stage-agent turn failed: {exc}") from exc

        if stage_run.result.is_error:
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
            raise LoopError(
                f"stage-agent turn for stage {stage.stage_id!r} reported "
                f"is_error=true (session {stage_run.result.session_id!r}); "
                "refusing to send a failed implementation turn to the "
                "sparrer"
            )

        try:
            sparring_run = run_sparring_agent(
                stage,
                sparring_dir,
                repo_root,
                sparring_adapter,
                expected_branch=expected_branch,
            )
        except SparringAgentRunError as exc:
            raise LoopError(f"sparring-agent turn failed: {exc}") from exc

        cycle_records.append(
            LoopCycleRecord(
                stage_resumed=stage_run.resumed,
                sparring_resumed=sparring_run.resumed,
                routing=sparring_run.routing,
            )
        )

        action = sparring_run.routing.action
        if action == RoutingAction.SEND_BACK:
            send_back_count += 1
            if send_back_count > max_send_back_cycles:
                raise LoopRunawayError(
                    f"exceeded the configured runaway limit of "
                    f"{max_send_back_cycles} SEND_BACK cycle(s) for stage "
                    f"{stage.stage_id!r} without reaching READY, NEEDS_YOU, "
                    "or ESCALATE; stopping cleanly rather than continuing "
                    "unbounded"
                )
            continue

        return LoopResult(
            outcome=action,
            routing=sparring_run.routing,
            send_back_count=send_back_count,
            cycles=tuple(cycle_records),
        )


__all__ = [
    "DEFAULT_MAX_SEND_BACK_CYCLES",
    "LoopCycleRecord",
    "LoopError",
    "LoopResult",
    "LoopRunawayError",
    "run_unattended_loop",
]
