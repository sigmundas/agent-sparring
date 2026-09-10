# Stage 6 handoff: Acceptance

Date: 2026-09-10

## Branch state

- Base SHA: `c182d7584603739ee043b3d64691ed06da2613cf` (verified as the exact
  pushed HEAD of `feature/sparring-v2` before starting; no divergence)
- Candidate SHA: `2d5b967eaad017ed5e27e26d6ae505d54a742650` — all Stage 6
  production code and tests. This handoff document is then updated by one
  documentation-only follow-up commit (the current branch HEAD), which
  touches no code; review the implementation at the SHA above, and note
  that `accept-candidate` on this repo would itself require freezing the
  actual branch HEAD.
- Branch: `feature/sparring-v2`, pushed to `origin`

## Files changed

Production code (new):
- `src/agent_sparring/acceptance.py` — the whole gate: `freeze_candidate`,
  `accept_candidate`, `FreezeResult`, `AcceptanceResult`, `AcceptanceError`,
  `StaleCandidateError`

Production code (modified):
- `src/agent_sparring/git_context.py` — added public `is_full_sha()` so a
  *recorded* candidate sha can be rejected as malformed with acceptance's own
  message instead of being fed to git; no parallel git logic was added
- `src/agent_sparring/stage_agent.py` — one lifecycle guard: refuse an
  unattended implementation turn against an `ACCEPTED` stage
- `src/agent_sparring/cli.py` — `freeze-candidate` and `accept-candidate`
  subcommands (plus a small `_resolved_stage` helper)
- `src/agent_sparring/__init__.py` — re-export the acceptance surface

Tests (new):
- `tests/test_acceptance.py` (27 tests) — real repo with a real bare local
  remote, so the pushed/reachable check is proven the same way it is in
  production rather than stubbed

Tests (modified):
- `tests/test_stage_agent.py` — ACCEPTED refused / FROZEN still allowed (+2)
- `tests/test_loop.py` — the loop inherits the ACCEPTED guard (+1)
- `tests/test_cli.py` — `CliAcceptanceTests`: freeze→accept, accept without
  freeze, stale accept after HEAD moves (+3)

Production code changed: **yes**. Tests changed: **yes**.

## Freeze semantics

`freeze_candidate(stage, sparring_dir, repo_root, expected_branch=...)`
always freezes the repository's *resolved current HEAD*. There is
deliberately no revision parameter: "the candidate" means the state that
actually exists and has actually been pushed, not a revision someone typed.

Checks, in this order, all before anything is written:

1. the stage's `state.json` is readable, and its status is not `ACCEPTED`;
2. the worktree is on `expected_branch` (required, mirroring
   `run-stage`/`run-sparring`);
3. `HEAD` resolves to a full 40-hex commit (`git_context.resolve_commit`);
4. the worktree holds no unrepresented dirty changes
   (`git_context.dirty_paths`);
5. that commit is reachable from `expected_branch`'s configured remote ref
   (`git_context.verify_pushed`, which proves reachability locally via
   `merge-base --is-ancestor` rather than trusting a recorded flag).

Only then are `candidate_sha` (the exact full SHA) and `status = FROZEN`
recorded. `base_sha` and both session ids are preserved untouched. All git
work reuses the existing `git_context` helpers; no parallel git logic was
added.

Freezing means only *this exact SHA is the candidate being presented for
acceptance*. It does not make the repository immutable and does not stop
anyone from committing afterwards.

Re-freezing is allowed and is not an error — see same-SHA reconsideration
below, and the stale-candidate recovery path.

### One deliberate carve-out in the dirty check

Dirty paths *inside the stage's own `sparring_dir`* do not block a freeze;
everything else does. This tool rewrites `state.json`/`handoff.md`/
`sparring.md` on every stage and sparring turn, so a project that neither
commits nor gitignores `.sparring/` could otherwise never freeze anything at
all. The ignored paths are returned in `FreezeResult.ignored_dirty_paths`
and printed by the CLI, so the carve-out is visible rather than silent.
Tested both ways (`test_workflow_artifacts_under_sparring_dir_do_not_block_freeze`,
`test_dirty_working_tree_cannot_be_frozen`,
`test_untracked_file_outside_sparring_dir_cannot_be_frozen`).

