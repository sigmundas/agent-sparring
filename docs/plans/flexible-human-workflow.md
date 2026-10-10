# Flexible human gates and audited workflow recovery (engine)

**Status:** Draft for review (2026-10-10). Not approved; not started.
**Repository:** `agent-sparring` only. The VS Code presentation is a separate
plan in `agent-sparring-vscode` (`docs/plans/authoritative-status-ux.md`) that
consumes the contracts defined here.

Principle: **strict about evidence and safety, flexible about workflow.** When
a paused run meets an unexpected but explainable change, the engine explains
what changed and why it matters, offers specific supported recovery actions,
and performs the chosen one itself after the human authorizes exactly that
action. The engine does not gain a generic "ignore checks" switch.

## Why (audit summary, 2026-10-10)

- **Recording and continuing are one command.** `--evidence` exists only on
  `resume-plan`. It records into `notes.md`, sets `evidence_pending`, then
  drives the reviewer in the same process. The reviewer's SEND_BACK starts
  the stage agent at once. The first half (record, set `evidence_pending`)
  already works on its own, because a plain `resume-plan` re-enters the
  reviewer when `evidence_pending` matches (`plan.py` `_evidence_awaiting_review`).
  There is just no command that stops after recording. *(Case 2)*
- **Evidence that changes the candidate has no supported path.** The paused
  pin (`next_turn_candidate`) must match exactly. The only re-pin is
  `--accept-advanced-head`. It applies to commits *beneath* an unchanged attempt
  that touch no attempt path. It needs a hand-made commit and a hand-typed SHA,
  only works together with `--evidence`, and always launches. *(Cases 1, 3)*
- **No run-level mutual exclusion.** `resume-plan`/`run-plan` hold no lock
  across verify → record → launch. The per-worktree `flock` is held only
  *during* individual stage and reviewer turns. A second `resume-plan`, or any
  recovery command, can rewrite `notes.md`, stage `state.json` and the run
  state while another runner sits between turns. It can also rewrite them
  before that runner's own next turn fails on the lock. Under
  `--accept-advanced-head`, a refusal before the first provider turn restores
  byte snapshots taken at that process's start over whatever the other process
  wrote since. `PlanRunState.save` and `Stage.write_state` are plain
  `write_text`, not atomic. The `runner_live` check in `finish-run` probes the
  per-turn lock, so between turns it reports "no runner" for an active run.
  The planned `rescope-run` precondition relies on the same probe.
- **No machine-readable runner state.** Nothing tells a client whether a
  runner is executing a run. The VS Code extension infers liveness by parsing
  `ps`. Workflow state and process liveness are separate facts, but only the
  first has an engine-owned source.

## Relationship to other plans

- `docs/plans/repository-slices.md` (implementation branch
  `sparring/repository-slices-439b82fa-93f833e0`, unmerged, Stage 4 not yet
  built) owns repository scope, `rescope-run` and cross-repository
  continuation. This plan does **not** change scope or plan content. Stage 1's
  run lock is what the planned `rescope-run` "no live runner" precondition
  should use. That branch rewrites much of `resume_plan`, so this plan should
  start from `main` *after* it merges.
- `plan/post-completion-check-amendments` (unmerged plan) adds an append-only,
  `flock`-guarded amendment log for **complete** runs. Stage 3 here adds a
  decision log for **running/paused** runs. The two logs must not diverge into
  incompatible formats. Whichever lands first defines the record envelope, and
  the other extends it (see the decision at the end).
