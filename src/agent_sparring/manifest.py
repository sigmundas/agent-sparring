"""Execution manifest v1: a deterministic plan input for the plan runner.

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
          "repositories": [
            {"name": "sporely-web",
             "path": "../sporely-web-reported-statistics",
             "branch": "feature/reported-statistics-cloud-transport",
             "candidate_sha": null}
          ]
        }
      ]
    }

It carries stage identity, display label and title, the exact brief, and the
order. It carries **no** status, position, transition, verdict or session:
those are the engine's, and a manifest that tried to hold them would be a
second workflow engine. ``stages`` order is the execution order -- labels are
never parsed to derive one, so numeric ``1..N`` is not required here.

``source_digest`` is opaque provenance: the engine never recomputes it (it
does not know how the caller digested the document), but it is part of this
manifest's own digest, so re-emitting a manifest from an edited plan changes
the digest and a recorded run refuses to continue against it. That is the
same protection the Markdown path gets from its stage-section digest.

Unknown keys are refused rather than ignored, so a manifest written against
a later version of this contract fails loudly instead of being half-read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.plan_model import PlannedStage, digest_planned_stages
from agent_sparring.stage import CandidateRepository, StageError, validate_stage_id

MANIFEST_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"version", "plan_label", "source_digest", "stages"})
_STAGE_KEYS = frozenset({"stage_id", "label", "title", "brief", "repositories"})
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


def parse_manifest(text: str) -> ExecutionManifest:
    """Parse and fully validate manifest JSON, or raise :class:`ManifestError`."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ManifestError("a manifest must be a JSON object")
    _reject_unknown(payload, _TOP_LEVEL_KEYS, "manifest")

    version = payload.get("version")
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"unsupported manifest version {version!r}; this engine reads version "
            f"{MANIFEST_VERSION}"
        )
    plan_label = _text(payload, "plan_label", "manifest")
    source_digest = _text(payload, "source_digest", "manifest")

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ManifestError("a manifest must declare a non-empty 'stages' array")

    stages: list[PlannedStage] = []
    for position, entry in enumerate(raw_stages, start=1):
        stages.append(_stage(entry, position))

    ids = [stage.stage_id for stage in stages]
    duplicates = sorted({stage_id for stage_id in ids if ids.count(stage_id) > 1})
    if duplicates:
        raise ManifestError(f"manifest stage ids must be unique; repeated: {duplicates}")

    return ExecutionManifest(
        plan_label=plan_label,
        source_digest=source_digest,
        stages=tuple(stages),
        version=MANIFEST_VERSION,
    )


def _stage(entry: Any, position: int) -> PlannedStage:
    where = f"manifest stage {position}"
    if not isinstance(entry, Mapping):
        raise ManifestError(f"{where} must be a JSON object")
    _reject_unknown(entry, _STAGE_KEYS, where)

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
    )


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
    stage id, label, title, brief, and each declared repository. Anything
    that changes what would run changes this digest, and a recorded run
    refuses to continue against a different one.
    """

    parts: list[str] = [str(manifest.version), manifest.plan_label, manifest.source_digest]
    for stage in manifest.stages:
        parts += [stage.stage_id, stage.label, stage.title, stage.brief]
        for repository in stage.repositories:
            parts += [
                repository.name,
                repository.path,
                repository.branch,
                repository.candidate_sha or "",
            ]
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
    "MANIFEST_VERSION",
    "ExecutionManifest",
    "ManifestError",
    "ManifestPlanSource",
    "load_manifest_source",
    "manifest_digest",
    "parse_manifest",
]
