# Complete and verify engine Git authority (issue #5, Plan R2)

Plan R (`2ba8d59..36e2cea`) was accepted by its in-run reviewers without one
test being run. Run in isolation afterwards, it failed seven tests, and the
independent audit found authority bypasses at its edges. This plan finishes
Plan R and proves it. It is not a new design. Nothing in it relaxes an
invariant of Plan R.

**Inputs:**
- Plan R: `docs/plans/issue5-r-complete-engine-git-authority.md`
  (invariants I-R1 to I-R4, decision D2).
- The audit: `docs/audits/issue5-plan-r-audit-2026-10-22.md`
  (findings R-1 to R-15). Line numbers there are at `36e2cea`.
- The isolated test run of `36e2cea`, on 2026-10-22:
  - focused: 381 tests, 1 failure;
  - full: 2071 tests, 7 failures.

  The evidence is in `/Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-20261022T174121Z/`.

**Base.** `plan/issue5-r2`, which is `36e2cea` plus planning documents only.

**Separate from Plans A to D and Plan 2:**
- no provider launch changes;
- no sandbox or credential guarantees;
- no cross-account execution;
- no verification-evidence model.

A finding that needs any of those is recorded as a Plan D residual risk, not
fixed here.

## Verification rules (every stage)

These apply to the stage agent, the reviewer and the human.

- **Tests run, or the stage is not ready.**
  - Before handing over, the stage agent runs, at the exact candidate commit:
    1. the focused tests named in its stage;
    2. the eight baseline files
       `tests/test_engine_git_authority.py tests/test_push_authorization.py tests/test_managed_finish.py tests/test_acceptance.py tests/test_accept_advanced_head.py tests/test_sparring_acceptance_concurrency.py tests/test_finalization.py tests/test_intake_branch.py`,
       plus `tests/test_no_live_remote_config.py` and `tests/test_fail_closed_pins.py`;
    3. the full suite, `.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs`.
  - Its handoff records, for each command:
    - the full candidate SHA;
    - the command exactly as run;
    - the exit code;
    - the passed, failed, error and skipped counts;
    - every failing test name.
  - A handoff without these is incomplete. The reviewer sends it back. The
    reviewer never accepts on "not run", "could not run" or "deferred".
- **A missing tool is a stop, not a deferral.** If `.venv/bin/python`,
  `pytest` or `git` cannot be run, the stage agent's first action is
  `uv sync --frozen`. If tests still cannot run, it reports `NEEDS_YOU` with
  the exact error. It never writes code it cannot test.
- **No weakening.** No test, assertion, invariant or refusal is removed,
  loosened, skipped, marked `xfail` or made conditional to make a run pass.
  - A test whose premise was wrong is replaced by one that checks the
    intended property at least as strictly.
  - The handoff names each replaced assertion and its replacement.
- **Each negative test has a positive control.** A test that asserts an attack
  is refused also shows, in the same test or its class, that the same attack
  succeeds without the protection:
  - plain `git` against the same disposable repositories; or
  - the protection disabled through a test-only seam that production code
    never reads.

  A negative result with no positive control proves nothing and does not
  count.
- **Real repositories.** Every Git-authority test uses real disposable
  repositories and bare remotes under the test's temporary directory. A mock
  is allowed only for a race window that cannot be produced deterministically.
  Such a mock is named in the handoff.
- **Each stage is checked in isolation.** The run stops before each later
  stage and before completion. A person then runs the credential-free kit
  test at the accepted SHA (`test-r2.sh <sha>`) and answers the gate. A
  reviewer's `READY` is never the final evidence.

## Stage 1 — Repair verification

**Context.** The seven failures at `36e2cea`:

1. **`test_engine_git_authority.HardenedGitTests::test_remote_operations_never_read_the_repositorys_own_configuration`**
   - The test calls `engine_git.run_remote` to push to a local bare remote
     without a pinned destination.
   - `3a40a73` added the `unpinned_local_remote` refusal. That refusal fires
     before the property under test (a planted `url.<evil>.pushInsteadOf` is
     never followed) is reached.
   - So that property is currently untested.
