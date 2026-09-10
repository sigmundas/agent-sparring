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

## Known limitations / unresolved questions

- Acceptance verifies candidate *identity*, not that any particular sparring
  exchange happened. "The changed candidate must go through sparring again"
  is a convention plus the normal `run-loop` route, not a mechanical
  precondition — by design, per the task's prohibition on review-attempt
  identity.
- `verify_pushed` proves reachability from the branch's configured remote
  ref; if the remote is unreachable at freeze time the freeze is refused
  (fails closed), which is intended but does mean freezing needs network
  access to a real remote.
- The `.sparring` carve-out means a change to a *tracked* file inside
  `.sparring/` (e.g. an edited `project.toml`) does not block a freeze. This
  is the intended trade-off but is worth a sparrer's opinion.
- Stage 7 (the Sporely pilot) was not started; no `.sparring/project.toml`
  or `PROJECT.md` exists for this repo itself yet.
