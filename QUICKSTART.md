# Quickstart

This takes you from nothing to a first staged run. Every step links to the
reference page for details.

**1. Install the engine** (Python 3.11 or newer):

```sh
git clone https://github.com/sigmundas/agent-sparring.git
uv tool install --editable ./agent-sparring   # or: pip install -e ./agent-sparring
sparring --help
```

**2. Make the agents available.** The defaults are Claude Code for
implementation and Codex for review. Both CLIs must be on `PATH` and signed
in:

```sh
claude --version && codex --version
```

**3. Optional: install the Claude Code skills and the VS Code cockpit.** In
Claude Code, run `/plugin marketplace add sigmundas/agent-sparring`, then
`/plugin install agent-sparring@agent-sparring`. For the extension, see its
[installation guide](https://github.com/sigmundas/agent-sparring-vscode/blob/main/docs/setup.md#install-locally).

**4. Set up your repository.** Use a Git repository with an intended remote
that you can push to: acceptance verifies the reviewed commit on that remote.
Run these from the repository you want to work on (or run
`/agent-sparring:sparring-setup`):

```sh
sparring init-config     # writes .sparring/project.toml (which provider runs each role)
sparring fix-config      # adds .sparring/stages/, plans/, intake/ to .gitignore
sparring check-config    # must say "git-ignored: yes" three times
```

Optional: choose your models. Model and effort are your own preferences.
They're shared by every repository and never stored in `project.toml`:

```sh
sparring model-choices
sparring set-config stage --model claude-opus-5-5 --effort medium
sparring set-config sparring --model gpt-6-astra
```

Without a preference, each provider uses its own default. The VS Code
Overview edits the same preferences.

**5. Write `.sparring/PROJECT.md`.** This is prose every agent reads: stack,
directory map, test and build commands, conventions, and the current baseline
test results. Commit it together with `project.toml` and `.gitignore`.
([details](docs/setup.md))

**6. Write a plan.** Create `docs/plans/<name>.md` by hand, or run
`/agent-sparring:sparring-plan` after discussing the work with Claude. Each
stage is a level-2 heading, numbered from 1:

```markdown
## Stage 1 — Extract the parser
Scope, files, acceptance criteria, non-goals, any manual checks.

## Stage 2 — Stream results
...
```

**7. Review and check it.** Read the plan, since you are the one approving it.
For the staged format above, check that the parser finds the intended stages:

```sh
sparring check-plan docs/plans/<name>.md     # lists the stages; runs nothing
```

**8. Prepare and confirm it** on a clean feature branch (runs are refused on
`main`). Commit the reviewed plan before preparation:

```sh
git switch -c feature/<name>
git add docs/plans/<name>.md
git commit -m "Add reviewed implementation plan"
sparring start-plan docs/plans/<name>.md --expected-branch feature/<name>
```

Review the proposed stages and gates. If the engine asks for decisions, rerun
with the `--answer DECISION=OPTION` values you chose. When it reports `ready`,
run the confirmation command it prints, including its `--confirm <token>`.
Changing the plan, repository or execution settings requires a new confirmation.
Preparation of a free-form plan may use a read-only provider turn before this
confirmation; a plan in the staged format above runs directly.

Or use **Agent Sparring: Run Plan** in VS Code to see the preparation and press
**Start**. The run goes from stage to stage by itself and stops when it needs
you (`NEEDS_YOU`), encounters an escalation or failure, or finishes.
[Workflow details](docs/plans.md) cover the advanced `run-plan` route and the
`/agent-sparring:sparring-run` skill.

**9. Continue.** After you do the checks it asks for, use the resume command
for that run. For the staged Markdown plan above:

```sh
sparring resume-plan docs/plans/<name>.md --expected-branch feature/<name> \
    --run-key <run-key> --evidence "What you checked and what you saw."
```

Use the run key the engine recorded. Intake runs resume with their approved
`--manifest` and run key, rather than the source Markdown. At a deferred-check
checkpoint, use the requested `--deferred-result` instead of general evidence.
If the engine pauses for push permission, review the commit and use the
[push authorization command](docs/plans.md#pushing-a-verified-candidate) it offers.

In VS Code, the Overview panel shows the same pause, and offers the resume
action and any configuration fixes as buttons.

For free-form or multi-repository plans, see
[plan preparation and intake](docs/intake.md#one-command-start-sparring-start-plan).
The staged parser check in step 7 is optional for those plans; `start-plan`
presents the engine's interpretation and any decisions before execution.
