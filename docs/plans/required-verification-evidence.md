# Enforce required verification evidence before acceptance

Prevents a repeat of the Sporely Stage 8 failure. A reviewer returned READY
while mandatory real-stack verification had never run. The engine then
accepted the candidate, and nothing in the run state recorded the gap.
Background: `docs/plans/ready-with-missing-evidence.md`.

Run only after `docs/plans/secure-agent-git-writes.md` is fully accepted
and its agent boundary is active. This plan's evidence and human-resolution
records rely on agents being unable to write engine state.

Audit baseline: 2026-10-17, local `main` at `ea8e70d`. None of this exists on
any branch. Two unmerged branches touch the same area:

- `sparring/repository-slices-…` puts the obligation ledger behind a
  `PlanSliceContext` protocol and adds parsing of declarations inside stage
  sections (`_stage_declarations`). Plan 2 extends both if that branch lands
  first. It does not add a parallel ledger or declaration parser. If the
  branch has not landed, Plan 2 keeps to shapes that branch can absorb.
- `sparring/ui-screenshots-…` adds `visual_evidence.py`, which binds
  screenshot evidence to a candidate SHA and sibling pins.

Base each run on the current target branch at launch.

Three stages:

1. Mechanically enforce required checks at stage acceptance. This includes
   the Stage 8 regression.
2. Extend the same checks to later declared boundaries, using the existing
   obligation ledger.
3. Add the human resolutions: risk acceptance and documented amendment. Each
   is distinct from PASS.

Stage 1 alone is strictly safer than today: the only way past a blocked check
is real evidence, or a new run with an amended plan.

Decision settled by this plan: a PASS reported by the stage agent satisfies a
required check only if the independent reviewer affirmed it. The affirmation
must be per check, in structured form, in the review of that same candidate
identity. Engine-executed check results satisfy a check directly. Results of
human-executed checks are recorded by the person through the engine.

## Stage 1 — Required checks block stage acceptance until current evidence exists

**Context:** This stage fixes the Sporely Stage 8 failure. The run was
`2026-10-14-sync-key-identity-ff39f23d-03563a36`, candidate `46c9297`. That
stage's brief required three pieces of disposable integration verification:

- a browser-harness matrix row;
- a web/desktop coexistence case;
- real PostgREST behavior of a conditional update.

None of them ran, because a local service was down. Only unit tests ran.
The reviewer wrote that "Required disposable integration verification
remains outstanding" and that this "does not establish … acceptance
eligibility". It still returned READY, filing the checks under `deferred`.
The engine froze, pushed and accepted the candidate.

The engine at audit:

- **Routing.** `routing.RoutingResult` holds `action`
  (`READY|NEEDS_YOU|SEND_BACK|ESCALATE`), `human_gate`, `deferred_human_gate`
  (READY only), `promote_deferred` and prose in `deferred`.
- **Review prompt.** `review_prompt.py` (around line 183) tells the reviewer
  to put checks it cannot run into `deferred` and then choose the action the
  review warrants.
- **Required checks.** Agent-required checks exist only as prose in the
  brief.
- **Acceptance.** `acceptance.py` checks branch, clean tree, sibling pins
  and reachability, but no evidence.
- **Candidate identity.** A reviewed candidate is often an uncommitted
  worktree state: `next_turn.TurnCandidate` with `kind="worktree"`, plus
  `head_sha`, `content_digest` and sibling pins. The finalization turn
  commits it after READY, and `finalization.py` proves that the committed
  content equals the reviewed content.

The accepted Git-write plan already guarantees three things. Agents run
inside an engine-applied boundary. They cannot write engine state. Their
network is default-deny, with a per-project allowlist that can include local
disposable service ports.

**Outcome:** A plan stage can declare required checks. The engine records
evidence for each one against the exact candidate identity. Stage acceptance
is refused while any required check lacks current, satisfying evidence. In
that case a READY verdict becomes a typed pause, not acceptance. A reviewer's
READY never satisfies a required check by itself. Neither does prose, a
screenshot or an agent's narrative.

**Required invariants:**
- **Declaration.**
  - Required checks are declared inside the stage section, so the
    direct-Markdown execution digest binds them. Manifest and intake routes
    must carry them equally.
  - Each declaration gives a stable ID, what the check establishes, and its
    executor: stage agent, engine command, or human.
  - In this stage the only supported boundary is this stage's acceptance.
    `check-plan` refuses a declaration that names any other boundary, with a
    message saying it is not yet supported.
  - Plans without declarations behave exactly as today.