## Acceptance semantics

`accept_candidate(stage, repo_root, expected_branch=...)` accepts exactly the
frozen candidate, or refuses:

- status must be `FROZEN` (`WORKING` → refused: nothing has been frozen;
  `ACCEPTED` → refused: acceptance is terminal for a stage);
- the recorded `candidate_sha` must be a full 40-hex id (a missing or
  malformed value is refused rather than guessed at);
- the worktree must be on `expected_branch`;
- **the current `HEAD` is resolved independently here** and must be
  identical to the frozen `candidate_sha`.

On success: `status = ACCEPTED`, and `candidate_sha` remains the exact
accepted SHA.

`READY` is not acceptance. `loop.py` was not modified: the unattended loop
still stops at `READY` and never calls into acceptance, so acceptance stays
an explicit operation (`sparring freeze-candidate` / `sparring
accept-candidate`, or a direct call). The *evidence* for that decision lives
in the stage's human-readable `sparring.md`/`notes.md` and is deliberately
**not** parsed or gated on here — doing so would turn prose findings back
into workflow state, which the plan rules out. Covered by
`test_ready_sparring_outcome_is_not_automatically_accepted`.

`state.json` is written only on a *successful* freeze or acceptance, so no
refusal can half-advance the lifecycle.

## Stale-candidate behavior

If `HEAD` no longer equals the frozen `candidate_sha`, acceptance raises
`StaleCandidateError` (a subclass of `AcceptanceError`) naming both SHAs.
Specifically:

- the new HEAD is **never** silently substituted — `candidate_sha` keeps its
  frozen value, so the refusal is repeatable and inspectable;
- **no dummy commit** is created (asserted by commit-count checks);
- the state stays `FROZEN`, i.e. unaccepted. The failure path performs no
  state mutation at all, deliberately: making the refusal side-effect-free
  keeps it idempotent and easy to reason about.

Recovery is the ordinary path: the changed candidate must be frozen again
(which re-runs the clean/pushed checks) and go through sparring again before
a later acceptance. That "spar it again" step is the documented convention
plus the normal `run-loop`/`run-sparring` route — it is **not** enforced by
inspecting sparring history, since that would require exactly the
review-attempt identity/history machinery the task rules out.

Covered by `test_changing_head_after_freeze_makes_acceptance_stale`,
`test_stale_acceptance_never_substitutes_the_new_head`,
`test_stale_acceptance_creates_no_commit`,
`test_stale_candidate_can_be_re_frozen_and_then_accepted`.

## Same-SHA reconsideration behavior

Nothing records "this SHA was already reviewed", so this works by
construction rather than by a special case:

- `freeze_candidate` is re-runnable on the identical SHA (it is not an error
  to freeze an already-frozen candidate again);
- `accept_candidate` consults no sparring history whatsoever, so a SHA that
  drew `SEND_BACK`/`NEEDS_YOU`/`ESCALATE` in an earlier exchange is
  perfectly acceptable now as long as it is still the frozen candidate;
- therefore no dummy commit is needed to create a "new review attempt".

The full evidence-changes-but-code-does-not path is exercised in
`test_same_sha_may_be_reconsidered_after_evidence_changes` (freeze A →
`NEEDS_YOU` device check → human records evidence in `notes.md` → same SHA A
sparred to `READY` → accepted, with HEAD and commit count asserted unchanged
throughout) and `test_same_sha_reconsideration_requires_no_dummy_commit`.

## Lifecycle guards added to Stage 3–5 entry points

Exactly one guard, in `run_stage_agent` (inside the existing worktree lock,
before any prompt assembly or provider call):

    if state.status is StageStatus.ACCEPTED: raise StageAgentRunError(...)

`run_unattended_loop` inherits it by calling `run_stage_agent` — no second
guard was added there, and the loop was otherwise not modified.

Deliberately **not** guarded:

- **`FROZEN`.** A frozen stage may legitimately receive one more correction
  turn; the resulting HEAD move is exactly what the stale-candidate check at
  acceptance time is for, so a second guard here would be redundant
  machinery. Asserted by `test_frozen_stage_may_still_receive_a_correction_turn`.
- **`run_sparring_agent`.** The sparrer is hard read-only and sparring an
  accepted stage is harmless (and sometimes useful).

