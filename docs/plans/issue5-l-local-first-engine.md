# Local-first engine (issue #5, Plan L)

**Decision (the user, 2026-10-22; scope approved 2026-10-23).** Agent Sparring
is local-first. Implementation, review, acceptance and `finish-run` work
entirely in the local repositories. Neither agents nor the engine contact
GitHub, GitLab or any other remote as part of normal work.
- Publishing a commit for external review is a separate, explicit,
  human-triggered action, **Push to review**. It is specified in Plan P
  (`docs/plans/issue5-p-push-to-review.md`), not here. It never implies
  acceptance.

**This plan replaces Plan R2.**
- R2 (`docs/plans/issue5-r2-complete-and-verify-engine-git-authority.md`, at
  `d658eef`, with its kit at `d7f672d`) is kept unchanged, for reference.
- The first draft of this plan is `bad8811`. This revision applies the
  independent design review of 2026-10-23, with the user's corrections.

**Inputs:**
- Plan R: `docs/plans/issue5-r-complete-engine-git-authority.md` (base
  `36e2cea`).
- The Plan R audit: `docs/audits/issue5-plan-r-audit-2026-10-22.md`
  (findings R-1 to R-15).
- The isolated test run of `36e2cea`: focused 381 tests, 1 failure; full 2071
  tests, 7 failures. The evidence is in
  `/Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-20261022T174121Z/`.

**Base.** `plan/issue5-local-first`: `36e2cea` plus planning documents and
earlier kits only.

**Separate from:**
- **Plan P** (Push to review, engine side). It goes after Plan L and gets its
  own security review.
- **The VS Code companion plan.** It lives in `agent-sparring-vscode`, and
  feature-detects the engine's `local_first` capability (see Stage 2).
- **Plan D.** This is the real boundary between agents and the user's
  credentials and trusted engine state. A `git push` deny rule in Claude
  settings is a workflow setting, not an OS boundary. Until Plan D, a
  same-user agent can still run `git push` with the user's credentials, or
  any `sparring` command.
- **Plan 2** (required verification evidence), which stays in the backlog.
  Until it exists, mandatory tests are enforced by this run's human gates.
- **Plans A to C.** No provider launch, sandbox or credential-guarantee work.

## Invariants (every later refactor must keep these)

- **I-L1. No remote contact.**
  - No engine command runs a Git command that contacts a remote. That
    covers every run, stage, review, acceptance, status, handoff,
    slice-branch and finish path.
  - The remote commands are `push`, `fetch`, `pull`, `ls-remote`,
    `remote update`, `remote show`, a `clone` or `submodule update` from a
    URL, and any command given a URL.
  - `engine_git.run` refuses every one of them, unconditionally, with
    `remote_operation_not_allowed`. There is no exception context in
    Plan L.
  - Plan R's isolated remote primitive (`engine_git.run_remote` and its
    form allowlist) stays in the code for Plan P, with no production caller.
    A test asserts that it has none.
- **I-L2. Acceptance is local.** Freeze and accept require:
  - the exact candidate commit at `HEAD` of the expected branch, in each
    participating repository;
  - a working tree clean apart from the engine's own artifacts;
  - for accept, the reviewer's verdict at that exact SHA.

  Remote state is never an input.
- **I-L2b. Accepted history only grows, per repository.** For the primary
  repository and for each sibling repository, separately:
  - freeze, accept and `enter_review` require that repository's stage base
    and every earlier accepted candidate of the run *in that repository* to
    be ancestors of (or equal to) that repository's current candidate;
  - `finish-run` requires, in every participating repository, every
    accepted candidate of the run to be an ancestor of the commit it
    integrates.

  A violation refuses with `run_history_rewritten`, naming the repository and
  the missing commit.
  - Before Plan L, a candidate had to be pushed with an ordinary push, and
    remote movement was checked. That is where append-only history came
    from. Local-first must keep it explicitly.
- **I-L3. Nothing grants acceptance by implication.**
  - Acknowledging a recorded incident pause clears only that pause. It never
    counts as review or acceptance.
  - A legacy push permission never becomes acceptance (L-D1).
