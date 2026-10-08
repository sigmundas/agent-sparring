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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_sparring.acceptance import sibling_root
from agent_sparring.finalization import (
    CandidateContent,
    FinalizationError,
    _git,
    _is_stage_artifact,
    _uncommitted_content_paths,
    read_commit_content,
    read_worktree_content,
)
from agent_sparring.git_context import (
    GitContextError,
    dirty_entries,
    resolve_commit,
)
from agent_sparring.handoff import _previous_sparring_section, extract_section
from agent_sparring.prompt_capture import INDEX_FILENAME, prompts_dir
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring.stage import (
    NEXT_TURN_FINALIZATION,
    NEXT_TURN_SPARRING,
    NEXT_TURN_STAGE,
    NextTurnResolution,
    SiblingPin,
    Stage,
    StageState,
    TurnCandidate,
)


# Not a marker value: what resolve_finalization returns when the only step
# left is the push / acceptance gates for the exact reviewed commit.
RESUME_ACCEPT = "accept"

# Not a marker value: what derive_next_turn returns when a recorded gate
# (READY, NEEDS_YOU, ESCALATE) with nothing after it decides instead.
NO_TURN_OWED = "no-turn-owed"


class NextTurnError(RuntimeError):
    """The recorded or derivable next turn cannot be honoured."""


class AmbiguousNextTurn(NextTurnError):
    """Legacy state does not say unambiguously whose turn it is; the caller
    must choose explicitly (``next_turn`` = ``stage`` | ``sparring``)."""


class FinalizationDrift(AmbiguousNextTurn):
    """A ``finalization`` marker no longer matches the repository. Callers
    that know their own command line add the exact commands to continue."""

    def __init__(self, message: str, *, stage_id: str):
        super().__init__(message)
        self.stage_id = stage_id


def untracked_candidate_paths(repo_root: Path, stage: Stage) -> tuple[str, ...]:
    """Untracked, non-ignored paths that count as candidate content (stage
    artifacts excluded), file by file."""

    try:
        entries = dirty_entries(repo_root, all_untracked=True)
    except GitContextError as exc:
        raise NextTurnError(f"could not read the working tree status: {exc}") from exc
    return tuple(
        sorted(
            entry.path
            for entry in entries
            if entry.status == "??" and not _is_stage_artifact(repo_root, stage, entry.path)
        )
    )


def unrelated_untracked_paths(repo_root: Path, stage: Stage) -> tuple[str, ...]:
    """Untracked candidate paths the stage's implementation did not produce.

    Decided from engine-owned records only: a path is unrelated when it was
    present before the stage's first implementation turn
    (``untracked_baseline``) or is not among the exact untracked paths the
    last implementation turn left (``untracked_produced``). For a turn
    recorded before ``untracked_produced`` existed, the handoff's
    working-tree list stands in, matched exactly: a collapsed ``dir/`` entry
    does not say which files existed under it, so it vouches for none.
    """

    untracked = untracked_candidate_paths(repo_root, stage)
    if not untracked:
        return ()
    state = stage.read_state()
    baseline = frozenset(state.untracked_baseline or ())
    if state.untracked_produced is not None:
        produced = frozenset(state.untracked_produced)
    else:
        handoff = _read_handoff(stage)
        produced = handoff.dirty_paths if handoff is not None else frozenset()
    return tuple(path for path in untracked if path in baseline or path not in produced)


class UntrackedRefusal(NextTurnError):
    """A reviewer start refused for untracked files the implementation did
    not produce. ``paths`` names them; callers that know their own command
    line add the exact command to rerun."""

    def __init__(self, message: str, paths: tuple[str, ...]):
        super().__init__(message)
        self.paths = paths
        # Set when an explicit --next-turn choice was refused before it was
        # recorded: a retry must pass it again.
        self.unapplied_next_turn: str | None = None


