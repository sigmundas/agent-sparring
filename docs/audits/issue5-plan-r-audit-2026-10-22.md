# Issue #5 Plan R — independent static security audit (2026-10-22)

**Range:** `2ba8d59b9daa6fcd4db11bfd560224f8ddd117cc..36e2cea12c0e301b94ce2e7528030977530d40de`
(ref `refs/review/plan-r-36e2cea`; commits `86f5065`, `3a40a73`, `796fb4b`, `7acc5e7`, `36e2cea`).
**Plan:** `docs/plans/issue5-r-complete-engine-git-authority.md` (invariants I-R1..I-R4, D2).
**Method:** static only. Code was read at the review ref and at the base, from a `git archive`
export. The VS Code extension (`agent-sparring-vscode` at `cd863e3`) was read too. **No test
was executed, and no Git behaviour was confirmed at runtime.** The in-run handoffs say the
same for the implementer and both reviewers: nobody has ever run this candidate's tests.

## Summary

Plan R mostly does what it claims for the **primary** repository of a **pinned** run:
- `engine_git.run` refuses unguarded remote commands. Pinned remote operations run in an
  isolated bare repository built from the snapshot. Remote argument forms are allow-listed.
- Includes are refused only in local and worktree scope.
- The environment strip list matches the docstring.
- A pinned flag is kept outside `.sparring/`, and the managed finish and freeze/accept fail
  closed on a missing pin.
- Acknowledgement is only a baseline. `pushed_candidate_unauthorized` and the per-commit
  `authorized_without_push` are separate records.
- The push race is detected from the porcelain old→new.
- After-turn checks run after failed, raised and interrupted turns, including review-only
  turns.

The remaining defects are at the edges:
- The unpinned `verify_pushed` read still takes its **URL and tracking ref from live
  configuration** (I-R1 violated). It is used by sibling, hand-driven and handoff paths.
- Siblings have reachability but no movement or receipt model.
- Two "explains a candidate" and "who owns this stage" inputs are agent-writable stage files.
- Legacy managed runs with a remote appear unable to finish (compatibility, D3).
- Two pre-existing local-Git execution vectors (signature programs, submodule recursion) are
  outside the diff but reachable from status and finish paths.

**Integration recommendation: integrate after fixes.** Before merging:
1. Fix R-1.
2. Decide and fix or document R-2 to R-5.
3. Run the focused tests and the full suite in a provisioned, credential-free environment, and
   record the counts against an exact SHA. The plan requires this, and it has not happened.

R-6 and R-7 are pre-existing and may be tracked separately, but must not be described as
covered.

## Findings (ranked)

Line numbers are at `36e2cea`.

### R-1 — High — unpinned remote reads follow the live URL and tracking configuration
- **Where:** `src/agent_sparring/git_context.py:286-330` (`verify_pushed`; `tracking_remote`
  at 265-283; `remote get-url` at 312); `engine_git.py:400-412` (`is_repository_transport`).
- **Callers:**
  - `acceptance.py:568`: an unpinned sibling at freeze and accept, and in `enter_review`.
  - `acceptance.py:400`: a stage no run owns.
  - `git_context.py:479`: a handoff without a pin.
