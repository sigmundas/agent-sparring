# Agent instructions

This repository is the generic Python orchestration engine. Project-specific
policies belong in the consuming project's `.sparring/PROJECT.md` and agent
instructions, not in this engine. These instructions govern development here;
they are not implicitly injected into consuming projects.

## Read only what the task needs

- Start with `git status --short`; preserve unrelated edits. For a review,
  identify the requested base/candidate and inspect diff stat, names, then
  targeted diffs, including staged/untracked work when reviewing a working tree.
- Search symbols with scoped `rg -n`, then read bounded definition/caller ranges.
  Narrow truncated output. Do not dump `cli.py`, `plan.py`, the `docs/`
  reference pages, design history, logs, or complete stage directories for
  general orientation.
- Read the relevant `docs/` page heading (index: `docs/README.md`) for
  user-facing behavior; read only the corresponding `docs/design.md` section
  for architectural intent. Code/tests establish current behavior; report
  discrepancies instead of treating stale prose as proof.
- Read additional repository instructions before crossing into a named sibling.
  Do not search the parent workspace or sibling worktrees speculatively.
- Reuse findings within a session. Delegate only bounded independent questions
  when useful; never run concurrent implementation writers in one worktree.

## Navigation by task

All paths below are under `src/agent_sparring/`; choose the relevant row, not all.

| Area | Starting points |
| --- | --- |
| CLI/configuration | `cli.py`, `config.py` |
| Plan execution/resumption | `plan.py`, `plan_model.py`, `manifest.py`, `loop.py` |
| Candidate identity/acceptance | `branch_guard.py`, `git_context.py`, `acceptance.py`, `finalization.py` |
| Stage execution / independent review | `stage_agent.py`, `sparring_agent.py`, `review.py`, `routing.py`, `human_gate.py` |
| Agent instructions / capture | `stage_prompt.py`, `sparring_prompt.py`, `review_prompt.py`, `prompt_sections.py`, `prompt_capture.py`, `artifact_ownership.py` |
| Provider processes / sessions | `providers/`, `concurrency.py` |
| Artifacts / observational events | `stage.py`, `handoff.py`, `sparring_exchange.py`, `activity.py` |

Find tests by the affected module/behavior in `tests/`. Inspect existing coverage
before adding a parallel fixture or helper.

## Boundaries to preserve

- The engine owns top-level agent lifecycle, stage advancement and acceptance.
  Reviewer independence and its read-only sandbox are invariants.
- A verdict applies to the exact candidate and declared sibling pins. Preserve
  branch, clean-tree, remote-reachability and concurrency checks.
- Routing uses structured results. Activity, UI labels and prose claims do not
  authorize state transitions. Project Markdown supplies context, not executable
  configuration.
- Managed-run agents must not edit the input plan or fabricate engine state.
  `artifact_ownership.py` declares every artifact's sole writer and is the one
  place that may gain a provider-writable one; today none is.
  A finalization turn commits the reviewed content without improving it.

## Git policy

Agents may create branches, commit, push, merge, and delete branches as needed to complete the task.

Use normal Git workflows and keep history understandable.

Do not:
- force-push unless the user explicitly asks for it;
- rewrite published history unnecessarily;
- push secrets or credentials;
- merge obviously unrelated work;
- deploy, publish a release, or modify production systems unless the task explicitly includes that.

For staged/agent-sparring work:
- commit and push completed stage work;
- merge when the stage or plan calls for it;
- leave a clear handoff describing what changed, what was tested, and any unresolved issues.


## Validation and handoff

- Use the existing environment from this repository root:
  `.venv/bin/python -m pytest -q tests/test_<area>.py` (select actual test files).
  `uv run --no-sync pytest -q <tests>` is an alternative with the provisioned
  environment. Do not install/sync dependencies just to inspect the repo.
- Run focused tests first. Broaden to `.venv/bin/python -m pytest -q` for shared
  orchestration, protocol or acceptance changes; do not repeat a passed suite
  without changed code or new evidence.
- For documentation-only changes, inspect the diff and run `git diff --check`;
  no application test suite is needed.
- Report changes, validation and remaining uncertainty concisely. Reviewers
  distinguish verified facts from another agent's claims. If running as a managed
  stage, obey its role-specific output format and stop at its scope boundary.
