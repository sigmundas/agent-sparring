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

The second kind is configuration the engine no longer reads: ``model`` and
``effort`` under ``[agents.<role>]`` in ``project.toml``, from before they
became user preferences (:mod:`agent_sparring.user_config`). They are
reported so a stale value is never mistaken for the one in effect, and the fix
removes exactly those keys. It does not copy them into the user's
preferences: which repository's old values should win is the person's choice,
not a deterministic repair.

Both edits are ordinary tracked changes a person commits; the fix never
commits, stages or rewrites anything else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_sparring.config import CONFIG_FILENAME, ObsoleteAgentSetting, load_project_config
from agent_sparring.config_edit import remove_obsolete_agent_settings
from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.intake import INTAKE_DIRNAME, intake_not_ignored_message
from agent_sparring.plan import PLANS_DIRNAME, plan_state_not_ignored_message

STAGES_DIRNAME = "stages"

KIND_NOT_IGNORED = "not-ignored"
KIND_OBSOLETE_AGENT_SETTING = "obsolete-agent-setting"

_ROLE_NAMES = {"stage": "stage agent", "sparring": "sparring agent"}

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


@dataclass(frozen=True)
class ObsoleteSettingProblem:
    """A ``project.toml`` model/effort key that no longer takes effect."""

    setting: ObsoleteAgentSetting
    config_path: Path
    kind: str = KIND_OBSOLETE_AGENT_SETTING

    @property
    def what(self) -> str:
        return f"{_ROLE_NAMES[self.setting.role]} {self.setting.field}"

    @property
    def message(self) -> str:
        return (
            f"{self.config_path} sets [agents.{self.setting.role}] {self.setting.field} = "
            f"{self.setting.value!r}, which no longer takes effect: model and effort are now your "
            f"own preferences, shared by every project, and are not read from project.toml. "
            f"'sparring fix-config' removes the obsolete key; choose your preference with "
            f"'sparring set-config {self.setting.role} --{self.setting.field} <value>'"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "what": self.what,
            "role": self.setting.role,
            "field": self.setting.field,
            "value": self.setting.value,
            "config_path": str(self.config_path),
            "message": self.message,
        }


def obsolete_setting_problems(sparring_dir: Path) -> list[ObsoleteSettingProblem]:
    """Every obsolete model/effort key in ``project.toml``; none when there is no file."""

    path = Path(sparring_dir) / CONFIG_FILENAME
    if not path.is_file():
        return []
    config = load_project_config(Path(sparring_dir))
    return [ObsoleteSettingProblem(setting=setting, config_path=path) for setting in config.obsolete_agent_settings]


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


def setup_problems(repo_root: Path, sparring_dir: Path) -> list[SetupProblem | ObsoleteSettingProblem]:
    """Every fixable setup problem: obsolete project model/effort keys first,
    then every workflow-state directory git can see, in :data:`WORKFLOW_STATE`
    order.

    A directory outside the repository cannot be seen by git and is not a
    problem. A git failure is raised, never reported as "fine".
    """

    repo_root = Path(repo_root)
    sparring_dir = Path(sparring_dir)
    found: list[SetupProblem | ObsoleteSettingProblem] = list(obsolete_setting_problems(sparring_dir))
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


@dataclass(frozen=True)
class SetupFix:
    """What :func:`fix_setup` changed."""

    added: list[str]
    removed: list[ObsoleteAgentSetting]
    config_path: Path


def fix_setup(repo_root: Path, sparring_dir: Path) -> SetupFix:
    """Repair every fixable setup problem.

    Obsolete model/effort keys are removed from ``project.toml`` (and nothing
    else in it changes); then the missing ignore lines are appended to
    ``<repo_root>/.gitignore``. Idempotent: nothing is written when nothing
    is wrong. Re-checked afterwards: a line git still does not honour (a
    negation later in the file, say) is an error, not a success.
    """

    config_path, removed = remove_obsolete_agent_settings(Path(sparring_dir))
    return SetupFix(added=_fix_ignored(repo_root, sparring_dir), removed=removed, config_path=config_path)


def _fix_ignored(repo_root: Path, sparring_dir: Path) -> list[str]:
    problems = [p for p in setup_problems(repo_root, sparring_dir) if isinstance(p, SetupProblem)]
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
    remaining = [p for p in setup_problems(repo_root, sparring_dir) if isinstance(p, SetupProblem)]
    if remaining:
        raise GitContextError(
            "after adding " + ", ".join(repr(line) for line in lines or [p.ignore_line for p in problems])
            + f" to {gitignore}, git still does not ignore "
            + ", ".join(problem.ignore_line for problem in remaining)
            + "; another rule in the file overrides it. Fix .gitignore by hand"
        )
    return lines


__all__ = [
    "KIND_NOT_IGNORED",
    "KIND_OBSOLETE_AGENT_SETTING",
    "ObsoleteSettingProblem",
    "SetupFix",
    "SetupProblem",
    "WORKFLOW_STATE",
    "fix_setup",
    "obsolete_setting_problems",
    "setup_problems",
]
