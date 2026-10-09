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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agent_sparring.branch_guard import is_protected_branch
from agent_sparring.git_context import GitContextError, current_branch, is_ignored, repository_identity
from agent_sparring.intake_approval import (
    APPROVAL_FILENAME,
    APPROVAL_VERSION,
    DECISIONS_FILENAME,
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
from agent_sparring.intake_prompt import MODE_COMPILE, MODE_FAITHFUL, MODE_REFINE, assemble_intake_prompt
from agent_sparring.manifest import (
    MANIFEST_VERSION,
    MANIFEST_VERSION_GATES,
    ManifestError,
    manifest_digest,
    parse_manifest,
)
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
MODES: tuple[str, ...] = (MODE_FAITHFUL, MODE_REFINE, MODE_COMPILE)

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

#: Compile mode's finding dispositions (see :mod:`agent_sparring.intake_prompt`).
#: Faithful and refine findings carry none; their severity is the agent's.
DISPOSITION_AUTO_RESOLVED = "auto_resolved"
DISPOSITION_PLAN_NOTE = "plan_note"
DISPOSITION_NEEDS_DECISION = "needs_decision"
DISPOSITION_REFUSE = "refuse"
DISPOSITIONS: tuple[str, ...] = (
    DISPOSITION_AUTO_RESOLVED,
    DISPOSITION_PLAN_NOTE,
    DISPOSITION_NEEDS_DECISION,
    DISPOSITION_REFUSE,
)
#: A compile finding's severity is derived from its disposition, never stated.
DISPOSITION_SEVERITY: dict[str, str] = {
    DISPOSITION_AUTO_RESOLVED: SEVERITY_INFO,
    DISPOSITION_PLAN_NOTE: SEVERITY_RECOMMENDATION,
    DISPOSITION_NEEDS_DECISION: SEVERITY_BLOCKING,
    DISPOSITION_REFUSE: SEVERITY_BLOCKING,
}
#: The closed set of normalizations an ``auto_resolved`` finding may claim.
#: Each has a deterministic guard (:func:`_transform_problem`); any other
#: auto-resolution, or one whose guard fails, is downgraded to
#: ``needs_decision``.
TRANSFORMS: tuple[str, ...] = (
    "relabel_stage",
    "attach_context",
    "split_review_barrier",
    "gate_at_boundary",
    "reorder_within_candidate",
    "conditional_sibling",
)

GATE_KINDS: tuple[str, ...] = ("production", "manual", "external", "deferred")
STAGE_MODES: tuple[str, ...] = tuple(mode.value for mode in StageMode)

_HORIZONTAL_RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
#: Source lines compile mode treats as a review requirement, for the "maps
#: to a node" guard.
_REVIEW_REQUIREMENT_RE = re.compile(r"\bindependent(ly)?\s+review", re.IGNORECASE)
#: The "no gate disappears" guard works per sentence (headings, fenced code and
#: inline code spans ignored). A sentence states a gate when it names a
#: gate-like event and a stage is the object of a prerequisite relation:
#: "run a canary ... before Stage 3A", "Activate the release after Stage 1A,
#: with explicit go-ahead", "Stage 3A requires the owner's go-ahead". A stage
#: merely mentioned near such a word ("rendered in Stage 2", "Status: not
#: approved") is not a gate. A false match is not harmless: it refuses an
#: honest compile, so these patterns stay specific.
_GATE_TEXT_RE = re.compile(
    r"(?<![\w-])(gate[sd]?|canar(y|ies)|deploy\w*|go-ahead|sign-?off|approv(al|e|ed)|authori[sz]\w*|activat\w*)(?![\w-])",
    re.IGNORECASE,
)
_NEGATED_GATE_RE = re.compile(r"\bnot\s+(yet\s+)?(approved|authori[sz]ed|signed[- ]off|gated)\b", re.IGNORECASE)
_STAGE_REF = r"stages?\s+[A-Za-z]*\d\w*"
_START_PROHIBITION = r"(do\s+not|don\'t|never|cannot|can\'t|must\s+not|may\s+not)\s+(start|begin|run)\s+(the\s+)?"
_STAGE_AS_PREREQUISITE_RE = re.compile(
    rf"\b(before|after|until|once|prior\s+to)\s+(the\s+)?{_STAGE_REF}"
    rf"|\b{_START_PROHIBITION}{_STAGE_REF}"
    rf"|\b{_STAGE_REF}(\'s)?\s+(\w+\s+)?(requires?|needs?|must\s+wait|waits?|cannot\s+start|can\'t\s+start"
    r"|may\s+not\s+start|must\s+not\s+start|is\s+(gated|blocked)|depends)\b",
    re.IGNORECASE,
)
#: Within a gate sentence, which stage the gate blocks ("before Stage 3A",
#: "Stage 3A requires ...", "do not start Stage 3A ...") and which it
#: follows ("after Stage 1A", "until Stage 5 is complete" -- the gated
#: action waits for that stage). The declared gate carrying the sentence
#: must keep exactly these relations.
_LABEL = r"stages?\s+(?P<{}>[A-Za-z]*\d\w*)"
_GATE_BLOCKS_RE = re.compile(
    rf"\b(before|prior\s+to)\s+(the\s+)?{_LABEL.format('label')}"
    rf"|\b{_START_PROHIBITION}{_LABEL.format('object')}"
    rf"|\b{_LABEL.format('subject')}(\'s)?\s+(\w+\s+)?(requires?|needs?|must\s+wait|waits?|cannot\s+start|can\'t\s+start"
    r"|may\s+not\s+start|must\s+not\s+start|is\s+(gated|blocked)|depends)\b",
    re.IGNORECASE,
)
_GATE_FOLLOWS_RE = re.compile(rf"\b(after|once|until)\s+(the\s+)?{_LABEL.format('label')}", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(`+)[^`]*?\1")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
#: Evidence, in a gate's own source text, of each gate kind -- what
#: ``gate_at_boundary`` checks so a moved gate cannot silently change kind.
_GATE_KIND_EVIDENCE: dict[str, re.Pattern[str]] = {
    "production": re.compile(r"\b(production|deploy\w*|release|activat\w*)\b", re.IGNORECASE),
    "manual": re.compile(r"\b(manual\w*|canar(y|ies)|owner|go-ahead|sign-?off|approv\w*)\b", re.IGNORECASE),
    "external": re.compile(r"\b(external|third[- ]party|vendor|upstream)\b", re.IGNORECASE),
    "deferred": re.compile(r"\b(defer\w*|later|follow-?up)\b", re.IGNORECASE),
}


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


def _interpretation_properties(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {
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
        "findings": {"type": "array", "items": finding},
        "amended_plan": {"type": ["string", "null"]},
    }


_FINDING_SCHEMA: dict[str, Any] = _object(
    {
        "code": {"type": "string", "enum": list(AGENT_FINDING_CODES)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "stages": _STRINGS_SCHEMA,
        "ranges": _RANGES_SCHEMA,
        "message": {"type": "string"},
    }
)
_DECISION_SCHEMA: dict[str, Any] = {
    **_object(
        {
            "id": {"type": "string"},
            "question": {"type": "string"},
            "why": {"type": "string"},
            "options": {
                "type": "array",
                "items": _object(
                    {"id": {"type": "string"}, "label": {"type": "string"}, "consequence": {"type": "string"}}
                ),
            },
        }
    ),
    "type": ["object", "null"],
}
_COMPILE_FINDING_SCHEMA: dict[str, Any] = _object(
    {
        "code": {"type": "string", "enum": list(AGENT_FINDING_CODES)},
        "disposition": {"type": "string", "enum": list(DISPOSITIONS)},
        "transform": {"type": ["string", "null"], "enum": [*TRANSFORMS, None]},
        "decision": _DECISION_SCHEMA,
        "stages": _STRINGS_SCHEMA,
        "ranges": _RANGES_SCHEMA,
        "message": {"type": "string"},
    }
)

INTERPRETATION_SCHEMA: dict[str, Any] = _object(_interpretation_properties(_FINDING_SCHEMA))
#: Compile mode's answer: the same interpretation, with findings that carry a
#: ``disposition``, ``transform`` and ``decision`` instead of a severity.
COMPILE_INTERPRETATION_SCHEMA: dict[str, Any] = _object(_interpretation_properties(_COMPILE_FINDING_SCHEMA))


def interpretation_schema(mode: str) -> dict[str, Any]:
    return COMPILE_INTERPRETATION_SCHEMA if mode == MODE_COMPILE else INTERPRETATION_SCHEMA


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
class DecisionOption:
    id: str
    label: str
    consequence: str


@dataclass(frozen=True)
class Decision:
    """What a person must choose for a compile-mode ``needs_decision``
    finding. Recorded here; answering one is not part of this module yet."""

    id: str
    question: str
    why: str
    options: tuple[DecisionOption, ...]


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
    #: Compile mode only (``None`` otherwise): see :data:`DISPOSITIONS`.
    #: ``severity`` is then :data:`DISPOSITION_SEVERITY` of it.
    disposition: str | None = None
    transform: str | None = None
    decision: Decision | None = None
    #: Why the engine downgraded the agent's ``auto_resolved`` claim to
    #: ``needs_decision`` (see :func:`guard_compile_findings`), else ``None``.
    downgraded: str | None = None

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
    #: Read in compile mode, where a gate inside a run slice is rendered into
    #: that slice's manifest (:func:`slice_manifest_gates`) instead of refused.
    compiled: bool = False

    def stages(self) -> Iterable[tuple[int, int, RunSlice, IntakeStage]]:
        """Every stage with its (run index, stage index) coordinates."""

        for run_index, run in enumerate(self.runs):
            for stage_index, stage in enumerate(run.stages):
                yield run_index, stage_index, run, stage


def parse_interpretation(text: str, *, mode: str | None = None) -> Interpretation:
    """Parse the agent's answer structurally, or raise :class:`IntakeError`.

    Only shape is checked here -- the provider already validated the
    schema, and this repeats the part the engine depends on rather than
    trusting it. What the answer *means* (ranges in bounds, coverage,
    boundaries, references) is :func:`check_interpretation`'s, which reports
    findings instead of refusing, so a person sees every problem at once.

    ``mode`` selects the finding shape: compile findings carry a disposition
    (and derive their severity from it); faithful and refine findings carry
    a severity and must not carry a disposition. ``None`` accepts either
    shape per finding, for readers that only need the topology.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IntakeError(f"the intake agent's answer is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise IntakeError("the intake agent's answer must be a JSON object")
    return _interpretation(payload, mode)


def _interpretation(payload: Mapping[str, Any], mode: str | None = None) -> Interpretation:
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
        findings=tuple(_finding(item, mode) for item in _list(payload, "findings", "interpretation")),
        amended_plan=_optional_str(payload, "amended_plan", "interpretation", strip=False),
        compiled=mode == MODE_COMPILE,
    )


def _finding(item: Any, mode: str | None) -> Finding:
    where = "finding"
    if not isinstance(item, Mapping):
        raise IntakeError(f"each {where} must be a JSON object")
    compiled = "disposition" in item
    if mode == MODE_COMPILE and not compiled:
        raise IntakeError("a compile-mode finding must carry a 'disposition'")
    if mode is not None and mode != MODE_COMPILE and compiled:
        raise IntakeError(f"a {mode}-mode finding must not carry a 'disposition'; only compile mode has one")
    common = dict(
        code=_enum(item, "code", AGENT_FINDING_CODES, where),
        message=_str(item, "message", where),
        stages=_strs(item, "stages", where),
        ranges=_ranges(item, "ranges", where),
        origin="agent",
    )
    if not compiled:
        return Finding(severity=_enum(item, "severity", SEVERITIES, where), **common)
    disposition = _enum(item, "disposition", DISPOSITIONS, where)
    # An unknown transform is not a parse error: like any auto-resolution
    # the engine cannot verify, it is downgraded, so a person sees it.
    transform = _optional_str(item, "transform", where)
    decision = _decision(item.get("decision"))
    if decision is not None and disposition != DISPOSITION_NEEDS_DECISION:
        raise IntakeError(f"a {disposition!r} finding must not carry a decision; only needs_decision does")
    return Finding(
        severity=DISPOSITION_SEVERITY[disposition],
        disposition=disposition,
        transform=transform,
        decision=decision,
        **common,
    )


def _decision(value: Any) -> Decision | None:
    where = "decision"
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IntakeError("a finding's 'decision' must be a JSON object or null")
    options = tuple(
        DecisionOption(
            id=_str(option, "id", "decision option", nonempty=True),
            label=_str(option, "label", "decision option", nonempty=True),
            consequence=_str(option, "consequence", "decision option", nonempty=True),
        )
        for option in _list(value, "options", where)
    )
    decision = Decision(
        id=_str(value, "id", where, nonempty=True),
        question=_str(value, "question", where, nonempty=True),
        why=_str(value, "why", where),
        options=options,
    )
    if len(options) < 2:
        raise IntakeError(f"decision {decision.id!r} must offer at least two options")
    if len({option.id for option in options}) != len(options):
        raise IntakeError(f"decision {decision.id!r} repeats an option id")
    return decision


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

    def section_at(self, line: int) -> LineRange | None:
        """The Markdown heading section that starts at ``line`` -- through the
        line before the next heading of the same or a higher level -- or
        ``None`` if ``line`` is not a heading. Fenced code is not a heading."""

        level: int | None = None
        fenced = False
        for number, text in enumerate(self.lines, start=1):
            if _FENCE_RE.match(text):
                fenced = not fenced
                continue
            match = None if fenced else _HEADING_RE.match(text)
            if number == line:
                if match is None:
                    return None
                level = len(match.group(1))
            elif level is not None and match is not None and len(match.group(1)) <= level:
                return LineRange(line, number - 1)
        return LineRange(line, len(self.lines)) if level is not None else None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- deterministic checks -------------------------------------------------------


@dataclass(frozen=True)
class CompileContext:
    """What compile mode's repository guards check against, fixed at prepare
    and recorded in ``intake.json`` so approval recomputes the same findings.

    ``named``: repository names the source plan or PROJECT.md names (and the
    preparing project's own). ``inspected``: every repository intake read.
    """

    named: frozenset[str]
    inspected: frozenset[str]

    def as_dict(self) -> dict[str, list[str]]:
        return {"named_repositories": sorted(self.named), "inspected_repositories": sorted(self.inspected)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CompileContext":
        value = record.get("compile")
        if not isinstance(value, Mapping):
            raise IntakeError("this compile-mode intake.json records no compile context; run prepare-plan again")
        named, inspected = value.get("named_repositories"), value.get("inspected_repositories")
        if not all(isinstance(v, list) and all(isinstance(n, str) for n in v) for v in (named, inspected)):
            raise IntakeError("intake.json records a malformed compile context; run prepare-plan again")
        repositories = record.get("repositories")
        if not isinstance(repositories, Mapping) or set(inspected) != set(repositories):
            raise IntakeError(
                "intake.json's compile context does not match its recorded repositories; run prepare-plan again"
            )
        return cls(named=frozenset(named), inspected=frozenset(inspected))


def names_repository(text: str, name: str) -> bool:
    """Whether ``text`` names repository ``name`` as a repository.

    Conservative, so a plain word in prose ("the web client", "an app
    restart") does not count: the name must be in a code span, next to
    "repository"/"repo" (including a "Repos: a, b" list), or be a qualified name (containing ``-``, ``_``,
    ``.`` or ``/``) appearing as a whole token.
    """

    if not name:
        return False
    escaped = re.escape(name)
    token = rf"(?<![\w./-]){escaped}(?![\w/-])(?!\.\w)"
    patterns = [
        rf"`{escaped}`",
        # "repository web", "Repos: app, web", "repos `app` and web"
        rf"\b(repository|repositories|repo|repos)\b:?\s+(`?[\w./-]+`?\s*(,|and)\s*)*`?{token}",
        rf"{token}\s+(repository|repo)\b",
    ]
    if re.search(r"[-_./]", name):
        patterns.append(token)
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


# A name the document itself calls a repository: "**Pilot repository:**
# `sporely-landing`", "the `web` repo", "repository sporely-web".
_CALLED_REPOSITORY_RES = (
    re.compile(r"\b(?:repository|repo)\b[\s:*]{0,6}(?P<q>`?)(?P<name>[A-Za-z0-9][\w.-]*)(?P=q)", re.IGNORECASE),
    re.compile(r"(?P<q>`?)(?P<name>[A-Za-z0-9][\w.-]*)(?P=q)\s+(?:repository|repo)\b", re.IGNORECASE),
)


def undeclared_repository_mentions(text: str, stage_sections: Iterable[str], home: str) -> tuple[str, ...]:
    """Repositories other than ``home`` that a plan without ``Repository:``
    declarations names in a heading or a stage section -- the shape of a
    cross-repository plan whose ownership was never declared.

    Nothing configures which names are repositories, so candidates come from
    how the document writes them. A candidate is any of:

    - a name the document calls a repository ("**Pilot repository:**
      `sporely-landing`", "the `web` repo");
    - a lower-case hyphenated name backticked on its own in a heading or in
      stage prose ("Implement in `sporely-landing`.");
    - a lower-case hyphenated name in a heading, written bare ("## Stage 4 —
      sporely-landing pilot"), when the document corroborates it as a name:
      backticked somewhere, called a repository, or sharing its first part
      with another candidate or with ``home``. Bare heading words alone
      ("end-to-end", "read-only", "dry-run") are ordinary English as often
      as they are names, and warning on every one would make the warning
      noise.

    Commands (``sparring run-plan``, ``npm run x``), flags, paths, files and
    snake_case identifiers are not repository names. A candidate counts only
    if :func:`names_repository` finds it in a heading or a stage section.
    Advisory: a caller warns, never refuses.
    """

    sections = list(stage_sections)
    lines = text.splitlines()
    headings = "\n".join(line for line in lines if line.lstrip().startswith("#"))
    called: set[str] = set()
    for pattern in _CALLED_REPOSITORY_RES:
        for match in pattern.finditer(text):
            name = match.group("name").rstrip(".-")
            if match.group("q") or re.search(r"[-_.]", name):
                called.add(name)
    backticked_anywhere = set(re.findall(r"`([^`\s]+)`", text))
    backticked = {
        name
        for scope in (headings, *sections)
        for name in re.findall(r"`([^`\s]+)`", scope)
        if _HYPHENATED_NAME_RE.fullmatch(name)
    }
    bare = set(re.findall(r"(?<![\w./`-])[a-z0-9]+(?:-[a-z0-9]+)+(?![\w/`-])", headings))
    known = called | backticked
    families = {name.split("-", 1)[0] for name in known | {home}}
    corroborated = {
        name
        for name in bare
        if name in backticked_anywhere or name in called or (
            name.split("-", 1)[0] in families and len(name.split("-", 1)[0]) > 2
        )
    }
    commands = {
        match.group(1)
        for match in re.finditer(r"(?:\bsparring|\bnpm run|\bnpx|\bgit|\buv run|\bpnpm|\byarn)\s+([\w.-]+)", text)
    } | set(re.findall(r"`([\w.-]+)\s+--", text))
    candidates = {
        name
        for name in (known | corroborated) - commands
        if not _FILE_LIKE_RE.search(name) and "/" not in name
    }
    scopes = [headings, *sections]
    return tuple(
        sorted(
            name
            for name in candidates
            if name.lower() != home.lower()
            and not re.fullmatch(r"[\d.-]+", name)
            and any(names_repository(scope, name) for scope in scopes)
        )
    )


# A repository name as written in prose: lower-case words joined by hyphens.
# Dotted (``input.kind``), snake_case and CamelCase spans are identifiers.
_HYPHENATED_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
_FILE_LIKE_RE = re.compile(
    r"\.(md|py|toml|json|jsonl|ts|tsx|js|mjs|cjs|yml|yaml|sh|txt|png|jpg|svg|css|html|sql|lock|cfg|ini)$",
    re.IGNORECASE,
)


def compile_context_for(
    interpretation: Interpretation,
    source: SourcePlan,
    *,
    project_context: str | None,
    primary_repository: str,
    inspected: Iterable[str],
) -> CompileContext:
    """The :class:`CompileContext` of one prepare: of every repository the
    interpretation uses or intake inspected, those the source plan or
    PROJECT.md names, plus the preparing project itself."""

    inspected = frozenset(inspected)
    used = {run.primary_repository for run in interpretation.runs}
    used.update(name for _, _, _, stage in interpretation.stages() for name in stage.repositories)
    texts = (source.text, project_context or "")
    named = {name for name in used | inspected if any(names_repository(text, name) for text in texts)}
    named.add(primary_repository)
    return CompileContext(named=frozenset(named), inspected=inspected)


def check_interpretation(
    interpretation: Interpretation,
    source: SourcePlan,
    *,
    mode: str,
    compile_context: CompileContext | None = None,
) -> tuple[Finding, ...]:
    """Everything the engine can verify about an interpretation, as findings.

    Pure and deterministic, so approval recomputes exactly what prepare
    reported and a person's review of ``report.md`` is a review of what
    approval will enforce. Compile mode keeps every check below and adds its
    own (:func:`_compile_checks`); its findings carry a disposition.
    """

    if mode == MODE_COMPILE and compile_context is None:
        raise ValueError("compile mode is checked against a CompileContext")
    findings: list[Finding] = []

    def add(
        code: str,
        message: str,
        *,
        stages: Sequence[str] = (),
        ranges: Sequence[LineRange] = (),
        severity: str = SEVERITY_BLOCKING,
        disposition: str | None = None,
    ) -> None:
        if mode == MODE_COMPILE:
            disposition = disposition or (DISPOSITION_REFUSE if severity == SEVERITY_BLOCKING else DISPOSITION_PLAN_NOTE)
            severity = DISPOSITION_SEVERITY[disposition]
        else:
            disposition = None
        findings.append(
            Finding(
                code=code,
                severity=severity,
                message=message,
                stages=tuple(stages),
                ranges=tuple(ranges),
                origin="engine",
                disposition=disposition,
            )
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
            # Compile mode renders such a gate into the slice's manifest
            # (gates_before), and the runtime stops for it.
            if after[1] != len(slice_.stages) - 1 and mode != MODE_COMPILE:
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
            same_slice_later = after is not None and where[0] == after[0] and where[1] > after[1]
            if after is not None and where[0] <= after[0] and not (mode == MODE_COMPILE and same_slice_later):
                add(
                    "dependency_not_satisfied",
                    f"{owner} follows stage {gate.after_stage} but blocks stage {blocked}, which "
                    "does not run in a later run slice",
                    stages=[gate.after_stage or "", blocked],
                    ranges=good,
                )
            elif where[1] != 0 and mode != MODE_COMPILE:
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
    if mode == MODE_COMPILE:
        assert compile_context is not None
        if amendment is not None and amendment != source.text:
            add(
                "amendment_in_compile_mode",
                "compile mode never proposes a plan amendment; re-run in refine mode for one",
            )
        _compile_checks(interpretation, source, compile_context, add)
    if interpretation.verdict == VERDICT_CANNOT_INTERPRET and not any(
        f.blocking for f in interpretation.findings
    ):
        add(
            "unexplained_refusal",
            "the agent could not interpret the plan faithfully but raised no blocking finding saying why",
        )
    return tuple(findings)


def _lines(ranges: Iterable[LineRange], total: int) -> set[int]:
    """Every line number ``ranges`` cover, ignoring out-of-range ones (those
    are already an ``invalid_range`` finding)."""

    return {n for r in ranges if 1 <= r.start <= r.end <= total for n in range(r.start, r.end + 1)}


def _sentences(source: SourcePlan) -> list[tuple[list[int], str]]:
    """Each prose sentence with the line numbers it spans. Headings and
    fenced code are skipped; inline code spans are blanked."""

    out: list[tuple[list[int], str]] = []
    paragraph: list[int] = []
    fenced = False

    def flush() -> None:
        if not paragraph:
            return
        text, owner = "", []
        for number in paragraph:
            line = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), source.lines[number - 1])
            text += line + " "
            owner += [number] * (len(line) + 1)
        start = 0
        for match in [*_SENTENCE_END_RE.finditer(text), None]:
            end = match.start() if match else len(text)
            sentence = text[start:end]
            if sentence.strip():
                first = start + len(sentence) - len(sentence.lstrip())
                last = start + len(sentence.rstrip()) - 1
                out.append((sorted(set(owner[first : last + 1])), sentence.strip()))
            if match:
                start = match.end()
        paragraph.clear()

    for number, line in enumerate(source.lines, start=1):
        if _FENCE_RE.match(line):
            flush()
            fenced = not fenced
            continue
        if fenced or _HEADING_RE.match(line) or not line.strip():
            flush()
            continue
        paragraph.append(number)
    flush()
    return out


def states_gate(sentence: str) -> bool:
    """Whether one prose sentence states a gate on a stage."""

    text = _NEGATED_GATE_RE.sub(" ", sentence)
    return bool(_GATE_TEXT_RE.search(text) and _STAGE_AS_PREREQUISITE_RE.search(text))


def gate_relation_problems(
    interpretation: Interpretation, source: SourcePlan
) -> list[tuple[list[int], str]]:
    """``(lines, why)`` for every gate sentence whose declared gate lost the
    stage relation the source states: a gate sentence blocking Stage X
    must be carried by a gate that blocks X; one following Stage Y by a gate
    whose ``after_stage`` is Y. Labels that name no stage of the
    interpretation are not checked. Whether the text is covered at all is
    ``gate_dropped``'s."""

    labels = {_slug(stage.label): stage.label for _, _, _, stage in interpretation.stages()}
    total = len(source.lines)
    problems: list[tuple[list[int], str]] = []
    for lines, sentence in _sentences(source):
        if not states_gate(sentence):
            continue
        text = _NEGATED_GATE_RE.sub(" ", sentence)
        covering = [g for g in interpretation.gates if set(lines) & _lines(g.ranges, total)]
        if not covering:
            continue
        blocked = {
            labels[k]
            for m in _GATE_BLOCKS_RE.finditer(text)
            if (k := _slug(m.group("label") or m.group("subject") or m.group("object"))) in labels
        }
        follows = {labels[k] for m in _GATE_FOLLOWS_RE.finditer(text) if (k := _slug(m.group("label"))) in labels}
        enforced = {label for g in covering for label in g.blocks_stages}
        after = {g.after_stage for g in covering if g.after_stage}
        ids = ", ".join(repr(g.id) for g in covering)
        for label in sorted(blocked - enforced):
            problems.append((lines, f"the source gate blocks stage {label}, but gate {ids} does not block it"))
        for label in sorted(follows - after):
            problems.append((lines, f"the source gate follows stage {label}, but gate {ids} does not follow it"))
    return problems


def _compile_checks(
    interpretation: Interpretation,
    source: SourcePlan,
    context: CompileContext,
    add: Callable[..., None],
) -> None:
    """Compile mode's own guards, on top of every faithful/refine check.

    - Every source line stating an independent review maps to a node: it is
      some stage's own text or context a stage attaches.
    - No gate disappears: every line of a sentence that states a gate (see
      :func:`states_gate`) is part of a declared gate. Stage text, context
      or an exclusion does not substitute for an enforced prerequisite.
    - A repository neither the plan nor PROJECT.md names needs a decision; a
      named one intake did not inspect refuses, saying how to supply it.
    - Every agent ``needs_decision`` carries a decision, with a unique id.
    """

    total = len(source.lines)
    substantive = set(source.substantive_lines())
    blocks = {block.id: block for block in interpretation.context}
    on_node: set[int] = set()
    for _, _, _, stage in interpretation.stages():
        on_node |= _lines(stage.source_ranges, total)
        for cid in stage.context_ids:
            if cid in blocks:
                on_node |= _lines(blocks[cid].ranges, total)
    gated = _lines((r for gate in interpretation.gates for r in gate.ranges), total)

    for number in sorted(substantive):
        text = source.lines[number - 1]
        if _REVIEW_REQUIREMENT_RE.search(text) and number not in on_node:
            add(
                "review_requirement_unmapped",
                f"line {number} states an independent review, but no node carries it "
                f"({text.strip()[:80]!r}); a review requirement must reach a stage's brief",
                ranges=[LineRange(number, number)],
            )

    for paragraph, text in _sentences(source):
        if not states_gate(text):
            continue
        missing = [n for n in paragraph if n in substantive and n not in gated]
        if missing:
            add(
                "gate_dropped",
                f"lines {_ranges_text(_group(missing, source))} state a gate but no declared gate "
                f"carries them ({text.strip()[:80]!r}); a gate in a brief or an exclusion is not enforced",
                ranges=_group(missing, source),
            )

    for lines, why in gate_relation_problems(interpretation, source):
        ranges = _group(lines, source)
        add(
            "gate_unenforced",
            f"lines {_ranges_text(ranges)}: {why}; a gate that no longer blocks what the plan says it "
            "blocks is not enforced",
            ranges=ranges,
        )

    used: dict[str, list[str]] = {}
    for _, _, run, stage in interpretation.stages():
        used.setdefault(run.primary_repository, []).append(stage.label)
        for name in stage.repositories:
            used.setdefault(name, []).append(stage.label)
    for name, labels in sorted(used.items()):
        stages = sorted(set(labels))
        if name not in context.named:
            add(
                "unnamed_repository",
                f"repository {name!r} is used by stages {', '.join(stages)}, but neither the plan "
                "nor PROJECT.md names it; a person decides whether it belongs to this plan",
                stages=stages,
                disposition=DISPOSITION_NEEDS_DECISION,
            )
        elif name not in context.inspected:
            add(
                "sibling_not_supplied",
                f"the plan names repository {name!r} (stages {', '.join(stages)}), but intake did "
                f"not inspect it; run prepare-plan again with --context-repository {name}=<path>",
                stages=stages,
            )

    decision_ids: set[str] = set()
    for finding in interpretation.findings:
        if finding.disposition != DISPOSITION_NEEDS_DECISION:
            continue
        if finding.decision is None:
            add(
                "decision_missing",
                f"agent finding {finding.code!r} needs a decision but states none: {finding.message[:80]}",
                stages=finding.stages,
                ranges=finding.ranges,
            )
        elif finding.decision.id in decision_ids:
            add("duplicate_id", f"decision id {finding.decision.id!r} is used twice")
        else:
            decision_ids.add(finding.decision.id)


def guard_compile_findings(
    interpretation: Interpretation, source: SourcePlan, context: CompileContext
) -> tuple[Finding, ...]:
    """The agent's compile findings, each ``auto_resolved`` one kept only if
    its transform is in :data:`TRANSFORMS` and its guard holds; otherwise it
    becomes ``needs_decision`` (blocking), saying why. Never upgrades."""

    out: list[Finding] = []
    for finding in interpretation.findings:
        if finding.disposition == DISPOSITION_AUTO_RESOLVED:
            problem = _transform_problem(finding, interpretation, source, context)
            if problem is not None:
                finding = replace(
                    finding,
                    disposition=DISPOSITION_NEEDS_DECISION,
                    severity=DISPOSITION_SEVERITY[DISPOSITION_NEEDS_DECISION],
                    downgraded=problem,
                )
        out.append(finding)
    return tuple(out)


def _transform_problem(
    finding: Finding, interpretation: Interpretation, source: SourcePlan, context: CompileContext
) -> str | None:
    """Why ``finding``'s auto-resolution does not hold, or ``None`` if it does."""

    transform = finding.transform
    if transform is None:
        return "auto_resolved without a transform"
    if transform not in TRANSFORMS:
        return f"transform {transform!r} is not one the engine can check (expected one of {list(TRANSFORMS)})"
    total = len(source.lines)
    substantive = set(source.substantive_lines())
    for r in finding.ranges:
        if not 1 <= r.start <= r.end <= total:
            return f"it cites lines {r.start}–{r.end}, outside 1–{total}"
    cited = _lines(finding.ranges, total) & substantive
    where = {stage.label: (index, run, stage) for index, (_, _, run, stage) in enumerate(interpretation.stages())}
    if not finding.stages:
        return f"{transform} names no stage"
    unknown = [label for label in finding.stages if label not in where]
    if unknown:
        return f"{transform} names unknown stage(s) {unknown}"
    stages = [where[label] for label in finding.stages]
    blocks = {block.id: block for block in interpretation.context}

    def own(stage: IntakeStage) -> set[int]:
        return _lines(stage.source_ranges, total) & substantive

    def brief(stage: IntakeStage) -> set[int]:
        lines = own(stage)
        for cid in stage.context_ids:
            if cid in blocks:
                lines |= _lines(blocks[cid].ranges, total) & substantive
        return lines

    def whole_section(stage: IntakeStage) -> str | None:
        first = min((r.start for r in stage.source_ranges), default=0)
        section = source.section_at(first)
        if section is None:
            return f"stage {stage.label}'s text does not start at a heading"
        if own(stage) != _lines([section], total) & substantive:
            return f"stage {stage.label}'s text is not exactly its heading section (lines {section})"
        return None

    def needs_citation() -> str | None:
        return None if cited else f"{transform} cites no source text"

    if transform == "relabel_stage":
        for _, _, stage in stages:
            problem = whole_section(stage)
            if problem:
                return problem
            heading = source.lines[min(r.start for r in stage.source_ranges) - 1]
            if not re.search(rf"(?<![0-9A-Za-z]){re.escape(stage.label)}(?![0-9A-Za-z])", heading):
                return f"stage {stage.label}'s heading does not carry that label"
        return None

    if transform == "attach_context":
        if problem := needs_citation():
            return problem
        for _, _, stage in stages:
            attached = set()
            for cid in stage.context_ids:
                if cid in blocks:
                    attached |= _lines(blocks[cid].ranges, total)
            if not cited <= attached:
                return f"stage {stage.label} does not attach context carrying all the cited text"
        return None

    if transform == "split_review_barrier":
        if problem := needs_citation():
            return problem
        runs = {run.id for _, run, _ in stages}
        implementation = [s for s in stages if s[2].mode == StageMode.IMPLEMENTATION.value]
        reviews = [s for s in stages if s[2].mode == StageMode.INDEPENDENT_REVIEW.value]
        if len(runs) != 1 or not implementation or not reviews:
            return "a review barrier split needs an implementation node and an independent_review node in one run slice"
        if max(index for index, _, _ in implementation) > min(index for index, _, _ in reviews):
            return "the review node does not run after the implementation node it reviews"
        head = implementation[0][2]
        section = source.section_at(min((r.start for r in head.source_ranges), default=0))
        if section is None:
            return f"stage {head.label}'s text does not start at a heading"
        span = _lines([section], total)
        if any(not own(stage) <= span for _, _, stage in stages) or not cited <= span:
            return f"the nodes do not map to one source stage (lines {section})"
        if not any(_REVIEW_REQUIREMENT_RE.search(source.lines[n - 1]) for n in cited):
            return "the cited text does not state an independent review"
        if not cited <= set().union(*(own(stage) for _, _, stage in reviews)):
            return "the cited review text is not the review node's own text"
        return None

    if transform == "gate_at_boundary":
        if problem := needs_citation():
            return problem
        touching = [gate for gate in interpretation.gates if cited & _lines(gate.ranges, total)]
        if not cited <= _lines((r for gate in touching for r in gate.ranges), total):
            return "some cited gate text no longer maps to a declared gate"
        for gate in touching:
            if not set(finding.stages) & ({gate.after_stage} | set(gate.blocks_stages)):
                return f"gate {gate.id!r} neither follows nor blocks the named stages"
            text = " ".join(source.lines[n - 1] for n in sorted(_lines(gate.ranges, total)))
            kinds = {kind for kind, evidence in _GATE_KIND_EVIDENCE.items() if evidence.search(text)}
            if kinds != {gate.kind}:
                found = ", ".join(sorted(kinds)) or "none"
                return (
                    f"gate {gate.id!r}'s source text does not establish its kind {gate.kind!r} "
                    f"unambiguously (evidence for: {found})"
                )
        for lines, why in gate_relation_problems(interpretation, source):
            if cited & set(lines):
                return why
        return None

    if transform == "reorder_within_candidate":
        if problem := needs_citation():
            return problem
        if len(stages) < 2 or len({run.id for _, run, _ in stages}) != 1:
            return "a reorder within one candidate names at least two stages of one run slice"
        for _, _, stage in stages:
            problem = whole_section(stage)
            if problem:
                return problem
        if not cited <= set().union(*(own(stage) for _, _, stage in stages)):
            return "the cited dependency text is not the named stages' own text"
        return None

    # conditional_sibling
    if problem := needs_citation():
        return problem
    for _, _, stage in stages:
        if not stage.repositories:
            return f"stage {stage.label} declares no sibling"
        for name in stage.repositories:
            if name not in context.named:
                return f"sibling {name!r} is named by neither the plan nor PROJECT.md"
            if name not in context.inspected:
                return f"sibling {name!r} was not inspected (--context-repository {name}=<path>)"
        if not cited <= brief(stage):
            return f"the cited condition is not carried verbatim in stage {stage.label}'s brief"
    return None


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


def all_findings(
    interpretation: Interpretation,
    source: SourcePlan,
    *,
    mode: str,
    compile_context: CompileContext | None = None,
) -> tuple[Finding, ...]:
    """Agent findings then engine findings, blocking first within each. In
    compile mode the agent's auto-resolutions are guarded first."""

    engine = check_interpretation(interpretation, source, mode=mode, compile_context=compile_context)
    agent = (
        guard_compile_findings(interpretation, source, compile_context)
        if mode == MODE_COMPILE and compile_context is not None
        else interpretation.findings
    )
    ordered = list(agent) + list(engine)
    return tuple(sorted(ordered, key=lambda f: SEVERITIES.index(f.severity)))


def slice_manifest_gates(
    interpretation: Interpretation, run_id: str
) -> tuple[dict[str, tuple[Gate, ...]], tuple[Gate, ...]]:
    """``(gates_before by stage label, completion gates)`` that run slice
    ``run_id``'s manifest carries, so the runtime stops for them.

    Compile mode only; empty otherwise. A gate lands before the earliest
    stage of this slice it must precede: the stage after its ``after_stage``
    when that is inside this slice, or any stage of this slice it blocks --
    including the first, before which the run stops before creating
    anything. A gate after this slice's last stage that blocks nothing is
    the slice's closeout gate.
    """

    before: dict[str, list[Gate]] = {}
    completion: list[Gate] = []
    if not interpretation.compiled:
        return {}, ()
    run = next((run for run in interpretation.runs if run.id == run_id), None)
    if run is None:
        return {}, ()
    index = {stage.label: position for position, stage in enumerate(run.stages)}
    last = len(run.stages) - 1
    for gate in interpretation.gates:
        candidates = [index[b] for b in gate.blocks_stages if b in index]
        after = index.get(gate.after_stage) if gate.after_stage else None
        if after is not None and after < last:
            candidates.append(after + 1)
        if candidates:
            before.setdefault(run.stages[min(candidates)].label, []).append(gate)
        elif after == last and not gate.blocks_stages:
            completion.append(gate)
    return {label: tuple(gates) for label, gates in before.items()}, tuple(completion)


def run_prerequisites(interpretation: Interpretation, run_id: str) -> tuple[str, ...]:
    """What a person must confirm before approving run slice ``run_id``:
    every gate that blocks one of its stages, and every earlier run slice
    one of its stages depends on or one of its gates follows. Sorted, for a stable record. A gate the
    slice's own manifest carries (:func:`slice_manifest_gates`) is not one:
    the run stops for it instead."""

    run_of: dict[str, str] = {
        stage.label: run.id for _, _, run, stage in interpretation.stages()
    }
    labels = {stage.label for _, _, run, stage in interpretation.stages() if run.id == run_id}
    before, completion = slice_manifest_gates(interpretation, run_id)
    in_manifest = {gate.id for gates in before.values() for gate in gates} | {gate.id for gate in completion}
    required: set[str] = set()
    for gate in interpretation.gates:
        if gate.id not in in_manifest and labels.intersection(gate.blocks_stages):
            required.add(gate.id)
        # A gate this slice stops for or waits on that follows another
        # slice's stage stands between the two: the earlier slice must have
        # run, whether or not any stage here also depends on it.
        if gate.id in in_manifest or labels.intersection(gate.blocks_stages):
            other = run_of.get(gate.after_stage or "")
            if other is not None and other != run_id:
                required.add(other)
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
    if any(f.disposition for f in findings):
        counts["dispositions"] = {d: sum(1 for f in findings if f.disposition == d) for d in DISPOSITIONS}
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
    decisions: Sequence[Mapping[str, Any]] = (),
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

    ``decisions`` are the answered decisions of ``decisions.json`` (see
    :func:`answered_decisions`); each that applies to this stage is appended
    under its own labelled heading, after everything quoted from the plan.
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
    for answer in decisions_for_stage(decisions, stage.label):
        option = answer["option"]
        out += [
            "",
            "# Preparation decision (not plan text)",
            "",
            _DECISION_PREAMBLE,
            "",
            f"Decision `{answer['id']}`: {answer['question']}",
            "",
            f"Answer `{option['id']}`: {option['label']}. Consequence: {option['consequence']}",
        ]
    return "\n".join(out).rstrip() + "\n"


_DECISION_PREAMBLE = (
    "A person answered this question when the plan was prepared; it is recorded in the "
    "intake's decisions.json, verbatim. It is not text from the source plan, which was "
    "not edited."
)


def decisions_for_stage(decisions: Sequence[Mapping[str, Any]], label: str) -> list[Mapping[str, Any]]:
    """The answered decisions that apply to stage ``label``: those whose
    finding named it, and those whose finding named no stage at all."""

    return [answer for answer in decisions if not answer.get("stages") or label in answer["stages"]]


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


# -- answered decisions -------------------------------------------------------------

DECISIONS_VERSION = 1


def read_decisions(intake_dir: Path, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The answered decisions an intake was prepared with, verified against
    the digest ``intake.json`` records for them, or ``[]`` for an intake
    prepared without answers. Refuses a ``decisions.json`` that is missing,
    edited, or present without being recorded."""

    recorded = record.get("decisions")
    path = Path(intake_dir) / DECISIONS_FILENAME
    if recorded is None:
        if path.exists():
            raise IntakeError(f"{path} exists but intake.json records no decisions; run prepare-plan again")
        return []
    if not isinstance(recorded, Mapping) or not isinstance(recorded.get("digest"), str):
        raise IntakeError(f"{Path(intake_dir) / 'intake.json'} records malformed decisions; run prepare-plan again")
    text = _read_text(path)
    if sha256_text(text) != recorded["digest"]:
        raise IntakeError(f"{path} changed after the intake was prepared; run prepare-plan again")
    payload = json.loads(text)
    return list(payload.get("answers") or [])


def recorded_answers(record: Mapping[str, Any]) -> dict[str, str]:
    """``{decision id: option id}`` an intake was prepared with (display copy
    in ``intake.json``, sealed with it)."""

    recorded = record.get("decisions")
    return dict(recorded.get("answers") or {}) if isinstance(recorded, Mapping) else {}


def posed_decisions(interpretation: Interpretation) -> dict[str, Finding]:
    """Every decision a ``needs_decision`` finding of this interpretation asks."""

    return {
        f.decision.id: f
        for f in interpretation.findings
        if f.decision is not None and f.disposition == DISPOSITION_NEEDS_DECISION
    }


def answered_decisions(parent_dir: Path, answers: Mapping[str, str], source_text: str) -> dict[str, Any]:
    """``decisions.json`` for a compile prepare that answers ``parent_dir``'s
    decisions, or refuse (:class:`IntakeError`, before any provider turn).

    Refuses unless the parent is a finished compile intake, the source plan
    still has the parent's source digest, and every answer names a decision
    the parent's interpretation asks (or one the parent itself was prepared
    with, given the same option) and one of that decision's options. The
    parent's own answers are carried forward, so answers accumulate. The
    question, option label and consequence are copied verbatim.
    """

    parent_dir = Path(parent_dir)
    record = _read_json(parent_dir / "intake.json")
    problem = completion_marker_problem(parent_dir, record)
    if problem:
        raise IntakeError(problem)
    if record.get("mode") != MODE_COMPILE:
        raise IntakeError(f"{parent_dir} is a {record.get('mode')!r} intake; only compile intakes ask decisions")
    if sha256_text(source_text) != record.get("source_digest"):
        raise IntakeError(
            f"the source plan changed since {parent_dir.name} asked its questions; its decisions no "
            "longer describe it. Prepare it again without answers"
        )
    interpretation_text = _read_text(parent_dir / "interpretation.json")
    if sha256_text(interpretation_text) != record.get("interpretation_digest"):
        raise IntakeError(f"{parent_dir / 'interpretation.json'} was modified after intake")
    posed = posed_decisions(parse_interpretation(interpretation_text, mode=MODE_COMPILE))
    entries = {entry["id"]: entry for entry in read_decisions(parent_dir, record)}
    parent_id = str(record.get("intake_id") or parent_dir.name)
    for decision_id, option_id in answers.items():
        if decision_id in entries:
            if entries[decision_id]["option"]["id"] != option_id:
                raise IntakeError(
                    f"decision {decision_id!r} was already answered {entries[decision_id]['option']['id']!r}; "
                    "an answer is not changed by answering again. Prepare without answers to start over"
                )
            continue
        finding = posed.get(decision_id)
        if finding is None:
            raise IntakeError(
                f"--answer {decision_id}=…: {parent_id} asks no decision {decision_id!r}; it asks "
                f"{sorted(posed) or 'none'}"
            )
        decision = finding.decision
        assert decision is not None
        option = next((o for o in decision.options if o.id == option_id), None)
        if option is None:
            raise IntakeError(
                f"--answer {decision_id}={option_id}: decision {decision_id!r} offers "
                f"{[o.id for o in decision.options]}"
            )
        entries[decision_id] = {
            "id": decision.id,
            "question": decision.question,
            "why": decision.why,
            "option": {"id": option.id, "label": option.label, "consequence": option.consequence},
            "stages": [s for s in finding.stages if s],
            "posed_by": parent_id,
        }
    return {
        "version": DECISIONS_VERSION,
        "parent": {"intake_id": parent_id, "interpretation_digest": record.get("interpretation_digest")},
        "source_digest": record.get("source_digest"),
        "answers": [entries[key] for key in sorted(entries)],
    }


def _decisions_note(decisions: Mapping[str, Any]) -> str:
    lines = [
        "## Preparation decisions (a person's answers)",
        "",
        "An earlier intake of this plan asked these questions and a person answered them. "
        "Interpret the plan with these answers applied: do not ask them again, and do not "
        "treat them as plan text or edit the plan for them.",
    ]
    for answer in decisions["answers"]:
        option = answer["option"]
        stages = ", ".join(answer.get("stages") or []) or "every stage"
        lines += [
            "",
            f"- `{answer['id']}` ({stages}): {answer['question']}",
            f"  Answer `{option['id']}`: {option['label']}. Consequence: {option['consequence']}",
        ]
    return "\n".join(lines)


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
    answer_parent: Path | None = None,
    answers: Mapping[str, str] | None = None,
    validate: Callable[[Interpretation, list[Finding]], None] | None = None,
) -> IntakeResult:
    """Run one intake turn and write a reviewable proposal. Executes nothing.

    ``answer_parent`` and ``answers`` (compile mode only) answer the
    decisions an earlier intake of the same plan asked: they are checked
    (:func:`answered_decisions`) before any provider turn, given to the
    agent, written to this intake's ``decisions.json`` and rendered into the
    affected briefs. The answered intake itself is never written.

    ``validate(interpretation, findings)`` runs after the turn and before
    anything is written; whatever it raises refuses with nothing persisted.

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

    decisions: dict[str, Any] | None = None
    if answer_parent is not None:
        if mode != MODE_COMPILE:
            raise IntakeError("only a compile prepare answers decisions")
        decisions = answered_decisions(answer_parent, dict(answers or {}), text)
    elif answers:
        raise IntakeError("answers need the intake that asked them")
    answered = list(decisions["answers"]) if decisions else []

    prompt = assemble_intake_prompt(
        plan_label=label,
        source_text=text,
        mode=mode,
        project_context=project_context,
        primary_repository=primary_repository,
        repo_root=repo_root.resolve(),
        context_repositories={name: Path(path).resolve() for name, path in context_repositories.items()},
        extra_notes=[_decisions_note(decisions)] if decisions else (),
    )

    named = [(primary_repository, repo_root), *sorted(context_repositories.items())]
    watched = [Path(path) for _, path in named]
    before = [_fingerprint(path) for path in watched]
    try:
        result = adapter.start_structured(prompt, interpretation_schema(mode))
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

    interpretation = parse_interpretation(result.text, mode=mode)
    compile_context = (
        compile_context_for(
            interpretation,
            source,
            project_context=project_context,
            primary_repository=primary_repository,
            inspected=snapshots,
        )
        if mode == MODE_COMPILE
        else None
    )
    findings = all_findings(interpretation, source, mode=mode, compile_context=compile_context)
    if validate is not None:
        validate(interpretation, findings)

    run_keys = {run.id: mint_run_key(label) for run in interpretation.runs}
    briefs = _render_briefs(source, interpretation, run_keys, decisions=answered)
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

    decisions_text = json.dumps(decisions, indent=2, ensure_ascii=False) + "\n" if decisions else None
    if decisions_text is not None:
        _write(directory / DECISIONS_FILENAME, decisions_text)

    amendment = None
    if mode != MODE_COMPILE and interpretation.amended_plan is not None and interpretation.amended_plan != text:
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
        # Compile mode only: what its repository guards were checked
        # against. Sealed with intake.json; approval recomputes from it.
        **({"compile": compile_context.as_dict()} if compile_context is not None else {}),
        # The recorded decision inputs: sealed with intake.json, and the
        # digest is re-checked at approval and before every run.
        **(
            {
                "decisions": {
                    "file": DECISIONS_FILENAME,
                    "digest": sha256_text(decisions_text),
                    "parent_intake_id": decisions["parent"]["intake_id"],
                    "parent_interpretation_digest": decisions["parent"]["interpretation_digest"],
                    "answers": {entry["id"]: entry["option"]["id"] for entry in answered},
                }
            }
            if decisions is not None and decisions_text is not None
            else {}
        ),
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
        decisions=answered,
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
    decisions: Sequence[Mapping[str, Any]] = (),
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
                decisions=decisions,
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


def _manifest_gate_lines(interpretation: Interpretation, run: RunSlice) -> list[str]:
    before, completion = slice_manifest_gates(interpretation, run.id)
    lines = [
        f"- The run stops before stage {label} for: {', '.join('`' + g.id + '`' for g in gates)}"
        for label, gates in before.items()
    ]
    if completion:
        lines.append(
            "- The run stops before reporting complete for: "
            + ", ".join("`" + g.id + "`" for g in completion)
        )
    return lines


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
    decisions: Sequence[Mapping[str, Any]] = (),
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

    if decisions:
        out += ["", "## Preparation decisions", ""]
        out.append(
            "A person's answers to earlier intakes' questions (`decisions.json`). They render "
            "into the affected briefs as preparation decisions; the source plan is not edited."
        )
        for answer in decisions:
            option = answer["option"]
            stages = ", ".join(answer.get("stages") or []) or "every stage"
            out.append(
                f"- `{answer['id']}` (asked by `{answer['posed_by']}`; {stages}): {answer['question']} "
                f"→ `{option['id']}` {option['label']}: {option['consequence']}"
            )

    out += ["", "## Findings", ""]
    if not findings:
        out.append("None.")
    for f in findings:
        where = []
        if f.stages:
            where.append("stages " + ", ".join(s for s in f.stages if s))
        if f.ranges:
            where.append("lines " + _ranges_text(f.ranges))
        if f.transform:
            where.append(f"transform `{f.transform}`")
        suffix = f" ({'; '.join(where)})" if where else ""
        label = f"{f.disposition}** ({f.severity})" if f.disposition else f"{f.severity}**"
        out.append(f"- **{label} `{f.code}` [{f.origin}]{suffix}: {f.message}")
        if f.downgraded:
            out.append(f"  - Engine: auto-resolution rejected, so a person decides: {f.downgraded}")
        if f.decision is not None:
            out.append(f"  - Decision `{f.decision.id}`: {f.decision.question} {f.decision.why}".rstrip())
            for option in f.decision.options:
                out.append(f"    - `{option.id}` {option.label}: {option.consequence}")

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
            *_manifest_gate_lines(interpretation, run),
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

    if mode != MODE_COMPILE:  # compile mode never proposes one (amendment_in_compile_mode)
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


def _manifest_gate(gate: Gate) -> dict[str, str]:
    return {"id": gate.id, "title": gate.title or gate.id, "kind": gate.kind, "reason": gate.reason or gate.title or gate.id}


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

    return seal_approval(
        build_approval(
            intake_dir,
            run_id=run_id,
            repo_root=repo_root,
            sparring_dir=sparring_dir,
            primary_repository=primary_repository,
            expected_branch=expected_branch,
            repositories=repositories,
            repository_branches=repository_branches,
            confirmed_prerequisites=confirmed_prerequisites,
            without_amendment=without_amendment,
        ),
        now=now,
    )


@dataclass(frozen=True)
class PendingApproval:
    """What :func:`seal_approval` would write for one run slice, decided by
    :func:`build_approval` from the verified intake and the repositories as
    they are now. Holding one writes nothing."""

    intake_dir: Path
    run_id: str
    run_key: str
    expected_branch: str
    manifest_path: Path
    approval_path: Path
    manifest_text: str
    manifest_digest: str
    #: The idempotence decision an existing approval must agree with.
    decision: Mapping[str, Any]
    sparring_dir: Path
    #: ``approval.json`` without its ``approved_at``; ``None`` when an
    #: identical approval already exists, which sealing returns.
    payload: Mapping[str, Any] | None
    #: The parsed intake record and interpretation it was decided on.
    record: Mapping[str, Any]
    interpretation: "Interpretation"
    stages: tuple[Mapping[str, Any], ...]

    def result(self, created: bool) -> Approval:
        return Approval(
            manifest_path=self.manifest_path,
            approval_path=self.approval_path,
            run_key=self.run_key,
            expected_branch=self.expected_branch,
            manifest_digest=self.manifest_digest,
            created=created,
        )


def _existing_approval(pending: PendingApproval) -> Approval:
    """The approval already on disk for this slice, if it is the one
    ``pending`` would write, or refuse: an approval is never rewritten."""

    run_id, manifest_path = pending.run_id, pending.manifest_path
    try:
        previous = read_approval(pending.approval_path)
    except IntakeApprovalError as exc:
        raise IntakeError(f"run slice {run_id!r} has an approval that cannot be used: {exc}") from exc
    decision = dict(pending.decision)
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
    if on_disk != pending.manifest_text.encode("utf-8"):
        raise IntakeError(
            f"run slice {run_id!r} was already approved with a different manifest "
            f"({manifest_path}); an approval is not rewritten. Run prepare-plan again"
        )
    return pending.result(created=False)


def seal_approval(
    pending: PendingApproval,
    *,
    now: Callable[[], datetime] = _utc_now,
    approved_via: str | None = None,
) -> Approval:
    """Write what :func:`build_approval` decided: the manifest envelope, the
    registry entry and, exactly once, ``approval.json``.

    ``approved_via`` names the command a person approved through (recorded
    as ``approved_via`` when given). It is not part of the idempotence
    decision, so the same slice approved by ``approve-plan`` and confirmed
    by ``start-plan`` is one approval, not a conflict.
    """

    if pending.payload is None:
        return _existing_approval(pending)
    approval = {
        "version": APPROVAL_VERSION,
        "decision": "approved",
        "approved_at": now().isoformat(),
        **pending.payload,
        **({"approved_via": approved_via} if approved_via else {}),
    }
    # The manifest may be replaced until an approval exists for it; the
    # approval is created exactly once, and it is what makes the manifest
    # runnable. A crash between the two leaves an unapproved manifest that
    # run-plan refuses and a repeat of this approval completes.
    write_atomic(pending.manifest_path, pending.manifest_text)
    registry = intake_root(pending.sparring_dir) / REGISTRY_DIRNAME / f"{pending.run_key}.json"
    write_atomic(
        registry,
        json.dumps(
            {
                "run_key": pending.run_key,
                "intake_dir": str(pending.intake_dir.resolve()),
                "run_id": pending.run_id,
                "stage_ids": approval["stage_ids"],
            },
            indent=2,
        )
        + "\n",
    )
    if not write_exclusive(pending.approval_path, json.dumps(approval, indent=2) + "\n"):
        # Another approval won the race; it decides, and this one must agree.
        return _existing_approval(pending)
    return pending.result(created=True)


def build_approval(
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
) -> PendingApproval:
    """Every check :func:`approve_plan` makes, and the approval it would
    write, without writing anything. Refuses exactly as it does."""

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
    interpretation = parse_interpretation(interpretation_text, mode=mode)
    compile_context = CompileContext.from_record(record) if mode == MODE_COMPILE else None
    findings = all_findings(interpretation, source, mode=mode, compile_context=compile_context)
    # Derived from the interpretation itself, never taken from intake.json.
    amendment_proposed = (
        mode != MODE_COMPILE
        and interpretation.amended_plan is not None
        and interpretation.amended_plan != source.text
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
    decisions = read_decisions(intake_dir, record)
    briefs = _render_briefs(source, interpretation, run_keys, include_goal=include_goal, decisions=decisions)

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
        decisions=decisions,
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
    gates_before, completion_gates = slice_manifest_gates(interpretation, run_id)
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
        if gates_before.get(stage.label):
            entry["gates_before"] = [_manifest_gate(gate) for gate in gates_before[stage.label]]
        stages.append(entry)

    # v1 whenever no gate is carried, so such a manifest (and its digest) is
    # exactly what it was before v2 existed.
    gated = bool(gates_before or completion_gates)
    manifest_payload: dict[str, Any] = {
        "version": MANIFEST_VERSION_GATES if gated else MANIFEST_VERSION,
        "plan_label": source.label,
        "source_digest": f"sha256:{source.digest}",
        "stages": stages,
    }
    if completion_gates:
        manifest_payload["completion_gates"] = [_manifest_gate(gate) for gate in completion_gates]
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
    pending = PendingApproval(
        intake_dir=intake_dir,
        run_id=run_id,
        run_key=run_key,
        expected_branch=branch,
        manifest_path=manifest_path,
        approval_path=approval_path,
        manifest_text=manifest_text,
        manifest_digest=digest,
        decision=decision,
        sparring_dir=Path(sparring_dir),
        payload=None,
        record=record,
        interpretation=interpretation,
        stages=tuple(stages),
    )

    if approval_path.exists():
        _existing_approval(pending)
        return pending

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
    payload = {
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
    return replace(pending, payload=payload)


__all__ = [
    "AGENT_FINDING_CODES",
    "Approval",
    "COMPILE_INTERPRETATION_SCHEMA",
    "CompileContext",
    "DISPOSITIONS",
    "Decision",
    "DecisionOption",
    "Finding",
    "INTAKE_DIRNAME",
    "INTERPRETATION_SCHEMA",
    "IntakeError",
    "IntakeResult",
    "Interpretation",
    "MODES",
    "SourcePlan",
    "TRANSFORMS",
    "all_findings",
    "PendingApproval",
    "answered_decisions",
    "approve_plan",
    "build_approval",
    "posed_decisions",
    "read_decisions",
    "recorded_answers",
    "seal_approval",
    "check_interpretation",
    "compile_context_for",
    "guard_compile_findings",
    "interpretation_schema",
    "intake_not_ignored_message",
    "parse_interpretation",
    "prepare_plan",
    "render_brief",
    "repository_snapshot",
    "require_intake_ignored",
    "run_prerequisites",
    "slice_manifest_gates",
    "feature_branch_needed",
    "slice_branch",
    "slice_runs_agent",
    "split_prerequisites",
]
