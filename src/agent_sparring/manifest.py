"""Execution manifest v1/v2: a deterministic plan input for the plan runner.

A reviewed plan is a human document. Deciding which of its headings are
canonical stages, how labels like ``3A``/``3B``/``3C`` order, which sections
are historical handoffs that define nothing, and exactly what text briefs
each stage is *interpretation*, and it belongs to whoever owns the document
(today: the VS Code extension). This module is the contract that
interpretation is handed over in.

A manifest is deliberately small and explicit::

    {
      "version": 1,
      "plan_label": "docs/plans/active/reported-statistics.md",
      "source_digest": "<sha-256 of the plan document as it was read>",
      "stages": [
        {
          "stage_id": "stage-3c-cloud-schema-rpc-and-sync-transport",
          "label": "Stage 3C",
          "title": "Cloud schema/RPC and sync transport",
          "brief": "... the exact brief.md content ...",
          "mode": "implementation",
          "repositories": [
            {"name": "sporely-web",
             "path": "../sporely-web-reported-statistics",
             "branch": "feature/reported-statistics-cloud-transport",
             "candidate_sha": null}
          ]
        }
      ]
    }

It carries stage identity, display label and title, the exact brief, the
stage's mode, and the order. It carries **no** status, position, transition,
verdict or session: those are the engine's, and a manifest that tried to
hold them would be a second workflow engine. ``stages`` order is the
execution order -- labels are never parsed to derive one, so numeric
``1..N`` is not required here.

``mode``
--------

``mode`` is optional and defaults to ``"implementation"``, so every manifest
written before it existed means exactly what it always meant. The other
value is ``"independent_review"``: a stage that runs *no* implementation
agent, only a fresh independent reviewer over the candidate set the
preceding stages already accepted (see :class:`~agent_sparring.stage.
StageMode` and :mod:`agent_sparring.review`).

It is deliberately a declaration, not a description. The engine never
derives a stage's mode from its title, its brief's prose, or its position in
the plan -- a plan whose last stage is called "Independent final review and
activation decision" still runs the implementation lifecycle unless the
manifest says ``"mode": "independent_review"`` for it. Which agent runs is
too consequential to hang on a word in a heading, and the caller that
interprets the plan document is the one that knows.

``source_digest`` is opaque provenance: the engine never recomputes it (it
does not know how the caller digested the document), but it is part of this
manifest's own digest, so re-emitting a manifest from an edited plan changes
the digest and a recorded run refuses to continue against it. That is the
same protection the Markdown path gets from its stage-section digest.

Unknown keys are refused rather than ignored, so a manifest written against
a later version of this contract fails loudly instead of being half-read.

Briefs, and what "the plan says" means for a stage already under way
--------------------------------------------------------------------

``brief`` is the exact ``brief.md`` content the engine will write for a
stage it creates, and the exact content it requires an existing, started
stage to already hold (see :func:`agent_sparring.plan._check_adoption`). For
a stage that has real execution history those are not the same source: its
``brief.md`` is the contract the work was actually implemented and reviewed
against, while the plan section it came from is a living document that is
often rewritten afterwards to record what was built. So a caller emitting an
*adoption* manifest is expected to carry an already-executed stage's
existing ``brief.md`` verbatim, and to brief only the stages that do not
exist yet from the plan's current section. One manifest then describes both
the preserved history and the future execution, and adopting a sequence
never requires deleting a stage or rolling the plan document back.

Version 2: plan-declared gates
------------------------------

v2 is v1 plus two fields, and nothing else:

- per stage, ``gates_before: [{id, title, kind, reason}]`` -- gates owed
  after the preceding stage is accepted and before this one is created;
- top level, ``completion_gates`` (same shape) -- gates owed after the last
  stage is accepted and before the run may report COMPLETE.

A ``version: 2`` manifest must use at least one of them (a manifest that
needs neither is written as v1), and a v1 manifest carrying either is
refused as an unknown field. Gate ids are unique across the whole manifest.
The v2 digest covers every gate field and its position; v1 digests are
computed exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.plan_model import ManifestGate, PlannedStage, digest_planned_stages
from agent_sparring.stage import (
    CandidateRepository,
    StageError,
    StageMode,
    validate_stage_id,
)

MANIFEST_VERSION = 1
#: The version that adds plan-declared gates (see the module docstring).
MANIFEST_VERSION_GATES = 2
MANIFEST_VERSIONS = (MANIFEST_VERSION, MANIFEST_VERSION_GATES)

_TOP_LEVEL_KEYS = frozenset({"version", "plan_label", "source_digest", "stages"})
_TOP_LEVEL_KEYS_V2 = _TOP_LEVEL_KEYS | {"completion_gates"}
#: The top-level key of the envelope plan intake wraps an approved manifest
#: in (see :mod:`agent_sparring.intake_approval`). Named here only so the
#: plain reader can refuse one by name instead of as an unknown key.
INTAKE_ENVELOPE_KEY = "intake_manifest"
_STAGE_KEYS = frozenset({"stage_id", "label", "title", "brief", "mode", "repositories"})
_STAGE_KEYS_V2 = _STAGE_KEYS | {"gates_before"}
_GATE_KEYS = frozenset({"id", "title", "kind", "reason"})
#: A gate id becomes the id of the check a person answers, so it obeys the
#: check-id length bound.
_MAX_GATE_ID_LENGTH = 128
_REPOSITORY_KEYS = frozenset({"name", "path", "branch", "candidate_sha"})


class ManifestError(ValueError):
    """A manifest that cannot be executed as written."""


@dataclass(frozen=True)
class ExecutionManifest:
    """A parsed, validated manifest."""

    plan_label: str
    source_digest: str
    stages: tuple[PlannedStage, ...]
    version: int = MANIFEST_VERSION
    #: Gates owed before the run may report COMPLETE (v2 only).
    completion_gates: tuple[ManifestGate, ...] = ()


def parse_manifest(text: str) -> ExecutionManifest:
    """Parse and fully validate manifest JSON, or raise :class:`ManifestError`."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if isinstance(payload, Mapping) and INTAKE_ENVELOPE_KEY in payload:
        raise ManifestError(
            "this is an intake-approved manifest, which runs only with its approval "
            "(sparring run-plan --manifest <path>), never as a plain manifest"
        )
    return manifest_from_payload(payload)