- Execution windows / scheduling (Case 2's "not before Sunday 02:00") are
  deliberately **not** in this plan. See *Deferred* below.

## Stage 1 — Run-level runner lock, atomic state writes and runner status

**Outcome:** At most one engine process can mutate a run's state, from the
moment it reads that state until it exits, including the time between agent
turns. Every write of run state and stage state is atomic. A client can ask
the engine whether a runner currently holds a run, without parsing process
lists.

**Hard constraints**
- `run-plan`, `resume-plan` and every command that mutates a run's
  `.sparring/plans/<key>.json`, a stage's `state.json`, `notes.md` or
  `sparring.md` hold one exclusive, run-scoped lock for that whole mutation.
  For a runner, that is the process's whole life. Contention is a
  non-blocking refusal with a stable code that names the holder; it is never a
  wait and never a race. The existing per-worktree turn lock stays exactly as
  it is.
- The lock is kernel-released on process death, like `concurrency.py`'s
  `flock`. Liveness is never decided from a pid file. A pid, start time, run
  key and command recorded next to the lock are diagnostic only.
- A liveness probe must never make a starting runner fail or delay it
  materially. Probing a free lock must not acquire anything a runner could
  then contend on, or the runner must tolerate such a probe.
- The rollback around `--accept-advanced-head` may restore only what this
  process itself wrote, while it still holds the lock. It must never restore a
  snapshot over another process's writes.
- `PlanRunState.save`, stage state writes and evidence appends become atomic
  (temp file, fsync, rename). A crash leaves either the old or the new file,
  never a torn one.
- `finish-run`'s `runner_live` check uses the run lock, not the per-turn
  worktree lock.
- Windows: if the lock cannot be implemented, the probe answers `unknown`.
  It never answers `absent`.

**Client contract (additive):** `sparring runs --json` gains a per-run
`runner` object: `{"state": "live" | "absent" | "unknown", "pid", "started_at",
"command"}`. Only `state` is authoritative. A read-only `sparring run-status
--run-key K [--repo-root R] --json` returns the same object for any run,
managed or not, together with the run's workflow position (plan status,
current stage, stage status, `next_turn`, recorded outcome, `evidence_pending`,
`awaiting`). That is everything a client needs to say whose turn it is.
Document both in `docs/reference.md`. Additive fields keep `schema_version`
unless the reviewer finds the change incompatible.

**Starting points:** `concurrency.py`, `plan.py` (`resume_plan`, `_drive`,
`_rolled_back_on_failure`, `PlanRunState.save`), `stage.py` (`write_state`,
`append_note`), `recovery.py` (`reset_stage`, `reopen_for_failed_check`),
`managed_finish.py` (`runner_live`), `cli.py` (`_cmd_runs`).
`tests/test_accept_advanced_head.py` has the existing rollback tests.

**Boundaries / non-goals:** no change to verdict routing, gates, candidate
verification or acceptance. No new recovery behavior. No UI.

**Acceptance evidence**
- A test runs two concurrent `resume-plan` processes on one paused run: exactly
  one proceeds and the other is refused before writing anything (byte-identical
  `notes.md`, stage and run state).
- A test runs a recovery command (`reset-stage`, `reopen-stage`, a resume with
  `--accept-advanced-head`) while a runner sits *between* turns: it is
  refused, and it no longer rewrites state behind the runner.
- A test: the `--accept-advanced-head` rollback never restores over a write it
  did not make.
- An atomic-write test: a write interrupted at the replace step leaves
  parseable state.
- Tests: `runner.state` is `live` during and between turns, `absent` after
  normal exit and after SIGKILL, and a probe does not break a runner starting
  at the same moment.
- `finish-run --dry-run` reports `runner_live: false` for a runner that is
  between turns.
- The full suite passes: `.venv/bin/python -m pytest -q`.

**Handoff:** the name and location of the run lock, the refusal code, and the
`runner` / `run-status` JSON shapes, recorded in `docs/reference.md` for
Stages 2–5 and the VS Code plan.

## Stage 2 — Deviation report: invariant, recoverable deviation, warning

**Outcome:** For any recorded run, the engine can explain *why it cannot
simply continue*. The explanation is in one machine-readable report that
separates three classes:

- **invariant** — a hard safety property is violated or unprovable. No action
  continues the run past it. The report says what would have to be true. The
  property list: candidate content differs from its pin in a way no supported
  action explains; wrong branch or a protected branch is checked out;
  ownership or worktree-record mismatch; plan digest changed; a sibling pin
  moved; an accepted stage's history would change; the gate instance does not
  match.
- **recoverable deviation** — unexpected, explainable, and covered by a
  supported action. The report gives what changed (commits, paths, short
  diff stat), why it blocks continuation, the risk, and the specific action(s)
  that would recover. Each action carries a **confirmation token** bound to
  the exact facts it was computed from.
- **warning** — informational. It never blocks continuation.

**Hard constraints**
- Read-only. Producing the report writes nothing and takes no lock that could
  block a runner. It may report `runner.state: live`, and then it offers no
  mutating action.
