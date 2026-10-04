"""Provider session generations for one stage.

A role (``stage`` or ``sparring``) talks to one provider conversation at a
time. Ordinarily that is one conversation for the whole stage: SEND_BACK and
resume reuse it, and its agent configuration is locked to what was pinned at
its first turn. :func:`start_fresh_session` is the single way to discard a
conversation and continue the same stage in a new one -- a new *generation*
-- whose configuration is resolved anew at its first turn.

Generations are recorded in ``state.json`` as ``sessions: {role: [...]}``.
That list is absent until the first fresh session: before then the recorded
``implementation_session_id`` / ``sparring_session_id`` and ``agents[role]``
*are* generation 1, and :func:`generations` presents them that way. The flat
fields always hold the *current* generation's values, so every existing
reader keeps working unchanged.

A fresh session changes nothing but session bookkeeping: not the candidate,
base, branch, mode, brief, handoff, sparring.md, notes, deferred ledger, plan,
siblings, freeze/accept state, prompts, activity, nor ``next_turn``. Any
future automatic rollover must call :func:`start_fresh_session` with
``reason="auto:<cause>"``; none exists today.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.stage import (
    PinnedAgent,
    SessionGeneration,
    Stage,
    StageState,
    StageStatus,
)

ROLE_STAGE = "stage"
ROLE_SPARRING = "sparring"
ROLES = (ROLE_STAGE, ROLE_SPARRING)
# The activity actor each role speaks as (see agent_sparring.activity).
ROLE_ACTORS = {ROLE_STAGE: "stage", ROLE_SPARRING: "sparrer"}


class SessionError(RuntimeError):
    """Raised when a fresh session cannot be started."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise SessionError(f"unknown session role {role!r}; expected one of {list(ROLES)}")


def current_session_id(state: StageState, role: str) -> str | None:
    _validate_role(role)
    if role == ROLE_STAGE:
        return state.implementation_session_id
    return state.sparring_session_id


def _set_current_session_id(state: StageState, role: str, session_id: str | None) -> None:
    if role == ROLE_STAGE:
        state.implementation_session_id = session_id
    else:
        state.sparring_session_id = session_id


def generations(state: StageState, role: str) -> list[SessionGeneration]:
    """This role's generations, oldest first.

    State written before generations existed is presented as one implicit
    generation 1 when it records a session id or a pin for the role, and as
    no generations at all when nothing has run for it. The returned list is
    a view: mutating it does not change ``state`` unless it came from
    ``state.sessions`` itself.
    """

    _validate_role(role)
    recorded = state.sessions.get(role)
    if recorded:
        return recorded
    session_id = current_session_id(state, role)
    pin = (state.agents or {}).get(role)
    if session_id is None and pin is None:
        return []
    return [SessionGeneration(generation=1, session_id=session_id, agent=pin)]


def is_fresh(state: StageState, role: str) -> bool:
    """Is this role's next turn the first turn of a fresh generation -- an
    earlier generation exists, but the current one has no session yet?"""

    recorded = state.sessions.get(role) or []
    return len(recorded) > 1 and recorded[-1].session_id is None and recorded[-1].ended_at is None


def _current_generation(state: StageState, role: str) -> SessionGeneration:
    """The role's current generation, materializing the list first.

    Called *before* the flat fields are changed, so state written before
    generations existed (a recorded session id or pin, no list) becomes its
    generation 1 -- with ``started_at`` unknown, because nothing recorded
    it -- while a role nothing has run for gets a generation 1 starting now.
    """

    recorded = state.sessions.get(role)
    if not recorded:
        recorded = generations(state, role) or [
            SessionGeneration(generation=1, started_at=_now(), start_reason="initial")
        ]
        state.sessions[role] = recorded
    return recorded[-1]


def record_session_id(state: StageState, role: str, session_id: str) -> None:
    """Record ``session_id`` as the role's current session: on the current
    generation and on the flat field existing readers use. The caller
    writes ``state``."""

    _validate_role(role)
    _current_generation(state, role).session_id = session_id
    _set_current_session_id(state, role, session_id)


