# Local-first engine (issue #5, Plan L)

**Decision (the user, 2026-10-22).** Agent Sparring is local-first.
Implementation, review, acceptance and `finish-run` work entirely in the local
repository. Neither agents nor the engine push to GitHub or GitLab as part of
normal work.
- Publishing a commit for external review becomes a separate, explicit,
  human-triggered action, **Push to review**. It never implies acceptance.
- The original reason for automatic pushing was to make candidates visible to
  a web-chat reviewer. That need is met by Push to review.

**This plan replaces Plan R2**
(`docs/plans/issue5-r2-complete-and-verify-engine-git-authority.md`, at
`d658eef`, with its kit at `d7f672d`).
- Plan R2 is kept unchanged, for reference. Its Stage 1 is reused here as
  Stage 1, narrowed.
- Its Stages 2 and 3 are re-scoped below. Most of what they protected is the
  engine's own automatic remote authority, which this plan removes.

**Inputs:**
- Plan R: `docs/plans/issue5-r-complete-engine-git-authority.md` (base
  `36e2cea`).
- The Plan R audit: `docs/audits/issue5-plan-r-audit-2026-10-22.md`
  (findings R-1 to R-15).
- The isolated test run of `36e2cea`: focused 381 tests, 1 failure; full 2071
  tests, 7 failures. The evidence is in
  `/Users/Shared/sparring-recovery-r2/evidence/test-r-36e2cea-20261022T174121Z/`.

**Base.** `plan/issue5-local-first`: `36e2cea` plus planning documents only.
Plan R's local Git hardening is kept, and so is its remote-operation
machinery, which publication reuses.

**Separate from:**
- **Plan D.** This is the real boundary between agents and the user's
  credentials and trusted engine state. A `git push` deny rule in Claude
  settings is a workflow setting, not an OS boundary. Until Plan D, a
  same-user agent can still run `git push` with the user's credentials, or
  any `sparring` command.
- **Plan 2** (required verification evidence), which stays in the backlog.
  Until it exists, mandatory tests are enforced by this run's human gates.
- **Plans A to C.** No provider launch, sandbox or credential-guarantee work.
- **The VS Code extension.** It lives in another repository. Its companion
  plan is described under "Effects", and is not part of this plan.

## Invariants (every later refactor must keep these)

- **I-L1. No remote contact in normal work.** None of these runs a Git command
  that contacts a remote:
  - `start-plan`, `run-plan`, `resume-plan`;
  - `run-stage`, `run-sparring`, `run-loop`;
  - `freeze-candidate`, `accept-candidate`;
  - review, the handoff;
  - `runs`/status polling;
  - `slice-branch`;
  - `finish-run` without a publication flag.

  The remote commands are `push`, `fetch`, `pull`, `ls-remote`,
  `remote update`, `remote show`, a `clone` or `submodule update` from a URL,
  and any command given a URL. `engine_git` refuses each of them, with
  `remote_operation_not_allowed`, unless it runs inside an explicit
  publication context. That context is created only by the publication
  command (Stage 4) and by the legacy compatibility path (Stage 3).
- **I-L2. Acceptance is local.** Freeze and accept require:
  - the exact candidate commit on the stage's or run's branch in the local
    repository;
  - a working tree clean apart from the engine's own artifacts;
  - for accept, the reviewer's verdict at that exact SHA.

  Remote state is never an input to freeze, accept or finish.
- **I-L3. Publication and acceptance are separate.**
  - Publication never changes stage, run or acceptance state.
  - Acceptance never requires publication.
  - A publication receipt is stored apart from run and stage state. No freeze,
    accept, review or finish code reads it. A test asserts that by importing.
- **I-L4. Publication is explicit and exact.** A publish runs only with a
  confirmation token from its own preview. That token binds:
  - the commit SHA;
  - the repository path;
  - the remote name and URL;
  - the destination ref;
  - the expected current tip of the destination;
  - the trusted configuration snapshot.

  It refuses if any of these has changed. The token protects against
  accidents and drift. It is not a boundary against a same-user agent
  (Plan D).
- **I-L5. Local Git stays hardened.**
  - Every engine Git command, read-only ones included, keeps Plan R's local
    protections:
    - base overrides;
    - the include refusal;
    - the trusted-snapshot comparison in a guard;
    - the environment strip;
    - no hooks and no fsmonitor;
    - the remote-form allowlist.
  - Stage 5 adds signing programs, submodule recursion and the
    configuration-listing failure.
  - A repository's own configuration never makes an engine Git command run a
    helper, hook, filter, signing tool or other program.