2. **The five `test_fail_closed_pins.FinishWithoutThePinTests` tests:**
   `test_the_pin_malformed`, `test_the_pin_removed_from_the_state`,
   `test_the_state_file_deleted`, `test_the_state_file_unreadable` and
   `test_the_state_replaced_with_a_legacy_shaped_one`.
   - Each asserts exit `1`. But `finish-run`'s documented contract
     (`docs/reference.md`, "Exit codes") is `3` when refused by its checks,
     and `1` when a step or the command failed.
   - The assertion fails first, so the security assertions after it never
     ran: `stopped_at == "checks"`, the `remote_pin_missing` reason, no
     completed steps, no ref changed anywhere, no `merged` event, and the
     worktree kept.
3. **`test_no_live_remote_config.UnguardedRemoteTests::test_push_forms_that_hide_the_destination_are_refused`**
   - The engine assertions passed. The failing line is a positive control:
     `plain git push --repo <path> <sha>:refs/heads/x` was expected to reach
     `<path>`.
   - Git 2.53.0 behaves otherwise, as established on 2026-10-22 with
     disposable repositories and a `post-receive` marker hook:
     - `--repo` is used only when no positional argument is given;
     - with `--repo X <refspec>`, Git takes `<refspec>` as the repository,
       and `X` is never contacted;
     - `git push --repo <path> --mirror` (the separate-value form, with no
       positional argument) does reach `<path>`. Its hook ran.

**Outcome:**
- **The `pushInsteadOf` regression.** The test pins the remote, plants
  `url.<evil>.pushInsteadOf` (and the other transport keys in its matrix)
  after pinning, and pushes through the path that is meant to ignore them:
  `run_remote(..., pinned=pin.destination)`, or the pinned push the engine
  really uses.
  - It asserts that the push lands on the pinned remote and that `<evil>`
    received nothing (its hook marker is absent and its refs are unchanged).
  - **Positive control:** plain `git push` from the same repository with the
    same planted key does reach `<evil>`.
  - A separate assertion keeps `unpinned_local_remote` for an unpinned local
    destination. Neither property hides the other.
- **The finish tests.**
  - They assert exit `3`, the documented code for "refused by its checks".
  - Every later assertion then runs, and must pass unchanged:
    `stopped_at == "checks"`, `remote_pin_missing` in the reason,
    `completed_steps == []`, the refs of the primary remote, the push mirror
    and the repository unchanged, no `merged` event, and the worktree still
    present.
  - **Positive control:** the same finish with the pin intact and
    `--push-target` completes. Its push reaches the push destination. This
    shows the refusal, not an unrelated failure, is what held the refs still.
- **The `--repo` control.** The separate-value case uses a valid control:
  `plain git push --repo <path> --mirror` reaches `<path>`, shown by its hook
  marker and its changed refs.
  - The engine assertions are unchanged. Every form that hides the
    destination, including `--repo=<path>` and `--repo <path>`, stays refused
    as `unsupported_remote_form`.
  - A comment records the Git 2.53.0 semantics. A test asserts those
    semantics directly, so a future Git that changes them fails loudly
    instead of silently invalidating the control.
- **A positive-control audit of the two new test files.** Every negative test
  in `tests/test_no_live_remote_config.py` and `tests/test_fail_closed_pins.py`
  either has a positive control, or gains one. The handoff lists each
  negative test with its control.
  - The audit's "Tests" section names two vacuous checks: `sshCommand` and
    the credential-helper "no marker" assertions, with local remotes. Each
    gains a control in which the planted program demonstrably runs under
    plain Git, for example an `ssh://` URL to an unreachable host, where
    plain Git starts the planted `core.sshCommand` before failing. If no
    deterministic control exists, the check is rewritten to assert something
    it can observe, and the handoff says so.

**Starting points:**
- `tests/test_engine_git_authority.py` (`HardenedGitTests`),
  `tests/test_fail_closed_pins.py` (`FinishWithoutThePinTests._refused`),
  `tests/test_no_live_remote_config.py` (`UnguardedRemoteTests`).
- `docs/reference.md` ("Exit codes"), `src/agent_sparring/engine_git.py`
  (`run_remote`, `_destination`).

**Boundaries:**
- Tests and test helpers only. A production change is allowed only if a
  repaired test exposes a real defect. It is then the smallest fix, named in
  the handoff, and it never relaxes a refusal.
- No change to the exit-code contract.

**Acceptance evidence:**
- All seven tests pass at the candidate, each with its positive control
  shown.