- **I-L4. Local Git stays hardened.**
  - Every engine Git command, read-only ones included, keeps Plan R's local
    protections:
    - base overrides;
    - the include refusal;
    - the trusted-snapshot comparison in a guard;
    - the environment strip;
    - no hooks and no fsmonitor.
  - Stage 5 adds signing programs, submodule recursion and the
    configuration-listing failure.
  - A repository's own configuration never makes an engine Git command run a
    helper, hook, filter, signing tool or other program.
- **I-L5. Mandatory tests run at the reviewed SHA.** "Deferred" or "could not
  run" never satisfies a mandatory test.
- **I-L6. Explicit actions keep their meaning.**
  - A flag or command an older client sends either does what it always
    meant, or refuses with a typed deprecation code and guidance.
  - It is never silently reinterpreted.
  - JSON stays `schema_version` 1, with fields only added.

## What becomes of Plan R's changes

| Plan R change (at `36e2cea`) | Under Plan L |
|---|---|
| `engine_git` local hardening (overrides, include refusal, snapshot comparison, environment strip) | **Kept.** I-L4. |
| `engine_git.run_remote`, its isolated bare repository and form allowlist, `unpinned_local_remote` | **Kept, unused.** No production caller until Plan P. The tests keep it correct. |
| `remote_pin` snapshot | **Kept for Plan P.** In Plan L it is only read inertly from legacy runs. |
| Run-start pins, sibling pins, the `engine_says_pinned` flag, pin-missing refusals | **Removed from new runs.** Legacy records load inertly (Stage 4). |
| Per-turn remote movement checks, push receipts, the movement pauses | **Removed from normal runs (L-D2).** Recorded incident pauses stay answerable (Stage 4). |
| `acknowledge-remote-movement`, `authorize-pushed-candidate` | **Legacy only**, local, never acceptance (I-L3). |
| `push_gate.ensure_candidate_pushed`, `--allow-push-for-run`, the `push_authorization_required` pause | **Removed from runs.** The push primitive stays for Plan P. |
| `finish-run --push-target`, `[finish] delete_remote_branch` | **Refused** with a typed code (Stage 4). |
| Failed-turn checking (`checked_turn`) | **Kept for local checks.** Remote movement is dropped. |

**Audit findings under Plan L:**

| Finding | Disposition |
|---|---|
| R-1 live-configuration remote reads | **Removed with the reads** (Stages 2 and 3). |
| R-2 siblings | **Moot for remotes.** Siblings get I-L2b. |
| R-3 `base_sha` explains a candidate, R-13 pin digest | **Moot.** No remote authority remains. |
| R-4 ownership from `.sparring/` | **Stage 5** (local integrity). |
| R-5 legacy finish | **Stage 4.** |
| R-6 signing, R-7 submodules, R-8 listing failure | **Stage 5.** |
| R-9, R-10, R-11 | **Moot.** The code paths are removed or become legacy-only. |
| R-12 resume and push-then-rewind | **Moot for normal work.** It is a Plan D residual. |
| R-14 abbreviated SHA comparison | **Plan P.** |
| R-15 VS Code raw `git` | **Companion extension plan.** |

## The seven failing tests

None is deleted. Each stage's traceability table records what happened to
each one.

| Test (at `36e2cea`) | Status | Stage |
|---|---|---|
| `HardenedGitTests::test_remote_operations_never_read_the_repositorys_own_configuration` | **Applicable to the retained remote primitive (Plan P).** It is repaired so a planted `pushInsteadOf` is reached and never followed. It keeps exercising `run_remote` directly. | 1 |
| `UnguardedRemoteTests::test_push_forms_that_hide_the_destination_are_refused` | **Applicable to the retained primitive.** The `--repo` control is replaced (Git 2.53.0: `--repo X <refspec>` never contacts X; `--repo X --mirror` does). | 1 |
| `FinishWithoutThePinTests::test_the_state_file_deleted` | **Still applicable, as local integrity.** The exit code is fixed to `3` (Stage 1). In Stage 4 the reason becomes the run-state integrity refusal; nothing is merged, and no ref changes anywhere. | 1, 4 |
| `FinishWithoutThePinTests::test_the_state_file_unreadable` | Same as above. | 1, 4 |
| `FinishWithoutThePinTests::test_the_state_replaced_with_a_legacy_shaped_one` | Same as above. | 1, 4 |
| `FinishWithoutThePinTests::test_the_pin_malformed` | **The remote half is obsolete; a local property remains.** Exit code fixed in Stage 1. In Stage 4: a damaged legacy pin is inert. `--push-target` refuses with `remote_finish_removed`, and the remote's refs are unchanged. A local finish contacts no remote. The original property (no remote write without an intact pin) passes to Plan P. | 1, 4 |
| `FinishWithoutThePinTests::test_the_pin_removed_from_the_state` | Same as above. | 1, 4 |