## Contributor-sparrer handling

No contributor ledger, signature, reviewer identity, or attempt history was
added. The current Codex sparrer is hard read-only (adapter-level, plus the
before/after repository-integrity check in `sparring_agent.py`), so there is
no path today by which the sparrer becomes a contributor and hence nothing
the current implementation genuinely needs metadata for. The plan's rule —
a sparrer that contributes code cannot independently accept that candidate;
a fresh independent sparrer is required — is documented as a convention in
`acceptance.py`'s module docstring, left for the future writable-sparrer
mode that would actually require enforcement.

## Verification

Full test suite (unittest; no pytest in this environment):

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

Result: all 17 test modules pass (`OK`) — 232 tests total, including the new
`tests/test_acceptance.py` (27) and the extended `test_stage_agent.py` (18),
`test_loop.py` (13), `test_cli.py` (24).

`git diff --check` (staged): clean, no whitespace errors.

## Live disposable-repo check (performed, not skipped)

Run against a throwaway repo with a real bare local remote, created under
the session scratchpad and deleted afterwards; never against this candidate
repo (verified by `git status --short` on this repo immediately after
cleanup: only the intended Stage 6 changes present, nothing leaked in).

Exercised through the real CLI (`freeze-candidate`/`accept-candidate`):

1. **Genuinely pushed clean candidate frozen** →
   `frozen candidate: 5737467b…`, `pushed: origin/feature/x is exactly
   5737467b…`, `state.json` = `{"status":"frozen","candidate_sha":"5737467b…"}`.
2. **Accepted** → `accepted candidate: 5737467b…`, `state.json` status
   `accepted` with the same SHA; commit count unchanged (no dummy commit).
3. **Stale after HEAD moves** (second stage frozen at `5737467b…`, then a
   real pushed commit `cd14af59…`) → acceptance exited 1 with
   "refusing acceptance as stale", `state.json` still `frozen` at the *old*
   `5737467b…` (new HEAD not substituted), commit count unchanged by the
   refusal.
4. **Unpushed HEAD refused** → "not available on the intended remote branch
   (… is not reachable from origin/feature/x …)".
5. **Dirty worktree refused** → "the working tree holds changes that commit
   does not represent (impl.txt)".
6. **Same-SHA reconsideration** → same SHA `4b3826b8…` sparred `NEEDS_YOU`,
   then re-recorded `READY` after evidence, re-frozen at the identical SHA
   and accepted; commit count before 4, after 4 (no dummy commit).
7. **ACCEPTED lifecycle guard** → `run-stage` against the accepted stage
   exited 1 with "refusing an unattended implementation turn against an
   accepted stage", without ever launching the provider executable.

## Deliberate deviations

- **No revision argument on freeze.** Freezing always takes the resolved
  current HEAD, which is the smallest honest surface for "this exact
  candidate"; accepting a typed revision would let someone freeze something
  other than the state they just verified.
- **The `.sparring` dirty-check carve-out** described above. Without it the
  gate would be unusable in any project that does not commit or ignore its
  own workflow artifacts. Made visible via `FreezeResult.ignored_dirty_paths`.
- **`--expected-branch` is required on both commands**, matching
  `run-stage`/`run-sparring` rather than being optional. Freeze needs the
  intended branch for its remote reachability check anyway; requiring it on
  accept too keeps one convention.
- **Acceptance does not require a READY sparring outcome.** Enforcing that
  would mean parsing `sparring.md` prose into a precondition — the
  verdict-as-workflow-state pattern the plan rejects. Acceptance is an
  explicit deliberate operation whose evidence is the human-readable
  artifacts.
- **A stale refusal mutates nothing** (it does not reset the stage to
  `WORKING`), so the refusal is idempotent and inspectable.
- **`is_full_sha()` added to `git_context`** rather than duplicating the
  full-SHA regex in `acceptance.py`, keeping one source of truth for that
  shape.
- **Re-freeze after acceptance is refused** (acceptance is terminal for a
  stage; further work belongs to a new stage) — a small hard-gate choice not
  literally spelled out in the task.

## Known limitations / unresolved questions (round 1, superseded/updated by round 2 below)

- ~~Dirty paths inside the stage's own `sparring_dir` do not block a freeze;
  everything else does~~ — narrowed by round 2's fix 1: only this stage's
  own five artifact filenames are exempt now, not an entire subtree.