def manifest_from_payload(payload: Any) -> ExecutionManifest:
    """:func:`parse_manifest` for an already-decoded JSON value."""

    if not isinstance(payload, Mapping):
        raise ManifestError("a manifest must be a JSON object")
    version = payload.get("version")
    if isinstance(version, bool) or version not in MANIFEST_VERSIONS:
        raise ManifestError(
            f"unsupported manifest version {version!r}; this engine reads versions "
            f"{', '.join(map(str, MANIFEST_VERSIONS))}"
        )
    gated = version == MANIFEST_VERSION_GATES
    _reject_unknown(payload, _TOP_LEVEL_KEYS_V2 if gated else _TOP_LEVEL_KEYS, "manifest")
    plan_label = _text(payload, "plan_label", "manifest")
    source_digest = _text(payload, "source_digest", "manifest")

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ManifestError("a manifest must declare a non-empty 'stages' array")

    stages: list[PlannedStage] = []
    for position, entry in enumerate(raw_stages, start=1):
        stages.append(_stage(entry, position, gated=gated))

    ids = [stage.stage_id for stage in stages]
    duplicates = sorted({stage_id for stage_id in ids if ids.count(stage_id) > 1})
    if duplicates:
        raise ManifestError(f"manifest stage ids must be unique; repeated: {duplicates}")

    completion_gates = _gates(payload.get("completion_gates"), "manifest completion_gates") if gated else ()
    if gated:
        gate_ids = [gate.id for stage in stages for gate in stage.gates_before]
        gate_ids += [gate.id for gate in completion_gates]
        if not gate_ids:
            raise ManifestError(
                "a version 2 manifest must declare at least one gate (gates_before or "
                "completion_gates); a manifest without gates is written as version 1"
            )
        repeated = sorted({gate_id for gate_id in gate_ids if gate_ids.count(gate_id) > 1})
        if repeated:
            raise ManifestError(f"manifest gate ids must be unique; repeated: {repeated}")

    return ExecutionManifest(
        plan_label=plan_label,
        source_digest=source_digest,
        stages=tuple(stages),
        version=int(version),
        completion_gates=completion_gates,
    )


