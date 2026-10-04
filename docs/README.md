# agent-sparring documentation

New here? Start with the [quickstart](../QUICKSTART.md). These pages are the
reference it links into.

| Page | What it covers |
| --- | --- |
| [Set up a project](setup.md) | `project.toml`, `PROJECT.md`, your model and effort preferences, `fix-config`, git hygiene |
| [Run a stage](stages.md) | one stage by hand: run, review, ask the reviewer, what each agent was told |
| [Run a whole plan](plans.md) | `run-plan` / `resume-plan`, the Markdown stage convention, manifests, pausing, human checks, pushing |
| [Plan intake](intake.md) *(experimental)* | `prepare-plan` / `approve-plan`: turning a human plan into approved run slices |
| [Human gates](gates.md) | how `NEEDS_YOU` asks for a check and how an answer is matched to it |
| [Migration-order detection](migrations.md) *(Stage A, read-only)* | `[migrations]`, recorded history snapshots, the deferred registry, `check-migrations` |
| [Reference](reference.md) | cross-repository candidates, review-only stages, `next_turn`, resume vs fresh session vs reset, who owns which artifact |
| [Design](design.md) | principles, architecture and build history |

Design records under [plans/](plans/) are historical or proposed work, not
descriptions of current behavior:

- [plans/plan-intake-integrity-revision.md](plans/plan-intake-integrity-revision.md):
  the plan-intake integrity revision (Stage A implemented; B and C pending).
- [plans/backend-selection.md](plans/backend-selection.md): per-role backend
  selection for `codex-cli` (proposed, not implemented).
