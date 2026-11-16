"""A run's trusted configuration snapshots, one per participating repository.

Every engine Git command of a run is held to the security-relevant
configuration each repository had when the run trusted it (I-L4; see
:func:`agent_sparring.engine_git.trusted_config`). Local-first runs take
these snapshots locally -- the primary repository's when the run starts,
each declared sibling's when the run starts or, for a sibling that was not a
repository yet, when its stage is first entered -- and record them in the
run state (``config_snapshot``, ``sibling_snapshots``). A run recorded
before then carries them in its legacy pins.

A snapshot that should be there and is not -- removed, damaged, or replaced
with a legacy-shaped state -- refuses (:data:`TRUSTED_CONFIG_MISSING`)
rather than letting the operation run unguarded. What "should be there" is
the engine's own record outside the project directory
(:func:`agent_sparring.managed_run.mark_snapshot_recorded`), written before
the run state that holds the snapshots, so deleting them from the run state
cannot make a run look as if it never had them.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from agent_sparring import engine_git
from agent_sparring.engine_git import GitContextError

TRUSTED_CONFIG_MISSING = "trusted_config_missing"

Snapshot = tuple[tuple[str, str], ...]


class SnapshotError(ValueError):
    """Recorded snapshots that cannot be read as written."""


def resolved(path: Path | str) -> str:
    return str(Path(path).resolve())


def parse_entries(value: Any, where: str) -> Snapshot:
    if not isinstance(value, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair)
        for pair in value
    ):
        raise SnapshotError(f"{where} must be an array of [key, value]")
    return tuple((pair[0], pair[1]) for pair in value)


@dataclass(frozen=True)
class SiblingSnapshot:
    """One declared sibling repository's trusted snapshot, by its resolved
    path."""

    path: str
    entries: Snapshot

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "entries": [list(pair) for pair in self.entries]}

    @classmethod
    def from_dict(cls, payload: Any) -> "SiblingSnapshot":
        if not isinstance(payload, Mapping) or not isinstance(payload.get("path"), str):
            raise SnapshotError("a sibling snapshot needs a path and entries")
        return cls(path=payload["path"], entries=parse_entries(payload.get("entries"), "entries"))


def sibling_snapshots_from(payload: Any) -> tuple[SiblingSnapshot, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, (list, tuple)):
        raise SnapshotError("sibling_snapshots must be an array")
    return tuple(SiblingSnapshot.from_dict(entry) for entry in payload)


@dataclass(frozen=True)
class RunSnapshots:
    """The snapshots a run's state holds: the primary's (``None`` when there
    is none to recover) and each sibling's by resolved path."""

    primary: Snapshot | None
    siblings: Mapping[str, Snapshot] = field(default_factory=dict)

    def for_sibling(self, root: Path) -> Snapshot | None:
        return self.siblings.get(resolved(root))


def from_run_state(payload: Mapping[str, Any]) -> RunSnapshots:
    """The snapshots recorded in a run state payload: the run's own, else a
    legacy run's pins. A malformed local-first snapshot raises
    :class:`SnapshotError`; a legacy pin that cannot be read gives no
    snapshot (and so, where one is required, a refusal)."""

    from agent_sparring.remote_pin import RemotePin, SiblingRemotePin

    raw = payload.get("config_snapshot")
    primary: Snapshot | None = None
    if raw is not None:
        primary = parse_entries(raw, "config_snapshot")
    elif payload.get("remote_pin"):
        try:
            primary = RemotePin.from_dict(payload["remote_pin"]).trusted_config
        except (GitContextError, KeyError, TypeError, ValueError):
            primary = None
    siblings: dict[str, Snapshot] = {}
    for entry in payload.get("sibling_pins") or ():
        try:
            pin = SiblingRemotePin.from_dict(entry)
        except (GitContextError, KeyError, TypeError, ValueError):
            continue
        siblings[resolved(pin.path)] = pin.pin.trusted_config
    for entry in sibling_snapshots_from(payload.get("sibling_snapshots")):
        siblings[resolved(entry.path)] = entry.entries
    return RunSnapshots(primary=primary, siblings=siblings)


def capture(root: Path) -> Snapshot:
    """The repository's own security-relevant configuration, read locally."""

    return engine_git.capture_snapshot(Path(root))


def missing_message(
    repo_root: Path, run: str, *, what: str, consequence: str = "Nothing was frozen or accepted"
) -> str:
    """Why an operation of run ``run`` refuses for want of ``what``'s
    trusted snapshot: it was recorded and is gone, or the run predates
    local snapshots and a resume records one."""

    recorded = engine_recorded(repo_root, run)
    if recorded:
        cause = (
            "the engine's record says it was taken, but the run state no longer holds it "
            "(removed, damaged or replaced); it is not taken again from the current "
            "configuration"
        )
    else:
        cause = (
            "the run holds none; resume the run (resume-plan), which records one from the "
            "current configuration with a visible notice, before freezing or accepting"
        )
    return (
        f"refusing: {TRUSTED_CONFIG_MISSING}: run {run} has no trusted configuration snapshot "
        f"for {what}: {cause}. {consequence}"
    )


def engine_recorded(repo_root: Path, run: str) -> bool:
    """Does an engine record outside the project directory say ``run``'s
    trusted snapshot was taken (its local-first snapshot record, or a
    legacy run's pin record)? An unreadable record says yes."""

    from agent_sparring.managed_run import (
        ManagedRunError,
        engine_recorded_snapshot,
        engine_says_pinned,
    )

    try:
        return engine_recorded_snapshot(Path(repo_root), run) or engine_says_pinned(Path(repo_root), run)
    except (ManagedRunError, OSError):
        return True


@contextlib.contextmanager
def guard(repo_root: Path, snapshots: RunSnapshots) -> Iterator[None]:
    """Hold engine Git invocations in the primary repository and in each
    sibling with a snapshot to that repository's own snapshot."""

    with contextlib.ExitStack() as stack:
        if snapshots.primary is not None:
            stack.enter_context(engine_git.trusted_config([Path(repo_root)], snapshots.primary))
        for path, entries in snapshots.siblings.items():
            stack.enter_context(engine_git.trusted_config([Path(path)], entries))
        yield


def sibling_paths(entries: Sequence[SiblingSnapshot]) -> tuple[str, ...]:
    return tuple(entry.path for entry in entries)


__all__ = [
    "TRUSTED_CONFIG_MISSING",
    "RunSnapshots",
    "SiblingSnapshot",
    "SnapshotError",
    "capture",
    "engine_recorded",
    "from_run_state",
    "guard",
    "missing_message",
    "parse_entries",
    "resolved",
    "sibling_snapshots_from",
]
