"""Adapters that turn one migration tool's on-disk/CLI conventions into the
small vocabulary the migration-status engine needs: a version out of a file
name, an ordering over versions, and the applied-remote-version list out of a
saved history capture.

Stage A never runs a subprocess against a database or a migration CLI (see
:mod:`agent_sparring.migration_status`): an adapter here only *parses text
already captured on disk* (:func:`MigrationAdapter.parse_history`). There is
deliberately no method that shells out to anything.

This module is generic infrastructure the way :mod:`agent_sparring.
git_context` is: it must not encode any Sporely-specific path, project name,
or rule. ``supabase`` is a third-party migration tool's own public file/CLI
conventions, not a Sporely concept.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol


class MigrationAdapterError(ValueError):
    """Raised when adapter input (a file name, a captured CLI table) cannot
    be parsed."""


class MigrationAdapter(Protocol):
    """The small interface the status engine needs from a migration tool.

    Three operations only, all pure and read-only: name a version, order two
    versions, and parse a previously captured history listing. Nothing here
    reads a filesystem, opens a database, or runs a command.
    """

    #: The value a project's ``[migrations].adapter`` names to select this
    #: adapter (see :data:`agent_sparring.config.MIGRATIONS_ADAPTERS`).
    id: str

    def version_of(self, path: str) -> str | None:
        """The migration version a file name encodes, or ``None`` if this
        adapter does not recognise ``path`` as a migration file at all (e.g.
        a stray ``README.md`` sitting in the migrations directory). The
        status engine reports every such file rather than dropping it."""

    def order_key(self, version: str):
        """A key such that ``sorted(versions, key=adapter.order_key)`` is
        deployment order, oldest first."""

    def parse_history(self, text: str) -> list[str]:
        """The applied *remote* versions recorded in a captured history
        listing, in the order they appear. Raises
        :class:`MigrationAdapterError` if ``text`` is not recognisable
        output for this adapter."""


# ``supabase migration list`` prints a table such as::
#
#     Local          | Remote         | Time (UTC)
#   ------------------|----------------|---------------------
#     20260925160000 | 20260925160000 | 2026-09-25 16:00:00
#     20260929120000 |                |
#                     | 20260930181144 |
#
# A row with only a ``Local`` value is pending (not yet applied); a row with
# only ``Remote`` is applied on the target but absent from this checkout's
# migration files (the ``remote_only`` case). Both are real, valid rows, not
# malformed input.
_HEADER_RE = re.compile(r"^\s*Local\s*\|\s*Remote\s*\|")
_SEPARATOR_RE = re.compile(r"^\s*-+\s*\|")
# The Supabase CLI's own rule (``migrateFilePattern`` in the CLI's
# ``pkg/migration/file.go``): any all-ASCII-digit version, an underscore, any
# name (even empty), ``.sql``. ``supabase migration new`` always writes a
# 14-digit UTC timestamp, but the CLI applies whatever matches this, so
# anything narrower here would silently ignore a file the CLI deploys.
_VERSION_CELL_RE = re.compile(r"^[0-9]+$")
_FILENAME_RE = re.compile(r"^([0-9]+)_(.*)\.sql$")


class SupabaseMigrationAdapter:
    """Parses Supabase CLI migration-file names and ``migration list`` output.

    A version is the all-digit prefix of ``<digits>_name.sql`` -- normally a
    14-digit UTC timestamp, but the CLI accepts any digit string.
    :meth:`order_key` is the version itself, compared as a plain string,
    because that is the order ``supabase db push`` uses: local files in file
    name order, remote versions from a text ``ORDER BY version``, compared
    with string comparison when deciding what is pending or out of order.
    For same-width timestamps this is also numeric order.

    One CLI rule is not mirrored: the CLI skips a *first* file named
    ``<14 digits>_init.sql`` older than ``20211209000000`` (a deprecated
    layout from old CLI versions); this adapter counts it as a migration.
    """

    id = "supabase"

    def version_of(self, path: str) -> str | None:
        match = _FILENAME_RE.match(Path(path).name)
        return match.group(1) if match else None

    def order_key(self, version: str) -> str:
        return version

    def parse_history(self, text: str) -> list[str]:
        lines = text.splitlines()
        header = next((i for i, line in enumerate(lines) if _HEADER_RE.match(line)), None)
        if header is None:
            raise MigrationAdapterError(
                "this does not look like 'supabase migration list' output: no "
                "'Local | Remote' table header found"
            )

        applied: list[str] = []
        saw_row = False
        for line in lines[header + 1 :]:
            if "|" not in line or _SEPARATOR_RE.match(line):
                continue
            cells = line.split("|")
            if len(cells) < 2:
                continue
            local, remote = cells[0].strip(), cells[1].strip()
            if not local and not remote:
                continue
            for cell, name in ((local, "Local"), (remote, "Remote")):
                if cell and not _VERSION_CELL_RE.match(cell):
                    raise MigrationAdapterError(
                        f"unrecognised {name} value {cell!r} in migration list row: {line.strip()!r}"
                    )
            saw_row = True
            if remote:
                applied.append(remote)
        if not saw_row:
            raise MigrationAdapterError(
                "'supabase migration list' output has a table header but no migration rows"
            )
        return applied


_ADAPTERS: dict[str, MigrationAdapter] = {"supabase": SupabaseMigrationAdapter()}


def get_adapter(adapter_id: str) -> MigrationAdapter:
    """The adapter registered for ``adapter_id``.

    ``[migrations].adapter`` is already validated as a closed set at config
    load time (see :mod:`agent_sparring.config`); this raises the same error
    type so a caller that somehow reaches this with an unvalidated id still
    fails clearly rather than with a bare ``KeyError``.
    """

    try:
        return _ADAPTERS[adapter_id]
    except KeyError:
        raise MigrationAdapterError(
            f"unknown migration adapter {adapter_id!r}; supported: {sorted(_ADAPTERS)}"
        ) from None


__all__ = [
    "MigrationAdapter",
    "MigrationAdapterError",
    "SupabaseMigrationAdapter",
    "get_adapter",
]