- **Scenario:**
  - State: no snapshot governs the repository. `refuse_repository_transport` refuses
    credential, transport, `url.*`, `remote.*.pushurl` and `uploadpack`. It does **not**
    refuse `remote.<r>.url`, `branch.<b>.remote` or `branch.<b>.merge`.
  - Action: an agent sets `remote.origin.url=/tmp/decoy.git`, a bare repository holding the
    candidate. Or it sets `branch.X.merge=refs/heads/other`.
  - Wrong outcome: `verify_pushed` reports "pushed", and a sibling or hand-driven candidate is
    frozen or accepted though it is not on the real remote.
  - Second consequence: setting the URL to an agent-chosen `https://`/`ssh://` host makes the
    engine contact that host with the user's global credential helper and SSH configuration.
  - This violates I-R1 ("no engine Git command that contacts a remote reads the repository's
    live configuration") and the Stage 1 sibling outcome.
- **Fix:**
  - Without a snapshot, refuse while local or worktree scope sets `remote.<r>.url` or
    `branch.<b>.{remote,merge}`. The reader then needs a URL given explicitly. Better: require
    a run-start pin for every remote read of a repository that a freeze or accept depends on.
  - Add `remote.origin.url` and `branch.*.merge` to the planted-configuration matrix.

### R-2 — Medium — sibling repositories have no movement or receipt model
- **Where:** `acceptance.py:558-571` (`_sibling_pushed`); `remote_pin.verify_pinned`
  (`remote_pin.py:672-685`); `plan.py:2620-2667` (`_pin_siblings`).
- **Gap 1, no movement check:**
  - A pinned sibling is checked for reachability only. `check_movement` is never called, and
    no receipt exists.
  - Scenario: an agent pushes an unreviewed sibling commit to the sibling's pinned ref. The
    issue #5 case, in the sibling repository.
  - Wrong outcome: nothing pauses. Freeze treats reachability as "pushed", which D2 and the
    push-gate docstring say must never be success.
- **Gap 2, silent fallback:**
  - Removing `sibling_pins` from the agent-writable run state silently falls back to the
    unpinned R-1 read.
  - There is no outside-`.sparring` flag for siblings, so this is not `remote_pin_missing`.
  - A sibling that is not a repository at run start, or has no URL then, is also permanently
    unpinned.
- **Fix:**
  - Either extend `check_movement` and receipts to sibling pins, or document plainly that
    sibling remotes are reachability-only and outside D2.
  - Record the sibling pin set (paths and branches) in the outside-`.sparring` engine record,
    and refuse when it is missing.

### R-3 — Medium — the stage base comes from agent-writable `state.json` and explains a candidate
- **Where:** `remote_pin.py:593-595` (`_covered`); `plan.py:2562` and `plan.py:2772`;
  `acceptance.py:882`, `1044` and `1198` (`state.base_sha`).
- **Scenario:**
  1. An agent pushes C to the pinned ref.
  2. The run pauses with `unauthorized_remote_movement`.
  3. A human acknowledges it. The baseline is now C.
  4. The agent sets `base_sha` to C in `.sparring/stages/<id>/state.json`.
- **Wrong outcome:**
  - `_covered` is true, because C is an ancestor of the stage base.
  - The required `pushed_candidate_unauthorized` pause never happens, so D2's per-commit
    authorization is bypassed with one JSON edit.
  - The README for D2 lists the stage base as an explanation, but its provenance must be
    engine-recorded.
- **Fix:**
  - Take the stage base from the plan-run state, where the engine records it at stage entry.
  - Or count the stage base only when it is itself covered by a receipt or a non-legacy start
    tip. Then it adds nothing that is unsafe.
  - Note: the run state is also agent-writable (see Plan D), so this only raises the bar
    against accidental or one-step tampering.

### R-4 — Medium — stage ownership is derived only from agent-writable `.sparring/` files
- **Where:** `acceptance.py:244-294` (`_runs_positioned_at`, `_owning_run`).
- **Scenario:**
  - Action: an agent clears `run` in the stage's `state.json`. It also changes
    `current_stage` in `.sparring/plans/<run>.json`, or deletes that file.
  - Result: `_owning_run` returns `None`, and the stage is treated as hand-driven.
    `_require_explained` is skipped, and `_pushed` uses the unpinned `verify_pushed`
    (`acceptance.py:400`), which R-1 makes falsifiable.
  - I-R2 covers "pin missing" but not "owner missing". For a managed worktree, the engine
    already has an outside-`.sparring` record that names the run (`record_for_worktree`).
- **Fix:**
  - When `repo_root` is a managed worktree, take the owner from its managed record.
  - For unmanaged runs, record stage claims in `<git-common-dir>/agent-sparring/runs/`.
  - Refuse when the outside record names an owner and `.sparring` does not.

### R-5 — Medium (compatibility, D3) — legacy managed runs with a remote appear unable to finish
- **Where:**
  - `managed_finish.py:1084`: the legacy `nullcontext()`.
  - `managed_finish.py:140-163`: `candidate_not_pushed` is "remote state not checked: run has
    no pin".
  - `managed_finish.py:1376` and `managed_finish.py:1653`: `record.remote` is used by name, and
    `engine_git.run` refuses it as `unguarded_remote_operation`.
- **Scenario:** a pre-pin managed run whose record has a remote. Running `finish-run`:
  - always fails `candidate_not_pushed`, and the check is probably blocking;
  - and `--push-target` and the remote-branch deletion always refuse.
- **Contradiction:** `_run_guard`'s docstring and the plan say legacy runs still finish
  "under the hardened Git path alone".
- **Not verified:** whether `candidate_not_pushed` blocks the merge step.
- **Fix:** pin a legacy managed run on finish with the same visible notice that resume uses.
  Or document that legacy runs must be resumed once, which pins them, before they can finish.

### R-6 — Medium (pre-existing, outside the diff; suspected) — signing and verification programs are not neutralized
- **Where:** `engine_git.py:104-109` (`BASE_OVERRIDES`). Call sites:
  - `managed_finish.py:1569` and `1573` (`log`);
  - `managed_run.py:694` (`reflog show`);
  - `managed_finish.py:1603` (`commit`).
- **Scenario:**
  - Plant: `log.showSignature=true`, `commit.gpgSign=true` or `merge.verifySignatures=true`,
    together with `gpg.program`/`gpg.ssh.program` pointing at a script.
  - Effect: `gpg.*` is only *compared* inside a guard. Outside one — legacy or unpinned
    finish, status reads, handoff — a `log`/`show` of a commit carrying a `gpgsig` header,
    or an engine `commit`, would run the program as the user.
  - Inside a guard, planting only the `*.gpgSign`/`showSignature` switch still runs the
    user's own signer, which may raise a pinentry prompt.
  - Not confirmed at runtime: whether a `--format=%H` log triggers verification.
- **Fix:**
  - Add `log.showSignature=false`, `commit.gpgSign=false`, `tag.gpgSign=false` and
    `merge.verifySignatures=false` to `BASE_OVERRIDES`.
  - Refuse or override `gpg.*program` outside a guard.

### R-7 — Medium (pre-existing, suspected) — submodule recursion escapes the local hardening
- **Where:** `managed_finish.py:101`, `git_context.py:209` and `managed_run.py:208,830`
  (`status`); the `diff` call sites (`git_context.py:154,434`).
- **Scenario:**
  1. An agent adds a gitlink with a populated submodule.
  2. It sets `filter.x.clean=<script>` in `.git/modules/<n>/config` and `* filter=x` in the
     submodule's `.gitattributes`.
  3. `git status` in the superproject spawns `git status` in the submodule. `-c` values
     propagate there, but the filter overrides name only the superproject's filters. The
     include refusal and the snapshot comparison also never see the submodule's config.
  4. The clean filter runs when a file's stat information is stale. `diff.submodule=diff`
     (not watched) has a similar reach for `diff`.
- **Where it runs:** status polling, freeze and accept dirty checks, and handoff.
- **Fix:** pass `--ignore-submodules=all` to `status` and `diff`, or set
  `diff.ignoreSubmodules=all` and `diff.submodule=short` as base overrides. Or refuse while
  any gitlink is checked out.

### R-8 — Low — a configuration-listing failure fails open for local commands
- **Where:** `engine_git.py:529-536` and `engine_git.py:662`.
- **Scenario:** `config_entries` returns `None` when `git config --list` fails. `_checked`
  then skips the include refusal and the snapshot comparison, and `run` executes the command
  directly against live configuration with no isolation.
- **Why it matters:** the claim that "the real command would then fail the same way" is an
  assumption, not a check. Any input where the listing fails but the command proceeds — an
  unusual include target, say — bypasses every repository check.
- **Fix:** when the listing fails and `cwd` is inside a repository, return a typed refusal.

### R-9 — Low — handoff does not fail closed on a missing pin
- **Where:** `handoff.py:219-223`.
- **Scenario:** the pinned run's state file is deleted, or its `remote_pin` removed. `pin` is
  then `None`, and the handoff's "Pushed" line comes from the unpinned `verify_pushed`,
  which R-1 shows can be falsified.
- **Effect:** the handoff is evidence for the reviewer, so a false "pushed" there misleads
  review.
- **Fix:** if `engine_says_pinned` is true, report `remote_pin_missing`.

### R-10 — Low — an intake slice move re-pins a run from current configuration
- **Where:** `intake_branch.py:467-473`.
- **Scenario:** a run whose engine record says it is pinned, but whose `remote_pin` is `None`,
  is re-pinned through `pin_remote` from current configuration. Its snapshot then includes
  anything planted since.
- **Contradiction:** I-R2 and `_pin_legacy_run` ("not re-pinned from the current
  configuration"). Mitigated: the move refuses once any provider turn has run.
- **Fix:** check `engine_says_pinned` there, and refuse with `remote_pin_missing`.

### R-11 — Low (latent) — `ensure_candidate_pushed(pin=None)` pins from live configuration and then pushes
- **Where:** `push_gate.py:537-538`.
- **Scenario:** a caller passes no pin. The snapshot is captured *now*, including planted
  credential or `sshCommand` keys, and it is then used for an authorized push.
- **Reachability:** no current caller passes `None` (`plan.py:2566` passes
  `state.remote_pin`, which start and resume always set).
- **Fix:** make `pin` required, or raise when it is missing.

### R-12 — Low — a resume has no pre-turn movement check, and the check watches only the pinned tip
- **Where:** `plan.py:2756-2799`, `plan.py:4469` and later (`_drive_pinned`); `loop.py:245-270`.
- **Gap 1, resume:**
  - `checked_turn` covers raised and interrupted turns. If the engine is killed (SIGKILL, a
    crash or power loss), or the failed-turn check could not be made, nothing is recorded.
  - Resume then enters an agent turn with no movement check first. The check after that turn
    catches the movement only if it is still there.
- **Gap 2, push then rewind:** the after-turn check passes no candidate and compares only the
  tip. An agent that pushes and then rewinds the ref within one turn exposes an unreviewed
  commit undetected.
- **Fix:**
  - Run `_movement_now` (with the current HEAD as the candidate) before the first turn of
    every resume.
  - Document push-then-rewind as undetectable without server-side logs.

### R-13 — Info — the finish guard trusts whichever readable pin it finds first
- **Where:** `managed_finish.py:1030-1084`.
- **Detail:** the worktree copy is preferred over the archive copy. Neither is cross-checked,
  and no digest exists outside `.sparring`.
- **Fix (cheap hardening):** record a digest of the run-start pin in the managed record or the
  run flag file, and refuse on mismatch. See also Plan D.

### R-14 — Info — the race check compares abbreviated SHAs by prefix
- **Where:** `push_gate.py:430-467`.
- **Detail:** this is theoretical only.
- **Fix:** request full SHAs (`-c core.abbrev=40` on the push), or compare against the
  re-read `landed` tip.

### R-15 — Info — the VS Code extension runs raw `git`
- **Where:**
  - `agent-sparring-vscode/src/vscode/worktreeProbe.ts:44` (`worktree list --porcelain`,
    polled);
  - `core/repositoryMapping.ts:17` (`rev-parse`, `symbolic-ref`);
  - `core/featureBranch.ts:41`.
- **Detail:**
  - None of these currently runs a configured program.
  - The engine commands the extension polls are `runs --json` every refresh
    (`isolatedRunsProbe.ts:122`) and `slice-branch --json`. The `runs` poll contacts the
    pinned remote with `ls-remote` on every poll.
  - Any future raw `git status`/`diff` in the extension would bypass all of the engine's
    hardening.

## Coverage by focus area

| # | Area | Result |
|---|---|---|
| 1 | Planted Git config | **Primary, pinned:** sound. Remote ops never read repository config: isolated bare repository plus a global/system copy without `url.*`/includes. Local ops refuse local and worktree includes, compare watched keys inside a guard, and run under `isolated_config` with worktree config merged and `extensions.worktreeConfig` dropped. Hooks, fsmonitor, textconv, external diff and filters are neutralized. The `GIT_*` strip matches the docstring. Remote forms are allow-listed (`--repo`, `--mirror`, `--receive-pack`, `-c url.*` refused). **Gaps:** R-1 (`remote.*.url`, `branch.*`), R-6 (signing programs), R-7 (submodules), R-8 (listing failure). **VS Code:** `runs --json` reads through the pin, or reports "not checked", and no finding there beyond R-15 and the Plan D note about a forged pin. |
| 2 | Remote call sites at head | Every `ls-remote`/`push` goes through `engine_git.run` → `run_remote`, or calls `run_remote` directly: `push_gate.py:582`; `git_context.py:319,357`; `managed_finish.py:123,663,1396,1670`. No raw `subprocess` git remains outside `engine_git`; the `codex_cli.py:167` subprocess is `codex debug models`, not git. **Bypass of pin validation:** R-1 (the unpinned `verify_pushed` URL). Non-local push destinations are not compared to the pin inside `run_remote`; callers pass `pin.destination` (`push_gate`) or `_destination` (finish). **Siblings:** R-2. **Handoff:** R-9. **Managed finish:** guarded with the pin as the destination; legacy refused (R-5). |
| 3 | Missing, altered or unreadable pins | Missing or unreadable with an outside flag: fail-closed in finish (`_run_guard`), freeze/accept (`_run_remote`) and resume (`_pin_legacy_run`); status reports "not checked". **Not fail-closed:** handoff (R-9), intake move (R-10), sibling pins (R-2). **Altered pin:** accepted as is (R-13, Plan D). The flag file treats unreadable as pinned and `{"pinned": false}` as unpinned (`managed_run.py:474-486`); the agent can write it (Plan D). |
| 4 | Acknowledgement vs authorization vs acceptance | **No finding** in code paths. Acknowledgement only sets `expected_tip` (the baseline); `_covered` never counts it (`remote_pin.py:573-595`). `authorized_without_push` is a separate record for one exact SHA, and is refused unless the run is paused on that exact SHA. A run-level grant cannot clear the pause (`plan.py:2500-2510`, `_drive`). Acceptance still requires READY for the SHA; `36e2cea` adds a SEND_BACK regression, unexecuted. **Caveat:** R-3 (the stage base side door). |
| 5 | After-turn detection | Stage and sparring turns sit in `checked_turn`, which catches `BaseException`. Review-only turns are checked in the failure path (`plan.py:4727-4745`) and after success (`plan.py:4748-4766`). A `SEND_BACK` routes through a normal stage turn, so it is checked. Resume of a recorded pause stays paused (`_drive`). **Gaps:** R-12 (killed engine or failed check followed by resume; push-then-rewind; direct `run-stage`/`run-sparring` CLI turns at `cli.py:1141,1287` have no after-turn check, though freeze/accept still check). |
| 6 | Legacy and includes | Global and system includes are resolved by Git and copied expanded; `includeIf gitdir:` works. `hasconfig:remote.*.url` and `onbranch:` in the *global* config resolve against agent-writable repository state, so the agent can choose which of the user's own global blocks apply (Info). Old state without pins: resume pins with a notice; freeze/accept refuse until then. **Legacy managed finish:** R-5. An old engine reading a record with `pinned` was not checked (downgrade). |
| 7 | Tests | See below. |

## Tests

**What real Git covers.** The new tests (`tests/test_no_live_remote_config.py`,
`tests/test_fail_closed_pins.py`, and additions to `tests/test_push_authorization.py`) use:
- real temporary repositories, real bare remotes, and marker scripts;
- positive controls with plain `git`;
- `mock.patch` only for the push-race seam (patching `engine_git.run_remote`) and for
  providers.

The race seam therefore has no production hook, which satisfies the plan's "no configuration,
environment variable or CLI flag can enable it".

**Weaknesses:**
- **The planted matrix proves the snapshot comparison, not the isolation.**
  - Planting happens after pinning, so every status, handoff and sibling refusal is
    `repository_config_changed` or `repository_transport_config`, raised *before* any transport.
  - The `run_remote` isolation, the defence when configuration changes between check and use,
    is not exercised by those tests.
  - The `core.sshCommand` and `credential.helper` "no marker" assertions remain vacuous: the
    engine only ever talks to local bare remotes, which never invoke either. The 2026-10-18
    audit raised the same point.
- **Invariants without a real-Git test:**
  - R-1: a `remote.origin.url` or `branch.*.merge` redirect of `verify_pushed`;
  - sibling movement after the pin (R-2);
  - `base_sha` tampering (R-3);
  - an owner rewrite that touches both files (R-4; only the emptied `run` field is tested);
  - finish of a legacy managed run with a remote (R-5);
  - gpg and submodule vectors (R-6, R-7);
  - a configuration-listing failure (R-8);
  - a missing pin in the handoff (R-9);
  - resume after a hard kill (R-12);
  - an SSH or HTTPS remote, as opposed to a file remote, in any authority test.
- **Never executed.** Per both handoffs and both READY verdicts, no focused test and no full
  suite has ever run on `86f5065`..`36e2cea`. The plan's prerequisite (`d1d48d0` suite counts)
  and its "Test runs" acceptance evidence are unmet. The SEND_BACK-after-authorization test's
  reachability was "traced by reading the code".

## Residual risks for Plan D

These are not Plan R defects. They cannot be prevented while agents run as the same account
with write access to the worktree, `.git` and the user's home directory:

- **Engine authority in agent-writable state.** `.sparring/plans/<run>.json` holds
  `remote_pin` (`trusted_config`, `resolved`), `remote_records`, `awaiting` and
  `sibling_pins`. An agent that forges `trusted_config` and the matching `.git/config` can do
  either of the following:
  - add `core.sshCommand`, `credential.helper=!…` or `protocol.ext.allow`, and make the
    engine run it on the next status poll (`runs --json` from VS Code every refresh) or on the
    next push;
  - forge `resolved` to another destination.
  Once Plan A confines agents, this is a sandbox escape through the unconfined engine, so the
  state must move out of the agent's write authority (D4).
- **The pinned flag in `<git-common-dir>/agent-sparring/`.** The managed record and
  `runs/<key>.json` sit inside `.git`, which the agent writes. Deleting them, or writing
  `{"pinned": false}`, turns a pinned run into a legacy one.
- **Repository redirection.** The worktree's `.git` file and the git directory's `commondir`
  are agent-writable. They can point the engine's local Git at a decoy repository or common
  directory, with different refs or no flag.
- **The human-only actions are not human-only.** The agent can run
  `sparring acknowledge-remote-movement` and `sparring authorize-pushed-candidate` itself,
  and `by=who()` records the same OS user.
- **User-scope configuration.** `~/.gitconfig` and its includes, and SSH and credential
  configuration, are trusted by design and writable by the agent.
- **Pinned local-path remotes.** Their own receive hooks and configuration run on the
  engine's push, as documented. The agent can write those too.
- **What the detection cannot see.** It watches one ref. Pushes to other refs, tags or
  remotes, and push-then-rewind, are not seen.
- **VS Code's own Git.** VS Code's built-in Git extension runs `git status` (fsmonitor, hooks
  for commits) in the agent's worktree outside agent-sparring entirely.

## Not verified

- **No tests were executed** — not focused, not the full suite. No Git command was run against
  the candidate. Every behavioural statement here comes from reading the code.
- Whether `candidate_not_pushed` blocks the merge step (R-5).
- Whether `git log --format=%H` or `reflog show` trigger signature verification under
  `log.showSignature` (R-6).
- The exact submodule child-process behaviour (R-7).
- Git's porcelain output for up-to-date refs without `--verbose` (R-14).
- Whether `slice-branch --json` reaches any remote read (not traced).
- Downgrade behaviour of older engines reading `pinned`.
- macOS-specific behaviour of `tempfile` and symlinks in `isolated_config`.
