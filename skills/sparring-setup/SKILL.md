---
name: sparring-setup
description: Prepare the current repository for agent-sparring - check the sparring CLI and the claude/codex providers, create .sparring/project.toml with the engine, help write .sparring/PROJECT.md, and fix setup problems with the engine's own fix-config. Use when someone wants to start using agent-sparring in a repository or check-config reports a problem.
argument-hint: "[optional: repository path]"
allowed-tools: Read Grep Glob Write Edit Bash(sparring:*) Bash(git status:*) Bash(git rev-parse:*) Bash(git ls-files:*) Bash(git check-ignore:*) Bash(git branch:*) Bash(claude --version) Bash(codex --version) Bash(command -v:*) Bash(ls:*)
---

# Set up a repository for agent-sparring

The engine owns the configuration schema and every setup fix. This skill runs
engine commands and reports what they say. It never hand-writes
`project.toml`, and never edits `.gitignore` itself when `fix-config` can.

Work in the repository given in `$ARGUMENTS`, or the current one. Run every
`sparring` command from that repository's root.

## 1. Prerequisites

Check each of these and report all failures together:

- `command -v sparring`. If it is missing, the engine is not installed. Tell
  the person to run `uv tool install --editable <path-to-agent-sparring>` or
  `pip install -e <path>`, then stop.
- `claude --version` (implementation agent) and `codex --version`
  (reviewer). A missing CLI is only a problem for a role configured to use
  it. Signing in is the person's job; do not try to authenticate.
- `git rev-parse --show-toplevel` succeeds. agent-sparring only works inside
  a git repository.

## 2. Project configuration

- If `.sparring/project.toml` does not exist, run `sparring init-config`.
  It never overwrites an existing file.
- To change a provider, model or effort, use
  `sparring set-config <stage|sparring> --provider|--model|--effort …`.
  Don't edit the TOML by hand. Add `--local` to change only this worktree
  without dirtying the repository.
- Run `sparring show-config --json`. For each entry in `setup_problems`:
  - `kind: "not-ignored"`: run `sparring fix-config`. It appends exactly the
    missing `.sparring/stages/`, `.sparring/plans/` and `.sparring/intake/`
    lines to `.gitignore`.
  - any other kind: show the engine's `message` verbatim and ask the person.
    Don't invent a fix.

## 3. PROJECT.md

`.sparring/PROJECT.md` is prose that every agent in a run receives as context.
The engine never interprets it. If the file is missing or thin, draft it with
the person from facts you verify in the working tree, never from memory:

- what the project is, and its stack
- a directory map of the parts agents will touch
- exact build, test and lint commands
- conventions and invariants that must never be broken
- manual or device checks that only a person can do
- the **current baseline test results**, from actually running the tests,
  including known pre-existing failures

Show the draft and write it only after the person agrees. Keep it short.
Agents read all of it on every turn.

## 4. Verify and explain what to commit

Run `sparring check-config`. It must exit 0 and report `git-ignored: yes`
for stage artifacts, plan-run state and plan intake.

Then tell the person:

- **Commit:** `.sparring/project.toml`, `.sparring/PROJECT.md` and
  `.gitignore`. Runs require a clean worktree, so these must be committed
  before the first run.
- **Never commit:** `.sparring/stages/`, `.sparring/plans/` and
  `.sparring/intake/`. They are runtime state the engine rewrites during a
  run.
- **Local only, and invisible to git:** `set-config --local` values, stored
  in the worktree's git directory.

Don't commit on the person's behalf unless they ask. The next step is a plan:
`/agent-sparring:sparring-plan`, or one written by hand.