- **Structured agent evidence.** The stage agent reports each check in a
  structured result that the engine parses. Prose does not count. Each entry
  gives the check ID, a result (`PASS`, `FAIL`, `NOT_TESTED` or `BLOCKED`
  with a reason), the command and exit status, and a log path.
- **Engine record.** The engine appends one evidence record per attempt and
  never overwrites earlier attempts, so a FAIL stays visible. Each record
  holds the candidate identity (`head_sha`, `content_digest`, sibling pins),
  the executor and mechanism (agent-reported, engine-executed or
  human-recorded), the result, the evidence references and the time. An
  engine-executed command runs inside the agent boundary, never with the
  engine's own credentials.
- **What satisfies a check.** A check is satisfied by a `PASS` for the
  current candidate identity that is either:
  - engine-executed;
  - human-recorded, for a check whose declared executor is human;
  - agent-reported and affirmed by the independent reviewer in its
    structured per-check field, in the review of that same identity.
- **Staleness.**
  - Evidence applies only to the identity it names. Any change to the
    content digest or sibling pins makes it stale.
  - When finalization proves the committed content equals the reviewed
    content, the evidence carries forward and is also bound to the frozen
    commit SHA. If finalization drifts, it does not carry forward.
  - Older evidence remains as history and never satisfies a newer identity.
- **Reviewer contract.**
  - The reviewer result gains a structured per-check section. For each
    declared check it says either affirmed (with what was inspected) or not
    satisfied (with a reason). It also lists any required verification the
    brief demands that is missing, even if undeclared.
  - The review prompt separates two cases. Checks the *reviewer* cannot
    reproduce stay in `deferred`. Checks the *stage* was required to run and
    did not run belong in the new section.
  - A reviewer cannot move a declared check into `deferred`.
- **Enforcement.**
  - READY with any declared check unsatisfied, or with a non-empty
    missing-verification list, is recorded as the review outcome. It routes
    to a typed `required_verification_missing` pause, a name deliberately
    distinct from the existing `deferred_verification_required`.
  - Freeze and accept are refused. The candidate is neither frozen nor
    pushed. The pause lists each missing, failed, blocked or stale check
    with its reason.
  - Environment or tooling blockers, such as "service down" or "not
    permitted", are presented as a setup action for the person.
- **Resuming after the pause.**
  - The run can resume with an evidence-only stage turn. That turn runs the
    checks and must not change the candidate content. Then the reviewer
    affirms the new evidence for the same identity.
  - If the content changed, that is a new candidate and goes through normal
    review.
  - Because the content digest includes untracked files, evidence logs and
    test artifacts are written outside the worktree or to ignored paths. A
    log the engine records must not change the candidate identity.
- **Separate facts.** Review approval and execution evidence are kept as
  separate facts in the run state.
- **Compatibility.** Existing runs and stage history load. Send-back,
  fresh-session, resume, `human_gate` and `deferred_human_gate` behave as
  before. Legacy READY results are never relabeled as verified.
- **Visibility.** `sparring runs --json`, the plan-run state and the CLI
  stage output expose each check's ID, latest result, provenance, identity
  and satisfaction.

**Starting points:** `routing.py`, `review_prompt.py`, `sparring_prompt.py`,
`sparring_exchange.py` (`read_recorded_outcome`), `stage_prompt.py`,
`stage_agent.py`, `next_turn.py` (`TurnCandidate`), `finalization.py`,
`acceptance.py`, `plan.py` (`PlanRunState`, typed pauses), `plan_model.py`,
`manifest.py`, `intake.py`, `human_gate.py`, `artifact_ownership.py`,
`skills/sparring-plan/SKILL.md`, `docs/gates.md`. Tests:
`tests/test_acceptance.py`, `tests/test_routing.py`,
`tests/test_sparring_exchange.py`, `tests/test_human_gate.py`,
`tests/test_independent_review.py`. If `visual_evidence.py` has been merged
by launch time, reuse its candidate binding rather than adding a second
schema. Otherwise keep the record shape compatible with it.

**Implementation freedom:**
- The declaration syntax.
- Record and section field names.
- Whether engine-executed checks are supported in this stage at all.
  Agent-reported and human-recorded checks are required.

