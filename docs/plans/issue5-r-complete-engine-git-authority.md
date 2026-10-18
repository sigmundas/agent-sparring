# Complete the engine Git authority (issue #5, Plan R)

This plan finishes Stage 1 of `docs/plans/secure-agent-git-writes.md`, which
the bootstrap run accepted at `d1d48d0`. The audit is in
`docs/audits/issue5-recovery-2026-10-18.md` §1. It found that some engine remote
operations still follow live repository configuration, and that the finish path
fails open when the run state is absent.

**Base.** `d1d48d0`, the Stage 1 endpoint. Do not include any Stage 2 commit.

**Run with the frozen runner.** Run this plan with the frozen bootstrap runner
(`/Users/Shared/sparring-bootstrap/runner`) under the credential-free account.
This plan changes the engine that would otherwise supervise it.

**Prerequisite (human).**
- Run `d1d48d0`'s full suite (`.venv/bin/python -m pytest -q`) in a
  credential-free environment, and record the counts in the run notes. A failure
  there is fixed in Stage R1 before anything else.
- Decision D2 (acknowledgement semantics) is recorded in this file before
  Stage R2 starts. The recommended default is written into Stage R2.

**Threat model.** The threat model is unchanged from the original plan. Plan R
improves the engine's own authority. Without agent confinement (Plan A),
nothing here protects against an active agent, and no output may say so.

## Security invariants (every later refactor must keep these)

- **I-R1.** No engine Git command that contacts a remote reads the
  repository's live configuration. It uses a pinned destination and a trusted
  snapshot, or it refuses with a typed reason. This holds with or without an
  active run guard.
- **I-R2.** A run that was created pinned can never be finished, frozen or
  accepted unpinned. The absence of a pin is evidence of tampering, not of a
  legacy run, unless an engine record outside `.sparring/` says the run
  predates pins.
- **I-R3.** Reachability, push authorization, acknowledgement, acceptance and
  release are distinct records. No one of them implies another.
- **I-R4.** An engine push receipt records only movement the engine itself
  caused, as observed in that push's own old→new result.

## Stage 1 — No engine remote operation follows live configuration

**Context.** Audit findings S1-1, S1-2, S1-6, S1-8, S1-9 and S1-10.
- `engine_git.run` (`engine_git.py:476-480`) sends a `REMOTE_COMMANDS` call
  through `run_remote` only when a run guard governs `cwd`. Otherwise it runs
  against live configuration.
- These paths reach that fallback:
  - `managed_finish.finish_status` and `_remote_contains`, through
    `runs_report` (`sparring runs --json`, which VS Code polls) and
    `finish-run --dry-run`;
  - sibling-repository `verify_pushed` in `acceptance.py:510`;
  - `git_context.py:299-302`;
  - `handoff.py:213`.

**Outcome.**
- **No unguarded remote call.** `engine_git.run` refuses a `REMOTE_COMMANDS`
  call that has neither an active guard nor an explicit trusted snapshot and
  pinned URL. The refusal is typed (`unguarded_remote_operation`) and names the
  caller's subcommand.
- **Status reads use the pin.** `finish_status`/`runs_report` and finish dry-run
  read remotes through the run's pin and snapshot.
- **Records without a pin.** A record that has no pin reports
  "remote state not checked: run has no pin". It never contacts the remote.
- **Siblings.**
  - Declared sibling repositories are snapshotted at run start, with the same
    `trusted_config` shape as the primary.
  - Sibling remote reads use their own snapshot.
  - Where no snapshot exists, a sibling remote read refuses any
    repository-scope transport or credential key.
- **Live-config commands are isolated.** Checkout-performing commands that run
  with live configuration (`worktree add`, and any switch or checkout) run under
  `isolated_config`.
- **Includes.** `include`/`includeIf` are refused only in local and worktree
  scope. Global and system includes are resolved into expanded entries, as
  `run_remote` already does, so `includeIf "gitdir:"` in `~/.gitconfig` keeps
  working.
- **Inherited environment.** `_environment` strips `GIT_SSH`, `GIT_SSH_COMMAND`,
  `GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`,
  `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`,
  `GIT_TEMPLATE_DIR`, `GIT_EDITOR`, `GIT_CONFIG_PARAMETERS` and
  `GIT_CONFIG_COUNT`/`KEY_*`/`VALUE_*`. The docstring states exactly this list.
- **Local-path remotes.** Pinned local-path remotes are documented in
  `docs/plans.md` "Engine Git": their own receive hooks run. An unpinned
  file-path destination is refused.

**Starting points.** `engine_git.py` (`run`, `run_remote`, `_environment`,
`_checked`, `_LIVE_COMMANDS`), `managed_finish.py` (`finish_status`,
`_remote_contains`, `_destination`, `runs_report`), `acceptance.py`,
`git_context.py` (`verify_pushed`), `handoff.py`, `remote_pin.py`
(`trusted_config`), `plan.py` (start-plan pinning). Enumerate remote call sites
with `rg -n "ls-remote|fetch|push|REMOTE_COMMANDS" src/agent_sparring`.

**Boundaries.**
- No provider, sandbox or network-policy changes.
- No changes to the push, receipt or acknowledgement semantics (that is
  Stage 2).
- Disposable local remotes only.

**Acceptance evidence.** Use real disposable bare remotes and marker files. Do
not use mocks of `engine_git`.
- **Planted-configuration matrix.**
  - Planted items, one per test:
    - `core.sshCommand`;
    - `credential.helper=!<marker>`;
    - `remote.origin.uploadpack=<marker>`;
    - `url.<x>.insteadOf` pointing to a second bare repository;
    - `remote.origin.pushurl`.
  - Each is planted in the repository config and, separately, in
    `config.worktree`.
  - Then run, with no guard active:
    - `sparring runs --json`;
    - `finish-run --dry-run`;
    - `handoff`;
    - freeze/accept with a declared sibling.
  - Expected: no marker file appears, the second bare repository receives no
    contact, and each call returns the typed refusal or the pinned result.
  - For the uploadpack case the remote must be a local-path remote, so that the
    program would really run if it were consulted. A marker test for
    `uploadpack` that never reaches the transport is not evidence.
