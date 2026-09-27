"""The prompt for one plan-intake turn (see :mod:`agent_sparring.intake`).

The intake agent reads a human plan and answers with a structured
interpretation. Everything this prompt asks for is *checked* by the engine
afterwards -- line ranges, source coverage, run boundaries, gate placement,
dependency order -- so the prompt states the rules the checks enforce
rather than hoping they are followed. What the engine cannot check is
judgement (is this acceptance criterion missing? is this stage too broad?),
and that is what the findings are for.

No project is special here. The source plan, the project's ``PROJECT.md``
and the repository names are data the prompt carries; nothing in this module
knows what any particular project's plans look like.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

MODE_FAITHFUL = "faithful"
MODE_REFINE = "refine"


def number_lines(text: str) -> str:
    """The source plan with 1-based line numbers, the coordinates every range
    in the interpretation is written in."""

    lines = text.splitlines()
    width = len(str(len(lines))) if lines else 1
    return "\n".join(f"{index:>{width}}| {line}" for index, line in enumerate(lines, start=1))


_ROLE = """\
# Plan intake

You are preparing a human-written plan for deterministic, staged execution by
Agent Sparring. You do not execute anything and you do not edit anything: this
turn runs in a read-only sandbox, and the engine verifies afterwards that no
repository changed. You may read files and run read-only commands (for example
`git log`, `git show`, `rg`) in the repositories listed below, to verify facts
the plan states rather than inheriting them.

Your answer is a structured interpretation. A person reviews it, and only a
manifest they explicitly approve is ever executed. Your job is to make that
review easy and honest: interpret the plan exactly, and point out what is
wrong with it."""

_EXECUTION_MODEL = """\
## How execution works (what your interpretation must fit)

- A *run* executes an ordered list of stages, one after another, inside ONE
  primary repository on ONE expected branch. Each stage is implemented by an
  agent working in that primary repository, then reviewed, then accepted.
- A stage may additionally declare *sibling repositories*. A sibling
  declaration only pins a coupled candidate commit in another repository so
  acceptance covers both. It does NOT move the stage's work into that
  repository. A stage whose implementation lives only in another repository
  therefore cannot run inside this run: it belongs to a run whose primary
  repository is that other repository.
- The runtime enforces nothing between two stages of one run except "the
  previous stage was accepted". It has no way to wait for a production
  deployment, a release activation, an owner's go-ahead, or any other
  external/manual event between two stages of the same run.
- Therefore: whenever later executable work depends on such an event, the
  event is a *gate* and it must fall BETWEEN runs. The stages it blocks go in
  a later run. A person confirms the gate explicitly when approving that
  later run. Never order stages in one run so that the order merely implies a
  prerequisite that nothing enforces.
- A stage's brief is assembled by the engine, not by you: the exact source
  lines of the context blocks you attach, then the exact source lines of the
  stage itself, then your optional `intake_scope` note. You choose line
  ranges; you never paraphrase requirements."""

_INTERPRETATION = """\
## What to produce

Line ranges are 1-based and inclusive, in the numbering shown in the source
plan below.

- `context`: reusable blocks of plan-wide text that some stage needs in order
  to be implemented or reviewed correctly without the rest of the document --
  principles, invariants, approved decisions, definitions, starting-point
  facts. Give each an id. Attach a context block to EVERY stage whose work it
  constrains (`context_ids`); one block may serve many stages. Leaving a
  global constraint out of a stage that it governs is the most important
  mistake to avoid.
- `runs`: ordered run slices. Each has an id, its `primary_repository` (one
  of the repository names listed below, or a name the plan uses), its
  `expected_branch`, a rationale, and its ordered `stages`. The expected
  branch is the feature branch this slice's stages commit their candidates
  to. Give it only if the plan names such a branch; otherwise null, and the
  person supplies one at approval. A baseline or starting-point branch the
  plan describes (for example "`main` at `<sha>`") is where work starts
  from, not where a run commits, and is never the expected branch.