**Obsolete for normal work, not deleted.** These are the tests whose subject
is push authorization during a run, and the movement and pin tests. Each one
is:
- **kept, for legacy runs** (Stage 4); or
- **converted** into an assertion of the new invariants (I-L1, I-L2,
  I-L2b); or
- **removed**, only when the traceability table names the test that now
  covers its property.

## Verification rules (every stage)

- **Tests run, at the exact candidate commit.** The stage agent runs:
  - its focused tests;
  - the eight Stage 1 baseline files plus `tests/test_no_live_remote_config.py`
    and `tests/test_fail_closed_pins.py`;
  - the full suite (`.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs`),
    sharded by file if one command would exceed the Bash timeout.

  For each command, the handoff records the full SHA, the command exactly as
  run, the exit code, the counts and every failing test name. A handoff
  without these goes back. "Deferred" or "could not run" is never `READY`.
- **A missing tool is `NEEDS_YOU`**, after `uv sync --frozen`.
- **No weakening.**
  - No skip, xfail or loosened assertion. A test is never deleted without a
    named replacement.
  - The handoff lists every converted or removed test, with the test that now
    covers its property.
  - The full-suite count may fall only by removals the handoff names.
- **Positive controls.** Every negative test shows that the same attack or
  rewrite succeeds without the protection: plain `git`, an already
  protected path, or a `mock.patch` that disables the protection in the test
  only.
- **Real repositories.**
  - Git tests use real disposable repositories, and bare remotes where a
    remote must be shown untouched.
  - I-L1 is proven by a `git` wrapper placed first on `PATH`, which logs
    every subcommand the engine runs. `engine_git` calls `git` through
    `PATH`. It is never proven by mocking `engine_git`.
- **Traceability.** The handoff has a table: acceptance item → test id →
  positive-control test id. The reviewer checks it statically.
- **Exact-SHA evidence.**
  - Before each later stage and before completion, the run stops at a human
    gate.
  - A person runs the credential-free isolated test of the accepted SHA. Its
    evidence records `git rev-parse HEAD`, an empty `git status --porcelain`
    and the junit test counts and ids.
  - Each must match the handoff's SHA and counts.
  - The gate, not the reviewer's `READY`, is the evidence.
- **Send-back cap.** `--max-send-back-cycles 3`. After that, a person reviews
  the design before any further round.

## Stage 1 — Repair verification

**Outcome:**
- **The `pushInsteadOf` regression.** It pins, plants
  `url.<evil>.pushInsteadOf` after pinning, and pushes through the pinned
  path of `run_remote`. The push lands on the pinned remote, and `<evil>`
  receives nothing.
  - Positive control: plain `git push` reaches `<evil>`.
  - `unpinned_local_remote` stays asserted separately.
- **The five finish tests** assert exit `3`, the documented code for "refused
  by its checks". Every later assertion then runs unchanged.
  - Positive control: the same run, with an intact pin and no planted
    `pushurl`, finishes, and `push.git` stays empty.
- **The `--repo` control** is `plain git push --repo <path> --mirror`. A test
  asserts the Git 2.53.0 semantics directly.
- **Positive controls** are added to every negative test in the two new test
  files, including the `sshCommand` and credential-helper checks.

**Boundaries:**
- Tests only. A production fix is allowed only for a defect a repaired test
  exposes, and it never relaxes a refusal.

**Acceptance evidence:**
- **The tests.** All seven pass, each with its control, and the full suite is
  green at the candidate.
- **Independent check.**
  - The suite is green at an exact SHA in the isolated test.
  - The seven ids are present and passing in its junit XML.

## Stage 2 — Local acceptance and history preservation

**Outcome:**
- **Freeze, accept and `enter_review` are local (I-L2).**
  - They require the candidate at `HEAD` of the expected branch, in each
    participating repository, and a clean tree. Accept also requires the
    verdict at the exact SHA.
  - They read no remote and no remote configuration.
  - The refusal "not available on the intended remote branch" is removed.
    Where the local rule fails, a typed `candidate_not_on_branch` refusal
    states the rule.