- **Positive controls.** The same tests show that each planted program *does*
  run when invoked with plain `git` outside the engine, proving the marker
  mechanism works.
- **Global include.** A global `includeIf "gitdir:<repo>"` in a test `HOME`
  pointing at a harmless file: engine status, diff and an authorized push all
  succeed. A repository-scope include is still refused.
- **Environment.** With `GIT_SSH_COMMAND=<marker>` and
  `GIT_CONFIG_PARAMETERS='credential.helper=!<marker>'` set in the engine's
  environment, an authorized push leaves no marker.
- **Test runs.** Focused tests pass, then `.venv/bin/python -m pytest -q`. The
  report gives the counts and the candidate SHA.

## Stage 2 — Fail closed on missing pins; acknowledgement is not authorization

**Context.** Audit findings S1-3, S1-4, S1-5, S1-7 and S1-11.
- `managed_finish._run_guard` returns `nullcontext()` when the state file is
  absent or has no pin.
- Freeze/accept read ownership from the stage's own `state.json`. They fall
  back to reachability for unpinned legacy runs.
- `remote_pin._covered` counts acknowledged tips. `test_push_authorization.py`
  (`test_pushing_it_yourself_is_unexplained_movement_until_acknowledged`)
  asserts that an acknowledged candidate is then accepted with no push
  authorization.

**Decision D2.** The recommended default, used unless this section is amended
before the run:
- An acknowledgement records that a person observed a remote tip. It clears
  the movement pause only.
- A candidate is "explained" only by an engine receipt, the pinned start tip or
  the stage base.
- After an acknowledgement, the run pauses on the normal push-authorization
  pause for that SHA. The authorization is recorded as
  `authorized_without_push`, because the SHA is already on the remote.
- A run-level push grant does **not** satisfy it. It needs a per-SHA decision.

**Outcome.**
- **Pinned flag outside `.sparring/`.**
  - The managed-run record in `<git-common-dir>/agent-sparring/` gains
    `pinned: true` for every run created after this change.
  - Unmanaged runs record the same flag in an engine file under
    `<git-common-dir>/agent-sparring/runs/<run key>.json`. This is the
    pre-Plan-A location, and Plan A protects it.
  - A run so flagged whose pin is missing or unreadable refuses
    finish/freeze/accept with `remote_pin_missing`.
  - The unguarded finish path remains only for runs whose engine record proves
    they predate pins.
- **Ownership.** Freeze/accept of a stage resolve ownership from engine run
  state. Freeze/accept of an unpinned legacy run either pins it first, with the
  existing visible notice, or refuses. They never fall back to reachability.
- **D2 is implemented as written above.** `test_push_authorization.py` is
  updated to assert the new behavior. Do not delete the test.
- **Push race.** An engine push compares the push's porcelain old→new with
  the observed tip. If they differ, or the push is a no-op, it records
  `unauthorized_remote_movement` instead of a receipt.
- **After-turn check.** `after_turn` movement checks run in a `finally` after
  a turn that raises or times out.

**Starting points.** `managed_finish.py` (`_run_guard`, `_pinned_finish`),
`managed_run.py` (record schema), `acceptance.py` (`freeze_candidate`,
`_accept_*_locked`), `remote_pin.py` (`_covered`, `check_movement`,
`acknowledge_remote_movement`), `push_gate.py`, `loop.py`, `plan.py`, `cli.py`
(acknowledge and resume flags), `docs/plans.md` ("Remote movement the engine did
not make").

**Boundaries.**
- No agent confinement.
- No VS Code changes. Note the new pause and the `authorized_without_push`
  rendering as a follow-up.

**Acceptance evidence.**
- **Pinned run, pin removed.**
  - Setup: a pinned managed run.
  - Each case runs separately:
    - the state file deleted;
    - `remote_pin` deleted from it;
    - the state file replaced with a legacy-shaped one.
  - Each case runs `finish-run --push-target` with a planted `pushurl`.
  - Expected: refused with `remote_pin_missing`. Nothing is merged, pushed or
    deleted (all refs of both remotes are compared before and after).
- **Unpinned legacy run.** `accept-candidate` on an unpinned legacy run whose
  candidate is already on the remote either pins it and pauses on
  `unauthorized_remote_movement`, or refuses. It never accepts.
- **D2 sequence.** Pause, then acknowledge. Then:
  - the run pauses for the per-SHA push authorization;
  - a run-level grant does not clear the pause;
  - a per-SHA authorization records `authorized_without_push`, followed by
    acceptance;
  - an acknowledgement of a tip ahead of the candidate does not explain a
    later, different candidate.
- **Race.** The test pushes between `check_movement` and the engine push, by
  inserting the agent push through a test hook in the real push path. It covers
  both a fast-forward to the same SHA and an ancestor push. Expected: the result
  is movement, not a receipt.
- **Failed turn.** A turn that raises after an out-of-band push, followed by
  resume: the movement pause comes before any stage-agent turn.
- **Test runs.** The focused tests and the full suite pass, with counts and the
  candidate SHA in the report.

## After Plan R is accepted (human)

- **Merge.** Merge Stage 1 + Plan R to `main` as one reviewed integration.
  Describe it as "engine authority and detection; no protection against an
  active agent until Plan A".
- **Plan A.** Plan A starts from that `main`.
