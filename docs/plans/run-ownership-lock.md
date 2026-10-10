# Run ownership lock and separate workflow/runner status (engine, P0)

**Status:** Approved direction (2026-10-10). Plan text drafted; not started.
**Repository:** `agent-sparring` only.
**Order:**
1. after repository-slices Stages 1–3 and 5 are reviewed and merged;
2. this plan;
3. before repository-slices Stage 4 (`rescope-run`), which is not yet built
   and was left out of the completed repository-slices run.

**Integration constraint:** the installed `sparring` is an editable install of
the main checkout (`~/Documents/Code/agent-sparring/src`). Do not update that
checkout's files while any run executes with it. See *Integration* at the
end.
**Priority:** P0 safety prerequisite for:
- repository-slices Stage 4 (`rescope-run`) and the ui-screenshots recovery
  that uses it;
- `docs/plans/flexible-human-workflow.md`;
- Stage 3 of the VS Code plan `authoritative-status-ux.md`.

## Why

There is no run-level mutual exclusion today. `run-plan` and `resume-plan`
hold no lock across read state → verify → record → launch. The per-worktree
`flock` (`concurrency.py`) is held only *during* an individual stage-agent or
reviewer turn. So:

- Two `resume-plan` processes on one paused run can both pass validation and
  both write `notes.md`, stage `state.json` and the run state. The second
  write wins, and evidence can be duplicated or overwritten.
- A recovery command (`reset-stage`, `reopen-stage`) can rewrite state while a
  runner sits *between* turns.
- `--accept-advanced-head`'s rollback restores byte snapshots taken at its own
  start. A refusal before its first provider turn can therefore restore stale
  bytes over another process's writes.
- `PlanRunState.save` and stage state writes are plain `write_text`, not
  atomic.
- "Is a runner live?" is answered by probing the per-turn lock
  (`managed_finish` `runner_live`). It reports *no runner* while an active
  runner is between turns. The repository-slices Stage 4 plan uses the same
  probe as its `rescope-run` precondition.

**Why `rescope-run` must not ship without this.** It archives (moves) the
current stage directory, rewrites run state to `complete` with a `scope`, and
appends to the logical-plan record. Consider a `resume-plan --evidence` for
the open gate being withdrawn, started from a terminal or from the
extension's *Submit result and continue*. Run concurrently, it can:
- append evidence into the directory being moved, recreating a partial stage
  directory or writing into the archive;
- save `status: running` over `complete`;
- start a reviewer on a stage that no longer exists in the run's scope.

A run whose status is `running`, with a runner between turns, passes today's
probe as "no live runner". Then `rescope-run` would rewrite a run that is
being executed. Neither case is fixed by holding the existing worktree lock
during `rescope-run`. Both writers must exclude each other for their whole
mutation, and the lock must not depend on which checkout path the command was
invoked from (`resume-plan --run-key` works from any checkout).

## Stage 1 — Run-scoped ownership lock and atomic engine state writes

**Outcome:** For any run, at most one engine process can mutate its state at a
time, for the whole duration of that mutation. For a runner, that is from
reading the run state until the process exits, including between turns. A
crash can never leave a torn state file.

**Hard constraints**
- **Identity.** The lock is identified by the run, not by the invoking path:
  at least the run key within the repository's git common dir, so every
  checkout and worktree of the repository resolves the same lock.
- **Logical plans.** repository-slices is merged before this stage, so
  `logical_plan.py` and its record are on `main`. An operation that mutates a
  logical plan's shared record also holds that logical plan's lock. That
  covers cross-repository continuation, which creates the next execution, and
  the future `rescope-run`.
- **Lock order.** Multiple locks are always acquired in one fixed, documented
  order: logical plan before execution run. If this stage finds that
  repository-slices is *not* on `main`, stop and report it; do not design
  around its absence.
- **Scope.** Every engine command that mutates any of these holds the lock
  for its whole mutation, including pre-launch writes:
  - a run's `.sparring/plans/<key>.json`;
  - a stage's `state.json`, `notes.md` or `sparring.md`;
  - a logical-plan record.

  That covers `run-plan`, `resume-plan` (including `--evidence`,
  `--deferred-result`, `--accept-advanced-head` and push grants),
  `reset-stage`, `reopen-stage` and `finish-run`. For a runner, the hold
  covers the process's whole life. The existing per-worktree turn lock stays
  unchanged.
- **Contention** is a non-blocking refusal with a stable reason code. It names
  the holder (pid, command, start time: diagnostic only) and writes nothing
  first. It is never a wait.
- **Kernel-released.** The lock is released by the kernel on process death
  (`flock` semantics, as in `concurrency.py`). Liveness is never decided from
  a pid or heartbeat file.
- **Rollback.** The `--accept-advanced-head` rollback restores only while the
  same process still holds the lock, so it can only restore its own writes.
- **Atomic writes.** Run state, stage state and evidence/note appends are
  atomic (temp file, fsync, rename): after a crash, either the old or the new
  file exists, whole.
- **`finish-run`.** Its `runner_live` check uses this lock.
- **Platforms.** Where the lock is unavailable (Windows today), mutating
  commands refuse as they do now for the worktree lock, rather than run
  unlocked. Any probe answers `unknown`, never "absent".