- ~~A change to a *tracked* file inside `.sparring/` (e.g. an edited
  `project.toml`) does not block a freeze~~ — no longer true after fix 1:
  `project.toml`/`PROJECT.md`/another stage's artifacts were never on the
  allowlist and are exercised by a regression now.
- ~~Acceptance trusts a matching `HEAD` alone~~ — fixed by round 2's fix 2:
  the worktree is independently re-checked for unrepresented dirty changes
  at acceptance time too, not only at freeze time.
- ~~`freeze_candidate`/`accept_candidate` run outside the implementation
  worktree lock~~ — fixed by round 2's fix 3.
- Acceptance verifies candidate *identity*, not that any particular sparring
  exchange happened. "The changed candidate must go through sparring again"
  is a convention plus the normal `run-loop` route, not a mechanical
  precondition — by design, per the task's prohibition on review-attempt
  identity. Unchanged by round 2.
- `verify_pushed` proves reachability from the branch's configured remote
  ref; if the remote is unreachable at freeze time the freeze is refused
  (fails closed), which is intended but does mean freezing needs network
  access to a real remote. Unchanged by round 2.
- Stage 7 (the Sporely pilot) was not started; no `.sparring/project.toml`
  or `PROJECT.md` exists for this repo itself yet. Unchanged by round 2.

## Round 2: narrow the dirty exemption, re-check at acceptance, serialize with the worktree lock

- Base for this round: `2d5b967eaad017ed5e27e26d6ae505d54a742650` (round 1's
  implementation candidate; `bee52b1` on top of it was documentation-only)
- Round-2 candidate SHA: `c8ef2c7ce609bf2214a252fb3b4c1c38961ff486`
- Branch: `feature/sparring-v2`, pushed to `origin`

Three findings, all resolved. All original Stage 6 semantics are preserved:
exact full candidate SHA, freeze proves pushed/reachable, stale HEAD is
refused, same-SHA reconsideration, no dummy commits, no READY-history state,
explicit acceptance, the ACCEPTED implementation guard, and the
contributor-sparrer convention-only stance.

### Fix 1 — narrow the `.sparring` dirty-path exemption to an exact per-stage artifact allowlist

The round-1 exemption matched any dirty path whose *destination* started
with the repo-relative prefix of `sparring_dir`. Three concrete problems:

- `sparring_dir == repo_root` computed an empty prefix, so `str.startswith("")`
  is true for every path — every dirty file in the repository was silently
  exempt.
- Any file dropped anywhere under `sparring_dir` was exempt merely by
  location, not because it was actually a workflow artifact — including
  `project.toml`, `PROJECT.md`, another stage's own directory, or a stray
  file.
- A rename was classified only by its destination path, so renaming a real,
  tracked source file *into* `sparring_dir` under a name that happened to
  collide with a real artifact filename hid an unrepresented deletion
  elsewhere.

Fixed by replacing the prefix rule with `_stage_artifact_allowlist()`: an
exact set of five repo-relative paths, computed from `stage.directory`
itself (`<sparring_dir>/stages/<stage_id>/{state.json,brief.md,notes.md,
handoff.md,sparring.md}` — the exact filenames `stage.py` defines and
`Stage.create`/`write_state`/`write_handoff`/`write_sparring`/`write_notes`
actually rewrite), never from `sparring_dir` by prefix. A dirty entry is
exempt only if its path is in that set; a rename is exempt only if *both*
its destination and its source are in that set (`_entry_is_exempt`) — so a
rename from a real file outside the allowlist blocks, regardless of its
destination name.

This also required more precise status data than `git status`'s default
reporting gives: an entirely untracked stage directory collapses to one
`?? .sparring/...` entry by default, which is too coarse to tell "the five
real artifacts" apart from "the five real artifacts plus one stray file" in
the same untracked directory. `git_context.py` gained `DirtyEntry` (status
plus separate `path`/`old_path`, not folded into one string) and
`dirty_entries(repo_root, *, all_untracked=False)`, which passes
`--untracked-files=all` when requested so every file is reported
individually; `dirty_paths()` (used by handoff rendering, unchanged
contract) is now a thin fold on top of the same parser. Acceptance always
calls `dirty_entries(..., all_untracked=True)`.

