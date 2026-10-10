# Flexible human gates and audited workflow recovery (engine)

**Status:** Approved direction with decisions (2026-10-10). Not started.
**Repository:** `agent-sparring` only. The VS Code presentation is a separate
plan in `agent-sparring-vscode` (`docs/plans/authoritative-status-ux.md`) that
consumes the contracts defined here.
**Prerequisites:**
- `docs/plans/run-ownership-lock.md` (P0), both stages accepted and merged.
- repository-slices merged to `main`. This plan changes `resume_plan`, which
  that work reshapes.

Principle: **strict about evidence and safety, flexible about workflow.** When
a paused run meets an unexpected but explainable change, the engine explains
what changed and why it matters, offers specific supported recovery actions,
and performs the chosen one itself after the human authorizes exactly that
action. There is no generic "ignore checks" switch. Human authorization never
overrides a live runner. Every mutating action holds the run ownership lock.

## Why (audit summary, 2026-10-10)

- **Recording and continuing are one command.** `--evidence` exists only on
  `resume-plan`. It records into `notes.md`, sets `evidence_pending`, then
  drives the reviewer in the same process. The reviewer's SEND_BACK starts
  the stage agent at once. The record half already works on its own: a plain
  `resume-plan` re-enters the reviewer when `evidence_pending` matches
  (`plan.py` `_evidence_awaiting_review`). *(Case 2: a production measurement
  window was approved for Sunday 02:00. Recording it on Saturday started the
  stage agent, which then kept a provider turn alive for about 15 hours,
  sleeping until the window.)*
- **Evidence that changes the candidate has no supported path.** The paused
  pin (`next_turn_candidate`) must match exactly. The only re-pin is
  `--accept-advanced-head`, which covers commits *beneath* an unchanged
  attempt that touch no attempt path. It needs a hand-made commit and a
  hand-typed SHA, only works together with `--evidence`, and always launches.
  *(Cases 1, 3)*

## Approved decisions (2026-10-10)

1. **Order.**
   1. repository-slices: review and merge its implemented Stages 1–3 and 5.
   2. run-ownership-lock (P0).
   3. repository-slices Stage 4 (`rescope-run`), held for its whole apply
      step under the run ownership lock, and only then the ui-screenshots
      recovery.
   4. This plan.

   `rescope-run` must not be built, or used for recovery, before the run
   ownership lock exists. See `run-ownership-lock.md` *Why*.
2. **Audit record.** Use one append-only audit record format, shared with the
   post-completion check amendments plan. The two plans keep distinct
   operation kinds, each with its own validation rules. Neither plan's
   implementation depends on the other's code beyond that format.
3. **Evidence during candidate drift** may be recorded durably against the
   correct gate instance. It stays *unverified / pending review* until the
   candidate is reconciled and independently reviewed. Candidate integrity
   checks are not weakened.
4. **Execution windows** are deferred to a separate follow-up. The preferred
   shape is a minimal `not-before` / `expires-at` constraint, not a general
   scheduler. This plan delivers the independent *record* and *continue*
   actions first.
5. **No waiting turns.** The design must not require a provider turn to stay
   alive merely to wait for an external time or condition.

## Stage 1 — Deviation report: invariant, recoverable deviation, warning

**Prerequisite:** run-ownership-lock is merged. It provides the run ownership
lock and `sparring run-status --json` with separate `workflow` and `runner`
objects.

**Outcome:** For any recorded run, the engine can explain *why it cannot
simply continue*, in one machine-readable report. The report separates three
classes:

- **invariant**: a hard safety property is violated or unprovable. No action
  continues the run past it, and the report says what would have to be true.
  Examples:
  - the candidate content differs from its pin in a way no supported action
    explains;
  - the wrong branch, or a protected branch, is checked out;
  - ownership or the worktree record does not match;
  - the plan digest changed;
  - a sibling pin moved;
  - an accepted stage's history would change;
  - the gate instance does not match;
  - another runner holds the run.
- **recoverable deviation**: unexpected and explainable, and covered by a
  supported action. The report gives:
  - what changed (commits, paths, diff stat);
  - why it blocks continuation;
  - the risk;
  - the specific recovery action(s), each with a **confirmation token** bound
    to the exact facts it was computed from.
