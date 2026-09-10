"""Stage-agent run orchestration.

Ties together the branch guard, the implementation lock, bounded prompt
assembly, and the provider adapter for one stage-agent turn: fresh session
for a new stage, resume of the same session for a SEND_BACK follow-up. This
module owns start-vs-resume selection and session-id bookkeeping in
state.json; it does not implement any provider itself (see
:mod:`agent_sparring.providers`) and does not decide sparring routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_sparring.branch_guard import BranchGuardError, ensure_branch_for_unattended_run
from agent_sparring.concurrency import StageLockError, stage_lock
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


def run_stage_agent(
    stage: Stage,
    sparring_dir: Path,
    repo_root: Path,
    adapter: StageAgentAdapter,
    *,
    expected_branch: str | None = None,
) -> StageAgentRunResult:
    """Start or resume the stage agent for one turn, and record its session id.

    Raises :class:`StageAgentRunError` if the branch guard refuses the run,
    the stage is already locked by another live process, or the provider
    itself fails.
    """

    try:
        branch = ensure_branch_for_unattended_run(repo_root, expected_branch=expected_branch)
    except BranchGuardError as exc:
        raise StageAgentRunError(str(exc)) from exc

    try:
        with stage_lock(stage.directory):
            state = stage.read_state()
            resume_id = state.implementation_session_id
            prompt = build_stage_prompt(stage, sparring_dir, resume=resume_id is not None)
            try:
                if resume_id:
                    result = adapter.resume(resume_id, prompt)
                else:
                    result = adapter.start(prompt)
            except ProviderError as exc:
                raise StageAgentRunError(str(exc)) from exc

            state.implementation_session_id = result.session_id
            stage.write_state(state)
    except StageLockError as exc:
        raise StageAgentRunError(str(exc)) from exc

    return StageAgentRunResult(
        result=result, resumed=resume_id is not None, branch=branch, prompt=prompt
    )


__all__ = ["StageAgentRunError", "StageAgentRunResult", "run_stage_agent"]
