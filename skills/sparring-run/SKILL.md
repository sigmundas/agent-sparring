---
name: sparring-run
description: Start or resume an agent-sparring plan run for a reviewed plan - verify config, branch and worktree through the engine, then dry-run start-plan, ask its decision questions and launch it with one confirmation (or resume-plan; prepare-plan/approve-plan as an advanced path). Invoke explicitly; says before the dry run that it may spend one read-only preparation turn, and asks one confirmation before the run starts.
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
next stage. Don't pass `--stage-model` or similar flags to an ordinary
resume to force a change: the engine refuses an override that contradicts
the stage's recorded configuration. The one exception is a fresh session
(step 6): the new conversation may be given a different model or effort.

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
   headings is prepared first; a matching earlier preparation is reused),
   and that each rerun with `--answer` may spend a further read-only
   preparation turn. Then run:

   ```sh
   sparring start-plan <plan> --expected-branch <branch> --json
   ```

   Add `--context-repository NAME=PATH` for each other repository the person
   says the plan touches, and `--repository-branch NAME=BRANCH` for a
   declared sibling on a branch other than the one intake saw; the engine
   refuses a `--repository-branch` for a repository the run does not
   declare. Add `--allow-push-for-run` only if they asked for
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
   the dry run. The token binds every execution input the summary showed:
   models, effort, permission mode, executables, max send-back cycles,
   push, sparring dir, repositories and answers. Changing any of them needs
   a fresh dry run and a new token; never reuse a token across changes.

Launching does not pass the plan's gates: a gate still pauses the run when
it is reached, with `NEEDS_YOU`, which you relay as in step 5.

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

## 6. Resuming after a pause

There are three ways to continue a stage, and they are not interchangeable:

- **Ordinary resume** — `sparring resume-plan <plan> --expected-branch
  <branch>`. Continues the same conversations, with the configuration the
  stage recorded. The default.
- **Fresh-session resume** — the same command plus `--fresh-sparrer`
  (reviewer) or `--fresh-stage-agent` (implementation agent), optionally
  with `--fresh-reason <text>`. Replaces only that role's conversation: same
  stage, same candidate, earlier exchange kept as history, a new generation
  recorded. Use it when that conversation cannot or should not continue —
  the engine said it cannot be resumed, or the person wants a clean context.
  Role-scoped `--stage-model` / `--stage-effort` or `--sparring-model` /
  `--sparring-effort` may choose the new conversation's model or effort.
  Each role supports exactly one provider today, so a different
  `--stage-provider` / `--sparring-provider` is refused; don't try one.
- **`sparring reset-stage`** — recovery for a stage that ran under the
  wrong mode (implementation vs independent review) compared with what the
  plan input now declares: archives that attempt as history and restarts
  the stage. The repository must already be at the preceding accepted
  candidate; the command verifies this and does not move HEAD there. It
  refuses a stage that ran in the declared mode, so it is not a way to
  discard an unwanted attempt. Use it only when the engine names it in a
  refusal. Ask first.

The engine decides whose turn runs next, from its recorded `next_turn`.
Don't pick one. `--next-turn stage|sparring` exists only for a legacy stage
whose turn the engine refused to derive, and said so; then the person
chooses, and the engine records it.

When a provider fails, the engine pauses the run, prints the retry to use
and records `provider_pause` in the plan-run state. Read it there or in its
printed output; never edit it, and run the printed commands as printed.

- **`session-unresumable`**: the named role's conversation cannot continue.
  Run the printed retry, which adds `--fresh-sparrer` or
  `--fresh-stage-agent` with `--fresh-reason session-unresumable`. If that
  role has no recorded conversation (`has_session: false`), a plain resume
  is all there is.
- **`provider-unavailable`** (quota, rate limit, overloaded): either wait
  and run the printed plain retry, which continues the same conversation, or
  run the printed fresh alternative, optionally with a different model or
  effort. Moving to another backend or account is set up in the provider
  CLI itself, outside the engine.

In every case the candidate is unchanged. Never edit anything under
`.sparring/` to get past a pause; if the engine refuses a resume, relay its
message and stop, as for any refusal.