- Each stage: `label` exactly as the plan labels it (`0`, `1A`, `W3B`, ... --
  labels are display strings; order comes only from the array), `title`,
  `source_ranges` (the stage's own text, including its heading),
  `context_ids`, `repositories` (sibling repository names whose candidates
  belong to this stage; never the run's primary), `depends_on` (labels of
  stages that must be accepted first), `mode` (`implementation`, or
  `independent_review` for a stage that is only a fresh review of what earlier
  stages produced), `intake_scope` (see below, or null), and a rationale.
- `gates`: every production, manual, external or deferred action the plan
  mentions that is not itself executable stage work -- deployments, release
  activation, production data runs, owner go-aheads, security sign-offs that
  gate later work. `after_stage`: the stage it follows (or null).
  `blocks_stages`: labels of executable stages that must not start until the
  gate is satisfied (empty if nothing executable depends on it). A gate that
  blocks stages must separate runs, as described above.
- `excluded`: text that is neither stage text, context nor a gate, with the
  reason (history, references, open questions outside scope, and so on).
- `findings`: your plan-quality review (see below).
- `verdict`: `executable_as_written`, `executable_with_recommendations`, or
  `cannot_interpret_faithfully` (you cannot produce a faithful interpretation
  without a person resolving something; say what in a blocking finding).
- `summary`: two or three sentences for the person reviewing this.

Source coverage is checked by the engine: every non-blank line of the source
plan (other than horizontal rules) must fall in at least one stage source
range, context block, gate, or exclusion. Uncovered text blocks approval. Do
not silence it by excluding text that actually constrains a stage."""

_INTAKE_SCOPE = """\
## `intake_scope`: what it may and may not say

`intake_scope` is rendered in the brief under a heading that says it was
written by intake, not taken from the plan, and that the plan text governs
where they differ. It may only:

- narrow what this stage executes (for example "implementation, tests and a
  dry-run report only; the production run is a separate gated action");
- make a boundary explicit (which repository, which run, what is deferred);
- defer an action to a named gate.

It may NOT weaken, override, reinterpret or invent requirements, acceptance
criteria or constraints. A substantive change you think the plan needs goes
in a finding and, in refine mode, in `amended_plan` -- never in
`intake_scope`."""

_QUALITY = """\
## Plan-quality review: actively look for these

Use the matching `code`. Severity `blocking` means a faithful, safe manifest
cannot be approved until a person resolves it; `recommendation` means
executable, but you advise a change; `info` is context for the reviewer.

- `missing_acceptance_criteria`: a stage has no checkable definition of done.
- `omitted_global_context`: a stage depends on context outside its section
  (say which context block you attached, or why none fits).
- `dependency_order_mismatch`: the numbering or document order differs from
  the dependency order the prose implies.
- `implementation_mixed_with_production`: a stage mixes implementation with a
  deployment, activation or production data action.
- `cross_repository_without_candidate_strategy`: a stage spans repositories
  without saying how the candidates relate.
- `stage_too_broad`: too much for one reviewable candidate.
- `repeated_or_contradictory_requirement`: the same requirement stated twice,
  or two that conflict.
- `stale_baseline_fact`: a fact the plan asserts (a commit, a version, a
  count, a deployed state) that should be verified at execution time rather
  than inherited -- say what you checked, if you checked.
- `open_question_as_instruction`: an unresolved question phrased as work to
  do, or work that cannot start until a question is answered.
- `ambiguous_source`: anything else you could only interpret by guessing.
- `other`: anything else worth the reviewer's attention.

You may say "executable as written, but I recommend these stage changes", or
"I cannot produce a faithful manifest without resolving X"."""

_MODE_TEXT = {
    MODE_FAITHFUL: """\
## Mode: faithful

Interpret the plan as written. Keep its substantive staging: one stage per
stage the plan defines, in an order its dependencies allow. You may still
narrow a stage with `intake_scope` to split out a gated production action the
plan itself says needs separate authorization, and you must still separate
runs where the execution model above requires it. `amended_plan` must be
null. Recommend any other change as a finding.""",
    MODE_REFINE: """\
## Mode: refine

You may propose different stage boundaries or order where the plan is
difficult or unsafe to execute as written: split implementation from a later
production action, split coupled work across repositories into bounded
stages plus a cross-repository review stage, carry plan-wide invariants into
every stage that needs them. Stages must still be assembled from exact
source ranges; a refined stage may take part of a section plus a narrowing
`intake_scope`.

If you propose a substantive change to the plan's content, put the complete
revised plan document in `amended_plan`. The engine shows the person a diff
against the source; nothing is applied to the source plan. The manifest
executes the source text as you sliced it, never the amendment, so do not
rely on amended text in any stage. Use null when no amendment is needed.""",
}


def assemble_intake_prompt(
    *,
    plan_label: str,
    source_text: str,
    mode: str,
    project_context: str | None,
    primary_repository: str,
    repo_root: Path,
    context_repositories: Mapping[str, Path],
    extra_notes: Sequence[str] = (),
) -> str:
    """The full prompt for one intake turn."""

    if mode not in _MODE_TEXT:
        raise ValueError(f"unknown intake mode {mode!r}")

    repositories = [f"- `{primary_repository}` (this project): `{repo_root}`"]
    for name, path in sorted(context_repositories.items()):
        repositories.append(f"- `{name}`: `{path}`")

    sections = [
        _ROLE,
        _EXECUTION_MODEL,
        _INTERPRETATION,
        _INTAKE_SCOPE,
        _QUALITY,
        _MODE_TEXT[mode],
        "## Repositories\n\nRepository names to use in `primary_repository` and "
        "`repositories`. Only read these; do not modify them.\n\n" + "\n".join(repositories),
    ]
    if project_context and project_context.strip():
        sections.append(
            "## Project context (the project's PROJECT.md, verbatim)\n\n"
            + project_context.strip()
        )
    for note in extra_notes:
        sections.append(note)
    sections.append(
        f"## Source plan: `{plan_label}`\n\n"
        "Line-numbered. The numbers and the `| ` separator are not part of the "
        "text.\n\n"
        "```text\n" + number_lines(source_text) + "\n```"
    )
    return "\n\n".join(sections) + "\n"


__all__ = ["MODE_FAITHFUL", "MODE_REFINE", "assemble_intake_prompt", "number_lines"]
