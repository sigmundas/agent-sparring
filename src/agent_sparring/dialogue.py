"""One turn of the human's read-only side conversation with the reviewer.

A NEEDS_YOU gate asks a person for judgement, and until now it could do so
without giving them the material to exercise it: the reviewer's evidence and
reasoning lived in its own provider thread, where the gate could cite it but
nobody could read it. The only recourse was to paste fragments into a
separate chat, which throws away the context this run already holds. This
module is the path back into that thread.

**It resumes the real review thread.** ``state.sparring_session_id`` is the
reviewer's own conversation, so it can quote the evidence it actually used
rather than reconstructing a plausible rationale from artifacts. That is the
entire point, and it has a price worth stating plainly rather than
discovering:

- the exchange becomes part of the review thread, so a later sparring turn
  has seen it. A person can therefore argue a reviewer out of a finding.
  That is wanted -- a reviewer that will not reconsider when shown something
  it missed is not much of a reviewer -- but it does narrow the
  independence the sparring role otherwise has, and it is the reason the
  prompt (:mod:`agent_sparring.dialogue_prompt`) is explicit that this turn
  decides nothing;
- it consumes the reviewer's context window, which is finite and shared with
  the review itself. ``sparring usage`` reports what each turn cost, so a
  conversation that is crowding out the review is visible rather than
  inferred.

**It changes no state.** Not ``state.json``, not ``sparring.md``, not
``handoff.md``, not the plan run. A recorded verdict changes only when a
real sparring turn writes one. The only thing this module writes is
``dialogue.jsonl``, which is provenance for a conversation that would
otherwise leave no trace outside the provider's thread, and the captured
prompt every turn writes.

**It asks for prose, structurally.** The turn goes through the adapter's
``converse`` rather than ``resume``, which runs the same session without the
verdict output schema. That distinction is not a nicety: a provider handed
an output schema cannot answer outside it no matter what the prompt says,
so a dialogue on the ``resume`` path came back -- verified live -- as a
verdict envelope with the answer stuffed into ``findings``. The prompt asks
for prose as well, and the reply is still never parsed, but the schema is
what actually decides the shape.

**It proves read-only rather than trusting it**, with the same before/after
repository fingerprint :mod:`agent_sparring.sparring_agent` takes
(:func:`~agent_sparring.sparring_agent.repo_fingerprint`, shared rather than
reimplemented). The Codex adapter's OS-level read-only sandbox already makes
a write fail, but this module does not depend on which provider is
configured.

The turn holds the worktree lock for its duration, non-blocking. A dialogue
is short and read-only, so the lock is not there to protect a write of its
own; it is there because a reviewer reading a tree that a stage agent is
halfway through editing would answer questions about a state that never
existed. A turn already in flight therefore gets a refusal that says so,
not a queue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_sparring.activity import ActivityEmitter
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.dialogue_prompt import assemble_dialogue_prompt, find_check
from agent_sparring.git_context import GitContextError
from agent_sparring.human_gate import HumanCheck
from agent_sparring.prompt_capture import capture_prompt
from agent_sparring.providers import ProviderError, SparringAgentAdapter
from agent_sparring.sparring_agent import repo_fingerprint
from agent_sparring.sparring_exchange import recorded_gate
from agent_sparring.stage import Stage, StageError


class DialogueError(RuntimeError):
    """Raised when a dialogue turn is refused or the provider fails."""


@dataclass(frozen=True)
class DialogueTurn:
    """One completed exchange, as the caller and the transcript see it."""

    question: str
    answer: str
    session_id: str
    check_id: str | None
    gate_instance: str | None
    duration_ms: int
    prompt: str

    def as_record(self, *, when: str) -> dict[str, object]:
        return {
            "ts": when,
            "session_id": self.session_id,
            "check_id": self.check_id,
            "gate_instance": self.gate_instance,
            "question": self.question,
            "answer": self.answer,
            "duration_ms": self.duration_ms,
        }


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def resolve_check(stage: Stage, check_id: str | None) -> HumanCheck | None:
    """The gate check ``check_id`` names, or raise :class:`DialogueError`.

    Refusing an unknown id, and listing the ids that do exist, matters more
    than it looks: the alternative is a conversation that silently drops the
    check and answers a general question about the review, which reads like
    a reviewer ignoring what was asked.
    """

    if check_id is None:
        return None
    try:
        sparring_text = stage.read_sparring()
    except StageError as exc:
        raise DialogueError(
            f"--check-id {check_id!r} was given, but stage {stage.stage_id!r} has no "
            f"recorded sparring verdict to take a check from: {exc}"
        ) from exc

    gate = recorded_gate(sparring_text)
    check = find_check(gate, check_id)
    if check is not None:
        return check

    if gate is None:
        raise DialogueError(
            f"--check-id {check_id!r} was given, but the recorded verdict for stage "
            f"{stage.stage_id!r} carries no human gate; ask without --check-id"
        )
    known = ", ".join(repr(item.id) for item in gate.checks)
    raise DialogueError(
        f"unknown check id {check_id!r} for stage {stage.stage_id!r}; "
        f"the recorded gate asks: {known}"
    )


def run_dialogue_turn(
    stage: Stage,
    repo_root: Path,
    adapter: SparringAgentAdapter,
    *,
    message: str,
    check_id: str | None = None,
    gate_instance: str | None = None,
    expected_branch: str | None = None,
    activity: ActivityEmitter | None = None,
) -> DialogueTurn:
    """Ask the stage's reviewer one question and record the exchange."""

    if not message.strip():
        raise DialogueError("a dialogue turn needs a question; --message was empty")

    check = resolve_check(stage, check_id)

    try:
        with worktree_lock(repo_root):
            return _run_locked(
                stage,
                repo_root,
                adapter,
                message=message,
                check=check,
                check_id=check_id,
                gate_instance=gate_instance,
                expected_branch=expected_branch,
                activity=activity,
            )
    except WorktreeLockError as exc:
        raise DialogueError(
            f"{exc}. A provider turn is running in this worktree; asking the "
            "reviewer now would read a tree that is being edited. Wait for the "
            "turn to finish, or stop the run, and ask again"
        ) from exc


