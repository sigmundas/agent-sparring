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


def _non_negative_int(value: Any) -> int | None:
    """``value`` as a token count, or None when it is not one.

    Tolerant on purpose: this reads another program's JSON, where a field
    may be absent, null or a string. Anything that is not a plain
    non-negative integer is "not stated", because a wrong number shown as
    a budget is worse than no number.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


# What the model was actually holding when it produced a message: the
# fresh prompt tokens, the cached prompt it read, the prompt it wrote to
# cache, and its own reply. Together these are the context occupancy the
# ctx dial is about. ``input_tokens`` alone is a small remainder.
_CONTEXT_TOKEN_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _assistant_usage(message: dict[str, Any]) -> dict[str, Any]:
    """Tokens stated by one ``assistant`` line, including its cache."""

    inner = message.get("message")
    usage = inner.get("usage") if isinstance(inner, dict) else None
    if not isinstance(usage, dict):
        usage = message.get("usage")
    if not isinstance(usage, dict):
        return {}
    stated = {key: _non_negative_int(usage.get(key)) for key in _CONTEXT_TOKEN_KEYS}
    fields: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens"):
        if stated[key] is not None:
            fields[key] = stated[key]
    present = [value for value in stated.values() if value is not None]
    # The prompt side must be stated for this to be an occupancy at all; a
    # line carrying only `output_tokens` says nothing about how full the
    # window is.
    if present and stated["input_tokens"] is not None:
        fields["context_used_tokens"] = sum(present)
    return fields


def _result_usage(message: dict[str, Any], model: str | None) -> dict[str, Any]:
    """The context window the final ``result`` line states, if it does.

    ``modelUsage`` is keyed by model id and one turn may list several of
    them, because a subagent can run on a different model with a different
    window. The stage's own model -- the one the ``init`` line announced --
    is the one whose window the dial is a share of. Where that cannot be
    matched, a single entry, or several that agree, still says the window
    unambiguously; anything else is left unstated rather than picked from.
    """

    usage = message.get("modelUsage")
    if not isinstance(usage, dict):
        return {}
    windows: dict[str, int] = {}
    for name, entry in usage.items():
        if not isinstance(entry, dict) or not isinstance(name, str):
            continue
        window = _non_negative_int(entry.get("contextWindow"))
        if window:
            windows[name] = window
    if not windows:
        return {}
    if model:
        for name, window in windows.items():
            canonical = _str_or_none(usage[name].get("canonicalModel"))
            if model in (name, canonical) or name.startswith(model):
                return {"context_window": window}
    distinct = set(windows.values())
    return {"context_window": distinct.pop()} if len(distinct) == 1 else {}


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
        self._last_usage: dict[str, object] = {}
        # The model the `init` line announced, kept only so the final
        # `result` line's per-model context windows can be told apart.
        self._model: str | None = None

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
        # Budget first: which line carries which number is the CLI's
        # business, but *what* each line's numbers mean is not
        # interchangeable, so `_usage` is told which kind it is reading.
        self._usage(message, kind)
        if kind == "system":
            self._system(message)
        elif kind == "assistant":
            self._assistant(message)
        elif kind == "user":
            self._user(message)
        elif kind == "result":
            self._result(message)
        # "stream_event" (partial deltas) and anything unknown: ignored.

    def _usage(self, message: dict[str, Any], kind: Any) -> None:
        """Emit ``provider.usage`` for what this message says about tokens.

        Two different facts come off two different lines, and conflating
        them is how this first got the number wrong:

        - An ``assistant`` line's ``usage`` describes *that one request*:
          how much context the model was just handed, and what it wrote
          back. Summing those across a turn would double-count a prompt
          that is re-sent on every request, so the occupancy reported here
          is the latest message's, not a running total.
        - The final ``result`` line states the model's context window,
          under ``modelUsage.<model>.contextWindow``. That is the provider
          stating it, which is the only reason a window may be reported at
          all -- nothing here derives one from a model name.

        Occupancy must count the cached prompt. The CLI puts almost the
        whole conversation in ``cache_read_input_tokens`` and only the
        newest slice in ``input_tokens``, so reading ``input_tokens``
        alone reports a 200k-token session as eighteen tokens.

        Subagent messages (those carrying ``parent_tool_use_id``) are a
        different conversation with its own much smaller context, and are
        skipped: letting one land would make the dial collapse whenever
        the agent delegated.
        """

        if kind == "assistant":
            if _str_or_none(message.get("parent_tool_use_id")):
                return
            fields = _assistant_usage(message)
        elif kind == "result":
            fields = _result_usage(message, self._model)
        else:
            return
        if not fields or fields == self._last_usage:
            return
        self._last_usage = fields
        emit(self._emitter, "provider.usage", **fields)

    def _system(self, message: dict[str, Any]) -> None:
        if message.get("subtype") != "init":
            return
        session_id = _str_or_none(message.get("session_id"))
        self._model = _str_or_none(message.get("model"))
        emit(
            self._emitter,
            "session.observed",
            session_id=session_id,
            model=self._model,
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