- **I-L6. Mandatory tests run at the reviewed SHA.** "Deferred" or "could not
  run" never satisfies a mandatory test.

## What becomes of Plan R's changes

| Plan R change (at `36e2cea`) | Under local-first |
|---|---|
| `engine_git` local hardening (overrides, include refusal, snapshot comparison, environment strip) | **Kept.** I-L5. |
| `engine_git` remote machinery (refusing unguarded remote commands, isolated bare repository for remote operations, allowlisted forms, `unpinned_local_remote`) | **Kept, narrowed.** Used only inside a publication context. |
| `remote_pin` snapshot of remote, URL and trusted configuration | **Kept, as the publication snapshot.** Taken at the publish preview and bound to its token, instead of at run start. |
| Run-start pins, sibling pins, `engine_says_pinned` flag, pin-missing refusals | **Unnecessary for new runs.** Kept read-only for legacy runs (Stage 3). |
| Per-turn remote movement checks, push receipts, `unauthorized_remote_movement` / `pushed_candidate_unauthorized` pauses | **Unnecessary in normal work.** They needed `ls-remote` after every turn and detected only one ref. Plan D prevents instead. Existing pauses in legacy runs stay answerable (Stage 3). |
| `acknowledge-remote-movement`, `authorize-pushed-candidate` | **Legacy only.** They stay for paused legacy runs, and are documented as deprecated. |
| `push_gate.ensure_candidate_pushed`, `--allow-push-for-run`, the `push_authorization_required` pause | **Removed from runs.** The push half is reused by publication. |
| `finish-run --push-target`, `[finish] delete_remote_branch` | **Removed from finish.** It refuses with a pointer to publication. A deliberate legacy path exists (Stage 3). |
| Failed-turn checking (`checked_turn`) | **Kept for local integrity checks**, for example an unexpected branch or `HEAD` movement. Remote movement is dropped. |

**Audit findings under local-first:**