def record_pin(state: StageState, role: str, pin: PinnedAgent) -> None:
    """Record the configuration pinned for the role's current generation.
    The caller sets ``agents[role]`` and writes ``state``."""

    _validate_role(role)
    current = _current_generation(state, role)
    if current.agent is None:
        current.agent = pin


def pin_pending_generation(state: StageState, role: str, adapter: object) -> bool:
    """Pin a pending fresh generation's configuration at its first turn.

    The loop adapters are built before either role runs, so the
    configuration they were built with for a pending fresh generation is
    carried on the adapter (``pending_pin``) instead of being written when
    it was resolved. It becomes the generation's pin only here, immediately
    before that generation's first provider turn. Returns whether ``state``
    changed (the caller writes it). An adapter without ``pending_pin`` -- a
    fake, ``run-stage`` / ``run-sparring``, which pin at once -- changes
    nothing.
    """

    pin = getattr(adapter, "pending_pin", None)
    if not isinstance(pin, PinnedAgent) or not is_fresh(state, role):
        return False
    if (state.agents or {}).get(role) is not None:
        return False
    record_pin(state, role, pin)
    state.agents = {**(state.agents or {}), role: pin}
    return True


def start_fresh_session(
    stage: Stage,
    role: str,
    reason: str,
    *,
    repo_root: Path,
) -> SessionGeneration:
    """Close ``role``'s current conversation and open a pending fresh one.

    Under the worktree lock: materializes the generation list if this is the
    first fresh session, closes the current generation (``ended_at``,
    ``end_reason``), clears the role's current session id and its pinned
    agent so the next turn resolves configuration anew, and appends a
    pending generation with ``start_reason = "fresh:<reason>"``. Nothing
    else in the stage is touched -- in particular not ``next_turn``.

    Refuses an ACCEPTED stage (nothing will run for it again), a role with no
    conversation to replace, and a role whose fresh generation is still
    pending (starting another would discard nothing).
    """

    _validate_role(role)
    if not reason or not reason.strip():
        raise SessionError("a fresh session needs a reason")
    reason = reason.strip()
    try:
        with worktree_lock(repo_root):
            state = stage.read_state()
            if state.status is StageStatus.ACCEPTED:
                raise SessionError(
                    f"stage {stage.stage_id!r} is ACCEPTED; nothing will run for it again, so "
                    "there is no session to replace"
                )
            existing = generations(state, role)
            if not existing:
                raise SessionError(
                    f"stage {stage.stage_id!r} has no {role} session yet; its first turn "
                    "already starts a new conversation"
                )
            if existing[-1].session_id is None and not is_fresh(state, role):
                raise SessionError(
                    f"stage {stage.stage_id!r} has no {role} conversation yet (its configuration "
                    "is pinned, but no provider session was started); there is nothing to "
                    "replace"
                )
            if is_fresh(state, role):
                raise SessionError(
                    f"stage {stage.stage_id!r} already has a fresh {role} session pending "
                    f"(generation {existing[-1].generation}); it starts at the next {role} turn"
                )
            # Materialize: from here on the list is the record.
            recorded = state.sessions.setdefault(role, list(existing))
            now = _now()
            current = recorded[-1]
            current.ended_at = now
            current.end_reason = f"fresh:{reason}"
            pending = SessionGeneration(
                generation=current.generation + 1,
                session_id=None,
                agent=None,
                started_at=now,
                start_reason=f"fresh:{reason}",
            )
            recorded.append(pending)
            _set_current_session_id(state, role, None)
            if state.agents and role in state.agents:
                agents = dict(state.agents)
                del agents[role]
                state.agents = agents or None
            stage.write_state(state)
    except WorktreeLockError as exc:
        raise SessionError(f"cannot start a fresh {role} session: {exc}") from exc
    # Observational only; `sparring usage` uses it to draw the generation
    # boundary in its report. Nothing in orchestration reads it.
    stage.activity_log().bind(ROLE_ACTORS[role]).emit(
        "session.fresh", role=role, summary=pending.start_reason
    )
    return pending


__all__ = [
    "pin_pending_generation",
    "ROLES",
    "ROLE_SPARRING",
    "ROLE_STAGE",
    "SessionError",
    "current_session_id",
    "generations",
    "is_fresh",
    "record_pin",
    "record_session_id",
    "start_fresh_session",
]
