---
name: sparring-run
description: Start or resume an agent-sparring plan run for a reviewed plan - verify config, branch and worktree through the engine, then launch run-plan or resume-plan (or the experimental prepare-plan/approve-plan intake path). Invoke explicitly; asks for confirmation before any agent turn starts.
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
- **A plan with stage headings (the normal case):** run
  `sparring check-plan <plan>` and show the stage list. That is what will run.
- **A plan intake (experimental; for multi-repository or free-form plans):**
  1. `sparring prepare-plan <plan>` runs one read-only reviewer turn and
     writes `.sparring/intake/<id>/`. Ask before running it, since it spends
     a provider turn.
  2. The person reviews `.sparring/intake/<id>/report.md` and the briefs.
  3. `sparring approve-plan .sparring/intake/<id> --run <slice>`, with
     `--confirm-prerequisite <gate>` only for gates the person confirms have
     happened. This command prints the exact `run-plan --manifest …`
     command. Use that command as printed.

## 4. Confirm, then launch

Show the person the command, the branch and the stages, and ask once with
AskUserQuestion: *Start this run now?* On any answer other than yes, stop.

A run can take hours and starts its own agent sessions. In order of
preference:

1. Give the person the exact command to run in their own terminal, or tell
   them to use **Agent Sparring: Run Plan** / **Resume Plan** in VS Code,
   where the Overview then follows the run.
2. If they want it launched from here, run it in the background and send
   its output to a log file **outside the repository**, for example
   `"${TMPDIR:-/tmp}/sparring-<slug>.log"`. A log inside the worktree would
   dirty the tree and make the next acceptance fail.

Typical commands:

```sh
sparring run-plan docs/plans/<slug>.md --expected-branch <branch>
sparring resume-plan docs/plans/<slug>.md --expected-branch <branch> --evidence "<their words>"
```

`--stop-after-stage <stage-id>` takes the plan one stage at a time.

## 5. When it stops

Report the engine's final status as it wrote it: `NEEDS_YOU` with its
checks, `ESCALATE`, a refusal, or complete. For `NEEDS_YOU`, list each
check's instruction and pass criteria verbatim, and say that resuming needs
the person's evidence. Don't merge or push anything. Ask before any push
authorization (`--allow-push-candidate`).