- The focused set, the two new files and the full suite all pass. They are
  recorded as in "Verification rules".
- For each of the seven, the handoff shows the old assertion, the new one and
  why the new one is no weaker.

## Stage 2 — Close authority bypasses

**Context (audit R-1 to R-4, R-13):**
- **R-1.** `git_context.verify_pushed` takes `remote.<r>.url`, `branch.<b>.remote`
  and `branch.<b>.merge` from live configuration, and
  `refuse_repository_transport` does not cover those keys. An agent can
  redirect the read to a decoy repository holding the candidate, so the
  engine reports "pushed", or make the engine contact a host it chose.
  Callers:
  - `acceptance.py` `_sibling_pushed`: an unpinned sibling;
  - `_pushed`: a stage no run owns;
  - `git_context.py`: a handoff without a pin.
- **R-2.** Siblings are checked for reachability only, with no movement check
  and no receipts. Removing `sibling_pins` from run state silently falls back
  to the R-1 read.
- **R-3.** `remote_pin._covered` counts the stage `base_sha` from the
  agent-writable stage `state.json` as explaining a candidate. After an
  acknowledgement, setting `base_sha` to a pushed commit skips
  `pushed_candidate_unauthorized`.
- **R-4.** `acceptance._owning_run` decides ownership from `.sparring/` files
  only. Clearing `run`, or changing `current_stage`, makes a run's stage look
  hand-driven.
- **R-13.** The finish takes the first readable pin, with no cross-check. An
  altered but well-formed pin is accepted.

**Outcome:**
- **I-R1 holds for every remote read (R-1).** No engine code determines
  remote authorization, or reports a candidate as pushed, from live
  repository configuration. That includes `remote.*.url`, `branch.*.remote`,
  `branch.*.merge`, `url.*`, `remote.*.pushurl` and transport keys.
  - A remote read that a freeze, an accept, a handoff "Pushed" line or a push
    authorization depends on uses a run-start pin.
  - If no pin can govern the repository, the read refuses with a typed code
    (`remote_unpinned`). It never falls back to a live-configuration read.
  - Hand-driven stages that no run owns keep working for local operations.
    Their "pushed" status is reported as "not checked: no pin", never as
    pushed.
- **Siblings are first-class (R-2).**
  - `start-plan` and `resume-plan` record a pin for each sibling repository,
    the same as for the primary.
  - The set of pinned siblings (path, branch and pin digest) is recorded in
    the engine record outside `.sparring/`, alongside the existing pinned
    flag.
  - `check_movement` and push receipts apply to each sibling pin after every
    turn, under the same rules as the primary. A sibling movement that
    nothing explains pauses the run with `unauthorized_remote_movement`, and
    names the repository.
  - Authorizing a sibling commit already on its remote uses the same explicit
    per-commit CLI action as for the primary (D2). An acknowledgement never
    authorizes it.
  - A sibling that is listed in the outside record but missing from run
    state, malformed or unreadable refuses with `remote_pin_missing`. It never
    falls back.
- **Pins fail closed when altered (R-13, and "missing or corrupted").**
  - The engine record outside `.sparring/` holds a digest of each run-start
    pin.
  - A pin whose digest differs, whether from the worktree copy or the archive
    copy, refuses with `remote_pin_mismatch`.
  - Missing, malformed or unreadable pins keep refusing with
    `remote_pin_missing`, as Plan R already does.
- **Agent-writable stage state confers nothing (R-3, R-4).**
  - The stage `base_sha` counts as an explanation only when it is itself
    covered by a push receipt or by the non-legacy run-start tip. It never
    adds an explanation of its own.
  - Ownership: when `repo_root` is a managed worktree, the owning run comes
    from its managed record (`record_for_worktree`).
    - When the outside record names an owner and `.sparring/` does not
      (`run` cleared, `current_stage` changed, plan state deleted), the
      operation refuses with `stage_owner_mismatch`.
    - Unmanaged runs record their stage claims under
      `<git-common-dir>/agent-sparring/runs/`, and get the same refusal.
- **The residual risk is recorded.** `.git` and `.sparring/` stay writable by
  a same-user agent, so these records raise the bar against accidental or
  one-step tampering. They are not a boundary. Code comments and
  `docs/reference.md` say so, and point to Plan D.

