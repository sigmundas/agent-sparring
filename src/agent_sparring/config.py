"""Generic project configuration loading.

A project supplies two distinct kinds of configuration, per the project plan:

- ``.sparring/project.toml``: machine-readable configuration, parsed by this
  module into a small :class:`ProjectConfig`.
- ``.sparring/PROJECT.md``: agent-readable project knowledge, loaded as
  opaque text. This module never interprets its prose.

This module must not encode any application-specific (e.g. Sporely) names,
paths, tests, or rules. Everything here is generic.
"""

from __future__ import annotations

import posixpath
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

CONFIG_FILENAME = "project.toml"
CONTEXT_FILENAME = "PROJECT.md"


class ProjectConfigError(ValueError):
    """Raised when project.toml is missing required fields or malformed."""


@dataclass(frozen=True)
class ProjectConfig:
    """Minimal machine-readable project configuration.

    Only fields that generic code actually needs are represented here.
    """

    project: str
    repo_root: str = "."
    commands: Mapping[str, str] = field(default_factory=dict)
    stage_agent_provider: str | None = None
    sparring_agent_provider: str | None = None
    default_sparring_mode: str | None = None
    stage_self_check: bool = False
    # ``model``/``effort`` under ``[agents.<role>]`` from before they became
    # user preferences (see :mod:`agent_sparring.user_config`). Parsed only so
    # they can be reported as a setup problem and removed by ``fix-config``;
    # nothing resolves a provider turn from them.
    obsolete_agent_settings: tuple["ObsoleteAgentSetting", ...] = ()
    # ``[migrations]``: read-only migration-order detection (see
    # :mod:`agent_sparring.migration_status`). ``None`` when the project does
    # not opt in -- every migration command then refuses with a clear
    # "not configured" message rather than guessing a default.
    migrations: "MigrationsConfig | None" = None

    def command(self, name: str) -> str | None:
        """Return a configured project command by name, if any."""

        return self.commands.get(name)


@dataclass(frozen=True)
class ObsoleteAgentSetting:
    """One project-level ``model``/``effort`` key that no longer takes effect."""

    role: str
    field: str
    value: str


@dataclass(frozen=True)
class MigrationsConfig:
    """``[migrations]``: where a project's schema-migration history lives, and
    which saved snapshot/registry files describe it.

    Stage A is detection only: nothing here names a command to run, and
    ``adapter`` is deliberately a closed set of *parsers* (currently just
    ``"supabase"``, for its ``migration list`` table format and
    ``<digits>_name.sql`` naming), never a live probe. There is
    intentionally no ``probe``/command field to invoke a CLI or a database --
    such a key is rejected by the closed schema below like any other unknown
    field, exactly so it cannot be added by accident later without this
    module's own review.
    """

    adapter: str
    directory: str
    main_ref: str
    target: str = "production"
    target_ref: str | None = None
    deferred_registry: str | None = None
    max_observation_age_minutes: int = 60


def _require_str(table: Mapping[str, Any], key: str, *, where: str) -> str:
    if key not in table:
        raise ProjectConfigError(f"{where} is missing required field '{key}'")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(
            f"{where} field '{key}' must be a non-empty string"
        )
    return value


