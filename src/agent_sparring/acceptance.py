"""The acceptance gate: freeze an exact candidate, then accept exactly that.

Per the project plan's "Acceptance is the hard gate": normal sparring is
soft, acceptance is strict. This module implements only two operations on
the *existing* :class:`~agent_sparring.stage.StageState` vocabulary
(``status`` WORKING/FROZEN/ACCEPTED plus ``candidate_sha``):

    freeze_candidate()  WORKING/FROZEN -> FROZEN, candidate_sha = exact HEAD
    accept_candidate()  FROZEN         -> ACCEPTED, candidate_sha unchanged

and one more for the single kind of stage that has no candidate of its own
to freeze:

    accept_reviewed_candidate()
                        WORKING -> ACCEPTED, candidate_sha = the commit this
                        review-only stage was entered against

A review-only stage (:attr:`~agent_sparring.stage.StageMode.
INDEPENDENT_REVIEW`) reviews commits the preceding stages already accepted
and writes none of its own, so the two-step freeze has nothing to pin at
READY: there is no new commit, and inventing one to satisfy the machinery
would mean accepting a candidate that represents no work. The identity it
is judged against is therefore pinned when the stage is *entered*, before
the reviewer runs, and this third operation re-verifies exactly that pinned
set before recording acceptance. The implementation path above is
untouched, and neither operation is reachable from the other's mode.

There is deliberately no transition table, no generic workflow engine, and
none of the V1 machinery the plan explicitly rules out: no review states,
no attempt ids, no immutable intermediate verdicts, no history log. Both
operations fail loudly and leave the stage *unaccepted* whenever their
invariants are not satisfied.

Freezing means exactly one thing:

    this exact SHA is the candidate being presented for acceptance

It does not make the repository immutable, and it does not stop anyone
from committing afterwards. What it does is pin the identity that
:func:`accept_candidate` must still find, so a candidate that moved on is
caught rather than silently swapped for whatever HEAD happens to be at
acceptance time.

Same-SHA reconsideration is a first-class case, not an exception. Evidence
can change while code does not — a manual/device check a sparrer asked for
gets performed, the human records it, and the *same* SHA is sparred again
and reaches READY. Nothing here records "this SHA was already reviewed", so
that path needs no dummy commit and no new candidate identity: freeze is
re-runnable on the same SHA, and acceptance of a SHA that appeared in an
earlier non-READY sparring exchange is not refused.

READY is not acceptance. This module is never called automatically by
:mod:`agent_sparring.loop` when the sparrer says READY; acceptance stays an
explicit operation (``sparring freeze-candidate`` / ``sparring
accept-candidate``, or a direct call), so a human or an explicit
orchestration step always decides that a stage is done. The evidence for
that decision lives in the stage's human-readable ``sparring.md`` /
``notes.md``; it is deliberately not parsed or gated on here, which would
turn prose findings back into workflow state.

Contributor sparrers: the plan's rule is that a sparrer which contributes
code to a candidate cannot independently accept that candidate — a fresh
independent sparrer is required. The current sparring adapter is hard
read-only (see :mod:`agent_sparring.providers.codex_cli` and the
before/after repository-integrity check in
:mod:`agent_sparring.sparring_agent`), so there is no path today by which
the sparrer becomes a contributor. No contributor ledger, signature, or
reviewer identity is therefore recorded here: the rule stands as a
documented convention until a writable sparrer mode actually exists to
need metadata for it.

Both operations run inside :func:`agent_sparring.concurrency.worktree_lock`
-- the same one-writer-per-worktree lock :func:`agent_sparring.stage_agent.
run_stage_agent` already holds for an implementation turn. This is not a
second lock system: it is the existing lock, reused, so a FROZEN stage's
correction turn and a concurrent freeze/accept on the same worktree cannot
interleave (one checks HEAD and changes lifecycle state while the other is
mid-commit). A contended lock is wrapped as :class:`AcceptanceError` rather
than left as a raw :class:`~agent_sparring.concurrency.WorktreeLockError`,
and, like every other refusal here, leaves ``state.json`` untouched.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent_sparring import engine_git, managed_run
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.git_context import (
    DirtyEntry,
    GitContextError,
    current_branch,
    dirty_entries,
    is_ancestor,
    is_full_sha,
    resolve_commit,
)
from agent_sparring.prompt_capture import PROMPTS_DIRNAME, is_prompt_artifact
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_exchange import read_recorded_outcome
from agent_sparring import trusted_snapshots
from agent_sparring.remote_pin import (
    REMOTE_PAUSE_KINDS,
    PushedCandidateUnauthorized,
    RemotePause,
    remote_pause_from_dict,
)
from agent_sparring.trusted_snapshots import TRUSTED_CONFIG_MISSING, RunSnapshots, missing_message
from agent_sparring.run_history import (
    RUN_HISTORY_REWRITTEN,
    AcceptedCandidate,
    RunHistoryError,
    describe_repository,
    required_ancestors,
    run_accepted,
)
from agent_sparring.stage import (
    ACTIVITY_FILENAME,
    BRIEF_FILENAME,
    DIALOGUE_FILENAME,
    HANDOFF_FILENAME,
    NOTES_FILENAME,
    SPARRING_FILENAME,
    NEXT_TURN_FINALIZATION,
    NEXT_TURN_SPARRING,
    STATE_FILENAME,
    CandidateRepository,
    ReviewRecord,
    SiblingPin,
    Stage,
    StageError,
    StageMode,
    StageState,
    StageStatus,
)

#: The typed refusal when a candidate is not the exact, clean ``HEAD`` of its
#: expected branch in a participating repository (I-L2).
CANDIDATE_NOT_ON_BRANCH = "candidate_not_on_branch"
#: The typed refusal when the reviewer's recorded READY is not over the
#: exact commit being accepted.
VERDICT_NOT_AT_CANDIDATE = "verdict_not_at_candidate"
#: The typed refusal when, in a managed worktree, the stage's own artifacts
#: contradict the run its managed record names.
STAGE_OWNER_MISMATCH = "stage_owner_mismatch"
#: What the engine's local acceptance rule is, stated in every
#: ``candidate_not_on_branch`` refusal.
LOCAL_RULE = (
    "a candidate is the exact HEAD of its expected branch in every participating "
    "repository, with a working tree clean apart from the engine's own artifacts; "
    "no remote is consulted"
)

# The only paths ever exempted from the "no unrepresented dirty changes"
# checks below: this stage's own artifact files, which agent_sparring
# itself rewrites on every stage/sparring turn (see stage.py's Stage.create/
# write_state/write_handoff/write_sparring/write_notes, plus the
# observational activity.jsonl appended by every turn). These are workflow
# bookkeeping and evidence, deliberately outside candidate identity -- never
# application/test/config code. The exemption is an explicit allowlist of
# exact filenames, not a subtree/prefix rule: naming a directory
# "sparring_dir" must never make an arbitrary tree (up to and including the
# whole repository, if a caller passed sparring_dir == repo_root) invisible
# to this check.
STAGE_ARTIFACT_FILENAMES = (
    STATE_FILENAME,
    BRIEF_FILENAME,
    NOTES_FILENAME,
    HANDOFF_FILENAME,
    SPARRING_FILENAME,
    ACTIVITY_FILENAME,
    # A reviewer conversation is bookkeeping about the candidate, never part
    # of it. Without this entry, asking the reviewer a question would dirty
    # the worktree and the stage's own acceptance gate would then refuse the
    # candidate because someone had asked about it.
    DIALOGUE_FILENAME,
)

# The one artifact that is not a flat file beside the others: every turn's
# captured prompt lands in this stage's own prompts/ subdirectory (see
# agent_sparring.prompt_capture). Handled by _is_allowed rather than added
# here, because the filenames are per-turn and cannot be enumerated -- but
# handled with the same suspicion: this stage's directory only, no nesting,
# and only filenames this engine actually writes.


class AcceptanceError(RuntimeError):
    """Raised when a freeze or an acceptance is refused.

    Every refusal leaves the stage unaccepted. ``state.json`` is only ever
    written on a *successful* freeze or acceptance, so a refused operation
    cannot half-advance the lifecycle.

    ``code`` names the typed refusals a caller may act on
    (:data:`CANDIDATE_NOT_ON_BRANCH`, :data:`~agent_sparring.run_history.
    RUN_HISTORY_REWRITTEN`, :data:`VERDICT_NOT_AT_CANDIDATE`,
    :data:`STAGE_OWNER_MISMATCH`); ``None`` for
    the rest. The message always starts with the code when there is one.
    """

    code: str | None = None


class CandidateNotOnBranchError(AcceptanceError):
    """The candidate is not the exact, clean ``HEAD`` of its expected branch
    in a participating repository (:data:`LOCAL_RULE`)."""

    code = CANDIDATE_NOT_ON_BRANCH

    def __init__(self, message: str) -> None:
        super().__init__(f"refusing: {CANDIDATE_NOT_ON_BRANCH}: {message}. The rule: {LOCAL_RULE}")


class StaleCandidateError(CandidateNotOnBranchError):
    """Raised when the repository no longer matches the frozen candidate.

    The frozen ``candidate_sha`` is left exactly as it was: the new HEAD is
    never silently substituted, and no dummy commit is created. Presenting
    the changed code means freezing it as a new candidate and sparring it
    again before a later acceptance.
    """


class RunHistoryRewrittenError(AcceptanceError):
    """A repository's stage base, or a candidate the run already accepted in
    it, is not an ancestor of that repository's current candidate (see
    :mod:`agent_sparring.run_history`)."""

    code = RUN_HISTORY_REWRITTEN

    def __init__(self, repository: str, missing: str, what: str, candidate: str) -> None:
        super().__init__(
            f"refusing: {RUN_HISTORY_REWRITTEN}: in {repository}, {what} ({missing}) is not an "
            f"ancestor of the candidate {candidate}, so accepted history was rewritten. A run's "
            "accepted history only grows, per repository: build on top of it (new commits or a "
            "merge that contains it), or restore it. Nothing was frozen or accepted"
        )
        self.repository = repository
        self.missing = missing


class VerdictNotAtCandidateError(AcceptanceError):
    """The reviewer's recorded READY is not over the commit being accepted."""

    code = VERDICT_NOT_AT_CANDIDATE


