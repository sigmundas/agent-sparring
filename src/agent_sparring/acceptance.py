"""The acceptance gate: freeze an exact candidate, then accept exactly that.

Per the project plan's "Acceptance is the hard gate": normal sparring is
soft, acceptance is strict. This module implements only two operations on
the *existing* :class:`~agent_sparring.stage.StageState` vocabulary
(``status`` WORKING/FROZEN/ACCEPTED plus ``candidate_sha``):

    freeze_candidate()  WORKING/FROZEN -> FROZEN, candidate_sha = exact HEAD
    accept_candidate()  FROZEN         -> ACCEPTED, candidate_sha unchanged

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_sparring.git_context import (
    GitContextError,
    current_branch,
    dirty_paths,
    is_full_sha,
    resolve_commit,
    verify_pushed,
)
from agent_sparring.stage import Stage, StageError, StageState, StageStatus


class AcceptanceError(RuntimeError):
    """Raised when a freeze or an acceptance is refused.

    Every refusal leaves the stage unaccepted. ``state.json`` is only ever
    written on a *successful* freeze or acceptance, so a refused operation
    cannot half-advance the lifecycle.
    """


class StaleCandidateError(AcceptanceError):
    """Raised when the repository no longer matches the frozen candidate.

    The frozen ``candidate_sha`` is left exactly as it was: the new HEAD is
    never silently substituted, and no dummy commit is created. Presenting
    the changed code means freezing it as a new candidate and sparring it
    again before a later acceptance.
    """


@dataclass(frozen=True)
class FreezeResult:
    """What was frozen, and the push evidence it was frozen on."""

    candidate_sha: str
    branch: str
    push_detail: str
    ignored_dirty_paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AcceptanceResult:
    """The exact candidate that was accepted."""

    candidate_sha: str
    branch: str


def _read_state(stage: Stage) -> StageState:
    try:
        return stage.read_state()
    except StageError as exc:
        raise AcceptanceError(f"cannot read stage state: {exc}") from exc


def _require_branch(repo_root: Path, expected_branch: str, *, operation: str) -> str:
    if not expected_branch or not expected_branch.strip():
        raise AcceptanceError(
            f"expected_branch is required to {operation}; the candidate's "
            "intended branch is what its pushed/reachable check is made against"
        )
    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc
    if branch != expected_branch:
        raise AcceptanceError(
            f"expected branch {expected_branch!r} but {repo_root} is on "
            f"{branch!r}; refusing to {operation} from the wrong branch"
        )
    return branch


def _sparring_dir_prefix(repo_root: Path, sparring_dir: Path) -> str | None:
    """The repo-relative path prefix of ``sparring_dir``, or ``None`` if it
    lives outside the repository."""

    try:
        relative = sparring_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    text = relative.as_posix()
    return "" if text in ("", ".") else f"{text}/"


def _partition_dirty(
    repo_root: Path, sparring_dir: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the working tree's dirty paths into (blocking, ignored).

    Workflow artifacts inside the stage's own ``.sparring`` directory are
    ignored: this tool rewrites ``state.json``/``handoff.md``/``sparring.md``
    on every stage and sparring turn, so a project that neither commits nor
    ignores that directory would otherwise be unable to freeze anything at
    all. Everything else — any unrepresented source, test, or config change
    that the candidate commit does not contain — blocks the freeze.
    """

    try:
        paths = dirty_paths(repo_root)
    except GitContextError as exc:
        raise AcceptanceError(str(exc)) from exc

    prefix = _sparring_dir_prefix(repo_root, sparring_dir)
    if prefix is None:
        return paths, tuple()

    blocking: list[str] = []
    ignored: list[str] = []
    for path in paths:
        # dirty_paths folds a rename into "<new> (renamed from <old>)"; the
        # destination path is what matters for this classification.
        head = path.split(" (renamed from ", 1)[0]
        (ignored if head.startswith(prefix) else blocking).append(path)
    return tuple(blocking), tuple(ignored)


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
    right now and has actually been pushed, not a revision someone typed.

    Checks, in order, all before anything is written:

    1. the stage's state is readable and is not already ACCEPTED;
    2. the worktree is on ``expected_branch``;
    3. ``HEAD`` resolves to a full 40-hex commit;
    4. the worktree holds no unrepresented dirty changes outside
       ``sparring_dir`` (see :func:`_partition_dirty`);
    5. that commit is reachable from ``expected_branch``'s configured
       remote ref, proven locally by
       :func:`~agent_sparring.git_context.verify_pushed` rather than trusted
       from any recorded flag.

    Only then are ``candidate_sha`` and ``status = FROZEN`` recorded. The
    stage's ``base_sha`` and both session ids are preserved untouched.

    Re-freezing is allowed and is not an error: the same SHA can be frozen
    again after its evidence changed (no dummy commit needed), and a stage
    whose HEAD legitimately moved on can be frozen again at the new commit.

    Raises :class:`AcceptanceError` if any check above fails.
    """

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

    blocking, ignored = _partition_dirty(repo_root, sparring_dir)
    if blocking:
        listed = ", ".join(blocking)
        raise AcceptanceError(
            f"refusing to freeze {candidate_sha} as the candidate for stage "
            f"{stage.stage_id!r}: the working tree holds changes that commit "
            f"does not represent ({listed}). Commit or discard them first."
        )

    pushed, push_detail = verify_pushed(repo_root, candidate_sha, branch)
    if not pushed:
        raise AcceptanceError(
            f"refusing to freeze {candidate_sha} as the candidate for stage "
            f"{stage.stage_id!r}: it is not available on the intended remote "
            f"branch ({push_detail})"
        )

    state.candidate_sha = candidate_sha
    state.status = StageStatus.FROZEN
    stage.write_state(state)

    return FreezeResult(
        candidate_sha=candidate_sha,
        branch=branch,
        push_detail=push_detail,
        ignored_dirty_paths=ignored,
    )


def accept_candidate(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
) -> AcceptanceResult:
    """Accept exactly the frozen candidate, or refuse.

    The frozen ``candidate_sha`` is not trusted as a description of the
    repository: the current ``HEAD`` is resolved independently here and the
    two must be identical. If they are not, the candidate has moved on and
    :class:`StaleCandidateError` is raised — the new HEAD is never accepted
    in its place, no commit is created, and ``candidate_sha`` keeps its
    frozen value so the refusal is repeatable and inspectable. Presenting
    the changed code means freezing it again and sparring it again first.

    Nothing about the candidate's sparring *history* is consulted: a SHA
    that previously drew SEND_BACK/NEEDS_YOU/ESCALATE is perfectly
    acceptable now if it is still the frozen candidate, which is what makes
    same-SHA reconsideration after new evidence work without a dummy commit.

    On success, ``status`` becomes ACCEPTED and ``candidate_sha`` remains
    the exact accepted SHA.

    Raises :class:`AcceptanceError` if the stage's state is unreadable, no
    candidate has been frozen (status WORKING), the recorded candidate is
    missing or malformed, the stage is already ACCEPTED, or the worktree is
    not on ``expected_branch``.
    """

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

    state.status = StageStatus.ACCEPTED
    stage.write_state(state)

    return AcceptanceResult(candidate_sha=frozen_sha, branch=branch)


__all__ = [
    "AcceptanceError",
    "AcceptanceResult",
    "FreezeResult",
    "StaleCandidateError",
    "accept_candidate",
    "freeze_candidate",
]
