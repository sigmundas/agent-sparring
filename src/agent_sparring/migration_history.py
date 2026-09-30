"""Versioned, saved migration-history snapshots.

Stage A never probes a database or a migration CLI live (see
:mod:`agent_sparring.migration_status`): everything it knows about what is
actually applied on a target comes from an explicit snapshot recorded here
from a person's own captured output (``sparring record-migration-history
--file <captured output>``). Recording is deliberately just that: this module
parses the captured text into a list of applied versions and writes it down
with when it was observed; it never interprets what the drift means (that is
:mod:`agent_sparring.migration_status`'s job) and never talks to anything but
the local filesystem and ``git`` (to find where to put the file).

Snapshots live under ``<git-common-dir>/agent-sparring/migrations/history/``,
outside the tracked tree (like the stage/plan/intake workflow-state
directories -- see :mod:`agent_sparring.setup_check`) and shared by every
worktree of one clone, since "what production actually has" is a fact about
the repository, not about which worktree happens to be checked out.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.git_context import GitContextError, repository_identity
from agent_sparring.migration_adapters import MigrationAdapterError, get_adapter

HISTORY_VERSION = 1
SOURCE_RECORDED = "recorded"

HISTORY_SUBDIR = Path("agent-sparring") / "migrations" / "history"

#: How far an ``observed_at`` may lie ahead of this machine's clock and still
#: be accepted -- room for a capture taken on another machine whose clock
#: runs slightly fast, and nothing more. A snapshot further in the future
#: would win latest-snapshot selection over every later recording and have
#: a negative age that never goes stale, so it is refused.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

_SNAPSHOT_KEYS = frozenset(
    {"version", "adapter", "target_ref", "observed_at", "source", "raw_sha256", "applied", "head"}
)


class MigrationHistoryError(ValueError):
    """Raised when a history snapshot cannot be recorded or read."""


@dataclass(frozen=True)
class HistorySnapshot:
    """One recorded observation of a target's applied migration history."""

    adapter: str
    target_ref: str | None
    observed_at: str
    raw_sha256: str
    applied: tuple[str, ...]
    head: str | None
    source: str = SOURCE_RECORDED
    version: int = HISTORY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "adapter": self.adapter,
            "target_ref": self.target_ref,
            "observed_at": self.observed_at,
            "source": self.source,
            "raw_sha256": self.raw_sha256,
            "applied": list(self.applied),
            "head": self.head,
        }


def now_observed_at() -> str:
    """The current time as this module's ``observed_at`` timestamps are
    written: UTC, second precision, ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_observed_at(value: str) -> datetime:
    """Parse an ``observed_at`` timestamp, requiring an explicit timezone.

    A timestamp without one (``2026-10-01T10:00:00``) is refused: it names
    no single moment, and comparing it with the current (aware) time is
    exactly what ``check-migrations`` has to do to compute a snapshot's age.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationHistoryError(f"observed_at {value!r} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MigrationHistoryError(
            f"observed_at {value!r} has no timezone; give one explicitly "
            "(e.g. '2026-10-01T10:00:00Z' or '2026-10-01T12:00:00+02:00')"
        )
    return parsed


