"""Claude Code CLI stage-agent adapter.

Behavior asserted here was verified against a real local ``claude`` install
(version 2.1.236 for the original single-object contract), not assumed:

- ``claude -p <prompt> --output-format json`` prints one JSON object on
  stdout containing a provider-issued ``session_id`` plus ``result`` (the
  final text) and ``is_error``.
- ``claude -p ... --resume <session_id>`` continues that same session and
  the JSON output reports the same ``session_id`` back.
- Resuming an unknown/invalid session id exits non-zero and prints a plain
  text error (not JSON) on stdout — this is handled as a
  :class:`~agent_sparring.providers.ProviderError`, not a crash.

Streaming (the activity-stream stage) switches the actual invocation to
``--output-format stream-json --verbose`` (``--verbose`` is mandatory for
stream-json in print mode: the installed 2.1.269 binary carries the check
"When using --print, --output-format=stream-json requires --verbose").
Per the Claude Code headless documentation, stdout is then one JSON object
per line: ``system`` (subtype ``init``), ``assistant``, ``user``, and a
final ``result`` line carrying the same ``session_id``/``result``/
``is_error`` fields as the single-object form. The final result is still
parsed from that ``result`` line (the old single-object shape is also
accepted, so existing fixtures and any older CLI keep working); the other
lines feed the observational activity stream only, via
:class:`_ClaudeStreamTranslator`. Nothing about the returned
:class:`~agent_sparring.providers.StageAgentResult` depends on those
intermediate lines. Exact ``tool_use`` block shapes have not been confirmed
against a live run in this repository yet; the translator only reads
fields defensively and emits nothing when they are absent.

This module is the only place that knows any of the above. Generic
orchestration code talks to the :class:`~agent_sparring.providers.
StageAgentAdapter` protocol, not to ``claude`` flags directly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_sparring.activity import ActivityEmitter, emit, repo_relative_path
from agent_sparring.providers import ProviderError, Runner, StageAgentResult
from agent_sparring.providers.subprocess_runner import LineSink, run_streaming

DEFAULT_EXECUTABLE = "claude"
DEFAULT_PERMISSION_MODE = "acceptEdits"
PROVIDER_ID = "claude-cli"
# Human-readable name for this provider. The engine keeps PROVIDER_ID as the
# canonical identifier everywhere; this is only for showing a person.
DISPLAY_NAME = "Claude"

# Verified against the installed CLI (claude 2.1.277): "--effort <level>
# Effort level for the current session (low, medium, high, xhigh, max)".
#
# The CLI does NOT reject an unrecognised level. It prints "Warning: Unknown
# --effort value 'bogus' - ignoring it and using the default effort" and runs
# the turn anyway. A typo would therefore buy a full-price turn at an effort
# nobody chose, so this adapter refuses the value itself rather than passing
# it through (see ClaudeCliAdapter.__post_init__).
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Tool names whose ``input.file_path`` (or ``notebook_path``) names a file
# the agent is changing. Only the path is ever recorded -- never
# old/new strings, content, or any other input field.
_FILE_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_FILE_PATH_KEYS = ("file_path", "notebook_path")
# Tool names that run a shell command. The command text is never recorded.
_SHELL_TOOLS = {"Bash"}
# Tool names that genuinely start a subagent in Claude Code.
_SUBAGENT_TOOLS = {"Task", "Agent"}


def _default_runner(
    args: list[str], cwd: Path, timeout_seconds: float | None, on_line: LineSink | None
) -> "subprocess.CompletedProcess[str]":
    return run_streaming(args, cwd, timeout_seconds, on_line)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class _ClaudeStreamTranslator:
    """Turns Claude stream-json lines into activity events.

    Observational only: every method swallows its own errors (the runner
    also guards the callback), never touches the final result, and records
    no prompt text, tool inputs beyond a file path, tool results, or
    partial-message deltas. File paths are persisted repo-relative via
    :func:`agent_sparring.activity.repo_relative_path` (Claude reports
    absolute paths); a path outside ``repo_root`` is omitted, never leaked.

    ``on_session`` is the one exception to "observational only", and it is
    not telemetry: the ``system``/``init`` line is where the CLI announces
    the session id for this turn, and a caller that must record that
    identity before the turn ends (see
    :func:`agent_sparring.stage_agent.run_stage_agent`) has nowhere else to
    learn it. It is called at most once, with the provider's own id.
    """

    def __init__(
        self,
        emitter: ActivityEmitter | None,
        repo_root: Path,
        on_session: "Callable[[str], None] | None" = None,
    ) -> None:
        self._emitter = emitter
        self._repo_root = repo_root
        self._on_session = on_session
        self._tool_names: dict[str, str] = {}
        self._session_announced = False

    def feed(self, line: str) -> None:
        if self._emitter is None and self._on_session is None:
            return
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "system":
            self._system(message)
        elif kind == "assistant":
            self._assistant(message)
        elif kind == "user":
            self._user(message)
        elif kind == "result":
            self._result(message)
        # "stream_event" (partial deltas) and anything unknown: ignored.

    def _system(self, message: dict[str, Any]) -> None:
        if message.get("subtype") != "init":
            return
        session_id = _str_or_none(message.get("session_id"))
        emit(
            self._emitter,
            "session.observed",
            session_id=session_id,
            model=_str_or_none(message.get("model")),
        )
        if session_id and self._on_session and not self._session_announced:
            self._session_announced = True
            self._on_session(session_id)

    @staticmethod
    def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else None
        if not isinstance(content, list):
            return []
        return [block for block in content if isinstance(block, dict)]

    def _assistant(self, message: dict[str, Any]) -> None:
        parent_id = _str_or_none(message.get("parent_tool_use_id"))
        for block in self._content_blocks(message):
            if block.get("type") != "tool_use":
                continue
            name = _str_or_none(block.get("name"))
            if name is None:
                continue
            tool_use_id = _str_or_none(block.get("id"))
            if tool_use_id is not None:
                self._tool_names[tool_use_id] = name
            raw_input = block.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}

            if name in _FILE_EDIT_TOOLS:
                raw_path = next(
                    (
                        _str_or_none(tool_input.get(key))
                        for key in _FILE_PATH_KEYS
                        if _str_or_none(tool_input.get(key))
                    ),
                    None,
                )
                emit(
                    self._emitter,
                    "file.changed",
                    tool=name,
                    path=repo_relative_path(raw_path, self._repo_root),
                    parent_id=parent_id,
                )
            elif name in _SHELL_TOOLS:
                emit(
                    self._emitter,
                    "command.started",
                    tool=name,
                    tool_use_id=tool_use_id,
                    parent_id=parent_id,
                )
            elif name in _SUBAGENT_TOOLS:
                # The structured stream shows an actual Task/Agent tool call:
                # that, and only that, establishes a subagent. Nothing from
                # its input (description, prompt, subagent type) is recorded.
                emit(
                    self._emitter,
                    "subagent.started",
                    tool=name,
                    tool_use_id=tool_use_id,
                    parent_id=parent_id,
                )
            else:
                emit(self._emitter, "tool.call", tool=name, parent_id=parent_id)

    def _user(self, message: dict[str, Any]) -> None:
        parent_id = _str_or_none(message.get("parent_tool_use_id"))
        for block in self._content_blocks(message):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = _str_or_none(block.get("tool_use_id"))
            name = self._tool_names.get(tool_use_id or "")
            if name in _SHELL_TOOLS:
                # Only the fact that the command finished. Its output is in
                # the block's content and is deliberately never read.
                emit(
                    self._emitter,
                    "command.finished",
                    tool=name,
                    tool_use_id=tool_use_id,
                    parent_id=parent_id,
                )

    def _result(self, message: dict[str, Any]) -> None:
        num_turns = message.get("num_turns")
        subtype = _str_or_none(message.get("subtype"))
        parts = []
        if subtype:
            parts.append(subtype)
        if isinstance(num_turns, int):
            parts.append(f"{num_turns} turn(s)")
        emit(
            self._emitter,
            "provider.result",
            session_id=_str_or_none(message.get("session_id")),
            summary=", ".join(parts) or None,
        )


@dataclass
class ClaudeCliAdapter:
    """Stage-agent adapter backed by the ``claude`` CLI in print mode.

    ``runner`` is injectable so tests can exercise argument-building and
    output-parsing without launching a real CLI process or model.
    ``activity`` is an optional observational emitter; when ``None`` no
    telemetry is produced and nothing else changes.
    """

    repo_root: Path
    executable: str = DEFAULT_EXECUTABLE
    permission_mode: str = DEFAULT_PERMISSION_MODE
    # Typed provider-turn configuration, already resolved by
    # agent_sparring.agent_config. ``None`` means "pass no flag": the CLI
    # then picks its own model/effort, and the engine claims nothing about
    # which one that is.
    model: str | None = None
    effort: str | None = None
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    runner: Runner = field(default=_default_runner)
    activity: ActivityEmitter | None = None

    # Called with the provider's own session id as soon as the CLI announces
    # it, while the turn is still running. The orchestrator uses it to record
    # the session identity of a *fresh* turn before that turn can be killed
    # (see agent_sparring.stage_agent.run_stage_agent); an adapter nobody set
    # it on behaves exactly as before.
    on_session_observed: "Callable[[str], None] | None" = None

    provider_id: str = PROVIDER_ID

    # Claude Code CLI supports both a fresh session and resuming a prior one
    # by its own session id (see module docstring); a future provider that
    # cannot resume should say so via this flag rather than the caller
    # discovering it by a failed call.
    supports_resume: bool = True

    def __post_init__(self) -> None:
        # Refuse an unsupported effort at construction, which is always
        # before a provider process exists. The CLI would merely warn and
        # silently fall back (see EFFORT_LEVELS), and a turn that ran at an
        # unintended effort cannot be undone.
        if self.effort is not None and self.effort not in EFFORT_LEVELS:
            raise ProviderError(
                f"effort {self.effort!r} is not supported by {PROVIDER_ID}; "
                f"supported levels: {', '.join(EFFORT_LEVELS)}"
            )

    def start(self, prompt: str) -> StageAgentResult:
        return self._invoke(prompt, resume_session_id=None)

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ProviderError(
                "resume requires a non-empty session_id obtained from a prior "
                "provider result, not an empty/invented value"
            )
        return self._invoke(prompt, resume_session_id=session_id)

    # -- internals ----------------------------------------------------

    def _build_args(self, prompt: str, resume_session_id: str | None) -> list[str]:
        args = [
            self.executable,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            # Mandatory with stream-json in print mode (the CLI refuses
            # otherwise); it does not add partial-message deltas.
            "--verbose",
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        args += list(self.extra_args)
        return args

    def _invoke(self, prompt: str, resume_session_id: str | None) -> StageAgentResult:
        args = self._build_args(prompt, resume_session_id)
        translator = _ClaudeStreamTranslator(self.activity, self.repo_root, self.on_session_observed)
        try:
            result = self.runner(args, self.repo_root, self.timeout_seconds, translator.feed)
        except OSError as exc:
            raise ProviderError(f"could not launch {self.executable!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"{self.executable} timed out after {self.timeout_seconds}s"
            ) from exc
        return self._parse(result)

    def _final_payload(self, stdout: str) -> dict[str, Any] | None:
        """The final result object: the last ``type == "result"`` line of a
        stream-json run, or the whole stdout when it is one JSON object
        (the older ``--output-format json`` shape). ``None`` if neither."""

        try:
            whole = json.loads(stdout)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, dict) and whole.get("type") in (None, "result"):
            # The single-object shape (--output-format json), which also
            # carries type == "result". A lone stream-json line of another
            # type (e.g. only "system"/"init" before a crash) is not a
            # final result and falls through to the scan below.
            return whole

        final: dict[str, Any] | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("type") == "result":
                final = message
        return final

    def _parse(self, result: "subprocess.CompletedProcess[str]") -> StageAgentResult:
        stdout = result.stdout or ""
        payload = self._final_payload(stdout)
        if payload is None:
            detail = stdout.strip() or (result.stderr or "").strip() or "(no output)"
            raise ProviderError(
                f"{self.executable} did not return a JSON result (exit "
                f"{result.returncode}): {detail}"
            )

        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProviderError(
                f"{self.executable} JSON output has no usable session_id: {payload!r}"
            )

        text = payload.get("result")
        if not isinstance(text, str):
            text = ""

        return StageAgentResult(
            session_id=session_id,
            text=text,
            is_error=bool(payload.get("is_error")),
            raw=payload,
        )


__all__ = [
    "ClaudeCliAdapter",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_PERMISSION_MODE",
    "DISPLAY_NAME",
    "EFFORT_LEVELS",
    "PROVIDER_ID",
]