def implementation_awaiting_review(stage: Stage, state: StageState) -> bool:
    """A completed implementation turn whose review was never pinned.

    ``implementation_unreviewed`` is set by a successful implementation turn
    and cleared by every later marker write -- the review pin, a verdict
    (SEND_BACK or READY), a reopen, the next cycle start. Still standing
    with ``next_turn = stage`` means the reviewer-start check refused before
    the review marker was written: the turn owed is that review, not
    another implementation turn. Structured state only; no prose is read.
    """

    return state.next_turn == NEXT_TURN_STAGE and state.implementation_unreviewed


def check_untracked_before_review(repo_root: Path, stage: Stage) -> None:
    """Refuse a reviewer turn while untracked files the implementation did
    not produce would become part of the candidate (see
    :func:`unrelated_untracked_paths`). Checks only; records nothing.

    Every caller runs it before writing a review marker or a resolution, so
    a refusal leaves the stage exactly as it was. Once the files are
    removed, committed or ignored, the same resume reaches review: a
    completed implementation turn with no review marker after it is owed a
    review (see :func:`implementation_awaiting_review`).
    """

    unrelated = unrelated_untracked_paths(repo_root, stage)
    if not unrelated:
        return
    raise UntrackedRefusal(
        f"refusing to start the reviewer for stage {stage.stage_id!r}: the working tree holds "
        "untracked files the stage's implementation did not produce, and they would become "
        "part of the reviewed candidate: "
        + ", ".join(unrelated)
        + ". Nothing was recorded for the review. Remove them, commit them, or ignore them "
        "(.gitignore or .git/info/exclude), then rerun the same command.",
        unrelated,
    )


def capture_candidate(repo_root: Path, stage: Stage, state: StageState) -> TurnCandidate:
    """The current candidate identity: HEAD, whether all candidate content is
    committed, the content digest, and every declared sibling's HEAD -- plus
    the tracked-content digest and the untracked paths with their blobs."""

    try:
        head = resolve_commit(repo_root, "HEAD", label="HEAD")
        uncommitted = _uncommitted_content_paths(repo_root, stage)
        content = read_worktree_content(repo_root, stage)
    except (GitContextError, FinalizationError) as exc:
        raise NextTurnError(f"could not read the candidate identity: {exc}") from exc
    untracked_paths = set(untracked_candidate_paths(repo_root, stage))
    untracked = tuple(entry for entry in content.entries if entry[0] in untracked_paths)
    tracked_digest = CandidateContent(
        entries=tuple(entry for entry in content.entries if entry[0] not in untracked_paths)
    ).digest
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
        content_digest=content.digest,
        repositories=tuple(pins),
        tracked_digest=tracked_digest,
        untracked=untracked,
    )


def record_resolution(stage: Stage, turn: str, *, source: str, reason: str) -> None:
    """Persist how an ambiguous legacy state was resolved, once. Routine
    marker writes (:func:`record_next_turn`) never touch it."""

    state = stage.read_state()
    if state.next_turn_resolution is not None:
        return
    state.next_turn_resolution = NextTurnResolution(
        turn=turn,
        source=source,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        reason=reason,
    )
    stage.write_state(state)


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
    state.implementation_unreviewed = False
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


