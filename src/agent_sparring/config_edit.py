"""The one writer of ``.sparring/project.toml``.

``project.toml`` is a human-owned file. Managed runs read it and never write
it, and the agents taking part in a run are instructed not to touch it (see
:mod:`agent_sparring.artifact_ownership`). This module is the single
exception: a person, through ``sparring set-config`` or a UI that shells out
to it, changing one of a small set of typed fields.

Three properties make that safe enough to be worth having:

*Typed, not generic.* There is no "set an arbitrary TOML key to an arbitrary
value" operation. The role is one of :data:`~agent_sparring.agent_config.ROLES`
and the fields are ``provider``, ``model`` and ``effort`` -- the same closed
schema :mod:`agent_sparring.config` parses. A caller cannot reach
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

Nothing here silently discards a value. Changing a role's provider to one
that cannot honour the model or effort already configured is an error naming
both, not a quiet reset: the caller can then decide, and set the provider and
the effort together in one invocation if that is what they meant.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, replace
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
    ProjectConfig,
    ProjectConfigError,
    parse_project_config,
)
from agent_sparring.templates import render_project_config

# The fields of an [agents.<role>] table this module may write. Deliberately
# the same closed set config.py parses, named here so the two cannot drift
# without a test noticing.
EDITABLE_FIELDS: tuple[str, ...] = ("provider", "model", "effort")


@dataclass(frozen=True)
class RoleEdit:
    """One requested change to one role.

    Each field is three-valued. ``None`` means "leave it alone"; a string
    means "set it to this"; the matching ``clear_*`` flag means "remove the
    project override, so the provider's own default applies again".

    Setting and clearing the same field is a contradiction and is refused
    rather than resolved in some order the caller did not choose.
    """

    provider: str | None = None
    model: str | None = None
    clear_model: bool = False
    effort: str | None = None
    clear_effort: bool = False

    def __post_init__(self) -> None:
        if self.model is not None and self.clear_model:
            raise AgentConfigError("cannot set a model and clear the model override at once")
        if self.effort is not None and self.clear_effort:
            raise AgentConfigError("cannot set an effort and clear the effort override at once")
        for name in ("provider", "model", "effort"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                # An empty string is how a UI most plausibly expresses "use
                # the default", and writing `model = ""` would produce a file
                # config.py then refuses to parse. Say which operation was
                # meant instead of guessing.
                raise AgentConfigError(
                    f"{name} cannot be empty; to use the provider's own default, clear the "
                    f"override instead of setting it to an empty value"
                )

    @property
    def is_empty(self) -> bool:
        return not (
            self.provider or self.model or self.clear_model or self.effort or self.clear_effort
        )


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
    if edit.model is not None:
        table["model"] = edit.model
    elif edit.clear_model:
        table.pop("model", None)
    if edit.effort is not None:
        table["effort"] = edit.effort
    elif edit.clear_effort:
        table.pop("effort", None)


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


def _explain(
    exc: AgentConfigError, role: str, edit: RoleEdit, config: ProjectConfig
) -> AgentConfigError:
    """The refusal, with a sentence about what to do when a provider change
    is what made an otherwise-fine setting unusable.

    Distinguishing the two cases is worth the extra resolution: "that
    provider does not do this role" and "that provider cannot honour the
    effort already in your file" call for completely different responses,
    and only the second one is fixed by a combined edit. The question is
    asked of the engine rather than guessed at from the message text.
    """

    if edit.provider is None:
        return exc
    without_overrides = replace(
        config,
        **{f"{role}_agent_model": None, f"{role}_agent_effort": None},
    )
    try:
        resolve_role_config(role, without_overrides)
    except AgentConfigError:
        # The new provider is unusable for this role no matter what else the
        # file says; the original message already explains that exactly.
        return exc
    return AgentConfigError(
        f"{exc}. Changing the {role} agent's provider to {edit.provider!r} would leave a value "
        f"already in project.toml unusable, and set-config will not discard it for you: set the "
        f"provider and that value together, or clear the override, in a single invocation"
    )


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
            f"nothing to change for the {role} agent; name at least one of "
            f"{', '.join(EDITABLE_FIELDS)} to set, or an override to clear"
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
    try:
        resolved = resolve_role_config(role, config)
    except AgentConfigError as exc:
        raise _explain(exc, role, edit, config) from exc

    if not created and candidate == original:
        # Already exactly this. Writing identical bytes would only churn the
        # mtime and re-trigger every watcher for no reason.
        return EditOutcome(path=path, changed=False, created=False, resolved=resolved)

    _atomic_write(path, candidate)
    return EditOutcome(path=path, changed=True, created=created, resolved=resolved)


__all__ = [
    "EDITABLE_FIELDS",
    "EditOutcome",
    "RoleEdit",
    "apply_role_edit",
]
