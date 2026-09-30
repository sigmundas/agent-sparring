"""Plan intake: turning an imperfect human plan into a reviewable manifest.

A reviewed plan is a human document, and a common one does not fit the
engine's strict Markdown convention: stages labelled ``0``/``1A``/``1B``,
constraints that live above the stage headings, stages that span
repositories, implementation mixed with a later production action,
dependencies stated in prose. The deterministic parser in
:mod:`agent_sparring.plan` deliberately does not grow into a
natural-language parser -- its strictness is what makes a run trustworthy
once it has started. The execution manifest (:mod:`agent_sparring.manifest`)
is already the boundary for "a caller that understands the document
interpreted it". This module is such a caller, with a person in the loop.

Three things stay separate
--------------------------

1. **The source plan.** The human's Markdown. Never written, by this module
   or anything it runs. Intake records its SHA-256 and a snapshot copy, and
   approval refuses if the document on disk no longer has that digest.
2. **The interpretation.** One fresh, read-only, schema-constrained agent
   turn (:meth:`~agent_sparring.providers.StructuredAgentAdapter.
   start_structured`) reads the whole plan plus project context and returns
   :data:`INTERPRETATION_SCHEMA`: run slices and their ordered stages,
   reusable context blocks, gates, exclusions, findings, a verdict and, in
   refine mode, an optional proposed amendment. Everything the engine can
   check about it, it checks (:func:`check_interpretation`). The
   interpretation is a visible file a person reviews; it is never workflow
   state, and nothing after approval reads it.
3. **The execution manifest.** The existing contract, inside an intake
   envelope. Only :func:`approve_plan` writes one, and only for a run slice
   a person explicitly approved, together with the ``approval.json`` that
   binds it. ``run-plan --manifest`` runs it only through that approval
   (see :mod:`agent_sparring.intake_approval`), and then exactly as any
   other manifest -- digest pinned, briefs frozen, every existing gate in
   force. The runtime never asks a model what the next stage is.

Briefs are assembled, not written
---------------------------------

The agent chooses *line ranges*; the engine renders each brief from the
source snapshot: the stage's context blocks verbatim, then the stage's own
source text verbatim, then the agent's optional ``intake_scope`` note under
a heading that says it is intake's, not the plan's, and that the plan text
governs where they differ. So every requirement in a brief is the source
plan's own text by construction, and the only new prose is labelled as such.
``intake_scope`` may narrow, make a boundary explicit, or defer to a gate;
a substantive change belongs in the refine-mode ``amended_plan``, which the
engine turns into a diff for the person and never applies.

What the engine enforces
------------------------

- **Source coverage.** Every substantive source line (non-blank, not a
  horizontal rule) must be accounted for by a stage's source text, a context
  block, a gate, or an explicit exclusion with a reason. Unaccounted text is
  a blocking finding: this is what stops a global constraint silently
  disappearing when briefs are assembled.
- **Run boundaries.** A manifest runs in one primary repository on one
  expected branch. A sibling declaration pins a coupled candidate; it does
  not move a stage's work. So intake groups stages into run slices, each
  with one primary repository, and a manifest is approved per slice from
  that repository's project.
- **Gates are boundaries, not notes.** The runtime cannot wait for a
  production deployment or an owner's go-ahead between two stages of one
  run, so a gate that blocks later executable work must fall between run
  slices. A gate inside a slice is a blocking finding, never a manifest
  whose order merely implies an unenforced prerequisite. Approving a slice
  requires the person to confirm, by id, every gate that blocks it; an
  earlier slice it depends on must instead be approved and its sealed run
  complete, which approval checks and records.
- **Repository state.** Every inspected repository is snapshotted at
  prepare (:func:`repository_snapshot`), and approval refuses drift: a
  different repository, or a commit that is neither the inspected one nor
  one an earlier slice of this intake was accepted at.
- **Branch.** A slice runs on the branch its primary repository has checked
  out when the slice is *approved*, at the commit intake inspected (or an
  earlier slice's accepted one) -- so the branch is chosen no later than
  approval, not inherited from whatever prepare-plan happened to see. A
  slice with an implementation stage is refused on a protected branch
  (:func:`~agent_sparring.branch_guard.is_protected_branch`), because
  run-plan would refuse it there; the person checks out a feature branch at
  that commit first (``sparring slice-branch --create``). A plan that names
  its own branch still needs intake to have seen it checked out.
- **Dependency order.** Every ``depends_on`` must point at a stage that runs
  earlier (in an earlier slice, or earlier in the same slice).

Artifacts
---------

``.sparring/intake/<intake id>/`` is engine-written runtime state and must
be git-ignored (see :func:`require_intake_ignored`); ``check-config``
reports it. Inside: ``source.md`` (the snapshot), ``prompt.md``,
``interpretation.json`` (the agent's answer, verbatim content),
``briefs/<run>/<NN>-<label>.md`` (exactly the bytes a manifest will carry),
``amendment.diff`` (refine mode, when proposed), ``report.md`` and
``intake.json`` (the engine's record: digests, run keys, provider,
repository snapshots, report digest). None of these is in manifest format,
so none can be handed to ``run-plan`` by mistake. Approval adds
``runs/<run>/manifest.json`` and creates ``runs/<run>/approval.json``
exactly once, plus ``intake/registry/<run key>.json`` in the project that
runs the slice; approving again must be identical.

Nothing here is specific to any project or plan. A project's plans are
data.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agent_sparring.branch_guard import is_protected_branch
from agent_sparring.git_context import GitContextError, current_branch, is_ignored, repository_identity
from agent_sparring.intake_approval import (
    APPROVAL_FILENAME,
    APPROVAL_VERSION,
    INTAKE_DIRNAME,
    MANIFEST_FILENAME,
    REGISTRY_DIRNAME,
    RUNS_DIRNAME,
    SOURCE_KIND as INTAKE_SOURCE_KIND,
    IntakeApprovalError,
    envelope_text,
    load_intake_manifest,
    read_approval,
    sha256_bytes,
    write_atomic,
    write_exclusive,
)
from agent_sparring.intake_prompt import MODE_FAITHFUL, MODE_REFINE, assemble_intake_prompt
from agent_sparring.manifest import MANIFEST_VERSION, ManifestError, manifest_digest, parse_manifest
from agent_sparring.plan import (
    PlanError,
    PlanRunState,
    PlanRunStatus,
    new_run_key,
    plan_key,
    plan_label,
    run_state_path,
)
from agent_sparring.providers import ProviderError, StructuredAgentAdapter
from agent_sparring.sparring_agent import repo_fingerprint
from agent_sparring.stage import Stage, StageError, StageMode, StageStatus, validate_stage_id

#: ``intake.json`` version. ``1`` is an unsealed proposal from before
#: approvals bound execution: recognised, and refused.
INTAKE_VERSION = 2
MODES: tuple[str, ...] = (MODE_FAITHFUL, MODE_REFINE)

#: ``render_brief``'s output format, recorded in ``intake.json`` as
#: ``brief_format_version`` by every intake prepared from this version on.
#: ``2`` adds the ``## Goal`` paragraph; an intake recording no value at all
#: was prepared before that field existed and rendered the ``1`` form (no
#: Goal). Approval re-renders each brief in whichever form its own intake
#: recorded, so a legacy intake's on-disk briefs -- reviewed and digested
#: before the Goal paragraph existed -- still match and still approve; only
#: a brief that no longer renders to what its own intake recorded is refused.
BRIEF_FORMAT_VERSION = 2

#: Name of the file that proves a ``prepare_plan`` turn finished. Written as
#: the very last step of a successful prepare; an intake whose ``intake.json``
#: names this key (every intake from this version on) is usable only once it
#: exists. An interrupted prepare -- killed after ``intake.json`` was written
#: but before the marker -- leaves a directory that looks complete but is not;
#: the marker is what tells the difference. Legacy intakes recorded before
#: this key existed have no marker to check and remain usable as before.
COMPLETION_MARKER_FILENAME = "prepared.json"

VERDICT_AS_WRITTEN = "executable_as_written"
VERDICT_WITH_RECOMMENDATIONS = "executable_with_recommendations"
VERDICT_CANNOT_INTERPRET = "cannot_interpret_faithfully"
VERDICTS: tuple[str, ...] = (
    VERDICT_AS_WRITTEN,
    VERDICT_WITH_RECOMMENDATIONS,
    VERDICT_CANNOT_INTERPRET,
)

SEVERITY_BLOCKING = "blocking"
SEVERITY_RECOMMENDATION = "recommendation"
SEVERITY_INFO = "info"
SEVERITIES: tuple[str, ...] = (SEVERITY_BLOCKING, SEVERITY_RECOMMENDATION, SEVERITY_INFO)

#: The plan-quality categories the agent reports under (see
#: :mod:`agent_sparring.intake_prompt`).
AGENT_FINDING_CODES: tuple[str, ...] = (
    "missing_acceptance_criteria",
    "omitted_global_context",
    "dependency_order_mismatch",
    "implementation_mixed_with_production",
    "cross_repository_without_candidate_strategy",
    "stage_too_broad",
    "repeated_or_contradictory_requirement",
    "stale_baseline_fact",
    "open_question_as_instruction",
    "ambiguous_source",
    "other",
)

GATE_KINDS: tuple[str, ...] = ("production", "manual", "external", "deferred")
STAGE_MODES: tuple[str, ...] = tuple(mode.value for mode in StageMode)

_HORIZONTAL_RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


class IntakeError(RuntimeError):
    """Intake or approval refused; nothing executable was produced."""


# -- the schema the agent answers in ------------------------------------------

# Codex strict mode: every property listed in "required", optional values
# expressed as a nullable type, additionalProperties false everywhere.
_RANGES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
        "required": ["start", "end"],
        "additionalProperties": False,
    },
}
_STRINGS_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


INTERPRETATION_SCHEMA: dict[str, Any] = _object(
    {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "summary": {"type": "string"},
        "context": {
            "type": "array",
            "items": _object(
                {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "ranges": _RANGES_SCHEMA,
                    "reason": {"type": "string"},
                }
            ),
        },
        "runs": {
            "type": "array",
            "items": _object(
                {
                    "id": {"type": "string"},
                    "primary_repository": {"type": "string"},
                    "expected_branch": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                    "stages": {
                        "type": "array",
                        "items": _object(
                            {
                                "label": {"type": "string"},
                                "title": {"type": "string"},
                                "source_ranges": _RANGES_SCHEMA,
                                "context_ids": _STRINGS_SCHEMA,
                                "repositories": _STRINGS_SCHEMA,
                                "depends_on": _STRINGS_SCHEMA,
                                "mode": {"type": "string", "enum": list(STAGE_MODES)},
                                "intake_scope": {"type": ["string", "null"]},
                                "rationale": {"type": "string"},
                            }
                        ),
                    },
                }
            ),
        },
        "gates": {
            "type": "array",
            "items": _object(
                {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": list(GATE_KINDS)},
                    "ranges": _RANGES_SCHEMA,
                    "after_stage": {"type": ["string", "null"]},
                    "blocks_stages": _STRINGS_SCHEMA,
                    "reason": {"type": "string"},
                }
            ),
        },
        "excluded": {
            "type": "array",
            "items": _object({"ranges": _RANGES_SCHEMA, "reason": {"type": "string"}}),
        },
        "findings": {
            "type": "array",
            "items": _object(
                {
                    "code": {"type": "string", "enum": list(AGENT_FINDING_CODES)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "stages": _STRINGS_SCHEMA,
                    "ranges": _RANGES_SCHEMA,
                    "message": {"type": "string"},
                }
            ),
        },
        "amended_plan": {"type": ["string", "null"]},
    }
)


# -- the parsed interpretation ------------------------------------------------


@dataclass(frozen=True)
class LineRange:
    start: int
    end: int

    def __str__(self) -> str:
        return f"{self.start}" if self.start == self.end else f"{self.start}–{self.end}"


@dataclass(frozen=True)
class ContextBlock:
    id: str
    title: str
    ranges: tuple[LineRange, ...]
    reason: str


@dataclass(frozen=True)
class IntakeStage:
    label: str
    title: str
    source_ranges: tuple[LineRange, ...]
    context_ids: tuple[str, ...]
    repositories: tuple[str, ...]
    depends_on: tuple[str, ...]
    mode: str
    intake_scope: str | None
    rationale: str


@dataclass(frozen=True)
class RunSlice:
    id: str
    primary_repository: str
    expected_branch: str | None
    rationale: str
    stages: tuple[IntakeStage, ...]


@dataclass(frozen=True)
class Gate:
    id: str
    title: str
    kind: str
    ranges: tuple[LineRange, ...]
    after_stage: str | None
    blocks_stages: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Exclusion:
    ranges: tuple[LineRange, ...]
    reason: str


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    stages: tuple[str, ...] = ()
    ranges: tuple[LineRange, ...] = ()
    #: ``"agent"`` for the intake agent's plan review, ``"engine"`` for a
    #: deterministic check the engine made of the interpretation.
    origin: str = "agent"

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCKING


@dataclass(frozen=True)
class Interpretation:
    verdict: str
    summary: str
    context: tuple[ContextBlock, ...]
    runs: tuple[RunSlice, ...]
    gates: tuple[Gate, ...]
    excluded: tuple[Exclusion, ...]
    findings: tuple[Finding, ...]
    amended_plan: str | None

    def stages(self) -> Iterable[tuple[int, int, RunSlice, IntakeStage]]:
        """Every stage with its (run index, stage index) coordinates."""

        for run_index, run in enumerate(self.runs):
            for stage_index, stage in enumerate(run.stages):
                yield run_index, stage_index, run, stage


def parse_interpretation(text: str) -> Interpretation:
    """Parse the agent's answer structurally, or raise :class:`IntakeError`.

    Only shape is checked here -- the provider already validated the
    schema, and this repeats the part the engine depends on rather than
    trusting it. What the answer *means* (ranges in bounds, coverage,
    boundaries, references) is :func:`check_interpretation`'s, which reports
    findings instead of refusing, so a person sees every problem at once.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IntakeError(f"the intake agent's answer is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise IntakeError("the intake agent's answer must be a JSON object")
    return _interpretation(payload)


def _interpretation(payload: Mapping[str, Any]) -> Interpretation:
    verdict = _enum(payload, "verdict", VERDICTS, "interpretation")
    return Interpretation(
        verdict=verdict,
        summary=_str(payload, "summary", "interpretation"),
        context=tuple(
            ContextBlock(
                id=_str(item, "id", "context block", nonempty=True),
                title=_str(item, "title", "context block"),
                ranges=_ranges(item, "ranges", "context block"),
                reason=_str(item, "reason", "context block"),
            )
            for item in _list(payload, "context", "interpretation")
        ),
        runs=tuple(_run(item) for item in _list(payload, "runs", "interpretation")),
        gates=tuple(
            Gate(
                id=_str(item, "id", "gate", nonempty=True),
                title=_str(item, "title", "gate"),
                kind=_enum(item, "kind", GATE_KINDS, "gate"),
                ranges=_ranges(item, "ranges", "gate"),
                after_stage=_optional_str(item, "after_stage", "gate"),
                blocks_stages=_strs(item, "blocks_stages", "gate"),
                reason=_str(item, "reason", "gate"),
            )
            for item in _list(payload, "gates", "interpretation")
        ),
        excluded=tuple(
            Exclusion(
                ranges=_ranges(item, "ranges", "exclusion"),
                reason=_str(item, "reason", "exclusion"),
            )
            for item in _list(payload, "excluded", "interpretation")
        ),
        findings=tuple(
            Finding(
                code=_enum(item, "code", AGENT_FINDING_CODES, "finding"),
                severity=_enum(item, "severity", SEVERITIES, "finding"),
                message=_str(item, "message", "finding"),
                stages=_strs(item, "stages", "finding"),
                ranges=_ranges(item, "ranges", "finding"),
                origin="agent",
            )
            for item in _list(payload, "findings", "interpretation")
        ),
        amended_plan=_optional_str(payload, "amended_plan", "interpretation", strip=False),
    )


def _run(item: Any) -> RunSlice:
    where = "run slice"
    if not isinstance(item, Mapping):
        raise IntakeError(f"each {where} must be a JSON object")
    return RunSlice(
        id=_str(item, "id", where, nonempty=True),
        primary_repository=_str(item, "primary_repository", where, nonempty=True),
        expected_branch=_optional_str(item, "expected_branch", where),
        rationale=_str(item, "rationale", where),
        stages=tuple(
            IntakeStage(
                label=_str(stage, "label", "stage", nonempty=True),
                title=_str(stage, "title", "stage", nonempty=True),
                source_ranges=_ranges(stage, "source_ranges", "stage"),
                context_ids=_strs(stage, "context_ids", "stage"),
                repositories=_strs(stage, "repositories", "stage"),
                depends_on=_strs(stage, "depends_on", "stage"),
                mode=_enum(stage, "mode", STAGE_MODES, "stage"),
                intake_scope=_optional_str(stage, "intake_scope", "stage"),
                rationale=_str(stage, "rationale", "stage"),
            )
            for stage in _list(item, "stages", where)
        ),
    )


def _list(payload: Any, key: str, where: str) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise IntakeError(f"each {where} must be a JSON object")
    value = payload.get(key)
    if not isinstance(value, list):
        raise IntakeError(f"{where} field {key!r} must be an array")
    for entry in value:
        if not isinstance(entry, (Mapping, str)):
            raise IntakeError(f"{where} field {key!r} holds a non-object entry")
    return value


def _str(payload: Any, key: str, where: str, *, nonempty: bool = False) -> str:
    if not isinstance(payload, Mapping):
        raise IntakeError(f"each {where} must be a JSON object")
    value = payload.get(key)
    if not isinstance(value, str):
        raise IntakeError(f"{where} field {key!r} must be a string")
    value = value.strip()
    if nonempty and not value:
        raise IntakeError(f"{where} field {key!r} must not be empty")
    return value


def _optional_str(payload: Any, key: str, where: str, *, strip: bool = True) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntakeError(f"{where} field {key!r} must be a string or null")
    if not value.strip():
        return None
    return value.strip() if strip else value


def _strs(payload: Any, key: str, where: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise IntakeError(f"{where} field {key!r} must be an array of strings")
    return tuple(entry.strip() for entry in value if entry.strip())


def _enum(payload: Any, key: str, allowed: Sequence[str], where: str) -> str:
    value = _str(payload, key, where)
    if value not in allowed:
        raise IntakeError(f"{where} field {key!r} is {value!r}; expected one of {list(allowed)}")
    return value


def _ranges(payload: Any, key: str, where: str) -> tuple[LineRange, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise IntakeError(f"{where} field {key!r} must be an array of line ranges")
    ranges: list[LineRange] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IntakeError(f"{where} field {key!r} holds a non-object range")
        start, end = entry.get("start"), entry.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
            raise IntakeError(f"{where} field {key!r} holds a range without integer start/end")
        ranges.append(LineRange(start, end))
    return tuple(ranges)


# -- the source plan ------------------------------------------------------------


@dataclass(frozen=True)
class SourcePlan:
    """The human document as intake read it. Never written back."""

    label: str
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def digest(self) -> str:
        return sha256_text(self.text)

    def substantive_lines(self) -> list[int]:
        """1-based numbers of the lines the coverage invariant is about."""

        return [
            number
            for number, line in enumerate(self.lines, start=1)
            if line.strip() and not _HORIZONTAL_RULE_RE.match(line)
        ]

    def excerpt(self, ranges: Sequence[LineRange]) -> str:
        """The exact source text of ``ranges``, in the order given."""

        lines = self.lines
        parts = ["\n".join(lines[r.start - 1 : r.end]).rstrip() for r in ranges]
        return "\n\n".join(part for part in parts if part.strip())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- deterministic checks -------------------------------------------------------


def check_interpretation(
    interpretation: Interpretation, source: SourcePlan, *, mode: str
) -> tuple[Finding, ...]:
    """Everything the engine can verify about an interpretation, as findings.

    Pure and deterministic, so approval recomputes exactly what prepare
    reported and a person's review of ``report.md`` is a review of what
    approval will enforce.
    """

    findings: list[Finding] = []

    def add(code: str, message: str, *, stages: Sequence[str] = (), ranges: Sequence[LineRange] = (), severity: str = SEVERITY_BLOCKING) -> None:
        findings.append(
            Finding(code=code, severity=severity, message=message, stages=tuple(stages), ranges=tuple(ranges), origin="engine")
        )

    total = len(source.lines)

    def valid(ranges: Sequence[LineRange], owner: str) -> list[LineRange]:
        good: list[LineRange] = []
        for r in ranges:
            if 1 <= r.start <= r.end <= total:
                good.append(r)
            else:
                add("invalid_range", f"{owner} cites lines {r.start}–{r.end}, outside 1–{total}")
        return good

    covered: set[int] = set()

    def cover(ranges: Iterable[LineRange]) -> None:
        for r in ranges:
            covered.update(range(r.start, r.end + 1))

    # Context blocks. Their text counts as accounted for only once a stage
    # attaches them (below): a block no brief carries is text that has
    # silently dropped out, which is exactly what coverage exists to catch.
    context_ids: set[str] = set()
    context_ranges: dict[str, list[LineRange]] = {}
    for block in interpretation.context:
        if block.id in context_ids:
            add("duplicate_id", f"context id {block.id!r} is declared twice")
        context_ids.add(block.id)
        good = valid(block.ranges, f"context block {block.id!r}")
        if not good:
            add("empty_reference", f"context block {block.id!r} cites no source text")
        context_ranges.setdefault(block.id, []).extend(good)

    # Run slices and stages. Ids and labels must also be unique once
    # slugged: they name brief files, run directories and stage ids.
    if not interpretation.runs:
        add("no_stages", "the interpretation defines no run slice and no stage")
    run_ids: set[str] = set()
    run_slugs: set[str] = set()
    label_slugs: set[str] = set()
    position: dict[str, tuple[int, int]] = {}
    for run_index, run in enumerate(interpretation.runs):
        if run.id in run_ids or _slug(run.id) in run_slugs:
            add("duplicate_id", f"run slice id {run.id!r} is not unique (ids are compared as slugs)")
        run_ids.add(run.id)
        run_slugs.add(_slug(run.id))
        if not run.stages:
            add("empty_run", f"run slice {run.id!r} has no stages")
        for stage_index, stage in enumerate(run.stages):
            if stage.label in position or _slug(stage.label) in label_slugs:
                add(
                    "duplicate_id",
                    f"stage label {stage.label!r} is not unique (labels are compared as slugs)",
                    stages=[stage.label],
                )
            label_slugs.add(_slug(stage.label))
            position.setdefault(stage.label, (run_index, stage_index))

    for run_index, stage_index, run, stage in interpretation.stages():
        owner = f"stage {stage.label}"
        good = valid(stage.source_ranges, owner)
        if not good:
            add("empty_reference", f"{owner} cites no source text of its own", stages=[stage.label])
        cover(good)
        for context_id in stage.context_ids:
            if context_id not in context_ids:
                add("unknown_reference", f"{owner} attaches unknown context block {context_id!r}", stages=[stage.label])
        if run.primary_repository in stage.repositories:
            add(
                "primary_declared_as_sibling",
                f"{owner} declares its run's primary repository {run.primary_repository!r} as a sibling",
                stages=[stage.label],
            )
        if len(set(stage.repositories)) != len(stage.repositories):
            add("duplicate_id", f"{owner} declares a sibling repository twice", stages=[stage.label])
        for dependency in stage.depends_on:
            where = position.get(dependency)
            if where is None:
                add("unknown_reference", f"{owner} depends on unknown stage {dependency!r}", stages=[stage.label])
            elif where >= (run_index, stage_index):
                add(
                    "dependency_not_satisfied",
                    f"{owner} depends on stage {dependency}, which does not run before it",
                    stages=[stage.label, dependency],
                )

    attached = {cid for _, _, _, stage in interpretation.stages() for cid in stage.context_ids}
    for block in interpretation.context:
        if block.id in attached:
            cover(context_ranges.get(block.id, ()))
        else:
            add(
                "unused_context",
                f"context block {block.id!r} is attached to no stage, so its text reaches no "
                "brief; attach it to the stages it constrains, or exclude it with a reason",
                ranges=block.ranges,
            )

    # Gates may only sit at run-slice boundaries. The runtime cannot wait for
    # anything between two stages of one run, so a gate after a stage that is
    # not its slice's last one would let the next stage run straight past it
    # -- whatever the agent said it blocks. A gate that blocks a stage must
    # come before that stage's slice starts, and is confirmed at approval.
    gate_ids: set[str] = set()
    for gate in interpretation.gates:
        owner = f"gate {gate.id!r}"
        if gate.id in gate_ids or gate.id in run_ids:
            add("duplicate_id", f"{owner}: id is not unique among gates and run slices")
        gate_ids.add(gate.id)
        good = valid(gate.ranges, owner)
        if not good:
            add("empty_reference", f"{owner} cites no source text")
        cover(good)
        after = position.get(gate.after_stage) if gate.after_stage else None
        if gate.after_stage and after is None:
            add("unknown_reference", f"{owner} follows unknown stage {gate.after_stage!r}")
        if after is not None:
            slice_ = interpretation.runs[after[0]]
            if after[1] != len(slice_.stages) - 1:
                following = slice_.stages[after[1] + 1].label
                add(
                    "gate_inside_run",
                    f"{owner} ({gate.kind}) follows stage {gate.after_stage}, but stage {following} "
                    f"runs straight after it in run slice {slice_.id!r}. The runtime cannot wait "
                    "for a gate between two stages of one run; end the slice at "
                    f"{gate.after_stage} and start a new one after the gate",
                    stages=[gate.after_stage or "", following],
                    ranges=good,
                )
        for blocked in gate.blocks_stages:
            where = position.get(blocked)
            if where is None:
                add("unknown_reference", f"{owner} blocks unknown stage {blocked!r}")
                continue
            if after is not None and where[0] <= after[0]:
                add(
                    "dependency_not_satisfied",
                    f"{owner} follows stage {gate.after_stage} but blocks stage {blocked}, which "
                    "does not run in a later run slice",
                    stages=[gate.after_stage or "", blocked],
                    ranges=good,
                )
            elif where[1] != 0:
                first = interpretation.runs[where[0]].stages[0].label
                add(
                    "gate_inside_run",
                    f"{owner} ({gate.kind}) blocks stage {blocked}, which is not the first stage "
                    f"of its run slice (stage {first} is). Either the gate really comes before "
                    f"{first}, or the slice must start at {blocked}; state `after_stage` and put "
                    "the blocked stage first in a later slice",
                    stages=[blocked],
                    ranges=good,
                )

    for exclusion in interpretation.excluded:
        good = valid(exclusion.ranges, "an exclusion")
        if not exclusion.reason:
            add("exclusion_without_reason", f"lines {', '.join(map(str, good))} are excluded without a reason", ranges=good)
        cover(good)

    for finding in interpretation.findings:
        valid(finding.ranges, f"agent finding {finding.code!r}")

    # Source coverage.
    for gap in _group(sorted(set(source.substantive_lines()) - covered), source):
        first = source.lines[gap.start - 1].strip()
        add(
            "uncovered_source",
            f"lines {gap} are not accounted for by any stage, context block, gate or "
            f"exclusion (starting {first[:80]!r})",
            ranges=[gap],
        )

    # Mode.
    amendment = interpretation.amended_plan
    if mode == MODE_FAITHFUL and amendment is not None and amendment != source.text:
        add(
            "amendment_in_faithful_mode",
            "faithful mode must not propose a plan amendment; re-run in refine mode or drop it",
        )
    if interpretation.verdict == VERDICT_CANNOT_INTERPRET and not any(
        f.blocking for f in interpretation.findings
    ):
        add(
            "unexplained_refusal",
            "the agent could not interpret the plan faithfully but raised no blocking finding saying why",
        )
    return tuple(findings)


def _group(numbers: Sequence[int], source: SourcePlan) -> list[LineRange]:
    """Contiguous runs of uncovered substantive lines, bridging blank lines."""

    substantive = set(source.substantive_lines())
    groups: list[LineRange] = []
    for number in numbers:
        if groups:
            last = groups[-1]
            between = range(last.end + 1, number)
            if not any(n in substantive for n in between):
                groups[-1] = LineRange(last.start, number)
                continue
        groups.append(LineRange(number, number))
    return groups


def all_findings(interpretation: Interpretation, source: SourcePlan, *, mode: str) -> tuple[Finding, ...]:
    """Agent findings then engine findings, blocking first within each."""

    engine = check_interpretation(interpretation, source, mode=mode)
    ordered = list(interpretation.findings) + list(engine)
    return tuple(sorted(ordered, key=lambda f: SEVERITIES.index(f.severity)))


def run_prerequisites(interpretation: Interpretation, run_id: str) -> tuple[str, ...]:
    """What a person must confirm before approving run slice ``run_id``:
    every gate that blocks one of its stages, and every earlier run slice
    one of its stages depends on. Sorted, for a stable record."""

    run_of: dict[str, str] = {
        stage.label: run.id for _, _, run, stage in interpretation.stages()
    }
    labels = {stage.label for _, _, run, stage in interpretation.stages() if run.id == run_id}
    required: set[str] = set()
    for gate in interpretation.gates:
        if labels.intersection(gate.blocks_stages):
            required.add(gate.id)
    for _, _, run, stage in interpretation.stages():
        if run.id != run_id:
            continue
        for dependency in stage.depends_on:
            other = run_of.get(dependency)
            if other is not None and other != run_id:
                required.add(other)
    return tuple(sorted(required))


def slice_ancestors(interpretation: Interpretation, run_id: str) -> tuple[str, ...]:
    """Every earlier run slice ``run_id`` depends on, directly or through
    another slice: the transitive closure of the slices in
    :func:`run_prerequisites`, excluding ``run_id`` itself. Sorted."""

    slices = {run.id for run in interpretation.runs}
    found: set[str] = set()
    pending = [run_id]
    while pending:
        for prior in run_prerequisites(interpretation, pending.pop()):
            if prior in slices and prior != run_id and prior not in found:
                found.add(prior)
                pending.append(prior)
    return tuple(sorted(found))


def split_prerequisites(interpretation: Interpretation, run_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(gates, earlier run slices)`` of :func:`run_prerequisites`.

    They are satisfied differently: a gate is a person's confirmation, an
    earlier slice is proven by its own approval and completed run -- never
    by naming it.
    """

    slices = {run.id for run in interpretation.runs}
    required = run_prerequisites(interpretation, run_id)
    return (
        tuple(r for r in required if r not in slices),
        tuple(r for r in required if r in slices),
    )


def slice_branch(run: RunSlice, repositories: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(expected branch, None)`` for a run slice, or ``(None, why not)``.

    The branch is the one its primary repository had checked out when intake
    inspected it. A plan that names a different branch is refused rather
    than trusted: the interpretation was made against what was inspected,
    so the intended branch is checked out and the plan prepared again.
    """

    snapshot = repositories.get(run.primary_repository)
    if not isinstance(snapshot, Mapping):
        return None, (
            f"repository {run.primary_repository!r} was not inspected by this intake; run "
            f"prepare-plan with --context-repository {run.primary_repository}=<path>"
        )
    inspected = str(snapshot.get("branch"))
    if run.expected_branch and run.expected_branch != inspected:
        return None, (
            f"the plan runs slice {run.id!r} on {run.expected_branch!r}, but "
            f"{run.primary_repository} was on {inspected!r} when intake inspected it; check out "
            f"{run.expected_branch!r} there and run prepare-plan again"
        )
    return inspected, None


def slice_runs_agent(run: RunSlice) -> bool:
    """Whether any stage of the slice runs an unattended implementation agent,
    which the branch guard never lets run on a protected branch."""

    return any(stage.mode == StageMode.IMPLEMENTATION.value for stage in run.stages)


def slice_stage_names(run: RunSlice) -> str:
    return ", ".join(f"Stage {stage.label}" for stage in run.stages)


def feature_branch_needed(run: RunSlice, repo_root: Path, branch: str) -> str:
    """Why an implementation slice cannot be approved on ``branch``."""

    return (
        f"{slice_stage_names(run)} needs a feature branch: it runs an implementation agent, "
        f"and no stage agent runs unattended on protected branch {branch!r}. Check out a "
        f"feature branch at this commit in {repo_root} (sparring slice-branch --create "
        "<branch> does it) and approve again"
    )


def approval_requirements(
    interpretation: Interpretation, repositories: Mapping[str, Any], *, amendment: bool
) -> dict[str, dict[str, Any]]:
    """Per run slice, what ``approve-plan`` will ask for, as display metadata.

    The single source for both ``report.md``'s "Next step" and the copy in
    ``intake.json`` that tools (the VS Code extension) show without parsing
    the report. It is **not** an input to approval: :func:`approve_plan`
    recomputes findings, prerequisites, branches and repository identity
    from the verified intake and enforces them itself, whatever this says.

    ``approvable`` is whether this intake can approve the slice at all
    (``reason`` says why not); blocking findings are reported separately,
    because they refuse every slice.
    """

    out: dict[str, dict[str, Any]] = {}
    for run in interpretation.runs:
        gates, earlier = split_prerequisites(interpretation, run.id)
        branch, problem = slice_branch(run, repositories)
        out[run.id] = {
            "approvable": problem is None,
            "reason": problem,
            "primary_repository": run.primary_repository,
            "expected_branch": branch,
            "siblings": sorted({name for stage in run.stages for name in stage.repositories}),
            "gates": list(gates),
            "earlier_slices": list(earlier),
            "without_amendment": amendment,
        }
    return out


def findings_summary(findings: Sequence[Finding], verdict: str) -> dict[str, Any]:
    """How many findings of each severity, and the verdict, as display
    metadata for ``intake.json``. Approval recomputes the findings itself."""

    counts = {severity: sum(1 for f in findings if f.severity == severity) for severity in SEVERITIES}
    return {**counts, "verdict": verdict}


# -- stage ids and briefs -------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def stage_id_for(run_key: str, stage: IntakeStage) -> str:
    """``<run key>-stage-<label>-<title>``: the manifest convention the
    README asks callers to follow, so ids never collide across runs."""

    base = f"{run_key}-stage-{_slug(stage.label) or 'x'}"
    title = _slug(stage.title)
    stage_id = f"{base}-{title}"[:128].rstrip("-") if title else base
    return validate_stage_id(stage_id)


_SCOPE_PREAMBLE = (
    "This block was written by plan intake; it is not text from the source plan. It "
    "may only narrow what this stage executes, make a boundary explicit, or defer an "
    "action to a named gate. It adds and relaxes no requirement: where it appears to "
    "differ from the source plan text above, the source plan text governs."
)


def render_brief(
    source: SourcePlan,
    interpretation: Interpretation,
    run: RunSlice,
    stage: IntakeStage,
    *,
    stage_id: str,
    position: int,
    include_goal: bool = True,
) -> str:
    """The exact ``brief.md`` for one stage, assembled from source text.

    Context blocks first, in source order, each verbatim; then the stage's
    own ranges, verbatim; then the labelled intake note, if any. Line
    numbers are cited so a reviewer can find every excerpt in the plan.
    The brief's own sections are level-1 headings so that the quoted
    plan's section headings, which are never rewritten, nest beneath them
    (a quoted level-1 document title sits beside them).

    When ``include_goal`` (the default), a ``## Goal`` paragraph comes first,
    from intake's own structured understanding of the stage (its rationale,
    or its title when intake gave no rationale) -- never invented by
    re-reading the quoted source text. A client presenting this brief can
    show that paragraph verbatim instead of guessing at one from the
    plan-context boilerplate below. ``include_goal=False`` reproduces the
    ``1`` brief format (see ``BRIEF_FORMAT_VERSION``), byte for byte, so a
    legacy intake's recorded digests still match at approval.
    """

    blocks = {block.id: block for block in interpretation.context}
    attached = [blocks[cid] for cid in stage.context_ids if cid in blocks]
    attached.sort(key=lambda block: min((r.start for r in block.ranges), default=0))

    siblings = ", ".join(f"`{name}`" for name in stage.repositories) or "none"
    out = [
        f"# Stage brief: {stage_id}",
        "",
        f"Stage {stage.label} ({position} of {len(run.stages)} in run slice `{run.id}`) from "
        f"plan `{source.label}`, prepared by plan intake. Implement only this stage; the "
        "other stages are separate.",
        "",
        f"Primary repository: `{run.primary_repository}`. Sibling repositories: {siblings}.",
    ]
    if include_goal:
        goal_text = stage.rationale.strip() if stage.rationale and stage.rationale.strip() else stage.title.strip()
        out += ["", "## Goal", "", goal_text]
    if attached:
        out += ["", "# Plan context", ""]
        out.append(
            "Plan-wide text this stage is bound by, quoted verbatim from the source plan."
        )
        for block in attached:
            cited = ", ".join(str(r) for r in block.ranges)
            out += ["", f"<!-- source plan lines {cited} -->", "", source.excerpt(block.ranges)]
    cited = ", ".join(str(r) for r in stage.source_ranges)
    out += [
        "",
        "# Stage source",
        "",
        f"<!-- source plan lines {cited} -->",
        "",
        source.excerpt(stage.source_ranges),
    ]
    if stage.intake_scope:
        out += ["", "# Intake scoping", "", _SCOPE_PREAMBLE, "", stage.intake_scope.strip()]
    return "\n".join(out).rstrip() + "\n"


def amendment_diff(source: SourcePlan, amended: str) -> str:
    """A unified diff from the source plan to the proposed amendment."""

    return "".join(
        difflib.unified_diff(
            source.text.splitlines(keepends=True),
            amended.splitlines(keepends=True),
            fromfile=f"a/{source.label}",
            tofile=f"b/{source.label} (proposed by intake)",
        )
    )


# -- prepare ----------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeResult:
    directory: Path
    interpretation: Interpretation
    findings: tuple[Finding, ...]
    run_keys: Mapping[str, str]

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)


def intake_root(sparring_dir: Path) -> Path:
    return Path(sparring_dir) / INTAKE_DIRNAME


def require_intake_ignored(repo_root: Path, sparring_dir: Path) -> None:
    """Refuse before any provider turn if git could see intake artifacts.

    They are engine-generated runtime state written into the project's
    ``.sparring/``; if git can see them, the next ``freeze-candidate`` in
    this worktree refuses it as dirty -- part-way through a run.
    """

    probe = intake_root(sparring_dir) / "any-intake" / "intake.json"
    try:
        probe.resolve().relative_to(Path(repo_root).resolve())
    except ValueError:
        return
    try:
        ignored = is_ignored(repo_root, probe)
    except GitContextError as exc:
        raise IntakeError(str(exc)) from exc
    if not ignored:
        raise IntakeError(intake_not_ignored_message(repo_root))


def intake_not_ignored_message(repo_root: Path) -> str:
    return (
        f"plan intake artifacts under .sparring/{INTAKE_DIRNAME}/ are not ignored by git. "
        "They are engine-generated runtime state, and leaving them visible would make "
        "freeze-candidate refuse the worktree as dirty.\n"
        f"Fix: add a line '.sparring/{INTAKE_DIRNAME}/' to {repo_root}/.gitignore, next to "
        "'.sparring/stages/' and '.sparring/plans/'.\n"
        "Check it with: sparring check-config"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def prepare_plan(
    plan_path: Path,
    repo_root: Path,
    sparring_dir: Path,
    adapter: StructuredAgentAdapter,
    *,
    mode: str,
    primary_repository: str,
    context_repositories: Mapping[str, Path] | None = None,
    project_context: str | None = None,
    provider: Mapping[str, Any] | None = None,
    now: Callable[[], datetime] = _utc_now,
    mint_run_key: Callable[[str], str] = new_run_key,
) -> IntakeResult:
    """Run one intake turn and write a reviewable proposal. Executes nothing.

    Refuses (:class:`IntakeError`) when the mode is unknown, intake
    artifacts would be visible to git, the provider fails, any repository
    changed during the read-only turn, the source plan changed during it,
    or the answer is not a structurally valid interpretation. Problems with
    what the interpretation *says* are findings in the report instead, so a
    person sees all of them; any blocking one stops approval.
    """

    if mode not in MODES:
        raise IntakeError(f"unknown intake mode {mode!r}; expected one of {list(MODES)}")
    repo_root = Path(repo_root)
    context_repositories = dict(context_repositories or {})
    if primary_repository in context_repositories:
        raise IntakeError(
            f"{primary_repository!r} is this project's own repository; do not also pass it "
            "as a context repository"
        )
    require_intake_ignored(repo_root, sparring_dir)

    try:
        text = Path(plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"cannot read plan {plan_path}: {exc}") from exc
    label = plan_label(Path(plan_path), repo_root)
    source = SourcePlan(label=label, text=text)

    prompt = assemble_intake_prompt(
        plan_label=label,
        source_text=text,
        mode=mode,
        project_context=project_context,
        primary_repository=primary_repository,
        repo_root=repo_root.resolve(),
        context_repositories={name: Path(path).resolve() for name, path in context_repositories.items()},
    )

    named = [(primary_repository, repo_root), *sorted(context_repositories.items())]
    watched = [Path(path) for _, path in named]
    before = [_fingerprint(path) for path in watched]
    try:
        result = adapter.start_structured(prompt, INTERPRETATION_SCHEMA)
    except ProviderError as exc:
        raise IntakeError(f"the intake agent failed: {exc}") from exc
    after = [_fingerprint(path) for path in watched]
    for path, was, now_is in zip(watched, before, after):
        if was != now_is:
            raise IntakeError(
                f"repository {path} changed during the read-only intake turn "
                f"(before {was}, after {now_is}); refusing its answer"
            )
    try:
        reread = Path(plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"cannot re-read plan {plan_path}: {exc}") from exc
    if reread != text:
        raise IntakeError(f"the plan {label} changed during the intake turn; refusing its answer")
    # Every inspected repository, context-only ones included, as the intake
    # agent saw it. Approval compares against these, never against whatever
    # a later argument claims.
    snapshots = {
        name: repository_snapshot(path, fingerprint=fingerprint)
        for (name, path), fingerprint in zip(named, after)
    }

    interpretation = parse_interpretation(result.text)
    findings = all_findings(interpretation, source, mode=mode)

    run_keys = {run.id: mint_run_key(label) for run in interpretation.runs}
    briefs = _render_briefs(source, interpretation, run_keys)
    duplicate_ids = _duplicates([stage_id for stage_id, _, _ in briefs.values()])
    if duplicate_ids:
        raise IntakeError(f"derived stage ids collide: {duplicate_ids}; labels must slug uniquely")

    # Lineage: every already-finished intake of this same plan, oldest
    # first, and where each of this intake's stages, by its own
    # plan_stage_label, was first seen among them (see stage_lineage). Both
    # are purely structural here; carry-forward (what may execute) is built
    # on top of this, not decided by it.
    priors = previous_finished_intakes(sparring_dir, label)
    contracts = stage_contracts(source, interpretation, run_keys)
    lineage = stage_lineage(contracts, priors)

    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    intake_id = f"{plan_key(label)}-{stamp}-{mode}-{uuid.uuid4().hex[:4]}"
    directory = intake_root(sparring_dir) / intake_id
    (directory / "briefs").mkdir(parents=True, exist_ok=False)

    interpretation_text = json.dumps(json.loads(result.text), indent=2, ensure_ascii=False) + "\n"
    _write(directory / "source.md", text)
    _write(directory / "prompt.md", prompt)
    _write(directory / "interpretation.json", interpretation_text)

    brief_digests: dict[str, str] = {}
    for key, (stage_id, relative, content) in briefs.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, content)
        brief_digests[stage_id] = sha256_text(content)

    amendment = None
    if interpretation.amended_plan is not None and interpretation.amended_plan != text:
        amendment = amendment_diff(source, interpretation.amended_plan)
        _write(directory / "amendment.diff", amendment)

    record = {
        "version": INTAKE_VERSION,
        "intake_id": intake_id,
        "brief_format_version": BRIEF_FORMAT_VERSION,
        "completion_marker": COMPLETION_MARKER_FILENAME,
        "mode": mode,
        "created_at": now().isoformat(),
        "plan_label": label,
        "source_path": str(Path(plan_path).resolve()),
        "source_digest": source.digest,
        "interpretation_digest": sha256_text(interpretation_text),
        "primary_repository": primary_repository,
        "context_repositories": {name: str(Path(p).resolve()) for name, p in sorted(context_repositories.items())},
        "run_keys": run_keys,
        "briefs": brief_digests,
        "previous_intakes": [prior.intake_id for prior in priors],
        "stages": contracts,
        "stage_lineage": lineage,
        "provider": dict(provider or {}),
        "provider_session_id": result.session_id,
        "amendment_proposed": amendment is not None,
        # Display metadata only (see approval_requirements): approval never
        # reads these two, it recomputes and enforces everything.
        "findings": findings_summary(findings, interpretation.verdict),
        "approval_requirements": approval_requirements(
            interpretation, snapshots, amendment=amendment is not None
        ),
    }
    report = render_report(
        source,
        interpretation,
        findings,
        run_keys=run_keys,
        briefs=briefs,
        mode=mode,
        intake_dir=directory,
        amendment=amendment is not None,
        provider=record["provider"],
        repositories=snapshots,
    )
    record["repositories"] = snapshots
    record["report_intake_dir"] = str(directory)
    record["report_digest"] = sha256_text(report)
    _write(directory / "intake.json", json.dumps(record, indent=2) + "\n")
    _write(directory / "report.md", report)
    # The very last write of a successful prepare. Its presence is what
    # distinguishes a finished intake from one interrupted after intake.json
    # was written; approval refuses one without it (see completion_marker).
    marker = {
        "intake_id": intake_id,
        "prepared_at": now().isoformat(),
        "verdict": interpretation.verdict,
    }
    write_atomic(directory / COMPLETION_MARKER_FILENAME, json.dumps(marker, indent=2) + "\n")
    return IntakeResult(directory=directory, interpretation=interpretation, findings=findings, run_keys=run_keys)


def completion_marker_problem(intake_dir: Path, record: Mapping[str, Any]) -> str | None:
    """``None`` if this intake is usable, else why it is not.

    An intake whose ``intake.json`` names a ``completion_marker`` is usable
    only once that file exists in ``intake_dir``: its absence means
    ``prepare_plan`` was interrupted after writing ``intake.json`` but before
    finishing, so ``report.md`` may not be what a finished intake would have
    produced. A legacy intake with no ``completion_marker`` key predates this
    check and is unaffected by it.
    """

    marker_name = record.get("completion_marker")
    if not marker_name:
        return None
    if not isinstance(marker_name, str):
        raise IntakeError(f"{intake_dir / 'intake.json'} names a non-string completion_marker")
    if (intake_dir / marker_name).is_file():
        return None
    return (
        f"{intake_dir} has no {marker_name} -- prepare_plan did not finish (it was likely "
        "interrupted). This intake cannot be approved; run prepare-plan again"
    )


@dataclass(frozen=True)
class _PriorIntake:
    intake_id: str
    directory: Path
    record: Mapping[str, Any]


def previous_finished_intakes(sparring_dir: Path, label: str) -> list[_PriorIntake]:
    """Every already-finished intake of the same plan (by :func:`plan_key`)
    recorded under ``sparring_dir``, oldest first.

    An intake with no completion marker, or one whose marker is missing (see
    :func:`completion_marker_problem`), was never reviewed and contributes no
    lineage. A directory that is not a readable intake at all -- foreign or
    corrupt state under ``intake/`` -- is skipped rather than failing the
    scan: this is best-effort discovery, not a claim about that directory.
    """

    root = intake_root(sparring_dir)
    if not root.is_dir():
        return []
    key = plan_key(label)
    found: list[_PriorIntake] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == REGISTRY_DIRNAME:
            continue
        record_path = entry / "intake.json"
        if not record_path.is_file():
            continue
        try:
            record = _read_json(record_path)
        except IntakeError:
            continue
        if plan_key(str(record.get("plan_label", ""))) != key:
            continue
        if completion_marker_problem(entry, record) is not None:
            continue
        found.append(_PriorIntake(intake_id=str(record.get("intake_id") or entry.name), directory=entry, record=record))
    found.sort(key=lambda prior: str(prior.record.get("created_at", "")))
    return found


def _normalize_contract_text(text: str) -> str:
    """Whitespace-only normalization for :func:`stage_contracts`: trailing
    space on a line and leading/trailing blank lines never change a stage's
    contract, but no other difference is smoothed over."""

    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def stage_contracts(
    source: SourcePlan, interpretation: Interpretation, run_keys: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """Each stage's stable identity: ``{stage_id: {plan_stage_label, run_id,
    contract_digest}}``.

    ``contract_digest`` hashes only the plan's own text for that stage
    (``stage.source_ranges``, whitespace-normalized) -- never attached
    context blocks, never ``depends_on``, never any other stage's text. A
    stage's dependencies are not part of its contract: inserting a stage
    elsewhere, or changing what a stage depends on, does not move its
    digest, by construction, because this never reads ``depends_on`` at
    all. Only a change to the stage's own cited source text does.
    """

    contracts: dict[str, dict[str, str]] = {}
    for run in interpretation.runs:
        for stage in run.stages:
            stage_id = stage_id_for(run_keys[run.id], stage)
            text = _normalize_contract_text(source.excerpt(stage.source_ranges))
            contracts[stage_id] = {
                "plan_stage_label": stage.label,
                "run_id": run.id,
                "contract_digest": sha256_text(text),
            }
    return contracts


def stage_lineage(
    contracts: Mapping[str, Mapping[str, str]], priors: Sequence[_PriorIntake]
) -> dict[str, dict[str, Any]]:
    """For each of this intake's stages whose ``plan_stage_label`` also
    named a stage in an earlier finished intake of the same plan, where that
    label was first seen: ``{stage_id: {label, first_seen: {intake_id,
    run_id, stage_id, contract_digest}}}``.

    Purely structural traceability by label, computed from ``priors`` in
    the order given (oldest first) so "first seen" is truly the earliest
    occurrence. It says nothing about whether that earlier stage was ever
    approved or completed, or whether its contract still matches -- that is
    carry-forward's job, built on top of this.
    """

    by_label: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for prior in priors:
        for prior_stage_id, prior_stage in (prior.record.get("stages") or {}).items():
            label = prior_stage.get("plan_stage_label")
            if not isinstance(label, str) or label in by_label:
                continue
            by_label[label] = (
                prior.intake_id,
                {
                    "run_id": prior_stage.get("run_id"),
                    "stage_id": prior_stage_id,
                    "contract_digest": prior_stage.get("contract_digest"),
                },
            )
    lineage: dict[str, dict[str, Any]] = {}
    for stage_id, contract in contracts.items():
        label = contract["plan_stage_label"]
        seen = by_label.get(label)
        if seen is None:
            continue
        intake_id, prior_stage = seen
        lineage[stage_id] = {
            "label": label,
            "first_seen": {"intake_id": intake_id, **prior_stage},
        }
    return lineage


def _fingerprint(path: Path) -> tuple[str, str, tuple[str, ...]]:
    try:
        return repo_fingerprint(Path(path))
    except GitContextError as exc:
        raise IntakeError(f"cannot fingerprint repository {path}: {exc}") from exc


def repository_snapshot(
    path: Path, *, fingerprint: tuple[str, str, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    """One repository's identity and state: canonical worktree path, common
    git directory, branch, HEAD and dirty paths.

    Built from the shared :func:`~agent_sparring.sparring_agent.
    repo_fingerprint`, so it attests what that helper attests and no more:
    branch, HEAD and which paths are dirty -- not file contents, not ignored
    material, and not a modify-then-restore in between two observations.
    """

    branch, head, dirty = fingerprint if fingerprint is not None else _fingerprint(path)
    try:
        top, common = repository_identity(Path(path))
    except GitContextError as exc:
        raise IntakeError(f"cannot identify repository {path}: {exc}") from exc
    return {
        "path": top,
        "git_common_dir": common,
        "branch": branch,
        "head": head,
        "dirty_paths": list(dirty),
    }


def _render_briefs(
    source: SourcePlan,
    interpretation: Interpretation,
    run_keys: Mapping[str, str],
    *,
    include_goal: bool = True,
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """``{(run id, label): (stage id, relative path, content)}``."""

    briefs: dict[tuple[str, str], tuple[str, str, str]] = {}
    for run in interpretation.runs:
        for index, stage in enumerate(run.stages, start=1):
            stage_id = stage_id_for(run_keys[run.id], stage)
            content = render_brief(
                source,
                interpretation,
                run,
                stage,
                stage_id=stage_id,
                position=index,
                include_goal=include_goal,
            )
            relative = f"briefs/{_slug(run.id) or 'run'}/{index:02d}-{_slug(stage.label) or 'stage'}.md"
            briefs[(run.id, stage.label)] = (stage_id, relative, content)
    return briefs


def _duplicates(values: Sequence[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# -- the report -------------------------------------------------------------------


def _ranges_text(ranges: Sequence[LineRange]) -> str:
    return ", ".join(str(r) for r in ranges) or "—"


def render_report(
    source: SourcePlan,
    interpretation: Interpretation,
    findings: Sequence[Finding],
    *,
    run_keys: Mapping[str, str],
    briefs: Mapping[tuple[str, str], tuple[str, str, str]],
    mode: str,
    intake_dir: Path,
    amendment: bool,
    provider: Mapping[str, Any],
    repositories: Mapping[str, Any],
) -> str:
    """``report.md``: everything a person needs to decide on approval."""

    blocking = [f for f in findings if f.blocking]
    out = [
        f"# Plan intake: `{source.label}`",
        "",
        f"- Mode: **{mode}**",
        f"- Verdict: **{interpretation.verdict}**",
        f"- Source digest: `{source.digest}` ({len(source.lines)} lines; snapshot in `source.md`)",
    ]
    if provider:
        out.append("- Intake agent: " + ", ".join(f"{k}={v}" for k, v in provider.items() if v))
    out.append(
        f"- Approval: {'**blocked** by ' + str(len(blocking)) + ' blocking finding(s)' if blocking else 'no blocking findings'}"
    )
    if interpretation.summary:
        out += ["", interpretation.summary]

    out += ["", "## Findings", ""]
    if not findings:
        out.append("None.")
    for f in findings:
        where = []
        if f.stages:
            where.append("stages " + ", ".join(s for s in f.stages if s))
        if f.ranges:
            where.append("lines " + _ranges_text(f.ranges))
        suffix = f" ({'; '.join(where)})" if where else ""
        out.append(f"- **{f.severity}** `{f.code}` [{f.origin}]{suffix}: {f.message}")

    out += ["", "## Run slices", ""]
    out.append(
        "Each run slice becomes one manifest, run in its primary repository on one "
        "expected branch. Slices are approved and started separately, in order."
    )
    for run in interpretation.runs:
        gates, earlier = split_prerequisites(interpretation, run.id)
        branch, problem = slice_branch(run, repositories)
        out += [
            "",
            f"### Run slice `{run.id}` — primary `{run.primary_repository}`",
            "",
            f"- Expected branch: `{branch}` (checked out when intake inspected it)"
            if branch
            else f"- Expected branch: **cannot be approved**: {problem}",
            f"- Run key: `{run_keys.get(run.id, '?')}`",
            f"- Gates a person confirms at approval: {', '.join('`' + g + '`' for g in gates) or 'none'}",
            "- Earlier run slices that must be approved and complete first: "
            + (', '.join('`' + e + '`' for e in earlier) or 'none'),
        ]
        if run.rationale:
            out.append(f"- Why this slice: {run.rationale}")
        out += [
            "",
            "| # | Label | Title | Mode | Siblings | Depends on | Source lines | Context | Brief |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for index, stage in enumerate(run.stages, start=1):
            stage_id, relative, _ = briefs[(run.id, stage.label)]
            out.append(
                f"| {index} | {stage.label} | {stage.title} | {stage.mode} | "
                f"{', '.join(stage.repositories) or '—'} | {', '.join(stage.depends_on) or '—'} | "
                f"{_ranges_text(stage.source_ranges)} | {', '.join(stage.context_ids) or '—'} | "
                f"`{relative}` |"
            )
        for stage in run.stages:
            notes = []
            if stage.intake_scope:
                notes.append(f"  - Intake scoping: {stage.intake_scope}")
            if stage.rationale:
                notes.append(f"  - Rationale: {stage.rationale}")
            if notes:
                out += ["", f"- Stage {stage.label}:", *notes]

    out += ["", "## Gates and deferred actions", ""]
    if not interpretation.gates:
        out.append("None.")
    for gate in interpretation.gates:
        blocks = ", ".join(gate.blocks_stages) or "nothing executable"
        out.append(
            f"- `{gate.id}` ({gate.kind}) — {gate.title}. After: {gate.after_stage or '—'}; "
            f"blocks: {blocks}; lines {_ranges_text(gate.ranges)}. {gate.reason}"
        )

    out += ["", "## Context blocks", ""]
    if not interpretation.context:
        out.append("None.")
    users: dict[str, list[str]] = {}
    for _, _, _, stage in interpretation.stages():
        for cid in stage.context_ids:
            users.setdefault(cid, []).append(stage.label)
    for block in interpretation.context:
        out.append(
            f"- `{block.id}` — {block.title} (lines {_ranges_text(block.ranges)}); used by "
            f"{', '.join(users.get(block.id, [])) or 'no stage'}. {block.reason}"
        )

    out += ["", "## Excluded text", ""]
    if not interpretation.excluded:
        out.append("None.")
    for exclusion in interpretation.excluded:
        out.append(f"- lines {_ranges_text(exclusion.ranges)}: {exclusion.reason}")

    uncovered = [f for f in findings if f.code == "uncovered_source"]
    out += ["", "## Source coverage", ""]
    out.append(
        f"{len(source.substantive_lines())} substantive lines; "
        + ("all accounted for." if not uncovered else f"{len(uncovered)} unaccounted range(s) (blocking, see findings).")
    )

    out += ["", "## Repositories inspected", ""]
    out.append(
        "Approval refuses if any of these has moved to another branch or commit, except to a "
        "commit an earlier run slice of this intake was accepted at."
    )
    for name, snapshot in sorted(repositories.items()):
        dirty = snapshot.get("dirty_paths") or []
        out.append(
            f"- `{name}`: `{snapshot.get('path')}` on `{snapshot.get('branch')}` at "
            f"`{str(snapshot.get('head'))[:12]}`" + (f"; {len(dirty)} dirty path(s)" if dirty else "")
        )

    out += ["", "## Proposed amendment", ""]
    out.append(
        "See `amendment.diff`. It is a proposal for the source plan and is not applied; the "
        "manifest executes the source text as sliced above. Approval requires "
        "--without-amendment, or apply it to the plan and run intake again."
        if amendment
        else "None."
    )

    out += ["", "## Next step", ""]
    if blocking:
        out.append("Resolve the blocking findings (edit the plan, or run intake again), then re-run prepare-plan.")
    else:
        requirements = approval_requirements(interpretation, repositories, amendment=amendment)
        for run in interpretation.runs:
            needs = requirements[run.id]
            if not needs["approvable"]:
                out.append(f"- Run slice `{run.id}` cannot be approved from this intake (see above).")
                continue
            parts = [f"sparring approve-plan {intake_dir} --run {run.id}"]
            for name in needs["siblings"]:
                parts.append(f"--repository {name}=<path> --repository-branch {name}=<branch>")
            for gate in needs["gates"]:
                parts.append(f"--confirm-prerequisite {gate}")
            if needs["without_amendment"]:
                parts.append("--without-amendment")
            earlier = needs["earlier_slices"]
            after = f", once {', '.join('`' + e + '`' for e in earlier)} completed" if earlier else ""
            confirm = " (confirm each gate only once it is actually satisfied)" if needs["gates"] else ""
            out.append(f"- From the `{run.primary_repository}` project{after}{confirm}: `{' '.join(parts)}`")
    return "\n".join(out).rstrip() + "\n"


# -- approve ----------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    manifest_path: Path
    approval_path: Path
    run_key: str
    expected_branch: str
    manifest_digest: str
    created: bool


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise IntakeError(f"{path} is not a JSON object")
    return payload


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"cannot read {path}: {exc}") from exc


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IntakeError(f"cannot read {path}: {exc}") from exc


def _recorded_repositories(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    repositories = record.get("repositories")
    keys = ("path", "git_common_dir", "branch", "head")
    if not isinstance(repositories, dict) or not all(
        isinstance(snapshot, dict) and all(isinstance(snapshot.get(k), str) and snapshot.get(k) for k in keys)
        for snapshot in repositories.values()
    ):
        raise IntakeError("intake.json records no valid repository snapshots; run prepare-plan again")
    return repositories


def _same_repository(path: Path, snapshot: Mapping[str, Any]) -> bool:
    try:
        return repository_identity(path) == (snapshot["path"], snapshot["git_common_dir"])
    except GitContextError:
        return False


@dataclass(frozen=True)
class _CompletedSlice:
    """What proves an earlier run slice: its approval and its finished run."""

    evidence: dict[str, Any]
    repository: str
    #: The branch that slice ran on, which its accepted work is on.
    branch: str
    final_candidate: str


def _completed_slice(intake_dir: Path, run_id: str) -> _CompletedSlice:
    """Prove that earlier run slice ``run_id`` of this intake was approved and
    its sealed run completed with an accepted candidate, or refuse."""

    manifest_path = intake_dir / RUNS_DIRNAME / (_slug(run_id) or "run") / MANIFEST_FILENAME
    try:
        prior = load_intake_manifest(manifest_path)
    except IntakeApprovalError as exc:
        raise IntakeError(
            f"earlier run slice {run_id!r} has no valid approval ({exc}). It must be approved "
            "and run to completion before a slice that depends on it is approved"
        ) from exc
    approval = prior.approval
    sparring_dir = Path(str(approval.field("sparring_dir")))
    state_path = run_state_path(sparring_dir, approval.run_key)
    if not state_path.is_file():
        raise IntakeError(
            f"earlier run slice {run_id!r} is approved but has not been run (no run "
            f"{approval.run_key} at {state_path}); run and complete it first"
        )
    try:
        state = PlanRunState.load(state_path)
    except PlanError as exc:
        raise IntakeError(f"cannot read the run of earlier slice {run_id!r}: {exc}") from exc
    if (state.run, state.source, state.plan_digest) != (approval.run_key, INTAKE_SOURCE_KIND, prior.digest()):
        raise IntakeError(
            f"the run recorded at {state_path} is not the sealed run of earlier slice {run_id!r}"
        )
    if state.status is not PlanRunStatus.COMPLETE:
        raise IntakeError(
            f"earlier run slice {run_id!r} has not completed (its run {approval.run_key} is "
            f"{state.status.value} at stage {state.current_stage!r}); approval alone does not "
            "mean its work happened"
        )
    last = prior.stages()[-1]
    try:
        stage_state = Stage.resolve(sparring_dir, last.stage_id).read_state()
    except StageError as exc:
        raise IntakeError(f"cannot read the final stage of earlier slice {run_id!r}: {exc}") from exc
    if stage_state.status is not StageStatus.ACCEPTED or not stage_state.candidate_sha:
        raise IntakeError(f"the final stage of earlier slice {run_id!r} has no accepted candidate")
    return _CompletedSlice(
        evidence={
            "run_id": run_id,
            "run_key": approval.run_key,
            "approval_sha256": approval.sha256,
            "plan_digest": state.plan_digest,
            "final_stage": last.stage_id,
            "final_candidate_sha": stage_state.candidate_sha,
        },
        repository=str(approval.field("primary_repository")),
        branch=approval.expected_branch,
        final_candidate=stage_state.candidate_sha,
    )


def approve_plan(
    intake_dir: Path,
    *,
    run_id: str,
    repo_root: Path,
    sparring_dir: Path,
    primary_repository: str,
    expected_branch: str | None = None,
    repositories: Mapping[str, str] | None = None,
    repository_branches: Mapping[str, str] | None = None,
    confirmed_prerequisites: Sequence[str] = (),
    without_amendment: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> Approval:
    """A person's explicit approval of one reviewed run slice, sealed.

    Refuses (:class:`IntakeError`, writing nothing) unless the intake is
    exactly what was reviewed -- source plan, snapshot, interpretation and
    ``report.md`` -- has no blocking finding, and the slice is approved from
    its inspected primary repository -- on the branch it has checked out now,
    which must not be protected when the slice runs an implementation agent
    -- with every inspected repository at the commit intake saw (or at one an
    earlier slice it depends on, directly or transitively, was accepted
    at), every gate it waits for
    confirmed by id, and every earlier slice it depends on proven by that
    slice's own approval and completed run.

    Writes ``runs/<run>/manifest.json`` -- an intake envelope around the
    ordinary manifest -- and then ``approval.json``, which binds the exact
    manifest bytes, the inputs above, the repository state the run must
    start from and the prerequisite evidence (see
    :mod:`agent_sparring.intake_approval`). An approval is created once and
    never rewritten: repeating an identical one returns it, and a
    conflicting one refuses.
    """

    intake_dir = Path(intake_dir)
    repo_root = Path(repo_root)
    record_raw = _read_bytes(intake_dir / "intake.json")
    record = _read_json(intake_dir / "intake.json")
    if record.get("version") == 1:
        raise IntakeError(
            f"{intake_dir} is an unsealed intake prepared before approvals bound execution; it "
            "cannot be approved. Run prepare-plan again and review the new report"
        )
    if record.get("version") != INTAKE_VERSION:
        raise IntakeError(f"unsupported intake record version {record.get('version')!r}")
    # A legacy intake recorded no brief_format_version at all -- it was
    # prepared before the ## Goal paragraph existed, so its on-disk briefs
    # (and their recorded digests) are in the pre-Goal form. Re-render every
    # brief in whichever form this intake actually recorded, not whatever
    # render_brief defaults to today, or a legacy intake's briefs would never
    # match again and could never be approved.
    brief_format = record.get("brief_format_version")
    if brief_format is None:
        include_goal = False
    elif brief_format == BRIEF_FORMAT_VERSION:
        include_goal = True
    else:
        raise IntakeError(f"intake.json records an unsupported brief format {brief_format!r}; run prepare-plan again")
    marker_problem = completion_marker_problem(intake_dir, record)
    if marker_problem:
        raise IntakeError(marker_problem)
    mode = str(record.get("mode"))
    if mode not in MODES:
        raise IntakeError(f"intake.json records an unknown mode {mode!r}; run prepare-plan again")

    snapshot = _read_text(intake_dir / "source.md")
    if sha256_text(snapshot) != record.get("source_digest"):
        raise IntakeError("source.md no longer matches the digest intake recorded; run prepare-plan again")
    source_path = Path(str(record.get("source_path")))
    current = _read_text(source_path)
    if sha256_text(current) != record.get("source_digest"):
        raise IntakeError(
            f"the source plan {source_path} changed since intake read it; the reviewed "
            "interpretation no longer describes it. Run prepare-plan again"
        )
    interpretation_text = _read_text(intake_dir / "interpretation.json")
    if sha256_text(interpretation_text) != record.get("interpretation_digest"):
        raise IntakeError("interpretation.json was modified after intake; run prepare-plan again")

    source = SourcePlan(label=str(record["plan_label"]), text=snapshot)
    interpretation = parse_interpretation(interpretation_text)
    findings = all_findings(interpretation, source, mode=mode)
    # Derived from the interpretation itself, never taken from intake.json.
    amendment_proposed = (
        interpretation.amended_plan is not None and interpretation.amended_plan != source.text
    )
    blocking = [f for f in findings if f.blocking]
    if blocking:
        listed = "\n".join(f"- {f.code}: {f.message}" for f in blocking)
        raise IntakeError(f"approval refused: {len(blocking)} blocking finding(s)\n{listed}")
    if interpretation.verdict == VERDICT_CANNOT_INTERPRET:
        raise IntakeError("approval refused: the intake agent could not interpret the plan faithfully")
    if amendment_proposed and not without_amendment:
        raise IntakeError(
            "intake proposed an amendment to the source plan (amendment.diff). Either apply it "
            "to the plan and run prepare-plan again, or pass --without-amendment to approve the "
            "manifest built from the unamended source text"
        )

    runs = {run.id: run for run in interpretation.runs}
    run = runs.get(run_id)
    if run is None:
        raise IntakeError(f"no run slice {run_id!r}; this intake has {sorted(runs)}")
    if run.primary_repository != primary_repository:
        raise IntakeError(
            f"run slice {run_id!r} runs in {run.primary_repository!r}, but this project is "
            f"{primary_repository!r}. A sibling declaration does not move a stage's work; "
            f"approve and run this slice from the {run.primary_repository!r} project"
        )

    run_keys = record.get("run_keys") or {}
    if not isinstance(run_keys, Mapping) or any(
        not isinstance(run_keys.get(slice_id), str) or not run_keys.get(slice_id) for slice_id in runs
    ):
        raise IntakeError("intake.json does not record a run key for every run slice; run prepare-plan again")
    inspected = _recorded_repositories(record)
    briefs = _render_briefs(source, interpretation, run_keys, include_goal=include_goal)

    # The person reviewed report.md. Approving is only meaningful if it is
    # the report this intake renders, so a stale or edited one refuses.
    report_dir = record.get("report_intake_dir")
    report_text = _read_text(intake_dir / "report.md")
    rendered = render_report(
        source,
        interpretation,
        findings,
        run_keys=run_keys,
        briefs=briefs,
        mode=mode,
        intake_dir=Path(str(report_dir)),
        amendment=amendment_proposed,
        provider=record.get("provider") or {},
        repositories=inspected,
    )
    if not isinstance(report_dir, str) or rendered != report_text or sha256_text(report_text) != record.get("report_digest"):
        raise IntakeError(
            f"{intake_dir / 'report.md'} is not the report this intake renders; what you reviewed "
            "is not what approval would bind. Run prepare-plan again"
        )

    gates, earlier = split_prerequisites(interpretation, run_id)
    confirmed = {value.strip() for value in confirmed_prerequisites if value.strip()}
    named_slices = sorted(confirmed & set(runs))
    if named_slices:
        raise IntakeError(
            f"--confirm-prerequisite {named_slices}: an earlier run slice is not confirmed by "
            "name. It is proven by its own approval and completed run, which approval checks"
        )
    unknown = sorted(confirmed - {gate.id for gate in interpretation.gates})
    if unknown:
        raise IntakeError(f"--confirm-prerequisite names no gate in this intake: {unknown}")
    irrelevant = sorted(confirmed - set(gates))
    if irrelevant:
        raise IntakeError(f"run slice {run_id!r} does not wait for {irrelevant}; confirm only its gates")
    missing = sorted(set(gates) - confirmed)
    if missing:
        raise IntakeError(
            f"run slice {run_id!r} waits for gates {missing}. The runtime cannot enforce these "
            "inside a run; confirm each with --confirm-prerequisite once it is actually satisfied"
        )

    _, problem = slice_branch(run, inspected)
    if problem:
        raise IntakeError(problem)
    # The slice runs on the branch checked out now: the branch is chosen at
    # approval. What intake sealed -- repository and commit -- is checked
    # below exactly as before.
    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise IntakeError(f"cannot read the branch of {repo_root}: {exc}") from exc
    if run.expected_branch and branch != run.expected_branch:
        raise IntakeError(
            f"the plan runs slice {run_id!r} on {run.expected_branch!r}, but {repo_root} is on "
            f"{branch!r}; check out {run.expected_branch!r} and approve again"
        )
    requested = (expected_branch or "").strip()
    if requested and requested != branch:
        raise IntakeError(
            f"{repo_root} is on {branch!r}, the branch a slice is approved on; --expected-branch "
            f"{requested!r} cannot replace it. Check out {requested!r} and approve again"
        )
    if slice_runs_agent(run) and is_protected_branch(branch):
        raise IntakeError(feature_branch_needed(run, repo_root, branch))

    repositories = dict(repositories or {})
    repository_branches = dict(repository_branches or {})
    needed = sorted({name for stage in run.stages for name in stage.repositories})
    extra = sorted((set(repositories) | set(repository_branches)) - set(needed))
    if extra:
        raise IntakeError(f"run slice {run_id!r} declares no sibling repository named {extra}")
    missing_repos = [name for name in needed if name not in repositories or name not in repository_branches]
    if missing_repos:
        raise IntakeError(
            f"run slice {run_id!r} declares sibling repositories {missing_repos}; give each a "
            "path and branch with --repository NAME=PATH and --repository-branch NAME=BRANCH"
        )
    for name in needed:
        if name not in inspected:
            raise IntakeError(
                f"sibling repository {name!r} was not inspected by this intake; run prepare-plan "
                f"with --context-repository {name}=<path>"
            )
        if not _same_repository(repo_root / repositories[name], inspected[name]):
            raise IntakeError(
                f"--repository {name}={repositories[name]} is not the repository intake inspected "
                f"as {name!r} ({inspected[name]['path']})"
            )

    run_key = run_keys[run_id]
    stages = []
    recorded_briefs = record.get("briefs") or {}
    for stage in run.stages:
        stage_id, _, content = briefs[(run.id, stage.label)]
        if recorded_briefs.get(stage_id) != sha256_text(content):
            raise IntakeError(
                f"the brief for stage {stage.label} no longer renders to what intake recorded; "
                "run prepare-plan again"
            )
        entry: dict[str, Any] = {
            "stage_id": stage_id,
            "label": f"Stage {stage.label}",
            "title": stage.title,
            "brief": content,
        }
        if stage.mode != StageMode.IMPLEMENTATION.value:
            entry["mode"] = stage.mode
        if stage.repositories:
            entry["repositories"] = [
                {
                    "name": name,
                    "path": repositories[name],
                    "branch": repository_branches[name],
                    "candidate_sha": None,
                }
                for name in stage.repositories
            ]
        stages.append(entry)

    manifest_payload = {
        "version": MANIFEST_VERSION,
        "plan_label": source.label,
        "source_digest": f"sha256:{source.digest}",
        "stages": stages,
    }
    try:
        manifest = parse_manifest(json.dumps(manifest_payload))
    except (ManifestError, StageError) as exc:
        raise IntakeError(f"the approved slice does not form a valid manifest: {exc}") from exc
    digest = manifest_digest(manifest)
    intake_id = str(record.get("intake_id"))
    manifest_text = envelope_text(intake_id=intake_id, run_id=run_id, run_key=run_key, manifest=manifest_payload)

    run_dir = intake_dir / RUNS_DIRNAME / (_slug(run_id) or "run")
    manifest_path = run_dir / MANIFEST_FILENAME
    approval_path = run_dir / APPROVAL_FILENAME
    decision = {
        "expected_branch": branch,
        "confirmed_gates": sorted(confirmed),
        "without_amendment": bool(without_amendment and amendment_proposed),
    }

    def result(created: bool) -> Approval:
        return Approval(
            manifest_path=manifest_path,
            approval_path=approval_path,
            run_key=run_key,
            expected_branch=branch,
            manifest_digest=digest,
            created=created,
        )

    def existing() -> Approval:
        try:
            previous = read_approval(approval_path)
        except IntakeApprovalError as exc:
            raise IntakeError(f"run slice {run_id!r} has an approval that cannot be used: {exc}") from exc
        recorded = {key: previous.payload.get(key) for key in decision}
        # A recorded branch move is part of the approval's decision.
        recorded["expected_branch"] = previous.expected_branch
        if recorded != decision:
            raise IntakeError(
                f"run slice {run_id!r} was already approved with {recorded}; an approval is not "
                f"rewritten, and this one would record {decision}"
            )
        on_disk = manifest_path.read_bytes() if manifest_path.is_file() else b""
        if sha256_bytes(on_disk) != previous.field("manifest_sha256"):
            raise IntakeError(
                f"{manifest_path} changed after run slice {run_id!r} was approved; it will not "
                "run. Run prepare-plan and approve-plan again"
            )
        if on_disk != manifest_text.encode("utf-8"):
            raise IntakeError(
                f"run slice {run_id!r} was already approved with a different manifest "
                f"({manifest_path}); an approval is not rewritten. Run prepare-plan again"
            )
        return result(created=False)

    if approval_path.exists():
        return existing()

    # A new decision: the repositories must still be where intake saw them.
    require_intake_ignored(repo_root, sparring_dir)
    if not _same_repository(repo_root, inspected[primary_repository]):
        raise IntakeError(
            f"this project ({repo_root}) is not the repository intake inspected as "
            f"{primary_repository!r} ({inspected[primary_repository]['path']}); approve from that one"
        )
    completed = [_completed_slice(intake_dir, prior) for prior in earlier]
    # A slice this one depends on only through another slice (A1 -> B1 ->
    # A2) moved its repository just as legitimately. It is not required
    # here -- the direct prerequisite's own approval required it -- so one
    # that is not proven complete and accepted simply does not count.
    indirect = []
    for prior in slice_ancestors(interpretation, run_id):
        if prior in earlier:
            continue
        try:
            indirect.append(_completed_slice(intake_dir, prior))
        except IntakeError:
            continue
    # Nor is a slice that runs *beside* this one -- both after a common
    # prerequisite, as Stage 5 and Stage 3P after Stage 2. Completed and
    # accepted, it moved its repository just as legitimately, and approving
    # the other must not read that as drift. It is proven by _completed_slice
    # exactly as an ancestor is, and its final candidate is the only commit it
    # explains; it is recorded apart, because it is not a prerequisite.
    related = {run_id, *earlier, *slice_ancestors(interpretation, run_id)}
    parallel = []
    for run in interpretation.runs:
        if run.id in related:
            continue
        try:
            parallel.append(_completed_slice(intake_dir, run.id))
        except IntakeError:
            continue
    # Where an earlier slice's accepted work left a repository: its branch
    # and final candidate. Only that, never an arbitrary later commit.
    advanced: dict[str, set[tuple[str, str]]] = {}
    for prior in (*completed, *indirect, *parallel):
        advanced.setdefault(prior.repository, set()).add((prior.branch, prior.final_candidate))
    now_seen: dict[str, dict[str, Any]] = {}
    drift = []
    for name, seen in sorted(inspected.items()):
        present = repository_snapshot(Path(seen["path"]))
        now_seen[name] = present
        accepted = advanced.get(name, set())
        heads = {seen["head"]} | {head for _, head in accepted}
        if (present["path"], present["git_common_dir"]) != (seen["path"], seen["git_common_dir"]):
            drift.append(f"{name}: {seen['path']} is now a different repository")
        elif name != primary_repository and (present["branch"], present["head"]) not in (
            {(seen["branch"], head) for head in heads} | accepted
        ) and present["branch"] != seen["branch"]:
            drift.append(f"{name}: branch {seen['branch']!r} is now {present['branch']!r}")
        elif present["head"] not in heads:
            drift.append(
                f"{name}: {present['branch']} moved from {seen['head'][:12]} to {present['head'][:12]}"
                + (" (not a commit an earlier slice was accepted at)" if name in advanced else "")
            )
    if drift:
        raise IntakeError(
            "repositories moved since intake inspected them, so the reviewed interpretation may "
            "no longer describe them:\n- " + "\n- ".join(drift) + "\nRun prepare-plan again"
        )

    starting = now_seen[primary_repository]
    approval = {
        "version": APPROVAL_VERSION,
        "decision": "approved",
        "approved_at": now().isoformat(),
        "intake_id": intake_id,
        "intake_dir": str(intake_dir.resolve()),
        "run_id": run_id,
        "run_key": run_key,
        "primary_repository": primary_repository,
        "sparring_dir": str(Path(sparring_dir).resolve()),
        "source": {"path": str(source_path), "label": source.label, "digest": source.digest},
        "intake_record_sha256": sha256_bytes(record_raw),
        "interpretation_digest": str(record.get("interpretation_digest")),
        "report_digest": str(record.get("report_digest")),
        "manifest_sha256": sha256_bytes(manifest_text.encode("utf-8")),
        "manifest_digest": digest,
        "stage_ids": [entry["stage_id"] for entry in stages],
        "starting_snapshot": {
            key: starting[key] for key in ("path", "git_common_dir", "branch", "head")
        },
        "repositories": {"inspected": inspected, "at_approval": now_seen},
        "prerequisites": {
            "gates_confirmed": sorted(confirmed),
            "earlier_slices": [prior.evidence for prior in completed],
            "indirect_slices": [prior.evidence for prior in indirect],
            "parallel_slices": [prior.evidence for prior in parallel],
        },
        **decision,
    }
    # The manifest may be replaced until an approval exists for it; the
    # approval is created exactly once, and it is what makes the manifest
    # runnable. A crash between the two leaves an unapproved manifest that
    # run-plan refuses and a repeat of this approval completes.
    write_atomic(manifest_path, manifest_text)
    registry = intake_root(sparring_dir) / REGISTRY_DIRNAME / f"{run_key}.json"
    write_atomic(
        registry,
        json.dumps(
            {
                "run_key": run_key,
                "intake_dir": str(intake_dir.resolve()),
                "run_id": run_id,
                "stage_ids": approval["stage_ids"],
            },
            indent=2,
        )
        + "\n",
    )
    if not write_exclusive(approval_path, json.dumps(approval, indent=2) + "\n"):
        # Another approval won the race; it decides, and this one must agree.
        return existing()
    return result(created=True)


__all__ = [
    "AGENT_FINDING_CODES",
    "Approval",
    "Finding",
    "INTAKE_DIRNAME",
    "INTERPRETATION_SCHEMA",
    "IntakeError",
    "IntakeResult",
    "Interpretation",
    "MODES",
    "SourcePlan",
    "all_findings",
    "approve_plan",
    "check_interpretation",
    "intake_not_ignored_message",
    "parse_interpretation",
    "prepare_plan",
    "render_brief",
    "repository_snapshot",
    "require_intake_ignored",
    "run_prerequisites",
    "feature_branch_needed",
    "slice_branch",
    "slice_runs_agent",
    "split_prerequisites",
]