- **warning**: informational only; it never blocks continuation.

**Hard constraints**
- **Read-only.** The report writes nothing. While `runner.state` is `live` or
  `unknown`, it offers no mutating action.
- **Reuse the existing proofs.** The classifier uses `verify_candidate`,
  `repin_after_authorized_advance`, the plan-digest checks and the ownership
  checks. It never adds a looser parallel version of any of them. Anything it
  cannot prove recoverable is classed as invariant.
- **Tokens.** A token binds at least: run key, stage id, gate instance, pinned
  candidate identity, current HEAD and content digest, action kind and
  parameters. The executing action recomputes the token and compares it. Any
  difference refuses with "the repository changed since you reviewed this".
  Reuse the `plan_start` confirm-token approach where it fits.
- **Actions map to commands.** Each offered action names an existing engine
  command or one of this plan's commands. Existing recovery commands are
  classified, not reimplemented: `reset-stage`, `reopen-stage`, fresh
  sessions, and `rescope-run` when it exists on `main`.
- **Codes.** Reason codes are stable and come with human sentences, documented
  in `docs/reference.md`.

**Interface:** `run-status --json` gains a `deviations` list. Each entry has
`class`, `code`, `summary`, `detail`, `evidence` and
`actions: [{kind, command, token, effect}]`. The default output is human
readable.

**Boundaries / non-goals:** no new mutating operation. `resume-plan`'s
refusals keep their behavior; they may add a pointer to `run-status`.

**Acceptance evidence**
- Fixture tests for each class:
  - The branch advanced beneath an unchanged attempt → recoverable. It offers
    `--accept-advanced-head` with a token.
  - An attempt file was edited while paused → recoverable as a candidate
    amendment, which Stage 4 executes and which is "unavailable" until then.
  - An advance touching attempt paths → recoverable only as an amendment.
  - Rewritten history, a moved sibling pin, a protected branch or an edited
    plan stage section → invariant, with no action.
  - A live runner → no mutating actions.
- A token is refused after any repository change made between the report and
  its use.
- `.sparring` and `git status` are byte-identical after the report.

## Stage 2 — Record human-check outcomes without continuing the run

**Outcome:** A person can record one or several human-check answers, plus
notes, into a paused run without starting any agent. **Continuing** is a
separate action. `resume-plan --evidence` keeps working as "record, then
continue".

**Hard constraints**
- **Record-only command.** A new command, named by the stage agent (for
  example `sparring record-evidence`):
  - writes the answers into the current stage's `notes.md`
    `## Human evidence`, in the engine's existing shape, so `gate_answers.py`
    and the sparrer prompt read them unchanged;
  - sets `evidence_pending`;
  - exits without launching anything.

  A later plain `resume-plan` continues reviewer-first through the existing
  `_evidence_awaiting_review` path.
- **Gate instance.** Answers name their gate **instance** and check ids. An
  answer to any instance other than the stage's current open gate is refused.
  Deferred and plan-declared gates keep `--deferred-result`.
- **Idempotent and atomic, under the run ownership lock.**
  - Each submission carries an id, supplied by the caller or derived from its
    content. Repeating it records nothing new and succeeds.
  - A different submission for the same instance is appended. Nothing is
    overwritten, and history is kept.
  - An interrupted write leaves either the whole entry or none of it.
  - The command is refused while another process holds the run.
- **Drift: recorded, never verified.**
  - Recording is allowed when the working tree no longer matches the pin.
  - The entry records the pinned candidate identity it was given against, and
    its status is *recorded, pending review*.
  - `run-status` reports it as unverified until a reviewer turn on a candidate
    that verifies has consumed it.
  - Starting the reviewer still requires the candidate to verify (an
    unchanged rule).
  - Recording never moves, re-pins or validates the candidate.
- **Audit record.**
  - Each submission appends one entry to the run's append-only audit log:
    operation kind `evidence_recorded`, submission id, gate instance, check
    outcomes, pinned candidate, time and actor (`person`).
  - The log is registered in `artifact_ownership.py` with the engine as its
    sole writer.
  - The record format is shared with the post-completion check-amendment
    plan:
    - If that format exists on `main`, adopt it.
    - Otherwise, define it as a small versioned envelope (schema version,
      sequence, operation kind, actor, time, subject, payload) that the other
      plan can adopt without code changes. Its operation kinds and validation
      stay that plan's own.
  - `notes.md` remains the canonical text agents read; the audit log is the
    audit and idempotency record.