- The classifier reuses the engine's existing proofs (`verify_candidate`,
  `repin_after_authorized_advance`, the plan-digest and ownership checks). It
  never adds a looser parallel version of them. Anything it cannot prove
  recoverable is an `invariant`.
- A token binds at least: run key, stage id, gate instance, pinned candidate
  identity, current HEAD and content digest, action kind and action
  parameters. A token is recomputed and compared when the action executes. Any
  difference refuses with "the repository changed since you reviewed this".
  Reuse the confirm-token approach `plan_start` already has where it fits.
- Each offered action names an existing or planned engine command. Existing
  recovery commands are classified, not reimplemented: `reset-stage`,
  `reopen-stage`, fresh sessions, and `rescope-run` once repository-slices
  provides it. Stages 4 and 5 register their actions here.
- Stable reason codes, each with a human sentence, documented in
  `docs/reference.md`.

**Interface:** `sparring run-status … --json` (from Stage 1) gains a
`deviations` list. Each entry has `class`, `code`, `summary`, `detail`,
`evidence` and `actions: [{kind, command, token, effect}]`. A human-readable
form prints by default.

**Boundaries / non-goals:** no new mutating operation in this stage.
`resume-plan`'s refusals keep their current behavior and wording, though they
may add a pointer to `run-status`.

**Acceptance evidence**
- Fixture-driven tests for each class:
  - a paused stage whose branch advanced beneath an unchanged attempt →
    recoverable, offering today's `--accept-advanced-head` command with a
    token;
  - an attempt file edited while paused → recoverable, offered as a candidate
    amendment, which Stage 5 executes (until then the action is listed as
    unavailable);
  - an advance that touches attempt paths → recoverable only as an amendment,
    never as a prerequisite advance;
  - rewritten history, a moved sibling pin, a protected branch, or an edited
    plan stage section → invariant, with no action;
  - a live runner → no mutating actions.
- A token is refused after any repository change between report and use.
- The report changes nothing (byte comparison of `.sparring` and the
  `git status` output).

**Handoff:** the deviation codes, the action registry, and the token contract
in `docs/reference.md`.

## Stage 3 — Record human-check outcomes without continuing the run

**Outcome:** A person can record one or several human-check answers, and
further notes, into a paused run without starting any agent. Continuing the run
is a separate action. `resume-plan --evidence` keeps working as "record, then
continue".

**Hard constraints**
- New command (name is the stage agent's choice, for example
  `sparring record-evidence`). It writes the answers into the current stage's
  `notes.md` `## Human evidence` in the engine's existing shape, so
  `gate_answers.py` and the sparrer prompt read it unchanged. It sets
  `evidence_pending` exactly as `resume-plan --evidence` does. It exits
  without launching anything. A later plain `resume-plan` continues
  reviewer-first through the existing `_evidence_awaiting_review` path.
- Answers name their gate **instance** and check ids. An answer to an instance
  that is not the stage's current open gate is refused. Recording never
  answers a deferred or plan-declared gate. Those keep `--deferred-result`.
- **Idempotent and atomic.** Each submission carries an id, supplied by the
  caller or derived from the content. Repeating the same submission records
  nothing new and succeeds. A different submission for the same instance is
  appended, never overwrites, and history stays. An interrupted write leaves
  either the whole entry or none of it. The whole operation runs under
  Stage 1's run lock and is refused while a runner holds the run.
- Recording is allowed when the working tree no longer matches the pin. The
  answer is the person's observation, and the drift is a separate fact. The
  entry records the pinned candidate identity it was given against. Starting
  the reviewer still requires the candidate to verify (unchanged rule), and
  the refusal points at `run-status`. This is what lets Case 1 record its
  evidence first and then resolve the drift (Stages 4 and 5).
- A per-run, append-only **decision log** records each submission's id, gate
  instance, check outcomes, pinned candidate, time and actor (`person`). The
  log is registered in `artifact_ownership.py` with the engine as sole writer.
  If the post-completion check-amendment log
  (`.sparring/plans/amendments/<run>.jsonl`) exists on `main` when this stage
  starts, extend that format and its locking rather than adding a second one.
  Otherwise, define a versioned record envelope that it can adopt. Either way,
  the stage report says which. `notes.md` remains the canonical text agents
  read. The log is the audit and idempotency record.
- `resume-plan --evidence …` behaves exactly as before, as a composition of
  record + continue, under one lock hold. Its existing tests pass unchanged.