def repin_after_authorized_advance(
    repo_root: Path, stage: Stage, state: StageState, accepted_head: str
) -> TurnCandidate:
    """Re-pin a pending review onto a HEAD a person named, when the branch
    advanced by commits made outside the stage while it waited on them.

    The one case this exists for: a stage stopped on NEEDS_YOU because a
    prerequisite had to land separately, and the person brought that commit
    onto the branch beneath the stage's uncommitted attempt. Everything else
    stays refused. The re-pin happens only if ``accepted_head`` is the
    current HEAD, the pinned HEAD is a strict ancestor of it, every sibling
    pin is unchanged, no path the new commits change has uncommitted
    content, the untracked candidate files are byte-identical to the pinned
    ones, and the current content with exactly those commits taken back out
    reproduces the pinned content digest. That last check proves the
    attempt's work is preserved exactly and that the only difference is the
    named commits. Returns the newly pinned candidate; writes nothing.
    """

    pinned = state.next_turn_candidate
    if pinned is None:
        raise NextTurnError(
            f"stage {stage.stage_id!r} has no pinned candidate to re-pin"
        )
    # A commit id, not a revision expression: "HEAD", a branch or "HEAD~1"
    # would accept whatever the branch holds rather than a commit the person
    # named, and a hex-looking ref name must not stand in for a SHA prefix.
    if not re.fullmatch(r"[0-9a-f]{4,64}", accepted_head):
        raise NextTurnError(
            f"{accepted_head!r} is not a commit SHA; name the commit by its (abbreviated) SHA"
        )
    try:
        accepted = resolve_commit(repo_root, accepted_head, label="accepted head")
    except GitContextError as exc:
        raise NextTurnError(f"could not resolve the accepted head: {exc}") from exc
    if not accepted.startswith(accepted_head):
        raise NextTurnError(
            f"{accepted_head!r} resolves to {accepted}, which it is not a prefix of (a ref "
            "with that name?); name the commit by its SHA"
        )
    current = capture_candidate(repo_root, stage, state)
    if current.head_sha != accepted:
        raise NextTurnError(
            f"the accepted head {accepted} is not the current HEAD {current.head_sha}; "
            "name the commit the branch actually holds"
        )
    if accepted == pinned.head_sha:
        raise NextTurnError(
            f"HEAD has not advanced from the pinned {pinned.head_sha}; there is nothing "
            "to accept"
        )
    try:
        _git(repo_root, "merge-base", "--is-ancestor", pinned.head_sha, accepted)
    except FinalizationError as exc:
        raise NextTurnError(
            f"the pinned HEAD {pinned.head_sha} is not an ancestor of {accepted}; only a "
            "fast-forward on top of the pinned candidate can be accepted"
        ) from exc
    if current.repositories != pinned.repositories:
        raise NextTurnError(
            "a declared sibling repository moved as well; refusing to re-pin more than "
            "the named commits"
        )
    if current.untracked != pinned.untracked:
        raise NextTurnError(
            "the untracked candidate files differ from the pinned ones; the attempt's "
            "work is not preserved exactly"
        )
    try:
        before = read_commit_content(repo_root, stage, pinned.head_sha)
        after = read_commit_content(repo_root, stage, accepted)
        uncommitted = set(_uncommitted_content_paths(repo_root, stage))
        content = read_worktree_content(repo_root, stage)
    except FinalizationError as exc:
        raise NextTurnError(f"could not read the candidate content: {exc}") from exc
    advanced = before.diverged_from(after)
    overlap = sorted(uncommitted.intersection(advanced))
    if overlap:
        raise NextTurnError(
            "the accepted commits change paths that also hold uncommitted attempt "
            f"content ({', '.join(overlap)}); refusing to merge them into one candidate"
        )
    old_entries = dict(before.entries)
    rebuilt = dict(content.entries)
    for path in advanced:
        if path in old_entries:
            rebuilt[path] = old_entries[path]
        else:
            rebuilt.pop(path, None)
    if CandidateContent(entries=tuple(sorted(rebuilt.items()))).digest != pinned.content_digest:
        raise NextTurnError(
            f"the repository is not the pinned candidate plus the commits "
            f"{pinned.head_sha[:12]}..{accepted[:12]}; something else changed as well"
        )
    return current


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


def _untouched(stage: Stage, state: StageState) -> bool:
    """Nothing has run for this stage yet."""

    return (
        read_recorded_outcome(stage) is None
        and state.implementation_session_id is None
        and not state.sessions
    )


def derive_next_turn(repo_root: Path, stage: Stage, state: StageState) -> str:
    """Reconstruct the marker for state written before it existed.

    Returns ``stage`` (including for an untouched stage, whose first turn is
    simply next), ``sparring``, or :data:`NO_TURN_OWED` when the recorded
    verdict is a gate (READY, NEEDS_YOU, ESCALATE) with no later
    implementation turn -- the gate state is authoritative and existing gate
    handling decides it. Raises :class:`AmbiguousNextTurn`, explaining what
    was found, when the engine cannot tell.
    """

    outcome = read_recorded_outcome(stage)
    if _untouched(stage, state):
        return NEXT_TURN_STAGE
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
    return NO_TURN_OWED


