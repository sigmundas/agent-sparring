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
it captures the repository's current branch, HEAD commit, and working-tree
status before and after the provider turn and refuses the run if any of
those changed, so a provider without real OS-level enforcement (or a future
provider that ignores its own read-only flag) cannot silently become a
contributor to the candidate, and cannot silently review a different branch
than the one the caller was told to expect.

The provider's final structured message is expected to carry both the tiny
routing verdict (``action``/``summary``/``needs_you_reason``) and separate
human-readable ``findings``/``deferred`` prose (see
:mod:`agent_sparring.sparring_prompt`'s verdict instructions and
:mod:`agent_sparring.providers.codex_cli`'s ``VERDICT_SCHEMA``).
:class:`~agent_sparring.routing.RoutingResult` itself stays tiny -- the
detailed findings are passed to :func:`~agent_sparring.sparring_exchange.
record_sparring` separately, never folded into routing/workflow state.
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


def _repo_fingerprint(repo_root: Path) -> tuple[str, str, tuple[str, ...]]:
    """A cheap (branch, head commit, dirty paths) snapshot used to detect
    writes or a branch switch.

    Not a cryptographic guarantee -- a provider could in principle modify
    and then restore files without changing any of these -- but it catches
    the ordinary case (a stray commit, an edited/added/deleted file, a
    branch checkout) that a misbehaving or misconfigured provider would
    actually produce.
    """

    branch = current_branch(repo_root)
    head = resolve_commit(repo_root, "HEAD", label="sparring HEAD")
    dirty = dirty_paths(repo_root)
    return branch, head, dirty


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


def _build_routing_result(payload: dict[str, Any]) -> RoutingResult:
    """Construct the tiny :class:`RoutingResult` from the provider's verdict
    envelope, deliberately dropping ``findings``/``deferred`` -- those are
    human-readable prose (see :func:`_extract_findings`), never folded into
    routing/workflow state. ``deferred``, if present and non-empty, is
    carried through ``details["deferred"]`` -- the same existing
    human-readable path :func:`agent_sparring.sparring_exchange.
    render_sparring` already reads for its ``## Deferred`` section.
    """

    routing_payload: dict[str, Any] = {
        "action": payload.get("action"),
        "summary": payload.get("summary"),
        "needs_you_reason": payload.get("needs_you_reason"),
    }
    deferred = payload.get("deferred")
    if deferred:
        routing_payload["details"] = {"deferred": deferred}
    return RoutingResult.from_dict(routing_payload)


def _extract_findings(payload: dict[str, Any]) -> str:
    """The provider's detailed human-readable findings/discussion text.

    This -- not the tiny routing summary -- is what carries a real
    technical explanation for SEND_BACK/NEEDS_YOU/ESCALATE or a useful
    READY rationale into ``sparring.md``'s "Finding / discussion" section.
    """

    findings = payload.get("findings")
    if not isinstance(findings, str):
        raise RoutingResultError(
            f"provider's final message is missing a usable 'findings' string: {payload!r}"
        )
    return findings


def _parse_verdict(text: str) -> tuple[RoutingResult, str]:
    payload = _parse_verdict_payload(text)
    routing = _build_routing_result(payload)
    findings = _extract_findings(payload)
    return routing, findings


def run_sparring_agent(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    adapter: SparringAgentAdapter,
    *,
    expected_branch: str,
) -> SparringAgentRunResult:
    """Start or resume the sparring agent for one turn.

    ``expected_branch`` is required, mirroring
    :func:`agent_sparring.stage_agent.run_stage_agent`: a local
    repo-aware sparring run must know which branch it is meant to review.
    The worktree is checked against it before the provider is invoked (a
    mismatch refuses the run without invoking the provider at all) and
    again afterward as part of the read-only integrity check below --
    unlike the implementation-side branch guard, this only verifies exact
    branch identity and does not ban reviewing ``main``/``master``, since
    the sparrer never writes.

    Records the provider's own returned session id in state.json and
    refuses to silently replace it if a resume call returns a different id
    (mirrors :func:`agent_sparring.stage_agent.run_stage_agent`). Parses the
    provider's final message into the tiny structured routing verdict plus
    separate human-readable findings, and (re)writes ``sparring.md`` from
    both.

    Raises :class:`SparringAgentRunError` if: ``expected_branch`` is
    missing/blank; the worktree is not on ``expected_branch`` before the
    turn (provider is never invoked in this case); the provider itself
    fails; the provider turn left the repository modified or switched
    branches (read-only contract violated -- the sparring result is not
    recorded in this case); the provider's final message is not a usable
    routing verdict; or, on resume, the provider returns a different
    session id than the one it was asked to resume.
    """

    if not expected_branch or not expected_branch.strip():
        raise SparringAgentRunError(
            "expected_branch is required for a sparring-agent run; a local "
            "repo-aware sparring run must know which branch it is meant to review"
        )

    state = stage.read_state()
    resume_id = state.sparring_session_id

    prompt = build_sparring_prompt(
        stage, sparring_dir, resume=resume_id is not None, expected_branch=expected_branch
    )

    try:
        before_branch, before_head, before_dirty = _repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    if before_branch != expected_branch:
        raise SparringAgentRunError(
            f"expected branch {expected_branch!r} but {repo_root} is on "
            f"{before_branch!r}; refusing to spar against the wrong branch"
        )

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
        after_branch, after_head, after_dirty = _repo_fingerprint(repo_root)
    except GitContextError as exc:
        raise SparringAgentRunError(str(exc)) from exc

    if (after_branch, after_head, after_dirty) != (before_branch, before_head, before_dirty):
        raise SparringAgentRunError(
            "sparring turn modified the repository or switched branches "
            f"(read-only contract violated): branch {before_branch!r} -> "
            f"{after_branch!r}, HEAD {before_head} -> {after_head}, dirty "
            f"paths {before_dirty!r} -> {after_dirty!r}"
        )

    try:
        routing, findings_text = _parse_verdict(result.text)
    except RoutingResultError as exc:
        raise SparringAgentRunError(
            f"provider's final message was not a usable routing verdict: {exc}"
        ) from exc

    # Both the integrity check above and the session-id record below only
    # happen once the turn is confirmed read-only/on-branch and the verdict
    # parses; a failed/refused turn must not record a session id or
    # overwrite sparring.md.
    state.sparring_session_id = result.session_id
    stage.write_state(state)

    sparring_content = record_sparring(stage, routing, findings=findings_text)

    return SparringAgentRunResult(
        result=result,
        routing=routing,
        resumed=resume_id is not None,
        prompt=prompt,
        sparring=sparring_content,
    )


__all__ = ["SparringAgentRunError", "SparringAgentRunResult", "run_sparring_agent"]
