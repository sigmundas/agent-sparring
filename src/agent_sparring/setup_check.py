"""Fixable project-setup problems, reported as data and repaired by the engine.

The engine refuses to start work when git can see its workflow-state
directories -- ``.sparring/stages/``, ``.sparring/plans/`` and
``.sparring/intake/`` are rewritten by the engine itself, and a visible one
makes the next ``freeze-candidate`` refuse the worktree as dirty. Those
refusals are prose written for a terminal. This module states the same
condition as structured data (``show-config --json`` reports it as
``setup_problems``) so a UI can say "setup needs updating" and offer the fix
without parsing sentences, and it owns the fix itself (``sparring
fix-config``): exactly the missing ignore lines are appended to the
repository's ``.gitignore``, and nothing else is touched.

The ``.gitignore`` edit is an ordinary tracked change a person commits; the
fix never commits, stages or rewrites anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.intake import INTAKE_DIRNAME, intake_not_ignored_message
from agent_sparring.plan import PLANS_DIRNAME, plan_state_not_ignored_message

STAGES_DIRNAME = "stages"

KIND_NOT_IGNORED = "not-ignored"

#: The workflow-state directories that must be ignored: (directory, what it is, a probe file).
WORKFLOW_STATE: tuple[tuple[str, str, str], ...] = (
    (STAGES_DIRNAME, "stage artifacts", "any-stage/state.json"),
    (PLANS_DIRNAME, "plan-run state", "any-plan.json"),
    (INTAKE_DIRNAME, "plan intake", "any-intake/intake.json"),
)


@dataclass(frozen=True)
class SetupProblem:
    """One fixable setup problem."""

    kind: str
    #: What is affected, as a person names it (``plan intake``).
    what: str
    #: The ``.gitignore`` line that fixes it (``.sparring/intake/``).
    ignore_line: str
    #: The repository ``.gitignore`` the fix appends to.
    gitignore: Path
    #: The engine's full refusal text for this problem, for technical details.
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "what": self.what,
            "ignore_line": self.ignore_line,
            "gitignore": str(self.gitignore),
            "message": self.message,
        }


def _ignore_line(repo_root: Path, sparring_dir: Path, dirname: str) -> str:
    relative = Path(os.path.relpath(Path(sparring_dir).resolve(), Path(repo_root).resolve())).as_posix()
    return f"{relative}/{dirname}/"


def _message(repo_root: Path, sparring_dir: Path, dirname: str) -> str:
    if dirname == PLANS_DIRNAME:
        return plan_state_not_ignored_message(repo_root, sparring_dir / PLANS_DIRNAME / "any-plan.json")
    if dirname == INTAKE_DIRNAME:
        return intake_not_ignored_message(repo_root)
    return (
        f"stage artifacts under {sparring_dir}/{STAGES_DIRNAME}/ are not ignored by git; add a "
        f"line '.sparring/{STAGES_DIRNAME}/' to {repo_root}/.gitignore"
    )


def setup_problems(repo_root: Path, sparring_dir: Path) -> list[SetupProblem]:
    """Every workflow-state directory git can see, in :data:`WORKFLOW_STATE` order.

    A directory outside the repository cannot be seen by git and is not a
    problem. A git failure is raised, never reported as "fine".
    """

    repo_root = Path(repo_root)
    sparring_dir = Path(sparring_dir)
    found: list[SetupProblem] = []
    for dirname, what, probe_name in WORKFLOW_STATE:
        probe = sparring_dir / dirname / probe_name
        try:
            probe.resolve().relative_to(repo_root.resolve())
        except ValueError:
            continue
        if is_ignored(repo_root, probe):
            continue
        found.append(
            SetupProblem(
                kind=KIND_NOT_IGNORED,
                what=what,
                ignore_line=_ignore_line(repo_root, sparring_dir, dirname),
                gitignore=repo_root / ".gitignore",
                message=_message(repo_root, sparring_dir, dirname),
            )
        )
    return found


def fix_setup(repo_root: Path, sparring_dir: Path) -> list[str]:
    """Append the missing ignore lines to ``<repo_root>/.gitignore``; return the lines added.

    Idempotent: a line already present is not added twice, and nothing is
    written when every directory is already ignored. The rest of the file is
    preserved byte for byte. Re-checked afterwards: a line git still does not
    honour (a negation later in the file, say) is an error, not a success.
    """

    problems = setup_problems(repo_root, sparring_dir)
    if not problems:
        return []
    gitignore = Path(repo_root) / ".gitignore"
    before = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    present = {line.strip() for line in before.splitlines()}
    lines = [problem.ignore_line for problem in problems if problem.ignore_line not in present]
    if lines:
        text = before
        if text and not text.endswith("\n"):
            text += "\n"
        if not any(line.startswith(".sparring/") for line in present):
            text += ("\n" if text else "") + "# Agent Sparring workflow state\n"
        gitignore.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
    remaining = setup_problems(repo_root, sparring_dir)
    if remaining:
        raise GitContextError(
            "after adding " + ", ".join(repr(line) for line in lines or [p.ignore_line for p in problems])
            + f" to {gitignore}, git still does not ignore "
            + ", ".join(problem.ignore_line for problem in remaining)
            + "; another rule in the file overrides it. Fix .gitignore by hand"
        )
    return lines


__all__ = ["KIND_NOT_IGNORED", "SetupProblem", "WORKFLOW_STATE", "fix_setup", "setup_problems"]
