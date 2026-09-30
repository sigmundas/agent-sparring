"""Read a deferred-migration registry: the record of migrations that are
committed but deliberately *not* applied to a target yet, with the exact file
and hash pinned so the gap cannot be silently widened.

This module only reads the file's documented fields
(``deferredMigrations[].{version,file,sha256,reason}``); it is intentionally
lenient about anything else in the file (an informational
``productionProjectRef``, a ``doc`` pointer on each entry) since this format
is owned by the project that writes it, not by this engine -- adding a field
there must not break every existing project's ``sparring check-migrations``.
What *is* validated strictly is the shape of the four fields this module
actually uses, because a malformed one of those would silently misclassify a
migration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class DeferredRegistryError(ValueError):
    """Raised when a deferred-migration registry file cannot be read."""


@dataclass(frozen=True)
class DeferredEntry:
    """One deliberately-deferred migration."""

    version: str
    file: str
    sha256: str
    reason: str


@dataclass(frozen=True)
class DeferredRegistry:
    """A parsed deferred-migration registry."""

    entries: tuple[DeferredEntry, ...]
    source: str

    def by_version(self) -> dict[str, DeferredEntry]:
        return {entry.version: entry for entry in self.entries}


def _text_field(entry: Mapping[str, Any], key: str, *, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeferredRegistryError(f"{where} field {key!r} must be a non-empty string")
    return value


def parse_deferred_registry(text: str, *, source: str = "deferred registry") -> DeferredRegistry:
    """Parse deferred-registry JSON text into a :class:`DeferredRegistry`."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeferredRegistryError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise DeferredRegistryError(f"{source} must be a JSON object")

    raw_entries = payload.get("deferredMigrations", [])
    if not isinstance(raw_entries, list):
        raise DeferredRegistryError(f"{source} field 'deferredMigrations' must be an array")

    entries: list[DeferredEntry] = []
    for index, raw in enumerate(raw_entries):
        where = f"{source} deferredMigrations[{index}]"
        if not isinstance(raw, Mapping):
            raise DeferredRegistryError(f"{where} must be a JSON object")
        entries.append(
            DeferredEntry(
                version=_text_field(raw, "version", where=where),
                file=_text_field(raw, "file", where=where),
                sha256=_text_field(raw, "sha256", where=where).lower(),
                reason=_text_field(raw, "reason", where=where),
            )
        )

    versions = [entry.version for entry in entries]
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        raise DeferredRegistryError(f"{source} lists the same version more than once: {duplicates}")

    return DeferredRegistry(entries=tuple(entries), source=source)


def load_deferred_registry(path: Path) -> DeferredRegistry:
    """Read and parse a deferred-registry file from disk."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeferredRegistryError(f"could not read deferred registry {path}: {exc}") from exc
    return parse_deferred_registry(text, source=str(path))


def sha256_hex(content: bytes) -> str:
    """The lowercase hex SHA-256 of ``content``, in the same form the
    registry's own ``sha256`` field uses."""

    return hashlib.sha256(content).hexdigest()


__all__ = [
    "DeferredEntry",
    "DeferredRegistry",
    "DeferredRegistryError",
    "load_deferred_registry",
    "parse_deferred_registry",
    "sha256_hex",
]
