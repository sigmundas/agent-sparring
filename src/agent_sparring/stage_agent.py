"""Stage-agent run orchestration.

Ties together the branch guard, the worktree implementation lock, bounded
prompt assembly, and the provider adapter for one stage-agent turn: fresh
session for a new stage, resume of the same session for a SEND_BACK
follow-up. This module owns start-vs-resume selection and session-id
bookkeeping in state.json, and regenerates handoff.md after a successful
turn. It does not implement any provider itself (see
:mod:`agent_sparring.providers`) and does not decide sparring routing.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agent_sparring.activity import ActivityEmitter
from agent_sparring.branch_guard import BranchGuardError, ensure_branch_for_unattended_run
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.git_context import GitContextError, resolve_commit
from agent_sparring.handoff import generate_handoff
from agent_sparring.providers import ProviderError, StageAgentAdapter, StageAgentResult
from agent_sparring.stage import Stage, StageState, StageStatus
from agent_sparring.prompt_capture import capture_prompt
from agent_sparring.stage_prompt import assemble_stage_prompt


class StageAgentRunError(RuntimeError):
    """Raised when a stage-agent run is refused or the provider fails."""


@dataclass(frozen=True)
class StageAgentRunResult:
    result: StageAgentResult
    resumed: bool
    branch: str
    prompt: str
    handoff: str


def _stage_emitter(stage: Stage, adapter: object) -> ActivityEmitter:
    """The observational emitter for this stage's implementation side.

    Telemetry only (see :mod:`agent_sparring.activity`): nothing below reads
    it back, and a failed write never raises. The provider id is taken from
    the adapter's own ``provider_id`` attribute when it has one; a fake or
    third-party adapter without it simply produces events with no provider
    field.
    """

    provider = getattr(adapter, "provider_id", None)
    return stage.activity_log().bind(
        "stage", provider=provider if isinstance(provider, str) else None
    )


@contextmanager
def _session_recorded_early(
    adapter: StageAgentAdapter, stage: Stage, state: StageState
) -> "Iterator[None]":
    """Persist a *fresh* turn's session id the moment the provider announces
    it, rather than only after the turn returns.

    A killed turn -- Ctrl-C, a closed terminal, a crashed machine -- used to
    leave ``implementation_session_id`` null even though the provider had
    long since issued one and the worktree was full of that turn's edits.
    The only record of the identity was the stage's ``session.observed``
    telemetry, which the orchestrator deliberately never reads back, so the
    next run had no choice but to start a second session over the first
    one's unfinished work. Recording it up front makes an interrupted turn
    resumable by the ordinary resume path, which is what the rest of this
    module already assumes.

    Only a fresh turn is recorded this way. On resume the identity is
    already known, and the post-turn check that the provider returned *that*
    session (below) stays the single authority on replacing it -- an early
    write would overwrite the recorded identity before that check could
    refuse.

    The hook is optional: an adapter without ``on_session_observed`` (a fake,
    a third-party adapter) is left exactly as it was, and the session id is
    recorded after the turn as before. Nothing here fails a turn: the write
    is the same ``stage.write_state`` the post-turn path performs.
    """

    if not hasattr(adapter, "on_session_observed"):
        yield
        return

    def record(session_id: str) -> None:
        if not session_id or state.implementation_session_id == session_id:
            return
        state.implementation_session_id = session_id
        stage.write_state(state)

    previous = adapter.on_session_observed  # type: ignore[attr-defined]
    adapter.on_session_observed = record  # type: ignore[attr-defined]
    try:
        yield
    finally:
        adapter.on_session_observed = previous  # type: ignore[attr-defined]


def run_stage_agent(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    adapter: StageAgentAdapter,
    *,
    expected_branch: str,
    self_check: bool = False,
    finalize_only: bool = False,
) -> StageAgentRunResult:
    """Start or resume the stage agent for one turn.

    ``expected_branch`` is required: an unattended run must always know
    which branch it is meant to modify — "any non-main branch" is not an
    acceptable stand-in for that. It is enforced by the branch guard and
    included in the stage prompt so the agent is told not to switch
    branches.

    ``self_check`` (default false) is forwarded to :func:`build_stage_prompt`
    verbatim; see that function for what it adds. It never becomes machine
    state here — it is not recorded in ``state.json`` and does not change
    any control flow in this function.

    ``finalize_only`` (default false) is forwarded the same way, and is
    equally not machine state: it narrows this turn's prompt to committing
    and pushing an already-reviewed candidate. Nothing in this function
    enforces that narrowing — :mod:`agent_sparring.loop` asks for such a
    turn and :mod:`agent_sparring.finalization` checks afterwards that the
    committed content is the content that was reviewed.

    Records the provider's own returned session id in state.json and
    regenerates handoff.md from the provider's result and actual git
    context (never invented test evidence). A fresh turn's session id is
    written as soon as the provider announces it rather than only when the
    turn returns (see :func:`_session_recorded_early`), so a turn that is
    killed mid-flight can still be resumed instead of being started over on
    top of its own unfinished work. On the stage's first run, the
    pre-turn HEAD is resolved and persisted to state.json as the stage's
    ``base_sha`` *before* the provider gets control, so a provider that
    modifies the repo and then fails does not lose the true stage baseline.
    That base is preserved across later SEND_BACK/resume turns and never
    re-resolved or moved forward, even after a failed turn — it represents
    where implementation for this stage began, so any changes from a
    failed/partial turn are correctly still included against it.

    Immediately after the provider returns, the branch is re-checked: a
    provider can run arbitrary git commands, so a successful-looking turn
    that actually left the worktree off ``expected_branch`` (e.g. on
    main/master) is treated as a failure, not silently accepted.

    Raises :class:`StageAgentRunError` if: the stage is already ACCEPTED
    (the acceptance gate is hard — an accepted stage is not ordinary
    WORKING state for an unattended implementation turn to mutate); the
    branch guard refuses the run
    (before or after the provider turn); the worktree is already locked by
    another live process; the provider itself fails; or, on resume, the
    provider returns a different session id than the one it was asked to
    resume (the recorded implementation session identity is never silently
    replaced).
    """

    if not expected_branch or not expected_branch.strip():
        raise StageAgentRunError(
            "expected_branch is required for a stage-agent run; an unattended "
            "run must know which branch it is meant to modify"
        )

    try:
        branch = ensure_branch_for_unattended_run(repo_root, expected_branch=expected_branch)
    except BranchGuardError as exc:
        raise StageAgentRunError(str(exc)) from exc

    try:
        with worktree_lock(repo_root):
            state = stage.read_state()

            # Acceptance is the hard gate (see agent_sparring.acceptance):
            # an ACCEPTED stage must not be treated as ordinary WORKING and
            # quietly mutated by another unattended implementation turn.
            # Only ACCEPTED is refused: a FROZEN stage may legitimately
            # receive one more correction turn, and the resulting HEAD move
            # is caught as a stale candidate at acceptance time rather than
            # needing a second guard here.
            if state.status is StageStatus.ACCEPTED:
                raise StageAgentRunError(
                    f"stage {stage.stage_id!r} is ACCEPTED at "
                    f"{state.candidate_sha}; refusing an unattended "
                    "implementation turn against an accepted stage. Further "
                    "work belongs to a new stage."
                )

            resume_id = state.implementation_session_id

            if state.base_sha is not None:
                base_sha = state.base_sha
            else:
                try:
                    base_sha = resolve_commit(repo_root, "HEAD", label="stage base")
                except GitContextError as exc:
                    raise StageAgentRunError(str(exc)) from exc
                # Persist the stage's baseline before the provider gets
                # control: if the provider modifies the repo and then fails,
                # the true stage baseline (where implementation began) must
                # not be lost.
                state.base_sha = base_sha
                stage.write_state(state)

            assembled = assemble_stage_prompt(
                stage,
                sparring_dir,
                resume=resume_id is not None,
                expected_branch=expected_branch,
                self_check=self_check,
                finalize_only=finalize_only,
            )
            prompt = assembled.text

            # Captured here, between assembly and the adapter call, so the
            # bytes on disk are the bytes the provider is about to receive.
            # capture_prompt never raises: a turn must not fail because its
            # record could not be written (see agent_sparring.prompt_capture).
            capture_prompt(stage.directory, assembled)

            # Observational telemetry only: emitted alongside the existing
            # control flow, never consulted by it. Failure summaries are
            # fixed phrases, not exception text (which can carry provider
            # stdout).
            activity = _stage_emitter(stage, adapter)
            resumed = resume_id is not None
            activity.emit("turn.started", resumed=resumed, session_id=resume_id)
            # Measured with a monotonic clock so a system clock adjustment
            # mid-turn cannot produce a negative or absurd duration. This is
            # the engine's own wall-clock observation of the provider call,
            # not something the provider reported, so unlike the token
            # fields it is present on every outcome including failure.
            started_at = time.monotonic()

            def _elapsed_ms() -> int:
                return int((time.monotonic() - started_at) * 1000)

            try:
                if resume_id:
                    result = adapter.resume(resume_id, prompt)
                else:
                    with _session_recorded_early(adapter, stage, state):
                        result = adapter.start(prompt)
            except ProviderError as exc:
                activity.emit(
                    "turn.failed",
                    resumed=resumed,
                    duration_ms=_elapsed_ms(),
                    summary="provider error",
                )
                raise StageAgentRunError(str(exc)) from exc

            if resume_id and result.session_id != resume_id:
                activity.emit(
                    "turn.failed",
                    resumed=resumed,
                    duration_ms=_elapsed_ms(),
                    summary="session identity mismatch",
                )
                raise StageAgentRunError(
                    f"provider was asked to resume session {resume_id!r} but "
                    f"returned session {result.session_id!r}; refusing to "
                    "silently replace the recorded implementation session identity"
                )

            try:
                ensure_branch_for_unattended_run(repo_root, expected_branch=expected_branch)
            except BranchGuardError as exc:
                activity.emit(
                    "turn.failed",
                    resumed=resumed,
                    session_id=result.session_id,
                    duration_ms=_elapsed_ms(),
                    summary="worktree left the expected branch",
                )
                raise StageAgentRunError(
                    f"provider turn left the worktree off the expected branch: {exc}"
                ) from exc

            # state.base_sha was already persisted above (before the provider
            # ran) if this was the first run; it is never moved forward here.
            state.implementation_session_id = result.session_id
            stage.write_state(state)
            activity.emit(
                "turn.finished",
                resumed=resumed,
                session_id=result.session_id,
                duration_ms=_elapsed_ms(),
                summary="provider reported is_error=true" if result.is_error else None,
            )

            try:
                handoff = generate_handoff(
                    stage,
                    repo_root,
                    stage_goal=stage.read_brief(),
                    claims=result.text,
                    check_pushed=False,
                    base_sha=base_sha,
                )
            except GitContextError as exc:
                raise StageAgentRunError(str(exc)) from exc
            activity.emit("handoff.ready", session_id=result.session_id)
    except WorktreeLockError as exc:
        raise StageAgentRunError(str(exc)) from exc

    return StageAgentRunResult(
        result=result,
        resumed=resume_id is not None,
        branch=branch,
        prompt=prompt,
        handoff=handoff,
    )


__all__ = ["StageAgentRunError", "StageAgentRunResult", "run_stage_agent"]
