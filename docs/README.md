# agent-sparring documentation

New here? Start with the [quickstart](../QUICKSTART.md). These pages are the
reference it links into.

| Page | What it covers |
| --- | --- |
| [Set up a project](setup.md) | `project.toml`, `PROJECT.md`, your model and effort preferences, `fix-config`, git hygiene |
| [Run a stage](stages.md) | one stage by hand, context reuse and efficiency limits, usage reports, asking the reviewer, captured instructions |
| [Run a whole plan](plans.md) | `start-plan`, `run-plan` / `resume-plan`, the Markdown stage convention, manifests, pausing, human checks, pushing |
| [Plan intake](intake.md) | `start-plan`, the default way to start a run (one confirmation from a human plan to a run), and the advanced `prepare-plan` / `approve-plan`: turning a human plan into approved run slices; the intake integrity revision's Stages B and C are still experimental |
| [Human gates](gates.md) | how `NEEDS_YOU` asks for a check and how an answer is matched to it |
| [Migration-order detection](migrations.md) *(Stage A, read-only)* | `[migrations]`, recorded history snapshots, the deferred registry, `check-migrations` |
| [Reference](reference.md) | cross-repository candidates, review-only stages, `next_turn`, resume vs fresh session vs reset, who owns which artifact |
| [Design](design.md) | principles, architecture and build history |

For a minimal staged plan, see the [Quickstart](../QUICKSTART.md). For a
completed implementation example, see the extension's
[colleague-sharing plan](https://github.com/sigmundas/agent-sparring-vscode/blob/main/docs/plans/colleague-sharing.md).
Retired engine implementation plans remain in Git history; the reference pages
above describe current behavior.
