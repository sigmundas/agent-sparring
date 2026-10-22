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
  (findings R-1 to R-15, and its "Coverage by focus area" table). Line numbers
  there are at `36e2cea`.
- The isolated test run of `36e2cea`, on 2026-10-22:
  - focused: 381 tests, 1 failure;
  - full: 2071 tests, 7 failures.

  The evidence is in `/Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-20261022T174121Z/`.

**Base.** `plan/issue5-r2`, which is `36e2cea` plus planning documents and
the Plan R2 kit (`docs/audits/issue5-r2-kit/`) only.

**Separate from Plans A to D and Plan 2:**
- no provider launch changes;
- no sandbox or credential guarantees;
- no cross-account execution;
- no verification-evidence model.

A finding that needs any of those is recorded as a Plan D residual risk, not
fixed here.

## Run prerequisites (run configuration, not plan scope)

The last run could not test because of its environment, not its code:
- there was no `.venv` in the managed worktree;
- Claude refused Bash `pytest` calls for approval under
  `--permission-mode acceptEdits`;
- the focused set (705 s) and the full suite (2073 s) are both longer than
  Claude's default Bash timeout.

These are fixed outside the loop, before the run starts, by the Plan R2 kit.
None of it changes the engine's provider launch code, so it is not Plan A
work.
- **`setup-r2.sh`** prepares the account:
  - runs `uv sync --frozen` (which warms the uv cache);
  - allows exactly `uv sync --frozen`, `.venv/bin/python -m pytest …` and
    local Git read and commit commands, and denies `git push`;
  - raises the Bash timeouts, through `BASH_DEFAULT_TIMEOUT_MS` and
    `BASH_MAX_TIMEOUT_MS` in the agent account's Claude settings.
- **`preflight-r2.sh`** starts Claude exactly as the frozen runner does
  (`-p`, `stream-json`, `--verbose`, `--permission-mode acceptEdits`), in a
  linked worktree with no `.venv`. It must show that Claude runs
  `uv sync --frozen` and a real-Git test file that takes longer than the
  default timeout (`tests/test_managed_finish.py`, about 310 s), with the
  same count as a direct run. The run cannot start until it prints `PASS`.
- **The reviewer** (Codex, in a read-only OS sandbox) cannot run tests that
  create repositories. The human isolated-test gates replace that, and the
  reviewer checks what can be checked statically (see "Verification rules").

## Verification rules (every stage)

These apply to the stage agent, the reviewer and the human.

- **Tests run, or the stage is not ready.**
  - Before handing over, the stage agent runs, at the exact candidate commit:
    1. the focused tests named in its stage;
    2. the eight baseline files
       `tests/test_engine_git_authority.py tests/test_push_authorization.py tests/test_managed_finish.py tests/test_acceptance.py tests/test_accept_advanced_head.py tests/test_sparring_acceptance_concurrency.py tests/test_finalization.py tests/test_intake_branch.py`,
       plus `tests/test_no_live_remote_config.py` and `tests/test_fail_closed_pins.py`;
    3. the full suite, `.venv/bin/python -m pytest -q -p no:cacheprovider -rfEs`.
  - Each run passes `--junitxml` to a path under `$TMPDIR`. If one command
    would exceed the Bash timeout, the full suite may be run in shards of
    test files that together cover every file under `tests/` exactly once.
  - The handoff records, for each command or shard:
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
  - The handoff names each replaced or removed assertion, its replacement,
    and why the replacement is no weaker.
  - The diff adds no `skip`, `skipIf`, `skipUnless`, `xfail` or
    `expectedFailure`. The full-suite test count never drops, except for
    renames the handoff lists.
- **Each negative test has a positive control.** A test that asserts an attack
  is refused also shows, in the same test or its class, that the same attack
  succeeds without the protection. Acceptable controls are:
  - plain `git` against the same disposable repositories;
  - the same scenario on a path that is already protected, shown to be
    detected (for example the primary repository, for a sibling test);
  - the protection disabled with `unittest.mock.patch` in the test only.

  A negative result with no positive control proves nothing and does not
  count.