class StageOwnerMismatchError(AcceptanceError):
    """In a managed worktree, the stage's ``.sparring/`` artifacts contradict
    the run the engine's managed record says owns it (audit R-4)."""

    code = STAGE_OWNER_MISMATCH

    def __init__(self, stage_id: str, run: str, problems: list[str]) -> None:
        super().__init__(
            f"refusing: {STAGE_OWNER_MISMATCH}: stage {stage_id!r} is in the managed worktree of "
            f"run {run} (the engine's managed record), but {'; '.join(problems)}. The project "
            "directory's claims never decide who owns a managed stage; nothing was frozen or "
            "accepted"
        )
        self.run = run


@dataclass(frozen=True)
class FreezeResult:
    """What was frozen, and the local evidence it was frozen on.

    ``repositories`` is the pinned sibling candidate set (empty for the
    ordinary single-repository stage): each declared sibling resolved to the
    exact commit its branch was at, verified clean.

    ``push_detail`` keeps its name for callers that read it; it describes
    the local check the candidate passed, since nothing is pushed or read
    from a remote.
    """

    candidate_sha: str
    branch: str
    push_detail: str
    ignored_dirty_paths: tuple[str, ...] = field(default_factory=tuple)
    repositories: tuple[CandidateRepository, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AcceptanceResult:
    """The exact candidate set that was accepted: the primary commit plus
    every pinned sibling candidate, each re-verified as still current."""

    candidate_sha: str
    branch: str
    repositories: tuple[CandidateRepository, ...] = field(default_factory=tuple)


class UnauthorizedRemoteMovementError(AcceptanceError):
    """The run this stage belongs to is paused on a recorded remote incident
    (unexplained movement of its legacy pinned ref, or a candidate already on
    it -- see :mod:`agent_sparring.remote_pin`): nothing is frozen or
    accepted until a person has resolved that pause explicitly.

    Only the run's own recorded pause is read; no remote is consulted, and
    resolving the pause is never review or acceptance."""

    def __init__(self, movement: RemotePause) -> None:
        if isinstance(movement, PushedCandidateUnauthorized):
            action = (
                "authorize exactly that commit (sparring authorize-pushed-candidate) once you "
                "have looked; a run-level push grant does not"
            )
        else:
            action = "acknowledge the movement (sparring acknowledge-remote-movement) once you have looked"
        super().__init__(
            f"refusing: {movement.kind}: {movement.describe}. Nothing was frozen or accepted; "
            f"{action}"
        )
        self.movement = movement


def _read_state(stage: Stage) -> StageState:
    try:
        return stage.read_state()
    except StageError as exc:
        raise AcceptanceError(f"cannot read stage state: {exc}") from exc


def _runs_positioned_at(sparring_dir: Path, stage_id: str) -> set[str]:
    """The run keys whose engine-written run state
    (``<sparring_dir>/plans/<run>.json``) is positioned at ``stage_id``.

    A run state that cannot be read refuses: it cannot be shown not to own
    the stage."""

    plans = Path(sparring_dir) / "plans"
    if not plans.is_dir():
        return set()
    owners: set[str] = set()
    for path in sorted(plans.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AcceptanceError(
                f"cannot read run state {path} to establish who owns stage {stage_id!r}: {exc}"
            ) from exc
        if isinstance(payload, dict) and payload.get("current_stage") == stage_id:
            owners.add(str(payload.get("run") or path.stem))
    return owners


def _managed_owner(sparring_dir: Path) -> str | None:
    """The run whose managed worktree holds ``sparring_dir``, from the
    engine's managed record (in the common Git directory, outside anything
    an agent's project directory can rewrite); ``None`` outside a managed
    worktree. A record that cannot be read refuses."""

    try:
        top = managed_run.worktree_top(sparring_dir)
        record = managed_run.record_for_worktree(top, top)
    except managed_run.ManagedRunError as exc:
        if exc.code == "not_a_repository":
            return None
        raise AcceptanceError(
            f"refusing: cannot read the managed-run records to establish who owns {sparring_dir} "
            f"({exc}); nothing was frozen or accepted"
        ) from exc
    # A creation_failed record never claimed its path (see
    # managed_run.refuse_unmanaged_here).
    if record is None or record.lifecycle == "creation_failed":
        return None
    return record.run_key


def _managed_problems(sparring_dir: Path, stage_id: str, run: str, claimed: str | None) -> list[str]:
    """How the project directory contradicts managed run ``run`` owning
    ``stage_id``: the stage's ``run``, the run state's existence, shape,
    declared ``run`` and ``current_stage``, and any other run state
    positioned at the stage. A
    run state that cannot be read, or is not a JSON object, is one: it
    cannot be shown to agree."""

    problems: list[str] = []
    if claimed != run:
        problems.append(
            f"the stage records {'run ' + repr(claimed) if claimed else 'no run'} as its owner"
        )
    plans = Path(sparring_dir) / "plans"
    own = plans / f"{run}.json"
    if not own.is_file():
        problems.append(f"the run state {own} is gone")
    paths = sorted(plans.glob("*.json")) if plans.is_dir() else []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError: malformed JSON or bytes that are not UTF-8.
            problems.append(f"the run state {path} cannot be read ({exc})")
            continue
        if not isinstance(payload, dict):
            problems.append(f"the run state {path} is not a JSON object")
            continue
        current = payload.get("current_stage")
        if path == own:
            # A run state recorded before run instances names no run of its
            # own; one that names a run must name this one.
            declared = payload.get("run")
            if declared is not None and declared != run:
                problems.append(f"the run state {path} declares run {declared!r}")
            if current != stage_id:
                problems.append(f"the run state is positioned at {current!r}, not at this stage")
        elif current == stage_id:
            problems.append(
                f"the run state of {payload.get('run') or path.stem} is positioned at it too"
            )
    return problems


def _owning_run(stage: Stage) -> str | None:
    """The run that owns ``stage``, from engine records.

    In a managed worktree the owner is the run the engine's managed record
    names, and the stage's ``run``, that run's state (present, positioned at
    this stage) and every other run state must agree with it; any
    contradiction refuses with ``stage_owner_mismatch`` (audit R-4).

    Elsewhere, the stage's own ``state.json`` names its owner, but an emptied
    or rewritten ``run`` field must not make an owned stage look hand-driven
    (and so skip its run's accepted history and recorded pause): every run
    whose state is positioned at this stage owns it too, and a claim that
    contradicts them refuses. ``None`` only when neither names a run."""

    claimed = _read_state(stage).run
    sparring_dir = Path(stage.directory).parent.parent
    managed = _managed_owner(sparring_dir)
    if managed is not None:
        problems = _managed_problems(sparring_dir, stage.stage_id, managed, claimed)
        if problems:
            raise StageOwnerMismatchError(stage.stage_id, managed, problems)
        return managed
    positioned = _runs_positioned_at(sparring_dir, stage.stage_id)
    if claimed:
        if positioned and claimed not in positioned:
            raise AcceptanceError(
                f"refusing: stage {stage.stage_id!r} records run {claimed!r} as its owner, but "
                f"the run state positioned at it is {', '.join(sorted(positioned))}; nothing was "
                "frozen or accepted"
            )
        return claimed
    if len(positioned) > 1:
        raise AcceptanceError(
            f"refusing: runs {', '.join(sorted(positioned))} are all positioned at stage "
            f"{stage.stage_id!r}; nothing was frozen or accepted"
        )
    return next(iter(positioned), None)


@dataclass(frozen=True)
class RunRecords:
    """What the owning run's engine state says that acceptance needs, all of
    it read locally: the remote incident it is paused on, if any; the
    trusted snapshots of each participating repository's configuration it
    holds Git to; and everything it has accepted, per repository."""

    run: str
    pause: RemotePause | None
    snapshots: RunSnapshots
    accepted: tuple[AcceptedCandidate, ...]
    repo_root: Path

    @property
    def snapshot(self) -> tuple[tuple[str, str], ...] | None:
        return self.snapshots.primary

    def require_sibling_snapshot(self, root: Path, repository: CandidateRepository) -> None:
        """Refuse an operation on a sibling the run holds no trusted
        snapshot for: it would otherwise run unguarded (I-L4)."""

        if self.snapshots.for_sibling(root) is None:
            raise TrustedConfigMissingError(
                missing_message(self.repo_root, self.run, what=f"sibling repository {_describe(repository)}")
            )


class TrustedConfigMissingError(AcceptanceError):
    """The owning run holds no trusted configuration snapshot for a
    participating repository, so its Git commands cannot be held to one."""

    code = TRUSTED_CONFIG_MISSING


def run_records(stage: Stage, repo_root: Path) -> RunRecords | None:
    """The records of the run that owns ``stage`` (see :func:`_owning_run`),
    read from that run's engine-written state and the stages it owns --
    never from an agent-written artifact, and never from a remote. ``None``
    for a stage no run owns.

    An owned stage whose run holds no recoverable trusted snapshot of the
    primary repository refuses (``trusted_config_missing``): its operations
    are never run unguarded."""

    run = _owning_run(stage)
    if not run:
        return None
    sparring_dir = Path(stage.directory).parent.parent
    path = sparring_dir / "plans" / f"{run}.json"
    payload: Mapping[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcceptanceError(
                f"refusing: cannot read the state of run {run} ({exc}); nothing was frozen or "
                "accepted"
            ) from exc
        if not isinstance(loaded, dict):
            raise AcceptanceError(
                f"refusing: the state of run {run} at {path} is not a JSON object; nothing was "
                "frozen or accepted"
            )
        payload = loaded
    awaiting = payload.get("awaiting")
    pause: RemotePause | None = None
    if isinstance(awaiting, Mapping) and awaiting.get("kind") in REMOTE_PAUSE_KINDS:
        try:
            pause = remote_pause_from_dict(awaiting)
        except (GitContextError, KeyError, TypeError, ValueError) as exc:
            raise AcceptanceError(
                f"refusing: run {run} is paused on a recorded remote incident that cannot be "
                f"read ({exc}); nothing was frozen or accepted"
            ) from exc
    try:
        accepted = run_accepted(sparring_dir, run)
    except RunHistoryError as exc:
        raise AcceptanceError(
            f"refusing: the accepted history of run {run} cannot be read ({exc}), so it cannot "
            "be shown to be preserved; nothing was frozen or accepted"
        ) from exc
    try:
        snapshots = trusted_snapshots.from_run_state(payload)
    except trusted_snapshots.SnapshotError as exc:
        raise TrustedConfigMissingError(
            f"refusing: {TRUSTED_CONFIG_MISSING}: the trusted configuration snapshots of run "
            f"{run} cannot be read ({exc}); nothing was frozen or accepted"
        ) from exc
    if snapshots.primary is None:
        raise TrustedConfigMissingError(
            missing_message(Path(repo_root), run, what="the primary repository")
        )
    return RunRecords(
        run=run, pause=pause, snapshots=snapshots, accepted=accepted, repo_root=Path(repo_root)
    )


@contextlib.contextmanager
def _run_guard(stage: Stage, repo_root: Path) -> Iterator[RunRecords | None]:
    """Hold this operation's Git invocations -- in the primary repository
    and in each sibling -- to the owning run's trusted snapshots. An owned
    stage always has the primary's (see :func:`run_records`)."""

    records = run_records(stage, repo_root)
    if records is None:
        yield records
        return
    with trusted_snapshots.guard(Path(repo_root), records.snapshots):
        yield records


def _require_no_pause(records: RunRecords | None) -> None:
    """Refuse while the owning run is paused on a recorded remote incident."""

    if records is not None and records.pause is not None:
        raise UnauthorizedRemoteMovementError(records.pause)


def _require_history(
    records: RunRecords | None,
    stage_id: str,
    root: Path,
    candidate_sha: str,
    stage_base: str | None,
    repository: CandidateRepository | None = None,
) -> None:
    """I-L2b for one repository: its stage base and every candidate the run
    accepted in it are ancestors of (or equal to) ``candidate_sha``.

    ``repository`` is ``None`` for the primary repository. A commit that is
    not present in the repository at all cannot be shown to be an
    ancestor, and refuses too."""

    accepted = records.accepted if records is not None else ()
    for what, sha in required_ancestors(
        accepted, stage_id=stage_id, stage_base=stage_base, repository=repository
    ):
        if not is_ancestor(root, sha, candidate_sha):
            raise RunHistoryRewrittenError(
                describe_repository(repository), sha, what, candidate_sha
            )


def require_primary_history(
    stage: Stage,
    repo_root: Path,
    candidate_sha: str,
    *,
    earlier: tuple[tuple[str, str], ...] = (),
) -> None:
    """I-L2b in the primary repository, for a caller outside the freeze and
    accept paths (an independent review being entered): the stage's base,
    every candidate its owning run accepted here, and each ``earlier``
    ``(stage id, commit)`` are ancestors of ``candidate_sha``.

    Raises :class:`RunHistoryRewrittenError` (an :class:`AcceptanceError`)
    naming the missing commit."""

    with _run_guard(stage, repo_root) as records:
        _require_no_pause(records)
        _require_history(records, stage.stage_id, repo_root, candidate_sha, _read_state(stage).base_sha)
        for stage_id, sha in earlier:
            if not is_ancestor(repo_root, sha, candidate_sha):
                raise RunHistoryRewrittenError(
                    describe_repository(None),
                    sha,
                    f"the candidate accepted by stage {stage_id!r}",
                    candidate_sha,
                )


def _require_branch(repo_root: Path, expected_branch: str, *, operation: str) -> str:
    if not expected_branch or not expected_branch.strip():
        raise AcceptanceError(
            f"expected_branch is required to {operation}; the candidate is the exact HEAD "
            "of that branch"
        )
    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise CandidateNotOnBranchError(f"{exc}; refusing to {operation}") from exc
    if branch != expected_branch:
        raise CandidateNotOnBranchError(
            f"expected branch {expected_branch!r} but {repo_root} is on "
            f"{branch!r}; refusing to {operation} from the wrong branch"
        )
    return branch


def _local_detail(sha: str, branch: str) -> str:
    return f"local: {sha} is the exact HEAD of {branch}, with a clean working tree"


#: The verdicts after which a person's explicit acceptance is the decision:
#: the reviewer handed it to them, for exactly what was reviewed.
_HANDED_TO_A_PERSON = (RoutingAction.ESCALATE.value, RoutingAction.NEEDS_YOU.value)


def record_review(stage: Stage, repo_root: Path | None, action: RoutingAction) -> None:
    """Record, beside the verdict just written, exactly what it was given
    over: the primary ``HEAD`` and whether its working tree then held
    nothing that commit does not represent, and each declared sibling's
    ``HEAD`` and whether its working tree was clean (see
    :class:`~agent_sparring.stage.ReviewRecord`). Engine-written, from the
    repositories themselves -- never from the reviewer's words.

    Fails closed. A new verdict always replaces the evidence of the one
    before it: the earlier record is invalidated first, and when what this
    verdict was over cannot be captured (``repo_root`` unknown, a
    repository unreadable) the record says so and can authorize nothing --
    neither it nor any earlier READY. A stage with no state has nothing to
    record into."""

    try:
        state = stage.read_state()
    except StageError:
        return
    # Invalidate before capturing anything: if the capture below fails in
    # any way, no earlier review evidence survives this verdict.
    state.review = ReviewRecord(action=action.value, head_sha=None, clean=False)
    stage.write_state(state)
    if repo_root is None:
        return
    try:
        head = resolve_commit(repo_root, "HEAD", label="reviewed HEAD")
        blocking, _ignored = _partition_dirty(repo_root, stage)
        siblings = []
        dirty_siblings = []
        for repository in state.repositories:
            root = sibling_root(repo_root, repository)
            siblings.append(
                SiblingPin(
                    name=repository.name,
                    head_sha=resolve_commit(root, "HEAD", label=f"{repository.name} HEAD"),
                )
            )
            if dirty_entries(root, all_untracked=True):
                dirty_siblings.append(repository.name)
    except (GitContextError, AcceptanceError, OSError):
        # The invalidated record written above stands: it authorizes nothing.
        return
    state.review = ReviewRecord(
        action=action.value,
        head_sha=head,
        clean=not blocking,
        repositories=tuple(siblings),
        dirty_repositories=tuple(dirty_siblings),
    )
    stage.write_state(state)


def _review_covers(review: ReviewRecord, sha: str, siblings: tuple[CandidateRepository, ...]) -> bool:
    if review.head_sha is None or not review.clean or review.head_sha != sha:
        return False
    reviewed = {entry.name: entry.head_sha for entry in review.repositories}
    return all(
        repository.candidate_sha is not None
        and reviewed.get(repository.name) == repository.candidate_sha
        and repository.name not in review.dirty_repositories
        for repository in siblings
    )


def _require_verdict_at(state: StageState, stage: Stage, sha: str) -> None:
    """The review the engine recorded must be over exactly ``sha`` -- and,
    for a cross-repository stage, over exactly the frozen sibling
    candidates (I-L2). Evidence is one of:

    - the stage's review record (:func:`record_review`): READY over ``sha``
      with a clean tree -- or ESCALATE / NEEDS_YOU there, after which the
      reviewer has handed the decision to a person and their explicit
      ``accept-candidate`` is that decision, for exactly what was reviewed;
    - the loop's ``next_turn`` marker (see :mod:`agent_sparring.next_turn`):
      ``finalization`` over ``sha`` as a commit (READY), or ``sparring``
      with ESCALATE / NEEDS_YOU recorded over it.

    No evidence at all -- a stage nobody reviewed, or one whose review
    predates this record -- refuses too: acceptance is never given to an
    unreviewed candidate."""

    review = state.review
    if review is not None:
        if review.action == RoutingAction.READY.value or review.action in _HANDED_TO_A_PERSON:
            if _review_covers(review, sha, state.repositories):
                return
            if review.head_sha is None:
                over = "a candidate that could not be recorded"
            elif review.head_sha != sha or not review.clean:
                over = f"{review.head_sha}{'' if review.clean else ' with uncommitted work'}"
            else:
                over = (
                    f"{review.head_sha} with other sibling commits or uncommitted sibling work"
                    + (f" ({', '.join(review.dirty_repositories)})" if review.dirty_repositories else "")
                )
            raise VerdictNotAtCandidateError(
                f"refusing: {VERDICT_NOT_AT_CANDIDATE}: stage {stage.stage_id!r} would accept "
                f"{sha}, but the reviewer's recorded {review.action} was over {over}. "
                "Acceptance needs the review at that exact candidate; have it reviewed. "
                "Nothing was accepted"
            )
        raise VerdictNotAtCandidateError(
            f"refusing: {VERDICT_NOT_AT_CANDIDATE}: stage {stage.stage_id!r} would accept {sha}, "
            f"but the reviewer's last recorded verdict is {review.action}, not a READY. Nothing "
            "was accepted"
        )
    if state.next_turn is None:
        raise VerdictNotAtCandidateError(
            f"refusing: {VERDICT_NOT_AT_CANDIDATE}: stage {stage.stage_id!r} would accept {sha}, "
            "but no review of it is recorded: acceptance needs the reviewer's READY at that "
            "exact commit (run-sparring, run-loop or a managed run records it). Nothing was "
            "accepted"
        )
    reviewed = state.next_turn_candidate
    at_sha = (
        reviewed is not None
        and reviewed.kind == "commit"
        and reviewed.head_sha == sha
        and all(
            repository.candidate_sha is not None
            and {entry.name: entry.head_sha for entry in reviewed.repositories}.get(repository.name)
            == repository.candidate_sha
            for repository in state.repositories
        )
    )
    if state.next_turn == NEXT_TURN_FINALIZATION and at_sha:
        return
    handed_to_a_person = False
    if state.next_turn == NEXT_TURN_SPARRING and not state.implementation_unreviewed:
        outcome = read_recorded_outcome(stage)
        handed_to_a_person = outcome is not None and outcome.action in (
            RoutingAction.ESCALATE,
            RoutingAction.NEEDS_YOU,
        )
    if handed_to_a_person and at_sha:
        return
    if handed_to_a_person:
        recorded = (
            "the reviewer handed the decision to a person over "
            f"{reviewed.head_sha if reviewed is not None else 'no recorded candidate'}"
        )
    elif state.next_turn != NEXT_TURN_FINALIZATION:
        recorded = f"the stage's last recorded step is {state.next_turn!r}, not a READY"
    elif reviewed is None:
        recorded = "the recorded READY names no candidate"
    elif reviewed.kind != "commit":
        recorded = f"the recorded READY was over uncommitted work on {reviewed.head_sha}"
    else:
        recorded = f"the recorded READY is over {reviewed.head_sha}"
    raise VerdictNotAtCandidateError(
        f"refusing: {VERDICT_NOT_AT_CANDIDATE}: stage {stage.stage_id!r} would accept {sha}, "
        f"but {recorded}. Acceptance needs the reviewer's READY at that exact commit; have "
        "it reviewed. Nothing was accepted"
    )


def _stage_artifact_allowlist(repo_root: Path, stage: Stage) -> frozenset[str]:
    """This stage's own artifact files, as exact repo-relative posix paths.

    Computed from ``stage.directory`` itself (``<sparring_dir>/stages/
    <stage_id>/``), never from ``sparring_dir`` by prefix -- so the
    allowlist is always exactly these five filenames for this one stage,
    regardless of where ``sparring_dir`` happens to live (including the
    degenerate case of ``sparring_dir == repo_root``, which must not turn
    into "everything is exempt").
    """

    try:
        rel_dir = stage.directory.resolve().relative_to(repo_root.resolve())
    except ValueError:
        # The stage directory is outside the repository entirely -- nothing
        # can match it, so the allowlist is correctly empty rather than an
        # error here (the dirty check below will simply block on anything).
        return frozenset()
    return frozenset((rel_dir / name).as_posix() for name in STAGE_ARTIFACT_FILENAMES)


def _prompts_dir_relative(repo_root: Path, stage: Stage) -> str | None:
    """This stage's ``prompts/`` directory, as a repo-relative posix path."""

    try:
        rel_dir = stage.directory.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    return (rel_dir / PROMPTS_DIRNAME).as_posix()


def _is_allowed(path: str, allowed: frozenset[str], prompts_dir: str | None) -> bool:
    """Is one repo-relative path a workflow artifact of this stage?

    Either an exact allowlisted filename, or a captured prompt directly
    inside this stage's own ``prompts/`` directory. The second case is
    still not a subtree rule: the directory is computed from
    ``stage.directory``, and the filename itself must match one this engine
    writes (see :func:`agent_sparring.prompt_capture.is_prompt_artifact`),
    so a stray file dropped into ``prompts/`` -- or anything at all in a
    nested directory below it -- still blocks the freeze.
    """

    if path in allowed:
        return True
    if prompts_dir is None:
        return False
    prefix = f"{prompts_dir}/"
    if not path.startswith(prefix):
        return False
    name = path[len(prefix) :]
    return "/" not in name and is_prompt_artifact(name)


def _entry_is_exempt(
    entry: DirtyEntry, allowed: frozenset[str], prompts_dir: str | None
) -> bool:
    """Is this one status entry entirely accounted for by the allowlist?

    A plain add/modify/delete is exempt only if its path is allowed. A
    rename/copy is exempt only if *both* the destination and the source are
    allowed -- exempting only the destination would let a rename from a
    real, non-workflow file (an unrepresented deletion elsewhere) hide
    behind a workflow-artifact-looking destination name.
    """

    if not _is_allowed(entry.path, allowed, prompts_dir):
        return False
    if entry.old_path is not None and not _is_allowed(entry.old_path, allowed, prompts_dir):
        return False
    return True


def _describe_entry(entry: DirtyEntry) -> str:
    """One status entry as a refusal names it."""

    return (
        f"{entry.path} (renamed from {entry.old_path})"
        if entry.old_path is not None
        else entry.path
    )


def _partition_dirty(
    repo_root: Path, stage: Stage
) -> tuple[tuple[DirtyEntry, ...], tuple[DirtyEntry, ...]]:
    """Split the working tree's status entries into (blocking, ignored).

    Only this stage's own artifact files (see ``STAGE_ARTIFACT_FILENAMES``)
    are ignored: agent_sparring itself rewrites those on every stage/
    sparring turn, so a project that neither commits nor gitignores them
    would otherwise be unable to freeze anything at all. Everything else —
    any unrepresented source, test, config, or *other* file under
    ``sparring_dir`` (another stage's artifacts, ``project.toml``,
    ``PROJECT.md``, or anything else a caller happened to name
    ``sparring_dir``) — blocks the freeze.

    Uses ``--untracked-files=all`` (via
    :func:`~agent_sparring.git_context.dirty_entries`) rather than git's
    default collapsed directory reporting: a brand-new, wholly-untracked
    stage directory must be checked file-by-file so a non-workflow file
    dropped alongside the real artifacts is still caught, instead of
    disappearing inside one collapsed ``?? .sparring/`` entry.
    """

    try:
        entries = dirty_entries(repo_root, all_untracked=True)
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc

    allowed = _stage_artifact_allowlist(repo_root, stage)
    prompts_dir = _prompts_dir_relative(repo_root, stage)

    blocking: list[DirtyEntry] = []
    ignored: list[DirtyEntry] = []
    for entry in entries:
        (ignored if _entry_is_exempt(entry, allowed, prompts_dir) else blocking).append(entry)
    return tuple(blocking), tuple(ignored)


# -- sibling repositories -----------------------------------------------------


def sibling_root(repo_root: Path, repository: CandidateRepository) -> Path:
    """Where a declared sibling repository lives: its ``path`` resolved
    against the primary ``repo_root`` when relative."""

    declared = Path(repository.path)
    return declared if declared.is_absolute() else (Path(repo_root) / declared).resolve()


def _describe(repository: CandidateRepository) -> str:
    return f"{repository.name!r} ({repository.path})"


def _freeze_siblings(
    repo_root: Path,
    repositories: tuple[CandidateRepository, ...],
    records: RunRecords | None = None,
    stage_id: str = "",
) -> tuple[CandidateRepository, ...]:
    """Resolve and pin every declared sibling candidate, or refuse.

    Each sibling gets the same treatment the primary repository already
    gets, in the same order: it must be on the declared branch, resolve
    HEAD, hold no dirty changes at all (a sibling holds none of *this*
    stage's workflow artifacts, so nothing is exempt there), and have its
    own history preserved -- its recorded stage base and every candidate the
    owning run (``records``) accepted in it are ancestors of that HEAD (see
    :mod:`agent_sparring.run_history`). Nothing is read from a remote. A
    sibling that declares a ``candidate_sha`` must be exactly at it -- that
    is the reviewed commit asserted by whoever declared it, and silently
    pinning a different one would defeat the point.

    Returns the same repositories with ``candidate_sha`` filled in with the
    resolved HEAD. Raises :class:`AcceptanceError` on the first refusal;
    nothing is written by this function.
    """

    pinned: list[CandidateRepository] = []
    for repository in repositories:
        root = sibling_root(repo_root, repository)
        if not (root / ".git").exists():
            raise AcceptanceError(
                f"sibling repository {_describe(repository)} does not resolve to a git "
                f"repository at {root}; refusing to freeze an incomplete candidate set"
            )
        if records is not None:
            # Before any Git command runs in the sibling: an owned stage's
            # sibling is only ever operated on under its own snapshot.
            records.require_sibling_snapshot(root, repository)
        try:
            branch = current_branch(root)
        except GitContextError as exc:
            raise CandidateNotOnBranchError(
                f"sibling repository {_describe(repository)}: {exc}"
            ) from exc
        if branch != repository.branch:
            raise CandidateNotOnBranchError(
                f"sibling repository {_describe(repository)} is on {branch!r} but its "
                f"candidate belongs to {repository.branch!r}; refusing to freeze a "
                "candidate set built from the wrong branch"
            )
        try:
            head = resolve_commit(root, "HEAD", label=f"{repository.name} candidate")
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if repository.candidate_sha is not None and repository.candidate_sha != head:
            raise CandidateNotOnBranchError(
                f"sibling repository {_describe(repository)} was declared at "
                f"{repository.candidate_sha} but {branch} is now at {head}; refusing to "
                "freeze a candidate set that is not the one that was reviewed"
            )
        try:
            dirty = dirty_entries(root, all_untracked=True)
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if dirty:
            listed = ", ".join(entry.path for entry in dirty)
            raise CandidateNotOnBranchError(
                f"refusing to freeze {head} as the candidate of sibling repository "
                f"{_describe(repository)}: its working tree holds changes that commit "
                f"does not represent ({listed}). Commit or discard them first"
            )
        _require_history(records, stage_id, root, head, repository.base_sha, repository)
        pinned.append(replace(repository, candidate_sha=head))
    return tuple(pinned)


def _verify_siblings(
    repo_root: Path,
    repositories: tuple[CandidateRepository, ...],
    records: RunRecords | None = None,
    stage_id: str = "",
) -> None:
    """Re-check that every pinned sibling candidate is still exactly what was
    frozen, mirroring the primary repository's stale-candidate check, and
    that its history is still preserved.

    Raises :class:`StaleCandidateError` when a sibling moved on, and plain
    :class:`AcceptanceError` when it was never pinned, is on the wrong
    branch, or its worktree no longer *is* that commit.
    """

    for repository in repositories:
        if not is_full_sha(repository.candidate_sha):
            raise AcceptanceError(
                f"sibling repository {_describe(repository)} has no pinned candidate "
                f"({repository.candidate_sha!r}); freeze the candidate set again rather "
                "than guessing which commit was reviewed"
            )
        root = sibling_root(repo_root, repository)
        if records is not None:
            records.require_sibling_snapshot(root, repository)
        try:
            branch = current_branch(root)
            head = resolve_commit(root, "HEAD", label=f"{repository.name} HEAD")
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if branch != repository.branch:
            raise CandidateNotOnBranchError(
                f"sibling repository {_describe(repository)} is on {branch!r} but its "
                f"frozen candidate belongs to {repository.branch!r}; refusing acceptance"
            )
        if head != repository.candidate_sha:
            raise StaleCandidateError(
                f"the frozen candidate of sibling repository {_describe(repository)} is "
                f"{repository.candidate_sha}, but {branch} in {root} is now at {head}; "
                "refusing acceptance as stale. The complete reviewed candidate set must "
                "still be the one that was reviewed; nothing was accepted"
            )
        try:
            dirty = dirty_entries(root, all_untracked=True)
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if dirty:
            listed = ", ".join(entry.path for entry in dirty)
            raise CandidateNotOnBranchError(
                f"refusing to accept {repository.candidate_sha} for sibling repository "
                f"{_describe(repository)}: its working tree holds changes that commit does "
                f"not represent ({listed}), even though HEAD still matches"
            )
        _require_history(records, stage_id, root, head, repository.base_sha, repository)


def unrepresented_dirty_entries(repo_root: Path, stage: Stage) -> tuple[DirtyEntry, ...]:
    """The working-tree status entries ``stage``'s own commit does not
    represent.

    The exact check :func:`freeze_candidate` and :func:`accept_candidate`
    make, exposed so the independent-review lifecycle
    (:mod:`agent_sparring.review`) and the recovery operation
    (:mod:`agent_sparring.recovery`) ask the same question of a worktree and
    get the same answer, rather than each growing its own idea of "clean".
    Empty means the worktree is exactly its commit, apart from this stage's
    own workflow artifacts.
    """

    blocking, _ignored = _partition_dirty(repo_root, stage)
    return blocking


def unrepresented_dirty(repo_root: Path, stage: Stage) -> tuple[str, ...]:
    """:func:`unrepresented_dirty_entries`, described the way a refusal
    names each entry."""

    return tuple(
        _describe_entry(entry) for entry in unrepresented_dirty_entries(repo_root, stage)
    )


def recorded_ready_problems(
    stage: Stage, repo_root: Path, *, expected_branch: str, candidate_sha: str
) -> tuple[tuple[int, str], ...]:
    """Why ``candidate_sha`` is not, right now, exactly what the stage's
    recorded READY was over, as ``(condition, detail)`` pairs: empty when it
    is. Read-only, and held to the owning run's trusted snapshots.

    For the legacy migration's conditions (see
    :func:`agent_sparring.plan.resume_plan`): ``2`` -- the engine-recorded
    review is a READY over ``candidate_sha``, which is the ``HEAD`` of
    ``expected_branch``; ``3`` -- the working tree is clean apart from the
    stage's own artifacts, and every declared sibling is clean and at
    exactly the commit that review was over. Acceptance itself re-checks all
    of it (:func:`freeze_candidate`, :func:`accept_candidate`)."""

    problems: list[tuple[int, str]] = []
    try:
        state = _read_state(stage)
        with _run_guard(stage, repo_root):
            try:
                branch = current_branch(repo_root)
                head = resolve_commit(repo_root, "HEAD", label="HEAD")
            except GitContextError as exc:
                return ((2, f"{repo_root} cannot be read: {exc}"),)
            if branch != expected_branch or head != candidate_sha:
                problems.append((
                    2, f"{repo_root} is at {head} on {branch!r}, not the paused candidate "
                    f"{candidate_sha} as the HEAD of {expected_branch!r}",
                ))
            blocking, _ignored = _partition_dirty(repo_root, stage)
            if blocking:
                problems.append((
                    3, "the working tree holds changes the candidate does not represent ("
                    + ", ".join(_describe_entry(entry) for entry in blocking) + ")",
                ))
            reviewed_siblings: dict[str, str | None] = {}
            if state.review is not None:
                reviewed_siblings = {entry.name: entry.head_sha for entry in state.review.repositories}
            elif state.next_turn_candidate is not None:
                reviewed_siblings = {
                    entry.name: entry.head_sha for entry in state.next_turn_candidate.repositories
                }
            at: list[CandidateRepository] = []
            for repository in state.repositories:
                root = sibling_root(repo_root, repository)
                try:
                    sibling_head = resolve_commit(root, "HEAD", label=f"{repository.name} HEAD")
                    dirty = dirty_entries(root, all_untracked=True)
                except (GitContextError, AcceptanceError, OSError) as exc:
                    problems.append((3, f"sibling {_describe(repository)} cannot be read: {exc}"))
                    continue
                recorded = repository.candidate_sha or reviewed_siblings.get(repository.name)
                if sibling_head != recorded:
                    problems.append((
                        3, f"sibling {_describe(repository)} is at {sibling_head}, not its recorded "
                        f"candidate {recorded or '(none recorded)'}",
                    ))
                if dirty:
                    problems.append((3, f"sibling {_describe(repository)} has uncommitted changes"))
                at.append(replace(repository, candidate_sha=sibling_head))
            try:
                _require_verdict_at(replace(state, repositories=tuple(at)), stage, candidate_sha)
            except AcceptanceError as exc:
                problems.append((2, str(exc)))
    except AcceptanceError as exc:
        problems.append((2, f"the recorded review cannot be checked: {exc}"))
    return tuple(problems)


def pin_sibling_candidates(
    repo_root: Path,
    repositories: tuple[CandidateRepository, ...],
    *,
    stage: Stage | None = None,
) -> tuple[CandidateRepository, ...]:
    """Resolve and pin every declared sibling candidate, or refuse.

    The public name for the freeze's own sibling handling, so an
    independent-review stage pins the sibling candidates it is reviewing
    under exactly the checks a freeze applies (see :func:`_freeze_siblings`)
    instead of a second, looser version of them. ``stage``, when given,
    supplies its owning run's accepted history, which each sibling's
    candidate must preserve, and its trusted snapshots, which every Git
    command here is held to.
    """

    if stage is None:
        return _freeze_siblings(repo_root, repositories)
    with _run_guard(stage, repo_root) as records:
        return _freeze_siblings(repo_root, repositories, records, stage.stage_id)


def verify_sibling_candidates(
    repo_root: Path,
    repositories: tuple[CandidateRepository, ...],
    *,
    stage: Stage | None = None,
) -> None:
    """Re-check that every pinned sibling candidate is still what was pinned.

    The public name for :func:`_verify_siblings`, for the same reason as
    :func:`pin_sibling_candidates`.
    """

    if stage is None:
        _verify_siblings(repo_root, repositories)
        return
    with _run_guard(stage, repo_root) as records:
        _verify_siblings(repo_root, repositories, records, stage.stage_id)


def freeze_candidate(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    *,
    expected_branch: str,
) -> FreezeResult:
    """Freeze the repository's exact current commit as this stage's candidate.

    Always freezes the resolved current ``HEAD`` — there is no revision
    parameter, because "the candidate" means the state that actually exists
    right now, not a revision someone typed.

    Runs inside :func:`~agent_sparring.concurrency.worktree_lock` -- the
    same lock :func:`~agent_sparring.stage_agent.run_stage_agent` holds for
    an implementation turn -- so a freeze cannot interleave with a
    concurrent stage-agent turn on the same worktree. A contended lock
    raises :class:`AcceptanceError` without touching ``state.json``.

    Checks, in order, all before anything is written:

    1. the stage's state is readable and is not already ACCEPTED;
    2. the worktree is on ``expected_branch``;
    3. ``HEAD`` resolves to a full 40-hex commit;
    4. the worktree holds no unrepresented dirty changes outside this
       stage's own artifact files (see :func:`_partition_dirty`) --
       ``sparring_dir`` itself is not treated as a blanket-exempt subtree;
    5. the owning run, if any, is not paused on a recorded remote incident;
    6. history is preserved (I-L2b): the stage's ``base_sha`` and every
       candidate the owning run already accepted in this repository are
       ancestors of that commit;
    7. every declared sibling passes the same checks in its own repository.

    No remote and no remote configuration is read: a candidate is local
    (I-L2), and publishing one is a separate, explicit action.

    Only then are ``candidate_sha`` and ``status = FROZEN`` recorded. The
    stage's ``base_sha`` and both session ids are preserved untouched.

    Re-freezing is allowed and is not an error: the same SHA can be frozen
    again after its evidence changed (no dummy commit needed), and a stage
    whose HEAD legitimately moved on can be frozen again at the new commit.

    Raises :class:`AcceptanceError` if any check above fails.
    """

    # sparring_dir is accepted for call-signature/API stability (and because
    # it is what a caller naturally has on hand) but is not used to compute
    # the workflow-artifact exemption below -- see _stage_artifact_allowlist,
    # which is derived from stage.directory only, precisely so that naming
    # some other tree "sparring_dir" (up to and including sparring_dir ==
    # repo_root) can never widen what is exempt from the dirty-tree check.
    del sparring_dir

    try:
        with worktree_lock(repo_root), _run_guard(stage, repo_root) as records:
            frozen = _freeze_candidate_locked(
                stage, repo_root, expected_branch=expected_branch, records=records
            )
    except WorktreeLockError as exc:
        error = AcceptanceError(
            f"cannot freeze a candidate for stage {stage.stage_id!r}: {exc}"
        )
        _emit_refusal(stage, "freeze", error)
        raise error from exc
    except AcceptanceError as exc:
        _emit_refusal(stage, "freeze", exc)
        raise
    stage.activity_log().emit("gate", "candidate.frozen", sha=frozen.candidate_sha)
    return frozen


def _emit_refusal(stage: Stage, operation: str, exc: AcceptanceError) -> None:
    """Mirror a refusal into the observational activity stream.

    Called only from ``except`` blocks that then re-raise the *original*
    exception unchanged; ``ActivityLog.emit`` itself never raises, so
    telemetry can neither mask nor replace the refusal. The summary is a
    fixed orchestration-authored phrase chosen by exception *type* -- the
    exception message itself (paths, remote details) is never copied into
    the log.
    """

    if isinstance(exc, StaleCandidateError):
        summary = f"{operation} refused: stale candidate"
    elif exc.code is not None:
        summary = f"{operation} refused: {exc.code}"
    else:
        summary = f"{operation} refused"
    stage.activity_log().emit("gate", "gate.refused", summary=summary)


def _freeze_candidate_locked(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
    records: RunRecords | None = None,
) -> FreezeResult:
    state = _read_state(stage)

    if state.status is StageStatus.ACCEPTED:
        raise AcceptanceError(
            f"stage {stage.stage_id!r} is already ACCEPTED at "
            f"{state.candidate_sha}; acceptance is terminal for a stage. "
            "Further work belongs to a new stage rather than re-freezing "
            "this one."
        )

    branch = _require_branch(repo_root, expected_branch, operation="freeze a candidate")

    try:
        candidate_sha = resolve_commit(repo_root, "HEAD", label="candidate")
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc

    blocking, ignored = _partition_dirty(repo_root, stage)
    if blocking:
        listed = ", ".join(_describe_entry(entry) for entry in blocking)
        raise CandidateNotOnBranchError(
            f"refusing to freeze {candidate_sha} as the candidate for stage "
            f"{stage.stage_id!r}: the working tree holds changes that commit "
            f"does not represent ({listed}). Commit or discard them first"
        )

    _require_no_pause(records)
    _require_history(records, stage.stage_id, repo_root, candidate_sha, state.base_sha)

    # A cross-repository stage is frozen as a *set*: pinning only the primary
    # commit would let a reviewed sibling move between review and acceptance.
    # Refuses before anything is written, like every check above.
    repositories = _freeze_siblings(repo_root, state.repositories, records, stage.stage_id)

    state.candidate_sha = candidate_sha
    state.status = StageStatus.FROZEN
    state.repositories = repositories
    stage.write_state(state)

    return FreezeResult(
        candidate_sha=candidate_sha,
        branch=branch,
        push_detail=_local_detail(candidate_sha, branch),
        ignored_dirty_paths=tuple(_describe_entry(entry) for entry in ignored),
        repositories=repositories,
    )


def accept_candidate(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
) -> AcceptanceResult:
    """Accept exactly the frozen candidate, or refuse.

    Runs inside :func:`~agent_sparring.concurrency.worktree_lock`, the same
    lock a stage-agent turn holds, so acceptance cannot interleave with a
    concurrent correction turn on the same worktree. A contended lock
    raises :class:`AcceptanceError` without touching ``state.json``.

    The frozen ``candidate_sha`` is not trusted as a description of the
    repository. Two things are independently re-checked here, not merely
    read back from ``state.json``:

    - the current ``HEAD`` must be identical to the frozen ``candidate_sha``.
      If not, the candidate has moved on and :class:`StaleCandidateError` is
      raised — the new HEAD is never accepted in its place, no commit is
      created, and ``candidate_sha`` keeps its frozen value so the refusal
      is repeatable and inspectable. Presenting the changed code means
      freezing it again and sparring it again first.
    - the worktree must hold no unrepresented dirty changes outside this
      stage's own artifact files (the same check and the same allowlist
      :func:`freeze_candidate` uses -- see :func:`_partition_dirty`). A
      clean freeze of commit A followed by an uncommitted edit to a real
      file leaves ``HEAD`` at A but the worktree no longer *is* A; that must
      refuse acceptance too, not just a moved ``HEAD``.

    Nothing about the candidate's sparring *history* is consulted: a SHA
    that previously drew SEND_BACK/NEEDS_YOU/ESCALATE is perfectly
    acceptable now if it is still the frozen candidate, which is what makes
    same-SHA reconsideration after new evidence work without a dummy commit.
    Updating only this stage's own artifact files (recording evidence in
    ``notes.md``, a fresh ``sparring.md`` exchange) never blocks that
    reconsideration, by the same allowlist.

    On success, ``status`` becomes ACCEPTED and ``candidate_sha`` remains
    the exact accepted SHA.

    Raises :class:`AcceptanceError` if the stage's state is unreadable, no
    candidate has been frozen (status WORKING), the recorded candidate is
    missing or malformed, the stage is already ACCEPTED, the worktree is not
    on ``expected_branch``, or the worktree holds unrepresented dirty
    changes. Raises :class:`StaleCandidateError` (a subclass of
    :class:`AcceptanceError`) specifically when ``HEAD`` no longer matches
    the frozen candidate.
    """

    try:
        with worktree_lock(repo_root), _run_guard(stage, repo_root) as records:
            accepted = _accept_candidate_locked(
                stage, repo_root, expected_branch=expected_branch, records=records
            )
    except WorktreeLockError as exc:
        error = AcceptanceError(
            f"cannot accept the candidate for stage {stage.stage_id!r}: {exc}"
        )
        _emit_refusal(stage, "accept", error)
        raise error from exc
    except AcceptanceError as exc:
        _emit_refusal(stage, "accept", exc)
        raise
    stage.activity_log().emit("gate", "candidate.accepted", sha=accepted.candidate_sha)
    return accepted


def _accept_candidate_locked(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
    records: RunRecords | None = None,
) -> AcceptanceResult:
    state = _read_state(stage)

    if state.status is StageStatus.ACCEPTED:
        raise AcceptanceError(
            f"stage {stage.stage_id!r} is already ACCEPTED at "
            f"{state.candidate_sha}; refusing to re-accept it"
        )
    if state.status is not StageStatus.FROZEN:
        raise AcceptanceError(
            f"stage {stage.stage_id!r} has status {state.status.value!r}; a "
            "candidate must be frozen before it can be accepted"
        )

    frozen_sha = state.candidate_sha
    if not is_full_sha(frozen_sha):
        raise AcceptanceError(
            f"stage {stage.stage_id!r} is FROZEN but its recorded "
            f"candidate_sha is not a full commit id ({frozen_sha!r}); freeze "
            "a candidate again rather than guessing what was meant"
        )

    branch = _require_branch(repo_root, expected_branch, operation="accept a candidate")

    try:
        head_sha = resolve_commit(repo_root, "HEAD", label="current HEAD")
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc

    if head_sha != frozen_sha:
        raise StaleCandidateError(
            f"the frozen candidate for stage {stage.stage_id!r} is "
            f"{frozen_sha}, but {branch} in {repo_root} is now at {head_sha}; "
            "refusing acceptance as stale. The candidate that changed must be "
            "frozen again and go through sparring again before it can be "
            "accepted; no commit was created and the frozen candidate was "
            "left unchanged."
        )

    # HEAD matches, but HEAD alone does not prove the worktree still *is*
    # the frozen candidate: an uncommitted edit to a real file leaves HEAD
    # untouched. The same allowlist freeze uses applies here, so recording
    # evidence in this stage's own artifact files never blocks acceptance.
    blocking, _ignored = _partition_dirty(repo_root, stage)
    if blocking:
        listed = ", ".join(_describe_entry(entry) for entry in blocking)
        raise CandidateNotOnBranchError(
            f"refusing to accept {frozen_sha} for stage {stage.stage_id!r}: "
            f"the working tree holds changes that commit does not represent "
            f"({listed}), even though HEAD still matches the frozen "
            "candidate. Commit or discard them first"
        )

    _require_no_pause(records)
    _require_history(records, stage.stage_id, repo_root, frozen_sha, state.base_sha)

    # The same questions, asked of every pinned sibling candidate: is it
    # still exactly the commit that was reviewed, does its worktree still
    # *be* that commit, and does it keep its own history? A cross-repository
    # stage is accepted as a complete set or not at all.
    _verify_siblings(repo_root, state.repositories, records, stage.stage_id)

    # And the reviewer's verdict must be over exactly that set.
    _require_verdict_at(state, stage, frozen_sha)

    state.status = StageStatus.ACCEPTED
    stage.write_state(state)

    return AcceptanceResult(
        candidate_sha=frozen_sha, branch=branch, repositories=state.repositories
    )


def accept_reviewed_candidate(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
) -> AcceptanceResult:
    """Complete a review-only stage over the candidate set it reviewed.

    A stage in :attr:`~agent_sparring.stage.StageMode.INDEPENDENT_REVIEW`
    mode produces no commit. Its content is a judgement about commits the
    preceding stages already froze and accepted, and there is nothing for it
    to implement -- so requiring it to present a new candidate commit before
    it may complete would mean manufacturing one, which is the opposite of
    what a review is. The smallest honest completion rule instead is: *the
    candidate set you reviewed must still be exactly what you reviewed.*

    That identity is not decided here. :mod:`agent_sparring.review` pins it
    into ``state.json`` when the stage is entered, before the reviewer is
    given control: ``base_sha`` is the primary commit under review, and
    ``repositories`` holds every sibling candidate, each pinned at the
    commit it was at. This function re-verifies exactly that pinned set,
    the same way :func:`accept_candidate` re-verifies a freeze:

    1. the stage's recorded mode really is ``INDEPENDENT_REVIEW`` -- an
       implementation stage can never reach completion through here, so this
       is not a second, softer door onto the same gate;
    2. the stage is not already ACCEPTED;
    3. ``base_sha`` is a full commit id, i.e. the stage was entered through
       the review lifecycle and the reviewed identity was actually recorded;
    4. the worktree is on ``expected_branch``;
    5. ``HEAD`` is still exactly the reviewed commit -- otherwise
       :class:`StaleCandidateError`, because a review of one commit is not a
       review of another;
    6. the worktree holds no changes that commit does not represent (the
       same allowlist the freeze uses, so this stage's own artifacts never
       block it);
    7. history is preserved: every candidate the owning run accepted in
       this repository is an ancestor of the reviewed commit (no remote is
       read; see :mod:`agent_sparring.run_history`);
    8. every pinned sibling candidate is still on its branch, at its pinned
       commit, with a clean worktree and its own history preserved.

    Only then is ``candidate_sha`` written as that same reviewed commit and
    ``status`` set to ACCEPTED. ``candidate_sha == base_sha`` is not a
    degenerate case to be tidied away: it is the accurate statement that
    this stage added no commit of its own, and it keeps the reviewed
    identity inspectable afterwards.

    Nothing is committed, pushed, merged or created. There is no new
    candidate, no dummy commit, and no change to
    :func:`freeze_candidate`/:func:`accept_candidate`, which an
    implementation stage still goes through unchanged.
    """

    try:
        with worktree_lock(repo_root), _run_guard(stage, repo_root) as records:
            accepted = _accept_reviewed_candidate_locked(
                stage, repo_root, expected_branch=expected_branch, records=records
            )
    except WorktreeLockError as exc:
        error = AcceptanceError(
            f"cannot complete the independent review for stage {stage.stage_id!r}: {exc}"
        )
        _emit_refusal(stage, "accept", error)
        raise error from exc
    except AcceptanceError as exc:
        _emit_refusal(stage, "accept", exc)
        raise
    stage.activity_log().emit("gate", "candidate.accepted", sha=accepted.candidate_sha)
    return accepted


def _accept_reviewed_candidate_locked(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
    records: RunRecords | None = None,
) -> AcceptanceResult:
    state = _read_state(stage)

    if state.mode is not StageMode.INDEPENDENT_REVIEW:
        raise AcceptanceError(
            f"stage {stage.stage_id!r} ran as {state.mode.describe}, not as an "
            "independent review; a stage that implements something is accepted through "
            "freeze-candidate and accept-candidate over the commit it produced, and "
            "completing it without one would accept work no candidate represents"
        )
    if state.status is StageStatus.ACCEPTED:
        raise AcceptanceError(
            f"stage {stage.stage_id!r} is already ACCEPTED at "
            f"{state.candidate_sha}; refusing to re-accept it"
        )

    reviewed_sha = state.base_sha
    if not is_full_sha(reviewed_sha):
        raise AcceptanceError(
            f"stage {stage.stage_id!r} is an independent review but the commit it is "
            f"reviewing is not recorded as a full commit id ({reviewed_sha!r}); the "
            "reviewed identity is pinned when the stage is entered, so this stage was "
            "never entered through the review lifecycle. Nothing was accepted."
        )

    branch = _require_branch(
        repo_root, expected_branch, operation="complete an independent review"
    )

    try:
        head_sha = resolve_commit(repo_root, "HEAD", label="current HEAD")
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc

    if head_sha != reviewed_sha:
        raise StaleCandidateError(
            f"stage {stage.stage_id!r} reviewed {reviewed_sha}, but {branch} in "
            f"{repo_root} is now at {head_sha}; refusing to complete the review as "
            "stale. A review of one commit is not a review of another: the reviewer "
            "must see the moved candidate before this stage can complete. Nothing was "
            "accepted and the reviewed commit was left recorded as it was."
        )

    blocking, _ignored = _partition_dirty(repo_root, stage)
    if blocking:
        listed = ", ".join(_describe_entry(entry) for entry in blocking)
        raise CandidateNotOnBranchError(
            f"refusing to complete the independent review of {reviewed_sha} for stage "
            f"{stage.stage_id!r}: the working tree holds changes that commit does not "
            f"represent ({listed}), so it is not the state that was reviewed, even "
            "though HEAD still matches. Commit or discard them first"
        )

    _require_verdict_at(state, stage, reviewed_sha)
    _require_no_pause(records)
    _require_history(records, stage.stage_id, repo_root, reviewed_sha, state.base_sha)

    _verify_siblings(repo_root, state.repositories, records, stage.stage_id)

    state.status = StageStatus.ACCEPTED
    # The reviewed commit, not a new one: this stage's candidate *is* the
    # candidate it reviewed, which is exactly what "review only" means.
    state.candidate_sha = reviewed_sha
    stage.write_state(state)

    return AcceptanceResult(
        candidate_sha=reviewed_sha, branch=branch, repositories=state.repositories
    )


__all__ = [
    "CANDIDATE_NOT_ON_BRANCH",
    "LOCAL_RULE",
    "RUN_HISTORY_REWRITTEN",
    "VERDICT_NOT_AT_CANDIDATE",
    "AcceptanceError",
    "AcceptanceResult",
    "CandidateNotOnBranchError",
    "FreezeResult",
    "RunHistoryRewrittenError",
    "RunRecords",
    "StaleCandidateError",
    "UnauthorizedRemoteMovementError",
    "VerdictNotAtCandidateError",
    "accept_candidate",
    "accept_reviewed_candidate",
    "freeze_candidate",
    "pin_sibling_candidates",
    "require_primary_history",
    "run_records",
    "sibling_root",
    "unrepresented_dirty",
    "unrepresented_dirty_entries",
    "verify_sibling_candidates",
]
