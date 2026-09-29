---
name: sparring-setup
description: Prepare the current repository for agent-sparring - check the sparring CLI and the claude/codex providers, diagnose the project setup with the engine, and, only with the person's approval, create .sparring/project.toml, apply engine-owned fixes and write .sparring/PROJECT.md. Invoke explicitly.
argument-hint: "[optional: repository path]"
disable-model-invocation: true
allowed-tools: Read Grep Glob Write Edit AskUserQuestion Bash(sparring:*) Bash(git status:*) Bash(git rev-parse:*) Bash(git ls-files:*) Bash(git check-ignore:*) Bash(git branch:*) Bash(claude --version) Bash(codex --version) Bash(command -v:*) Bash(ls:*)
---

# Set up a repository for agent-sparring

The engine owns the configuration schema and every setup fix. This skill runs
engine commands and reports what they say. It never hand-writes
`project.toml` or the user preference file, and never edits `.gitignore`
itself when `fix-config` can.

Work in the repository given in `$ARGUMENTS`, or the current one. Run every
`sparring` command from that repository's root.

**Diagnose first, change nothing yet.** Steps 1 and 2 are read-only. Every
write in step 3 (`init-config`, `fix-config`, `PROJECT.md`) happens only after
the person has seen what it will change and said yes.

## 1. Prerequisites (read-only)

Check each of these and report all failures together:

- `command -v sparring`. If it is missing, the engine is not installed. Tell
  the person to run `uv tool install --editable <path-to-agent-sparring>` or
  `pip install -e <path>`, then stop.
- `claude --version` (implementation agent) and `codex --version`
  (reviewer). A missing CLI is only a problem for a role configured to use
  it. Signing in is the person's job; do not try to authenticate.
- `git rev-parse --show-toplevel` succeeds. agent-sparring only works inside
  a git repository.

## 2. Diagnose (read-only)

- Does `.sparring/project.toml` exist? Does `.sparring/PROJECT.md`?
- If `project.toml` exists, run `sparring show-config --json` and read
  `setup_problems`:
  - `kind: "not-ignored"`: a workflow-state directory git can see. The fix
    appends that `.gitignore` line.
  - `kind: "obsolete-agent-setting"`: `project.toml` still sets a `model` or
    `effort`, which the engine no longer reads. The fix removes the key. It
    does **not** choose a preference for the person.
  - any other kind: show the engine's `message` verbatim. Don't invent a fix.
- Report the providers and where each model/effort comes from (`user`
  means the person's own preference; `provider-default` means none is set).

Summarise what you found and list the exact changes you propose.

## 3. Apply, after approval

Ask once with AskUserQuestion which proposed changes to make. Then, only for
the approved ones:

- `sparring init-config` creates `.sparring/project.toml`. It never
  overwrites an existing file.
- `sparring fix-config` applies the engine's own fixes for the problems
  listed above, and nothing else.
- **PROJECT.md** is prose that every agent in a run receives as context.
  Draft it with the person from facts you verify in the working tree, never
  from memory:
  - what the project is, and its stack
  - a directory map of the parts agents will touch
  - exact build, test and lint commands
  - conventions and invariants that must never be broken
  - manual or device checks that only a person can do
  - the **current baseline test results**, from actually running the tests,
    including known pre-existing failures

  Show the draft and write it only after the person agrees. Keep it short.
  Agents read all of it on every turn.

## 4. Models and effort are the person's own

Model and effort are not project settings. They are the person's
preferences, shared by every repository and kept per role and provider in
their user configuration (`show-config` prints its path). Change them only
when the person asks, never as part of a setup fix:

```sh
sparring model-choices                                   # what to choose from
sparring set-config stage --model <exact id> --effort <level>
sparring set-config sparring --model <exact id>
```

Use exact model ids, not aliases such as `opus`; the engine refuses aliases.
The VS Code cockpit edits the same preferences. A project's provider is
changed with `sparring set-config <role> --provider <id>`, which does write
`project.toml`.

## 5. Verify and explain what to commit

Run `sparring check-config`. It must exit 0, report `git-ignored: yes` for
stage artifacts, plan-run state and plan intake, and print no `obsolete:`
lines.

Then tell the person:

- **Commit:** `.sparring/project.toml`, `.sparring/PROJECT.md` and
  `.gitignore`. Runs require a clean worktree, so these must be committed
  before the first run.
- **Never commit:** `.sparring/stages/`, `.sparring/plans/` and
  `.sparring/intake/`. They are runtime state the engine rewrites during a
  run.
- **Not in any repository:** model and effort preferences, in the user
  configuration file.

Don't commit on the person's behalf unless they ask. The next step is a plan:
`/agent-sparring:sparring-plan`, or one written by hand.
