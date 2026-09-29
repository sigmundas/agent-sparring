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
[README](https://github.com/sigmundas/agent-sparring-vscode#install-locally).

**4. Set up your repository.** Run these from the repository you want to work
on (or run `/agent-sparring:sparring-setup`):

```sh
sparring init-config     # writes .sparring/project.toml
sparring fix-config      # adds .sparring/stages/, plans/, intake/ to .gitignore
sparring check-config    # must say "git-ignored: yes" three times
```

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
Then confirm that the engine reads it the way you meant:

```sh
sparring check-plan docs/plans/<name>.md     # lists the stages; runs nothing
```

**8. Run it** on a feature branch (runs are refused on `main`):

```sh
git switch -c feature/<name>
sparring run-plan docs/plans/<name>.md --expected-branch feature/<name>
```

Or run `/agent-sparring:sparring-run`, or use **Agent Sparring: Run Plan** in
VS Code. The run goes from stage to stage by itself. It stops when a stage
needs you (`NEEDS_YOU`), when something fails, or when the plan is done.
([details](docs/plans.md))

**9. Continue.** After you do the checks it asks for:

```sh
sparring resume-plan docs/plans/<name>.md --expected-branch feature/<name> \
    --evidence "What you checked and what you saw."
```

In VS Code, the Overview panel shows the same pause, and offers the resume
action and any configuration fixes as buttons.

A plan that doesn't fit the stage heading format, or that spans several
repositories, can go through [plan intake](docs/intake.md) *(experimental)*
instead of step 7.
