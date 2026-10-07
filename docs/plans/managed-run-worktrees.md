# Managed run worktrees (engine)

Goal: *pick plan → run → watch → merge & clean up*. For a **managed** run the
engine creates, records, resumes in, merges from and removes one disposable
branch + worktree. Branches and worktrees become implementation details.

This plan implements the "Run worktrees" section of `docs/design.md`, with the
decisions below where it was open. The VS Code client is a separate plan in
`agent-sparring-vscode` and only consumes what this plan defines.

## Shared rules (every stage reads this section first)

- **Authority.** Only the engine creates or removes a managed worktree, merges a
  managed branch, or decides eligibility. Ownership comes **only** from the
  managed-run record below — never from a directory name, branch name,
  `.sparring/` existing in a worktree, or `git worktree list`.
- **Managed vs unmanaged.** Existing current-checkout runs (`run-plan` /
  `start-plan` / `resume-plan` without `--managed`) behave exactly as today.
  An unmanaged worktree is never a cleanup candidate.
- **Never:** `--force` on any git command, `git branch -D`, force push,
  `reset --hard`, rebase or any rewrite of the reviewed candidate, deleting
  untracked or modified user files, switching the branch of any existing
  checkout, or writing engine state into another run's files.
- **Refuse, don't guess.** Every refusal names a stable reason code and a
  human sentence, and leaves everything in place.
- **Single repository first.** A managed run whose input declares sibling
  repositories (manifest `repositories`) is refused with a clear message.
- **Tests** use the existing isolated git environment (`tests/conftest.py`
  autouse fixtures) and the existing fake providers; never the developer's
  global git config or a real provider. Each stage adds focused tests and runs
  `.venv/bin/python -m pytest -q` before READY.
- Keep the change bounded: no general Git abstraction, no PR/hosting features.

## Managed-run record (schema v1)

Path: `<git-common-dir>/agent-sparring/worktrees/<run-key>.json` (common dir,
so every worktree of the repository sees it and no checkout owns it). Written
atomically (temp file + rename); created with exclusive create so a run key can
be claimed only once.

```json
{
  "schema_version": 1,
  "run_key": "<plan key>-<8 hex>",
  "plan_label": "docs/plans/foo.md",
  "input": {"kind": "markdown | manifest", "path": "/abs/path"},
  "worktree_path": "/abs/canonical/<main-worktree-name>-sparring-<run-key>",
  "branch": "sparring/<plan-slug>-<8 hex run suffix>",
  "target_branch": "main",
  "base_sha": "<40 hex>",
  "remote": "origin | null",
  "created_at": "<UTC ISO-8601>",
  "created_by": "engine",
  "events": [{"at": "...", "event": "created", "detail": {}}]
}
```

- Fields other than `events` are never changed after creation. Lifecycle is
  the append-only `events` list: `created`, `merged` (`{mode, target_sha}`),
  `target_pushed`, `state_archived`, `worktree_removed`, `branch_deleted`,
  `remote_branch_deleted`, `finished`. A record with no `created` event is
  `creating`. Lifecycle status is derived from the events, not stored twice.
- The run's plan state `.sparring/plans/<run-key>.json` (inside the managed
  worktree, as for any run) gains `"managed": true`. A run state saying
  `managed: true` with no matching record is an integrity refusal, and so is
  a record whose `worktree_path`/`branch` disagrees with where the run state was
  found. Older run states without the field read as `managed: false`.

## Stage 1 — Managed worktree record, creation and resume

Read the "Shared rules" and "Managed-run record" sections of
`docs/plans/managed-run-worktrees.md` first.

Implement, in a new module (e.g. `managed_run.py`) plus the minimum wiring in
`cli.py` / `plan.py`:

1. **Record I/O**: read/list/create(exclusive)/append-event for the schema
   above; strict parsing (unknown `schema_version` refuses).
