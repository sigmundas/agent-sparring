"""Append-only observational activity stream (``activity.jsonl``).

This module is telemetry, not workflow state. Per the project plan ("an
optional append-only activity log may exist for observability, but it must
not become the authority controlling which workflow transitions are
legal"), nothing in orchestration reads the activity log to decide routing,
lifecycle status, acceptance, resume behavior, candidate identity, or
provider session identity. Deleting, corrupting, or making the log
unwritable leaves every orchestration outcome unchanged; the tests in
``tests/test_activity_non_authority.py`` prove that behaviorally.

Consequently this module offers no read API at all, and :meth:`ActivityLog.
emit` never raises: the first write failure silently disables that log
instance for the rest of the process. No warning is printed and no exit
code changes -- telemetry failure is not an orchestration event.

Event shape (schema version 1). Every line is one JSON object with a
fixed envelope::

    {"v": 1, "ts": "<UTC ISO 8601>", "actor": "...", "event": "..."}

plus, only when genuinely applicable, a small closed set of semantic fields
(see :data:`OPTIONAL_FIELDS`). Anything outside that set is dropped before
the line is written, so a caller cannot smuggle a raw provider payload,
prompt text, tool output, or shell command text into the log by accident.
Prompt text, chain-of-thought, tool inputs/outputs, diffs, and command
strings are never fields here by design.

This module deliberately imports nothing from the stage/loop/provider
modules: provider adapters emit through an :class:`ActivityEmitter` and
know nothing about stage directories or lifecycle semantics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

SCHEMA_VERSION = 1

# Actor vocabulary. "stage" is the implementation side (both its provider's
# own events and the stage-turn lifecycle), "sparrer" the sparring side,
# "loop" the unattended router's control decisions, "gate" the acceptance
# gate (freeze/accept), "plan" the plan runner walking planned stages
# (written into whichever stage's log is current).
ACTORS = ("stage", "sparrer", "loop", "gate", "plan")

# The only semantic fields an event may carry beyond the envelope. Closed
# on purpose: adding a field is a schema decision, not a call-site
# convenience. In particular there is no field for command text, tool
# input, tool output, prompt text, or arbitrary provider payloads.
OPTIONAL_FIELDS = frozenset(
    {
        "provider",  # adapter id, e.g. "claude-cli"
        "session_id",  # provider-issued session/thread id
        "model",  # only when the provider's own output states it
        "summary",  # one short human-readable line; never universally required
        "action",  # routing action, on a verdict
        "cycle",  # 1-based unattended-loop cycle number
        "sha",  # full commit id, on acceptance-gate events
        "tool",  # provider tool name, e.g. "Edit", "Bash", "shell"
        "path",  # repo path a file event is about (never its contents)
        "kind",  # semantic change kind when the provider states it (add/update/delete)
        "exit_code",  # process exit status when the provider reports it
        "resumed",  # whether a turn resumed an existing provider session
        "parent_id",  # provider-established parent tool-use id for nested-agent activity
        "tool_use_id",  # provider-issued id of the tool call an event is about
        # -- what a provider said about its own budget, on provider.usage --
        #
        # Every one of these is written only when the provider's own output
        # states it. There is no default and nothing is computed from a
        # model name: a number here is a quotation, and its absence means
        # "this provider did not say", which a reader must show as unknown
        # rather than as zero. That distinction is the whole reason these
        # are separate optional fields instead of one always-present blob.
        "input_tokens",  # prompt tokens the provider reported for the session
        "output_tokens",  # completion tokens likewise
        "total_tokens",  # the provider's own total, cumulative over the session
        # How full the window is *right now*: the tokens the model was
        # holding on its latest request, cached prompt included. A
        # different fact from "total_tokens", which only ever grows and
        # routinely exceeds the window; only this one is a share of
        # "context_window".
        "context_used_tokens",
        "context_window",  # the model's context size *as the provider stated it*
        "rate_limit_percent",  # primary window usage, 0-100, when reported
        "rate_limit_window_minutes",  # the primary window that percentage is of
        "rate_limit_secondary_percent",  # the longer window (e.g. weekly), when reported
        "rate_limit_secondary_window_minutes",  # its length
    }
)

SUMMARY_MAX_CHARS = 200


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def truncate(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Clamp a human-readable one-liner to ``limit`` characters."""

    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _split_lexical(raw: str) -> tuple[bool, str, list[str], bool]:
    """Split a path string lexically into (is_absolute, anchor, parts,
    windows_flavour), collapsing ``.`` and ``..`` without touching the
    filesystem. A ``..`` that climbs above the start is kept so the caller
    can detect an escape."""

    windows = bool(PureWindowsPath(raw).drive) or "\\" in raw
    pure = PureWindowsPath(raw) if windows else PurePosixPath(raw)
    anchor = pure.anchor
    parts: list[str] = []
    for part in pure.parts[1:] if anchor else pure.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append("..")
            continue
        parts.append(part)
    return bool(anchor), anchor, parts, windows