- **History is preserved per repository (I-L2b).**
  - The engine records each accepted candidate per repository in run state:
    the primary, and each sibling by its recorded name and path.
  - Freeze, accept and `enter_review` refuse with `run_history_rewritten`
    when that repository's stage base, or an earlier accepted candidate in
    it, is not an ancestor of its current candidate.
- **Runs never push.**
  - `start-plan`, `run-plan` and `resume-plan` no longer push, pin a remote,
    pause for push authorization, or check remote movement after a turn.
  - Failed-turn checking keeps its local checks.
- **Deprecated flags refuse, with guidance (I-L6).**
  - `--allow-push-for-run` refuses with `push_not_part_of_runs`. Nothing is
    created, and the message points to Plan P's Push to review.
  - `resume-plan --allow-push-candidate <sha>` refuses with
    `push_permission_deprecated`. Nothing changes.
    - The message says that runs are local-first.
    - It says a legacy push pause converts only on a plain `resume-plan`,
      under the L-D1 rules (Stage 4).
    - It names the command.
  - Neither flag is ever an alias for local progression.
- **A capability flag.** `runs --json`, the handoff JSON and `finish-run
  --json` gain `"capabilities": ["local_first"]` (an added field;
  `schema_version` stays 1). `sparring capabilities --json` prints the same
  list. The companion extension uses it to hide its push UI.

**Starting points:**
- `acceptance.py` (`_pushed`, `_sibling_pushed`, `_freeze_siblings`,
  `_verify_siblings`, `_require_explained`, `_run_remote`, the freeze,
  accept and review paths), `review.py` (`enter_review`).
- `plan.py` (start, resume and turn paths; `_pin_siblings`; the push and
  movement pauses; per-repository accepted records), `loop.py`, `cli.py`.

**Boundaries:**
- The unconditional `engine_git` refusal and offline status are Stage 3.
  Finish and legacy migration are Stage 4.
- Legacy run state still loads.

**Acceptance evidence (real repositories):**
- **End-to-end.** A managed run and a hand-driven stage each run to
  acceptance with fake providers:
  - in a repository with no remote at all;
  - with a sibling repository;
  - in a repository whose `origin` is a real bare remote, whose refs are
    unchanged byte for byte.
- **I-L2b, positive.** Legitimate advances are accepted:
  - a new commit on top of the accepted candidate;
  - several commits;
  - a merge commit that contains the accepted candidate;
  - a sibling that advances while the primary does not, and the other way
    round.
- **I-L2b, negative.** Each of the following refuses with
  `run_history_rewritten`, names the repository, and freezes and accepts
  nothing:
  - in the primary, `git reset --hard` to before the accepted candidate
    between stages;
  - in the primary, an interactive-equivalent rebase that rewrites the
    accepted commit;
  - in a sibling, the same two cases.
  - Positive control: the same reset with the check disabled through
    `mock.patch` is accepted. That shows the check, not a side effect, is
    what refuses.
- **Local rules.** A candidate that is not `HEAD` of the expected branch, or a
  dirty tree, refuses with `candidate_not_on_branch`. A verdict recorded at
  another SHA refuses accept.
- **Flags.** `--allow-push-for-run` refuses with `push_not_part_of_runs`, and
  nothing is created. `--allow-push-candidate` refuses with
  `push_permission_deprecated`, and the run state is byte-identical.
- **Capabilities.** The capability field is present, and `schema_version` is
  1.
- **Old assertions.** Every test that asserted the removed "not pushed"
  refusal or a push pause is converted or reclassified in the traceability
  table.

## Stage 3 — Unconditional remote refusal and offline operations

**Outcome:**
- **The refusal (I-L1).** `engine_git.run` refuses every remote command form
  with `remote_operation_not_allowed`. That includes forms reached through
  aliases, `-c` options, `--git-dir` or `-C`.
  - `run_remote` has no production caller. A test scans `src/` and asserts
    that.