**Boundaries / non-goals:** no candidate changes and no re-pinning (Stages 4
and 5). No scheduling. No change to how the reviewer judges evidence.

**Acceptance evidence**
- Record → no provider started (fake providers assert zero turns) →
  `run-status` shows evidence recorded and awaiting review → `resume-plan`
  starts the reviewer first, against the same pin.
- Replaying the identical submission is a no-op. A second, different answer
  for the same instance appears as history. Answering a superseded instance is
  refused.
- Recording while a runner holds the run is refused with nothing written.
- Killing the process mid-write leaves notes and the decision log consistent
  (either both have the entry or neither does). Recovery through retry is
  covered.
- Recording against a drifted candidate succeeds and is labelled with the
  pin. The following `resume-plan` refuses to start the reviewer until the
  drift is resolved.
- Existing evidence tests pass unchanged: `tests/test_plan.py` evidence and
  `--accept-advanced-head` cases, `tests/test_accept_advanced_head.py`.

**Handoff:** the command, its JSON result, the decision-log schema and the
idempotency semantics, in `docs/plans.md` *Answering a human gate* and
`docs/reference.md`.

## Stage 4 — Authorized prerequisite advance as a standalone action

**Outcome:** When a paused stage's branch advanced beneath an unchanged
attempt (Case 3), the person authorizes "accept these commits as a
prerequisite" from Stage 2's report. The engine re-pins without requiring
evidence and without launching. The person never has to find or type a SHA.

**Hard constraints**
- The proof is exactly `repin_after_authorized_advance` as it stands:
  - the accepted head is the current HEAD on the run's branch;
  - the pinned head is a strict ancestor of it;
  - sibling pins are unchanged;
  - no path in the range overlaps uncommitted attempt content;
  - untracked files are byte-identical;
  - content and index, minus the range, reproduce the pinned digests.

  Nothing in that proof is relaxed.
- Authorization is Stage 2's token. The action executes only if the
  recomputed token matches, and only under the run lock.
- Allowed while the stage waits on a recorded NEEDS_YOU. The answer may be
  recorded before or after (Stage 3). The gate instance is unchanged, because
  the asking did not change. Today's restriction to NEEDS_YOU stays. Any
  extension to other paused states (ESCALATE, a failed review turn) must be
  argued in the stage report and is otherwise out of scope.
- Audit:
  - the decision log gets `{action: prerequisite_advance, previous_pin,
    new_pin, commits, paths, token digest, actor, time}`;
  - `notes.md` gets the existing human-readable note;
  - an activity event is emitted.

  The previous pin stays recoverable from the log, so it does not survive only
  as prose.
- `--accept-advanced-head <sha>` keeps working with its current semantics
  and tests. It is implemented on the same path and logs the same record.

**Boundaries / non-goals:** no changes to the attempt itself (Stage 5).
Never a rebase, reset or any history rewrite. No automatic re-pin without a
token.

**Acceptance evidence**
- The scenarios already in `tests/test_accept_advanced_head.py` all pass
  through the new action as well: each refusal still refuses, and accepted
  cases re-pin identically.
- A replayed or stale token is refused. Nothing launches. The decision log
  holds the old and new pins.
- A stage with recorded evidence, then the advance action, then `resume-plan`:
  the reviewer judges the new pin first.

## Stage 5 — Authorized evidence amendment of the candidate

**Outcome:** While a stage waits on a human, a person can approve a bounded
change to the candidate itself, for example measurements written into
`docs/perf/baseline-2026-10.md` (Case 1). The engine previews it, commits
exactly the approved change on the run's branch, re-pins, and requires the
reviewer to judge the amended candidate. No manual Git is needed.

**Hard constraints**
- The person approves a **named path set**. The preview (Stage 2 action) lists
  the exact changed paths and diff (stat plus full text for text files, with a
  size cap) and binds them into the token. Execution refuses if any path
  outside the approved set differs from the pin (tracked, staged or
  untracked), or if the approved paths' content differs from the preview.
  Unrelated changes are refused, never silently left out.
- Amendments touch only files tracked in the candidate repository. They never
  touch `.sparring/` stage or run artifacts, the input plan, or another
  repository. An approved path inside the plan's executable content is refused
  (the plan-digest invariant).