Regressions (`tests/test_acceptance.py::NarrowedDirtyExemptionTests`, 5
new): `sparring_dir == repo_root` still blocks a dirty source file
(`test_sparring_dir_equal_to_repo_root_cannot_hide_a_dirty_source_file`); a
non-artifact file dropped in the stage's own directory blocks
(`test_non_workflow_file_underneath_sparring_dir_blocks_freeze`); a rename
from a real tracked file into a colliding artifact-looking destination name
blocks (`test_rename_from_outside_sparring_dir_into_it_blocks_freeze`);
`project.toml` and another stage's own artifacts are not exempt
(`test_project_toml_and_other_stage_dirs_are_not_exempt`); the five real
artifacts for *this* stage still do not block freeze or same-SHA
reconsideration
(`test_intended_stage_artifacts_still_do_not_block_freeze_or_same_sha_reconsideration`).
Live-verified too (see below): a dirty `stray.py` at `sparring_dir ==
repo_root` blocked; a stray file inside the stage directory blocked; a
`project.toml` at the `.sparring` root blocked; the five real artifacts
alone still froze cleanly.

### Fix 2 — independently re-check the worktree for unrepresented dirty changes at acceptance time

Previously `accept_candidate` trusted `HEAD == frozen_sha` as proof the
worktree still *is* the frozen candidate. It is not: a source/test/config
file can be edited without committing, leaving `HEAD` untouched while the
worktree no longer represents that commit.

`_accept_candidate_locked` now calls the same `_partition_dirty(repo_root,
stage)` freeze uses (same allowlist from fix 1) immediately after
confirming `HEAD == frozen_sha`, and raises a plain `AcceptanceError`
(*not* `StaleCandidateError` — `HEAD` has not moved, so this is a distinct
failure mode) if anything blocks. Recording evidence in this stage's own
`notes.md`/`sparring.md` still never blocks, by the same allowlist, so the
same-SHA reconsideration workflow is unaffected.

Regressions (`tests/test_acceptance.py::AcceptanceDirtyRecheckTests`, 3
new): an uncommitted edit to a real tracked file after a clean freeze
refuses acceptance, with `HEAD` still equal to the frozen SHA (proving this
is not the stale-candidate path) and status remaining `FROZEN`
(`test_uncommitted_edit_to_a_real_file_after_clean_freeze_refuses_acceptance`);
a new untracked source file after a clean freeze refuses acceptance
(`test_new_untracked_source_file_after_clean_freeze_refuses_acceptance`); an
evidence-only update (`notes.md` plus a fresh `sparring.md` exchange) still
permits acceptance
(`test_evidence_only_update_still_permits_acceptance`). Live-verified: an
uncommitted edit to `impl.txt` after a clean freeze refused acceptance with
status remaining `frozen`; reverting the edit let the same `accept-candidate`
invocation succeed.

### Fix 3 — serialize freeze/accept with the existing implementation worktree lock

`freeze_candidate`/`accept_candidate` previously ran with no lock at all,
while `run_stage_agent` already acquires
`agent_sparring.concurrency.worktree_lock` for an implementation turn. That
left a window where a FROZEN stage's correction turn could be mutating the
worktree while acceptance was independently reading `HEAD`/dirty state and
about to flip lifecycle status.

Both functions are now thin wrappers: `freeze_candidate`/`accept_candidate`
open `with worktree_lock(repo_root):` around the entire check-and-write body
(delegated to new `_freeze_candidate_locked`/`_accept_candidate_locked`
functions) and catch `WorktreeLockError`, re-raising it as `AcceptanceError`
without touching `state.json` — no second lock system, the same one
`run_stage_agent` already uses, keyed the same way (by `repo_root`'s
resolved identity), so a stage-agent turn and an acceptance operation on the
same worktree now genuinely contend on one lock.