- **Offline status.**
  - `runs --json` (the extension's poll), `slice-branch --json`, the handoff
    and `finish-run --dry-run` run no remote command.
  - The handoff's push line becomes "Local commit: <sha> on <branch>". Its
    `- Branch:` line is kept for the current extension.
- **The recorder.** A reusable test fixture puts a `git` wrapper first on
  `PATH`, which logs every subcommand to a file. Every end-to-end test in
  this plan uses it.

**Starting points:**
- `engine_git.py` (`run`, `_checked`, the remote-command classifier),
  `git_context.py`, `managed_run.py` (`runs` report), `intake_branch.py`,
  `handoff.py`, `cli.py`.

**Boundaries:**
- No change to acceptance rules (Stage 2) or to finish (Stage 4).

**Acceptance evidence:**
- **The refusal.** One test per remote command form refuses through
  `engine_git.run`. Positive control: the same form under plain `git` does
  contact a local bare remote, whose hook marker runs.
- **Planted configuration, end to end.** A repository plants
  `remote.origin.url` at a decoy, `url.*.insteadOf`, `core.sshCommand`, and
  `credential.helper` at marker scripts.
  - A full managed run, `runs --json` polling and the handoff record no
    remote subcommand, and no marker runs.
  - Positive control: plain `git ls-remote` in that repository runs the
    planted program.
- **No production caller.** The `src/` scan finds no caller of `run_remote`.

## Stage 4 — Local finish and safe legacy migration

**Outcome:**
- **The default finish is local.** `finish-run` merges into the local target
  in every participating repository, after I-L2b holds in each. It then
  cleans up (optional plan removal, worktree removal, local branch
  deletion). It contacts no remote.
  - It no longer emits `keep_remote_branch` or `candidate_not_pushed`. The
    current extension then hides its push choice by itself.
  - `--push-target` and `[finish] delete_remote_branch = true` refuse with
    `remote_finish_removed`, naming Plan P's Push to review. `check-config`
    reports a config that still sets `delete_remote_branch`.
- **Legacy push pauses (L-D1, strict).** On a plain `resume-plan`, a legacy
  `push_authorization_required` pause converts only if **all** of these hold:
  1. `awaiting` is exactly that pause, for the current stage, and the stage is
     not accepted;
  2. the recorded verdict (`read_recorded_outcome`) is `READY`, and its
     reviewed commit equals the paused candidate SHA and `HEAD` of the
     expected branch;
  3. the tree is clean apart from stage artifacts, every sibling is at its
     recorded candidate, and the plan digest is unchanged;
  4. no deferred obligation is promoted, and the recorded verdict names no
     `promote_deferred`;
  5. the recorded verdict has no deferred gate without an instance id;
  6. every human check of the stage's recorded gate is answered Pass.

  **Then it runs the ordinary post-`READY` sequence, unchanged:**
  1. local freeze and accept, I-L2b included;
  2. `_reconcile_deferred` over the recorded verdict;
  3. the promoted, `gates_before` and completion checks.

  It records `legacy_push_pause_converted` (stage, SHA, time) and prints a
  notice. If any condition fails, the pause stays, and resume reports which
  condition failed. Nothing is pushed or granted, and the stage is never
  re-entered as an implementation turn.
- **Recorded incident pauses (L-D2).** `unauthorized_remote_movement` and
  `pushed_candidate_unauthorized` pauses stay until a person answers them
  with the existing local commands.
  - The answer clears only that pause (I-L3).
  - A pause raised mid-loop then resumes the stage normally.
  - A pause raised after `READY` then goes through the L-D1 conditions above.
- **Legacy records.** Legacy pins and remote records load inertly. They are
  never read to authorize, accept or finish.

**Starting points:**
- `managed_finish.py` (`_run_guard`, `candidate_not_pushed`,
  `keep_remote_branch`, the `push_target` and `delete_remote_branch` steps,
  `_destination`), `config.py` (`[finish]`).
- `plan.py` (resume of paused legacy runs, `_reconcile_deferred`),
  `acceptance.py`, `cli.py`.

**Boundaries:**
- No remote contact anywhere. There is no publication (Plan P).

**Acceptance evidence (real repositories, legacy-shaped state fixtures):**
- **Local finish.** With and without a bare remote, and with a sibling:
  - the target is merged locally in each repository;
  - the recorder shows no remote subcommand;
  - the remote's refs are unchanged.
- **Finish history.** A finish after an accepted commit was rewritten away in
  the primary, or in a sibling, refuses with `run_history_rewritten`, and
  nothing is merged. Positive control: the unrewritten run finishes.
- **Refusals.** `--push-target` and `delete_remote_branch` refuse with
  `remote_finish_removed`, and nothing is merged.
- **Conversion, positive.**
  - A pause meeting every condition converts, is accepted locally, and
    records `legacy_push_pause_converted`.
  - A pause followed by a `gates_before` on the next stage converts, then
    stops at that gate.
  - A recorded verdict that defers a check converts, and the obligation is in
    the ledger afterwards.
- **Conversion, negative.** Each of the following stays paused, names the
  failed condition, and leaves state byte-identical:
  - a promoted obligation;
  - a moved `HEAD`;
  - a non-`READY` verdict;
  - a dirty tree;
  - a moved sibling;
  - an edited plan;
  - an unanswered human check.
- **Incident pauses.** They stay until answered. The answer clears only the
  pause and accepts nothing.
- **The five finish tests,** retargeted as in "The seven failing tests".

## Stage 5 — Remaining local Git hardening

**Outcome (audit R-4 and R-6 to R-8):**
- **R-4, ownership.** In a managed worktree, the owning run comes from the
  managed record. A `.sparring/` claim that contradicts it refuses with
  `stage_owner_mismatch`. That covers `run` cleared, `current_stage`
  changed, or plan state deleted.
- **R-6, signing programs.**
  - Marker tests establish which engine commands run a signing or
    verification program from local or worktree scope: `log --format=%H`,
    `reflog show` and the finish merge `commit`.
  - Only local or worktree-scope `log.showSignature`, `commit.gpgSign`,
    `tag.gpgSign`, `merge.verifySignatures` and `gpg.*program` values are
    neutralized or refused, and only where a test shows the program runs.
  - The user's global signing is kept, so finish merges are still signed.
- **R-7, submodules.** Engine `status` and `diff` use
  `--ignore-submodules=dirty` and `submodule.recurse=false`. A gitlink
  pointer change is still reported dirty.
- **R-8, listing failure.** A failed configuration listing inside a repository
  refuses with `repository_config_unreadable`.

**Starting points:**
- `acceptance.py` (`_owning_run`), `managed_run.py` (`record_for_worktree`),
  `engine_git.py` (`BASE_OVERRIDES`, `config_entries`, `_checked`),
  `managed_finish.py`, `git_context.py`.

**Acceptance evidence:**
- **R-4.** Each ownership manipulation refuses with `stage_owner_mismatch`.
  Positive control: the unmanipulated stage is accepted.
- **R-6.** For each engine command, a local-scope marker signing program runs
  under plain Git (positive control) and not under the engine. A globally
  configured signer still signs the finish merge.
- **R-7.** A populated submodule with a clean-filter marker in its own
  configuration: the marker runs under plain `git status`, and not under the
  engine's. An uncommitted gitlink change is still reported dirty.
- **R-8.** The typed refusal, with no command run.

## Design decisions

- **L-D1 (approved 2026-10-23, conditional).** A legacy push pause converts
  only under the six conditions in Stage 4. Each is verified at conversion,
  never assumed. Then the ordinary post-`READY` sequence runs. A push-only
  permission never becomes acceptance of unreviewed code.
- **L-D2 (approved 2026-10-23).** Remote-movement monitoring and all remote
  contact are removed from normal runs. Recorded incident pauses stay
  available for explicit resolution.
- **L-D3.** Deprecated flags refuse with typed codes and guidance
  (`push_not_part_of_runs`, `push_permission_deprecated`,
  `remote_finish_removed`). They are never aliases (I-L6, the user's
  correction of 2026-10-23).
- **L-D4.** Publication is Plan P. Plan L has no remote exception at all.
- **L-D5.** I-L2b is per repository (the user's correction of 2026-10-23).

## After Plan L is accepted (human)

1. Run the credential-free isolated test at the final exact SHA.
2. Run an independent security review of `36e2cea..<final sha>`, focused on
   I-L1 to I-L6.
3. Run the companion extension plan in `agent-sparring-vscode`:
   - feature-detect `local_first`;
   - hide the auto-push start option, the "Allow push" panel and the finish
     push choice;
   - show converted legacy pauses as "Continue locally (no push)", which runs
     a plain `resume-plan`;
   - make polling offline.

   It can ship after Stage 2 is integrated. Older extensions keep working,
   and receive typed refusals for the removed push actions.
4. Then Plan P, then Plan D, then Plan 2.