- If the pinned candidate is a commit, the engine makes **one** commit with
  exactly the approved paths, on the run's expected branch, with a trailer
  naming the run, stage, gate instance and decision id. It never amends,
  rebases or force-pushes. If the pinned candidate is a deliberately
  uncommitted worktree candidate, the amendment re-pins the working tree
  without committing, and the existing finalization comparison applies later.
- After an amendment the reviewer **must** review the amended candidate
  before any acceptance: `next_turn` is the reviewer, and `evidence_pending`
  is set so it gets the evidence turn. The previous review's gate stays
  recorded. No acceptance carries over from the previous pin.
- Branch/protected-branch guard, run lock, token match and ownership checks
  apply exactly as for agent turns. Push follows the run's existing push
  policy. Never push implicitly.
- Audit: the decision log records the previous pin, the new pin and commit,
  the approved paths, a diff digest, the gate instance and the actor. The note
  in `notes.md` states that the candidate was amended by a person and lists
  the paths.

**Boundaries / non-goals:** not a way to change plan scope, accepted stages
or another repository. Not available while a runner holds the run, or for an
accepted stage (that is `reopen-stage`'s or a follow-up stage's job).

**Acceptance evidence**
- Case 1 replay as a test: paused NEEDS_YOU on a committed candidate → a person
  edits one approved file → `run-status` offers an amendment for exactly that
  path → executing it creates one commit with exactly that path → `resume-plan`
  starts the reviewer on the new pin.
- Refusals:
  - an extra changed file;
  - the approved file changed after preview;
  - the plan file;
  - `.sparring/`;
  - wrong branch;
  - live runner;
  - accepted stage;
  - stale token.
- The uncommitted-candidate variant re-pins without a commit, and finalization
  later compares against the amended tree.
- No path exists from an amendment to acceptance without a reviewer turn on
  the new pin (asserted through fake providers).

## Stage 6 — Adversarial integration, documentation and planning skill

**Outcome:** The recovery operations are proven together under concurrency
and failure, documented as one model, and reflected in the planning skill.

**Scope**
- End-to-end tests with fake providers:
  - Case 1: record → amend → continue → READY;
  - Case 2: record now → no agent → continue later;
  - Case 3: prerequisite lands while paused → authorize advance → continue;
  - interleavings of each with a second `resume-plan`, a recovery command and
    a SIGKILL at every write step, showing the run lock and atomic writes
    leave consistent, auditable state.
- `docs/gates.md` / `docs/plans.md` / `docs/reference.md`: one section,
  *When a paused run cannot simply continue*, covering the three classes, the
  report, tokens, and each action and its proof. `docs/design.md`: record the
  invariant/deviation/warning contract and that the engine performs recovery.
- `skills/sparring-plan` and `skills/sparring-run`, only where behavior
  changed:
  - a human gate that is a scheduling or permission decision is not a Pass
    check that releases immediate work;
  - evidence that will be committed into the repository should be named in
    the stage so an amendment is expected;
  - the run skill uses `run-status` and the recovery actions instead of
    teaching Git plumbing.

**Acceptance evidence:** the full suite passes. Docs examples match real
`run-status` output from a test fixture. `git diff --check` is clean.

## Deferred (not in this plan)

- **Execution windows / scheduling.** The engine can enforce a window only at
  turn boundaries: refuse to start a turn outside it, and pause at the next
  boundary after it closes. It cannot police commands inside a running turn.
  Production-safety for measurements also belongs in project tooling, for
  example scripts that refuse outside an authorized window. Turn-boundary
  enforcement fits naturally after Stage 3 (record now, continue later) and is
  a candidate for a small follow-up plan. Automatic start at the window's
  opening is scheduling and is out of scope.
- **Changing plan scope or contracts mid-run.** By engine invariant, changed
  executable content needs a newly reviewed plan or run identity. Scope moves
  are owned by repository-slices `rescope-run`. This plan only lists those
  actions in the deviation report once they exist.

## Decisions needed before approval

1. Land order: repository-slices first (recommended, because it reshapes
   `resume_plan`), then this plan from `main`.
2. One shared run decision log for this plan and post-completion amendments
   (recommended), or two separate files with a shared envelope.
3. Whether recording evidence against a drifted candidate (Stage 3) is
   acceptable. Recommended: yes, because the reviewer still cannot start until
   the candidate verifies.
4. Whether execution windows become a follow-up plan now or wait.
