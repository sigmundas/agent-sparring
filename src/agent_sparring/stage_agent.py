"""Stage-agent run orchestration.

Ties together the branch guard, the worktree implementation lock, bounded
prompt assembly, and the provider adapter for one stage-agent turn: fresh
session for a new stage, resume of the same session for a SEND_BACK
follow-up. This module owns start-vs-resume selection and session-id
bookkeeping in state.json, and regenerates handoff.md after a successful
turn. It does not implement any provider itself (see
:mod:`agent_sparring.providers`) and does not decide sparring routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_sparring.branch_guard import BranchGuardError, ensure_branch_for_unattended_run
from agent_sparring.concurrency import WorktreeLockError, worktree_lock
from agent_sparring.git_context import GitContextError
from agent_sparring.handoff import generate_handoff
from agent_sparring.providers import ProviderError, StageAgentAdapter, StageAgentResult
from agent_sparring.stage import Stage
from agent_sparring.stage_prompt import build_stage_prompt


class StageAgentRunError(RuntimeError):
    """Raised when a stage-agent run is refused or the provider fails."""


@dataclass(frozen=True)
class StageAgentRunResult:
    result: StageAgentResult
    resumed: bool
    branch: str
    prompt: str
    handoff: str


def run_stage_agent(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    adapter: StageAgentAdapter,
    *,
    expected_branch: str,
) -> StageAgentRunResult:
    """Start or resume the stage agent for one turn.

    ``expected_branch`` is required: an unattended run must always know
    which branch it is meant to modify — "any non-main branch" is not an
    acceptable stand-in for that. It is enforced by the branch guard and
    included in the stage prompt so the agent is told not to switch
    branches.

    Records the provider's own returned session id in state.json and
    regenerates handoff.md from the provider's result and actual git
    context (never invented test evidence). Raises
    :class:`StageAgentRunError` if: the branch guard refuses the run; the
    worktree is already locked by another live process; the provider
    itself fails; or, on resume, the provider returns a different session
    id than the one it was asked to resume (the recorded implementation
    session identity is never silently replaced).
    """

    if not expected_branch or not expected_branch.strip():
        raise StageAgentRunError(
            "expected_branch is required for a stage-agent run; an unattended "
            "run must know which branch it is meant to modify"
        )

    try:
        branch = ensure_branch_for_unattended_run(repo_root, expected_branch=expected_branch)
    except BranchGuardError as exc:
        raise StageAgentRunError(str(exc)) from exc

    try:
        with worktree_lock(repo_root):
            state = stage.read_state()
            resume_id = state.implementation_session_id
            prompt = build_stage_prompt(
                stage,
                sparring_dir,
                resume=resume_id is not None,
                expected_branch=expected_branch,
            )
            try:
                if resume_id:
                    result = adapter.resume(resume_id, prompt)
                else:
                    result = adapter.start(prompt)
            except ProviderError as exc:
                raise StageAgentRunError(str(exc)) from exc

            if resume_id and result.session_id != resume_id:
                raise StageAgentRunError(
                    f"provider was asked to resume session {resume_id!r} but "
                    f"returned session {result.session_id!r}; refusing to "
                    "silently replace the recorded implementation session identity"
                )

            state.implementation_session_id = result.session_id
            stage.write_state(state)

            try:
                handoff = generate_handoff(
                    stage,
                    repo_root,
                    stage_goal=stage.read_brief(),
                    claims=result.text,
                    check_pushed=False,
                )
            except GitContextError as exc:
                raise StageAgentRunError(str(exc)) from exc
    except WorktreeLockError as exc:
        raise StageAgentRunError(str(exc)) from exc

    return StageAgentRunResult(
        result=result,
        resumed=resume_id is not None,
        branch=branch,
        prompt=prompt,
        handoff=handoff,
    )


__all__ = ["StageAgentRunError", "StageAgentRunResult", "run_stage_agent"]
