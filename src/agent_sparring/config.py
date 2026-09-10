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

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
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

    def command(self, name: str) -> str | None:
        """Return a configured project command by name, if any."""

        return self.commands.get(name)


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


def _optional_table(
    table: Mapping[str, Any], key: str, *, where: str
) -> Mapping[str, Any]:
    if key not in table:
        return {}
    value = table[key]
    if not isinstance(value, Mapping):
        raise ProjectConfigError(f"{where} field '{key}' must be a table")
    return value


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
    stage_provider = _optional_str(
        stage_agents, "provider", where=f"{source} [agents.stage]"
    )
    sparring_provider = _optional_str(
        sparring_agents, "provider", where=f"{source} [agents.sparring]"
    )

    sparring_table = _optional_table(table, "sparring", where=source)
    default_mode = _optional_str(
        sparring_table, "default_mode", where=f"{source} [sparring]"
    )

    stage_table = _optional_table(table, "stage", where=source)
    self_check = _optional_bool(stage_table, "self_check", where=f"{source} [stage]")

    return ProjectConfig(
        project=project_name,
        repo_root=repo_root,
        commands=commands,
        stage_agent_provider=stage_provider,
        sparring_agent_provider=sparring_provider,
        default_sparring_mode=default_mode,
        stage_self_check=self_check if self_check is not None else False,
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