def check_not_future(observed_dt: datetime, *, now: datetime, what: str) -> None:
    """Refuse an observation more than :data:`CLOCK_SKEW_TOLERANCE` ahead of
    ``now``."""

    if observed_dt > now + CLOCK_SKEW_TOLERANCE:
        tolerance = int(CLOCK_SKEW_TOLERANCE.total_seconds() // 60)
        raise MigrationHistoryError(
            f"{what} is in the future (observed_at {observed_dt.isoformat()} is after "
            f"now, {now.isoformat(timespec='seconds')}, by more than the {tolerance}-minute "
            "clock-skew tolerance)"
        )


def snapshot_from_dict(payload: Mapping[str, Any], *, source: str) -> HistorySnapshot:
    if not isinstance(payload, Mapping):
        raise MigrationHistoryError(f"{source} must be a JSON object")
    unknown = sorted(set(payload) - _SNAPSHOT_KEYS)
    if unknown:
        raise MigrationHistoryError(f"{source} carries unknown field(s) {unknown}")
    version = payload.get("version")
    if version != HISTORY_VERSION:
        raise MigrationHistoryError(
            f"{source} has version {version!r}; this engine reads version {HISTORY_VERSION}"
        )
    adapter = payload.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        raise MigrationHistoryError(f"{source} field 'adapter' must be a non-empty string")
    observed_at = payload.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise MigrationHistoryError(f"{source} field 'observed_at' must be a non-empty string")
    try:
        parse_observed_at(observed_at)
    except MigrationHistoryError as exc:
        raise MigrationHistoryError(f"{source} field 'observed_at': {exc}") from exc
    raw_sha256 = payload.get("raw_sha256")
    if not isinstance(raw_sha256, str) or not raw_sha256:
        raise MigrationHistoryError(f"{source} field 'raw_sha256' must be a non-empty string")
    applied = payload.get("applied")
    if not isinstance(applied, list) or not all(isinstance(v, str) for v in applied):
        raise MigrationHistoryError(f"{source} field 'applied' must be an array of strings")
    target_ref = payload.get("target_ref")
    if target_ref is not None and not isinstance(target_ref, str):
        raise MigrationHistoryError(f"{source} field 'target_ref' must be a string or null")
    head = payload.get("head")
    if head is not None and not isinstance(head, str):
        raise MigrationHistoryError(f"{source} field 'head' must be a string or null")
    source_field = payload.get("source", SOURCE_RECORDED)
    if source_field != SOURCE_RECORDED:
        raise MigrationHistoryError(f"{source} field 'source' must be {SOURCE_RECORDED!r}")

    return HistorySnapshot(
        adapter=adapter,
        target_ref=target_ref,
        observed_at=observed_at,
        raw_sha256=raw_sha256,
        applied=tuple(applied),
        head=head,
        source=SOURCE_RECORDED,
        version=HISTORY_VERSION,
    )


def _git_common_dir(repo_root: Path) -> Path:
    """The repository's ``git-common-dir``, or a clear refusal.

    There is deliberately no fallback (a temp directory, the repo root
    itself): a snapshot recorded somewhere other than the repository it
    describes would be silently unreachable next time, and "no usable git
    common dir" is a small, checkable precondition -- refusing it plainly is
    better than guessing a location.
    """

    try:
        _, common = repository_identity(repo_root)
    except GitContextError as exc:
        raise MigrationHistoryError(
            f"{repo_root} has no usable git common directory ({exc}); "
            "history snapshots are stored under it and refuse to guess another location"
        ) from exc
    return Path(common)


def history_dir(repo_root: Path) -> Path:
    """Where this repository's history snapshots live."""

    return _git_common_dir(repo_root) / HISTORY_SUBDIR


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def record_history_snapshot(
    repo_root: Path,
    *,
    adapter_id: str,
    raw_text: str,
    target_ref: str | None,
    observed_at: str | None = None,
    now: datetime | None = None,
) -> tuple[HistorySnapshot, Path]:
    """Parse ``raw_text`` with the named adapter and write a new snapshot file.

    Raises :class:`MigrationHistoryError` if ``raw_text`` is not parseable by
    that adapter, if ``observed_at`` has no timezone or lies in the future
    beyond :data:`CLOCK_SKEW_TOLERANCE`, or if there is nowhere to record it (see
    :func:`_git_common_dir`). Record, don't interpret: this never compares the
    result against any other snapshot or declares anything stale.
    """

    try:
        adapter = get_adapter(adapter_id)
        applied = adapter.parse_history(raw_text)
    except MigrationAdapterError as exc:
        raise MigrationHistoryError(f"could not parse the captured migration history: {exc}") from exc

    observed = observed_at or now_observed_at()
    observed_dt = parse_observed_at(observed)  # validates the caller-supplied timestamp too
    check_not_future(
        observed_dt, now=now or datetime.now(timezone.utc), what="the migration history observation"
    )
    head = max(applied, key=adapter.order_key) if applied else None
    raw_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    snapshot = HistorySnapshot(
        adapter=adapter_id,
        target_ref=target_ref,
        observed_at=observed,
        raw_sha256=raw_sha256,
        applied=tuple(applied),
        head=head,
    )

    directory = history_dir(repo_root)
    # The file name's timestamp is always this filesystem-safe UTC rendering,
    # even when a caller supplied ``observed_at`` in another valid ISO-8601
    # form (an offset other than Z, say); the recorded ``observed_at`` field
    # itself keeps exactly what was given.
    ts_name = observed_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{ts_name}-{raw_sha256[:12]}.json"
    _atomic_write(path, json.dumps(snapshot.as_dict(), indent=2, sort_keys=True) + "\n")
    return snapshot, path


def latest_snapshot(repo_root: Path, *, now: datetime | None = None) -> HistorySnapshot | None:
    """The most recently observed snapshot, or ``None`` if none is recorded.

    "No usable git common dir" is reported as "no snapshot" here (there is
    nowhere one could be), rather than raised -- a read-only report should
    say ``production_history_unknown``, not refuse to run, for a repository
    that simply has not been configured for this yet. A snapshot file that
    exists but cannot be read or validated raises
    :class:`MigrationHistoryError` naming it -- including one observed in
    the future beyond :data:`CLOCK_SKEW_TOLERANCE`, which would otherwise be
    selected over every later recording forever.
    """

    moment = now or datetime.now(timezone.utc)

    try:
        directory = history_dir(repo_root)
    except MigrationHistoryError:
        return None
    if not directory.is_dir():
        return None

    best: HistorySnapshot | None = None
    best_dt: datetime | None = None
    for path in sorted(directory.glob("*.json")):
        # A snapshot this module itself wrote always parses. One that does
        # not (damaged, hand-edited, or written before a validation existed)
        # is refused by name rather than skipped: skipping it could silently
        # fall back to an older observation, and "which snapshot is latest"
        # cannot be answered while one of them cannot be read.
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationHistoryError(
                f"history snapshot {path} could not be read ({exc}); fix or remove it"
            ) from exc
        try:
            snapshot = snapshot_from_dict(payload, source=f"history snapshot {path}")
        except MigrationHistoryError as exc:
            raise MigrationHistoryError(f"{exc}; fix or remove it") from exc
        observed_dt = parse_observed_at(snapshot.observed_at)
        try:
            check_not_future(observed_dt, now=moment, what=f"history snapshot {path}")
        except MigrationHistoryError as exc:
            raise MigrationHistoryError(f"{exc}; fix or remove it") from exc
        if best_dt is None or observed_dt > best_dt:
            best, best_dt = snapshot, observed_dt
    return best


__all__ = [
    "CLOCK_SKEW_TOLERANCE",
    "HISTORY_VERSION",
    "SOURCE_RECORDED",
    "HistorySnapshot",
    "MigrationHistoryError",
    "check_not_future",
    "history_dir",
    "latest_snapshot",
    "now_observed_at",
    "parse_observed_at",
    "record_history_snapshot",
    "snapshot_from_dict",
]
