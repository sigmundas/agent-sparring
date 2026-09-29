"""The person's own model/effort preferences, shared by every project.

Provider is a project decision and lives in ``.sparring/project.toml``. Which
exact model a provider runs, and how hard it thinks, is the *person's*
preference: choosing a model while working in one repository must still be
the choice after switching to another that uses the same provider for the
same role. So model and effort live in one engine-owned file outside every
repository::

    $SPARRING_USER_CONFIG                              (when set: the file itself)
    $XDG_CONFIG_HOME/agent-sparring/config.toml        (when XDG_CONFIG_HOME is set)
    %APPDATA%\\agent-sparring\\config.toml               (Windows)
    ~/.config/agent-sparring/config.toml               (macOS, Linux)

keyed by role *and* provider, so a preference for one provider is never
applied to another::

    version = 1

    [agents.stage.claude-cli]
    model = "claude-opus-5-5"
    effort = "low"

    [agents.sparring.codex-cli]
    model = "gpt-6-astra"

The file is never inside a repository, so writing it cannot dirty one. Writes
are validated by the caller before they happen (see
:func:`agent_sparring.agent_config.validate_preference`), serialised by a lock
file, and atomic. The schema is closed: an unknown role, a provider id that is
not a plain name, or a key other than ``model``/``effort`` is a load error, not
silently ignored.

A change applies from the next stage: each stage pins the configuration it
resolved before its first provider turn and keeps it to the end (see
:class:`agent_sparring.stage.PinnedAgent`).
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

from agent_sparring.config import ProjectConfigError

#: Environment variable naming the preference file itself. Not of the
#: ``SPARRING_<ROLE>_<FIELD>`` shape, so it can never read as a role override.
USER_CONFIG_ENV = "SPARRING_USER_CONFIG"
USER_CONFIG_DIRNAME = "agent-sparring"
USER_CONFIG_FILENAME = "config.toml"
USER_CONFIG_VERSION = 1

PREFERENCE_ROLES: tuple[str, ...] = ("stage", "sparring")
PREFERENCE_FIELDS: tuple[str, ...] = ("model", "effort")

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UserConfigError(ProjectConfigError):
    """The user preference file is unreadable, malformed, or could not be written."""


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where this person's preferences live. Never inside a repository."""

    env = os.environ if environ is None else environ
    explicit = env.get(USER_CONFIG_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        base = Path(xdg).expanduser()
    elif sys.platform == "win32" and env.get("APPDATA", "").strip():
        base = Path(env["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / USER_CONFIG_DIRNAME / USER_CONFIG_FILENAME


@dataclass(frozen=True)
class RolePreference:
    """One (role, provider)'s preference. ``None`` = not set: provider default."""

    model: str | None = None
    effort: str | None = None

    def get(self, field_name: str) -> str | None:
        return getattr(self, field_name)

    @property
    def is_empty(self) -> bool:
        return self.model is None and self.effort is None


@dataclass(frozen=True)
class UserPreferences:
    """Every preference in the file, as ``{(role, provider): RolePreference}``."""

    path: Path
    entries: Mapping[tuple[str, str], RolePreference] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def for_role(self, role: str, provider: str) -> RolePreference:
        """The preference for exactly this role and provider -- never another provider's."""

        return self.entries.get((role, provider), RolePreference())

    def with_edit(self, role: str, provider: str, field_name: str, value: str | None) -> "UserPreferences":
        """These preferences after one edit (``value`` ``None`` clears it). Nothing is written."""

        _check_key(role, provider, where="edit")
        if field_name not in PREFERENCE_FIELDS:
            raise UserConfigError(
                f"{field_name!r} is not a user preference; only {', '.join(PREFERENCE_FIELDS)} are"
            )
        if value is not None:
            value = value.strip()
            if not value:
                raise UserConfigError(f"{field_name} must not be empty; clear the preference instead")
        current = self.for_role(role, provider)
        values = {name: current.get(name) for name in PREFERENCE_FIELDS}
        values[field_name] = value
        entries = dict(self.entries)
        updated = RolePreference(**values)
        if updated.is_empty:
            entries.pop((role, provider), None)
        else:
            entries[(role, provider)] = updated
        return UserPreferences(path=self.path, entries=entries)


def _check_key(role: str, provider: str, *, where: str) -> None:
    if role not in PREFERENCE_ROLES:
        raise UserConfigError(f"{where}: unknown agent role {role!r}; known roles: {', '.join(PREFERENCE_ROLES)}")
    if not _PROVIDER_ID_RE.match(provider):
        raise UserConfigError(f"{where}: {provider!r} is not a provider id")


def load_user_preferences(path: Path | None = None) -> UserPreferences:
    """Read the preference file; an absent file is simply "no preferences"."""

    path = user_config_path() if path is None else Path(path)
    if not path.is_file():
        return UserPreferences(path=path)
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UserConfigError(f"user preferences {path} are unreadable: {exc}") from exc
    return UserPreferences(path=path, entries=_parse(table, path))


def _parse(table: Mapping[str, object], path: Path) -> dict[tuple[str, str], RolePreference]:
    unknown = sorted(set(table) - {"version", "agents"})
    if unknown:
        raise UserConfigError(f"user preferences {path}: unknown key(s) {', '.join(unknown)}")
    version = table.get("version", USER_CONFIG_VERSION)
    if version != USER_CONFIG_VERSION:
        raise UserConfigError(
            f"user preferences {path} are version {version!r}; this engine reads version {USER_CONFIG_VERSION}"
        )
    agents = table.get("agents", {})
    if not isinstance(agents, dict):
        raise UserConfigError(f"user preferences {path}: 'agents' must be a table")
    entries: dict[tuple[str, str], RolePreference] = {}
    for role, providers in agents.items():
        if not isinstance(providers, dict):
            raise UserConfigError(f"user preferences {path}: [agents.{role}] must be a table of providers")
        for provider, values in providers.items():
            where = f"user preferences {path} [agents.{role}.{provider}]"
            _check_key(role, provider, where=where)
            if not isinstance(values, dict):
                raise UserConfigError(f"{where} must be a table")
            bad = sorted(set(values) - set(PREFERENCE_FIELDS))
            if bad:
                raise UserConfigError(
                    f"{where}: unknown field(s) {', '.join(bad)}; supported: {', '.join(PREFERENCE_FIELDS)}"
                )
            for name, value in values.items():
                if not isinstance(value, str) or not value.strip():
                    raise UserConfigError(f"{where}: {name} must be a non-empty string")
            preference = RolePreference(**{name: values[name].strip() for name in values})
            if not preference.is_empty:
                entries[(role, provider)] = preference
    return entries


def render_user_preferences(preferences: UserPreferences) -> str:
    """The file's text: deterministic, so an unchanged edit writes nothing."""

    lines = [
        "# Agent Sparring user preferences: model and effort per role and provider.",
        "# Shared by every project; written by 'sparring set-config'. The provider",
        "# itself is chosen per project, in .sparring/project.toml.",
        "",
        f"version = {USER_CONFIG_VERSION}",
    ]
    for role, provider in sorted(preferences.entries):
        preference = preferences.entries[(role, provider)]
        lines.append("")
        lines.append(f"[agents.{role}.{provider}]")
        for name in PREFERENCE_FIELDS:
            value = preference.get(name)
            if value is not None:
                lines.append(f"{name} = {_toml_string(value)}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise UserConfigError(f"{value!r} contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise writers of one preference file (a double-clicked control, two
    windows). POSIX only; elsewhere the atomic replace alone applies."""

    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.name}.lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_user_preferences(
    path: Path,
    edit: Callable[[UserPreferences], UserPreferences],
) -> tuple[UserPreferences, bool]:
    """Re-read ``path`` under the lock, apply ``edit``, validate, write atomically.

    ``edit`` returns the edited preferences, or raises before anything is
    written. The file is removed when no preference is left in it. Returns
    the preferences now on disk and whether anything changed.
    """

    try:
        with _locked(path):
            current = load_user_preferences(path)
            updated = edit(current)
            before = path.read_text(encoding="utf-8") if path.is_file() else None
            if not updated.entries:
                if before is None:
                    return updated, False
                path.unlink()
                return updated, True
            text = render_user_preferences(updated)
            if text == before:
                return updated, False
            _atomic_write(path, text)
            return updated, True
    except OSError as exc:
        raise UserConfigError(f"could not write user preferences {path}: {exc}") from exc


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


__all__ = [
    "PREFERENCE_FIELDS",
    "PREFERENCE_ROLES",
    "RolePreference",
    "USER_CONFIG_ENV",
    "UserConfigError",
    "UserPreferences",
    "load_user_preferences",
    "render_user_preferences",
    "update_user_preferences",
    "user_config_path",
]
