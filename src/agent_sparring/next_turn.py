"""Engine authority for whose turn it is in a stage.

``state.json``'s ``next_turn`` (``stage`` | ``sparring`` | ``finalization``)
is written by :mod:`agent_sparring.loop` only after the preceding
authoritative operation fully succeeded and its state was persisted:

- stage start and a recorded SEND_BACK -> ``stage``;
- a successful implementation turn, once its result, handoff and session are
  recorded -> ``sparring``, with ``next_turn_candidate`` pinning the exact
  candidate (:func:`capture_candidate`);
- READY over an uncommitted candidate -> ``finalization``.

A failed provider turn never advances it, and NEEDS_YOU / ESCALATE leave it
where it is: the recorded gate already says what happens next.

Before a reviewer starts on a ``sparring`` marker the loop calls
:func:`verify_candidate`; any drift in HEAD, uncommitted content or sibling
HEADs is refused rather than routed to another actor.

State written before the marker existed is reconstructed once by
:func:`derive_next_turn` from engine-owned records only (prompt-capture
index, handoff.md, sparring.md's routing block, the current repository) --
never activity.jsonl or agent prose -- and recorded with provenance so it is
never derived again.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agent_sparring.acceptance import sibling_root
from agent_sparring.finalization import (
    FinalizationError,
    _uncommitted_content_paths,
    read_worktree_content,
)
from agent_sparring.git_context import GitContextError, resolve_commit
from agent_sparring.handoff import _previous_sparring_section, extract_section
from agent_sparring.prompt_capture import INDEX_FILENAME, prompts_dir
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring.stage import (
    NEXT_TURN_SPARRING,
    NEXT_TURN_STAGE,
    SiblingPin,
    Stage,
    StageState,
    TurnCandidate,
)


class NextTurnError(RuntimeError):
    """The recorded or derivable next turn cannot be honoured."""


class AmbiguousNextTurn(NextTurnError):
    """Legacy state does not say unambiguously whose turn it is; the caller
    must choose explicitly (``next_turn`` = ``stage`` | ``sparring``)."""


def capture_candidate(repo_root: Path, stage: Stage, state: StageState) -> TurnCandidate:
    """The current candidate identity: HEAD, whether all candidate content is
    committed, the content digest, and every declared sibling's HEAD."""

    try:
        head = resolve_commit(repo_root, "HEAD", label="HEAD")
        uncommitted = _uncommitted_content_paths(repo_root, stage)
        digest = read_worktree_content(repo_root, stage).digest
    except (GitContextError, FinalizationError) as exc:
        raise NextTurnError(f"could not read the candidate identity: {exc}") from exc
    pins = []
    for repository in state.repositories:
        try:
            sibling_head: str | None = resolve_commit(
                sibling_root(repo_root, repository), "HEAD", label=f"sibling {repository.name}"
            )
        except GitContextError:
            sibling_head = None
        pins.append(SiblingPin(name=repository.name, head_sha=sibling_head))
    return TurnCandidate(
        head_sha=head,
        kind="worktree" if uncommitted else "commit",
        content_digest=digest,
        repositories=tuple(pins),
    )


def record_next_turn(
    stage: Stage,
    next_turn: str,
    *,
    candidate: TurnCandidate | None = None,
    source: str = "engine",
) -> None:
    """Persist the marker, re-reading state.json so only these three fields
    change."""

    state = stage.read_state()
    state.next_turn = next_turn
    state.next_turn_candidate = candidate if next_turn == NEXT_TURN_SPARRING else None
    state.next_turn_source = source
    stage.write_state(state)


def verify_candidate(repo_root: Path, stage: Stage, state: StageState) -> None:
    """Refuse unless the repository still holds ``next_turn_candidate``."""

    expected = state.next_turn_candidate
    if expected is None:
        raise NextTurnError(
            f"stage {stage.stage_id!r} records next_turn=sparring without the candidate it "
            "refers to; refusing to start the reviewer on an unidentified candidate"
        )
    current = capture_candidate(repo_root, stage, state)
    if current != expected:
        raise NextTurnError(
            f"stage {stage.stage_id!r} is waiting for review of {expected.describe()}, but the "
            f"repository now holds {current.describe()}. Refusing to start the reviewer on a "
            "candidate the implementation turn did not produce, and refusing to run any other "
            "turn in its place. Restore the recorded candidate, or deliberately choose how to "
            "continue (for example 'sparring reset-stage')."
        )


@dataclass(frozen=True)
class _IndexOrder:
    last_stage: int | None
    last_sparrer: int | None


def _index_order(stage: Stage) -> _IndexOrder:
    last_stage = last_sparrer = None
    path = prompts_dir(stage.directory) / INDEX_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("seq"), int):
            continue
        if entry.get("role") == "stage":
            last_stage = entry["seq"]
        elif entry.get("role") == "sparrer":
            last_sparrer = entry["seq"]
    return _IndexOrder(last_stage=last_stage, last_sparrer=last_sparrer)


_CANDIDATE_RE = re.compile(r"^- Candidate commit: `([0-9a-f]{40})`", re.MULTILINE)