Regressions (`tests/test_acceptance.py::AcceptanceWorktreeLockTests`, 2
new): holding `worktree_lock(repo)` in the test itself makes a concurrent
`freeze_candidate` raise `AcceptanceError` leaving status `WORKING` and
`candidate_sha` `None`, and releasing the lock lets an identical call
succeed immediately after
(`test_freeze_refuses_while_worktree_lock_is_held_elsewhere`); the same
pattern for `accept_candidate` against an already-frozen stage, leaving
status `FROZEN` with the frozen SHA intact, then succeeding once the lock is
released
(`test_accept_refuses_while_worktree_lock_is_held_elsewhere`). These
directly verify serialization with agent-sparring's own implementation
writer, per the task's scope — no claim is made about, or attempt made at,
blocking arbitrary external Git processes. Live-verified: holding the lock
via `worktree_lock(repo)` from a separate Python process in the same shell
session made both `freeze_candidate` and `accept_candidate` raise
`AcceptanceError` mentioning "already locked by another live implementation
process", with lifecycle state unchanged in both cases; both succeeded
immediately once the lock was released.

### Files changed (round 2)

Production code (modified):
- `src/agent_sparring/git_context.py` — `DirtyEntry`, `dirty_entries()`;
  `dirty_paths()` refactored to use the same parser, contract unchanged
- `src/agent_sparring/acceptance.py` — `_stage_artifact_allowlist`,
  `_entry_is_exempt`, rewritten `_partition_dirty`; `freeze_candidate`/
  `accept_candidate` split into lock-acquiring wrappers plus
  `_freeze_candidate_locked`/`_accept_candidate_locked`; the new dirty
  re-check in `_accept_candidate_locked`

Tests (modified):
- `tests/test_acceptance.py` — three new test classes:
  `NarrowedDirtyExemptionTests` (+5), `AcceptanceDirtyRecheckTests` (+3),
  `AcceptanceWorktreeLockTests` (+2); all 27 round-1 tests unchanged and
  still passing against the new implementation

Production code changed: **yes**. Tests changed: **yes**.

### Verification (round 2)

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

All 17 test modules pass (`OK`) — 242 tests total (round 1's 232 plus 10 new
in `test_acceptance.py`, now 37 tests in that module). `git diff --check`
(staged): clean.

### Live disposable-repo check (round 2, performed)

Run against a second throwaway repo with a real bare local remote, created
under the session scratchpad and deleted afterwards; `git status --short`
on this candidate repo immediately after cleanup showed only the intended
round-2 file changes, nothing leaked in.

Exercised through the real CLI plus a couple of direct Python calls (for the
lock-contention check, which needs to hold the lock from the same process
while calling in):

1. `sparring_dir == repo_root`: a dirty `stray.py` at repo root blocked
   freeze with "the working tree holds changes that commit does not
   represent (stray.py)".
2. A stray `random.txt` dropped inside the stage's own
   `.sparring/stages/stage1/` blocked freeze, naming that exact path.
3. A `project.toml` written directly under `.sparring/` blocked freeze,
   naming that exact path.
4. Control: with only the five real artifacts present, freeze succeeded
   normally, reporting all five as `ignored_dirty_paths`.
5. An uncommitted edit to `impl.txt` after that clean freeze made
   `accept-candidate` refuse, naming `impl.txt`, with `state.json` still
   `frozen` at the original SHA; reverting the edit let the same
   `accept-candidate` invocation succeed and record `accepted`.
6. On a second stage: holding `worktree_lock(repo)` from a Python
   `with`-block made a concurrent `freeze_candidate()` call raise
   `AcceptanceError` naming "already locked by another live implementation
   process", leaving status `working`/`candidate_sha=None`; releasing the
   lock let an identical call freeze normally. The same pattern for
   `accept_candidate()` against that now-frozen stage: refused while locked
   (status stayed `frozen`, SHA unchanged), succeeded once released.

### Deliberate deviations (round 2)

- The accept-time dirty-change refusal raises plain `AcceptanceError`, not
  `StaleCandidateError` — `HEAD` has not moved in that scenario, so labeling
  it "stale" would misdescribe the failure. `StaleCandidateError` remains
  reserved for an actual `HEAD` mismatch.
- The per-stage artifact allowlist exempts exactly
  `{state.json, brief.md, notes.md, handoff.md, sparring.md}` for the
  *current* stage only — including `brief.md`, since a freshly created stage
  (`Stage.create()`) writes all five as untracked files immediately, and
  round 1's own tests already relied on none of the five blocking a freeze.
  Narrowing the exemption below all five would have broken ordinary,
  already-tested usage rather than closing a real gap.
