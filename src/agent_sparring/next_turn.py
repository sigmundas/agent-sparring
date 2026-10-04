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
    _is_stage_artifact,
    _uncommitted_content_paths,
    read_worktree_content,
)
from agent_sparring.git_context import GitContextError, resolve_commit
from agent_sparring.handoff import _previous_sparring_section, extract_section
from agent_sparring.prompt_capture import INDEX_FILENAME, prompts_dir
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring.stage import (
    NEXT_TURN_FINALIZATION,
    NEXT_TURN_SPARRING,
    NEXT_TURN_STAGE,
    SiblingPin,
    Stage,
    StageState,
    TurnCandidate,
)


# Not a marker value: what resolve_finalization returns when the only step
# left is the push / acceptance gates for the exact reviewed commit.
RESUME_ACCEPT = "accept"


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
        # A declared sibling that cannot be read has no identity to pin, and
        # "unreadable" must never compare equal to "unreadable" later.
        try:
            sibling_head = resolve_commit(
                sibling_root(repo_root, repository), "HEAD", label=f"sibling {repository.name}"
            )
        except GitContextError as exc:
            raise NextTurnError(
                f"could not read declared sibling repository {repository.name!r} "
                f"({repository.path}): {exc}"
            ) from exc
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
    change. ``candidate`` is kept for ``sparring`` (what the reviewer owes a
    verdict on) and ``finalization`` (what the reviewer said READY over)."""

    state = stage.read_state()
    state.next_turn = next_turn
    state.next_turn_candidate = candidate if next_turn != NEXT_TURN_STAGE else None
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
        # The handoff records a commit and dirty path *names*, never content.
        # Only a committed candidate can therefore be matched exactly: an
        # uncommitted one may have been edited since without a name changing.
        handoff_content_dirty = any(
            not _is_stage_artifact(repo_root, stage, path) for path in handoff.dirty_paths
        )
        if head == handoff.candidate_sha and not uncommitted and not handoff_content_dirty:
            return NEXT_TURN_SPARRING
        if head != handoff.candidate_sha:
            found = f"HEAD is now {head}"
        elif handoff_content_dirty or uncommitted:
            found = (
                "the candidate includes uncommitted content ("
                + (", ".join(sorted(uncommitted)) or "recorded as dirty in the handoff")
                + "), whose bytes the handoff does not record, so it cannot be matched exactly"
            )
        else:  # pragma: no cover -- the first branch returned
            found = "the candidate does not match"
        raise AmbiguousNextTurn(
            f"stage {stage.stage_id!r} has no recorded next_turn. Its last completed "
            f"implementation turn recorded candidate {handoff.candidate_sha}, later than the "
            f"last verdict ({outcome.action.value if outcome else 'none'}), but {found}. The "
            "engine cannot tell whether the reviewer should see the current candidate or the "
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
    repin: bool = False,
) -> str | None:
    """The marker an ordinary resume obeys, deriving and recording it once
    for legacy state.

    An explicit ``choice`` (``stage`` | ``sparring``) is only for ambiguous
    legacy state: it is honoured -- and recorded as ``manual``, with the
    current candidate for ``sparring`` -- only when no marker is recorded
    and derivation is ambiguous or has no answer. A recorded marker, or a
    derived answer that differs from the choice, is refused and nothing is
    recorded; a choice never overrides the engine's record.

    ``repin`` is the one exception, used only by ``resume-plan --evidence
    --next-turn sparring``: over a recorded ``sparring`` marker the person
    re-pins the repository as it is now for the same reviewer turn the
    marker already owes. It never changes which actor runs.

    Returns ``None`` when there is no marker and none can be derived without
    overriding gate state.
    """

    state = stage.read_state()
    if choice is not None:
        if choice not in (NEXT_TURN_STAGE, NEXT_TURN_SPARRING):
            raise NextTurnError(f"next_turn must be 'stage' or 'sparring', got {choice!r}")
        if state.next_turn is not None:
            if not (repin and choice == NEXT_TURN_SPARRING == state.next_turn):
                raise NextTurnError(
                    f"stage {stage.stage_id!r} already records next_turn = {state.next_turn} "
                    f"(source: {state.next_turn_source or 'engine'}); --next-turn {choice} is "
                    "only for state the engine cannot read unambiguously and cannot override "
                    "a recorded marker. Nothing was changed; resume without --next-turn."
                )
        else:
            try:
                derived = derive_next_turn(repo_root, stage, state)
            except AmbiguousNextTurn:
                derived = None
            if derived is not None and derived != choice:
                raise NextTurnError(
                    f"stage {stage.stage_id!r} unambiguously owes next_turn = {derived}; "
                    f"--next-turn {choice} is only for state the engine cannot read "
                    "unambiguously. Nothing was changed; resume without --next-turn."
                )
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


def resolve_finalization(repo_root: Path, stage: Stage, state: StageState) -> str:
    """Where a resume enters for a recorded ``finalization`` marker.

    The marker carries the candidate the reviewer said READY over. Resuming
    is only safe when the repository can be matched against it exactly:

    - the same uncommitted candidate -> ``finalization`` (the bounded commit
      turn, held to that content);
    - the reviewed content now committed and nothing uncommitted -- a
      finalization or a person committed it, but nothing verified that yet
      -> ``sparring``: the marker is re-recorded over that commit so the
      reviewer rules on it before acceptance;
    - anything else is refused. Never an implementation turn: an
      unrestricted turn on reviewed (perhaps human-verified) work is the one
      thing this state must not lead to.

    READY over a candidate that was *already a commit* needed no commit
    turn: what is left is the push and acceptance gates for exactly that
    commit. That is :data:`RESUME_ACCEPT` when the repository still holds it
    (HEAD, content and sibling HEADs), and a refusal otherwise -- no agent
    turn of either kind.
    """

    reviewed = state.next_turn_candidate
    if reviewed is None:
        raise AmbiguousNextTurn(
            f"stage {stage.stage_id!r} is waiting to finalize a READY candidate, but the "
            "candidate it was reviewed as is not recorded. A recorded marker is not "
            "overridden by --next-turn; deliberately choose how to continue (for example "
            "'sparring reset-stage')."
        )
    current = capture_candidate(repo_root, stage, state)
    if reviewed.kind == "commit":
        if current == reviewed:
            return RESUME_ACCEPT
        raise AmbiguousNextTurn(
            f"stage {stage.stage_id!r} was READY over the commit {reviewed.describe()}, but "
            f"the repository now holds {current.describe()}. Only that reviewed commit may be "
            "pushed and accepted, and the engine will not start an agent turn on a READY stage "
            "by itself; restore the reviewed commit, or deliberately choose how to continue "
            "(for example 'sparring reset-stage')."
        )
    if current == reviewed and current.kind == "worktree":
        return NEXT_TURN_FINALIZATION
    if (
        current.kind == "commit"
        and current.content_digest == reviewed.content_digest
        and current.repositories == reviewed.repositories
    ):
        record_next_turn(stage, NEXT_TURN_SPARRING, candidate=current)
        return NEXT_TURN_SPARRING
    raise AmbiguousNextTurn(
        f"stage {stage.stage_id!r} was READY over {reviewed.describe()}, but the repository "
        f"now holds {current.describe()}. That is neither the reviewed candidate awaiting its "
        "commit nor a commit of exactly the reviewed content, so the engine will not commit "
        "it, review it or implement on it by itself; restore the reviewed candidate, or "
        "deliberately choose how to continue (for example 'sparring reset-stage')."
    )


def standalone_start_with(
    repo_root: Path, stage: Stage, *, choice: str | None = None
) -> str:
    """Where a standalone ``run-loop`` resume enters the loop:
    ``stage``, ``sparring`` or ``finalization``.

    The same precedence as a managed resume, minus the parts only a managed
    run has. A recorded human gate (NEEDS_YOU / ESCALATE) still stands
    unless the marker already says an implementation turn is owed: running
    either agent would not answer it, so it is refused -- before anything is
    recorded, and always when a ``choice`` is given. A ``finalization``
    marker is resolved by :func:`resolve_finalization`. ``choice`` is the
    caller's explicit ``stage`` | ``sparring`` (see
    :func:`resolve_resume_turn`).
    """

    # The recorded gate is checked before anything is recorded: a choice can
    # never answer or bypass it, and a refusal leaves state.json untouched.
    outcome = read_recorded_outcome(stage)
    if outcome is not None and outcome.awaits_a_human and (
        choice is not None or stage.read_state().next_turn != NEXT_TURN_STAGE
    ):
        raise NextTurnError(
            f"stage {stage.stage_id!r} is waiting for a person ({outcome.action.value}: "
            f"{outcome.summary or 'see sparring.md'}); running either agent would not answer "
            "it. Record the answer (resume-plan --evidence for a managed run, or under '## "
            "Human evidence' in notes.md) and run the reviewer (run-sparring)."
        )
    marker = resolve_resume_turn(repo_root, stage, choice=choice)
    if marker == NEXT_TURN_FINALIZATION:
        resolved = resolve_finalization(repo_root, stage, stage.read_state())
        if resolved == RESUME_ACCEPT:
            # run-loop never accepts; the managed run (or `freeze`/`accept`)
            # does. Running an agent here would only re-do a recorded READY.
            raise NextTurnError(
                f"stage {stage.stage_id!r} is READY over its committed candidate; no agent turn "
                "is owed. Complete it through the acceptance gate (resume-plan for a managed "
                "run)."
            )
        return resolved
    return marker if marker == NEXT_TURN_SPARRING else NEXT_TURN_STAGE


__all__ = [
    "AmbiguousNextTurn",
    "NextTurnError",
    "RESUME_ACCEPT",
    "capture_candidate",
    "derive_next_turn",
    "record_next_turn",
    "resolve_finalization",
    "resolve_resume_turn",
    "standalone_start_with",
    "verify_candidate",
]