def _optional_str(table: Mapping[str, Any], key: str, *, where: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigError(
            f"{where} field '{key}' must be a non-empty string if present"
        )
    return value


def _optional_bool(table: Mapping[str, Any], key: str, *, where: str) -> bool | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise ProjectConfigError(f"{where} field '{key}' must be a boolean if present")
    return value


def _optional_int(table: Mapping[str, Any], key: str, *, where: str, default: int) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectConfigError(f"{where} field '{key}' must be an integer")
    return value


def _optional_table(
    table: Mapping[str, Any], key: str, *, where: str
) -> Mapping[str, Any]:
    if key not in table:
        return {}
    value = table[key]
    if not isinstance(value, Mapping):
        raise ProjectConfigError(f"{where} field '{key}' must be a table")
    return value


# Keys an [agents.<role>] table may still contain but that no longer take
# effect: model and effort are user preferences now. Accepted by the parser so
# the engine can diagnose and remove them, rather than refuse to load the file.
OBSOLETE_AGENT_KEYS: tuple[str, ...] = ("model", "effort")

# The complete schema of an [agents.<role>] table. Kept closed on purpose:
# a misspelled key here (``efort``, ``reasoning``) would otherwise be
# silently ignored and the run would quietly use the provider default,
# which is precisely the "engine claims support it does not have" failure
# this configuration is meant to avoid.
_AGENT_ROLE_KEYS: frozenset[str] = frozenset({"provider", "model", "effort"})


def _reject_unknown_agent_keys(table: Mapping[str, Any], *, where: str) -> None:
    unknown = sorted(set(table) - _AGENT_ROLE_KEYS)
    if not unknown:
        return
    known = ", ".join(sorted(_AGENT_ROLE_KEYS))
    raise ProjectConfigError(
        f"{where} has unknown field(s) {', '.join(repr(key) for key in unknown)}; "
        f"supported fields: {known}"
    )


# The complete schema of ``[migrations]``. Kept closed for the same reason as
# ``_AGENT_ROLE_KEYS``: a misspelled or invented key (``probe``, say -- a live
# database/CLI check has no place in Stage A) must be refused loudly rather
# than silently ignored.
_MIGRATIONS_KEYS: frozenset[str] = frozenset(
    {
        "adapter",
        "directory",
        "main_ref",
        "target",
        "target_ref",
        "deferred_registry",
        "max_observation_age_minutes",
    }
)

# Parsers this engine knows how to read saved migration-history/registry
# output for. Not a plugin point yet -- adding one means adding the parser in
# :mod:`agent_sparring.migration_adapters` too.
MIGRATIONS_ADAPTERS: frozenset[str] = frozenset({"supabase"})


def _require_repo_relative(value: str, key: str, *, where: str) -> str:
    """Refuse a path that is absolute or that escapes the repository.

    ``[migrations]`` paths are read from git trees relative to the
    repository root; an absolute path or a ``..`` that climbs out of it
    could only ever point somewhere this engine must not read.
    """

    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).anchor:
        raise ProjectConfigError(
            f"{where} field '{key}' must be relative to the repository root, got {value!r}"
        )
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../"):
        raise ProjectConfigError(
            f"{where} field '{key}' must stay inside the repository, got {value!r}"
        )
    return value


def _parse_migrations(table: Mapping[str, Any], *, source: str) -> MigrationsConfig | None:
    if "migrations" not in table:
        return None
    raw = table["migrations"]
    if not isinstance(raw, Mapping):
        raise ProjectConfigError(f"{source} field 'migrations' must be a table")
    where = f"{source} [migrations]"
    unknown = sorted(set(raw) - _MIGRATIONS_KEYS)
    if unknown:
        known = ", ".join(sorted(_MIGRATIONS_KEYS))
        raise ProjectConfigError(
            f"{where} has unknown field(s) {', '.join(repr(key) for key in unknown)}; "
            f"supported fields: {known}"
        )

    adapter = _require_str(raw, "adapter", where=where)
    if adapter not in MIGRATIONS_ADAPTERS:
        raise ProjectConfigError(
            f"{where} field 'adapter' must be one of {sorted(MIGRATIONS_ADAPTERS)}, got {adapter!r}"
        )
    directory = _require_repo_relative(
        _require_str(raw, "directory", where=where), "directory", where=where
    )
    main_ref = _require_str(raw, "main_ref", where=where)
    target = _optional_str(raw, "target", where=where) or "production"
    target_ref = _optional_str(raw, "target_ref", where=where)
    deferred_registry = _optional_str(raw, "deferred_registry", where=where)
    if deferred_registry is not None:
        _require_repo_relative(deferred_registry, "deferred_registry", where=where)
    max_age = _optional_int(raw, "max_observation_age_minutes", where=where, default=60)
    if max_age <= 0:
        raise ProjectConfigError(
            f"{where} field 'max_observation_age_minutes' must be a positive integer"
        )

    return MigrationsConfig(
        adapter=adapter,
        directory=directory,
        main_ref=main_ref,
        target=target,
        target_ref=target_ref,
        deferred_registry=deferred_registry,
        max_observation_age_minutes=max_age,
    )