- `freeze_candidate`'s `sparring_dir` parameter is now unused by the
  exemption logic (which is derived from `stage.directory`) but is kept for
  call-signature/API stability; this is called out explicitly in the
  function body rather than left implicit.
- The worktree lock wraps the *entire* check-and-write body of both
  functions (state read through the final `write_state`), not just the
  final write — so lock contention is reported as early as possible and no
  partial check work happens under a lock that might be released and
  re-acquired mid-function.

## Round 3: serialize the sparring agent's state.json write with acceptance

- Base for this round: `c8ef2c7ce609bf2214a252fb3b4c1c38961ff486` (round 2's
  implementation candidate; `529389d` on top of it was documentation-only)
- Round-3 candidate SHA: recorded below after the push
- Branch: `feature/sparring-v2`, pushed to `origin`

One finding, resolved. All prior Stage 6 semantics preserved unchanged:
exact per-stage dirty allowlist, accept-time dirty re-check, freeze/accept
serialization with the worktree lock, exact pushed-SHA freeze, stale-HEAD
refusal, same-SHA reconsideration, no dummy commits, explicit acceptance.

### The finding

`run_stage_agent` and `freeze_candidate`/`accept_candidate` already
serialize on `agent_sparring.concurrency.worktree_lock`, but
`run_sparring_agent` did not:

    state = stage.read_state()          # e.g. reads FROZEN
    ... a potentially long provider turn, unlocked ...
    state.sparring_session_id = ...     # mutates the object read above
    stage.write_state(state)            # writes the WHOLE object back

If a concurrent `accept_candidate` completed a real FROZEN -> ACCEPTED
transition during that unlocked window, the sparring turn's final
`write_state` would overwrite it with the stale `status=FROZEN` object it
read before the provider turn started — silently reversing acceptance. The
same argument applies to a concurrent `freeze_candidate` being reversed back
to `WORKING`.

### The fix

Chose the smaller of the two options the task offered: hold the existing
worktree lock for the *entire* local sparring turn, rather than trying to
detect-and-merge a lifecycle change that happened mid-turn. `run_loop.py`
already runs stage and sparring turns strictly sequentially, and a
concurrent implementation write during the sparring fingerprint window was
already fatal to that turn (the read-only integrity check would catch it)
— so widening the sparring turn's lock scope to match costs nothing new in
the normal unattended-loop path.

`run_sparring_agent` is now a thin wrapper: it opens
`with worktree_lock(repo_root):` around everything from the initial
`stage.read_state()` through the final `write_state` (delegated to a new
`_run_sparring_agent_locked`), and wraps a contended
`WorktreeLockError` as `SparringAgentRunError` before the provider is ever
invoked. This is the same lock `run_stage_agent`/`freeze_candidate`/
`accept_candidate` already use, keyed the same way (`repo_root`'s resolved
identity) — no second lock system. Since `run_sparring_agent` acquires and
fully releases its own lock per call (never nested inside another
`worktree_lock` block from this codebase), and `run_unattended_loop` calls
`run_stage_agent` then `run_sparring_agent` sequentially, there is no
deadlock risk: it is acquire → release → acquire → release, never
acquire → acquire.