def _root_candidates(repo_root: Path) -> list[tuple[str, list[str], bool]]:
    """Lexical forms of the repository root to match a provider path
    against: as given (made absolute against the process cwd if relative)
    and, when it differs, its real path -- so a provider that reports
    ``/private/tmp/...`` for a root given as ``/tmp/...`` still matches.
    Only the root directory is looked up, never the target file."""

    candidates: list[str] = []
    try:
        candidates.append(os.path.abspath(str(repo_root)))
    except Exception:
        pass
    try:
        real = os.path.realpath(str(repo_root))
        if real not in candidates:
            candidates.append(real)
    except Exception:
        pass
    result = []
    for text in candidates:
        is_abs, anchor, parts, windows = _split_lexical(text)
        if is_abs:
            result.append((anchor, parts, windows))
    return result


def repo_relative_path(raw: object, repo_root: Path) -> str | None:
    """Normalize a provider-reported file path for telemetry.

    Returns the path relative to ``repo_root`` with ``/`` separators, or
    ``None`` when the path cannot be shown to lie inside the repository:
    an absolute path under a different root, a relative path escaping via
    ``..``, the root itself, or anything unparseable. An absolute path is
    therefore never persisted verbatim -- telemetry drops the field rather
    than leak local filesystem layout. Purely lexical: the target file is
    never resolved or read.
    """

    if not isinstance(raw, str) or not raw.strip():
        return None
    is_abs, anchor, parts, windows = _split_lexical(raw)

    if not is_abs:
        if not parts or parts[0] == "..":
            return None
        return "/".join(parts)

    def _key(part: str) -> str:
        return part.lower() if windows else part

    for root_anchor, root_parts, root_windows in _root_candidates(repo_root):
        if root_windows != windows:
            continue
        if _key(root_anchor) != _key(anchor):
            continue
        if len(parts) <= len(root_parts):
            continue
        if [_key(p) for p in parts[: len(root_parts)]] != [_key(p) for p in root_parts]:
            continue
        return "/".join(parts[len(root_parts) :])
    return None


def _clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allowed, non-None fields, with the summary clamped."""

    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in OPTIONAL_FIELDS or value is None:
            continue
        if key == "summary":
            value = truncate(str(value))
        cleaned[key] = value
    return cleaned


@dataclass
class ActivityLog:
    """One append-only JSONL activity file.

    ``emit`` never raises. After the first failed write (unwritable path, a
    directory where the file should be, disk full, ...) the instance marks
    itself disabled and every later emit is a silent no-op. There is
    intentionally no method that reads the file.
    """

    path: Path
    disabled: bool = False

    def emit(self, actor: str, event: str, **fields: Any) -> None:
        if self.disabled:
            return
        try:
            record: dict[str, Any] = {
                "v": SCHEMA_VERSION,
                "ts": _utc_now_iso(),
                "actor": str(actor),
                "event": str(event),
            }
            record.update(_clean_fields(fields))
            line = (
                json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n"
            )
            data = line.encode("utf-8")
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception:
            # Telemetry failure is silent and non-fatal by decision: no
            # exception into orchestration, no stderr warning, no exit-code
            # change. Just stop trying for this instance.
            self.disabled = True

    def bind(self, actor: str, *, provider: str | None = None) -> "ActivityEmitter":
        return ActivityEmitter(log=self, actor=actor, provider=provider)


@dataclass(frozen=True)
class ActivityEmitter:
    """A view onto an :class:`ActivityLog` with the actor (and optionally
    the provider id) fixed, so a provider adapter can emit its own events
    without knowing which stage, role, or file it is serving."""

    log: ActivityLog
    actor: str
    provider: str | None = None

    def emit(self, event: str, **fields: Any) -> None:
        if self.provider is not None and "provider" not in fields:
            fields["provider"] = self.provider
        self.log.emit(self.actor, event, **fields)


def emit(emitter: ActivityEmitter | None, event: str, **fields: Any) -> None:
    """Emit through ``emitter`` if there is one; a ``None`` emitter is silent.

    Small convenience so adapters can write ``emit(self.activity, ...)``
    without a None check at every call site.
    """

    if emitter is not None:
        emitter.emit(event, **fields)


__all__ = [
    "ACTORS",
    "ActivityEmitter",
    "ActivityLog",
    "OPTIONAL_FIELDS",
    "SCHEMA_VERSION",
    "SUMMARY_MAX_CHARS",
    "emit",
    "repo_relative_path",
    "truncate",
]