def _stage(entry: Any, position: int, *, gated: bool = False) -> PlannedStage:
    where = f"manifest stage {position}"
    if not isinstance(entry, Mapping):
        raise ManifestError(f"{where} must be a JSON object")
    _reject_unknown(entry, _STAGE_KEYS_V2 if gated else _STAGE_KEYS, where)

    stage_id = _text(entry, "stage_id", where)
    try:
        validate_stage_id(stage_id)
    except StageError as exc:
        raise ManifestError(f"{where}: {exc}") from exc

    brief = entry.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        raise ManifestError(
            f"{where} ({stage_id}) must carry a non-empty 'brief'; a stage with nothing "
            "to implement from is not executable"
        )

    return PlannedStage(
        position=position,
        label=_text(entry, "label", where),
        title=_text(entry, "title", where),
        stage_id=stage_id,
        brief=brief,
        repositories=_repositories(entry.get("repositories"), where),
        mode=_mode(entry.get("mode"), f"{where} ({stage_id})"),
        gates_before=_gates(entry.get("gates_before"), f"{where} ({stage_id}) gates_before"),
    )


def _gates(raw: Any, where: str) -> tuple[ManifestGate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{where} must be an array or null")
    gates: list[ManifestGate] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: each gate must be a JSON object")
        _reject_unknown(entry, _GATE_KEYS, f"{where} gate")
        gate = ManifestGate(
            id=_text(entry, "id", f"{where} gate"),
            title=_text(entry, "title", f"{where} gate"),
            kind=_text(entry, "kind", f"{where} gate"),
            reason=_text(entry, "reason", f"{where} gate"),
        )
        if len(gate.id) > _MAX_GATE_ID_LENGTH:
            raise ManifestError(f"{where}: gate id is longer than {_MAX_GATE_ID_LENGTH} characters")
        gates.append(gate)
    return tuple(gates)


def _gate_parts(gates: tuple[ManifestGate, ...], marker: str) -> list[str]:
    parts = [marker, str(len(gates))]
    for gate in gates:
        parts += [gate.id, gate.title, gate.kind, gate.reason]
    return parts


def _mode(raw: Any, where: str) -> StageMode:
    """The stage's declared mode, defaulting to the implementation lifecycle.

    Absent or null means :attr:`~agent_sparring.stage.StageMode.
    IMPLEMENTATION`, which is what every manifest written before modes
    existed means and is why adding this field changed no existing run. A
    value this engine does not recognise is refused rather than defaulted:
    the whole point of an explicit mode is that which agent runs is never
    guessed, and quietly running the implementation lifecycle for a stage
    whose manifest asked for something else would be the exact accident
    this field exists to rule out.
    """

    if raw is None:
        return StageMode.IMPLEMENTATION
    if not isinstance(raw, str):
        raise ManifestError(f"{where}: 'mode' must be a string or null, got {raw!r}")
    try:
        return StageMode.from_str(raw.strip())
    except StageError as exc:
        raise ManifestError(f"{where}: {exc}") from exc


def _repositories(raw: Any, where: str) -> tuple[CandidateRepository, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{where}: 'repositories' must be an array or null")
    repositories: list[CandidateRepository] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: each repository entry must be a JSON object")
        _reject_unknown(entry, _REPOSITORY_KEYS, f"{where} repository")
        try:
            repositories.append(CandidateRepository.from_dict(dict(entry)))
        except StageError as exc:
            raise ManifestError(f"{where}: {exc}") from exc
    names = [repository.name for repository in repositories]
    if len(set(names)) != len(names):
        raise ManifestError(f"{where}: repository names must be unique, got {names}")
    return tuple(repositories)


def _text(payload: Mapping[str, Any], key: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} field {key!r} must be a non-empty string")
    return value.strip()


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ManifestError(
            f"{where} carries unknown field(s) {unknown}; refusing to half-read a manifest "
            "written against a different contract"
        )


def manifest_digest(manifest: ExecutionManifest) -> str:
    """SHA-256 over everything this manifest actually executes.

    Version, plan label and source digest, then, per stage in order: the
    stage id, label, title, brief, a non-default mode, and each declared
    repository. Anything that changes what would run changes this digest,
    and a recorded run refuses to continue against a different one.

    A stage in the default :attr:`~agent_sparring.stage.StageMode.
    IMPLEMENTATION` mode contributes nothing for its mode, which is not a
    softening of the guard but the precise statement of it: the digest
    carries exactly what would change execution, and a manifest written
    before modes existed executes the implementation lifecycle either way,
    so it must digest to the same value and its recorded run must keep
    resuming. Declaring a stage ``independent_review`` *does* change the
    digest, in both directions -- so a stage cannot be flipped between the
    two lifecycles under a run that is already under way.

    A v2 manifest additionally digests each stage's ``gates_before`` (marked
    and counted, so a gate cannot move between stages or into
    ``completion_gates`` without changing the digest) and the completion
    gates. A v1 manifest contributes neither, so its digest is unchanged.
    """

    parts: list[str] = [str(manifest.version), manifest.plan_label, manifest.source_digest]
    for stage in manifest.stages:
        parts += [stage.stage_id, stage.label, stage.title, stage.brief]
        if stage.mode is not StageMode.IMPLEMENTATION:
            parts.append(stage.mode.value)
        for repository in stage.repositories:
            parts += [
                repository.name,
                repository.path,
                repository.branch,
                repository.candidate_sha or "",
            ]
        if manifest.version == MANIFEST_VERSION_GATES:
            parts += _gate_parts(stage.gates_before, "gates_before")
    if manifest.version == MANIFEST_VERSION_GATES:
        parts += _gate_parts(manifest.completion_gates, "completion_gates")
    return digest_planned_stages(*parts)


@dataclass(frozen=True)
class ManifestPlanSource:
    """A :class:`~agent_sparring.plan_model.PlanSource` backed by a manifest file."""

    path: Path
    manifest: ExecutionManifest
    kind: str = "manifest"

    @property
    def label(self) -> str:
        return self.manifest.plan_label

    def digest(self) -> str:
        return manifest_digest(self.manifest)

    def stages(self) -> tuple[PlannedStage, ...]:
        return self.manifest.stages

    @property
    def completion_gates(self) -> tuple[ManifestGate, ...]:
        return self.manifest.completion_gates

    def reload(self) -> "ManifestPlanSource":
        return load_manifest_source(self.path)

    def describe(self) -> str:
        return f"manifest {self.path}"


def load_manifest_source(path: Path) -> ManifestPlanSource:
    """Read and validate a manifest file into a plan source."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    return ManifestPlanSource(path=Path(path), manifest=parse_manifest(text))


__all__ = [
    "INTAKE_ENVELOPE_KEY",
    "MANIFEST_VERSION",
    "MANIFEST_VERSION_GATES",
    "MANIFEST_VERSIONS",
    "ExecutionManifest",
    "ManifestError",
    "ManifestPlanSource",
    "load_manifest_source",
    "manifest_digest",
    "manifest_from_payload",
    "parse_manifest",
]
