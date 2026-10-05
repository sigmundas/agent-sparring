---
name: sparring-run
description: Start or resume an agent-sparring plan run for a reviewed plan - verify config, branch and worktree through the engine, then dry-run start-plan, ask its decision questions and launch it with one confirmation (or resume-plan; prepare-plan/approve-plan as an advanced path). Invoke explicitly; asks for confirmation before any agent turn starts.
argument-hint: "<plan path> [resume] [evidence…]"
disable-model-invocation: true
allowed-tools: Read Grep Glob AskUserQuestion Bash(sparring:*) Bash(git status:*) Bash(git branch:*) Bash(git rev-parse:*) Bash(git log:*) Bash(git switch -c:*) Bash(git ls-files:*)
---

# Run a plan

The engine is the authority on whether a run may start. This skill asks the
engine, relays exactly what it says, and launches the engine's commands.

Never weaken a protection to get a run going. That means no editing run
state under `.sparring/`, no deleting stage directories, no stashing or
committing someone's work to make the tree clean, no `--adopt` or push flags
the person didn't ask for, and no retries with different flags hoping a
refusal goes away. When the engine refuses, show its message and the fix it
names, then stop.

A plan that was reviewed or approved has not been authorized to run. Start
only after the person confirms in step 4.

## 1. Setup

Run `sparring check-config`. If it fails, run `sparring show-config --json`.
For `setup_problems` of kind `not-ignored` (missing `.gitignore` lines) or
`obsolete-agent-setting` (old `model`/`effort` keys in `project.toml`),
offer `sparring fix-config`, which repairs exactly those and nothing else.
While obsolete keys remain, the engine refuses to start any run.
Then remind the person that the changed files must be committed. Removing
obsolete keys doesn't choose a model for them. For anything else, relay the
engine's message and point to `/agent-sparring:sparring-setup`.

Show which model and effort each role will use (`show-config`). They come
from the person's own preferences, or the provider's default. Don't change
them as part of starting a run. A stage keeps the configuration it started
with until it ends, so a preference changed during a run applies from the
next stage. Don't pass `--stage-model` or similar flags to a resume to force
a change: the engine refuses an override that contradicts the stage's
recorded configuration.

## 2. Branch and worktree

- `git branch --show-current`. An empty result means a detached HEAD: stop.
  On `main` or `master`, runs with implementation stages are refused. Ask
  for a feature branch name and create it with `git switch -c <name>` only
  after the person agrees.
- `git status --short` must show nothing outside the ignored `.sparring/`
  runtime directories. If it shows anything else, list it and stop. The
  person decides whether to commit or set it aside.
- The plan file itself must be committed. An uncommitted plan counts as a
  dirty tree.

## 3. Which command

Take the plan path from `$ARGUMENTS` and ask for it if it's missing.

- **A plan that is paused or waiting for you:** use `resume-plan`. Answering
  a human check needs the person's own words about what they checked and
  saw. Never write evidence for them. Pass it as `--evidence "<their text>"`
  (for a deferred check: `--deferred-result CHECK=OUTCOME[=NOTE]`).
- **Any other plan:** use `start-plan` (step 4). It works for plans with
  stage headings and for multi-repository or free-form plans alike.

## 4. Start: dry run, decisions, one confirmation

1. Before the dry run, tell the person it may spend one read-only provider
   turn preparing the plan (a plan without `## Stage <n> — <title>`
   headings is prepared first; a matching earlier preparation is reused).
   Then run:

   ```sh
   sparring start-plan <plan> --expected-branch <branch> --json
   ```

   Add `--context-repository NAME=PATH` for each other repository the person
   says the plan touches. Add `--allow-push-for-run` only if they asked for
   it. Without `--confirm`, start-plan starts no run.
2. Read `status`:
   - `refused`: show `error` and the fix it names, then stop.
   - `needs_decision`: ask only the questions in `decisions`, each with its
     `options` (label and consequence), using AskUserQuestion. Don't answer
     for the person and don't ask anything else. Rerun the same command
     with `--answer <decision id>=<option id>` for every answer, and read
     `status` again.
   - `ready`: continue.
3. Show a summary: the plan, `route`, the branch, each stage in `slice`
   (label, title, and any `gates_before`), the `completion_gates`,
   `later_slices` (started separately, from their own repository), any
   `findings`, the models from `show-config`, and whether push is allowed.
   On the intake route, link `intake.report` as the preparation details.
4. Ask once with AskUserQuestion: *Start this run now?* On any answer other
   than yes, stop.
5. Launch the same command with every flag and answer unchanged, plus
   `--confirm <confirm_token>` (drop `--json` if the person runs it in their
   own terminal). The engine recomputes everything and refuses if anything
   changed since the summary; then relay the refusal and start again from
   the dry run. Never reuse a token across changes.

A run can take hours and starts its own agent sessions. In order of
preference:

1. Give the person the exact command to run in their own terminal, or tell
   them to use **Agent Sparring: Run Plan** / **Resume Plan** in VS Code,
   where the Overview then follows the run.
2. If they want it launched from here, run it in the background and send
   its output to a log file **outside the repository**, for example
   `"${TMPDIR:-/tmp}/sparring-<slug>.log"`. A log inside the worktree would
   dirty the tree and make the next acceptance fail.

A resume uses the same confirmation: show the command and the branch, ask
once, then launch.

```sh
sparring resume-plan docs/plans/<slug>.md --expected-branch <branch> --evidence "<their words>"
```

## Advanced: separate prepare and approve

Only when the person asks for it. `start-plan` does the same steps in one
confirmation.

1. `sparring check-plan <plan>` lists the stages of a headed plan and runs
   nothing. `sparring run-plan <plan> --expected-branch <branch>` runs it;
   `--stop-after-stage <stage-id>` takes it one stage at a time.
2. `sparring prepare-plan <plan>` runs one read-only reviewer turn and
   writes `.sparring/intake/<id>/`. Ask before running it, since it spends
   a provider turn. The person reviews `.sparring/intake/<id>/report.md`
   and the briefs.
3. `sparring approve-plan .sparring/intake/<id> --run <slice>`, with
   `--confirm-prerequisite <gate>` only for gates the person confirms have
   happened. This prints the exact `run-plan --manifest …` command. Use it
   as printed, after the same single confirmation as above.

## 5. When it stops

Report the engine's final status as it wrote it: `NEEDS_YOU` with its
checks, `ESCALATE`, a refusal, or complete. For `NEEDS_YOU`, list each
check's instruction and pass criteria verbatim, and say that resuming needs
the person's evidence. Don't merge or push anything. Ask before any push
authorization (`--allow-push-candidate`).
