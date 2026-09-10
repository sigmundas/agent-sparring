"""Handoff packet generation: a thin default form and an explicit
self-contained fallback.

A handoff makes the stage goal, implementation-agent claims, git identity,
changed files, working-tree status, test evidence, and any open/deferred
checks or previous unresolved sparring findings available to the sparrer.
The thin form (default) never embeds a diff; the self-contained form adds
one for a sparrer without repository access. Shape only (not code) is drawn
from the Sporely V1 web review packet at
``/Users/sigmundas/Documents/Code/sporely/.sparring/handoff.py`` — that file
is deeply Sporely/multi-repo/transcript-specific and is not ported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_sparring.git_context import ChangedFile, GitContext, gather_git_context, diff_patch
from agent_sparring.stage import Stage


@dataclass(frozen=True)
class HandoffInput:
    stage_goal: str
    claims: str
    git: GitContext
    test_evidence: str | None = None


def _extract_section(text: str, heading: str) -> str | None:
    """Best-effort body of a markdown ``## heading`` section.

    Returns ``None`` if the heading is missing or its body is still the
    unedited template placeholder (parenthesized prose).
    """

    target = heading.strip().lstrip("#").strip().lower()
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lstrip("#").strip().lower() == target:
            start = index + 1
            break
    if start is None:
        return None

    body_lines: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body or body.startswith("("):
        return None
    return body


def _open_checks_section(stage: Stage) -> str:
    try:
        notes = stage.read_notes()
    except Exception:
        return "(no notes.md available)"
    return _extract_section(notes, "## Deferred checks") or "(none recorded)"


def _previous_sparring_section(stage: Stage) -> str:
    try:
        sparring = stage.read_sparring()
    except Exception:
        return "(no sparring.md available)"
    parts = []
    for heading in (
        "## Finding / discussion",
        "## Routing outcome",
        "## SEND BACK TO STAGE",
        "## NEEDS YOU",
        "## ESCALATE",
        "## Deferred",
    ):
        body = _extract_section(sparring, heading)
        if body:
            parts.append(f"{heading}\n\n{body}")
    return "\n\n".join(parts) if parts else "(none recorded)"


def _pushed_line(git: GitContext) -> str:
    if git.pushed is None:
        return "not checked"
    if git.pushed:
        return f"yes ({git.push_detail})"
    return f"NO ({git.push_detail})"


def _changed_files_block(files: tuple[ChangedFile, ...]) -> str:
    if not files:
        return "(no base commit recorded, or no changes between base and candidate)"
    lines = []
    for f in files:
        if f.old_path is not None:
            lines.append(f"- {f.status}\t{f.old_path} -> {f.path}")
        else:
            lines.append(f"- {f.status}\t{f.path}")
    return "\n".join(lines)


def _dirty_paths_block(paths: tuple[str, ...]) -> str:
    if not paths:
        return "(working tree clean)"
    return "\n".join(f"- {path}" for path in paths)


def render_thin_handoff(stage: Stage, handoff_input: HandoffInput) -> str:
    """Render the default handoff: identity/context, no embedded diff."""

    git = handoff_input.git
    lines = [
        f"# Handoff: {stage.stage_id}",
        "",
        "## Stage goal",
        "",
        handoff_input.stage_goal.strip() or "(not recorded)",
        "",
        "## Claims",
        "",
        handoff_input.claims.strip() or "(not recorded)",
        "",
        "## Git context",
        "",
        f"- Branch: `{git.branch}`",
        f"- Base commit: `{git.base_sha or 'not recorded'}`",
        f"- Candidate commit: `{git.candidate_sha}`",
        f"- Pushed: {_pushed_line(git)}",
        "",
        "### Changed files",
        "",
        _changed_files_block(git.changed_files),
        "",
        "### Working tree status",
        "",
        _dirty_paths_block(git.dirty_paths),
        "",
        "## Test / build evidence",
        "",
        (handoff_input.test_evidence or "").strip() or "(not recorded)",
        "",
        "## Open / deferred checks",
        "",
        _open_checks_section(stage),
        "",
        "## Previous unresolved sparring findings",
        "",
        _previous_sparring_section(stage),
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_self_contained_packet(
    stage: Stage, handoff_input: HandoffInput, repo_root: Path
) -> str:
    """Render the thin handoff plus an embedded diff, for a sparrer with no
    repository access. Explicit opt-in only; never the default."""

    git = handoff_input.git
    thin = render_thin_handoff(stage, handoff_input)
    if git.base_sha is None:
        diff_text = "(no base commit recorded; cannot compute a diff)"
    else:
        diff_text = diff_patch(repo_root, git.base_sha, git.candidate_sha) or "(no differences)"
    return thin + "\n## Diff (self-contained)\n\n```diff\n" + diff_text + "\n```\n"


def generate_handoff(
    stage: Stage,
    repo_root: Path,
    *,
    stage_goal: str,
    claims: str,
    test_evidence: str | None = None,
    self_contained: bool = False,
    check_pushed: bool = True,
    base_sha: str | None = None,
    candidate_sha: str | None = None,
) -> str:
    """Gather git context and render/write ``handoff.md``.

    ``base_sha``/``candidate_sha`` override the stage's recorded
    ``state.json`` values when given (useful before those are recorded,
    e.g. from the CLI); otherwise the recorded values are used.
    """

    state = stage.read_state()
    git = gather_git_context(
        repo_root,
        base_sha=base_sha or state.base_sha,
        candidate_sha=candidate_sha or state.candidate_sha,
        check_pushed=check_pushed,
    )
    handoff_input = HandoffInput(
        stage_goal=stage_goal, claims=claims, git=git, test_evidence=test_evidence
    )
    content = (
        build_self_contained_packet(stage, handoff_input, repo_root)
        if self_contained
        else render_thin_handoff(stage, handoff_input)
    )
    stage.write_handoff(content)
    return content