**Starting points:**
- `git_context.py` (`verify_pushed`, `tracking_remote`, the handoff read).
- `acceptance.py` (`_sibling_pushed`, `_pushed`, `_owning_run`,
  `_runs_positioned_at`, the `state.base_sha` uses).
- `plan.py` (`_pin_siblings`, the movement checks, turn bookkeeping).
- `remote_pin.py` (`_covered`, `check_movement`, `verify_pinned`).
- `managed_run.py` (record, `run_flag_path`), `managed_finish.py` (pin
  selection), `engine_git.py` (`refuse_repository_transport`),
  `handoff.py`, `push_gate.py`.

**Boundaries:**
- No provider, sandbox or cross-account changes. No VS Code changes beyond
  showing the new refusal codes as text.

**Acceptance evidence.** All of it uses real disposable repositories and bare
remotes. Each case has a positive control.
- **R-1:**
  - Planting `remote.origin.url` pointed at a decoy holding the candidate,
    or `branch.<b>.merge` pointed at another ref, never yields "pushed". The
    decoy's hook marker is absent.
  - Control: plain `git ls-remote` with the same configuration does read the
    decoy.
  - An unpinned read refuses with `remote_unpinned`.
- **R-2:**
  - An agent-style push of an unreviewed commit to a pinned sibling's ref,
    during a turn, pauses the run and names the sibling.
  - The explicit per-commit authorization lets it continue. An
    acknowledgement alone does not.
  - Deleting or corrupting `sibling_pins` refuses with `remote_pin_missing`.
  - Control: the same push with sibling checks disabled through a test seam
    goes unnoticed.
- **R-3.** After an acknowledgement, setting the stage `base_sha` to the pushed
  commit still produces `pushed_candidate_unauthorized`.
- **R-4.**
  - Clearing `run` in the stage state, editing `current_stage` or deleting
    the plan state refuses with `stage_owner_mismatch`.
  - None of these reaches an unpinned read.
- **R-13.** Altering a well-formed pin's destination or tip refuses with
  `remote_pin_mismatch`, in finish, freeze, accept and resume.
- **Regressions.** Every Plan R test still passes. Legitimate engine-authorized
  pushes of the primary and of siblings still succeed. Normal read-only Git
  operations (status, diff, log, rev-parse) are unchanged.
- **Test runs.** Recorded as in "Verification rules".

## Stage 3 — Compatibility and residual privileged Git execution

**Context (audit R-5 to R-12, R-14):**
- **R-5.** A legacy managed run, created before pins and having a remote,
  appears unable to finish:
  - `candidate_not_pushed` reads "not checked";
  - `--push-target` and remote-branch deletion refuse as
    `unguarded_remote_operation`.
- **R-6.** `BASE_OVERRIDES` does not neutralize `log.showSignature`,
  `commit.gpgSign`, `tag.gpgSign`, `merge.verifySignatures` or `gpg.*program`.
- **R-7.** `status` and `diff` recurse into submodules, whose own
  configuration (filters, `diff.submodule`) is never checked.
- **R-8.** A failed `git config --list` makes `_checked` run the command
  against live configuration, unchecked.
- **R-9.** A handoff for a pinned run whose pin is missing falls back to the
  unpinned read.
- **R-10.** An intake slice move re-pins a pinned run from current
  configuration.
- **R-11.** `ensure_candidate_pushed(pin=None)` pins from live configuration
  and then pushes. It has no current caller.
- **R-12:**
  - A resume has no movement check before its first turn.
  - Push-then-rewind within one turn is not seen.
- **R-14.** The push race check compares abbreviated SHAs by prefix.

**Outcome:**
- **R-5, legacy finish.** Decision R2-D1 (below) is applied.
  - The recommended behaviour: `finish-run` on a legacy managed run that has a
    remote refuses with the typed code `legacy_run_needs_pin`. Its message
    names the command that pins it: one `resume-plan`, with the existing
    visible legacy notice.
  - After that resume, the finish works normally. A legacy run with no
    remote finishes as it does today.
  - `docs/reference.md` and the `_run_guard` docstring are corrected to match.
