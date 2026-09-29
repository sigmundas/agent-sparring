---
name: sparring-plan
description: Turn the current discussion (an audit, design conversation or bug investigation) into a staged agent-sparring plan under docs/plans/, grounded in the repository's real code, and validate it with the engine without running it. Invoke explicitly; writing a plan never approves or starts it.
argument-hint: "[plan name or emphasis]"
disable-model-invocation: true
allowed-tools: Read Grep Glob Write Edit Bash(sparring check-plan:*) Bash(sparring check-config:*) Bash(git status:*) Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(git ls-files:*) Bash(git rev-parse:*) Bash(git branch:*)
---

# Write a staged plan

The input is the conversation so far: findings, decisions, constraints and
open questions. The output is one Markdown plan that the engine can run stage
by stage, and that a person will read and approve before anything runs.
`$ARGUMENTS` may name the plan or say what to emphasise.

This skill writes and checks a file. It does **not** approve the plan, create
a branch, commit anything, or start a run. Producing a plan is not a signal
to begin.

## 1. Collect what was decided

List what the discussion actually settled, and separately what is still open.
If an open question changes the shape of the work (what to build, not how),
ask it now instead of guessing. Leave out ideas the discussion rejected.

## 2. Ground it in the repository

Read `.sparring/PROJECT.md` if it exists, then the code the work touches.
Search by symbol and read bounded ranges. Every file, function, command and
test you name in the plan must exist, or be explicitly marked as new. Record
the test command and the current baseline if PROJECT.md doesn't already.

## 3. Cut it into stages

A stage is one reviewable commit's worth of work that a fresh reviewer can
judge on its own. Prefer more, smaller stages over one large one. Put
risky or foundational changes first, and keep refactors separate from
behavior changes.

Write `docs/plans/<slug>.md` (don't overwrite an existing file; pick a new
name). The engine's convention is strict:

```markdown
# <Plan title>

<Context every stage needs: goal, constraints, invariants, decisions made.
Text above the first stage heading is NOT sent to stage agents. Repeat
anything a stage must know inside that stage.>

## Stage 1 — <Title>

**Goal:** one or two sentences.
**Scope:** files/modules/areas this stage may change.
**Depends on:** earlier stages, or "none".
**Acceptance criteria:** concrete, checkable; name the tests or commands.
**Non-goals:** what this stage must not touch.
**Human checks:** only checks an agent cannot do (device QA, visual review,
a product decision, a production action). Omit when there are none.

## Stage 2 — <Title>
...
```

Rules the engine enforces:

- Headings are exactly `## Stage <n> — <title>`, numbered 1..N in order
  (an en dash, hyphen or colon also works as the separator).
- Every stage has content. Deeper headings (`###`) belong to the stage.
- Only the stage's own section becomes its brief. Keep each stage
  self-contained.

Put a human check in the stage where it genuinely applies. The run pauses
there and waits for the person's evidence. Don't invent checks for things
tests can verify.

## 4. Validate without running

```sh
sparring check-plan docs/plans/<slug>.md
```

It lists the stages the engine will see and runs nothing. Fix and re-check
until it passes.

If the plan spans several repositories, or its structure really can't use
the heading convention, say so. [Plan intake](../../docs/intake.md)
(`sparring prepare-plan`, experimental) is the path for that, and
`/agent-sparring:sparring-run` handles it. Don't run `prepare-plan` from
this skill, because it starts an agent turn.

## 5. Hand over

Report the path, the stage list, and any open questions you left in the
plan. Say plainly that the plan still needs the person's review, and that
running it is a separate step (`/agent-sparring:sparring-run` or VS Code's
**Run Plan**). Leave the file uncommitted unless asked.
