"""The one writer of ``.sparring/project.toml``.

``project.toml`` is a human-owned file. Managed runs read it and never write
it, and the agents taking part in a run are instructed not to touch it (see
:mod:`agent_sparring.artifact_ownership`). This module is the single
exception: a person, through ``sparring set-config`` or a UI that shells out
to it, changing one of a small set of typed fields.

Three properties make that safe enough to be worth having:

*Typed, not generic.* There is no "set an arbitrary TOML key to an arbitrary
value" operation. The role is one of :data:`~agent_sparring.agent_config.ROLES`
and the one editable field is ``provider``. Model and effort are not project
settings (they are user preferences, :mod:`agent_sparring.user_config`); the
only thing this module does with them is remove obsolete copies
(:func:`remove_obsolete_agent_settings`). A caller cannot reach
``[repo].root``, cannot add a key the parser would later reject, and cannot
express anything that ends up in a provider's argv beyond what the adapters
already validate.

*Validated before written, not after.* The edit is applied to an in-memory
document, the result is re-parsed and put through
:func:`~agent_sparring.agent_config.resolve_role_config` -- the very
resolution a run will perform -- and only a configuration that would actually
launch is allowed to reach the disk. A rejected edit leaves the file byte-for-
byte as it was. In particular a malformed existing file is refused rather
than replaced with a well-formed one, because "your file was broken so I
threw it away" is not a repair.

*Surgical, not regenerated.* tomlkit round-trips the document, so the file
keeps its comments, its key order and its author's formatting, and a mutation
changes the line it was asked to change. An edit that would produce the bytes
already on disk writes nothing at all, which also makes a duplicate request
-- a double-clicked control, a repeated command -- harmless.

"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table

from agent_sparring.agent_config import (
    AgentConfigError,
    ROLES,
    ResolvedAgentConfig,
    resolve_role_config,
)
from agent_sparring.config import (
    CONFIG_FILENAME,
    ObsoleteAgentSetting,
    ProjectConfig,
    ProjectConfigError,
    parse_project_config,
)
from agent_sparring.templates import render_project_config

# The fields of an [agents.<role>] table this module may write.
EDITABLE_FIELDS: tuple[str, ...] = ("provider",)


@dataclass(frozen=True)
class RoleEdit:
    """One requested change to one role's project configuration: its provider."""

    provider: str | None = None

    def __post_init__(self) -> None:
        if self.provider is not None and not self.provider.strip():
            raise AgentConfigError("provider cannot be empty")

    @property
    def is_empty(self) -> bool:
        return not self.provider


@dataclass(frozen=True)
class EditOutcome:
    """What an accepted edit did.

    ``changed`` is false when the requested state was already the state on
    disk; the file was then not rewritten at all. ``created`` is true when
    the project had no ``project.toml`` and the engine's own template was
    used as the starting point.
    """

    path: Path
    changed: bool
    created: bool
    resolved: ResolvedAgentConfig


def _document(path: Path, project: str) -> tuple[tomlkit.TOMLDocument, str, bool]:
    """The document to edit, its original text, and whether it is new.

    A file that exists but cannot be parsed raises: it is someone's work,
    and replacing it with a fresh template would destroy whatever they were
    in the middle of writing.
    """

    if not path.is_file():
        text = render_project_config(project)
        return tomlkit.parse(text), text, True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectConfigError(f"could not read {path}: {exc}") from exc
    try:
        return tomlkit.parse(text), text, False
    except TOMLKitError as exc:
        raise ProjectConfigError(
            f"{path} is not valid TOML: {exc}. Fix the file by hand; set-config will not "
            f"overwrite a file it cannot read"
        ) from exc


def _role_table(document: tomlkit.TOMLDocument, role: str) -> MutableMapping[str, object]:
    """The ``[agents.<role>]`` table, created empty if the file has none.

    Typed as a mapping rather than :class:`~tomlkit.items.Table` because
    tomlkit hands back a proxy for a table whose keys are written
    out of order; both support the item assignment and ``pop`` used here.
    """

    agents = document.get("agents")
    if agents is None:
        # A super table renders as "[agents.stage]" rather than an empty
        # "[agents]" header followed by it, which is how a person writes it.
        agents = tomlkit.table(True)
        document["agents"] = agents
    if not isinstance(agents, (Table, dict)):
        raise ProjectConfigError("project.toml field 'agents' must be a table")
    role_table = agents.get(role)
    if role_table is None:
        role_table = tomlkit.table()
        agents[role] = role_table
    if not isinstance(role_table, (Table, dict)):
        raise ProjectConfigError(f"project.toml field 'agents.{role}' must be a table")
    return role_table


