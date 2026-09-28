"""Refuse unattended stage-agent runs on the wrong branch.

Per the project plan: verify the expected feature branch before an
unattended implementation run, and never permit an unattended stage agent to
commit directly to main.
"""

from __future__ import annotations

from pathlib import Path

from agent_sparring.git_context import current_branch

DEFAULT_PROTECTED_BRANCHES = ("main", "master")


def is_protected_branch(
    branch: str, protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
) -> bool:
    """Whether no unattended stage agent may ever run on ``branch``."""

    return branch in protected_branches


class BranchGuardError(RuntimeError):
    """Raised when the repo is not on a branch safe for an unattended run."""


def ensure_branch_for_unattended_run(
    repo_root: Path,
    *,
    expected_branch: str | None = None,
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES,
) -> str:
    """Return the current branch if it is safe for an unattended stage run.

    Always refuses a protected branch (``main``/``master`` by default),
    regardless of ``expected_branch``. When ``expected_branch`` is given,
    also refuses any branch other than that one.
    """

    branch = current_branch(repo_root)

    if is_protected_branch(branch, protected_branches):
        raise BranchGuardError(
            f"refusing unattended stage-agent run on protected branch {branch!r} "
            f"in {repo_root}; check out the stage's feature branch first"
        )

    if expected_branch is not None and branch != expected_branch:
        raise BranchGuardError(
            f"expected branch {expected_branch!r} but {repo_root} is on {branch!r}"
        )

    return branch


__all__ = [
    "BranchGuardError",
    "DEFAULT_PROTECTED_BRANCHES",
    "ensure_branch_for_unattended_run",
    "is_protected_branch",
]
