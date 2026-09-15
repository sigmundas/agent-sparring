"""The independent-review lifecycle: one reviewer, no implementation agent.

:mod:`agent_sparring.loop` drives the ordinary stage: an implementation turn,
a sparring turn, and a correction cycle between them. A stage in
:attr:`~agent_sparring.stage.StageMode.INDEPENDENT_REVIEW` mode has no
implementation turn to drive, so it does not get that loop at all::

    enter the stage
        v
    pin exactly what is under review (and refuse if it is not what it should be)
        v
    one fresh independent reviewer turn
        v
    READY / NEEDS_YOU / SEND_BACK (a defect) / ESCALATE

Every one of those four is terminal here, which is the difference that
matters. In the ordinary loop SEND_BACK continues the cycle, because there
is a stage agent to send the work back *to*. There is none behind a
review-only stage, so a defect stops the plan, unaccepted, and a person
decides where the fix belongs -- in a new stage, in a reopened earlier one,
or outside this plan. Silently turning the review stage into an
implementation stage to fix what its own reviewer found would make the
reviewer the author of the work it was brought in to judge, which is the
whole thing this mode exists to prevent.

Review only, enforced rather than requested
-------------------------------------------

The reviewer runs through :func:`~agent_sparring.sparring_agent.
run_sparring_agent`, which is not a convenience: that function holds the
OS-level read-only provider (see :mod:`agent_sparring.providers.codex_cli`),
the branch check before the turn, and the before/after branch/HEAD/dirty
fingerprint that refuses the run if the provider wrote anything at all. An
independent reviewer needs more of that enforcement than a sparrer, not
less, so it uses the same one implementation. Only the prompt and the
recorded role differ (:mod:`agent_sparring.review_prompt`).

The human gate is the ordinary one
----------------------------------

An activation decision, a device check, a pre-activation reader gate: all of
them travel the existing structured NEEDS_YOU path. The reviewer asks with a
``human_gate``, the plan pauses, ``resume-plan --evidence`` records the
human's answer in ``notes.md``, and the *same* reviewer session resumes and
judges it. No new state, no second kind of pause, and nothing about the
answer is interpreted by this module.

What is under review is pinned before the reviewer sees it
----------------------------------------------------------

:func:`enter_review` resolves the accepted candidate set from the preceding
stages' own ``state.json`` files, verifies the repositories are actually at
those commits, and records the primary commit as this stage's ``base_sha``
with every sibling candidate pinned in ``repositories`` -- all before the
provider is given control, the same ordering
:func:`~agent_sparring.stage_agent.run_stage_agent` uses for an
implementation stage's base. That recorded identity is what
:func:`~agent_sparring.acceptance.accept_reviewed_candidate` later re-checks,
so the stage can only complete over the set that was actually reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_sparring.acceptance import (
    AcceptanceError,
    pin_sibling_candidates,
    unrepresented_dirty,
)
from agent_sparring.git_context import GitContextError, current_branch, is_full_sha, resolve_commit
from agent_sparring.plan_model import PlannedStage
from agent_sparring.providers import SparringAgentAdapter
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.stage import CandidateRepository, Stage, StageError, StageMode, StageStatus


class ReviewError(RuntimeError):
    """Raised when a review-only stage cannot be entered or run as written.

    Every refusal leaves the stage unaccepted and, for a refusal raised
    before the reviewer turn, leaves ``state.json`` exactly as it was.
    """


@dataclass(frozen=True)
class ReviewedStage:
    """One accepted stage whose candidate this review covers."""

    stage_id: str
    display: str
    candidate_sha: str
    repositories: tuple[CandidateRepository, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReviewSubject:
    """Exactly what a review-only stage is reviewing, as verified on entry.

    ``candidate_sha`` is the primary repository's commit -- the candidate the
    last accepted stage froze, which the repository must actually be at.
    ``accepted`` is every preceding stage with its own accepted commit, in
    plan order, so the reviewer can address them by the names people use.
    ``repositories`` is the union of their sibling candidates, each pinned at
    the commit it was reviewed at.
    """

    repo_root: Path
    branch: str
    candidate_sha: str
    accepted: tuple[ReviewedStage, ...]
    repositories: tuple[CandidateRepository, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReviewResult:
    """The independent reviewer's one outcome.

    ``defect`` distinguishes the SEND_BACK case for a caller that reports
    it: nothing about it is a correction cycle, and calling it "sent back"
    would suggest something is going to act on it automatically.
    """

    outcome: RoutingAction
    routing: RoutingResult
    resumed: bool

    @property
    def defect(self) -> bool:
        return self.outcome is RoutingAction.SEND_BACK


def _read_state(stage: Stage):
    try:
        return stage.read_state()
    except StageError as exc:
        raise ReviewError(f"cannot read the state of stage {stage.stage_id!r}: {exc}") from exc


def declare_stage_mode(stage: Stage, planned: PlannedStage) -> None:
    """Record in ``state.json`` that this stage runs as the plan declares.

    Written before anything else happens for the stage, so what a stage
    *ran as* is on disk from the start rather than being inferred afterwards
    from which artifacts it happens to have.

    A stage that has already run under a different mode is refused here, and
    that refusal is the point: it is exactly the condition a stage mistakenly
    started through the wrong lifecycle is in, and neither answer available
    at this moment is acceptable. Continuing under the recorded mode would
    ignore the plan; overwriting the recorded mode would adopt an
    implementation session, a moved candidate or a stage agent's leftovers
    into a review that is supposed to be independent of them. So it stops
    and names the supported recovery (see :mod:`agent_sparring.recovery`),
    which archives that attempt and reinitialises the stage deliberately.

    An ACCEPTED stage is never touched: acceptance is terminal and nothing
    will run for it again.
    """

    state = _read_state(stage)
    if state.status is StageStatus.ACCEPTED:
        return
    if state.mode is planned.mode:
        return

    has_history = any(
        (
            state.implementation_session_id,
            state.sparring_session_id,
            state.candidate_sha,
            state.base_sha,
        )
    )
    if has_history:
        raise ReviewError(
            f"stage {planned.stage_id!r} already ran as {state.mode.describe}, but this "
            f"plan declares it as {planned.mode.describe}. Refusing to continue: running "
            "it under the recorded mode would ignore the plan, and adopting that attempt "
            "into the declared mode would carry its session, its base commit and whatever "
            "it left in the worktree into a review that is meant to be independent of "
            "them.\n"
            f"Fix it deliberately with: sparring reset-stage {planned.stage_id} "
            "--mode " + planned.mode.value + " (it archives the existing attempt as "
            "history, verifies the repository is at the preceding accepted candidate, and "
            "reinitialises this stage under the declared mode with a fresh session)."
        )
    # No session, no candidate, no base: nothing ran under the old mode, so
    # there is no history to preserve and recording the declared mode is
    # just labelling an empty stage.
    state.mode = planned.mode
    stage.write_state(state)


def enter_review(
    stage: Stage,
    repo_root: Path,
    *,
    expected_branch: str,
    preceding: tuple[tuple[PlannedStage, Stage], ...],
) -> ReviewSubject:
    """Pin and verify exactly what this review-only stage reviews.

    ``preceding`` is every planned stage before this one, paired with its
    resolved :class:`~agent_sparring.stage.Stage`, in plan order. Each must
    be ACCEPTED with a full candidate commit: a review of "the accepted
    candidate set" is meaningless if part of that set was never accepted,
    and guessing which commit a half-finished stage meant is exactly the
    kind of inference this engine refuses to make.

    Then the repositories themselves are checked, not trusted:

    - the primary worktree is on ``expected_branch``;
    - its ``HEAD`` is the last accepted stage's candidate commit -- so a
      reviewer is never handed a repository that has moved past the work it
      was asked about;
    - the worktree holds no changes that commit does not represent (the
      acceptance gate's own check, via
      :func:`~agent_sparring.acceptance.unrepresented_dirty`);
    - every sibling candidate declared by an accepted stage is on its
      branch, at its pinned commit, clean and pushed (the freeze's own
      sibling handling, via
      :func:`~agent_sparring.acceptance.pin_sibling_candidates`).

    On success the primary commit is recorded as this stage's ``base_sha``
    and the sibling set as its ``repositories``, before the reviewer runs.
    Raises :class:`ReviewError` on any refusal, having written nothing.
    """

    if not expected_branch or not expected_branch.strip():
        raise ReviewError("expected_branch is required to enter an independent-review stage")

    accepted: list[ReviewedStage] = []
    for planned, earlier in preceding:
        earlier_state = _read_state(earlier)
        if earlier_state.status is not StageStatus.ACCEPTED:
            raise ReviewError(
                f"stage {stage.stage_id!r} is an independent review of the accepted "
                f"candidate set, but {planned.display} [{planned.stage_id}] has status "
                f"{earlier_state.status.value!r} rather than accepted. There is no "
                "accepted candidate set to review yet; nothing was run."
            )
        if not is_full_sha(earlier_state.candidate_sha):
            raise ReviewError(
                f"stage {stage.stage_id!r} is an independent review, but "
                f"{planned.display} [{planned.stage_id}] is recorded as accepted with no "
                f"usable candidate commit ({earlier_state.candidate_sha!r}); refusing to "
                "guess which commit it accepted"
            )
        accepted.append(
            ReviewedStage(
                stage_id=planned.stage_id,
                display=planned.display,
                candidate_sha=str(earlier_state.candidate_sha),
                repositories=earlier_state.repositories,
            )
        )

    if not accepted:
        raise ReviewError(
            f"stage {stage.stage_id!r} is an independent review but no stage precedes it "
            "in this plan; there is nothing accepted for it to review"
        )

    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise ReviewError(str(exc)) from exc
    if branch != expected_branch:
        raise ReviewError(
            f"expected branch {expected_branch!r} but {repo_root} is on {branch!r}; "
            "refusing to review the wrong branch"
        )

    try:
        head = resolve_commit(repo_root, "HEAD", label="reviewed candidate")
    except GitContextError as exc:
        raise ReviewError(str(exc)) from exc

    reviewed = accepted[-1]
    if head != reviewed.candidate_sha:
        raise ReviewError(
            f"stage {stage.stage_id!r} reviews the candidate accepted by "
            f"{reviewed.display} [{reviewed.stage_id}], which is "
            f"{reviewed.candidate_sha}, but {branch} in {repo_root} is at {head}. "
            "Refusing to review a repository that is not at the accepted candidate: "
            "check out that commit, or accept the work that moved it as its own stage "
            "first. Nothing was run."
        )

    blocking = unrepresented_dirty(repo_root, stage)
    if blocking:
        listed = ", ".join(blocking)
        raise ReviewError(
            f"refusing to review {head} for stage {stage.stage_id!r}: the working tree "
            f"holds changes that commit does not represent ({listed}), so what the "
            "reviewer would inspect is not the accepted candidate. Commit or discard "
            "them first."
        )

    # The sibling candidates under review, deduplicated by identity: every
    # accepted stage's declarations, plus this stage's own. Several stages of
    # one plan routinely name the same sibling repository, and the reviewer
    # needs the set rather than one entry per stage.
    #
    # This stage's own declaration is included because it is the only way to
    # say "the review also covers that repository" for a sibling no earlier
    # stage declared -- a plan whose review stage spans two repositories
    # while its implementation stages each touched one. Dropping it would
    # silently narrow what acceptance re-verifies to the earlier stages'
    # declarations, which is the opposite of what declaring it asked for.
    state = _read_state(stage)
    declared: dict[tuple[str, str, str], CandidateRepository] = {}
    for repository in state.repositories:
        declared[(repository.name, repository.path, repository.branch)] = repository
    for entry in accepted:
        for repository in entry.repositories:
            declared.setdefault(
                (repository.name, repository.path, repository.branch), repository
            )
    try:
        repositories = pin_sibling_candidates(repo_root, tuple(declared.values()))
    except AcceptanceError as exc:
        raise ReviewError(
            f"stage {stage.stage_id!r} cannot review the accepted candidate set: {exc}"
        ) from exc

    state.mode = StageMode.INDEPENDENT_REVIEW
    state.base_sha = head
    state.repositories = repositories
    stage.write_state(state)

    return ReviewSubject(
        repo_root=Path(repo_root),
        branch=branch,
        candidate_sha=head,
        accepted=tuple(accepted),
        repositories=repositories,
    )


def describe_candidate_set(stage: Stage, subject: ReviewSubject) -> str:
    """The engine's factual statement of what is under review.

    Written for the reviewer's prompt, and deliberately only facts the
    engine established itself in :func:`enter_review`: the commits, where
    they live, and that the repositories were verified to be at them.
    No claim about whether the work is any good -- that is the reviewer's
    entire job, and prejudging it in the prompt would be the fastest way to
    get it rubber-stamped.
    """

    lines = [
        f"This stage reviews work that is already accepted. Its candidate is "
        f"`{subject.candidate_sha}` on branch `{subject.branch}` in the primary "
        f"repository at `{subject.repo_root}`, and this engine verified immediately "
        "before this turn that the repository is at exactly that commit with no "
        "uncommitted changes outside this stage's own workflow artifacts.",
        "",
        "Accepted stages of this plan, in order, each with the commit it froze and "
        "had accepted:",
        "",
    ]
    for entry in subject.accepted:
        lines.append(f"- {entry.display} [`{entry.stage_id}`] — `{entry.candidate_sha}`")
    if subject.repositories:
        lines += [
            "",
            "Sibling repositories whose candidates are part of that set, each verified "
            "just now to be on its branch, at its pinned commit, clean and pushed:",
            "",
        ]
        for repository in subject.repositories:
            lines.append(
                f"- `{repository.name}` at `{repository.path}` on branch "
                f"`{repository.branch}` — `{repository.candidate_sha}`"
            )
    lines += [
        "",
        f"This stage ({stage.stage_id}) will produce no commit of its own. Completing "
        "it records that this exact candidate set passed independent review; the same "
        "set is re-verified at that moment, and no branch is merged by it.",
    ]
    return "\n".join(lines)


def run_independent_review(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    reviewer_adapter: SparringAgentAdapter,
    *,
    expected_branch: str,
    subject: ReviewSubject,
    evidence_first: bool = False,
) -> ReviewResult:
    """Run exactly one independent-review turn and return its outcome.

    Fresh reviewer session when the stage has none recorded, the same
    session resumed when it has -- decided, as everywhere else in this
    package, by :func:`~agent_sparring.sparring_agent.run_sparring_agent`
    from ``state.json`` rather than by a second copy of that rule here. That
    is what makes a NEEDS_YOU gate answerable by the reviewer that raised it.

    There is no cycle: all four routing actions are terminal (see the module
    docstring). ``evidence_first`` only labels the captured prompt.

    Raises :class:`ReviewError` if the turn itself fails -- provider error,
    a read-only contract violation, a session-identity mismatch, an
    unparseable verdict. Nothing is retried and nothing is rolled back.
    """

    try:
        run = run_sparring_agent(
            stage,
            sparring_dir,
            repo_root,
            reviewer_adapter,
            expected_branch=expected_branch,
            evidence_first=evidence_first,
            review_candidate_set=describe_candidate_set(stage, subject),
        )
    except SparringAgentRunError as exc:
        raise ReviewError(f"independent-review turn failed: {exc}") from exc

    activity = stage.activity_log().bind("reviewer")
    activity.emit(
        "review.completed",
        action=run.routing.action.value,
        summary=run.routing.summary,
    )
    return ReviewResult(
        outcome=run.routing.action, routing=run.routing, resumed=run.resumed
    )


__all__ = [
    "ReviewError",
    "ReviewResult",
    "ReviewSubject",
    "ReviewedStage",
    "declare_stage_mode",
    "describe_candidate_set",
    "enter_review",
    "run_independent_review",
]