def _apply(table: MutableMapping[str, object], edit: RoleEdit) -> None:
    """Write the requested fields into ``table``. Order is irrelevant: the
    fields are independent, and the combination is validated afterwards."""

    if edit.provider is not None:
        table["provider"] = edit.provider


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or not at all.

    The temporary file is created in the destination's own directory so the
    final rename is a same-filesystem :func:`os.replace`, which is atomic:
    a reader sees either the whole old file or the whole new one, and a
    failure part-way through leaves the original untouched.
    """

    temp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Creating the temporary file is itself a write to the destination's
        # directory and can fail for exactly the reasons the rename can, so
        # it belongs inside this handler rather than ahead of it.
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp = Path(handle.name)
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
        raise ProjectConfigError(f"could not write {path}: {exc}") from exc


def apply_role_edit(sparring_dir: Path, role: str, edit: RoleEdit) -> EditOutcome:
    """Apply ``edit`` to ``role`` in ``sparring_dir``'s ``project.toml``.

    Raises :class:`~agent_sparring.config.ProjectConfigError` (of which
    :class:`~agent_sparring.agent_config.AgentConfigError` is a subclass)
    without writing anything if the existing file cannot be parsed, if the
    result would not parse, or if the resulting role configuration is one no
    provider turn could actually run with.
    """

    if role not in ROLES:
        raise AgentConfigError(f"unknown agent role {role!r}; known roles: {', '.join(ROLES)}")
    if edit.is_empty:
        raise AgentConfigError(
            f"nothing to change in project.toml for the {role} agent; the only project "
            f"setting is its provider"
        )

    path = sparring_dir / CONFIG_FILENAME
    project = sparring_dir.resolve().parent.name
    document, original, created = _document(path, project)

    if not created:
        # The file as it stands must already be a configuration this engine
        # understands. Editing one field of a file with an unknown key would
        # otherwise "succeed" and leave behind something no run can load.
        parse_project_config(original, source=str(path))

    _apply(_role_table(document, role), edit)
    candidate = tomlkit.dumps(document)

    # Re-read the rendered bytes rather than trusting the in-memory edit,
    # and resolve the role exactly as a run would. Everything a provider
    # would refuse -- an unknown provider, a provider not implemented for
    # this role, an effort that provider has no concept of or does not
    # accept -- is refused here, with the file still untouched.
    config = parse_project_config(candidate, source=str(path))
    resolved = resolve_role_config(role, config)

    if not created and candidate == original:
        # Already exactly this. Writing identical bytes would only churn the
        # mtime and re-trigger every watcher for no reason.
        return EditOutcome(path=path, changed=False, created=False, resolved=resolved)

    _atomic_write(path, candidate)
    return EditOutcome(path=path, changed=True, created=created, resolved=resolved)


def remove_obsolete_agent_settings(sparring_dir: Path) -> tuple[Path, list[ObsoleteAgentSetting]]:
    """Remove ``model``/``effort`` from every ``[agents.<role>]`` table.

    Surgical like every other edit here: the provider, every other table,
    comments and formatting are kept, and an ``[agents.<role>]`` table left
    empty is kept too (it is the person's structure, not the engine's). The
    removed values are returned so they can be reported; they are never
    copied into the user's preferences -- which repository's old values should
    win is not something the engine can decide. Idempotent: with nothing to
    remove, nothing is written.
    """

    path = sparring_dir / CONFIG_FILENAME
    if not path.is_file():
        return path, []
    document, original, _ = _document(path, sparring_dir.resolve().parent.name)
    config = parse_project_config(original, source=str(path))
    if not config.obsolete_agent_settings:
        return path, []
    agents = document.get("agents")
    for setting in config.obsolete_agent_settings:
        agents[setting.role].pop(setting.field, None)  # type: ignore[index,union-attr]
    candidate = tomlkit.dumps(document)
    remaining = parse_project_config(candidate, source=str(path))
    if remaining.obsolete_agent_settings:  # pragma: no cover - defensive
        raise ProjectConfigError(f"could not remove obsolete agent settings from {path}")
    _atomic_write(path, candidate)
    return path, list(config.obsolete_agent_settings)


__all__ = [
    "EDITABLE_FIELDS",
    "EditOutcome",
    "RoleEdit",
    "apply_role_edit",
    "remove_obsolete_agent_settings",
]