def parse_project_config(raw: bytes | str, *, source: str = "project.toml") -> ProjectConfig:
    """Parse project.toml content into a :class:`ProjectConfig`.

    Raises :class:`ProjectConfigError` on malformed or incomplete input.
    """

    data: bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
    try:
        table = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"{source} is not valid TOML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectConfigError(f"{source} is not valid UTF-8: {exc}") from exc

    project_name = _require_str(table, "project", where=source)

    repo_table = _optional_table(table, "repo", where=source)
    repo_root = _optional_str(repo_table, "root", where=f"{source} [repo]") or "."

    commands_table = _optional_table(table, "commands", where=source)
    commands: dict[str, str] = {}
    for key, value in commands_table.items():
        if not isinstance(value, str) or not value.strip():
            raise ProjectConfigError(
                f"{source} [commands] field '{key}' must be a non-empty string"
            )
        commands[key] = value

    agents_table = _optional_table(table, "agents", where=source)
    stage_agents = _optional_table(agents_table, "stage", where=f"{source} [agents]")
    sparring_agents = _optional_table(
        agents_table, "sparring", where=f"{source} [agents]"
    )
    stage_where = f"{source} [agents.stage]"
    sparring_where = f"{source} [agents.sparring]"
    _reject_unknown_agent_keys(stage_agents, where=stage_where)
    _reject_unknown_agent_keys(sparring_agents, where=sparring_where)
    stage_provider = _optional_str(stage_agents, "provider", where=stage_where)
    sparring_provider = _optional_str(sparring_agents, "provider", where=sparring_where)
    obsolete = tuple(
        ObsoleteAgentSetting(role=role, field=key, value=str(role_table[key]))
        for role, role_table in (("stage", stage_agents), ("sparring", sparring_agents))
        for key in OBSOLETE_AGENT_KEYS
        if key in role_table
    )

    sparring_table = _optional_table(table, "sparring", where=source)
    default_mode = _optional_str(
        sparring_table, "default_mode", where=f"{source} [sparring]"
    )

    stage_table = _optional_table(table, "stage", where=source)
    self_check = _optional_bool(stage_table, "self_check", where=f"{source} [stage]")

    migrations = _parse_migrations(table, source=source)

    return ProjectConfig(
        project=project_name,
        repo_root=repo_root,
        commands=commands,
        stage_agent_provider=stage_provider,
        sparring_agent_provider=sparring_provider,
        default_sparring_mode=default_mode,
        stage_self_check=self_check if self_check is not None else False,
        obsolete_agent_settings=obsolete,
        migrations=migrations,
    )


def load_project_config(sparring_dir: Path) -> ProjectConfig:
    """Load and parse ``project.toml`` from a project's ``.sparring`` directory.

    Raises :class:`ProjectConfigError` if the file is missing, unreadable, or
    malformed.
    """

    path = Path(sparring_dir) / CONFIG_FILENAME
    if not path.is_file():
        raise ProjectConfigError(f"{path} does not exist")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectConfigError(f"could not read {path}: {exc}") from exc
    return parse_project_config(raw, source=str(path))


def load_project_markdown(sparring_dir: Path) -> str | None:
    """Load ``PROJECT.md`` as opaque text, or ``None`` if it does not exist.

    The content is never parsed or interpreted; it is passed through as-is
    for agents to read.
    """

    path = Path(sparring_dir) / CONTEXT_FILENAME
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