| Finding | Disposition |
|---|---|
| R-1 live-configuration remote reads | **Removed with the reads.** Freeze, accept and handoff no longer read remotes. Publication reads only its snapshot. |
| R-2 siblings | **Moot.** Siblings are local, like the primary. |
| R-3 `base_sha` explains a candidate, R-4 ownership from `.sparring/`, R-13 pin digest | **Moot for authority.** No remote authority remains to bypass. R-4's ownership question stays a local-integrity issue (see Stage 2). |
| R-5 legacy finish | **Stage 3.** |
| R-6 signing, R-7 submodules, R-8 listing failure | **Stage 5.** Still applicable: they are local, privileged Git execution. |
| R-9, R-10, R-11 | **Moot.** The code paths are removed or become legacy-only and read-only. |
| R-12 resume and push-then-rewind | **Moot for normal work.** It is a Plan D residual. |
| R-14 abbreviated SHA comparison | **Stage 4** (publication's lease check). |
| R-15 VS Code raw `git` | **Companion extension plan.** |

## The seven failing tests

None is deleted. Each is repaired, retargeted or reclassified, and the
handoff of each stage records what happened to it.

| Test (at `36e2cea`) | Status under local-first | Stage |
|---|---|---|
| `HardenedGitTests::test_remote_operations_never_read_the_repositorys_own_configuration` | **Applicable, publication path.** A planted `pushInsteadOf` must never redirect a publish. It is repaired so the property is reached, then becomes a publication test. | 1, then 4 |
| `UnguardedRemoteTests::test_push_forms_that_hide_the_destination_are_refused` | **Applicable, publication path.** The forms that hide the destination stay refused. The `--repo` control is replaced (Git 2.53.0: `--repo X <refspec>` never contacts X; `--repo X --mirror` does). | 1, then 4 |
| `FinishWithoutThePinTests::test_the_pin_malformed` | **The default finish no longer uses pins.** The exit code is fixed to `3` first, so the security assertions run. Then it is retargeted: a legacy remote finish, and a publish, with a malformed record refuse and change no ref. | 1, then 3 |
| `FinishWithoutThePinTests::test_the_pin_removed_from_the_state` | Same as above, for a removed pin. | 1, then 3 |
| `FinishWithoutThePinTests::test_the_state_file_deleted` | Same as above, for a deleted state file. | 1, then 3 |
| `FinishWithoutThePinTests::test_the_state_file_unreadable` | Same as above, for an unreadable state file. | 1, then 3 |
| `FinishWithoutThePinTests::test_the_state_replaced_with_a_legacy_shaped_one` | Same as above, for a legacy-shaped state. | 1, then 3 |

**Obsolete for normal work, not deleted:** the tests whose subject is push
authorization during a run (mainly `tests/test_push_authorization.py`, and the
movement and pin tests in `tests/test_fail_closed_pins.py`). Each one is:
- **kept, for legacy runs** (Stage 3); or
- **converted** into an assertion of the new invariant (no remote command
  runs, I-L1; acceptance is local, I-L2); or
- **removed**, only when another test covers the same property. The handoff's
  traceability table gives a reason for each removal.

## Verification rules (every stage)

These are the same rules as Plan R2, because the reason for them has not
changed.
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
- **Positive controls.** Every negative test shows that the same attack
  succeeds without the protection: plain `git`, an already protected path, or
  a `mock.patch` that disables the protection in the test only.
- **Real repositories.**
  - Git tests use real disposable repositories and bare remotes.
  - I-L1 is proven by a recorder: a `git` wrapper first on `PATH` (or a
    `GIT_TRACE2` event file) that logs every subcommand the engine runs.
  - It is never proven by mocking `engine_git`.
- **Traceability.** The handoff has a table: acceptance item → test id →
  positive-control test id. The reviewer checks it statically.
- **A human gate before each later stage and before completion.** At each
  gate, the credential-free isolated test runs at the exact accepted SHA.
  The human gate, not the reviewer's `READY`, is the evidence.

## Stage 1 — Repair verification

This is Plan R2's Stage 1, narrowed to what local-first still needs.

**Outcome:**
- **`pushInsteadOf` regression.** It pins, plants `url.<evil>.pushInsteadOf`
  after pinning, and pushes through the pinned path. The push lands on the
  pinned remote, and `<evil>` receives nothing.
  - Positive control: plain `git push` reaches `<evil>`.
  - `unpinned_local_remote` stays asserted separately.
- **The five finish tests** assert exit `3`, the documented code for "refused
  by its checks". Every later assertion then runs unchanged.
  - Positive control: the same run with an intact pin and no planted
    `pushurl` finishes, and `push.git` stays empty.
- **The `--repo` control** is `plain git push --repo <path> --mirror`. A test
  asserts the Git 2.53.0 semantics directly, so a change in Git fails loudly.
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

## Stage 2 — Local acceptance and runs

**Outcome:**
- **Freeze, accept and `enter_review` are local (I-L2).**
  - They require the candidate on the local stage or run branch, which is
    either its tip or, for an advanced head, the existing reviewed
    fast-forward rule. They also require a clean tree, and for accept the
    verdict at the exact SHA.
  - They read no remote and no remote configuration. This applies to the
    primary and to siblings alike.
  - The refusal "not available on the intended remote branch" is removed. In
    its place, a typed `candidate_not_on_branch` refusal states the local
    rule.
- **Runs never push or watch a remote.**
  - `start-plan`, `run-plan` and `resume-plan` no longer push, pin a remote,
    pause for push authorization, or check remote movement after a turn.
  - `--allow-push-for-run` is refused with `push_not_part_of_runs`, whose
    message points to Push to review. It is not silently ignored, so a
    script that relied on it learns immediately.
  - Failed-turn checking keeps its local integrity checks.
- **Status and polling are offline.** `runs --json`, `slice-branch --json`
  and the handoff run no remote command. The handoff's "Pushed" line becomes
  "Local commit: <sha> on <branch>".
- **Enforced in `engine_git` (I-L1).** Outside a publication context, every
  remote command refuses with `remote_operation_not_allowed`. That includes
  forms reached through aliases, `-c` options or `--git-dir`.
- **Ownership (R-4, local integrity).** In a managed worktree, the owning run
  comes from the managed record. A `.sparring/` claim that contradicts it
  refuses with `stage_owner_mismatch`. This is a local-integrity check.
  `.git` and `.sparring/` stay same-user writable until Plan D.

**Starting points:**
- `acceptance.py` (`_pushed`, `_sibling_pushed`, `_require_explained`,
  `_run_remote`, the freeze, accept and review paths).
- `plan.py` (start, resume and turn paths; `_pin_siblings`; the push and
  movement pauses), `loop.py`, `push_gate.py` (callers only),
  `git_context.py` (`verify_pushed` callers), `handoff.py`, `cli.py`,
  `engine_git.py` (the remote-command refusal), `managed_run.py`.

**Boundaries:**
- `finish-run` is Stage 3. Publication is Stage 4.
- Legacy run state still loads. Its remote records are read only by Stage 3
  code.

**Acceptance evidence:**
- **End-to-end runs.** A managed run and a hand-driven stage each run to
  acceptance, with fake providers and real Git, in each of two setups:
  1. a repository with **no remote at all**;
  2. a repository whose `origin` is a real bare remote.

  In both, the recorder shows no remote subcommand, and in the second, the
  bare remote's refs are unchanged byte for byte.
- **Planted configuration.** A repository whose live configuration plants:
  - `remote.origin.url` at a decoy;
  - `url.*.insteadOf`;
  - `core.sshCommand` and `credential.helper` at marker scripts.

  It still runs to acceptance. No marker runs and no remote command is
  recorded.
  - Positive control: plain `git ls-remote` in that repository does run the
    planted program.
- **The remote-command refusal.** One test per remote command form refuses
  outside a publication context.
  - Positive control: the same form works inside a test publication context.
- **Flag refusal.** `--allow-push-for-run` refuses with
  `push_not_part_of_runs`, and nothing is created.
- **Local acceptance.** A candidate that is not on the branch, or a dirty
  tree, refuses with `candidate_not_on_branch`. A verdict recorded at another
  SHA refuses accept.
- **Every old assertion is accounted for.** Every test that asserted the
  removed "not pushed" refusal or a push pause is converted or reclassified
  in the traceability table.

## Stage 3 — Local finish and legacy compatibility

**Outcome:**
- **The default finish is local.** `finish-run` merges into the local target,
  then cleans up (optional plan removal, worktree removal, local branch
  deletion). It contacts no remote.
  - `--push-target` and `[finish] delete_remote_branch = true` refuse with
    `remote_finish_removed`. The message names the Push to review command
    for the target branch.
  - A project config that still sets `delete_remote_branch` is reported by
    `check-config`. It is never silently ignored.
- **Legacy runs.** These are runs recorded before Plan L: with a pin, with
  `allow_push`, or paused on a remote reason.
  - Every legacy state still loads.
  - **`push_authorization_required`.** A pause of this kind came after a
    reviewer's `READY`, and was only about pushing. On `resume-plan` it is
    replaced, with a visible notice, by local acceptance under the normal
    rules (I-L2). The candidate must still be the reviewed SHA.
  - **`unauthorized_remote_movement` and `pushed_candidate_unauthorized`.**
    These pauses stay until a person answers them with the existing commands.
    They report that something other than the engine pushed, and that
    deserves a human look. The commands are documented as legacy-only.
  - **A legacy run that expects a remote finish**, and a project that needs
    the old behaviour once, use one deliberate path:
    1. `finish-run` (local);
    2. then a publication (Stage 4) of the target branch.

    There is no hidden remote step.
- **The five `FinishWithoutThePinTests`** are retargeted. Under a publication
  of the target, and under `finish-run --push-target` (now refused), each
  damaged-record case refuses and changes no ref anywhere.
  - Positive control: an intact publication of the same target succeeds.

**Starting points:**
- `managed_finish.py` (`_run_guard`, `candidate_not_pushed`, the
  `push_target` and `delete_remote_branch` steps, `_destination`).
- `config.py` (`[finish]`), `plan.py` (resume of a paused legacy run),
  `cli.py` (`finish-run`).

**Boundaries:**
- No publication internals (Stage 4), beyond the hand-off to them.

**Acceptance evidence:**
- **Local finish.** A finish on a repository with and without a bare remote:
  - the target is merged locally;
  - the recorder shows no remote subcommand;
  - the remote's refs are unchanged.
- **Refusals.** `--push-target` and `delete_remote_branch` each refuse with
  `remote_finish_removed`, and nothing is merged.
- **Legacy states.** Fixtures in legacy shape:
  - pinned and paused at `push_authorization_required` → resume accepts
    locally, with the notice;
  - paused at `unauthorized_remote_movement` → it stays paused until
    acknowledged;
  - legacy unpinned, with a remote → it finishes locally.
- **The retargeted tests.** The five, with their control.

## Stage 4 — Explicit publication (Push to review, engine side)

**Outcome:**
- **The command.** `sparring publish --repo-root <path> --sha <full sha> --remote <name> --branch <destination> [--json]`
  is a preview. It writes nothing and prints:
  - the repository path;
  - the commit SHA and subject;
  - the remote name and its URL, resolved from a trusted snapshot of user
    and system configuration only;
  - the destination ref and its current tip;
  - whether the push is a fast-forward, a new branch, or refused as
    non-fast-forward;
  - a confirmation token binding all of these (I-L4).
- **The push.** `… --confirm <token>` re-reads everything. It refuses with
  `publish_preview_changed` if anything differs. It then pushes only that SHA
  to only that ref:
  - through the hardened isolated remote path;
  - with an explicit expected old tip (`--force-with-lease=<ref>:<old>`);
  - comparing full SHAs (R-14).
  - There is no force push and no tag push.
  - A destination of `main`, `master` or the repository's default branch
    refuses unless `--allow-default-branch` is given. That flag is shown in
    the preview.
- **A publication receipt** goes to
  `<git-common-dir>/agent-sparring/publications/` (time, SHA, destination,
  result). No freeze, accept, review or finish code reads it (I-L3). The
  output says plainly "Published for review. This does not accept the
  candidate."
- **The two applicable tests** (Stage 1 #1 and #2) become publication tests:
  - planted `pushInsteadOf`, `pushurl` and `insteadOf` never redirect a
    publish;
  - forms that hide the destination stay refused.
- **VS Code contract.** The JSON schema of the preview and the result is
  documented in `docs/reference.md`, so the extension can show exactly those
  fields and require an explicit click.

**Starting points:**
- `push_gate.py` (the push half), `remote_pin.py` (the snapshot),
  `engine_git.py` (the remote path), `cli.py`, `docs/reference.md`.

**Boundaries:**
- Engine only. The VS Code button is the companion plan.
- No credential guarantees (Plan D).

**Acceptance evidence (real bare remotes):**
- **Success.** Preview then confirm pushes exactly the SHA to exactly the ref,
  and the receipt is written. Stage, run and acceptance state are byte-
  identical before and after.
- **Drift.** After the preview, changing the SHA, the branch, the remote URL,
  the destination tip, or a planted transport key → `publish_preview_changed`.
  Nothing is pushed.
- **A race.** A destination moved by someone else between preview and
  confirm makes the lease refuse, and the ref is unchanged.
  - Positive control: plain `git push` without a lease overwrites it.
- **Planted configuration.** Planted `pushInsteadOf`, `pushurl` or
  `insteadOf` never redirects the push. The decoy's refs and hook marker
  show nothing.
  - Positive control: plain `git push` is redirected.
- **The default branch** refuses without the flag.
- **Separation.** An import test shows that the acceptance, review and finish
  modules never import the receipt reader.

## Stage 5 — Local Git hardening completion

**Outcome (audit R-6 to R-8):**
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
- `engine_git.py` (`BASE_OVERRIDES`, `config_entries`, `_checked`),
  `managed_finish.py`, `managed_run.py`, `git_context.py`.

**Acceptance evidence:**
- **R-6.** For each engine command, a marker script set as a local-scope
  signing program runs under plain Git (positive control) and not under the
  engine. A globally configured signer still signs the finish merge.
- **R-7.** A populated submodule with a clean-filter marker in its own
  configuration: the marker runs under plain `git status`, and not under the
  engine's. An uncommitted gitlink change is still reported dirty.
- **R-8.** The typed refusal, with no command run.

## Design decisions

- **L-D1, legacy push pauses.** A paused `push_authorization_required` is
  converted to local acceptance on resume. The alternative is to require a
  person to answer it once.
  - Conversion is safe because that pause only ever followed a `READY` at the
    same SHA.
  - **Confirm before approval.**
- **L-D2, remote movement detection.** Remote movement detection is dropped
  from normal runs, not kept as an option.
  - It needed `ls-remote` after every turn, saw only one ref, and was
    detection rather than prevention.
  - Plan D is the prevention. A publication's lease still detects a moved
    destination at publish time.
  - **Confirm before approval.**
- **L-D3, `--allow-push-for-run`.** It is refused, not silently ignored.
- **L-D4, publishing the default branch** needs a visible extra flag.
- **L-D5, stage count.** Five small stages, each gated, instead of Plan R2's
  three broad ones.
  - Stages 4 and 5 are independent of each other. Stage 5 could run before 4.

## After Plan L is accepted (human)

1. Run the credential-free isolated test at the final exact SHA.
2. Run an independent security review of `36e2cea..<final sha>`, focused on
   I-L1 to I-L5.
3. Write the companion plan in `agent-sparring-vscode`:
   - remove the push choice and the push-authorization prompts;
   - make polling offline;
   - add **Push to review**, which shows the preview fields and needs an
     explicit click;
   - label it "not acceptance".
4. Plan D next, then Plan 2.