- **Real repositories.** Every Git-authority test uses real disposable
  repositories and bare remotes under the test's temporary directory. A
  `mock.patch` is allowed only:
  - for a race window that cannot be produced deterministically; or
  - to disable a protection as a positive control.

  Each one is named in the handoff.
- **Traceability.** The handoff has a table: acceptance item → test id →
  positive-control test id.
  - The reviewer checks it against the diff without running anything. Every
    item has a test, every negative test has its control, and no assertion
    was removed or weakened.
  - The human gate then confirms in the isolated run's junit XML that those
    ids ran and passed.
- **Each stage is checked in isolation.**
  - The run is started from the version 2 manifest
    `docs/plans/issue5-r2.manifest.json`, which is generated from this
    document by `docs/audits/issue5-r2-kit/make-manifest-r2.py`.
  - The frozen runner stops at these gates and only
    `resume-plan --deferred-result` releases them:
    - before Stage 2: `isolated-tests-before-issue5-r2-stage-2-close-authority-bypasses`
      (`manual`), then `go-ahead-issue5-r2-stage-2-close-authority-bypasses`
      (`authorization`);
    - before Stage 3: `isolated-tests-before-issue5-r2-stage-3-compatibility-and-residual-git-execution`
      (`manual`), then `go-ahead-issue5-r2-stage-3-compatibility-and-residual-git-execution`
      (`authorization`);
    - before completion: `isolated-tests-final` (`manual`), then
      `go-ahead-complete` (`authorization`).
  - At each isolated-test gate, a person runs
    `/Users/Shared/sparring-recovery-r2/test-r2.sh <sha>`, as `sparring-agent`,
    at the exact accepted SHA. It verifies:
    - the SHA and that the base is an ancestor;
    - a fresh clone with no remote, and a fresh uv cache;
    - the focused set, then the full suite;
    - the full-suite count;
    - no added skip markers and no removed asserts in `tests/`.

    The person copies the evidence to the owner's account and answers `pass`
    only if its summary supports `pass`. A reviewer's `READY` is never this
    evidence.