Separately, the final write no longer reuses the `state` object read at the
top of the turn: it re-reads `state.json` immediately before writing,
mutates only `sparring_session_id` on that fresh read, and writes it back.
Under the new full-turn lock this is not strictly needed for correctness
(nothing else can write while the lock is held), but it makes the "always
write current state, never a stale pre-turn snapshot" guarantee explicit in
the code rather than relying solely on lock-scope reasoning, and it is what
correctly preserves `status=ACCEPTED`/`candidate_sha` when sparring is run
against an already-accepted stage (a case that was, and remains,
explicitly permitted — no ACCEPTED guard was added to `run_sparring_agent`,
unlike `run_stage_agent`'s).

Nothing about the Codex OS-level read-only sandbox or the existing
before/after repository-fingerprint integrity check changed: the lock
addresses the state.json *write*, which was never something the read-only
sandbox was responsible for in the first place.

### Regressions

New module `tests/test_sparring_acceptance_concurrency.py` (4 tests), using
lock coordination rather than timing sleeps: a fake sparring adapter's
`start()` — invoked from inside `run_sparring_agent` while it still holds
the worktree lock — reentrantly attempts the "concurrent" freeze/accept
call in the same process and captures what happens, proving the
interleaving is actually impossible rather than merely improbable.

1. `test_sparring_turn_from_frozen_cannot_reverse_a_concurrent_acceptance`
   — freezes a candidate, then runs sparring with an adapter whose `start()`
   reentrantly calls `accept_candidate`; asserts that reentrant call raises
   `AcceptanceError` (lock contention), the sparring turn itself completes
   normally and leaves status exactly `FROZEN` (never touched, never
   reversed), and a real `accept_candidate` afterward succeeds and is not
   clobbered by anything the sparring turn wrote.
2. `test_sparring_turn_from_working_cannot_reverse_a_concurrent_freeze` —
   same pattern starting from `WORKING`, with the reentrant call being
   `freeze_candidate`; asserts the reentrant freeze is refused, status stays
   `WORKING` through the sparring turn, and a real freeze afterward succeeds
   normally.
3. `test_sparring_an_accepted_stage_preserves_accepted_status_and_sha` —
   freezes and accepts a stage, then runs a plain (non-reentrant) sparring
   turn against it; asserts sparring completes, records its session id and
   `sparring.md`, and `status`/`candidate_sha` remain exactly `ACCEPTED`/the
   accepted SHA.
4. `test_send_back_then_ready_loop_completes_without_deadlock` — runs
   `run_unattended_loop` through a SEND_BACK cycle then a READY cycle with
   the new locking in place; asserts it completes with `outcome=READY` and
   two cycles, proving the sequential stage-then-sparring lock
   acquire/release introduces no deadlock.

### Files changed (round 3)

Production code (modified):
- `src/agent_sparring/sparring_agent.py` — `run_sparring_agent` split into
  a lock-acquiring wrapper plus `_run_sparring_agent_locked`; the final
  `write_state` now re-reads state immediately before writing instead of
  reusing the turn-start object; module/function docstrings updated

Tests (new):
- `tests/test_sparring_acceptance_concurrency.py` (4 tests)

Production code changed: **yes**. Tests changed: **yes**.

### Verification (round 3)

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

All 18 test modules pass (`OK`) — 246 tests total (round 2's 242 plus the 4
new in `test_sparring_acceptance_concurrency.py`). `git diff --check`
(staged): clean.

### Live disposable-repo check (round 3, performed)

Run against a third throwaway repo with a real bare local remote, created
under the session scratchpad and deleted afterwards; `git status --short`
on this candidate repo immediately after cleanup showed only the intended
round-3 changes.

1. Froze a real candidate, then ran `run_sparring_agent` with an adapter
   whose `start()` reentrantly called the real `accept_candidate` against
   the same stage/repo: the reentrant call raised
   `AcceptanceError: ... already locked by another live implementation
   process ...`; the sparring turn itself completed with `READY` and left
   `state.json` at `status=frozen` with the original candidate SHA and the
   sparring session id recorded; a real `accept_candidate` run immediately
   afterward succeeded, producing `status=accepted` with the same candidate
   SHA and the sparring session id from the earlier turn intact.
2. Froze and accepted a fresh stage, then ran a plain sparring turn against
   it (`READY`, "post-acceptance sanity check"): `state.json` after the
   turn showed `status=accepted` with the original candidate SHA unchanged,
   plus the new sparring session id — confirming sparring an accepted stage
   is permitted and non-destructive.

### Deliberate deviations (round 3)

- Chose "lock the whole sparring turn" over a detect-and-merge scheme (the
  task explicitly allowed either); it is the smaller change, requires no
  new merge logic, and costs nothing in the normal sequential
  `run-loop` path.
- The accept-time-style dirty re-check added in round 2 was **not**
  duplicated into `run_sparring_agent` — the sparrer never writes (enforced
  by the existing before/after fingerprint check), so there is nothing for
  it to re-check; only `freeze_candidate`/`accept_candidate`, which
  transition lifecycle status, need that check.
- No ACCEPTED guard was added to `run_sparring_agent` (deliberately unlike
  `run_stage_agent`'s): sparring an accepted stage remains explicitly
  permitted, per the task's requirement 3.

Do not begin Stage 7 / the Sporely pilot.