- **R-6, signing programs.**
  - Engine Git commands run with `log.showSignature=false`,
    `commit.gpgSign=false`, `tag.gpgSign=false` and
    `merge.verifySignatures=false`.
  - Outside a guard, a repository-scope `gpg.program`, `gpg.ssh.program` or
    `gpg.x509.program` refuses with `repository_program_config`.
  - Tests establish, against real Git with a marker script, which commands
    the engine uses would run the program without the override. That
    includes `log --format=%H` and `reflog show`. Each finding is recorded in
    the handoff, and the override is shown to stop it.
- **R-7, submodules.**
  - Engine `status` and `diff` pass `--ignore-submodules=all`, and engine Git
    runs with `diff.submodule=short` and `submodule.recurse=false`.
  - A test with a populated submodule, whose own config sets a clean filter
    to a marker script, proves that the filter runs under plain
    `git status` and does not run under the engine's.
  - Any other engine command found to recurse is either given the same
    treatment or refused while a gitlink is checked out.
- **R-8.** If the configuration listing fails and `cwd` is inside a
  repository, the operation refuses with `config_unreadable`.
- **R-9.** The handoff of a pinned run whose pin is missing reports
  `remote_pin_missing`.
- **R-10.** An intake slice move on a pinned run with no `remote_pin` refuses
  with `remote_pin_missing`.
- **R-11.** `ensure_candidate_pushed` requires a pin. A missing one raises
  before any Git command runs.
- **R-12:**
  - Every resume runs the movement check, with the current `HEAD` as the
    candidate, before its first agent or reviewer turn.
  - Push-then-rewind within one turn is documented in `docs/reference.md` as
    undetectable without server-side logs. That is a disposition, not a fix.
- **R-14.** The race check compares full SHAs: `core.abbrev` is unset for the
  porcelain push, or the result is compared with the re-read tip.
- **Unchanged.** Normal read-only Git operations and engine-authorized pushes
  are unchanged, except for the overrides above.

**Starting points:**
- `managed_finish.py` (legacy guard, `candidate_not_pushed`, the remote
  steps), `engine_git.py` (`BASE_OVERRIDES`, `config_entries`, `_checked`).
- `git_context.py`, `managed_run.py` (`status` and `reflog` sites),
  `handoff.py`, `intake_branch.py`, `push_gate.py`, `plan.py` (resume entry).

**Boundaries:**
- No VS Code changes (R-15 belongs to Plan D's companion extension plan).
- No change to the CLI exit-code contract.

**Acceptance evidence.** All of it uses real disposable repositories and bare
remotes. Each negative case has a positive control.
- **R-5.** A legacy-shaped managed run with a remote refuses with
  `legacy_run_needs_pin`. After one resume it finishes, including
  `--push-target`. A legacy run with no remote finishes unchanged.
- **R-6 and R-7.** The marker tests described above. Each has a plain-Git
  positive control, so the marker does run without the override.
- **R-8 to R-11.** One regression test each, asserting the typed refusal. No
  Git command contacts a remote in any of them.
- **R-12.** A run whose engine was killed after an agent push, simulated by
  ending the process without the after-turn check, pauses on resume before
  any provider turn starts.
- **R-14.** A unit test with two distinct SHAs that share a prefix.
- **Regressions.** Every Plan R and Stage 1 to Stage 2 test still passes.
- **Test runs.** Recorded as in "Verification rules".

## Design decisions

- **R2-D1, legacy finish (R-5).** The plan applies the recommended option:
  refuse with `legacy_run_needs_pin`, and pin through one `resume-plan`.
  - The alternative is to pin automatically at finish, from current
    configuration, with a notice.
  - That alternative is rejected because it would take the snapshot at the
    moment an agent has had the most time to plant configuration, which is
    what R-10 forbids.
  - **Confirm before approval.**
- **R2-D2, sibling scope (R-2).** Siblings are given the full movement,
  receipt and per-commit authorization model. The alternative was to
  document them as reachability-only, outside D2.
- **R2-D3, provenance (R-3, R-4, R-13).** These fixes use records under
  `.git`, which a same-user agent can still write. They close one-step and
  accidental bypasses. They are not a boundary. Plan D owns the boundary.

## After Plan R2 is accepted (human)

1. Run a fresh credential-free isolated test (`test-r2.sh <final sha>`) at the
   final exact SHA, whatever the in-run reviewer said.
2. Run an independent security review of `36e2cea..<final sha>`.
3. Integrate into `main` only after both, as a deliberate human merge. No
   push is implied.