def check_next_turn_choice(repo_root: Path, stage: Stage, choice: str) -> str:
    """Refuse an explicit ``choice`` unless it is legal: no marker recorded
    and derivation is ambiguous. Records nothing either way; returns the
    ambiguity the choice answers."""

    if choice not in (NEXT_TURN_STAGE, NEXT_TURN_SPARRING):
        raise NextTurnError(f"next_turn must be 'stage' or 'sparring', got {choice!r}")
    state = stage.read_state()
    if state.next_turn is not None:
        raise NextTurnError(
            f"stage {stage.stage_id!r} already records next_turn = {state.next_turn} "
            f"(source: {state.next_turn_source or 'engine'}); --next-turn {choice} is "
            "only for state the engine cannot read unambiguously and cannot override "
            "or re-pin a recorded marker. Nothing was changed; resume without "
            "--next-turn."
        )
    try:
        derived = derive_next_turn(repo_root, stage, state)
    except AmbiguousNextTurn as exc:
        return str(exc)
    if derived == NO_TURN_OWED:
        outcome = read_recorded_outcome(stage)
        verdict = outcome.action.value if outcome is not None else "a gate"
        raise NextTurnError(
            f"stage {stage.stage_id!r} records {verdict} with no implementation turn after "
            f"it; that gate decides how the stage continues, and --next-turn {choice} cannot "
            "override it. Nothing was changed; resume without --next-turn"
            + (
                " (a READY stage continues through finalization and acceptance)."
                if outcome is not None and outcome.action is RoutingAction.READY
                else " (answer a person's gate with --evidence)."
            )
        )
    if _untouched(stage, state):
        raise NextTurnError(
            f"stage {stage.stage_id!r} has not run yet: its first turn is the implementation "
            f"turn, so --next-turn {choice} is "
            + (
                "unnecessary"
                if choice == NEXT_TURN_STAGE
                else "wrong (there is no candidate to review)"
            )
            + ". Nothing was changed; resume without --next-turn."
        )
    raise NextTurnError(
        f"stage {stage.stage_id!r} unambiguously owes next_turn = {derived}; "
        f"--next-turn {choice} is only for state the engine cannot read "
        "unambiguously. Nothing was changed; resume without --next-turn."
    )


def resolve_resume_turn(
    repo_root: Path,
    stage: Stage,
    *,
    choice: str | None = None,
) -> str | None:
    """The marker an ordinary resume obeys, deriving and recording it once
    for legacy state.

    An explicit ``choice`` (``stage`` | ``sparring``) is only for ambiguous
    legacy state: it is honoured -- and recorded as ``manual``, with the
    current candidate for ``sparring`` -- only when no marker is recorded
    and derivation is ambiguous (see :func:`check_next_turn_choice`). A recorded marker (of any
    value, including the one chosen) or any unambiguous derived answer
    (even the one chosen) is refused and nothing is recorded; a choice never
    overrides or re-pins the engine's record.

    Returns :data:`NO_TURN_OWED` when there is no marker and the recorded
    gate (READY, NEEDS_YOU, ESCALATE) decides instead; callers must route
    that through gate handling (see :func:`resolve_legacy_ready`), never to
    an implementation turn.
    """

    if choice is not None:
        ambiguity = check_next_turn_choice(repo_root, stage, choice)
        if choice == NEXT_TURN_SPARRING:
            # Before the marker or the resolution is written; the choice is
            # then still unapplied, so a retry must repeat it.
            try:
                check_untracked_before_review(repo_root, stage)
            except UntrackedRefusal as exc:
                exc.unapplied_next_turn = choice
                raise
        state = stage.read_state()
        candidate = (
            capture_candidate(repo_root, stage, state)
            if choice == NEXT_TURN_SPARRING
            else None
        )
        record_next_turn(stage, choice, candidate=candidate, source="manual")
        record_resolution(stage, choice, source="manual", reason=ambiguity)
        return choice
    state = stage.read_state()
    if implementation_awaiting_review(stage, state):
        # Nothing is recorded here: the loop pins the candidate once the
        # reviewer-start check passes.
        return NEXT_TURN_SPARRING
    if state.next_turn is not None:
        return state.next_turn
    if _untouched(stage, state):
        # Its first turn; nothing to reconstruct or record.
        return NEXT_TURN_STAGE
    derived = derive_next_turn(repo_root, stage, state)
    if derived == NO_TURN_OWED:
        # Gate state decides; never mapped to an implementation turn.
        return NO_TURN_OWED
    if derived == NEXT_TURN_SPARRING:
        check_untracked_before_review(repo_root, stage)
    candidate = (
        capture_candidate(repo_root, stage, state)
        if derived == NEXT_TURN_SPARRING
        else None
    )
    record_next_turn(stage, derived, candidate=candidate, source="derived")
    record_resolution(
        stage,
        derived,
        source="derived",
        reason="no next_turn recorded; derived once from the engine-owned stage records",
    )
    return derived


