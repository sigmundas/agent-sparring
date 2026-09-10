"""Codex CLI sparring adapter.

Behavior asserted here was verified against a real local ``codex`` install
(version 0.153.4), not assumed -- see the Stage 4 handoff for the full
capability probe. Summary of what was actually confirmed on this machine:

- ``codex exec --sandbox read-only --json --output-schema <file>
  -o <file> <prompt>`` prints one JSON object per line (JSONL) on stdout: a
  ``thread.started`` event carrying the provider-issued ``thread_id`` (used
  as this adapter's session id), zero or more ``item.started``/
  ``item.completed`` events, and a final ``turn.completed`` (or
  ``turn.failed`` on error) event. The model's final message -- validated
  against ``--output-schema`` -- is also written verbatim to the
  ``-o``/``--output-last-message`` file; that file, not re-parsed JSONL item
  text, is treated as authoritative here.
- ``--sandbox read-only`` is enforced by the OS, not merely a prompt
  instruction: a write attempt by the model's own shell tool fails with
  "operation not permitted" and no file is created, while read-only
  repository inspection (``git log``, ``git status``, ``git diff``) still
  works. This is the concrete evidence for choosing Codex CLI as the first
  local sparring provider over Claude Code CLI's ``--permission-mode``
  (a tool-gating setting inside the CLI, not an OS-enforced sandbox).
- ``codex exec resume <thread_id> ...`` continues that same session and
  reports the same ``thread_id`` back. Resuming an unknown/invalid thread id
  fails non-zero with a plain-text error on stderr (not JSON) and prints no
  ``thread.started`` event at all -- handled as a
  :class:`~agent_sparring.providers.ProviderError`, not a crash.
- ``--output-schema`` requires a strict JSON Schema: every key under
  ``properties`` must also appear in ``required`` (an optional field is
  expressed as a nullable type, not by omitting it from ``required``).
- The VS Code Codex extension was not assumed to be programmatically
  controllable and was not used or probed further once this CLI's headless
  ``exec``/``exec resume`` surface was confirmed sufficient.

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
from typing import Any, Callable

from agent_sparring.providers import ProviderError, SparringAgentResult

DEFAULT_EXECUTABLE = "codex"
DEFAULT_SANDBOX = "read-only"

# Every property must be listed in "required" (Codex's strict-schema
# requirement, verified above); the optional field is expressed as a
# nullable type rather than omitted from "required". Deliberately excludes
# a free-form "details" property: RoutingResult.details defaults to {} when
# absent from the parsed payload.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["SEND_BACK", "READY", "NEEDS_YOU", "ESCALATE"],
        },
        "summary": {"type": "string"},
        "needs_you_reason": {"type": ["string", "null"]},
    },
    "required": ["action", "summary", "needs_you_reason"],
    "additionalProperties": False,
}

Runner = Callable[[list[str], Path, "float | None"], "subprocess.CompletedProcess[str]"]


def _default_runner(
    args: list[str], cwd: Path, timeout_seconds: float | None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        stdin=subprocess.DEVNULL,
    )


@dataclass
class CodexCliAdapter:
    """Sparring adapter backed by the ``codex`` CLI's ``exec`` mode.

    Always invokes ``codex exec`` with ``--sandbox read-only`` (OS-enforced,
    see module docstring) and ``--output-schema``/``-o`` so the final
    message is a schema-validated JSON routing verdict written to a file
    this adapter controls. ``runner`` is injectable so tests can exercise
    argument-building and output-parsing without launching a real CLI
    process or model.
    """

    repo_root: Path
    executable: str = DEFAULT_EXECUTABLE
    sandbox: str = DEFAULT_SANDBOX
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    runner: Runner = field(default=_default_runner)

    # Confirmed by direct probe (see module docstring): codex exec resume
    # continues the same thread. A future provider that cannot resume should
    # say so via this flag rather than the caller discovering it by a failed
    # call.
    supports_resume: bool = True

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
        args += [
            "--sandbox",
            self.sandbox,
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        if self.model:
            args += ["--model", self.model]
        args += list(self.extra_args)
        args.append(prompt)
        return args

    def _invoke(self, prompt: str, resume_session_id: str | None) -> SparringAgentResult:
        with tempfile.TemporaryDirectory(prefix="agent-sparring-codex-") as tmp_dir:
            schema_path = Path(tmp_dir) / "verdict_schema.json"
            schema_path.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")
            output_path = Path(tmp_dir) / "last_message.txt"

            args = self._build_args(prompt, resume_session_id, schema_path, output_path)
            try:
                result = self.runner(args, self.repo_root, self.timeout_seconds)
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


__all__ = ["CodexCliAdapter", "DEFAULT_EXECUTABLE", "DEFAULT_SANDBOX", "VERDICT_SCHEMA"]
