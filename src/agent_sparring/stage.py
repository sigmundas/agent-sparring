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

# The notes.md heading under which a human's answer/check result/evidence is
# recorded (by `resume-plan --evidence`, or by hand). Prose only: the handoff
# and the stage prompt surface this section verbatim to the agents; nothing
# parses or gates on its content.
HUMAN_EVIDENCE_HEADING = "## Human evidence"

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


@dataclass(frozen=True)
class CandidateRepository:
    """One *sibling* repository whose reviewed candidate belongs to a stage.

    A stage's candidate normally lives in exactly one repository -- the
    ``repo_root`` every other operation is made against -- and nothing here
    is needed. Some stages are genuinely cross-repository: the reviewed work
    is a desktop change in the primary repository *and* a coupled change in
    another one, and the sparrer's READY verdict depends on both. Pinning
    only the primary SHA would let the sibling move between review and
    acceptance, so acceptance would claim a candidate set that no longer
    exists.

    This is deliberately the smallest representation that closes that hole:
    identity, the branch the candidate must be on, and the exact commit.
    There is no cross-repository merge, no transaction, no dependency graph
    and no remote coordination -- :mod:`agent_sparring.acceptance` simply
    resolves each declared sibling at freeze time and re-verifies it at
    acceptance time, exactly as it already does for the primary repository.

    ``path`` is resolved against the primary ``repo_root`` when relative.
    ``candidate_sha`` is ``None`` while the sibling is only *declared* (it is
    whatever the sibling's branch is at); ``freeze_candidate`` fills it in
    with the resolved HEAD, and from then on it is the pinned candidate. A
    declaration that already carries a ``candidate_sha`` is an assertion:
    freezing refuses if the sibling is not exactly there.
    """

    name: str
    path: str
    branch: str
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "branch": self.branch,
            "candidate_sha": self.candidate_sha,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CandidateRepository":
        if not isinstance(payload, dict):
            raise StageError(f"a repository entry must be a JSON object, got {payload!r}")
        for key in ("name", "path", "branch"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise StageError(f"repository entry field {key!r} must be a non-empty string")
        sha = payload.get("candidate_sha")
        if sha is not None and not isinstance(sha, str):
            raise StageError("repository entry field 'candidate_sha' must be a string or null")
        return cls(
            name=str(payload["name"]),
            path=str(payload["path"]),
            branch=str(payload["branch"]),
            candidate_sha=sha,
        )


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
    # Sibling repositories whose reviewed candidates are part of this stage.
    # Empty for the ordinary single-repository stage, and absent from
    # state.json in that case, so existing files are unchanged.
    repositories: tuple[CandidateRepository, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        if self.repositories:
            payload["repositories"] = [repo.to_dict() for repo in self.repositories]
        else:
            payload.pop("repositories", None)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StageState":
        status_raw = payload.get("status", StageStatus.WORKING.value)
        status = StageStatus.from_str(str(status_raw))
        raw_repositories = payload.get("repositories")
        if raw_repositories is None:
            repositories: tuple[CandidateRepository, ...] = ()
        elif isinstance(raw_repositories, list):
            repositories = tuple(CandidateRepository.from_dict(entry) for entry in raw_repositories)
        else:
            raise StageError("state.json field 'repositories' must be a list or null")
        return cls(
            status=status,
            implementation_session_id=_optional_str_field(
                payload, "implementation_session_id"
            ),
            sparring_session_id=_optional_str_field(payload, "sparring_session_id"),
            base_sha=_optional_str_field(payload, "base_sha"),
            candidate_sha=_optional_str_field(payload, "candidate_sha"),
            repositories=repositories,
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

    def create(self, *, exist_ok: bool = False, brief: str | None = None) -> "Stage":
        """Create a new stage skeleton with initial state and templates.

        ``brief`` is the initial ``brief.md`` content, written verbatim in
        place of the template. Like the template it is only written when
        ``brief.md`` does not exist yet: an existing brief (``exist_ok``) is
        never overwritten. The caller decides what the brief says; this
        method only owns *where* it goes and *when* it is written.

        Raises :class:`StageError` if the stage already exists and
        ``exist_ok`` is False.
        """

        if self.exists() and not exist_ok:
            raise StageError(f"stage {self.stage_id!r} already exists at {self.directory}")

        self.directory.mkdir(parents=True, exist_ok=True)

        state_path = self.directory / STATE_FILENAME
        if not state_path.is_file():
            self.write_state(StageState())

        initial_brief = (
            brief if brief is not None else templates.BRIEF_TEMPLATE.format(stage_id=self.stage_id)
        )
        for filename, content in (
            (BRIEF_FILENAME, initial_brief),
            (NOTES_FILENAME, templates.NOTES_TEMPLATE.format(stage_id=self.stage_id)),
            (HANDOFF_FILENAME, templates.HANDOFF_TEMPLATE.format(stage_id=self.stage_id)),
            (SPARRING_FILENAME, templates.SPARRING_TEMPLATE.format(stage_id=self.stage_id)),
        ):
            path = self.directory / filename
            if not path.is_file():
                path.write_text(content, encoding="utf-8")

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

    def write_brief(self, content: str) -> None:
        self._write_text(BRIEF_FILENAME, content)

    def write_notes(self, content: str) -> None:
        self._write_text(NOTES_FILENAME, content)

    def write_handoff(self, content: str) -> None:
        self._write_text(HANDOFF_FILENAME, content)

    def write_sparring(self, content: str) -> None:
        self._write_text(SPARRING_FILENAME, content)