2. **`run-plan --managed [--target-branch B]`** (Markdown plan or `--manifest`):
   - `--repo-root` may be any worktree of the repository. `--expected-branch`
     is refused together with `--managed` (the engine chooses the branch).
   - Target branch: `--target-branch`, else the branch checked out at
     `--repo-root`; detached HEAD or a missing `refs/heads/<target>` refuses.
     `base_sha` = the target branch's tip at creation.
   - Preflight before anything is written: `.sparring/project.toml` exists in
     the commit `base_sha` (the worktree must be a working project); the input
     declares no sibling repositories; no record, branch or path collision
     (refuse — never reuse or overwrite); a given `--run-key` is not already
     used by a record or a run state.
   - Create: record (exclusive) → `git worktree add -b <branch> <path>
     <base_sha>` → append `created`. If `worktree add` fails and neither the
     branch nor the path exists, delete the just-written record; otherwise
     leave it and report what exists.
   - Run: the existing run-plan path, in-process, with `repo_root` = the
     managed worktree, `expected_branch` = the managed branch, the minted
     run key, and the project directory mapped to the worktree's own
     `.sparring/` (the same relative location as the invoking one). The new
     run state records `managed: true`. The user's checkout is never switched,
     reset or written to (only git's shared metadata changes).
3. **`start-plan --managed [--target-branch B]`**: supported on the **direct
   route** only (Markdown stage convention). The token binds `managed`,
   `target_branch` and `base_sha` instead of the expected branch; the invoking
   checkout's cleanliness is not required (report uncommitted changes there as
   "not part of this run"). Nothing is created until `--confirm`, which then
   behaves as `run-plan --managed`. The intake route with `--managed` refuses
   with a clear message (follow-up work).
4. **Resume from identity**: `resume-plan --run-key K` run from **any** worktree
   of the repository: if a record for K exists, the engine uses the record's
   worktree path, branch, project directory and input path. A plan path,
   `--manifest`, `--expected-branch` or `--repo-root` given explicitly must agree
   with the record (repo-root may be any worktree of the same repository) or it
   refuses. Verify the path exists, is registered in `git worktree list
   --porcelain` for this repository, and has the record's branch checked out.
   A record with `created` but no run state starts that run (interrupted first
   start) — it never creates a second worktree. Manifest-backed runs must keep
   resuming as manifest runs (recorded `input.kind`), never as Markdown.
5. **Unmanaged guard**: an unmanaged `run-plan` inside a managed worktree is
   refused (one managed worktree belongs to one managed run).
6. **`sparring runs [--json]`** (read-only): the repository's managed runs —
   `{schema_version: 1, runs: [{run_key, plan_label, managed: true,
   worktree_path, worktree_exists, branch, target_branch, base_sha, created_at,
   lifecycle, run_status}]}` where `run_status` is the plan state's status or
   `"missing"`. Unmanaged runs are not listed in this stage.

Tests (at least): creation makes exactly one branch + worktree and a record;
the invoking checkout's HEAD, branch, index and files are untouched (including
when it is dirty); resume from the primary checkout reuses the worktree;
restart after an interrupted first start reuses it; second creation with the
same run key / colliding branch / path refuses and creates nothing; resume with
a disagreeing branch/input refuses; manifest-backed managed run resumes as a
manifest run; unmanaged runs unchanged; `--managed` with sibling repositories
refuses; `runs --json` shape.

Stop at READY. Do not implement merge, cleanup or status of git state.

## Stage 2 — Managed git status and finish eligibility (read-only)

Read the "Shared rules" and "Managed-run record" sections of
`docs/plans/managed-run-worktrees.md` first.

Add `sparring finish-run --run-key K --dry-run [--json]` and extend each entry
of `runs --json` with a `git` block and a `finish` block computed by the **same
function**. Strictly read-only: no git command that writes (merge simulation
uses `git merge-tree --write-tree`, which writes only unreferenced objects).

`git`: `{head, branch_tip, clean, final_candidate, candidate_pushed,
target_tip, target_contains_candidate, target_checked_out_at}`.

`finish` (`--dry-run --json` prints exactly this, schema v1):
`{schema_version, run_key, managed, eligible: {merge, cleanup}, merge_mode:
"already_merged" | "fast_forward" | "merge_commit" | null, actions: [...],
checks: [{code, ok, detail}], deleted_ignored_paths: [...], summary}`.

Checks (each reported with `ok` and a human `detail`; refusal codes):

