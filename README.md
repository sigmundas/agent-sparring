# agent-sparring

Pairs a stage implementation agent with an independent sparring agent, so the
two can exchange corrections without a human acting as courier.

## What it does

You write (or have an agent draft) a plan made of bounded stages. For each
stage, an implementation agent (Claude Code by default) does the work. An
independent, read-only reviewer (Codex by default) inspects the candidate.
The usual verdicts are:

- **READY**: the engine finalizes the reviewed work if needed, verifies the
  pushed commit, freezes and accepts it, then moves to the next stage.
- **SEND_BACK**: the findings go back to the implementer, and the loop
  repeats.
- **NEEDS_YOU**: the run pauses with a structured list of checks only a
  person can do, such as device QA or a product decision.

The engine owns every gate: which branch a stage runs on, a clean worktree,
the exact SHA that was reviewed. No agent can wave any of them through.

The tool is generic: it contains no knowledge of any particular project. Each
project supplies a small `.sparring/project.toml` and a prose
`.sparring/PROJECT.md`.

Review happens at each stage, with the aim of finding mistakes before later
work builds on them. Within a stage, the implementer and reviewer normally
resume their own separate conversations across corrections. Human checks that
block the stage return to the same reviewer, keeping feedback close to the
work and making it visible before the plan advances.

This continuity may reduce repeated setup and explanation. We have not
benchmarked token usage, cost or completion time against other workflows;
reusing a conversation does not by itself establish savings. See
[context reuse and its limits](docs/stages.md#feedback-and-context-continuity).

## Get started

**[QUICKSTART.md](QUICKSTART.md)** goes from clone to a first staged run in
a few steps, including installation, project setup and confirmation.

## Suggested workflows

- **Run a reviewed plan:** use `sparring start-plan <plan.md> --expected-branch
  <branch>` from a clean feature branch. Review the proposed stages and any
  decisions, then run the confirmation command it prints. The engine continues
  through implementation and review until it needs you or finishes.
- **Answer a pause:** perform the requested checks and use the run's
  `resume-plan` command with `--evidence` to send the results to the reviewer.
  At a deferred-check checkpoint, use the requested `--deferred-result` instead.
  [Human gates](docs/gates.md) explains what each answer means.
- **Work from VS Code:** use **Make Plan…**, **Run Plan** and the Overview to
  prepare work, follow progress and respond to checks. The
  [extension guide](https://github.com/sigmundas/agent-sparring-vscode) covers
  installation and workflows.

Preparation can use a read-only provider turn for a free-form plan before you
confirm execution. For individual stages or advanced control, see
[run a stage](docs/stages.md) and [run a whole plan](docs/plans.md).

## What's in this repository

| Part | What it is |
| --- | --- |
| `sparring` CLI (`src/agent_sparring/`) | the engine: stages, review loop, gates, plan runs |
| Claude Code plugin (`.claude-plugin/`, `skills/`) | `/agent-sparring:sparring-setup`, `/agent-sparring:sparring-plan`, `/agent-sparring:sparring-run` |
| [VS Code extension](https://github.com/sigmundas/agent-sparring-vscode) (separate repo) | a cockpit that watches runs and continues or fixes them with engine commands |

## Documentation

The reference lives under [docs/](docs/README.md):

- [Set up a project](docs/setup.md): configuration, models and effort, git hygiene
- [Run a stage](docs/stages.md): a single stage by hand, and asking the reviewer
- [Run a whole plan](docs/plans.md): `run-plan` / `resume-plan`, the stage convention, pausing
- [Plan preparation](docs/intake.md): `start-plan`, decisions and confirmation;
  advanced intake turns a free-form plan into approved run slices
- [Human gates](docs/gates.md): how `NEEDS_YOU` checks are asked and answered
- [Reference](docs/reference.md): cross-repository work, review-only stages, artifact ownership
- [Design](docs/design.md): principles and architecture

`sparring --help` lists every command.

## Status

This is alpha software, used daily on one real multi-repository project. The
single-stage loop and Markdown plan runs are the stable core. Plan intake
(`prepare-plan` / `approve-plan`) is experimental; see its page.

## Develop

```sh
uv sync            # creates .venv with the package and pytest
uv run pytest -q   # runs the test suite
```

Contributor and agent instructions: [AGENTS.md](AGENTS.md).

## License

MIT; see [LICENSE](LICENSE).
