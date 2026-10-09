"""Committing a reviewed candidate without letting it change.

A stage whose implementation is deliberately left uncommitted until a human
has verified it (the convention a project states in its own AGENTS.md/
PROJECT.md, not something this package invents) reaches READY with the
reviewed work still in the working tree. READY is the reviewer saying "no
unresolved implementation or human-gate issue remains"; it is not "there is
a pushed commit". The acceptance gate, correctly, only ever freezes an exact
commit, so something has to turn that reviewed working tree into one --
and the one thing it must not do while turning it into a commit is change
it, because the human verified the tree, not a re-interpretation of it.

This module is the factual half of that step. It answers two questions and
nothing else:

    pending_finalization()  is the reviewed candidate still uncommitted, and
                            what exactly does its content consist of?
    verify_finalized()      after the commit turn, is the committed content
                            the same content, path by path?

:mod:`agent_sparring.loop` does the routing (one bounded commit/push turn,
then the ordinary sparring turn over the exact committed SHA); this module
holds no lifecycle state, writes no ``state.json``, and decides nothing
about acceptance.

Why content, not "did it commit"
--------------------------------

"The tree is clean now" is not evidence that the human-verified work was
committed: a turn that rewrote a file and committed the rewrite leaves an
equally clean tree. So the comparison is made on content git itself
records -- every path's mode and blob id -- read the same way from both
sides via a throwaway index, so a reading taken from the working tree and a
reading taken from a commit are directly comparable.

Excluded from that content, on both sides:

- anything git ignores, which is skipped by ``git add -A`` exactly as it is
  by every other operation in this package;
- stage artifact files -- ``state.json``, ``brief.md``, ``notes.md``,
  ``handoff.md``, ``sparring.md``, ``activity.jsonl`` -- under the stages
  root this stage lives in. The engine rewrites those on every turn and a
  finalization turn is *expected* to change them, handoff.md above all, so
  including them would report a divergence on every single run.

That exemption is deliberately one step wider than the acceptance gate's,
which exempts only *this* stage's six files and blocks on a sibling stage's
(see :func:`agent_sparring.acceptance.freeze_candidate`). The two are
answering different questions. The gate asks "is this whole worktree
represented by the commit I am about to pin?", and a sibling stage's
uncommitted bookkeeping genuinely is not -- so it refuses, and it still
refuses, unchanged. This module asks the narrower question "is this stage's
reviewed implementation still uncommitted?", and another stage's workflow
files are not an answer to it: routing an implementation turn to commit
them would be wrong, so they must not trigger one. Neither the gate's
allowlist nor its refusal is relaxed by anything here; a project that
leaves such files around still gets the same freeze refusal it gets today.

The wider rule stays a rule about exact filenames at an exact depth --
``<stages root>/<one directory>/<one of those six names>`` -- and never a
subtree exemption, for the same reason the gate's is an allowlist: naming
some other tree ``sparring_dir`` must not make arbitrary content invisible.

Reading the working tree writes blob objects into the repository's object
database (that is how ``git add`` computes a blob id at all). It touches no
ref, no branch, no real index and no file in the working tree; the
unreferenced blobs are ordinary garbage-collectable objects. Nothing here
ever commits, resets, checks out or stages anything on the caller's behalf.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_sparring.acceptance import STAGE_ARTIFACT_FILENAMES
from agent_sparring.prompt_capture import PROMPTS_DIRNAME, is_prompt_artifact
from agent_sparring.git_context import (
    DirtyEntry,
    GitContextError,
    dirty_entries,
    resolve_commit,
)
from agent_sparring.stage import FINALIZATION_HEADING, Stage, StageError


class FinalizationError(RuntimeError):
    """Raised when a candidate's content cannot be read or compared.

    A read failure, never a verdict: whether a finalization turn preserved
    the reviewed work is reported as a :class:`FinalizationOutcome`, not as
    an exception.
    """


@dataclass(frozen=True)
class CandidateContent:
    """Every file that makes up a candidate's content, as git records it.

    ``entries`` pairs each repo-relative path with the exact ``"<mode>
    <blob>"`` git would store for it, sorted by path, so two readings are
    comparable whether they came from the working tree or from a commit.
    """

    entries: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        """A single stable id for this content set, for logs and messages."""

        joined = "\n".join(f"{path}\0{blob}" for path, blob in self.entries)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.entries)

    def diverged_from(self, other: "CandidateContent") -> tuple[str, ...]:
        """Every path whose content differs between the two readings --
        changed, added or missing -- sorted, so a message can name them."""

        mine = dict(self.entries)
        theirs = dict(other.entries)
        return tuple(
            sorted(path for path in set(mine) | set(theirs) if mine.get(path) != theirs.get(path))
        )


@dataclass(frozen=True)
class PendingFinalization:
    """A reviewed candidate that is not a commit yet.

    ``reviewed`` is the content as the sparrer saw it; ``reviewed_head`` is
    where HEAD stood at that moment, so a later message can say what the
    commit turn moved. ``uncommitted_paths`` is the candidate content that
    is not in any commit yet -- what the commit turn has to capture.
    """

    uncommitted_paths: tuple[str, ...]
    reviewed: CandidateContent
    reviewed_head: str


@dataclass(frozen=True)
class FinalizationOutcome:
    """What a finalization turn actually produced.

    ``preserved`` is the only question acceptance may proceed on: the
    reviewed content is now committed, all of it, unchanged.
    """

    candidate_sha: str
    committed: CandidateContent
    still_uncommitted: tuple[str, ...] = ()
    diverged_paths: tuple[str, ...] = ()

    @property
    def preserved(self) -> bool:
        return not self.still_uncommitted and not self.diverged_paths


def _is_stage_artifact(repo_root: Path, stage: Stage, path: str) -> bool:
    """Is ``path`` one stage's workflow bookkeeping, rather than candidate
    content?

    True only for a repo-relative path of exactly the shape ``<stages
    root>/<one directory>/<one of the six artifact filenames>``, or one
    level deeper for a captured prompt: ``<stages root>/<one directory>/
    prompts/<a filename prompt_capture writes>``. The stages root is the one
    this stage itself lives under (``stage.directory.parent``, never a
    caller-supplied directory name). Depth and filename are exact in both
    shapes, so this can never widen into a subtree exemption -- a stray file
    in ``prompts/``, or anything nested below it, is still candidate
    content. See the module docstring for why this is one step wider than
    the acceptance gate's per-stage allowlist and why that does not weaken
    the gate.

    Captured prompts belong on this side of the line for the same reason
    the other artifacts do: a turn writes one every time it runs, including
    the bounded commit turn itself, so treating them as candidate content
    would make every finalization refuse itself.
    """

    try:
        rel_root = stage.directory.parent.resolve().relative_to(repo_root.resolve())
    except ValueError:
        # The stages root is outside the repository, so no repo-relative
        # path can be one of its files.
        return False
    root_parts = rel_root.parts
    parts = PurePosixPath(path).parts
    if parts[: len(root_parts)] != root_parts:
        return False
    if len(parts) == len(root_parts) + 2:
        return parts[-1] in STAGE_ARTIFACT_FILENAMES
    if len(parts) == len(root_parts) + 3:
        return parts[-2] == PROMPTS_DIRNAME and is_prompt_artifact(parts[-1])
    return False


def _entry_is_stage_artifact(repo_root: Path, stage: Stage, entry: DirtyEntry) -> bool:
    """A rename counts as bookkeeping only if *both* sides are, mirroring
    the acceptance gate: exempting only the destination would let a rename
    away from real content hide behind an artifact-looking name."""

    if not _is_stage_artifact(repo_root, stage, entry.path):
        return False
    if entry.old_path is not None and not _is_stage_artifact(repo_root, stage, entry.old_path):
        return False
    return True


def _uncommitted_content_paths(repo_root: Path, stage: Stage) -> tuple[str, ...]:
    """Working-tree changes that are candidate content, not bookkeeping.

    Uses ``--untracked-files=all`` so a brand-new untracked directory is
    checked file by file rather than collapsing into one ``?? dir/`` entry
    that could hide real content behind an artifact-looking name.
    """

    try:
        entries = dirty_entries(repo_root, all_untracked=True)
    except GitContextError as exc:
        raise FinalizationError(str(exc)) from exc
    return tuple(
        (
            f"{entry.path} (renamed from {entry.old_path})"
            if entry.old_path is not None
            else entry.path
        )
        for entry in entries
        if not _entry_is_stage_artifact(repo_root, stage, entry)
    )


def _git(repo_root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise FinalizationError(
            result.stderr.strip() or f"git {' '.join(args)} failed in {repo_root}"
        )
    return result.stdout


def _read_content(
    repo_root: Path, stage: Stage, *, rev: str, include_worktree: bool
) -> CandidateContent:
    """Read one candidate content set through a throwaway index.

    ``include_worktree`` reads the working tree on top of ``rev`` (``git add
    -A``, which honours .gitignore); without it, ``rev``'s own tree is read
    as it stands. Both paths end in the same ``ls-files -s`` parse and the
    same exemption filter, which is what makes the two readings comparable.
    """

    with tempfile.TemporaryDirectory(prefix="agent-sparring-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        _git(repo_root, "read-tree", rev, env=env)
        if include_worktree:
            _git(repo_root, "add", "-A", env=env)
        raw = _git(repo_root, "ls-files", "-s", "-z", env=env)
    return CandidateContent(
        entries=tuple(
            (path, entry) for path, entry, _ in _parse_index(repo_root, stage, raw)
        )
    )


def _parse_index(repo_root: Path, stage: Stage, raw: str) -> list[tuple[str, str, str]]:
    """``(path, "<mode> <blob>", merge stage)`` for each ``ls-files -s -z``
    entry that is not stage bookkeeping, sorted."""

    entries: list[tuple[str, str, str]] = []
    for token in raw.split("\0"):
        if not token:
            continue
        meta, separator, path = token.partition("\t")
        fields = meta.split()
        if not separator or not path or len(fields) < 3:
            raise FinalizationError(f"unparseable git index entry {token!r} in {repo_root}")
        if _is_stage_artifact(repo_root, stage, path):
            continue
        entries.append((path, f"{fields[0]} {fields[1]}", fields[2]))
    return sorted(entries)


def read_index_content(repo_root: Path, stage: Stage) -> CandidateContent | None:
    """What the repository's real index stages, as candidate content entries;
    ``None`` while it holds an unmerged path, which has no single entry.

    The candidate itself is the working tree (:func:`read_worktree_content`);
    this is the separate record of which of it was staged, so that what an
    attempt staged can be shown unchanged, not only what it wrote."""

    raw = _git(repo_root, "ls-files", "-s", "-z")
    entries = _parse_index(repo_root, stage, raw)
    if any(merge_stage != "0" for _, _, merge_stage in entries):
        return None
    return CandidateContent(entries=tuple((path, entry) for path, entry, _ in entries))


def read_worktree_content(repo_root: Path, stage: Stage) -> CandidateContent:
    """The candidate content the working tree currently holds, committed or
    not: HEAD's tree with every working-tree change applied on top."""

    try:
        head = resolve_commit(repo_root, "HEAD", label="HEAD")
    except GitContextError as exc:
        raise FinalizationError(str(exc)) from exc
    return _read_content(repo_root, stage, rev=head, include_worktree=True)