- **Compatibility.** `resume-plan --evidence …` behaves exactly as before:
  record, then continue, under one lock hold. Its existing tests pass
  unchanged.

**Boundaries / non-goals:** no candidate changes or re-pinning (Stages 3–4).
No time windows. No change to how the reviewer judges evidence.

**Acceptance evidence**
- Record → fake providers show no provider started → `run-status` reports the
  evidence as recorded and pending review → `resume-plan` starts the reviewer
  first on the same pin.
- Repeating the identical submission is a no-op. A different second answer
  for the same instance is kept as history. A superseded instance is refused.
- Recording while another process holds the run is refused, and nothing is
  written.
- A kill injected at each write step leaves the notes and the audit log
  consistent, and a retry converges.
- Recording against a drifted candidate succeeds and is reported as
  unverified. The next `resume-plan` refuses to start the reviewer until the
  drift is reconciled, and the evidence stays pending throughout.
- Existing evidence tests pass unchanged (`tests/test_plan.py`,
  `tests/test_accept_advanced_head.py`).

## Stage 3 — Authorized prerequisite advance as a standalone action

**Outcome:** A paused stage's branch can advance beneath an unchanged attempt
(Case 3). The person then authorizes "accept these commits as a prerequisite"
from the deviation report (Stage 1). The engine re-pins without requiring
evidence and without launching anything. The person never finds or types a
SHA.

**Hard constraints**
- **The proof is unchanged.** It is exactly `repin_after_authorized_advance`:
  - the accepted head is the current HEAD on the run's branch;
  - the pinned head is a strict ancestor of it;
  - the sibling pins are unchanged;
  - no path in the range overlaps uncommitted attempt content;
  - untracked files are byte-identical;
  - content and index, with the range removed, reproduce the pinned digests.

  Nothing is relaxed.
- **Authorization.** It is Stage 1's token, executed only if the recomputed
  token matches, and only under the run ownership lock.
- **Allowed states.** The action is allowed while the stage waits on a
  recorded NEEDS_YOU. The answer may be recorded before or after (Stage 2).
  The gate instance is unchanged. Extending the action to other paused states
  is out of scope.
- **Audit.** Each advance:
  - appends an audit entry: operation kind `prerequisite_advance`, previous
    pin, new pin, commits, paths, token digest, actor and time;
  - writes the existing human-readable note to `notes.md`;
  - emits an activity event.
- **Compatibility.** `--accept-advanced-head <sha>` keeps its semantics and
  tests. It runs on the same path and appends the same audit entry.

**Boundaries / non-goals:** no changes to the attempt itself (Stage 4). No
rebase, reset or history rewrite. No re-pin without a token.

**Acceptance evidence**
- Every scenario in `tests/test_accept_advanced_head.py` behaves identically
  through the new action: refusals refuse, accepted cases re-pin identically.
- A replayed or stale token is refused. Nothing launches. The audit log holds
  the old and new pins.
- Recorded evidence → advance → `resume-plan`: the reviewer judges the new
  pin first.

## Stage 4 — Authorized evidence amendment of the candidate

**Outcome:** While a stage waits on a human, a person can approve a bounded
change to the candidate itself, for example measurements written into a
tracked document (Case 1). The engine previews it, records exactly the
approved change, re-pins, and requires the reviewer to judge the amended
candidate. No manual Git is needed.

**Hard constraints**
- **Path set.**
  - The person approves a **named path set**.
  - The preview, offered as an action in the Stage 1 report, lists the exact
    changed paths and diff (stat, plus full text for text files under a size
    cap) and binds them into the token.
  - Execution refuses if any path outside the set differs from the pin
    (tracked, staged or untracked), or if the approved paths' content differs
    from the preview.
  - Unrelated changes are refused, never silently left out.
- **Allowed files.** Only files tracked in the candidate repository. Never
  `.sparring/` artifacts, the input plan, plan-executable content (the
  plan-digest invariant) or another repository.
