"""Codex CLI sparring adapter.

Behavior asserted here was verified against a real local ``codex`` install
(version 0.153.4), not assumed -- see the Stage 4 handoff for the full
capability probe. Summary of what was actually confirmed on this machine:

- ``codex exec ... --json --output-schema <file> -o <file> <prompt>``
  prints one JSON object per line (JSONL) on stdout: a ``thread.started``
  event carrying the provider-issued ``thread_id`` (used as this adapter's
  session id), zero or more ``item.started``/``item.completed`` events,
  and a final ``turn.completed`` (or ``turn.failed`` on error) event. The
  model's final message -- validated against ``--output-schema`` -- is
  also written verbatim to the ``-o``/``--output-last-message`` file; that
  file, not re-parsed JSONL item text, is treated as authoritative here.
- A read-only sandbox is enforced by the OS, not merely a prompt
  instruction: a write attempt by the model's own shell tool fails with
  "operation not permitted" and no file is created, while read-only
  repository inspection (``git log``, ``git status``, ``git diff``) still
  works. This was first confirmed with the top-level ``--sandbox
  read-only`` flag on a fresh ``codex exec`` run; the third bullet below
  explains why this adapter actually uses a different mechanism
  (``-c sandbox_mode=...``) so the same guarantee also holds on resume.
  This OS-level enforcement is the concrete evidence for choosing Codex
  CLI as the first local sparring provider over Claude Code CLI's
  ``--permission-mode`` (a tool-gating setting inside the CLI, not an
  OS-enforced sandbox).
- ``codex exec resume <thread_id> ...`` continues that same session and
  reports the same ``thread_id`` back. Resuming an unknown/invalid thread id
  fails non-zero with a plain-text error on stderr (not JSON) and prints no
  ``thread.started`` event at all -- handled as a
  :class:`~agent_sparring.providers.ProviderError`, not a crash.
- ``--output-schema`` requires a strict JSON Schema: every key under
  ``properties`` must also appear in ``required`` (an optional field is
  expressed as a nullable type, not by omitting it from ``required``).
- ``codex exec resume`` does **not** accept the top-level ``--sandbox``
  flag at all (``codex exec resume --help`` never lists it, and passing it
  is a hard CLI parse error: "unexpected argument '--sandbox' found").
  Worse, a live end-to-end smoke test during Stage 4's review round showed
  that a resumed session *without* any sandbox override defaults to a
  writable sandbox -- a real write via the model's shell tool succeeded
  after ``codex exec resume <id> --json ...`` with no ``--sandbox``. The
  fix, also verified live: ``-c sandbox_mode="read-only"`` (a config
  override, not the ``--sandbox`` flag) is accepted by *both* ``codex exec``
  and ``codex exec resume`` and enforces the same OS-level read-only
  sandbox in both. This adapter therefore always passes
  ``-c sandbox_mode="<value>"``, never ``--sandbox``, for both start and
  resume.
- The VS Code Codex extension was not assumed to be programmatically
  controllable and was not used or probed further once this CLI's headless
  ``exec``/``exec resume`` surface was confirmed sufficient.

This adapter always runs with ``-c sandbox_mode="read-only"``: that is what
makes it a genuinely read-only sparrer rather than a prompt-only promise
(see above -- a write attempt actually fails at the OS level). A writable
sandbox would turn the sparrer into a contributor to the candidate, which
per the project plan's independence principle forfeits that sparrer's
authority to give independent acceptance -- a distinct, not-yet-built mode.

For this adapter, read-only is an invariant, not a configuration option:
there is no ``sandbox`` constructor field, no runtime-mutable attribute,
and no ``extra_args`` passthrough that could inject a different sandbox or
an arbitrary config override. (The one other ``-c`` this adapter can emit
is ``model_reasoning_effort``, whose value is restricted to
:data:`EFFORT_LEVELS`; it cannot name another key and cannot express a
sandbox.) The verified read-only setting is hard-coded directly
into argument-building (see ``_SANDBOX_CONFIG_ARG`` below) with no
supported path -- constructor, attribute mutation after construction, or
otherwise -- to select anything else. A future contributor-sparrer mode
(one that legitimately edits and forfeits independent acceptance
authority) would be a distinct adapter/mode, not a flag on this one.

Activity stream: the same ``--json`` JSONL this adapter already parses for
its final result is also consumed line by line while ``codex`` runs, and
translated into observational events (see :class:`_CodexStreamTranslator`
and :mod:`agent_sparring.activity`). Item vocabulary follows Codex's
``exec_events`` (``command_execution``, ``file_change``, ``mcp_tool_call``,
``collab_tool_call``, ``web_search``, ``agent_message``, ``reasoning``,
``todo_list``, ``error``; the installed 0.153.4 binary carries all of these
names). Only semantic facts are recorded: never ``reasoning``/
``agent_message`` text, ``aggregated_output``, command strings, MCP
arguments/results, or raw events. The final parse below is unchanged and
does not depend on anything the translator does.

This module is the only place that knows any of the above. Generic
orchestration code talks to the :class:`~agent_sparring.providers.
SparringAgentAdapter` protocol, not to ``codex`` flags directly.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.activity import ActivityEmitter, emit, repo_relative_path
from agent_sparring.deferred_gate import CHECKPOINTS
from agent_sparring.human_gate import HUMAN_GATE_CATEGORIES
from agent_sparring.providers import ProviderError, Runner, SparringAgentResult
from agent_sparring.providers.subprocess_runner import LineSink, run_streaming

DEFAULT_EXECUTABLE = "codex"
DEFAULT_SANDBOX = "read-only"
PROVIDER_ID = "codex-cli"
# Human-readable name for this provider. The engine keeps PROVIDER_ID as the
# canonical identifier everywhere; this is only for showing a person.
DISPLAY_NAME = "Codex"

# Verified against the installed CLI (codex-cli 0.153.4): "codex exec" has no
# --effort flag at all. Reasoning effort is the "model_reasoning_effort"
# config key, set with the same "-c key=value" override mechanism this
# adapter already uses for the read-only sandbox, and accepted by both
# "codex exec" and "codex exec resume". The vocabulary below was read out of
# the installed binary and cross-checked against "codex debug models", whose
# catalog reports each model's "supported_reasoning_levels" as a subset of
# it. That per-model subset is deliberately not enforced here: it moves with
# the catalog, and the provider is the authority on its own models.
EFFORT_LEVELS: tuple[str, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

# Verified live (see module docstring): "codex exec resume" rejects the
# top-level --sandbox flag outright, and without any override does not
# inherit the original session's read-only sandbox either -- a real write
# succeeded. "-c sandbox_mode=..." is accepted by, and verified to enforce
# read-only on, both "codex exec" and "codex exec resume". This is the
# fixed, hard-coded config arg pair this adapter always passes; there is
# no field or parameter anywhere in this class that can change it.
_SANDBOX_CONFIG_ARG: tuple[str, str] = ("-c", f'sandbox_mode="{DEFAULT_SANDBOX}"')

# Every property must be listed in "required" (Codex's strict-schema
# requirement, verified above); an optional field is expressed as a
# nullable type rather than omitted from "required". "findings"/"deferred"
# carry the detailed human-readable prose sparring.md needs (a real
# technical explanation for SEND_BACK/NEEDS_YOU/ESCALATE, or a useful READY
# rationale) -- kept separate from the tiny routing verdict
# (action/summary/needs_you_reason) so RoutingResult itself stays tiny; see
# agent_sparring.sparring_agent._build_routing_result /
# _extract_findings, which split this envelope back apart.
#
# "human_gate" is the one structured (non-prose) addition: the closed list
# of what a human must complete before the stage can be READY. It is
# nullable at the schema level because it must be null for SEND_BACK /
# READY / ESCALATE; the "NEEDS_YOU implies a gate" half of the contract is
# stated in the prompt and enforced by RoutingResult, since a JSON schema
# cannot express "required only when another field has a given value"
# without a conditional Codex's strict mode does not accept.
_HUMAN_GATE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "category": {
            "type": "string",
            "enum": list(HUMAN_GATE_CATEGORIES),
        },
        "title": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "instruction": {"type": "string"},
                    "pass_criteria": {"type": "string"},
                    "source": {"type": ["string", "null"]},
                },
                "required": ["id", "instruction", "pass_criteria", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["category", "title", "checks"],
    "additionalProperties": False,
}

# The deferred counterpart. Same checks, plus the reviewer's rationale and
# the checkpoint by which the answer is owed. Nullable at the schema level
# for the same reason the immediate gate is -- it is legal only with READY,
# and that half of the contract is stated in the prompt and enforced by
# RoutingResult (see agent_sparring.deferred_gate).
_DEFERRED_GATE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "category": {"type": "string", "enum": list(HUMAN_GATE_CATEGORIES)},
        "title": {"type": "string"},
        "checks": _HUMAN_GATE_SCHEMA["properties"]["checks"],
        "rationale": {"type": "string"},
        "checkpoint": {"type": "string", "enum": list(CHECKPOINTS)},
    },
    "required": ["category", "title", "checks", "rationale", "checkpoint"],
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["SEND_BACK", "READY", "NEEDS_YOU", "ESCALATE"],
        },
        "summary": {"type": "string"},
        "needs_you_reason": {"type": ["string", "null"]},
        "findings": {"type": "string"},
        "deferred": {"type": ["string", "null"]},
        "human_gate": _HUMAN_GATE_SCHEMA,
        "deferred_human_gate": _DEFERRED_GATE_SCHEMA,
        "promote_deferred": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "action",
        "summary",
        "needs_you_reason",
        "findings",
        "deferred",
        "human_gate",
        "deferred_human_gate",
        "promote_deferred",
    ],
    "additionalProperties": False,
}

def _default_runner(
    args: list[str], cwd: Path, timeout_seconds: float | None, on_line: LineSink | None
) -> "subprocess.CompletedProcess[str]":
    return run_streaming(args, cwd, timeout_seconds, on_line, stdin_devnull=True)


def _int_in(payload: Any, *keys: str) -> int | None:
    """A non-negative integer at ``payload[keys[0]][keys[1]]...``, or None.

    Tolerant on purpose: this reads another program's JSON, where a field
    may be absent, null, nested one level deeper than last version, or a
    string. Anything that is not a plain non-negative integer is treated as
    "not stated", because a wrong number shown as a budget is worse than no
    number at all.
    """

    for key in keys:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(key)
    if isinstance(payload, bool) or not isinstance(payload, int):
        return None
    return payload if payload >= 0 else None


def _percent_in(payload: Any, *keys: str) -> int | None:
    """A 0-100 percentage, rounded, or None. Floats are expected here."""

    for key in keys:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(key)
    if isinstance(payload, bool) or not isinstance(payload, (int, float)):
        return None
    if not 0 <= payload <= 100:
        return None
    return round(payload)


def _budget_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """What one Codex stream event says about tokens, context and limits.

    Keyed on where the numbers are rather than on the event's ``type``,
    which differs between the ``codex exec --json`` stream and Codex's own
    session log and has moved between releases. Both shapes seen in the
    wild are read: ``usage`` on a completed turn, and ``info`` carrying
    cumulative totals with the context window beside them.

    Returns only the fields actually present. An empty result means this
    event said nothing about budget, which is the common case.
    """

    fields: dict[str, Any] = {}
    # Cumulative first, so a per-response `usage` cannot overwrite it: the
    # context dial is about how full the session is, not about one reply.
    for source in (event.get("info"), event):
        if not isinstance(source, Mapping):
            continue
        for container in ("total_token_usage", "usage"):
            usage = source.get(container)
            if not isinstance(usage, Mapping):
                continue
            for field, key in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = _int_in(usage, key)
                if value is not None:
                    fields.setdefault(field, value)
        window = _int_in(source, "model_context_window")
        if window:
            fields.setdefault("context_window", window)

    limits = event.get("rate_limits")
    if not isinstance(limits, Mapping):
        limits = event.get("info", {})
        limits = limits.get("rate_limits") if isinstance(limits, Mapping) else None
    if isinstance(limits, Mapping):
        for field, (group, key) in (
            ("rate_limit_percent", ("primary", "used_percent")),
            ("rate_limit_window_minutes", ("primary", "window_minutes")),
            ("rate_limit_secondary_percent", ("secondary", "used_percent")),
            ("rate_limit_secondary_window_minutes", ("secondary", "window_minutes")),
        ):
            value = (
                _percent_in(limits, group, key)
                if key == "used_percent"
                else _int_in(limits, group, key)
            )
            if value is not None:
                fields[field] = value
    return fields


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class _CodexStreamTranslator:
    """Turns ``codex exec --json`` lines into activity events.

    Observational only. Emits nothing for ``reasoning``, ``agent_message``,
    ``todo_list`` and ``item.updated`` progress, and never reads
    ``aggregated_output``, ``command``, ``arguments``, ``result``, ``prompt``
    or any error message text. File paths are persisted repo-relative via
    :func:`agent_sparring.activity.repo_relative_path`; a path outside
    ``repo_root`` is omitted, never leaked.
    """

    def __init__(self, emitter: ActivityEmitter | None, repo_root: Path) -> None:
        self._emitter = emitter
        self._repo_root = repo_root
        self._started_items: set[str] = set()
        self._last_usage: dict[str, Any] = {}

    def feed(self, line: str) -> None:
        if self._emitter is None:
            return
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        # Budget first, and on *every* event: which event type carries the
        # numbers is the provider's business and has changed between
        # versions, so this looks for the numbers rather than for a type
        # name it would have to guess right. An event that carries none
        # emits nothing.
        self._usage(event)
        kind = event.get("type")
        if kind == "thread.started":
            emit(
                self._emitter,
                "session.observed",
                session_id=_str_or_none(event.get("thread_id")),
            )
        elif kind in ("item.started", "item.completed"):
            item = event.get("item")
            if isinstance(item, dict):
                self._item(kind, item)
        elif kind == "turn.completed":
            emit(self._emitter, "provider.result", summary=self._usage_summary(event))
        elif kind in ("turn.failed", "error"):
            # The provider reported a failure. Its message text is not
            # recorded here; the adapter's final parse raises with it.
            emit(self._emitter, "provider.error")

    def _usage(self, event: dict[str, Any]) -> None:
        """Emit ``provider.usage`` for whatever this event says about budget.

        Codex reports tokens on a completed turn, and — in the versions
        that do — the model's context window and the account's rate-limit
        windows alongside them. All of it is optional and all of it is
        quoted rather than derived: nothing here fills in a context window
        from the model name, because a guessed denominator would turn an
        unknown into a number a person would act on.

        Re-emitted only when something changed, since the same totals
        arriving twice is not an event. The log is telemetry and no
        orchestration reads it, so a dropped or duplicated line costs
        nothing but noise.
        """

        fields = _budget_fields(event)
        if not fields or fields == self._last_usage:
            return
        self._last_usage = fields
        emit(self._emitter, "provider.usage", **fields)

    @staticmethod
    def _usage_summary(event: dict[str, Any]) -> str | None:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        parts = []
        for key, label in (("input_tokens", "in"), ("output_tokens", "out")):
            value = usage.get(key)
            if isinstance(value, int):
                parts.append(f"{label}={value}")
        return f"tokens {' '.join(parts)}" if parts else None

    def _item(self, kind: str, item: dict[str, Any]) -> None:
        item_type = item.get("type")
        item_id = _str_or_none(item.get("id")) or ""
        started = kind == "item.started"
        completed = kind == "item.completed"

        if item_type == "command_execution":
            if started:
                self._started_items.add(item_id)
                emit(self._emitter, "command.started", tool="shell")
            elif completed:
                exit_code = item.get("exit_code")
                emit(
                    self._emitter,
                    "command.finished",
                    tool="shell",
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                )
        elif item_type == "file_change":
            if not completed:
                return
            changes = item.get("changes")
            if not isinstance(changes, list):
                return
            for change in changes:
                if not isinstance(change, dict):
                    continue
                emit(
                    self._emitter,
                    "file.changed",
                    path=repo_relative_path(change.get("path"), self._repo_root),
                    kind=_str_or_none(change.get("kind")),
                )
        elif item_type == "mcp_tool_call":
            if started or (completed and item_id not in self._started_items):
                self._started_items.add(item_id)
                server = _str_or_none(item.get("server"))
                tool = _str_or_none(item.get("tool"))
                name = f"{server}:{tool}" if server and tool else (tool or server)
                emit(self._emitter, "tool.call", tool=name)
        elif item_type == "collab_tool_call":
            # Codex's own multi-agent item: the provider states that a
            # collaboration tool ran. Only the tool name is recorded (never
            # the prompt or agent states).
            if started or (completed and item_id not in self._started_items):
                self._started_items.add(item_id)
                emit(self._emitter, "subagent.started", tool=_str_or_none(item.get("tool")))
        elif item_type == "web_search":
            if started or (completed and item_id not in self._started_items):
                self._started_items.add(item_id)
                emit(self._emitter, "tool.call", tool="web_search")
        # agent_message, reasoning, todo_list, error: deliberately ignored.


@dataclass
class CodexCliAdapter:
    """Sparring adapter backed by the ``codex`` CLI's ``exec`` mode.

    Always invokes ``codex exec`` with the fixed, hard-coded
    ``-c sandbox_mode="read-only"`` config override (OS-enforced, see
    module docstring) and ``--output-schema``/``-o`` so the final message
    is a schema-validated JSON routing verdict written to a file this
    adapter controls. There is deliberately no ``sandbox`` field and no
    ``extra_args`` passthrough on this class: for this sparring adapter,
    read-only is an invariant, not something a caller can configure,
    mutate after construction, or bypass by injecting arbitrary
    Codex/config flags. ``runner`` is injectable so tests can exercise
    argument-building and output-parsing without launching a real CLI
    process or model.
    """

    repo_root: Path
    executable: str = DEFAULT_EXECUTABLE
    # Typed provider-turn configuration, already resolved by
    # agent_sparring.agent_config. ``None`` means "pass no flag": the CLI
    # then picks its own model/effort, and the engine claims nothing about
    # which one that is. ``effort`` becomes a "-c model_reasoning_effort"
    # override, which is a config override like the sandbox one -- but a
    # closed, validated enum (see EFFORT_LEVELS and __post_init__), not an
    # arbitrary key=value passthrough: no value of this field can name a
    # different config key or touch the sandbox.
    model: str | None = None
    effort: str | None = None
    timeout_seconds: float | None = None
    runner: Runner = field(default=_default_runner)
    # Optional observational emitter; None means no telemetry, nothing else
    # changes. Never consulted by the final parse.
    activity: ActivityEmitter | None = None

    provider_id: str = PROVIDER_ID

    # Confirmed by direct probe (see module docstring): codex exec resume
    # continues the same thread. A future provider that cannot resume should
    # say so via this flag rather than the caller discovering it by a failed
    # call.
    supports_resume: bool = True

    def __post_init__(self) -> None:
        # Refuse an unsupported effort at construction, which is always
        # before a provider process exists, and which also guarantees the
        # "-c model_reasoning_effort=..." value below is one of a closed set
        # of bare words rather than anything a caller composed.
        if self.effort is not None and self.effort not in EFFORT_LEVELS:
            raise ProviderError(
                f"effort {self.effort!r} is not supported by {PROVIDER_ID}; "
                f"supported levels: {', '.join(EFFORT_LEVELS)}"
            )

    def start(self, prompt: str) -> SparringAgentResult:
        return self._invoke(prompt, resume_session_id=None)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ProviderError(
                "resume requires a non-empty session_id obtained from a prior "
                "provider result, not an empty/invented value"
            )
        return self._invoke(prompt, resume_session_id=session_id)

    # -- internals ----------------------------------------------------

    def _build_args(
        self,
        prompt: str,
        resume_session_id: str | None,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        args = [self.executable, "exec"]
        if resume_session_id:
            args += ["resume", resume_session_id]
        # _SANDBOX_CONFIG_ARG is the fixed, hard-coded read-only config
        # override (see module docstring for why it is "-c sandbox_mode=..."
        # and not --sandbox). There is no field on this class that can
        # change it, and no extra_args passthrough that could append a
        # conflicting override after it.
        args += list(_SANDBOX_CONFIG_ARG)
        if self.effort:
            # Codex has no --effort flag; the setting is a config override.
            # The value is one of EFFORT_LEVELS (enforced in __post_init__),
            # so this can never become a second sandbox override. It is
            # appended after the sandbox arg, but a later "-c" for a
            # different key does not disturb it.
            args += ["-c", f'model_reasoning_effort="{self.effort}"']
        args += [
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        if self.model:
            args += ["--model", self.model]
        args.append(prompt)
        return args

    def _invoke(self, prompt: str, resume_session_id: str | None) -> SparringAgentResult:
        with tempfile.TemporaryDirectory(prefix="agent-sparring-codex-") as tmp_dir:
            schema_path = Path(tmp_dir) / "verdict_schema.json"
            schema_path.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")
            output_path = Path(tmp_dir) / "last_message.txt"

            args = self._build_args(prompt, resume_session_id, schema_path, output_path)
            translator = _CodexStreamTranslator(self.activity, self.repo_root)
            try:
                result = self.runner(
                    args, self.repo_root, self.timeout_seconds, translator.feed
                )
            except OSError as exc:
                raise ProviderError(f"could not launch {self.executable!r}: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"{self.executable} timed out after {self.timeout_seconds}s"
                ) from exc
            return self._parse(result, output_path)

    def _parse(
        self, result: "subprocess.CompletedProcess[str]", output_path: Path
    ) -> SparringAgentResult:
        stdout = result.stdout or ""
        thread_id: str | None = None
        turn_failed_message: str | None = None
        events: list[dict[str, Any]] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            event_type = event.get("type")
            if event_type == "thread.started":
                candidate = event.get("thread_id")
                if isinstance(candidate, str) and candidate:
                    thread_id = candidate
            elif event_type == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    turn_failed_message = str(error.get("message") or error)
                else:
                    turn_failed_message = str(error)

        if thread_id is None:
            # Verified above: e.g. resuming an unknown thread id prints a
            # plain-text error to stderr and no JSONL at all.
            detail = (result.stderr or "").strip() or stdout.strip() or "(no output)"
            raise ProviderError(
                f"{self.executable} exec produced no thread.started event with a "
                f"usable thread_id (exit {result.returncode}): {detail}"
            )

        if turn_failed_message is not None:
            raise ProviderError(f"{self.executable} exec turn failed: {turn_failed_message}")

        if result.returncode != 0:
            detail = (result.stderr or "").strip() or "(no stderr)"
            raise ProviderError(
                f"{self.executable} exec exited {result.returncode} without a "
                f"turn.failed event: {detail}"
            )

        try:
            text = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderError(
                f"{self.executable} exec did not write its output-last-message "
                f"file {output_path}: {exc}"
            ) from exc
        if not text:
            raise ProviderError(
                f"{self.executable} exec wrote an empty output-last-message file "
                f"{output_path}"
            )

        return SparringAgentResult(
            session_id=thread_id,
            text=text,
            is_error=False,
            raw={"events": events},
        )


__all__ = [
    "CodexCliAdapter",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_SANDBOX",
    "DISPLAY_NAME",
    "EFFORT_LEVELS",
    "PROVIDER_ID",
    "VERDICT_SCHEMA",
]
