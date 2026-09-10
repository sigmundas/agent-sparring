"""Sparring-agent run orchestration.

Mirrors :mod:`agent_sparring.stage_agent`'s start-vs-resume selection and
session-id bookkeeping, but for the independent sparring side: fresh
sparring context for a new stage, resume of the same sparring context after
a SEND_BACK correction cycle (same underlying provider session/thread, not a
fresh one). Parses the provider's final structured message into a
:class:`~agent_sparring.routing.RoutingResult` and (re)writes
``sparring.md`` from it. This module does not implement any provider itself
(see :mod:`agent_sparring.providers`) and does not implement the unattended
stage<->sparring loop or acceptance logic -- those are later stages.

Per the project plan, "Sparring is read-only by default." A provider
adapter is expected to invoke its underlying tool in a read-only mode where
one exists (see :mod:`agent_sparring.providers.codex_cli` for the concrete
evidence backing that choice), but this module does not merely trust that:
it captures the repository's HEAD commit and working-tree status before and
after the provider turn and refuses the run if either changed, so a
provider without real OS-level enforcement (or a future provider that
ignores its own read-only flag) cannot silently become a contributor to the
candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_sparring.git_context import (
    GitContextError,
    current_branch,
    dirty_paths,
    resolve_commit,
)
from agent_sparring.providers import ProviderError, SparringAgentAdapter, SparringAgentResult
from agent_sparring.routing import RoutingResult, RoutingResultError
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.sparring_prompt import build_sparring_prompt
from agent_sparring.stage import Stage


class SparringAgentRunError(RuntimeError):
    """Raised when a sparring-agent run is refused or the provider fails."""


@dataclass(frozen=True)
class SparringAgentRunResult:
    result: SparringAgentResult
    routing: RoutingResult
    resumed: bool
    prompt: str
    sparring: str


def _repo_fingerprint(repo_root: Path) -> tuple[str, tuple[str, ...]]:
    """A cheap (head commit, dirty paths) snapshot used to detect writes.

    Not a cryptographic guarantee -- a provider could in principle modify
    and then restore files without changing either -- but it catches the
    ordinary case (a stray commit, an edited/added/deleted file) that a
    misbehaving or misconfigured provider would actually produce.
    """

    head = resolve_commit(repo_root, "HEAD", label="sparring HEAD")
    dirty = dirty_paths(repo_root)
    return head, dirty


def _parse_verdict_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoutingResultError(
            f"provider's final message is not valid JSON: {text!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise RoutingResultError(f"provider's final message is not a JSON object: {payload!r}")
    return payload


def run_sparring_agent(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    adapter: SparringAgentAdapter,
    *,
    expected_branch: str | None = None,
) -> SparringAgentRunResult:
    """Start or resume the sparring agent for one turn.

    Records the provider's own returned session id in state.json and
    refuses to silently replace it if a resume call returns a different id
    (mirrors :func:`agent_sparring.stage_agent.run_stage_agent`). Parses the
    provider's final message as the structured routing verdict and
    (re)writes ``sparring.md`` from it.

    Raises :class:`SparringAgentRunError` if: HEAD is detached; the
    provider itself fails; the provider turn left the repository modified
    (read-only contract violated); the provider's final message is not a
    usable routing verdict; or, on resume, the provider returns a different
    session id than the one it was asked to resume.
    """

    try:
        current_branch(repo_root)  # fail loudly on detached HEAD, as elsewhere
    except GitContextError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    state = stage.read_state()
    resume_id = state.sparring_session_id

    prompt = build_sparring_prompt(
        stage, sparring_dir, resume=resume_id is not None, expected_branch=expected_branch
    )

    try:
        before_head, before_dirty = _repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    try:
        if resume_id:
            result = adapter.resume(resume_id, prompt)
        else:
            result = adapter.start(prompt)
    except ProviderError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    if resume_id and result.session_id != resume_id:
        raise SparringAgentRunError(
            f"provider was asked to resume session {resume_id!r} but returned "
            f"session {result.session_id!r}; refusing to silently replace the "
            "recorded sparring session identity"
        )

    try:
        after_head, after_dirty = _repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    if after_head != before_head or after_dirty != before_dirty:
        raise SparringAgentRunError(
            "sparring turn modified the repository (read-only contract "
            f"violated): HEAD {before_head} -> {after_head}, dirty paths "
            f"{before_dirty!r} -> {after_dirty!r}"
        )

    try:
        routing = RoutingResult.from_dict(_parse_verdict_payload(result.text))
    except RoutingResultError as exc:
        raise SparringAgentRunError(
            f"provider's final message was not a usable routing verdict: {exc}"
        ) from exc

    # Both the fingerprint check above and the session-id record below only
    # happen once the turn is confirmed read-only and the verdict parses;
    # a failed/refused turn must not record a session id or overwrite
    # sparring.md.
    state.sparring_session_id = result.session_id
    stage.write_state(state)

    sparring_content = record_sparring(stage, routing, findings=result.text)

    return SparringAgentRunResult(
        result=result,
        routing=routing,
        resumed=resume_id is not None,
        prompt=prompt,
        sparring=sparring_content,
    )


__all__ = ["SparringAgentRunError", "SparringAgentRunResult", "run_sparring_agent"]