def read_commit_content(repo_root: Path, stage: Stage, rev: str) -> CandidateContent:
    """The candidate content a commit holds, ignoring the working tree."""

    try:
        resolved = resolve_commit(repo_root, rev, label="candidate")
    except GitContextError as exc:
        raise FinalizationError(str(exc)) from exc
    return _read_content(repo_root, stage, rev=resolved, include_worktree=False)


def pending_finalization(repo_root: Path, stage: Stage) -> PendingFinalization | None:
    """Is this stage's reviewed candidate still uncommitted?

    ``None`` means the working tree holds no candidate content outside a
    commit, so HEAD already *is* the reviewed candidate and no finalization
    turn is wanted. Otherwise the reviewed content is captured now, while it
    is still exactly what the sparrer ruled on.
    """

    uncommitted = _uncommitted_content_paths(repo_root, stage)
    if not uncommitted:
        return None
    try:
        head = resolve_commit(repo_root, "HEAD", label="HEAD")
    except GitContextError as exc:
        raise FinalizationError(str(exc)) from exc
    return PendingFinalization(
        uncommitted_paths=uncommitted,
        reviewed=read_worktree_content(repo_root, stage),
        reviewed_head=head,
    )


def verify_finalized(
    repo_root: Path, stage: Stage, pending: PendingFinalization
) -> FinalizationOutcome:
    """Did the finalization turn commit the reviewed content, unchanged?

    Two separate ways to fail, reported separately because they need
    different fixes: work still sitting in the working tree
    (``still_uncommitted``), and committed content that is not the content
    that was reviewed (``diverged_paths``).
    """

    still_uncommitted = _uncommitted_content_paths(repo_root, stage)
    try:
        candidate_sha = resolve_commit(repo_root, "HEAD", label="HEAD")
    except GitContextError as exc:
        raise FinalizationError(str(exc)) from exc
    committed = read_commit_content(repo_root, stage, candidate_sha)
    return FinalizationOutcome(
        candidate_sha=candidate_sha,
        committed=committed,
        still_uncommitted=still_uncommitted,
        diverged_paths=committed.diverged_from(pending.reviewed),
    )


