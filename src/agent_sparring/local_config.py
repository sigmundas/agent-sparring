"""Per-worktree agent model/effort overrides that never touch the working tree.

``.sparring/project.toml`` is tracked in the consuming repository. Changing a
model or an effort there dirties the worktree, and a managed run then refuses
acceptance because of an edit that is not part of any candidate -- so choosing
a different model for the next stage stopped the loop it was meant to steer.

This layer holds the same two fields (``model`` and ``effort``, per role) in
a small TOML file inside the worktree's *git directory*::

    <git rev-parse --absolute-git-dir>/agent-sparring/<sparring dir>.toml

The git directory is never part of ``git status`` -- not tracked, not
untracked, not ignored -- so nothing the engine or git reports as dirty can
see it, and each linked worktree has its own. It is local to this checkout by
construction: nothing about it is committed, pushed or shared.

Precedence (see :mod:`agent_sparring.agent_config`)::

    CLI  >  environment  >  local override (this file)  >  project.toml  >  default

Values are validated exactly as ``project.toml``'s are: an effort a provider
cannot honour is a configuration error whichever layer supplied it. Provider
selection is deliberately not overridable here -- a provider change alters
what a turn *is*, not how hard it thinks, and stays a committed decision.

Live-run semantics are the loop's own: the engine re-reads agent
configuration immediately before each planned stage, so a change applies to
the next stage's turns and never to a provider process already running.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agent_sparring.config import ProjectConfigError

LOCAL_DIRNAME = "agent-sparring"
LOCAL_ROLES: tuple[str, ...] = ("stage", "sparring")
LOCAL_FIELDS: tuple[str, ...] = ("model", "effort")


@dataclass(frozen=True)
class LocalAgentOverrides:
    """The overrides one ``.sparring`` has in this worktree. ``None`` = not overridden."""

    path: Path
    stage_model: str | None = None
    stage_effort: str | None = None
    sparring_model: str | None = None
    sparring_effort: str | None = None

    def get(self, role: str, field: str) -> str | None:
        return getattr(self, f"{role}_{field}")


def _git_locations(sparring_dir: Path) -> tuple[Path, Path] | None:
    """The worktree's absolute git directory and top level, or ``None`` outside git."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(sparring_dir), "rev-parse", "--absolute-git-dir", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) < 2:
        return None
    return Path(lines[0]), Path(lines[1]).resolve()


def local_overrides_path(sparring_dir: Path) -> Path | None:
    """Where this ``.sparring``'s local overrides live, or ``None`` outside git.

    The file is named after the ``.sparring`` directory's path relative to the
    worktree top level, so nested projects in one worktree never share one.
    """

    sparring_dir = Path(sparring_dir).resolve()
    found = _git_locations(sparring_dir)
    if found is None:
        return None
    git_dir, top = found
    try:
        relative = sparring_dir.relative_to(top)
    except ValueError:
        return None
    name = "__".join(relative.parts) or ".sparring"
    return git_dir / LOCAL_DIRNAME / f"{name}.toml"


def load_local_overrides(sparring_dir: Path) -> LocalAgentOverrides | None:
    """Read this worktree's overrides; ``None`` when there are none to read."""

    path = local_overrides_path(sparring_dir)
    if path is None or not path.is_file():
        return None
    return _parse(path)


def _parse(path: Path) -> LocalAgentOverrides:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(f"local agent overrides {path} are unreadable: {exc}") from exc
    unknown = sorted(set(table) - {"agents"})
    agents = table.get("agents", {})
    if unknown or not isinstance(agents, dict):
        raise ProjectConfigError(f"local agent overrides {path} may hold only [agents.stage] and [agents.sparring]")
    values: dict[str, str] = {}
    for role, entry in agents.items():
        if role not in LOCAL_ROLES or not isinstance(entry, dict):
            raise ProjectConfigError(f"local agent overrides {path}: unknown role [agents.{role}]")
        for field, value in entry.items():
            if field not in LOCAL_FIELDS:
                raise ProjectConfigError(
                    f"local agent overrides {path}: [agents.{role}] {field} cannot be overridden locally; "
                    f"only {', '.join(LOCAL_FIELDS)} can"
                )
            if not isinstance(value, str) or not value.strip():
                raise ProjectConfigError(f"local agent overrides {path}: [agents.{role}] {field} must be a non-empty string")
            values[f"{role}_{field}"] = value
    return LocalAgentOverrides(path=path, **values)


def _render(overrides: LocalAgentOverrides) -> str:
    lines = [
        "# Agent Sparring local overrides for this worktree only.",
        "# Written by 'sparring set-config --local'; never committed (this lives in the git directory).",
    ]
    for role in LOCAL_ROLES:
        entries = [(field, overrides.get(role, field)) for field in LOCAL_FIELDS]
        entries = [(field, value) for field, value in entries if value is not None]
        if entries:
            lines.append("")
            lines.append(f"[agents.{role}]")
            lines.extend(f"{field} = {_toml_string(value)}" for field, value in entries)
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in escaped):
        raise ProjectConfigError(f"{value!r} contains a control character")
    return f'"{escaped}"'


def with_local_edit(
    sparring_dir: Path,
    role: str,
    field: str,
    value: str | None,
    *,
    base: LocalAgentOverrides | None = None,
) -> LocalAgentOverrides:
    """The overrides as they would be after one edit (``value`` ``None`` clears it).

    ``base`` is an earlier, unwritten result to build on; otherwise the file
    on disk. Nothing is written.
    """

    if role not in LOCAL_ROLES:
        raise ProjectConfigError(f"unknown agent role {role!r}")
    if field not in LOCAL_FIELDS:
        raise ProjectConfigError(
            f"{field} cannot be overridden locally; only {', '.join(LOCAL_FIELDS)} can. "
            f"Change it in project.toml instead"
        )
    path = local_overrides_path(sparring_dir)
    if path is None:
        raise ProjectConfigError(
            f"{sparring_dir} is not inside a git worktree, so there is no local override location; "
            f"set it in project.toml instead"
        )
    if value is not None and not value.strip():
        raise ProjectConfigError(f"{field} must not be empty; use the default flag to clear it")
    current = base if base is not None else (_parse(path) if path.is_file() else LocalAgentOverrides(path=path))
    values = {f"{r}_{f}": current.get(r, f) for r in LOCAL_ROLES for f in LOCAL_FIELDS}
    values[f"{role}_{field}"] = value.strip() if value is not None else None
    return LocalAgentOverrides(path=path, **values)


def write_local_overrides(overrides: LocalAgentOverrides) -> bool:
    """Write atomically; remove the file when nothing is overridden. True when anything changed."""

    path = overrides.path
    empty = all(overrides.get(role, field) is None for role in LOCAL_ROLES for field in LOCAL_FIELDS)
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    if empty:
        if before is None:
            return False
        path.unlink()
        return True
    text = _render(overrides)
    if text == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".agents-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return True


__all__ = [
    "LOCAL_FIELDS",
    "LOCAL_ROLES",
    "LocalAgentOverrides",
    "load_local_overrides",
    "local_overrides_path",
    "with_local_edit",
    "write_local_overrides",
]
