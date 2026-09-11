"""Minimal stage artifact model.

Conceptually, a project-local stage lives at::

    .sparring/stages/<stage-id>/
        brief.md
        notes.md
        state.json
        handoff.md
        sparring.md
        activity.jsonl   (observational only; see agent_sparring.activity)

This module resolves stage directories safely, creates a new stage skeleton,
and reads/writes the minimal machine state as JSON. It does not implement any
workflow engine, provider invocation, or acceptance logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agent_sparring import templates
from agent_sparring.activity import ActivityLog

STAGES_DIRNAME = "stages"
STATE_FILENAME = "state.json"
BRIEF_FILENAME = "brief.md"
NOTES_FILENAME = "notes.md"
HANDOFF_FILENAME = "handoff.md"
SPARRING_FILENAME = "sparring.md"
# Append-only observational telemetry. Never read by orchestration; not
# created by Stage.create (it appears on first emit, if writable).
ACTIVITY_FILENAME = "activity.jsonl"

_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StageError(ValueError):
    """Raised for invalid stage ids, missing stages, or malformed state."""


class StageStatus(str, Enum):
    """Minimal stage lifecycle. Deliberately small; see the project plan."""

    WORKING = "working"
    FROZEN = "frozen"  # a candidate is prepared for the later hard acceptance gate
    ACCEPTED = "accepted"

    @classmethod
    def from_str(cls, value: str) -> "StageStatus":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise StageError(
                f"unknown stage status {value!r}; expected one of: {valid}"
            ) from exc


def validate_stage_id(stage_id: str) -> str:
    """Validate a stage id is safe to use as a single path segment.

    Rejects empty ids, path separators, and traversal segments (``.``/``..``).
    """

    if not isinstance(stage_id, str) or not _STAGE_ID_RE.match(stage_id):
        raise StageError(
            f"invalid stage id {stage_id!r}: must match {_STAGE_ID_RE.pattern}"
        )
    if stage_id in {".", ".."}:
        raise StageError(f"invalid stage id {stage_id!r}")
    return stage_id


def _optional_str_field(payload: dict[str, Any], key: str) -> str | None:
    """A ``str | None`` field read from machine-ingested JSON.

    Raises :class:`StageError` for a present-but-wrong-typed value (e.g. a
    number where downstream code, such as git context gathering, requires a
    string) instead of silently coercing or misrepresenting it.
    """

    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise StageError(f"state.json field {key!r} must be a string or null, got {value!r}")
    return value


@dataclass
class StageState:
    """The smallest machine state needed by later stages.

    Stage identity is not duplicated here: it comes from the stage's
    directory (``.sparring/stages/<stage-id>/``), which ``Stage`` itself
    owns. No V1 workflow states (changes_requested, review_attempt,
    ancillary states, migration states, immutable intermediate verdicts) are
    represented here either. See the project plan's "Minimal state" section
    for the rationale.
    """

    status: StageStatus = StageStatus.WORKING
    implementation_session_id: str | None = None
    sparring_session_id: str | None = None
    base_sha: str | None = None
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StageState":
        status_raw = payload.get("status", StageStatus.WORKING.value)
        status = StageStatus.from_str(str(status_raw))
        return cls(
            status=status,
            implementation_session_id=_optional_str_field(
                payload, "implementation_session_id"
            ),
            sparring_session_id=_optional_str_field(payload, "sparring_session_id"),
            base_sha=_optional_str_field(payload, "base_sha"),
            candidate_sha=_optional_str_field(payload, "candidate_sha"),
        )


@dataclass
class Stage:
    """Handle onto one stage's directory and artifacts."""

    stage_id: str
    directory: Path

    # -- resolution -----------------------------------------------------

    @classmethod
    def resolve(cls, sparring_dir: Path, stage_id: str) -> "Stage":
        """Resolve (without requiring existence) the directory for a stage id.

        Raises :class:`StageError` if ``stage_id`` is unsafe.
        """

        validate_stage_id(stage_id)
        stages_root = Path(sparring_dir) / STAGES_DIRNAME
        directory = stages_root / stage_id

        # Defense in depth: even though validate_stage_id already rejects
        # separators and traversal segments, confirm the resolved path stays
        # within stages_root before any filesystem write.
        resolved_root = stages_root.resolve()
        resolved_dir = (stages_root / stage_id).resolve()
        if resolved_root not in resolved_dir.parents and resolved_dir != resolved_root:
            raise StageError(f"stage id {stage_id!r} escapes the stages directory")

        return cls(stage_id=stage_id, directory=directory)

    def exists(self) -> bool:
        return self.directory.is_dir()

    # -- skeleton creation ------------------------------------------------

    def create(self, *, exist_ok: bool = False) -> "Stage":
        """Create a new stage skeleton with initial state and templates.

        Raises :class:`StageError` if the stage already exists and
        ``exist_ok`` is False.
        """

        if self.exists() and not exist_ok:
            raise StageError(f"stage {self.stage_id!r} already exists at {self.directory}")

        self.directory.mkdir(parents=True, exist_ok=True)

        state_path = self.directory / STATE_FILENAME
        if not state_path.is_file():
            self.write_state(StageState())

        for filename, template in (
            (BRIEF_FILENAME, templates.BRIEF_TEMPLATE),
            (NOTES_FILENAME, templates.NOTES_TEMPLATE),
            (HANDOFF_FILENAME, templates.HANDOFF_TEMPLATE),
            (SPARRING_FILENAME, templates.SPARRING_TEMPLATE),
        ):
            path = self.directory / filename
            if not path.is_file():
                path.write_text(template.format(stage_id=self.stage_id), encoding="utf-8")

        return self

    # -- machine state ----------------------------------------------------

    def read_state(self) -> StageState:
        path = self.directory / STATE_FILENAME
        if not path.is_file():
            raise StageError(f"no state.json for stage {self.stage_id!r} at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StageError(f"malformed state.json at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StageError(f"state.json at {path} must contain a JSON object")
        return StageState.from_dict(payload)

    def write_state(self, state: StageState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / STATE_FILENAME
        path.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # -- observational activity stream -------------------------------------

    def activity_path(self) -> Path:
        return self.directory / ACTIVITY_FILENAME

    def activity_log(self) -> ActivityLog:
        """A write-only handle onto this stage's ``activity.jsonl``.

        Orchestration code opens the log through this method and only ever
        emits to it; there is no corresponding reader anywhere in the
        package (see :mod:`agent_sparring.activity`).
        """

        return ActivityLog(self.activity_path())

    # -- human-readable artifacts ------------------------------------------

    def _read_text(self, filename: str) -> str:
        path = self.directory / filename
        if not path.is_file():
            raise StageError(f"no {filename} for stage {self.stage_id!r} at {path}")
        return path.read_text(encoding="utf-8")

    def _write_text(self, filename: str, content: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / filename).write_text(content, encoding="utf-8")

    def read_brief(self) -> str:
        return self._read_text(BRIEF_FILENAME)

    def read_notes(self) -> str:
        return self._read_text(NOTES_FILENAME)

    def read_handoff(self) -> str:
        return self._read_text(HANDOFF_FILENAME)

    def read_sparring(self) -> str:
        return self._read_text(SPARRING_FILENAME)

    def write_notes(self, content: str) -> None:
        self._write_text(NOTES_FILENAME, content)

    def write_handoff(self, content: str) -> None:
        self._write_text(HANDOFF_FILENAME, content)

    def write_sparring(self, content: str) -> None:
        self._write_text(SPARRING_FILENAME, content)
