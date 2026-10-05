"""Supported recovery, for two situations a person cannot otherwise get out
of without editing engine state by hand.

:func:`reset_stage` restarts an unaccepted stage under the right mode, and
the rest of this docstring is about it. :func:`reopen_for_failed_check`
reopens the run's current stage when a manual check its review deferred has
been reported failing -- a lighter operation that archives nothing, keeps
the candidate, both sessions and the notes, and is documented on itself.

reset_stage
-----------

A stage can be started through the wrong lifecycle. The plan declares a
stage as an independent review; the run reaches it before that declaration
exists, or against a manifest that did not carry it; an implementation agent
duly opens a session, reads the repository, and writes a scratch test file
to try something out. Nothing is corrupt and nothing is lost -- but that
stage now holds an implementation session, a stage agent's leftovers in the
worktree, and no independent review at all, and none of it can become the
authoritative review.

Before this module the only way out was to edit ``.sparring/state.json`` and
the stage directory by hand. That is not a recovery procedure, it is an
invitation to destroy the one thing that must not be touched -- the
preceding stages' acceptance -- while trying to fix the one thing that must.
So the operation is here, it is one command, and it is explicit about every
judgement it makes.

What it does
------------

1. **Keeps every earlier stage exactly as it is.** It verifies, before
   anything else, that the plan input still names the same stages in the
   same order up to the one being reset, and that every one of them is
   ACCEPTED with a real candidate commit. It never writes to them.
2. **Preserves the attempt as history, and takes away its authority.** The
   whole stage directory -- state, brief, notes, activity log, every
   captured prompt, including the erroneous session's id -- is *moved* into
   an archive beside the stages, not deleted. What it loses is its position:
   it is no longer the stage, so nothing will resume it, adopt its session
   or read its verdict.
3. **Verifies the repository is where the review must start.** The primary
   repository must be on the expected branch and at the exact commit the
   preceding stage had accepted. A repository that has moved on is refused,
   because restarting a review of commit A against commit B is not a
   recovery.
4. **Handles what the attempt wrote, and refuses to touch anything else.**
   The files the erroneous turn created or modified are read from its own
   activity log, then classified against the worktree as it stands: still
   there and untracked (or git-ignored) is quarantined into the archive;
   already gone is reported as such; *tracked and modified* is a refusal,
   because that is committed content and discarding it is not this
   command's business. Any dirty path that is **not** one of those files is
   also a refusal: unrelated work in the worktree is someone's, and this
   command does not get to decide it is expendable.
5. **Reinitialises the stage under the declared mode**, with the plan
   input's brief and a fresh ``state.json`` -- so no session id, no
   candidate, no status carries over -- in place, under the same stage id.
   No duplicate stage is created and ``.sparring`` is never removed
   wholesale.
6. **Re-records the run's plan digest.** Declaring a stage's mode changes
   what the plan executes and therefore changes its digest (see
   :func:`agent_sparring.manifest.manifest_digest`), which a recorded run
   would otherwise refuse to continue against. Re-recording it is the one
   thing here that rewrites plan-run state, it happens only after every
   check above has passed, and both digests are reported.

The archive lives at ``.sparring/stages/.archive/<stage-id>/<n>-<mode>-<ts>/``.
Under ``stages/`` on purpose: that directory is already git-ignored in every
project this workflow runs in (``sparring check-config`` refuses otherwise),
so preserved history cannot make a later freeze see a dirty worktree, and no
new acceptance-gate exemption had to be invented for it. ``.archive`` is not
a valid stage id (:func:`~agent_sparring.stage.validate_stage_id` rejects a
leading dot), so nothing can ever address the archive as a stage.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from agent_sparring.acceptance import unrepresented_dirty_entries
from agent_sparring.git_context import (
    GitContextError,
    current_branch,
    is_full_sha,
    resolve_commit,
)
from agent_sparring.manifest import ManifestError
from agent_sparring.plan_model import PlanSource, PlannedStage
from agent_sparring.stage import (
    STAGES_DIRNAME,
    Stage,
    StageError,
    StageMode,
    StageStatus,
)

ARCHIVE_DIRNAME = ".archive"
RECOVERY_FILENAME = "RECOVERY.md"
# Where the moved stage directory and the quarantined worktree files land
# inside one archived attempt.
ATTEMPT_DIRNAME = "stage"
QUARANTINE_DIRNAME = "worktree"


class RecoveryError(RuntimeError):
    """Raised when the recovery is refused.

    Every refusal happens before anything is moved, created or rewritten,
    so a refused reset leaves the stage, the repository and the plan-run
    state exactly as they were.
    """


@dataclass(frozen=True)
class TouchedFile:
    """One file the erroneous attempt wrote, and what became of it."""

    path: str
    #: ``"quarantined"`` (moved into the archive), ``"absent"`` (already
    #: gone before the reset), or ``"residue"`` (a compiled copy of a
    #: quarantined or absent file, moved into the archive with it).
    disposition: str


@dataclass(frozen=True)
class ResetResult:
    """What the recovery did, in enough detail to report without guessing."""

    stage_id: str
    #: The key of the run instance whose current stage this was, so the
    #: resume hint can name the run rather than only the document.
    run: str
    from_mode: StageMode
    to_mode: StageMode
    archive: Path
    stage_directory: Path
    reviewed_stage: str
    reviewed_stage_id: str
    reviewed_sha: str
    previous_digest: str
    digest: str
    discarded_sessions: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    files: tuple[TouchedFile, ...] = field(default_factory=tuple)

    @property
    def quarantined(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.disposition != "absent")

    @property
    def already_absent(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.disposition == "absent")


@dataclass(frozen=True)
class ReopenResult:
    """What reopening a stage for a failed deferred check did."""

    stage_id: str
    #: The run instance whose current stage was reopened.
    run: str
    #: The withdrawn asking, and the checks it was asking about.
    instance_id: str
    title: str
    failed_checks: tuple[str, ...]
    stage_directory: Path
    candidate_sha: str | None


def reopen_for_failed_check(
    source: PlanSource,
    sparring_dir: Path,
    repo_root: Path,
    *,
    instance_id: str,
    expected_branch: str,
    report=lambda message: None,
) -> ReopenResult:
    """Reopen the current stage so its agents can repair a deferred human
    check that failed, and withdraw the asking that failed.

    This is the way out of an otherwise closed loop. A reviewer may accept a
    stage while deferring a manual check until plan completion; if the
    person then reports a Fail, the obligation is unresolved, so the run
    stops at its verification checkpoint -- but every stage is accepted, the
    run loop skips accepted stages, and only a Pass settles an obligation.
    The reported failure therefore had no consequence at all, and the one
    exit was to make the check pass by hand.

    Deliberately **not** :func:`reset_stage`, which is a different
    operation for a different problem. That one recovers an attempt that ran
    through the wrong lifecycle: it archives the whole stage directory,
    quarantines what the attempt wrote, requires the repository to be back
    at the *preceding* stage's candidate, and re-records the plan digest.
    None of that applies here. The work is not wrong, it is incomplete: the
    right candidate, the right sessions and the right notes all stay, and
    what changes is only that the stage is open again.

    So this writes exactly two things:

    1. the stage's status, ACCEPTED back to WORKING. Re-entering a stage
       whose ``sparring.md`` records READY is an ordinary, supported path
       (see ``plan._recorded_pause``): the sparrer confirms it on its own
       terms before the hard acceptance gate runs again. The stage agent
       reads the Fail first, because the engine wrote it into this stage's
       ``notes.md`` when it was recorded;
    2. the ledger, minus the failed asking. See
       :meth:`~agent_sparring.plan.PlanRunState.withdraw_obligation` for
       why withdrawing is right rather than clearing the result: the
       question was about a candidate that is about to be replaced, and if
       it still applies the new review asks it again as a new asking.

    Acceptance is still terminal for every stage a run has moved past. This
    reopens the run's **current** stage only, which is why an obligation
    raised by an earlier stage is refused rather than reached for: restarting
    it would rewrite history the later accepted stages were built on. The
    honest answer in that case is a follow-up stage, and the refusal says so.

    Refuses (:class:`RecoveryError`), before writing anything, if: no
    recorded run of this plan is stopped for deferred verification; the
    asking is not in that run's ledger, or is not one the current pause
    named; nothing about it has actually failed; the stage that raised it is
    not the run's current stage; that stage does not exist or is not
    ACCEPTED; or the repository is on another branch.
    """

    from agent_sparring.deferred_gate import DeferredVerificationRequired
    from agent_sparring.plan import PLANS_DIRNAME, PlanRunStatus, find_runs

    recorded = find_runs(sparring_dir, source.label)
    stopped = [
        run
        for run in recorded
        if run.state.status is not PlanRunStatus.COMPLETE
        and isinstance(run.state.awaiting, DeferredVerificationRequired)
        and run.state.obligation(instance_id) is not None
    ]
    if not stopped:
        if not recorded:
            raise RecoveryError(
                f"no plan run is recorded for {source.label} in "
                f"{Path(sparring_dir) / PLANS_DIRNAME}; there is no verification "
                "checkpoint to repair"
            )
        raise RecoveryError(
            f"no recorded run of {source.label} is stopped at a verification checkpoint "
            f"that owes the asking {instance_id!r}. A failed check is repaired at the "
            "checkpoint that reported it; nothing was changed."
        )
    run = stopped[0]
    run_state = run.state
    awaiting = run_state.awaiting
    assert isinstance(awaiting, DeferredVerificationRequired)  # filtered above
    if instance_id not in awaiting.instance_ids:
        raise RecoveryError(
            f"asking {instance_id!r} is in the ledger of run {run.key} but is not one this "
            "checkpoint is stopped on, so a surface rendered from an older state is asking "
            "to repair a question the run has since replaced; nothing was changed"
        )
    obligation = run_state.obligation(instance_id)
    assert obligation is not None  # filtered above
    if not obligation.failed:
        raise RecoveryError(
            f"nothing has failed for asking {instance_id!r} ({obligation.gate.title!r}): "
            "it is still waiting for an answer. Reopening a stage to repair a check nobody "
            "reported failing would throw away an accepted candidate over a question that "
            "was never answered; answer it first."
        )
    if obligation.stage_id != run_state.current_stage:
        raise RecoveryError(
            f"asking {instance_id!r} was raised by {obligation.stage_id!r}, but run "
            f"{run.key} is now at {run_state.current_stage!r}. Only the current stage can "
            "be reopened: the raising stage was accepted and the stages after it were built "
            "on that acceptance, so restarting it would rewrite their history. Put the "
            "repair in a follow-up stage instead; the Fail stays recorded in that stage's "
            "notes.md either way."
        )

    stage = _resolve(sparring_dir, obligation.stage_id)
    if not stage.exists():
        raise RecoveryError(
            f"stage {obligation.stage_id!r} does not exist at {stage.directory}; there is "
            "nothing to reopen"
        )
    try:
        stage_state = stage.read_state()
    except StageError as exc:
        raise RecoveryError(
            f"cannot read the state of stage {obligation.stage_id!r}: {exc}"
        ) from exc
    if stage_state.status is not StageStatus.ACCEPTED:
        raise RecoveryError(
            f"stage {obligation.stage_id!r} is {stage_state.status.value}, not ACCEPTED; it "
            "is already open and resuming the plan will run it. Nothing was changed."
        )
    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise RecoveryError(str(exc)) from exc
    if branch != expected_branch:
        raise RecoveryError(
            f"expected branch {expected_branch!r} but {repo_root} is on {branch!r}; "
            "refusing to reopen a stage from the wrong branch"
        )

    # Verified. The stage is written first: a run whose ledger no longer owes
    # the asking but whose stage is still ACCEPTED would complete the plan
    # over the failure, which is the one outcome this must never produce.
    # The reverse order -- an open stage still owing a withdrawn asking --
    # merely stops again and asks, which is safe.
    failed = tuple(result.check_id for result in obligation.failed)
    reopened = replace(stage_state, status=StageStatus.WORKING)
    if reopened.next_turn is not None:
        # The failed check is implementation work owed against the reviewed
        # candidate: the stage's next turn is the implementation agent's,
        # not the acceptance of a READY the failure has overturned. (A stage
        # written before next_turn existed stays byte-identical.)
        reopened.next_turn = "stage"
        reopened.next_turn_candidate = None
        reopened.next_turn_source = "engine"
    reopened.implementation_unreviewed = False
    stage.write_state(reopened)
    report(
        f"stage {obligation.stage_id}: ACCEPTED -> WORKING; its candidate "
        f"{stage_state.candidate_sha}, both sessions and its notes are unchanged, and the "
        "reported failure is already in its notes.md as human evidence"
    )
    run_state.withdraw_obligation(instance_id)
    # The checkpoint this pause named is gone, so the recorded stop would
    # describe a question that is no longer asked.
    run_state.awaiting = None
    run_state.status = PlanRunStatus.PAUSED
    run_state.provider_pause = None
    run_state.save(run.path)
    report(
        f"withdrew asking {instance_id} ({obligation.gate.title}); if it still applies to "
        "the repaired candidate the next review raises it again. Continue with resume-plan."
    )
    return ReopenResult(
        stage_id=obligation.stage_id,
        run=run.key,
        instance_id=instance_id,
        title=obligation.gate.title,
        failed_checks=failed,
        stage_directory=stage.directory,
        candidate_sha=stage_state.candidate_sha,
    )


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def turn_touched_paths(stage: Stage) -> tuple[str, ...]:
    """The repo-relative paths a stage's turns recorded writing.

    Read from the stage's own ``activity.jsonl`` (see
    :mod:`agent_sparring.activity`), whose ``file.changed`` events already
    carry a repo-relative path when one could be resolved. This is the only
    place in the package that reads that stream back, and it is deliberately
    not an orchestration decision: nothing about the reset *depends* on the
    list being complete. It is used to decide which worktree files the
    recovery will offer to quarantine, and every path it does not know about
    is still caught by the unrelated-changes refusal, which reads the
    worktree rather than the log.

    Unreadable or malformed lines are skipped: a truncated telemetry line
    must not stop a recovery, and the worktree check below does not trust
    this list anyway.
    """

    path = stage.activity_path()
    if not path.is_file():
        return ()
    seen: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("event") != "file.changed":
            continue
        touched = entry.get("path")
        if isinstance(touched, str) and touched and touched not in seen:
            seen.append(touched)
    return tuple(seen)


def _compiled_residue(repo_root: Path, path: str) -> tuple[Path, ...]:
    """Compiled copies of ``path`` that git cannot see.

    One case only, and it is here because it actually bit: a Python test
    file the erroneous turn created leaves a ``__pycache__/<stem>.*.pyc``
    beside it, in a directory every project git-ignores. Delete or quarantine
    the source and that cached module is still importable, so the next test
    run collects a test the recovery believed it had removed -- invisibly,
    because git never reported the file in the first place. Nothing else in
    this engine guesses at derived files, and this does not either: it looks
    for the compiled form of exactly one named source file, and finds
    nothing for every other language.
    """

    source = Path(path)
    if source.suffix != ".py":
        return ()
    cache = (Path(repo_root) / source.parent / "__pycache__").resolve()
    if not cache.is_dir():
        return ()
    return tuple(sorted(cache.glob(f"{source.stem}.*.pyc")))


def _classify(
    repo_root: Path, stage: Stage, touched: tuple[str, ...]
) -> tuple[list[tuple[str, Path]], list[str], list[str]]:
    """Split the attempt's files into (quarantine, absent, refusals).

    ``quarantine`` pairs each repo-relative path with the absolute file to
    move. ``refusals`` holds paths whose current state this command must not
    act on -- tracked content the attempt modified, which belongs to a
    commit and is not a scratch file to be swept aside.
    """

    try:
        dirty = {entry.path: entry for entry in unrepresented_dirty_entries(repo_root, stage)}
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal, not swallowed
        raise RecoveryError(f"cannot read the worktree status of {repo_root}: {exc}") from exc

    quarantine: list[tuple[str, Path]] = []
    absent: list[str] = []
    refusals: list[str] = []
    for path in touched:
        absolute = (Path(repo_root) / path).resolve()
        entry = dirty.get(path)
        # "??" is git's own marker for untracked; anything else with a status
        # is tracked content the attempt changed.
        if entry is not None and entry.status != "??":
            refusals.append(
                f"{path} is tracked and modified, so it is part of this branch's committed "
                "content rather than a leftover of the attempt"
            )
            continue
        if absolute.is_file():
            quarantine.append((path, absolute))
        else:
            absent.append(path)
    return quarantine, absent, refusals


def _unrelated_changes(
    repo_root: Path, stage: Stage, touched: tuple[str, ...]
) -> tuple[str, ...]:
    """Dirty paths that are neither this stage's artifacts nor the attempt's
    own files. Anything here refuses the reset rather than being cleaned up:
    it is someone's uncommitted work, and this command is not entitled to an
    opinion about it."""

    known = set(touched)
    out: list[str] = []
    for entry in unrepresented_dirty_entries(repo_root, stage):
        if entry.path in known or (entry.old_path is not None and entry.old_path in known):
            continue
        out.append(entry.path)
    return tuple(out)


def _archive_root(sparring_dir: Path, stage_id: str) -> Path:
    return Path(sparring_dir) / STAGES_DIRNAME / ARCHIVE_DIRNAME / stage_id


def _next_attempt_dir(sparring_dir: Path, stage_id: str, mode: StageMode) -> Path:
    """A never-reused directory for this attempt.

    Numbered from what is already archived, so a stage reset more than once
    keeps every attempt, in order, and no earlier archive can be written
    over.
    """

    root = _archive_root(sparring_dir, stage_id)
    existing = 0
    if root.is_dir():
        for entry in root.iterdir():
            head = entry.name.split("-", 1)[0]
            if head.isdigit():
                existing = max(existing, int(head))
    return root / f"{existing + 1:04d}-{mode.value}-{_utc_stamp()}"


def _recovery_note(result_fields: dict[str, object]) -> str:
    """The archive's own account of why it exists.

    Written into the archive rather than only reported, so the next person
    to open this directory can tell an abandoned attempt from a real stage
    without reconstructing anything from a terminal that has closed.
    """

    lines = [
        f"# Recovery: {result_fields['stage_id']}",
        "",
        f"Archived {result_fields['when']} by `sparring reset-stage`.",
        "",
        "This directory holds an attempt at the stage named above that was started "
        "under the wrong mode, and its authority was withdrawn. It is history: nothing "
        "resumes it, nothing reads its verdict, and no session of it is reused.",
        "",
        f"- Started as: {result_fields['from_mode']}",
        f"- Restarted as: {result_fields['to_mode']}",
        f"- Repository verified at: {result_fields['reviewed_sha']} "
        f"(the candidate accepted by {result_fields['reviewed_stage']})",
        "",
        "## Sessions that are no longer authoritative",
        "",
    ]
    sessions = result_fields["sessions"]
    assert isinstance(sessions, list)
    if sessions:
        lines += [f"- {role}: `{session}`" for role, session in sessions]
    else:
        lines.append("(none were recorded)")
    lines += [
        "",
        "## What is in here",
        "",
        f"- `{ATTEMPT_DIRNAME}/` — the stage directory exactly as it stood: state.json, "
        "brief.md, notes.md, handoff.md, sparring.md, activity.jsonl and every captured "
        "prompt.",
        f"- `{QUARANTINE_DIRNAME}/` — files the attempt wrote into the working tree, "
        "moved here at their repository-relative paths.",
    ]
    quarantined = result_fields["quarantined"]
    assert isinstance(quarantined, list)
    if quarantined:
        lines += [""] + [f"  - `{path}`" for path in quarantined]
    absent = result_fields["absent"]
    assert isinstance(absent, list)
    if absent:
        lines += [
            "",
            "Files the attempt wrote that were already gone from the working tree before "
            "this recovery ran, and so are not here:",
            "",
        ] + [f"- `{path}`" for path in absent]
    return "\n".join(lines).rstrip() + "\n"


def reset_stage(
    source: PlanSource,
    sparring_dir: Path,
    repo_root: Path,
    *,
    stage_id: str,
    expected_branch: str,
    expect_mode: StageMode | None = None,
    report=lambda message: None,
) -> ResetResult:
    """Archive an unaccepted current stage and restart it under its declared
    mode.

    ``source`` is the plan input the run will continue with -- the same
    manifest or Markdown plan ``resume-plan`` is given -- and it is the
    authority for the new mode and the new brief. ``expect_mode``, when
    given, must equal the mode that plan input declares for the stage; it is
    there so the intent is stated at the call site and a stale plan input is
    caught rather than quietly obeyed.

    Refuses (:class:`RecoveryError`) without touching anything if: no
    recorded run of this plan is currently at ``stage_id`` (a stage a run
    has already moved past is history, not something to restart, and one
    document may have several recorded runs); the run's source kind differs
    from this input's; the stage does not exist; the stage is ACCEPTED; the plan input declares
    the mode the stage already ran under, so there is nothing to recover
    from; ``expect_mode`` disagrees with the plan input; the stages before
    this one are not the same stages in the same order, or any of them is
    not accepted with a real candidate; the repository is on another branch
    or not at the preceding accepted candidate; the attempt modified tracked
    content; or the worktree holds changes that are not the attempt's.
    """

    # Imported here rather than at module import time: plan.py imports the
    # review lifecycle, which imports the acceptance gate, and this module
    # is a peer of plan.py rather than a dependency of it.
    from agent_sparring.plan import (
        PLANS_DIRNAME,
        MarkdownPlanSource,
        PlanRunStatus,
        find_runs,
        legacy_run_key,
    )

    # Which run's current stage this is. One document may have been executed
    # more than once, and ``stage_id`` says which of those executions is
    # meant, because a stage instance belongs to exactly one run.
    recorded = find_runs(sparring_dir, source.label)
    owning = [run for run in recorded if run.state.current_stage == stage_id]
    if not owning:
        if not recorded:
            raise RecoveryError(
                f"no plan run is recorded for {source.label} in "
                f"{Path(sparring_dir) / PLANS_DIRNAME}; there is no run whose current "
                "stage could be reset"
            )
        raise RecoveryError(
            f"no recorded run of {source.label} is currently at {stage_id!r} "
            f"(recorded: {', '.join(f'{run.key} at {run.state.current_stage!r}' for run in recorded)}). "
            "Only the current stage can be reset: a stage a run has already moved past "
            "was accepted, and restarting it would rewrite history the later stages were "
            "built on."
        )
    run = owning[0]
    state_path = run.path
    run_state = run.state

    if run_state.status is PlanRunStatus.COMPLETE:
        raise RecoveryError(
            f"the plan run {run.key} of {source.label} is complete; nothing is current, so "
            "there is no stage to reset"
        )
    # The stage ids this reset reasons about are the owning run's, not the
    # ones the document would generate on its own.
    if isinstance(source, MarkdownPlanSource):
        source = source.in_namespace(run_state.run or legacy_run_key(run_state.plan))
    stages = source.stages()
    if run_state.source != source.kind:
        raise RecoveryError(
            f"the plan run for {source.label} was started from a {run_state.source} plan "
            f"input, not a {source.kind} one; refusing to reset a stage of it from a "
            "different kind of input"
        )
    if run_state.expected_branch != expected_branch:
        raise RecoveryError(
            f"the plan run for {source.label} was started for branch "
            f"{run_state.expected_branch!r}, not {expected_branch!r}; refusing to reset a "
            "stage of it for a different branch"
        )
    verify_run_context = getattr(source, "verify_run_context", None)
    if callable(verify_run_context):
        # A sealed intake run continues only from its approved worktree.
        try:
            verify_run_context(Path(repo_root), expected_branch=expected_branch, fresh=False)
        except ManifestError as exc:
            raise RecoveryError(str(exc)) from exc
    index = run_state.current_stage_index
    if not 0 <= index < len(stages):
        raise RecoveryError(
            f"recorded stage index {index} is out of range for the {len(stages)} stage(s) "
            "this plan input declares"
        )
    planned = stages[index]
    if planned.stage_id != stage_id:
        raise RecoveryError(
            f"this plan input has {planned.stage_id!r} at position {index + 1}, but the "
            f"run is at {stage_id!r}; refusing to reset a stage against a plan input that "
            "orders the sequence differently"
        )
    if expect_mode is not None and expect_mode is not planned.mode:
        raise RecoveryError(
            f"this plan input declares {stage_id!r} as {planned.mode.describe}, but the "
            f"reset was asked for {expect_mode.describe}. Regenerate the plan input, or "
            "ask for the mode it actually declares; refusing to act on a disagreement "
            "about which lifecycle the stage should run."
        )

    stage = _resolve(sparring_dir, stage_id)
    if not stage.exists():
        raise RecoveryError(
            f"stage {stage_id!r} does not exist at {stage.directory}; there is no attempt "
            "to archive. Run the plan normally instead."
        )
    try:
        stage_state = stage.read_state()
    except StageError as exc:
        raise RecoveryError(f"cannot read the state of stage {stage_id!r}: {exc}") from exc
    if stage_state.status is StageStatus.ACCEPTED:
        raise RecoveryError(
            f"stage {stage_id!r} is ACCEPTED at {stage_state.candidate_sha}; acceptance is "
            "terminal and this command will not undo it"
        )
    if stage_state.mode is planned.mode:
        raise RecoveryError(
            f"stage {stage_id!r} already ran as {planned.mode.describe}, which is what "
            "this plan input declares; there is no wrong-mode attempt to recover from. If "
            "the attempt is simply unwanted, that is a different decision and this command "
            "is not it."
        )

    reviewed = _verify_preceding(sparring_dir, stages, index)
    branch, head = _verify_repository(repo_root, expected_branch, reviewed)

    touched = turn_touched_paths(stage)
    quarantine, absent, refusals = _classify(repo_root, stage, touched)
    if refusals:
        listed = "\n  - ".join(refusals)
        raise RecoveryError(
            f"refusing to reset stage {stage_id!r}: the attempt changed content that is "
            f"committed on this branch, and discarding that is not this command's "
            f"decision:\n  - {listed}\n"
            "Decide what to do with those changes first -- keep them as their own stage, "
            "or revert them deliberately -- then run this again."
        )
    unrelated = _unrelated_changes(repo_root, stage, touched)
    if unrelated:
        listed = ", ".join(unrelated)
        raise RecoveryError(
            f"refusing to reset stage {stage_id!r}: the working tree holds changes this "
            f"command cannot account for ({listed}). They are not this stage's workflow "
            "artifacts and not files the attempt recorded writing, so they are someone's "
            "uncommitted work. Commit, stash or discard them deliberately; nothing was "
            "moved."
        )

    # Everything is verified. From here on the filesystem changes, in the
    # order that leaves the least behind if it is interrupted: the archive
    # is populated first, the stage is recreated immediately after, and the
    # run's digest -- the only plan-run state this touches -- is rewritten
    # last.
    attempt = _next_attempt_dir(sparring_dir, stage_id, stage_state.mode)
    attempt.mkdir(parents=True, exist_ok=False)

    files: list[TouchedFile] = []
    for path, absolute in quarantine:
        destination = attempt / QUARANTINE_DIRNAME / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(absolute), str(destination))
        files.append(TouchedFile(path=path, disposition="quarantined"))
    for path in absent:
        files.append(TouchedFile(path=path, disposition="absent"))
    # Compiled copies of the attempt's own files, quarantined with them so a
    # later test run cannot import a module whose source is gone.
    for path in touched:
        for residue in _compiled_residue(repo_root, path):
            try:
                relative = residue.relative_to(Path(repo_root).resolve()).as_posix()
            except ValueError:
                continue
            destination = attempt / QUARANTINE_DIRNAME / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(residue), str(destination))
            files.append(TouchedFile(path=relative, disposition="residue"))

    sessions = [
        (role, session)
        for role, session in (
            ("implementation", stage_state.implementation_session_id),
            ("sparring", stage_state.sparring_session_id),
        )
        if session
    ]
    (attempt / RECOVERY_FILENAME).write_text(
        _recovery_note(
            {
                "stage_id": stage_id,
                "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "from_mode": stage_state.mode.describe,
                "to_mode": planned.mode.describe,
                "reviewed_sha": head,
                "reviewed_stage": f"{reviewed[0].display} [{reviewed[0].stage_id}]",
                "sessions": sessions,
                "quarantined": [f.path for f in files if f.disposition != "absent"],
                "absent": [f.path for f in files if f.disposition == "absent"],
            }
        ),
        encoding="utf-8",
    )

    shutil.move(str(stage.directory), str(attempt / ATTEMPT_DIRNAME))
    stage = _resolve(sparring_dir, stage_id)
    try:
        stage.create(brief=planned.brief, mode=planned.mode)
    except StageError as exc:  # pragma: no cover -- the directory was just moved away
        raise RecoveryError(
            f"the attempt for stage {stage_id!r} was archived to {attempt}, but the stage "
            f"could not be recreated: {exc}"
        ) from exc
    stage.activity_log().emit(
        "recovery",
        "stage.reset",
        summary=f"restarted as {planned.mode.value}; previous attempt archived",
    )

    previous_digest = run_state.plan_digest
    digest = source.digest()
    run_state.plan_digest = digest
    run_state.status = PlanRunStatus.PAUSED
    run_state.provider_pause = None
    # The stage that was stopped is gone, so any typed reason the run was
    # stopped *at* it is gone with it -- a request to push a candidate whose
    # stage has just been archived describes nothing. A recorded push
    # authorization is left alone: a one-candidate one no longer covers
    # anything (it names the archived commit), and a run-scoped one is a
    # decision about the run, which this operation does not revisit.
    run_state.awaiting = None
    run_state.save(state_path)

    result = ResetResult(
        stage_id=stage_id,
        run=run.key,
        from_mode=stage_state.mode,
        to_mode=planned.mode,
        archive=attempt,
        stage_directory=stage.directory,
        reviewed_stage=reviewed[0].display,
        reviewed_stage_id=reviewed[0].stage_id,
        reviewed_sha=head,
        previous_digest=previous_digest,
        digest=digest,
        discarded_sessions=tuple(sessions),
        files=tuple(files),
    )
    report(
        f"stage {stage_id}: archived the {stage_state.mode.value} attempt to {attempt} "
        f"and reinitialised it as {planned.mode.value} at {stage.directory}"
    )
    report(
        f"stage {stage_id}: {branch} verified at {head}, the candidate accepted by "
        f"{reviewed[0].display}"
    )
    return result


def _resolve(sparring_dir: Path, stage_id: str) -> Stage:
    try:
        return Stage.resolve(sparring_dir, stage_id)
    except StageError as exc:
        raise RecoveryError(str(exc)) from exc


@dataclass(frozen=True)
class _Accepted:
    stage_id: str
    display: str
    candidate_sha: str


def _verify_preceding(
    sparring_dir: Path, stages: tuple[PlannedStage, ...], index: int
) -> tuple[_Accepted, ...]:
    """Every stage before ``index``, verified accepted, most recent first.

    This is the check that makes the recovery safe to run at all: it proves
    the work the restarted stage will review is on disk, accepted, and
    identified the same way by the plan input the run continues with. It
    writes nothing to any of them.
    """

    if index == 0:
        raise RecoveryError(
            "the run is at the first stage of this plan, so there is no accepted work for "
            "a restarted stage to be verified against"
        )
    verified: list[_Accepted] = []
    for planned in stages[:index]:
        earlier = _resolve(sparring_dir, planned.stage_id)
        if not earlier.exists():
            raise RecoveryError(
                f"{planned.display} [{planned.stage_id}] precedes the stage being reset "
                f"but does not exist at {earlier.directory}; refusing to restart a stage "
                "whose predecessors cannot be verified"
            )
        try:
            earlier_state = earlier.read_state()
        except StageError as exc:
            raise RecoveryError(
                f"cannot read the state of {planned.display} [{planned.stage_id}]: {exc}"
            ) from exc
        if earlier_state.status is not StageStatus.ACCEPTED:
            raise RecoveryError(
                f"{planned.display} [{planned.stage_id}] has status "
                f"{earlier_state.status.value!r}, not accepted; refusing to restart a "
                "later stage while an earlier one is unfinished"
            )
        if not is_full_sha(earlier_state.candidate_sha):
            raise RecoveryError(
                f"{planned.display} [{planned.stage_id}] is accepted with no usable "
                f"candidate commit ({earlier_state.candidate_sha!r}); refusing to guess "
                "which commit it accepted"
            )
        verified.append(
            _Accepted(
                stage_id=planned.stage_id,
                display=planned.display,
                candidate_sha=str(earlier_state.candidate_sha),
            )
        )
    verified.reverse()
    return tuple(verified)


def _verify_repository(
    repo_root: Path, expected_branch: str, reviewed: tuple[_Accepted, ...]
) -> tuple[str, str]:
    """The primary repository must be on ``expected_branch`` and at the
    commit the preceding stage accepted."""

    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise RecoveryError(str(exc)) from exc
    if branch != expected_branch:
        raise RecoveryError(
            f"expected branch {expected_branch!r} but {repo_root} is on {branch!r}; "
            "refusing to restart a stage from the wrong branch"
        )
    try:
        head = resolve_commit(repo_root, "HEAD", label="current HEAD")
    except GitContextError as exc:
        raise RecoveryError(str(exc)) from exc
    latest = reviewed[0]
    if head != latest.candidate_sha:
        raise RecoveryError(
            f"{repo_root} is at {head}, but {latest.display} [{latest.stage_id}] accepted "
            f"{latest.candidate_sha}. Refusing to restart the stage: it would review a "
            "different commit than the one the plan says was accepted. Check out the "
            "accepted candidate, or accept whatever moved the branch as its own stage "
            "first. Nothing was moved."
        )
    return branch, head


__all__ = [
    "ARCHIVE_DIRNAME",
    "RecoveryError",
    "ReopenResult",
    "ResetResult",
    "TouchedFile",
    "reopen_for_failed_check",
    "reset_stage",
    "turn_touched_paths",
]
