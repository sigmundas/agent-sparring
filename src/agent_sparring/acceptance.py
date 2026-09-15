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

from dataclasses import dataclass, field
from pathlib import Path

from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.git_context import (
    DirtyEntry,
    GitContextError,
    current_branch,
    dirty_entries,
    is_full_sha,
    resolve_commit,
    verify_pushed,
)
from agent_sparring.prompt_capture import PROMPTS_DIRNAME, is_prompt_artifact
from agent_sparring.stage import (
    ACTIVITY_FILENAME,
    BRIEF_FILENAME,
    HANDOFF_FILENAME,
    NOTES_FILENAME,
    SPARRING_FILENAME,
    STATE_FILENAME,
    CandidateRepository,
    Stage,
    StageError,
    StageState,
    StageStatus,
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
    """What was frozen, and the push evidence it was frozen on.

    ``repositories`` is the pinned sibling candidate set (empty for the
    ordinary single-repository stage): each declared sibling resolved to the
    exact commit its branch was at, verified clean and pushed.
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


def _partition_dirty(
    repo_root: Path, stage: Stage
) -> tuple[tuple[str, ...], tuple[str, ...]]:
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

    blocking: list[str] = []
    ignored: list[str] = []
    for entry in entries:
        description = (
            f"{entry.path} (renamed from {entry.old_path})"
            if entry.old_path is not None
            else entry.path
        )
        (ignored if _entry_is_exempt(entry, allowed, prompts_dir) else blocking).append(
            description
        )
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
    repo_root: Path, repositories: tuple[CandidateRepository, ...]
) -> tuple[CandidateRepository, ...]:
    """Resolve and pin every declared sibling candidate, or refuse.

    Each sibling gets the same treatment the primary repository already
    gets, in the same order: it must be on the declared branch, resolve
    HEAD, hold no dirty changes at all (a sibling holds none of *this*
    stage's workflow artifacts, so nothing is exempt there), and have that
    commit reachable from its branch's configured remote. A sibling that
    declares a ``candidate_sha`` must be exactly at it -- that is the
    reviewed commit asserted by whoever declared it, and silently pinning a
    different one would defeat the point.

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
        try:
            branch = current_branch(root)
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if branch != repository.branch:
            raise AcceptanceError(
                f"sibling repository {_describe(repository)} is on {branch!r} but its "
                f"candidate belongs to {repository.branch!r}; refusing to freeze a "
                "candidate set built from the wrong branch"
            )
        try:
            head = resolve_commit(root, "HEAD", label=f"{repository.name} candidate")
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if repository.candidate_sha is not None and repository.candidate_sha != head:
            raise AcceptanceError(
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
            raise AcceptanceError(
                f"refusing to freeze {head} as the candidate of sibling repository "
                f"{_describe(repository)}: its working tree holds changes that commit "
                f"does not represent ({listed}). Commit or discard them first."
            )
        pushed, push_detail = verify_pushed(root, head, branch)
        if not pushed:
            raise AcceptanceError(
                f"refusing to freeze {head} as the candidate of sibling repository "
                f"{_describe(repository)}: it is not available on the intended remote "
                f"branch ({push_detail})"
            )
        pinned.append(
            CandidateRepository(
                name=repository.name,
                path=repository.path,
                branch=repository.branch,
                candidate_sha=head,
            )
        )
    return tuple(pinned)


def _verify_siblings(repo_root: Path, repositories: tuple[CandidateRepository, ...]) -> None:
    """Re-check that every pinned sibling candidate is still exactly what was
    frozen, mirroring the primary repository's stale-candidate check.

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
        try:
            branch = current_branch(root)
            head = resolve_commit(root, "HEAD", label=f"{repository.name} HEAD")
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if branch != repository.branch:
            raise AcceptanceError(
                f"sibling repository {_describe(repository)} is on {branch!r} but its "
                f"frozen candidate belongs to {repository.branch!r}; refusing acceptance"
            )
        if head != repository.candidate_sha:
            raise StaleCandidateError(
                f"the frozen candidate of sibling repository {_describe(repository)} is "
                f"{repository.candidate_sha}, but {branch} in {root} is now at {head}; "
                "refusing acceptance as stale. The complete reviewed candidate set must "
                "still be the one that was reviewed; nothing was accepted."
            )
        try:
            dirty = dirty_entries(root, all_untracked=True)
        except GitContextError as exc:
            raise AcceptanceError(f"sibling repository {_describe(repository)}: {exc}") from exc
        if dirty:
            listed = ", ".join(entry.path for entry in dirty)
            raise AcceptanceError(
                f"refusing to accept {repository.candidate_sha} for sibling repository "
                f"{_describe(repository)}: its working tree holds changes that commit does "
                f"not represent ({listed}), even though HEAD still matches."
            )


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

    # sparring_dir is accepted for call-signature/API stability (and because
    # it is what a caller naturally has on hand) but is not used to compute
    # the workflow-artifact exemption below -- see _stage_artifact_allowlist,
    # which is derived from stage.directory only, precisely so that naming
    # some other tree "sparring_dir" (up to and including sparring_dir ==
    # repo_root) can never widen what is exempt from the dirty-tree check.
    del sparring_dir

    try:
        with worktree_lock(repo_root):
            frozen = _freeze_candidate_locked(stage, repo_root, expected_branch=expected_branch)
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
    else:
        summary = f"{operation} refused"
    stage.activity_log().emit("gate", "gate.refused", summary=summary)


def _freeze_candidate_locked(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
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

    # A cross-repository stage is frozen as a *set*: pinning only the primary
    # commit would let a reviewed sibling move between review and acceptance.
    # Refuses before anything is written, like every check above.
    repositories = _freeze_siblings(repo_root, state.repositories)

    state.candidate_sha = candidate_sha
    state.status = StageStatus.FROZEN
    state.repositories = repositories
    stage.write_state(state)

    return FreezeResult(
        candidate_sha=candidate_sha,
        branch=branch,
        push_detail=push_detail,
        ignored_dirty_paths=ignored,
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
        with worktree_lock(repo_root):
            accepted = _accept_candidate_locked(
                stage, repo_root, expected_branch=expected_branch
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
        listed = ", ".join(blocking)
        raise AcceptanceError(
            f"refusing to accept {frozen_sha} for stage {stage.stage_id!r}: "
            f"the working tree holds changes that commit does not represent "
            f"({listed}), even though HEAD still matches the frozen "
            "candidate. Commit or discard them first."
        )

    # The same two questions, asked of every pinned sibling candidate: is it
    # still exactly the commit that was reviewed, and does its worktree still
    # *be* that commit? A cross-repository stage is accepted as a complete
    # set or not at all.
    _verify_siblings(repo_root, state.repositories)

    state.status = StageStatus.ACCEPTED
    stage.write_state(state)

    return AcceptanceResult(
        candidate_sha=frozen_sha, branch=branch, repositories=state.repositories
    )


__all__ = [
    "AcceptanceError",
    "AcceptanceResult",
    "FreezeResult",
    "StaleCandidateError",
    "accept_candidate",
    "freeze_candidate",
    "sibling_root",
]