@dataclass(frozen=True)
class _Handoff:
    candidate_sha: str
    dirty_paths: frozenset[str]
    previous_sparring: str


def _read_handoff(stage: Stage) -> _Handoff | None:
    """The engine-written handoff's recorded candidate, or ``None`` for the
    template (no implementation turn has completed)."""

    try:
        text = stage.read_handoff()
    except Exception:  # noqa: BLE001 -- unreadable means "nothing recorded"
        return None
    match = _CANDIDATE_RE.search(text)
    if match is None:
        return None
    status = extract_section(text, "### Working tree status") or ""
    # The last section of the handoff, and it embeds level-2 headings of its
    # own, so it runs to the end of the file rather than to the next heading.
    marker = "\n## Previous unresolved sparring findings\n"
    previous = text.split(marker, 1)[1] if marker in text else ""
    return _Handoff(
        candidate_sha=match.group(1),
        dirty_paths=frozenset(
            line[2:].strip() for line in status.splitlines() if line.startswith("- ")
        ),
        previous_sparring=previous.strip(),
    )


def derive_next_turn(repo_root: Path, stage: Stage, state: StageState) -> str | None:
    """Reconstruct the marker for state written before it existed.

    Returns ``stage`` or ``sparring``, or ``None`` when the recorded verdict
    is a gate (READY, NEEDS_YOU, ESCALATE) with no later implementation turn
    -- existing gate handling already decides those. Raises
    :class:`AmbiguousNextTurn`, explaining what was found, for anything
    else.
    """

    outcome = read_recorded_outcome(stage)
    if outcome is None and state.implementation_session_id is None and not state.sessions:
        # Nothing has run for this stage: its first turn is simply next.
        return None
    order = _index_order(stage)
    handoff = _read_handoff(stage)

    # A completed implementation turn is later than the last verdict when the
    # handoff it wrote embeds the sparring.md that is on disk now: the
    # handoff is regenerated only after a completed implementation turn and
    # sparring.md only by a recorded verdict. The capture index must agree
    # that an implementation turn was sent at all. (Its order alone cannot
    # say more: a reviewer that failed before its verdict still captured
    # its prompt, which is exactly the stuck state this recovers.)
    implementation_after_verdict = (
        handoff is not None
        and state.implementation_session_id is not None
        and order.last_stage is not None
        and handoff.previous_sparring == _previous_sparring_section(stage).strip()
    )

    if implementation_after_verdict:
        assert handoff is not None
        try:
            head = resolve_commit(repo_root, "HEAD", label="HEAD")
        except GitContextError as exc:
            raise NextTurnError(str(exc)) from exc
        try:
            uncommitted = set(_uncommitted_content_paths(repo_root, stage))
        except FinalizationError as exc:
            raise NextTurnError(str(exc)) from exc
        if head == handoff.candidate_sha and uncommitted <= handoff.dirty_paths:
            return NEXT_TURN_SPARRING
        raise AmbiguousNextTurn(
            f"stage {stage.stage_id!r} has no recorded next_turn. Its last completed "
            f"implementation turn recorded candidate {handoff.candidate_sha}, later than the "
            f"last verdict ({outcome.action.value if outcome else 'none'}), but HEAD is now "
            f"{head} (uncommitted: {', '.join(sorted(uncommitted)) or 'none'}). The engine cannot tell whether the reviewer should see HEAD or the "
            "implementation agent should continue; choose explicitly (next_turn = stage | "
            "sparring)."
        )

    if outcome is None or outcome.action is RoutingAction.SEND_BACK:
        # No implementation turn completed after the last verdict (or there
        # is no verdict at all): the implementation agent owes the next turn.
        return NEXT_TURN_STAGE
    return None


def resolve_resume_turn(
    repo_root: Path,
    stage: Stage,
    *,
    choice: str | None = None,
) -> str | None:
    """The marker an ordinary resume obeys, deriving and recording it once
    for legacy state.

    An explicit ``choice`` (``stage`` | ``sparring``) is recorded as
    ``manual`` -- with the current candidate for ``sparring`` -- and wins
    over both a recorded marker and derivation. Returns ``None`` when there
    is no marker and none can be derived without overriding gate state.
    """

    state = stage.read_state()
    if choice is not None:
        if choice not in (NEXT_TURN_STAGE, NEXT_TURN_SPARRING):
            raise NextTurnError(f"next_turn must be 'stage' or 'sparring', got {choice!r}")
        candidate = (
            capture_candidate(repo_root, stage, state) if choice == NEXT_TURN_SPARRING else None
        )
        record_next_turn(stage, choice, candidate=candidate, source="manual")
        return choice
    if state.next_turn is not None:
        return state.next_turn
    derived = derive_next_turn(repo_root, stage, state)
    if derived is None:
        return None
    candidate = (
        capture_candidate(repo_root, stage, state) if derived == NEXT_TURN_SPARRING else None
    )
    record_next_turn(stage, derived, candidate=candidate, source="derived")
    return derived


__all__ = [
    "AmbiguousNextTurn",
    "NextTurnError",
    "capture_candidate",
    "derive_next_turn",
    "record_next_turn",
    "resolve_resume_turn",
    "verify_candidate",
]