def resolve_legacy_ready(repo_root: Path, stage: Stage) -> str:
    """Where a resume enters a READY recorded before the next_turn marker
    existed, with nothing after it: never an implementation turn.

    - reviewed content still uncommitted -> ``finalization`` (the managed
      resume's bounded commit turn, held to the uncommitted content;
      :func:`standalone_start_with` refuses this case for ``run-loop``);
    - a clean committed candidate -> ``sparring``, recorded as a derived
      marker over the current commit: which commit READY was given over is
      not recorded, so the reviewer rules on this one before acceptance.

    Anything that is not a recorded READY is refused.
    """

    outcome = read_recorded_outcome(stage)
    if outcome is None or outcome.action is not RoutingAction.READY:
        raise NextTurnError(
            f"stage {stage.stage_id!r} records "
            f"{outcome.action.value if outcome else 'no verdict'} with no implementation turn "
            "after it; no agent turn is owed until that gate is answered (--evidence)."
        )
    try:
        uncommitted = _uncommitted_content_paths(repo_root, stage)
    except FinalizationError as exc:
        raise NextTurnError(f"could not read the candidate content: {exc}") from exc
    if uncommitted:
        return NEXT_TURN_FINALIZATION
    check_untracked_before_review(repo_root, stage)
    state = stage.read_state()
    record_next_turn(
        stage,
        NEXT_TURN_SPARRING,
        candidate=capture_candidate(repo_root, stage, state),
        source="derived",
    )
    record_resolution(
        stage,
        NEXT_TURN_SPARRING,
        source="derived",
        reason=(
            "legacy READY over a clean commit with no recorded candidate; the reviewer rules "
            "on the current commit before acceptance"
        ),
    )
    return NEXT_TURN_SPARRING


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
    - the reviewed HEAD, now clean, with only previously reviewed untracked
      files removed (see :func:`_removed_untracked_only`) -> ``sparring``
      likewise, over that commit;
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
    removed = _removed_untracked_only(reviewed, current)
    if removed is not None:
        # Only previously reviewed untracked files disappeared: re-review
        # exactly this commit before acceptance. Never implementation,
        # never direct acceptance.
        record_next_turn(stage, NEXT_TURN_SPARRING, candidate=current)
        print(
            f"stage {stage.stage_id!r}: {removed}; the reviewer rules on the clean commit "
            f"{current.head_sha} before acceptance.",
            file=sys.stderr,
        )
        return NEXT_TURN_SPARRING
    raise FinalizationDrift(
        f"stage {stage.stage_id!r} was READY over {reviewed.describe()}, but the repository "
        f"now holds {current.describe()}. That is neither the reviewed candidate awaiting its "
        "commit, a commit of exactly the reviewed content, nor the reviewed HEAD with only "
        "previously reviewed untracked files removed, so the engine will not commit it, "
        "review it or implement on it by itself."
        + _drift_detail(repo_root, stage, reviewed, current)
        + " Restore the reviewed candidate and rerun the same resume, or deliberately "
        "discard this attempt with 'sparring reset-stage'.",
        stage_id=stage.stage_id,
    )