- **A failed gate.**
  - The run stays stopped. The accepted stage is not integrated.
  - Fixes go into a new candidate on the run branch (a send-back, through
    `resume-plan --deferred-result …=fail` and the runner's normal path),
    and the gate is asked again at the new SHA.
  - If the failure is a design problem rather than a defect, it goes to a
    person as `NEEDS_YOU`, not a fourth send-back.
- **Send-back cap.** The run uses `--max-send-back-cycles 3`. After three
  send-backs the run stops for a person, who reviews the design before any
  further round.

## Stage 1 — Repair verification

**Context.** The seven failures at `36e2cea`:

1. **`test_engine_git_authority.HardenedGitTests::test_remote_operations_never_read_the_repositorys_own_configuration`**
   - The test calls `engine_git.run_remote` to push to a local bare remote
     without a pinned destination.
   - The `unpinned_local_remote` refusal, added in `86f5065`, fires before
     the property under test (a planted `url.<evil>.pushInsteadOf` is never
     followed) is reached.
   - So that property is currently untested.
2. **The five `test_fail_closed_pins.FinishWithoutThePinTests` tests:**
   `test_the_pin_malformed`, `test_the_pin_removed_from_the_state`,
   `test_the_state_file_deleted`, `test_the_state_file_unreadable` and
   `test_the_state_replaced_with_a_legacy_shaped_one`.
   - Each asserts exit `1`. But `finish-run`'s documented contract
     (`docs/reference.md`, "Exit codes"; `cli.py` returns `3` when
     `stopped_at == "checks"`) is `3` when refused by its checks, and `1` when
     a step or the command failed.
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
  - It asserts that the push lands on the pinned remote, and that `<evil>`
    received nothing: its hook marker is absent and its refs are unchanged.
  - **Positive control:** plain `git push` from the same repository with the
    same planted key does reach `<evil>`, shown by its marker and its refs.
  - A separate assertion keeps `unpinned_local_remote` for an unpinned local
    destination. Neither property hides the other.
- **The finish tests.**
  - They assert exit `3`. Every later assertion then runs, and must pass
    unchanged.
  - **Positive control:** the same completed run, with the pin intact and no
    planted `pushurl`, finishes with `--push-target`. Its push lands on the
    pinned remote, and `push.git` stays empty.
  - The planted-`pushurl` case with an intact pin stays covered by
    `test_managed_finish.py`
    `test_a_push_url_added_after_the_run_started_is_refused_by_name`.
- **The `--repo` control.** The separate-value case uses a valid control:
  `plain git push --repo <path> --mirror` reaches `<path>`, shown by its hook
  marker and its changed refs.
  - The engine assertions are unchanged. Every form that hides the
    destination, including `--repo=<path>` and `--repo <path>`, stays
    refused as `unsupported_remote_form`.
  - A test asserts the Git 2.53.0 semantics directly, so a future Git that
    changes them fails loudly instead of silently invalidating the control.
- **A positive-control audit of the two new test files.**
  - Every negative test in `tests/test_no_live_remote_config.py` and
    `tests/test_fail_closed_pins.py` either has a control or gains one. The
    traceability table lists each one.
  - The audit's "Tests" section calls the `sshCommand` and credential-helper
    "no marker" checks vacuous with local remotes. Each gains a control in
    which the planted program demonstrably runs under plain Git: for
    example, an `ssh://` URL to an unreachable host, for which plain Git
    starts the planted `core.sshCommand` before failing.
  - If no deterministic control exists, the check is rewritten to assert
    something it can observe, and the handoff says so.

**Starting points:**
- `tests/test_engine_git_authority.py` (`HardenedGitTests`),
  `tests/test_fail_closed_pins.py` (`FinishWithoutThePinTests._refused`),
  `tests/test_no_live_remote_config.py` (`UnguardedRemoteTests`),
  `tests/test_managed_finish.py`.
- `docs/reference.md` ("Exit codes"), `src/agent_sparring/engine_git.py`
  (`run_remote`, `_destination`).

**Boundaries:**
- Tests and test helpers only. A production change is allowed only if a
  repaired test exposes a real defect. It is then the smallest fix, named in
  the handoff, and it never relaxes a refusal.
- No change to the exit-code contract.

**Acceptance evidence:**
- All seven tests pass at the candidate, each with its positive control.
- The focused set, the two new files and the full suite all pass. They are
  recorded as in "Verification rules", with the traceability table.

## Stage 2 — Close authority bypasses

**Context (audit R-1 to R-4, R-13):**
- **R-1.** `git_context.verify_pushed` takes `remote.<r>.url`, `branch.<b>.remote`
  and `branch.<b>.merge` from live configuration, and
  `refuse_repository_transport` does not cover those keys.
  - An agent can redirect the read to a decoy repository holding the
    candidate, so the engine reports "pushed", or make the engine contact a
    host it chose.
  - Refusing while `remote.<r>.url` is set locally does not work, because
    every clone sets it.
- **Other live-configuration remote paths**, found by the plan review:
  - `push_gate._binding(pin=None)` (via `intended_remote` and
    `tracking_remote`);
  - `managed_finish._destination` when `record.remote != pin.remote`
    (`resolved_push_url`);
  - `_remote_contains` and `_remote_sha` used with a remote name in the
    legacy path;
  - `slice-branch --json`, which the audit could not trace.
- **R-2.** Siblings are checked for reachability only, with no movement check
  and no receipts. Removing `sibling_pins` from run state silently falls back
  to the R-1 read.
- **R-3.** `remote_pin._covered` counts the stage `base_sha` from the
  agent-writable stage `state.json` as explaining a candidate.
- **R-4.** `acceptance._owning_run` decides ownership from `.sparring/` files
  only.
- **R-13.** The finish takes the first readable pin, with no cross-check.
- **Freeze and accept require "pushed".** `acceptance.py`
  (`freeze`/`accept`/`enter_review`, and the sibling check) refuses unless
  the push read succeeds. So removing the live-configuration read without
  replacing it would silently disable hand-driven stages (see R2-D4).

**Outcome:**
- **I-R1 holds for every remote read (R-1).** No engine code determines remote
  authorization, or reports a candidate as pushed, from live repository
  configuration. That includes `remote.*.url`, `branch.*.remote`,
  `branch.*.merge`, `url.*`, `remote.*.pushurl` and transport keys.
  - Every remote read that a freeze, an accept, `enter_review`, a handoff
    "Pushed" line or a push authorization depends on uses a pin.
  - **Hand-driven stages (R2-D4).** A stage that no run owns gets a pin when
    it is created (`run-stage`, or the stage-init path). It records the
    remote, branch, URL, snapshot and start tip, in the same form as a run
    pin, under `<git-common-dir>/agent-sparring/stages/<stage id>.json`.
    Freeze and accept then use `verify_pinned`.
  - A stage created before R2 has no pin. It refuses with
    `remote_unpinned`, and the message names the explicit command that pins
    it (`sparring pin-stage <stage id>`). That command prints the
    destination it pins, which is a visible human action.
  - Without a pin, the read refuses with `remote_unpinned`. It never falls
    back to a live-configuration read. The handoff reports "not checked: no
    pin" instead of "pushed".
  - **A call-site sweep.** The handoff includes a table of every hit for
    `git grep -n "verify_pushed\|tracking_remote\|resolved_push_url\|get-url\|ls-remote\|intended_remote"`
    in `src/`, each with its disposition (pinned, refused, local-only, or
    removed). The four paths listed above are fixed or refused.
- **Siblings (R-2, R2-D2).**
  - `start-plan` and `resume-plan` record a pin for each sibling repository.
  - The set of pinned siblings (path, branch and pin digest) is recorded in
    the engine record outside `.sparring/`.
  - `check_movement` and push receipts apply to each sibling pin after every
    turn. A sibling movement that nothing explains pauses the run with
    `unauthorized_remote_movement`, naming the repository.
  - The per-commit authorization CLI action (D2) accepts a sibling.
  - A sibling listed outside `.sparring/` but missing from run state,
    malformed or unreadable refuses with `remote_pin_missing`.
  - A sibling that is not a repository at run start, or has no URL then,
    refuses freeze and accept with `remote_unpinned`. It is never read
    through live configuration.
- **Pin digests (R-13).**
  - The engine record outside `.sparring/` holds a digest of every pin. It is
    written at exactly the sites that legitimately write a pin:
    - run start (`plan.py` start);
    - legacy pinning on resume;
    - an intake slice move that carries a pin;
    - sibling pinning;
    - stage pinning (R2-D4).
  - A pin whose digest differs, whether from the worktree copy or the archive
    copy, refuses with `remote_pin_mismatch`. Missing, malformed or unreadable
    pins keep refusing with `remote_pin_missing`.
  - **In-flight runs** pinned by Plan R have no digest. Their first resume
    after R2 records one from the current `.sparring/` pin. That is
    trust-on-first-use: it is named in `docs/reference.md`, and the resume
    prints the pin it adopted.
- **Stage base (R-3).** The `stage_base` parameter of `_covered` is removed.
  A candidate is explained only by a push receipt, a per-commit authorization
  or the non-legacy run-start tip. The stage `base_sha` explains nothing.
- **Ownership (R-4).**
  - In a managed worktree, the owning run comes from its managed record
    (`record_for_worktree`). When that record names a run and `.sparring/`
    does not (`run` cleared, `current_stage` changed, plan state deleted), the
    operation refuses with `stage_owner_mismatch`.
  - A hand-driven stage inside a managed worktree is refused with
    `stage_owner_mismatch`, deliberately and with that message.
  - Unmanaged runs record their stage claims under
    `<git-common-dir>/agent-sparring/runs/<run>.json` at stage entry. A run
    started before R2 has no claim. Its first resume after R2 records one,
    which is trust-on-first-use, named as for digests.
- **The residual risk is recorded.** `.git` and `.sparring/` stay writable by
  a same-user agent, so these records raise the bar against accidental or
  one-step tampering. They are not a boundary. Code comments and
  `docs/reference.md` say so, and point to Plan D.

**Starting points:**
- `git_context.py` (`verify_pushed`, `tracking_remote`, the handoff read),
  `acceptance.py` (`_sibling_pushed`, `_pushed`, `_owning_run`,
  `_runs_positioned_at`, the freeze, accept and `enter_review` refusals).
- `plan.py` (`_pin_siblings`, the movement checks, pin write sites),
  `remote_pin.py` (`_covered`, `check_movement`, `verify_pinned`).
- `managed_run.py` (record, `run_flag_path`), `managed_finish.py` (pin
  selection, `_destination`), `push_gate.py` (`_binding`), `intake_branch.py`,
  `handoff.py`, `engine_git.py`, `cli.py` (`run-stage`, new `pin-stage`).

**Boundaries:**
- No provider, sandbox or cross-account changes.
- Every new refusal code is listed in `docs/reference.md`.

**Acceptance evidence.** All of it uses real disposable repositories and bare
remotes. Each negative case has a positive control.
- **R-1:**
  - A decoy bare repository whose branch tip is a distinct SHA (not the
    candidate's). Planting `remote.origin.url` at it, or `branch.<b>.merge`
    at another ref, never yields "pushed". The engine reports `remote_unpinned`
    or reads the pinned destination.
  - Control: plain `git ls-remote` with the same configuration prints the
    decoy's distinct SHA.
  - One test per swept path from the call-site table.
- **R2-D4:**
  - A new hand-driven stage pins at creation and can be frozen and accepted,
    using the existing hand-driven acceptance tests unchanged, or changed
    only as the handoff lists.
  - A pre-R2 stage refuses with `remote_unpinned` until `pin-stage` runs, and
    then works.
- **R-2:**
  - An agent-style push of an unreviewed commit to a pinned sibling's ref,
    during a turn, pauses the run and names the sibling.
  - The per-commit authorization lets it continue. An acknowledgement alone
    does not.
  - Deleting or corrupting `sibling_pins` refuses with `remote_pin_missing`.
  - Control: the same scenario on the primary is detected. The same sibling
    test fails at `36e2cea`, shown in the handoff.
- **R-3.**
  - After an acknowledgement, setting the stage `base_sha` to the pushed
    commit still produces `pushed_candidate_unauthorized`.
  - A legacy-pinned run (start tip not counted) that relied on the stage base
    now pauses the same way. That behaviour change is documented.
- **R-4.** Each manipulation above refuses with `stage_owner_mismatch` and
  reaches no remote read. A hand-driven stage in a managed worktree refuses.
  A pre-R2 unmanaged run resumes once and records its claim.
- **R-13.**
  - Altering a well-formed pin's destination or tip refuses with
    `remote_pin_mismatch` in finish, freeze, accept and resume.
  - Positive test: an intake slice move followed by a freeze succeeds.
- **Regressions.** Every Plan R test still passes. Legitimate engine-authorized
  pushes of the primary and of siblings still succeed. Normal read-only Git
  operations are unchanged.
- **Test runs.** Recorded as in "Verification rules".

## Stage 3 — Compatibility and residual privileged Git execution

**Context (audit R-5 to R-12, R-14, and the coverage table):**
- **R-5.** A legacy managed run, created before pins and having a remote,
  appears unable to finish.
- **R-6.** `BASE_OVERRIDES` does not neutralize `log.showSignature`,
  `commit.gpgSign`, `tag.gpgSign`, `merge.verifySignatures` or `gpg.*program`.
- **R-7.** `status` and `diff` recurse into submodules, whose own
  configuration is never checked.
- **R-8.** A failed `git config --list` makes `_checked` run the command
  against live configuration, unchecked.
- **R-9.** A handoff for a pinned run whose pin is missing falls back to the
  unpinned read.
- **R-10.** An intake slice move re-pins a pinned run from current
  configuration.
- **R-11.** `ensure_candidate_pushed(pin=None)` pins from live configuration
  and then pushes. It has no current caller.
- **R-12.** A resume has no movement check before its first turn.
  Push-then-rewind within one turn is not seen.
- **R-14.** The push race check compares abbreviated SHAs by prefix.
- **Coverage rows with no disposition yet:**
  - direct `run-stage` and `run-sparring` turns have no after-turn check;
  - global `includeIf "hasconfig:remote.*.url"` and `onbranch:` includes.

**Outcome:**
- **R-5, legacy finish (R2-D1).**
  - `finish-run` on a legacy managed run that has a remote refuses with
    `legacy_run_needs_pin`. The message names the explicit command that pins
    it.
  - That command is `resume-plan` if it reaches a COMPLETE run. Otherwise it
    is a new `sparring pin-legacy-run --run-key <key>`, which prints the
    destination it pins.
  - After pinning, the finish works normally, including `--push-target`. A
    legacy run with no remote finishes as it does today.
  - `docs/reference.md` and the `_run_guard` docstring are corrected to match.
- **R-6, signing programs.**
  - Tests establish, against real Git with a marker script, which commands
    the engine uses run a signing or verification program from
    local or worktree scope. That covers `log --format=%H`, `reflog show` and
    the engine `commit`.
  - Only local or worktree-scope `log.showSignature`, `commit.gpgSign`,
    `tag.gpgSign`, `merge.verifySignatures` and `gpg.*program` values are
    neutralized or refused. That happens only where the marker test shows the
    program runs.
  - The user's own global signing configuration is kept. Finish merge commits
    are still signed when the user signs globally.
- **R-7, submodules.**
  - Engine `status` and `diff` pass `--ignore-submodules=dirty`, and engine
    Git runs with `submodule.recurse=false`.
  - A test with a populated submodule, whose own config sets a clean filter
    to a marker script, proves that the filter runs under plain
    `git status` and not under the engine's.
  - A second test proves that an uncommitted gitlink change is still reported
    dirty.
- **R-8.** If the configuration listing fails and `cwd` is inside a
  repository, the operation refuses with `repository_config_unreadable`.
- **R-9.** The handoff of a pinned run whose pin is missing reports
  `remote_pin_missing`.
- **R-10.** An intake slice move on a pinned run with no `remote_pin` refuses
  with `remote_pin_missing`.
- **R-11.** `ensure_candidate_pushed` requires a pin. A missing one raises
  before any Git command runs.
- **R-12:**
  - Every resume runs the movement check for the primary and every sibling
    pin, with the current `HEAD` as the candidate, before its first agent or
    reviewer turn.
  - Push-then-rewind within one turn is documented as undetectable without
    server-side logs. That is a disposition, not a fix.
- **R-14.** The race check compares full SHAs.
- **Dispositions for the coverage rows:**
  - Direct `run-stage` and `run-sparring` turns on a stage pinned under R2-D4
    get the same after-turn movement check. Turns on an unpinned stage are
    documented as unchecked.
  - Global-scope includes are the user's own trusted configuration (D4). They
    are documented as a residual risk, not refused.
- **Unchanged.** Normal read-only Git operations and engine-authorized pushes
  are unchanged, except for the overrides above.

**Starting points:**
- `managed_finish.py` (legacy guard, `candidate_not_pushed`, the remote
  steps), `engine_git.py` (`BASE_OVERRIDES`, `config_entries`, `_checked`).
- `git_context.py`, `managed_run.py` (`status` and `reflog` sites),
  `handoff.py`, `intake_branch.py`, `push_gate.py`, `plan.py` (resume entry),
  `cli.py` (`run-stage`, `run-sparring`).

**Boundaries:**
- No VS Code changes. Audit R-15 belongs to Plan D's companion extension
  plan.
- No change to the CLI exit-code contract. Every new refusal code is listed
  in `docs/reference.md`.

**Acceptance evidence.** All of it uses real disposable repositories and bare
remotes. Each negative case has a positive control.
- **R-5:**
  - A legacy-shaped managed run with a remote refuses with
    `legacy_run_needs_pin`.
  - A COMPLETE legacy run is pinned by the named command and then finishes,
    including `--push-target`.
  - A legacy run with no remote finishes unchanged.
- **R-6:**
  - The marker tests, each with a plain-Git control in which the marker does
    run without the override.
  - A globally configured signer still signs the finish merge commit.
- **R-7.** The filter-marker test with its plain-Git control, and the
  gitlink-dirty test.
- **R-8 to R-11.** One regression test each, asserting the typed refusal. No
  Git command contacts a remote in any of them.
- **R-12.** A run whose engine was killed after an agent push to the primary
  or a sibling, simulated by ending the process before the after-turn check,
  pauses on resume before any provider turn starts.
- **R-14.** A unit test with two distinct SHAs that share a prefix.
- **Regressions.** Every Plan R, Stage 1 and Stage 2 test still passes.
- **Test runs.** Recorded as in "Verification rules".

## Design decisions

- **R2-D1, legacy finish (R-5).** The finish refuses, and pinning is an
  explicit, visible human command.
  - The alternative is to pin automatically at finish.
  - Both options take the snapshot from current configuration. The choice
    rests on the human action: a person sees and confirms the destination,
    instead of the engine adopting it silently during finish.
  - **Confirm before approval.**
- **R2-D2, sibling scope (R-2).** Siblings get the full movement, receipt and
  per-commit authorization model. The alternative was to document them as
  reachability-only, outside D2.
- **R2-D3, provenance (R-3, R-4, R-13).** These fixes use records under
  `.git`, which a same-user agent can still write. They close one-step and
  accidental bypasses. They are not a boundary. Plan D owns the boundary.
- **R2-D4, hand-driven stages (R-1).** Hand-driven stages pin at creation, and
  pre-R2 stages are pinned by an explicit `pin-stage`.
  - The alternative is to refuse freeze and accept for every unpinned
    hand-driven stage, documented as unsupported. That would break the flow
    `docs/plans.md` documents.
  - **Confirm before approval.**
- **R2-D5, test account.** The isolated tests run as `sparring-agent`, the
  account the agents use, with the kit's mitigations: a fresh clone, a fresh
  uv cache, no user site, a refusal while a provider process is alive, and
  evidence copied to the owner.
  - The stronger option is a separate credential-free account for tests
    only.
  - **Confirm before approval.**
- **R2-D6, stage split.** The plan review recommends splitting Stage 2 into
  2a (R-1, R-3, R-4, R2-D4) and 2b (R-2, R-13), each with its own gate, so
  that each converges within three send-backs. This draft keeps the three
  stages requested.
  - **Confirm before approval.** The split only adds a stage and a gate pair
    to the manifest.

## After Plan R2 is accepted (human)

1. Run a fresh credential-free isolated test (`test-r2.sh <final sha>`) at the
   final exact SHA, whatever the in-run reviewer said. The completion gate
   requires this.
2. Run an independent security review of `36e2cea..<final sha>`.
3. Integrate into `main` only after both, as a deliberate human merge. No
   push is implied.
