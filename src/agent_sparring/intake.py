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
3. **The execution manifest.** The existing contract, unchanged. Only
   :func:`approve_plan` writes one, and only for a run slice a person
   explicitly approved. ``run-plan --manifest`` then executes it exactly as
   it would any other manifest -- digest pinned, briefs frozen, no agent
   able to edit it, every existing gate in force. The runtime never asks a
   model what the next stage is.

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
  requires the person to confirm, by id, every gate that blocks it and every
  earlier slice it depends on; the confirmations are recorded.
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
``intake.json`` (the engine's record: digests, run keys, provider). None of
these is in manifest format, so none can be handed to ``run-plan`` by
mistake. Approval adds ``runs/<run>/manifest.json`` and
``runs/<run>/approval.json``, once; approving again must produce the
identical manifest.

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

from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.intake_prompt import MODE_FAITHFUL, MODE_REFINE, assemble_intake_prompt
from agent_sparring.manifest import MANIFEST_VERSION, ManifestError, manifest_digest, parse_manifest
from agent_sparring.plan import new_run_key, plan_key, plan_label
from agent_sparring.providers import ProviderError, StructuredAgentAdapter
from agent_sparring.sparring_agent import repo_fingerprint
from agent_sparring.stage import StageError, StageMode, validate_stage_id

INTAKE_DIRNAME = "intake"
INTAKE_VERSION = 1
MODES: tuple[str, ...] = (MODE_FAITHFUL, MODE_REFINE)

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

    # Context blocks.
    context_ids: set[str] = set()
    for block in interpretation.context:
        if block.id in context_ids:
            add("duplicate_id", f"context id {block.id!r} is declared twice")
        context_ids.add(block.id)
        good = valid(block.ranges, f"context block {block.id!r}")
        if not good:
            add("empty_reference", f"context block {block.id!r} cites no source text")
        cover(good)

    # Run slices and stages.
    if not interpretation.runs:
        add("no_stages", "the interpretation defines no run slice and no stage")
    run_ids: set[str] = set()
    position: dict[str, tuple[int, int]] = {}
    for run_index, run in enumerate(interpretation.runs):
        if run.id in run_ids:
            add("duplicate_id", f"run slice id {run.id!r} is declared twice")
        run_ids.add(run.id)
        if not run.stages:
            add("empty_run", f"run slice {run.id!r} has no stages")
        for stage_index, stage in enumerate(run.stages):
            if stage.label in position:
                add("duplicate_id", f"stage label {stage.label!r} is used twice", stages=[stage.label])
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

    # Gates: a gate that blocks executable work must separate run slices.
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
        for blocked in gate.blocks_stages:
            where = position.get(blocked)
            if where is None:
                add("unknown_reference", f"{owner} blocks unknown stage {blocked!r}")
                continue
            if after is None:
                continue
            if where[0] == after[0]:
                add(
                    "gate_inside_run",
                    f"{owner} ({gate.kind}) must be satisfied after stage {gate.after_stage} and "
                    f"before stage {blocked}, but both are in run slice "
                    f"{interpretation.runs[where[0]].id!r}. The runtime cannot wait for it "
                    "between two stages of one run; the blocked stage belongs in a later run slice",
                    stages=[gate.after_stage or "", blocked],
                    ranges=good,
                )
            elif where[0] < after[0]:
                add(
                    "dependency_not_satisfied",
                    f"{owner} blocks stage {blocked}, which runs in an earlier run slice than "
                    f"the stage {gate.after_stage} it follows",
                    stages=[gate.after_stage or "", blocked],
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
) -> str:
    """The exact ``brief.md`` for one stage, assembled from source text.

    Context blocks first, in source order, each verbatim; then the stage's
    own ranges, verbatim; then the labelled intake note, if any. Line
    numbers are cited so a reviewer can find every excerpt in the plan.
    The brief's own sections are level-1 headings so that the quoted
    plan's headings, which are never rewritten, nest beneath them.
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

    watched = [repo_root, *context_repositories.values()]
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

    interpretation = parse_interpretation(result.text)
    findings = all_findings(interpretation, source, mode=mode)

    run_keys = {run.id: mint_run_key(label) for run in interpretation.runs}
    briefs = _render_briefs(source, interpretation, run_keys)
    duplicate_ids = _duplicates([stage_id for stage_id, _, _ in briefs.values()])
    if duplicate_ids:
        raise IntakeError(f"derived stage ids collide: {duplicate_ids}; labels must slug uniquely")

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
        "provider": dict(provider or {}),
        "provider_session_id": result.session_id,
        "amendment_proposed": amendment is not None,
    }
    _write(directory / "intake.json", json.dumps(record, indent=2) + "\n")
    _write(
        directory / "report.md",
        render_report(
            source,
            interpretation,
            findings,
            run_keys=run_keys,
            briefs=briefs,
            mode=mode,
            intake_dir=directory,
            amendment=amendment is not None,
            provider=record["provider"],
        ),
    )
    return IntakeResult(directory=directory, interpretation=interpretation, findings=findings, run_keys=run_keys)


def _fingerprint(path: Path) -> tuple[str, str, tuple[str, ...]]:
    try:
        return repo_fingerprint(Path(path))
    except GitContextError as exc:
        raise IntakeError(f"cannot fingerprint repository {path}: {exc}") from exc


def _render_briefs(
    source: SourcePlan, interpretation: Interpretation, run_keys: Mapping[str, str]
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """``{(run id, label): (stage id, relative path, content)}``."""

    briefs: dict[tuple[str, str], tuple[str, str, str]] = {}
    for run in interpretation.runs:
        for index, stage in enumerate(run.stages, start=1):
            stage_id = stage_id_for(run_keys[run.id], stage)
            content = render_brief(source, interpretation, run, stage, stage_id=stage_id, position=index)
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
        prerequisites = run_prerequisites(interpretation, run.id)
        out += [
            "",
            f"### Run slice `{run.id}` — primary `{run.primary_repository}`",
            "",
            f"- Expected branch: {('`' + run.expected_branch + '`') if run.expected_branch else 'not stated (give --expected-branch at approval)'}",
            f"- Run key: `{run_keys.get(run.id, '?')}`",
            f"- Must confirm before approval: {', '.join('`' + p + '`' for p in prerequisites) or 'nothing'}",
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
        for run in interpretation.runs:
            prerequisites = run_prerequisites(interpretation, run.id)
            parts = [f"sparring approve-plan {intake_dir} --run {run.id}"]
            if not run.expected_branch:
                parts.append("--expected-branch <branch>")
            siblings = sorted({name for stage in run.stages for name in stage.repositories})
            for name in siblings:
                parts.append(f"--repository {name}=<path> --repository-branch {name}=<branch>")
            for prerequisite in prerequisites:
                parts.append(f"--confirm-prerequisite {prerequisite}")
            if amendment:
                parts.append("--without-amendment")
            out.append(f"- From the `{run.primary_repository}` project: `{' '.join(parts)}`")
    return "\n".join(out).rstrip() + "\n"


# -- approve ----------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    manifest_path: Path
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


def approve_plan(
    intake_dir: Path,
    *,
    run_id: str,
    primary_repository: str,
    expected_branch: str | None = None,
    repositories: Mapping[str, str] | None = None,
    repository_branches: Mapping[str, str] | None = None,
    confirmed_prerequisites: Sequence[str] = (),
    without_amendment: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> Approval:
    """Turn one reviewed run slice into an execution manifest, or refuse.

    Every refusal names what to do. The manifest is built from the same
    source snapshot and interpretation the report was rendered from, with
    the same deterministic checks, so it holds exactly what the person
    reviewed. It is then validated with the engine's own manifest parser.
    """

    intake_dir = Path(intake_dir)
    record = _read_json(intake_dir / "intake.json")
    if record.get("version") != INTAKE_VERSION:
        raise IntakeError(f"unsupported intake record version {record.get('version')!r}")
    mode = str(record.get("mode"))

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
    blocking = [f for f in findings if f.blocking]
    if blocking:
        listed = "\n".join(f"- {f.code}: {f.message}" for f in blocking)
        raise IntakeError(f"approval refused: {len(blocking)} blocking finding(s)\n{listed}")
    if interpretation.verdict == VERDICT_CANNOT_INTERPRET:
        raise IntakeError("approval refused: the intake agent could not interpret the plan faithfully")
    if record.get("amendment_proposed") and not without_amendment:
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

    required = set(run_prerequisites(interpretation, run_id))
    confirmed = {value.strip() for value in confirmed_prerequisites if value.strip()}
    known = {gate.id for gate in interpretation.gates} | set(runs)
    unknown = sorted(confirmed - known)
    if unknown:
        raise IntakeError(f"--confirm-prerequisite names nothing in this intake: {unknown}")
    irrelevant = sorted(confirmed - required)
    if irrelevant:
        raise IntakeError(f"run slice {run_id!r} does not depend on {irrelevant}; confirm only its prerequisites")
    missing = sorted(required - confirmed)
    if missing:
        raise IntakeError(
            f"run slice {run_id!r} is blocked until a person confirms {missing} (gates it waits "
            "for, or earlier run slices it depends on). The runtime cannot enforce these inside "
            "a run; confirm each with --confirm-prerequisite once it is actually satisfied"
        )

    branch = (expected_branch or "").strip() or run.expected_branch
    if not branch:
        raise IntakeError(f"run slice {run_id!r} states no expected branch; pass --expected-branch")

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

    run_keys = record.get("run_keys") or {}
    run_key = run_keys.get(run_id)
    if not isinstance(run_key, str) or not run_key:
        raise IntakeError(f"intake.json records no run key for run slice {run_id!r}")
    briefs = _render_briefs(source, interpretation, {run_id: run_key, **run_keys})
    recorded_briefs = record.get("briefs") or {}

    stages = []
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
    manifest_text = json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n"
    try:
        manifest = parse_manifest(manifest_text)
    except (ManifestError, StageError) as exc:
        raise IntakeError(f"the approved slice does not form a valid manifest: {exc}") from exc
    digest = manifest_digest(manifest)

    run_dir = intake_dir / "runs" / (_slug(run_id) or "run")
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        if _read_text(manifest_path) != manifest_text:
            raise IntakeError(
                f"run slice {run_id!r} was already approved with a different manifest "
                f"({manifest_path}); an approval is not rewritten. Run prepare-plan again"
            )
        return Approval(manifest_path=manifest_path, run_key=run_key, expected_branch=branch, manifest_digest=digest, created=False)

    run_dir.mkdir(parents=True, exist_ok=True)
    _write(manifest_path, manifest_text)
    approval = {
        "version": INTAKE_VERSION,
        "intake_id": record.get("intake_id"),
        "run_id": run_id,
        "run_key": run_key,
        "approved_at": now().isoformat(),
        "primary_repository": primary_repository,
        "expected_branch": branch,
        "source_digest": record.get("source_digest"),
        "interpretation_digest": record.get("interpretation_digest"),
        "manifest_digest": digest,
        "confirmed_prerequisites": sorted(confirmed),
        "without_amendment": bool(without_amendment and record.get("amendment_proposed")),
    }
    _write(run_dir / "approval.json", json.dumps(approval, indent=2) + "\n")
    return Approval(manifest_path=manifest_path, run_key=run_key, expected_branch=branch, manifest_digest=digest, created=True)


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
    "require_intake_ignored",
    "run_prerequisites",
]
