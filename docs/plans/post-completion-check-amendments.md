# Post-completion amendment of deferred human checks (engine)

Goal: a person can correct the recorded answer to a deferred/plan-declared
human check of a **complete** plan run — fix a stale note, re-affirm a pass
with corrected evidence, or record that a check is consciously `deferred` to
follow-up work — through one narrow, audited, append-only command, without
reopening stages, rerunning agents or touching any candidate.

Today the only way to answer a check is `resume-plan --deferred-result`, which
refuses complete runs (`plan.py` `_run_to_resume`: "already complete; nothing to
resume"), and `reopen-stage` only works on a run stopped on a failing gate.

Primary repository: `agent-sparring`. Stages 1-3 are engine work. Stage 4 is
the consumer contract for `agent-sparring-vscode`, whose UI work gets its own
plan in that repository. Proposed work, not approval to launch a run.

## Shared rules (every stage reads this section first)

- **Narrow scope.** This amends the *answer* to a check that is in a complete
  run's `deferred_human_checks` ledger. It is not a run-history editor.
- **Explicit human intent only.** Only a person's own `sparring amend-check`
  invocation writes an amendment. The engine never calls it; no stage, sparring
  or review prompt mentions it; no auto-invocable skill runs it; nothing is
  inferred from notes, activity or prose. `artifact_ownership.py` registers the
  amendment log as engine-owned, `provider_writable=False`.
- **Append-only.** The run state file `.sparring/plans/<run>.json`, every stage
  directory (`state.json`, `sparring.md`, `notes.md`, handoffs, sessions),
  manifests, candidate pins, branches, worktrees, intake ledgers and git
  history are never written by this feature. The original `CheckResult` stays
  verbatim in the run state. The only writes are: one appended line in the
  amendment log, and one observational `plan.check_amended` activity event.
- **Active runs unchanged.** `CheckOutcome` (pass/fail/blocked),
  `DeferredAnswer.parse`, `CheckResult.from_dict`,
  `DeferredObligation.status/resolved/unanswered/failed`,
  `PlanRunState.unresolved_deferred`, `resume-plan --deferred-result`,
  `reopen-stage`, promotion and intake carry-forward
  (`plan_obligations.carry/sync/owed/origin_of`) keep their exact behavior.
  Amendments are refused on any run whose status is not `complete`.
- **Refuse, don't guess.** Every refusal is a `PlanError` with a human sentence
  and leaves everything in place (exit non-zero, nothing written).
- **Activity is telemetry.** The amendment log is the authority; the activity
  event mirrors it and is never read back.
- **Tests** use the existing isolated git fixtures (`tests/conftest.py`) and
  existing complete-run fixtures/helpers from
  `tests/test_deferred_human_verification.py`; no real provider, never the
  developer's real `.sparring` state. Each stage runs focused tests, then
  `.venv/bin/python -m pytest -q` before READY.

### Decisions

1. **Storage: a sidecar log, not the run state.** Path
   `.sparring/plans/amendments/<run-key>.jsonl` (subdirectory, so the
   `plans/*.json` run discovery glob in `find_runs` and the VS Code watcher never
   mistake it for a run). Reasons: the run state is "rewritten on every
   position/status change" and `PlanRunState.to_dict` drops unknown keys, so an
   older engine re-saving it would silently lose amendments; an older engine
   reading `"deferred"` in `results` would fail `CheckOutcome.from_str` and make
   the run unreadable; and a byte-identical run state is the simplest proof that
   history was not mutated. Managed `finish-run` already archives every ignored
   file under the project directory, so the log travels with the run.
2. **Record schema v1**, one JSON object per line:
   `{"v":1, "seq":n, "at":"<UTC ISO-8601>", "run":"<run key>",
   "instance_id":"<gate instance>", "check_id":"<id>", "stage_id":"<raising
   stage>", "outcome":"pass|fail|blocked|deferred", "note":"<non-empty>",
   "previous":{"outcome":..., "note":..., "source":"original|amendment",
   "seq":<n or null>}, "run_state_sha256":"<digest of the run state file read>",
   "actor":"person"}`. `seq` is 1..n contiguous per log. Unknown `v` or fields
   refuse.
3. **Effective resolution.** For each (gate instance, check id): the latest
   amendment by `seq` if any, else the original ledger `CheckResult`. History
   = original + every amendment in order. A reader validates the whole log
   (contiguous `seq`, matching `run`, instance/check present in the run's
   ledger, each `previous` equal to the effective value before it); any
   invalid record makes the log unreadable and every reader refuses loudly
   rather than skipping it. A final line without a terminating newline is a
   torn write that was never acknowledged: readers ignore it, and `amend-check`
   refuses to append after it until a person removes it.
4. **`deferred` is a terminal, human, amendment-only outcome**, in a new
   `AmendmentOutcome` enum (pass/fail/blocked/deferred) in the new module;
   `CheckOutcome` is not extended. Meaning: not a pass, not an implementation
   failure; the run stays `complete`; the check and its note stay visible as
   an outstanding follow-up obligation. It is distinct from `blocked`
   ("could not be performed, still owed by this run").
   It is **not** accepted by `resume-plan --deferred-result`: at an active
   checkpoint it would let a run complete over a verification nobody made,
   contradicting "Deferring is not waiving" in `docs/plans.md`. Changing that
   is a separate policy decision.
5. **`carried` stays the automatic intake-slice mechanism**, not an outcome.
   Carrying is the engine moving an *unanswered* obligation to the plan ledger
   for a later slice to claim; it is a scheduling fact, not a person's answer,
   and the last slice has nothing to carry into. `deferred` is a person's
   terminal answer that keeps the obligation with the run that owns it.
   Instance ids in `carried_deferred` are refused by `amend-check` (they are
   owed by the intake ledger, not this run). Amendments never write the intake
   ledger.
6. **Replay = idempotent by effect.** If the requested (outcome, note) equals
   the current effective (outcome, note) exactly, nothing is written, no event
   is emitted, and the command exits 0 saying so. A crash-retry or UI double
   submit is therefore safe and history contains only real changes. Changing
   only the note (pass -> pass, corrected evidence) is a real change and is
   recorded. Optional `--expect-current OUTCOME` refuses when the effective
   outcome differs (stale-surface guard; the VS Code client must always pass
   it).
7. **All four outcomes may be amended to; run status never changes.** An
   effective `fail`/`blocked` on a complete run is reported prominently and
   makes managed `finish-run` ineligible; `deferred` does not.
8. **Concurrency.** Read-validate-append runs under an exclusive `fcntl.flock`
   on `.sparring/plans/amendments/.<run-key>.jsonl.lock`, reusing the
   `plan_obligations._locked` pattern (factor it into a small shared helper if
   reused verbatim). The run state is re-read under the lock and the run must
   still be `complete`. One `os.write` of the full line with `O_APPEND`, then
   `fsync`, before success is reported.

### Non-goals

- Editing stage verdicts, acceptance, candidate SHAs, sibling pins, notes,
  handoffs, manifests, plan digests, run status or position.
- Amending immediate `NEEDS_YOU` gate answers (`gate_answers.py`, stage
  `notes.md`), adding checks, withdrawing obligations, or amending an open run.
- Amending runs whose worktree was removed by `finish-run` (archived state).
- Reopening, rerunning or creating follow-up work automatically from
  `deferred`; it only records the decision.
- Syncing amendments into intake obligation ledgers or approvals.
- Allowing `deferred` in active-run answering.

## Stage 1 — Amendment model, deferred outcome and effective resolution

Read "Shared rules" and "Decisions" of
`docs/plans/post-completion-check-amendments.md` first.

**Scope.** New module `src/agent_sparring/check_amendments.py` (pure model and
log I/O, no CLI):

- `AmendmentOutcome` enum; `CheckAmendment` frozen dataclass with strict
  `to_dict`/`from_dict` per schema v1.
- `amendment_log_path(sparring_dir, run_key)`; `load_amendments(path, state)`
  validating the whole log per Decision 3 (missing file = no amendments).
- `EffectiveCheck` (instance id, check id, stage id, effective outcome and
  note, `original: CheckResult | None`, `amendments: tuple`) and
  `effective_checks(state, amendments)` over every obligation in
  `state.deferred_human_checks`, in ledger order.
- Register the log in `artifact_ownership.py` (owner engine,
  `provider_writable=False`, lifetime "append-only; one line per amend-check").

**Files.** `check_amendments.py` (new), `artifact_ownership.py`,
`tests/test_check_amendments.py` (new), `tests/test_artifact_ownership.py`.

**Tests.** No log -> effective equals originals for pass/fail/blocked fixtures
written by the current engine; latest amendment wins; several sequential
amendments keep full ordered history; `deferred` round-trips; strict refusals
(unknown `v`, unknown field, `seq` gap, wrong `run`, unknown instance/check,
`previous` mismatch, empty note); torn final line ignored;
`CheckOutcome`/`DeferredAnswer.parse` still refuse `deferred`; an old run
state without a log loads and saves byte-identically.

**Acceptance.** Model and reader exist with no caller changing behavior;
existing suites pass unchanged.

**Out of scope.** CLI, writes, activity, consumers.

## Stage 2 — The amend-check command

Read "Shared rules" and "Decisions" of
`docs/plans/post-completion-check-amendments.md` first.

**Scope.** `sparring amend-check --run-key KEY --check REF --result
pass|fail|blocked|deferred (--note TEXT | --note-file PATH)
[--expect-current OUTCOME] [--repo-root R] [--json]`:

- Locate `.sparring/plans/<KEY>.json` directly by run key (no plan document or
  branch needed: nothing in git is touched); its `run` field must equal KEY.
- Resolve REF over **all** obligations of the run with the same bare/qualified
  union rules as `plan._locate_deferred_check` (reuse or factor it; an
  ambiguous bare id is refused listing the qualified refs).
- Refusals: unknown run; run not `complete` ("use resume-plan"); unknown or
  ambiguous check; instance in `carried_deferred`; empty note; unreadable or
  torn log; `--expect-current` mismatch.
- Under the lock (Decision 8) append the record, then emit `plan.check_amended`
  (summary `<instance>:<check> <old> -> <new>`) to the raising stage's
  activity log if that stage directory exists. Print old and new effective
  value and the log path. Replay per Decision 6.
- `docs/plans.md`: a section "Amending a check after the run completed" next to
  "Repairing a check that failed"; `docs/reference.md` CLI entry.

**Files.** `cli.py` (parser + `_cmd_amend_check`), `check_amendments.py`
(`append_amendment`), `plan.py` only if `_locate_deferred_check` is factored
out, shared lock helper if extracted from `plan_obligations.py`, docs above,
`tests/test_check_amendments.py`, `tests/test_cli.py`.

**Tests.** Note-only amendment on a complete run; pass -> deferred;
pass -> pass with corrected note; three sequential amendments, history and
latest effective; original `CheckResult` and the run-state file byte-identical
after amending; every stage directory byte-identical except the raising
stage's `activity.jsonl`, which gains exactly one `plan.check_amended` line;
`git rev-parse HEAD`, branch, `git status --porcelain` and worktree list
unchanged; exact replay writes nothing and emits nothing; `--expect-current`
mismatch refused; wrong run, wrong check, ambiguous bare ref, carried
instance, open/paused run refused with nothing written; two concurrent
`append_amendment` calls produce contiguous `seq`; `resume-plan
--deferred-result X=deferred` still refused; existing
`test_deferred_human_verification.py`, `test_deferred_across_slices.py` and
`test_recovery.py` pass unchanged.

**Acceptance.** All four outcomes amendable on complete runs only; all
refusals leave the filesystem unchanged.

**Out of scope.** Readers other than the command's own output; VS Code.

## Stage 3 — Engine readers of check outcomes

Read "Shared rules" and "Decisions" of
`docs/plans/post-completion-check-amendments.md` first.

**Scope.** Every engine reader of check outcomes, classified:

- Resolves amendments (changed here): new read-only `sparring run-checks
  --run-key KEY [--json]` listing every check of a run with effective outcome,
  note, original and amendment history (the contract Stage 4 consumes); the
  completion report printed by `resume-plan`/`run-plan` is untouched;
  `managed_finish.py` eligibility (line ~346 `unresolved_deferred`): on a
  complete run also evaluate effective checks — effective `fail`/`blocked`
  -> not eligible with a named reason, `deferred` -> eligible and listed.
- Explicitly unchanged, active-run only (amendments cannot exist while these
  run): `DeferredObligation.status/resolved/unanswered/failed`,
  `PlanRunState.unresolved_deferred/promoted_deferred`, `plan.py`
  answer application, `_stop_for_deferred`, manifest-gate minting/promotion,
  completion check and prompt `pending_deferred`; `cli.py` pause renderer
  (~1947); `recovery.py` reopen-stage; `plan_obligations` carry/sync/claim.
- Not a consumer: `gate_answers.py` (immediate NEEDS_YOU answers in
  `notes.md`), `usage.py` (cost accounting; new event ignored).

**Files.** `cli.py`, `check_amendments.py`, `managed_finish.py`,
`docs/reference.md`, `tests/test_check_amendments.py`,
`tests/test_managed_finish.py`.

**Tests.** `run-checks --json` shape for a run with and without amendments and
for an old fixture; finish eligibility with effective pass / deferred / fail;
carry-forward across slices unchanged (`test_deferred_across_slices.py`).

**Acceptance.** No engine path reports a superseded answer as effective for a
complete run; active-run behavior unchanged.

**Out of scope.** VS Code changes.

## Stage 4 — VS Code consumer contract and handoff

Read "Shared rules" and "Decisions" of
`docs/plans/post-completion-check-amendments.md` first.

**Scope.** Engine side only: shared JSON fixtures (run state + amendment log +
expected `run-checks --json`) under `tests/fixtures/` and a section in
`docs/design.md` stating the contract. Write
`../agent-sparring-vscode/docs/plans/check-amendments.md` (not implemented
here) listing its readers that must resolve amendments or be migrated:
`src/core/engineFormats.ts` (`DeferredCheckOutcome` ~112,
`obligationResolved` ~144, `obligationFailed` ~151,
`parseDeferredObligations`/`parseDeferredResults` ~278/307);
`src/core/overviewModel.ts` (~2108, ~2130, ~2285, ~2306, ~3424);
`src/core/humanChecks.ts` (`CheckOutcome`/`CHECK_OUTCOMES` ~394, ~427,
~1071-1092, progress ~1002/~1101); `src/core/reviewCopy.ts` ("Evidence recorded
so far" ~253, ~292); `src/vscode/controller.ts` watcher globs (~522, ~571 must
add `plans/amendments/*.jsonl`); `src/vscode/commands.ts` (~1625-1642,
`OUTCOME_WORDS`). Any UI submission passes free text as argv with no shell and
always sends `--expect-current`.

**Files.** `tests/fixtures/check_amendments/*`, `docs/design.md`,
`tests/test_check_amendments.py`, the extension plan file.

**Acceptance.** Fixtures validated by the engine reader; extension plan lists
every reader above.

**Out of scope.** Editing extension source.

## Human acceptance — repair sporely-py run 5fa58af8 (not a stage)

Run by the person, never by an agent, after Stages 1-3 are merged to `main`
and this checkout is on `main` (sporely-py's `.venv` has an editable install of
this repository). Run `run-checks` first and confirm the instance ids below
still match; if they do not, stop. Do **not** touch run
`2026-10-07-cloud-sync-extraction-d94afbbe-c71f5281`, and do not amend the
`G1` check (instance `c0108a47…`) that also appears in 5fa58af8's ledger.

```sh
S=/Users/sigmundas/Documents/Code/sporely/sporely-py/.venv/bin/sparring
R=/Users/sigmundas/Documents/Code/sporely/sporely-py
K=2026-10-07-cloud-sync-extraction-d94afbbe-5fa58af8
$S run-checks --repo-root "$R" --run-key "$K"

$S amend-check --repo-root "$R" --run-key "$K" \
  --check 5149a6516a6d4f0bb978e4eaba199a87:final-extraction-canary \
  --result pass --expect-current pass \
  --note 'G2 final extraction canary: pass. Build d28580a; isolated profile s7canary-test; disposable test account; DB /Users/sigmundas/Library/Application Support/Sporely/profiles/s7canary-test/mushrooms.db. Before: 8 observations, 11 cloud/local image rows, A=11, C/D1/D2/E/F/G/H=0. After one new observation and exactly one Sync Now: 9 observations, 12 rows, A=12, C/D1/D2/E/F/G/H=0. No cleanup/GC. Replaces a stale G1 identity/anchor canary note.'

$S amend-check --repo-root "$R" --run-key "$K" \
  --check 88434801a670402bb39675bb61f1c017:G2 \
  --result pass --expect-current pass \
  --note 'G2 pass: evidence is the corrected final-extraction-canary record (gate instance 5149a6516a6d4f0bb978e4eaba199a87) of this run.'

$S amend-check --repo-root "$R" --run-key "$K" \
  --check 4c1ed91316714e5381d645e63cfc95db:orchestration-follow-up \
  --result deferred --expect-current pass \
  --note 'prepare a separate orchestration follow-up plan using the accepted S1 design and this run'"'"'s human decisions'

$S run-checks --repo-root "$R" --run-key "$K"
```

Accepted when: `run-checks` shows the three new effective values with the
originals in history; G1 unchanged; the run is still `complete`; the run state
file, stage directories (other than one `plan.check_amended` activity line per
amended stage), sporely-py HEAD, branch and worktrees are unchanged; repeating
any command reports "already effective" and writes nothing.
