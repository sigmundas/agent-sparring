"""Claude Code CLI stage-agent adapter.

Behavior asserted here was verified against a real local ``claude`` install
(version 2.1.236), not assumed:

- ``claude -p <prompt> --output-format json`` prints one JSON object on
  stdout containing a provider-issued ``session_id`` plus ``result`` (the
  final text) and ``is_error``.
- ``claude -p ... --resume <session_id>`` continues that same session and
  the JSON output reports the same ``session_id`` back.
- Resuming an unknown/invalid session id exits non-zero and prints a plain
  text error (not JSON) on stdout — this is handled as a
  :class:`~agent_sparring.providers.ProviderError`, not a crash.

This module is the only place that knows any of the above. Generic
orchestration code talks to the :class:`~agent_sparring.providers.
StageAgentAdapter` protocol, not to ``claude`` flags directly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agent_sparring.providers import ProviderError, StageAgentResult

DEFAULT_EXECUTABLE = "claude"
DEFAULT_PERMISSION_MODE = "acceptEdits"

Runner = Callable[[list[str], Path, "float | None"], "subprocess.CompletedProcess[str]"]


def _default_runner(
    args: list[str], cwd: Path, timeout_seconds: float | None
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=False, timeout=timeout_seconds
    )


@dataclass
class ClaudeCliAdapter:
    """Stage-agent adapter backed by the ``claude`` CLI in print mode.

    ``runner`` is injectable so tests can exercise argument-building and
    output-parsing without launching a real CLI process or model.
    """

    repo_root: Path
    executable: str = DEFAULT_EXECUTABLE
    permission_mode: str = DEFAULT_PERMISSION_MODE
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    runner: Runner = field(default=_default_runner)

    # Claude Code CLI supports both a fresh session and resuming a prior one
    # by its own session id (see module docstring); a future provider that
    # cannot resume should say so via this flag rather than the caller
    # discovering it by a failed call.
    supports_resume: bool = True

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
            "json",
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            args += ["--model", self.model]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        args += list(self.extra_args)
        return args

    def _invoke(self, prompt: str, resume_session_id: str | None) -> StageAgentResult:
        args = self._build_args(prompt, resume_session_id)
        try:
            result = self.runner(args, self.repo_root, self.timeout_seconds)
        except OSError as exc:
            raise ProviderError(f"could not launch {self.executable!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"{self.executable} timed out after {self.timeout_seconds}s"
            ) from exc
        return self._parse(result)

    def _parse(self, result: "subprocess.CompletedProcess[str]") -> StageAgentResult:
        stdout = result.stdout or ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            detail = stdout.strip() or (result.stderr or "").strip() or "(no output)"
            raise ProviderError(
                f"{self.executable} did not return JSON (exit {result.returncode}): {detail}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.executable} returned non-object JSON: {payload!r}")

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


__all__ = ["ClaudeCliAdapter", "DEFAULT_EXECUTABLE", "DEFAULT_PERMISSION_MODE"]
