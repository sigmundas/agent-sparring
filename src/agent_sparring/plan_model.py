"""The one internal representation every plan input produces.

The plan runner (:mod:`agent_sparring.plan`) accepts two inputs today:

- a reviewed Markdown plan with ``## Stage <n> — <title>`` sections, parsed
  by the engine itself (:func:`agent_sparring.plan.parse_plan`);
- an execution *manifest* (:mod:`agent_sparring.manifest`), emitted by a
  caller that has already done the interpreting -- which headings are
  canonical stages, how labels like ``3A``/``3B``/``3C`` order, which
  sections are historical handoffs rather than definitions, and exactly what
  each stage's brief says.

Both are turned into the same ordered tuple of :class:`PlannedStage` before
anything runs, so there is exactly one execution path, one run-state model
and one ``_drive``. A manifest is *input*, not a second workflow engine: it
carries no status, no position, no transitions and no verdicts. Where a run
is, whether a stage is accepted, which candidate was frozen and which
provider sessions exist all stay where they already live -- the plan-run
state file and each stage's ``state.json``.

The digest is what makes a recorded run refuse to continue against changed
execution content. Each source computes its own over exactly the content it
executes (see :meth:`PlanSource.digest`), and the runner re-reads the source
from disk and re-checks that digest on resume, before accepting a candidate,
and before advancing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_sparring.stage import CandidateRepository


@dataclass(frozen=True)
class PlannedStage:
    """One stage as the runner sees it, whatever produced it.

    ``position`` is the 1-based execution order and is the *only* thing that
    decides sequencing: ``label`` is a display string (``"1"``, ``"3C"``,
    ``"Stage 3C"``) and is never parsed to derive an order. That is the
    difference that lets a manifest carry ``3A``/``3B``/``3C`` without the
    engine growing a label grammar.

    ``brief`` is the exact ``brief.md`` content for the stage, byte for byte;
    the runner writes it verbatim and, for a stage that already exists and is
    not accepted, requires the existing brief to equal it.

    ``repositories`` declares sibling repositories whose reviewed candidates
    belong to this stage (see
    :class:`~agent_sparring.stage.CandidateRepository`); empty for the
    ordinary single-repository stage.
    """

    position: int
    label: str
    title: str
    stage_id: str
    brief: str
    repositories: tuple[CandidateRepository, ...] = field(default_factory=tuple)

    @property
    def display(self) -> str:
        """``Stage 3C — Cloud schema``: how this stage is named in output."""

        label = self.label if self.label.lower().startswith("stage") else f"Stage {self.label}"
        return f"{label} — {self.title}" if self.title else label


@runtime_checkable
class PlanSource(Protocol):
    """Where an ordered list of planned stages comes from.

    Implementations are cheap value objects read from disk at construction
    (:class:`~agent_sparring.plan.MarkdownPlanSource`,
    :class:`~agent_sparring.manifest.ManifestPlanSource`). ``reload`` re-reads
    the same file so the runner can prove the execution content has not
    changed mid-run.
    """

    #: ``"markdown"`` or ``"manifest"``; recorded in the run state so a run
    #: cannot silently switch input kinds between start and resume.
    kind: str
    #: How the plan is named in state and output; for a manifest this is the
    #: human plan document it was built from, so both inputs key the same
    #: run-state file for the same plan.
    label: str
    #: The file this source was read from.
    path: Path

    def digest(self) -> str:
        """SHA-256 over exactly the content this source executes."""

    def stages(self) -> tuple[PlannedStage, ...]:
        """The plan's stages in execution order."""

    def reload(self) -> "PlanSource":
        """Re-read the same file, so the digest can be re-checked."""


def digest_planned_stages(*parts: str) -> str:
    """SHA-256 over NUL-separated content parts.

    Shared so every source digests the same way; what each source *feeds*
    in is its own decision about what counts as execution content.
    """

    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["PlanSource", "PlannedStage", "digest_planned_stages"]