def _run_locked(
    stage: Stage,
    repo_root: Path,
    adapter: SparringAgentAdapter,
    *,
    message: str,
    check: HumanCheck | None,
    check_id: str | None,
    gate_instance: str | None,
    expected_branch: str | None,
    activity: ActivityEmitter | None,
) -> DialogueTurn:
    state = stage.read_state()
    session_id = state.sparring_session_id
    if not session_id:
        raise DialogueError(
            f"stage {stage.stage_id!r} has no recorded sparring session, so there is "
            "no reviewer conversation to continue. Run the sparrer at least once "
            "first; this command deliberately never starts one, because a fresh "
            "session would be a different reviewer reconstructing a review rather "
            "than the one that wrote the verdict"
        )

    assembled = assemble_dialogue_prompt(
        stage, message=message, check=check, expected_branch=expected_branch
    )
    prompt = assembled.text
    # Captured between assembly and the call, as every other turn does, so
    # the bytes on disk are the bytes the provider received. Never fails a
    # turn (see agent_sparring.prompt_capture).
    capture_prompt(stage.directory, assembled)

    try:
        before = repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise DialogueError(str(exc)) from exc

    if activity is not None:
        activity.emit("dialogue.started", session_id=session_id)

    started_at = time.monotonic()
    provider_error: ProviderError | None = None
    result = None
    try:
        result = adapter.converse(session_id, prompt)
    except ProviderError as exc:
        provider_error = exc
    duration_ms = int((time.monotonic() - started_at) * 1000)

    # Checked after every attempted turn, successful or not, for the same
    # reason the sparring turn checks it: a provider that failed may have
    # written first, and that is the more important fact to report.
    try:
        after = repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise DialogueError(str(exc)) from exc

    if after != before:
        detail = (
            "the reviewer modified the repository during a read-only conversation "
            f"(branch {before[0]!r} -> {after[0]!r}, HEAD {before[1]} -> {after[1]}, "
            f"dirty paths {before[2]!r} -> {after[2]!r})"
        )
        if provider_error is not None:
            detail += f"; it also failed: {provider_error}"
        _fail(activity, duration_ms, "read-only contract violated")
        raise DialogueError(detail)

    if provider_error is not None:
        _fail(activity, duration_ms, "provider error")
        raise DialogueError(str(provider_error))

    assert result is not None  # provider_error is None, so the call succeeded

    if result.session_id != session_id:
        # The recorded session id is not this command's to move. A sparring
        # turn refuses the same mismatch; here there is no write to refuse,
        # so the point is that the answer did not come from the reviewer
        # whose verdict the person is asking about.
        _fail(activity, duration_ms, "session identity mismatch")
        raise DialogueError(
            f"asked to continue reviewer session {session_id!r} but the provider "
            f"answered as session {result.session_id!r}; discarding the reply "
            "rather than presenting another conversation's answer as this "
            "reviewer's. The recorded session id is unchanged"
        )

    turn = DialogueTurn(
        question=message.strip(),
        # Verbatim, never parsed. A reviewer that answers in JSON out of
        # habit has written a badly formatted answer, not a verdict, and
        # turning it into one here is exactly the confusion to avoid.
        answer=result.text,
        session_id=result.session_id,
        check_id=check_id,
        gate_instance=gate_instance,
        duration_ms=duration_ms,
        prompt=prompt,
    )
    stage.append_dialogue(turn.as_record(when=_utc_now_iso()))

    if activity is not None:
        activity.emit(
            "dialogue.finished", session_id=result.session_id, duration_ms=duration_ms
        )
    return turn


def _fail(activity: ActivityEmitter | None, duration_ms: int, summary: str) -> None:
    if activity is not None:
        activity.emit("dialogue.failed", duration_ms=duration_ms, summary=summary)


__all__ = ["DialogueError", "DialogueTurn", "resolve_check", "run_dialogue_turn"]