def describe_refusal(
    stage: Stage, pending: PendingFinalization, outcome: FinalizationOutcome
) -> str:
    """The factual account of a finalization turn that did not preserve the
    reviewed candidate -- used both as the refusal message and as the
    notes.md record, so the two cannot say different things."""

    lines = [
        f"The finalization turn for stage {stage.stage_id!r} did not produce the reviewed "
        f"candidate, so nothing was accepted. HEAD {pending.reviewed_head[:12]} -> "
        f"{outcome.candidate_sha[:12]}.",
    ]
    if outcome.still_uncommitted:
        lines.append(
            "Still outside any commit: "
            + ", ".join(outcome.still_uncommitted)
        )
    if outcome.diverged_paths:
        lines.append(
            "Committed content differs from the tree that was reviewed and human-verified: "
            + ", ".join(outcome.diverged_paths)
        )
        lines.append(
            "A commit turn may not change the work a human verified. Any manual check "
            "recorded for this stage covered the earlier content and does not carry "
            "forward to these paths; re-verify them, or make this a fresh "
            "implementation/review cycle, before this stage is accepted again."
        )
    return "\n\n".join(lines)


def record_refusal(stage: Stage, report: str) -> None:
    """Keep the refusal in the stage's own notes.md as well as in the run's
    output, under ``## Finalization``.

    A terminal closes; notes.md is what the next turn of both agents, and
    the next person to read the stage, actually see. Prose only -- nothing
    reads it back as state. A notes.md that cannot be read or written is not
    allowed to mask the refusal it was going to record, so the write failure
    is swallowed here and the caller still raises.
    """

    try:
        stage.append_note(FINALIZATION_HEADING, report)
    except (StageError, OSError):
        return


__all__ = [
    "CandidateContent",
    "FinalizationError",
    "FinalizationOutcome",
    "PendingFinalization",
    "describe_refusal",
    "pending_finalization",
    "read_commit_content",
    "read_worktree_content",
    "record_refusal",
    "verify_finalized",
]