| code | requires |
| --- | --- |
| `unmanaged` | a record exists and `created_by == "engine"`; anything else is ineligible for everything |
| `run_not_complete` | plan status `complete`, every stage accepted |
| `human_gate_pending` | no `awaiting`, no open deferred obligation, no pending evidence |
| `runner_live` | the worktree lock (`concurrency.py`) can be acquired now |
| `worktree_missing` / `branch_mismatch` | path exists, registered for this repo, record branch checked out |
| `worktree_dirty` | no modified, staged or untracked non-ignored file (`all_untracked`) |
| `candidate_mismatch` | worktree HEAD == branch tip == final accepted candidate (last accepted stage's accepted SHA, in plan order) |
| `candidate_not_pushed` | when the record has a remote: candidate reachable on the remote managed branch (`verify_pushed`) |
| `branch_in_use` | no other worktree has the managed branch checked out |
| `target_checkout_dirty` | if the target branch is checked out somewhere, that checkout has no staged/modified tracked files |
| `target_advanced` | target moved past `base_sha` and is not an ancestor of the candidate → only `merge_commit`, eligible only with `--allow-merge-commit` |
| `merge_conflict` | merge-tree reports conflicts → ineligible, no override |

`eligible.merge` needs every check except the cleanup-only ones;
`eligible.cleanup` additionally needs the target (locally) to contain the
candidate *after the planned merge*. `deleted_ignored_paths` lists the
top-level ignored entries removal would delete, excluding `.sparring/` (which
is archived, Stage 3). Unmanaged/unknown run keys return `unmanaged`, never an
error that looks like eligibility.

Tests: one per refusal code, plus eligible fast-forward, already-merged and
merge-commit cases; `runs --json` and `finish-run --dry-run --json` agree;
dry run changes no ref, file or record.

Stop at READY.

## Stage 3 — Finish execution: merge and clean up

Read the "Shared rules" and "Managed-run record" sections of
`docs/plans/managed-run-worktrees.md` first.

`sparring finish-run --run-key K [--merge-only] [--allow-merge-commit]
[--push-target] [--json]`. Holds the worktree lock for the whole operation,
re-runs every Stage 2 check at execution time (a dry run is advice, never a
token), then performs these steps in order. Each step is skipped if its event
is already recorded *and* still true, appends its event when done, and stops
the command at the first failure leaving everything later untouched:

1. **merge** (unless the target already contains the candidate):
   fast-forward — `git -C <target checkout> merge --ff-only <candidate>` when
   the target is checked out, else `git update-ref refs/heads/<target>
   <candidate> <old tip>` (compare-and-swap). Merge commit only with
   `--allow-merge-commit`: `git merge --no-ff --no-edit` in the target
   checkout (on any failure `git merge --abort` and refuse), or merge-tree +
   commit-tree + compare-and-swap update-ref when not checked out. Never rebase.
2. **push target** only with `--push-target`: `git push <remote>
   <target>:<target>` without force, under existing push rules; a rejection
   stops here with merge recorded.
3. `--merge-only` stops here.
4. **archive state**: copy `.sparring/plans/<K>.json` and the stage
   directories owned by K into `<git-common-dir>/agent-sparring/runs/<K>/`,
   verify the copy, append `state_archived`.
5. **remove worktree**: `git worktree remove <path>` (no `--force`).
6. **delete local branch**: only after verifying the target contains the
   candidate; `git branch -d` where it applies, otherwise compare-and-swap
   `git update-ref -d refs/heads/<branch> <candidate>`. Never `-D`.
7. **delete remote branch**: only if project config
   `[finish] delete_remote_branch = true` **and** the remote target ref
   contains the candidate **and** the remote branch tip equals the candidate.
   Default: keep it.
8. append `finished`. Re-running on a finished record is a successful no-op.

`--json` reports `{schema_version, run_key, completed_steps, stopped_at,
reason, remaining}` so a partial failure says what is left.

Tests: eligible run merges (ff) and is fully cleaned; merge commit only with
the flag; every Stage 2 refusal blocks execution with nothing changed;
conflict leaves target and both checkouts unchanged; dirty target checkout
refuses; partial failure (e.g. `worktree remove` fails) leaves record/branch
and a rerun completes; repeated run is idempotent; unmanaged worktree never
removed; remote branch deleted only under policy and containment; the
archived state is readable.

Stop at READY.

## Stage 4 — Prune report, documentation and end-to-end test

Read the "Shared rules" and "Managed-run record" sections of
`docs/plans/managed-run-worktrees.md` first.

1. `sparring prune --dry-run --json` (read-only; no deletion mode in this
   plan): finished records, records whose worktree directory is gone, archived
   runs, each with the reason it is believed unused.
2. **Engine snapshots**: nothing in the engine or extension creates
   `agent-sparring-engine-<sha>` worktrees today, so none are tracked. Add to
   `docs/design.md` the design: if an engine run ever executes from a snapshot,
   the run state records `engine: {commit, path}`; a snapshot is retained while
   any non-finished run references it and is reported prunable otherwise.
   `prune` must not report unrecorded snapshots by name.
3. Docs: a "Managed runs" section in `docs/plans.md` (start, resume, finish,
   refusal codes), JSON schemas in the reference, `docs/design.md` "Run
   worktrees" updated from *not implemented* to implemented-as-built
   (including what remains: intake route, multi-repository, abandon).
4. One end-to-end test with the fake providers: `run-plan --managed` →
   accepted completion → `finish-run --dry-run` eligible → `finish-run` →
   target contains the candidate, worktree and branch gone, record finished,
   primary checkout unchanged except the fast-forwarded target.

Stop at READY.