def _removed_untracked_only(reviewed: TurnCandidate, current: TurnCandidate) -> str | None:
    """A description of the narrow recovery when ``current`` is exactly the
    reviewed worktree candidate minus its untracked files, else ``None``.

    Requires: reviewed was a worktree; current is a clean commit at the
    reviewed HEAD with the same sibling HEADs; and tracked content is
    unchanged (current's content equals the reviewed tracked digest). A
    clean commit holds no untracked paths, so none were added and none
    remain modified. A marker recorded before the split was kept is allowed
    on HEAD, siblings and cleanliness alone.
    """

    if (
        reviewed.kind != "worktree"
        or current.kind != "commit"
        or current.head_sha != reviewed.head_sha
        or current.repositories != reviewed.repositories
    ):
        return None
    if reviewed.tracked_digest is None or reviewed.untracked is None:
        return (
            "the READY candidate was recorded without its untracked-file detail; HEAD and "
            "sibling HEADs match it and the working tree is now clean, so the engine cannot "
            "confirm only untracked files were removed"
        )
    if not reviewed.untracked or current.content_digest != reviewed.tracked_digest:
        return None
    return "previously reviewed untracked files were removed (" + ", ".join(
        path for path, _ in reviewed.untracked
    ) + ") and nothing else changed"


def _drift_detail(
    repo_root: Path, stage: Stage, reviewed: TurnCandidate, current: TurnCandidate
) -> str:
    """Name what blocks the untracked-removal recovery, with paths where the
    repository can supply them."""

    if reviewed.kind != "worktree":
        return ""
    if current.head_sha != reviewed.head_sha:
        try:
            changed = [
                line
                for line in _git(
                    repo_root, "diff", "--name-only", reviewed.head_sha, current.head_sha
                ).splitlines()
                if line
            ]
        except FinalizationError:
            changed = []
        return (
            f" HEAD moved from {reviewed.head_sha} to {current.head_sha}"
            + (" (changing " + ", ".join(changed) + ")" if changed else "")
            + "."
        )
    if current.repositories != reviewed.repositories:
        moved = [
            f"{after.name} {before.head_sha} -> {after.head_sha}"
            for before, after in zip(reviewed.repositories, current.repositories)
            if before != after
        ]
        return " Declared sibling HEADs changed: " + (", ".join(moved) or "pins differ") + "."
    if reviewed.untracked is None or current.untracked is None:
        return ""
    before = dict(reviewed.untracked)
    after = dict(current.untracked)
    added = sorted(set(after) - set(before))
    modified = sorted(path for path in after if path in before and after[path] != before[path])
    parts = []
    if added:
        parts.append("new untracked files: " + ", ".join(added))
    if modified:
        parts.append("modified previously reviewed untracked files: " + ", ".join(modified))
    if current.tracked_digest is not None and current.tracked_digest != reviewed.tracked_digest:
        try:
            tracked = sorted(
                set(_uncommitted_content_paths(repo_root, stage)) - set(after)
            )
        except FinalizationError:
            tracked = []
        parts.append(
            "tracked content changed"
            + (" (uncommitted tracked changes: " + ", ".join(tracked) + ")" if tracked else "")
        )
    return (" Found " + "; ".join(parts) + ".") if parts else ""


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
    if marker == NO_TURN_OWED:
        resolved = resolve_legacy_ready(repo_root, stage)
        if resolved == NEXT_TURN_FINALIZATION:
            # A legacy READY over uncommitted content has no pinned candidate
            # to hold a commit turn to; only the managed resume finalizes it.
            raise NextTurnError(
                f"stage {stage.stage_id!r} records a legacy READY over uncommitted content with "
                "no pinned candidate; run-loop does not finalize it. Resume it as a managed run "
                "(resume-plan)."
            )
        return resolved
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
    "FinalizationDrift",
    "NO_TURN_OWED",
    "NextTurnError",
    "RESUME_ACCEPT",
    "capture_candidate",
    "check_next_turn_choice",
    "check_untracked_before_review",
    "implementation_awaiting_review",
    "UntrackedRefusal",
    "unrelated_untracked_paths",
    "untracked_candidate_paths",
    "derive_next_turn",
    "record_next_turn",
    "record_resolution",
    "resolve_finalization",
    "resolve_legacy_ready",
    "resolve_resume_turn",
    "standalone_start_with",
    "verify_candidate",
]