**Boundaries / non-goals:**
- No later-boundary declarations (next stage).
- No waivers or amendments.
- No Sporely tests or services.
- No visual-evidence framework, plan-compiler rewrite, VS Code changes or
  post-acceptance correction workflow (issue #4).
- No hand edits of `.sparring` state.

**Acceptance evidence:**
- **Stage 8 regression.** A stage declares a required real-stack check. A
  fixture reviewer returns READY with that check under `deferred` and listed
  as not run, as in Stage 8. The run pauses with
  `required_verification_missing` naming the check. Freeze and accept are
  refused, and the next stage does not start.
- **Reviewer silence.** The same setup with READY, nothing listed and no
  evidence is still refused.
- **Satisfying evidence.** An agent PASS for the current identity that the
  reviewer affirmed lets acceptance proceed. It carries through a
  content-identical finalization. These do not satisfy the check:
  - an un-affirmed agent PASS;
  - `FAIL`, `BLOCKED` or `NOT_TESTED`;
  - a prose-only claim;
  - a PASS for an older identity;
  - a PASS that does not carry through finalization drift.
- **Evidence-only resume.** It works when the content is unchanged. It falls
  back to normal review when the content changed.
- **Legacy.** Legacy fixtures load and behave as before. The existing
  send-back, fresh-session and resume tests pass.
- **Test runs.** Focused tests pass, and so does `.venv/bin/python -m pytest
  -q`. The report gives the commands, counts and the candidate SHA.

## Stage 2 — Required checks at later declared boundaries

**Context:** The previous accepted stage of this plan lets a plan stage
declare required checks with stable IDs. It records engine-owned, append-only
evidence bound to the candidate identity (`head_sha`, `content_digest`,
sibling pins, carried to the frozen SHA through content-identical
finalization). It refuses stage acceptance with a typed
`required_verification_missing` pause while a check is unsatisfied.
Declarations that name any boundary other than stage acceptance are refused
for now.

Legitimate integration checkpoints often come later. An example is a
real-stack check after several stages are combined. The engine already has a
deferred-verification ledger:

- `deferred_gate.py`: `CheckOutcome` `pass`/`fail`/`blocked`, `CheckResult`
  (no SHA), `ObligationStatus`, the `deferred_verification_required` pause;
- `plan_obligations.py`: `CarriedObligation`, carry/sync across slices.

The 2026-10-14 Stage 8 reviewer invented a deferral for a check that the
brief required immediately.

**Outcome:** A required check can be declared for a named later boundary: a
later stage's acceptance, a declared gate, or plan completion. The earlier
stage is accepted normally and stays accepted. The check becomes an
obligation in the existing ledger. It blocks only its declared boundary
until evidence for the candidate identity that reaches that boundary
satisfies it.

**Required invariants:**
- **Use the existing ledger.** Later-boundary checks are carried as
  obligations in the existing deferred/obligation ledger and use its pause
  and promotion mechanics. There is no parallel mechanism.
- **Evidence target.**
  - Evidence must name the candidate identity in force at the boundary,
    such as the later stage's frozen SHA or the plan's final SHA.
  - The same satisfaction rules as stage-boundary checks apply.
  - Evidence for an earlier identity is history and does not satisfy the
    check.
- **No retroactive change.** An earlier stage's acceptance is never revoked
  or rewritten because a later obligation is still open.
- **Who sets the boundary.** Only the plan declaration fixes a check's
  boundary. A reviewer may promote an obligation to block sooner, using the
  existing `promote_deferred`. It cannot postpone one.
- **Legacy compatibility.** Legacy deferred `CheckResult`s without an
  identity remain valid for the legacy obligations they belong to. They can
  never satisfy a declared required check.
- **Run listing.** `sparring runs --json` shows open required obligations
  with their boundary.

**Starting points:** `deferred_gate.py`, `plan_obligations.py`, `plan.py`,
`manifest.py` (`gates_before` / `completion_gates`), `intake.py`, the
previous stage's check-record code. Tests:
`tests/test_deferred_human_verification.py`,
`tests/test_deferred_across_slices.py`, `tests/test_manifest_gates.py`.

**Boundaries / non-goals:** No waivers or amendments (next stage). No
cross-slice redesign. No VS Code, Sporely or post-acceptance correction work.

**Acceptance evidence:**
- **Later check accepted correctly.** A stage-2 check declared for plan
  completion:
  - stage 2 is accepted;
  - stage 3 runs;
  - plan completion is refused until PASS evidence for the final identity
    exists.
  - Stage 2's acceptance record is unchanged throughout.
- **Stale or failed evidence blocks.** Evidence for an earlier identity does
  not satisfy the check. `FAIL` and `BLOCKED` keep it blocked.
- **Promotion only.** A reviewer can promote the check to block sooner but
  cannot postpone a declared stage-acceptance check.
- **Legacy and test runs.** Legacy ledger fixtures load and behave as
  before. Focused tests pass, and so does `.venv/bin/python -m pytest -q`.
  The report gives the commands, counts and candidate SHA.

## Stage 3 — Human risk acceptance and gate amendment, never shown as PASS

**Context:** The earlier accepted stages of this plan put each required
check into one of these states: `PASS`, `FAIL`, `NOT_TESTED` or `BLOCKED`,
satisfied or not, and stale or current. Each check carries identity-bound,
append-only evidence. It blocks its declared boundary, either the stage's
own acceptance through the `required_verification_missing` pause, or a later
boundary through the obligation ledger. Today the only way past a blocked
check is real evidence, or a new run with an amended plan.

The engine has no waiver, risk-acceptance or amendment record. Human
evidence (`resume-plan --evidence`, `plan.record_human_evidence`) is free
text in `notes.md`. The accepted Git-write plan's agent boundary means agents
cannot write engine state or invoke engine state changes.

**Outcome:** A person can resolve a blocked required check without making it
look passed. They either accept the risk for the current candidate, or
record a documented amendment of the gate. Both are explicit, attributed
human actions, and consumers see them as "not verified, accepted by a
person".

**Required invariants:**
- **Risk acceptance.**
  - A dedicated CLI action names the run, the check ID, the current
    candidate identity and a reason.
  - The engine records it as a distinct resolution (for example
    `RISK_ACCEPTED`) with the OS user, the Git identity and the time.
  - It unblocks only that check, at that boundary, for that identity. A new
    identity needs a new decision.
  - It is never stored or rendered as `PASS`. Plan summaries and completion
    reports list it as unverified.
- **Amendment.**
  - A documented amendment removes or changes a required check for the
    running instance as an explicit, recorded exception, with its reason
    and the person.
  - It does not edit the plan file or its digest, which is the existing
    rule that an executing plan's contracts are not rewritten.
  - The run's state and reports show the amended contract next to the
    original.
- **Who can resolve.** Only these human CLI actions can resolve a blocked
  check. These change nothing:
  - agent output;
  - reviewer verdicts;
  - `notes.md` or `resume-plan --evidence` text;
  - stage files.
- **One confirmation.** Each resolution is one human confirmation. It is not
  bundled with push, merge or deploy authorization, which stay separate
  engine actions.
- **Visibility.** `sparring runs --json` and the plan-run state expose the
  resolution kind, the person, the reason and the identity. `docs/gates.md`
  and the CLI help describe both actions.

**Starting points:** the earlier stages' check-record and pause code,
`deferred_gate.py`, `gate_answers.py`, `plan.py` (`resume-plan` options),
`cli.py`, `docs/gates.md`, `docs/plans.md`, `docs/reference.md`. Tests:
`tests/test_gate_answers.py`, `tests/test_blocked_gate_answers.py`.

**Boundaries / non-goals:** No VS Code changes. No retroactive changes to
accepted stages. No new review workflow and no post-acceptance correction
workflow.

**Acceptance evidence:**
- **Risk acceptance.** Risk-accepting a `FAIL` or `BLOCKED` check unblocks
  exactly that check, boundary and identity. Its record and the completion
  report show `RISK_ACCEPTED`, not `PASS`, with the person, reason and
  identity. A new identity blocks again.
- **Amendment.** An amendment is recorded, shown next to the original
  contract, and leaves the plan digest unchanged.
- **Non-human claims.** A stage or reviewer output claiming a waiver or PASS
  leaves the check blocked. So does `--evidence` text.
- **Confirmations.** Each resolution needs exactly one confirmation.
- **Test runs.** Focused tests pass, and so does `.venv/bin/python -m pytest
  -q`. The report gives the commands, counts and the candidate SHA.