- **API.** Expose the lock as an engine API that a later recovery command can
  hold for its whole apply step, and document it as the required precondition
  for any future command that rewrites run, stage or logical-plan state. The
  first such command is repository-slices `rescope-run`.

**Starting points:** `concurrency.py`, `plan.py` (`resume_plan`, `run_plan`,
`_drive`, `_rolled_back_on_failure`, `PlanRunState.save`), `stage.py`
(state and note writes), `recovery.py`, `managed_finish.py`, `cli.py`.
`tests/test_accept_advanced_head.py` has the existing rollback tests. If the
repository-slices implementation is on `main`, also `logical_plan.py`.

**Boundaries / non-goals:** no change to routing, gates, candidate
verification, acceptance or any recovery semantics. No new user-facing
recovery. No status API (Stage 2).

**Acceptance evidence**
- Two concurrent `resume-plan` processes on one paused run (real processes,
  fake providers): exactly one proceeds, and the other is refused before any
  byte of `notes.md`, stage state or run state changes. Repeat with both
  carrying `--evidence`.
- Run with the runner held *between* turns (fake provider paused at a turn
  boundary):
  - `reset-stage`, `reopen-stage`, `finish-run` and a second resume are each
    refused;
  - `finish-run --dry-run` reports a live runner.
- A resume invoked from the main checkout with `--run-key` and one invoked
  from the managed worktree contend for the same lock.
- The `--accept-advanced-head` rollback cannot restore over another process's
  write (the other process is now refused instead).
- A crash injected at each write step leaves every state file parseable and
  consistent. SIGKILL of a runner releases the lock.
- The full suite passes: `.venv/bin/python -m pytest -q`.

**Handoff:** the lock identity, acquisition order, refusal code and the
API that recovery commands must hold, documented in `docs/reference.md`
and `docs/design.md`.

## Stage 2 — Workflow status and runner status as separate engine facts

**Outcome:** A client can ask the engine, read-only, *where the workflow is*
and *whether a runner holds the run*. These are two separate answers, and
neither is inferred from the other.

**Hard constraints**
- A new read-only command (for example `sparring run-status --run-key K
  [--repo-root R] --json`) works for managed and unmanaged runs from any
  checkout. It returns two independent objects:
  - `workflow`: run status; current stage index, id and title; stage status;
    `next_turn` (whose turn: stage agent, reviewer or finalization); the
    recorded outcome and gate instance; `evidence_pending`; `awaiting`;
    `provider_pause`. All of it is read from the engine's state files, never
    from activity telemetry or process state.
  - `runner`: `state` (`live | absent | unknown`) from the Stage 1 lock, plus
    diagnostic holder details and the timestamp of the last activity event.
- `runner.state` is the only liveness fact. A `workflow` saying it is the
  stage agent's turn with `runner.state: absent` is reported as exactly that
  (an interrupted run), and this command does not "repair" it.
- `sparring runs --json` gains the same `runner` object per run, additively.
- **Probing.** The probe never makes a starting runner fail or stall:
  - it must not acquire anything a runner contends on, or runners must
    tolerate a concurrent probe;
  - the probe and the command write nothing.
- The schema is versioned and documented in `docs/reference.md`, with a JSON
  fixture published for client parity tests.

**Boundaries / non-goals:** no deviation analysis or recovery actions (those
are in `flexible-human-workflow.md`). No UI.

**Acceptance evidence**
- `runner.state` is:
  - `live` during a turn and between turns;
  - `absent` after a normal exit and after SIGKILL;
  - `unknown` where the lock is unsupported.
- A runner started while a probe is in flight starts normally (stress test).
- Fixture tests map each workflow position to the expected `workflow` object.
  `workflow` is identical whether the runner is live or absent.
- `.sparring` and `git status` are byte-identical after the command.

## Required amendment to repository-slices Stage 4 (before it runs)

Stage 4 was left out of the completed repository-slices run, so it will run as
a new execution with a new brief. Before that run, replace the precondition
"no live runner (`concurrency.py` lock)" with the following:

- `rescope-run` acquires the run ownership lock, plus the logical-plan lock
  once it creates or appends to the logical record. It holds both from its
  first read of run state until its last write, through every apply step.
- If the lock is contended, it refuses with the lock's reason code before
  writing anything. No probe-then-act.
- Its idempotent retry runs under the same locks.
- Its tests must include a concurrent `resume-plan --evidence` against the
  gate being withdrawn: exactly one wins, and the archive, run state and
  logical record stay consistent.

The ui-screenshots recovery (repository-slices *Recovery procedure*) runs only
after that Stage 4 is accepted and merged. While it runs, the extension must
not be submitting evidence for run `ui-screenshots-f056173b-a19b12a7`.

## Integration (not a stage)

`~/Documents/Code/agent-sparring/.venv` is an editable install pointing at
`~/Documents/Code/agent-sparring/src`. Updating files in that checkout swaps
code under any live runner, which lazily imports modules, and under every new
`sparring` process, including the extension's polling. Merge only when no run
is executing with that install, or first decouple the installed engine from
the development checkout. Recording that choice is the person's decision, not
a stage's.