- **Commit rules.**
  - A committed pin gets **one** engine commit containing exactly the approved
    paths, on the run's expected branch. A trailer names the run, stage, gate
    instance and audit sequence.
  - Never amend, rebase or force-push.
  - A deliberately uncommitted worktree pin is re-pinned without a commit. The
    existing finalization comparison applies later.
- **Mandatory re-review.**
  - After an amendment the reviewer **must** review the amended candidate
    before any acceptance: `next_turn` becomes the reviewer, and
    `evidence_pending` is set.
  - Evidence recorded earlier stays pending review.
  - Nothing accepted carries over from the previous pin.
- **Guards.**
  - Branch, protected-branch, ownership, token and run-lock checks apply
    exactly as for agent turns.
  - Push follows the run's existing push policy. Never push implicitly.
- **Audit.** An entry of operation kind `evidence_amendment` records the
  previous pin, the new pin and commit, the approved paths, a diff digest, the
  gate instance and the actor. The `notes.md` note says that a person amended
  the candidate and lists the paths.

**Boundaries / non-goals:** not a way to change plan scope, accepted stages or
other repositories. Unavailable while any other process holds the run, and for
an accepted stage.

**Acceptance evidence**
- **Case 1 replay.**
  1. The stage is paused on NEEDS_YOU with a committed candidate.
  2. Evidence is recorded (pending).
  3. One approved file is edited.
  4. `run-status` offers an amendment for exactly that path.
  5. Executing it makes one commit containing exactly that path.
  6. `resume-plan` starts the reviewer on the new pin.
- **Refusals:**
  - an extra changed file;
  - an approved file changed after the preview;
  - the plan file;
  - `.sparring/`;
  - the wrong branch;
  - a live runner;
  - an accepted stage;
  - a stale token.
- The uncommitted-candidate variant re-pins without a commit.
- Fake providers prove there is no path from an amendment to acceptance
  without a reviewer turn on the new pin.

## Stage 5 — Adversarial integration, documentation, prompts and skills

**Outcome:** The recovery operations are proven together under concurrency
and failure. They are documented as one model, and agents are told not to
wait inside a turn.

**Scope**
- **End-to-end tests** with fake providers:
  - Case 1: record → amend → continue → READY.
  - Case 2: record now → no agent → continue later → reviewer first.
  - Case 3: a prerequisite lands while paused → authorize the advance →
    continue.
  - Each case interleaved with a second `resume-plan`, a recovery command,
    and SIGKILL at every write step. The run lock and atomic writes must leave
    the state consistent and auditable.
- **Docs.**
  - `docs/gates.md`, `docs/plans.md`, `docs/reference.md`: one section, *When
    a paused run cannot simply continue*, covering the three classes, the
    report, tokens, and each action with its proof.
  - `docs/design.md`: the invariant/deviation/warning contract.
- **No waiting turns.**
  - Stage-agent and reviewer prompts state that work which cannot start until
    an external time or condition must not be waited for inside a turn (no
    sleep or poll loops).
  - The stage agent reports the wait and stops. The reviewer expresses it as
    a NEEDS_YOU `EXTERNAL_CONDITION` gate, not as a SEND_BACK that releases
    the work immediately.
  - The person records the condition with Stage 2 and continues when it holds.
- **Skills** (`sparring-plan`, `sparring-run`), only where behavior changed:
  - A scheduling or permission decision is an external-condition gate, not a
    Pass that releases immediate work.
  - Evidence that will be committed into the repository is named in the stage,
    so an amendment is expected.
  - The run skill uses `run-status` and the recovery actions instead of Git
    plumbing.

**Acceptance evidence:** the full suite passes. Docs examples match real
`run-status` output from a fixture. Prompt tests assert the no-waiting
instruction. `git diff --check` is clean.

## Deferred (separate follow-up)

- **Execution window**: a minimal `not-before` / `expires-at` constraint on
  continuing a run. The engine refuses to start a turn outside it and pauses
  at the next turn boundary after it expires. It cannot police commands
  inside a running turn. Production safety also belongs in project tooling:
  for example, the sporely-landing measurement script's own window guard. Not
  a general scheduler, and no automatic start.
- **Changing plan scope or contracts mid-run**: changed executable content
  needs a newly reviewed plan or run identity. Scope moves are repository-
  slices `rescope-run`. This plan only lists them as actions once they exist.
