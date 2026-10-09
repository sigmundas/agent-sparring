# Repository-owned stages in one logical plan (engine)

**Status:** Approved topology (2026-10-09, revised), not started  
**Repository:** `agent-sparring`  
**Follow-on:** `agent-sparring-vscode/docs/plans/repository-slices.md` (X1–X3)  
**Prerequisite:** `feature/finish-cleanup-options` and
`fix/evidence-accept-advanced-head` reviewed and merged to `main` (both
overlap `managed_finish.py`, `managed_run.py`, `plan.py`). Do not commit or
start this plan (or the VS Code follow-on) before then.

## Goal

A plan is one ordered sequence of stages and human gates. A stage may belong to
a different repository from the one before it. The engine runs each run of
consecutive same-repository stages as its own managed execution in that
repository and keeps one logical plan: one stage sequence, one obligation
ledger, one status. Users never handle slices, stage ranges, manifests or run
keys; those are Technical details.

The first consumer is `docs/plans/ui-screenshots.md` (S1–S3 @ agent-sparring →
S4–S5 @ sporely-landing) and the recovery of its mis-scoped run
`ui-screenshots-f056173b-a19b12a7`.

## Decisions (approved)

1. **Ownership is declared, never inferred.** A Markdown stage declares its
   owner with `Repository: <name>` (also `**Repository:** <name>`) as the first
   non-blank line after `## Stage <n> — <title>`; `<name>` matches
   `[A-Za-z0-9._-]+`. An undeclared stage belongs to the **home** repository
   (the invoking project's name, `cli._project_repository_name`). The line is
   inside the section, so `plan.plan_digest` covers it. A plan with no
   declaration parses to byte-identical stages, ids, digest and run keys.
   Manifests gain optional per-stage `owner_repository` (`manifest._STAGE_KEYS`),
   digested only when present (same rule as non-default `mode` in
   `manifest.manifest_digest`). New field `plan_model.PlannedStage.owner`; this
   is **not** `PlannedStage.repositories` (sibling candidate pins).
2. **Resolved owners are persisted.** The logical-plan record stores, per
   stage, the resolved owner repository name and its binding (path, git common
   dir, `project` name). Nothing later re-derives ownership from the frozen
   source document.
3. **Repository resolution (v1):** explicit `--repository NAME=PATH` only,
   recorded with the logical plan. No `[repositories]` table in `project.toml`,
   no guessing sibling directories.
4. **Ordering:** the next repository's execution may be prepared only after
   the previous execution has been **integrated** into its target branch (its
   managed record shows `merged`, and the target contains its final accepted
   candidate).
5. **Every cross-repository transition requires explicit human
   authorization.** CLI: status + confirm token. VS Code: **Continue plan**,
   showing destination repository and target branch.
6. **Reuse verdict:** reuse the obligation ledger
   (`plan_obligations.carry/owed/sync/origin_of`, `plan._carry_to_plan`,
   `plan._claim_from_plan`), managed creation (`managed_run.plan_managed_run`,
   `create_managed_worktree`, `confirm_creation`, `append_event`), the confirm
   token pattern (`plan_start.confirm_token`, `repository_fingerprint`) and
   stage archiving (`recovery._archive_root`). Do **not** reuse intake
   interpretation/approval: `intake.RunSlice` is an LLM interpretation stored
   in a checkout's `.sparring/intake/`, slices are sealed separately
   (`intake.approve_plan`, `seal_approval`), a slice's branch is whatever was
   checked out at intake (`intake.slice_branch`), and completion is proved from
   `approval.sparring_dir` (`intake._completed_slice`), which
   `managed_finish._archive_state` removes. Instead, generalise
   `plan_obligations.SliceContext` into a small protocol (`ledger_path`,
   `run_id`, `plan_label`, `run_ids`, `incomplete_slices()`) with the intake
   implementation (unchanged) and a logical-plan implementation. Logical slice
   entries copy `RunSlice`'s field names (`id`, `primary_repository`, `stages`)
   so managed intake can adopt them later.
7. **`rescope-run` is recovery only** — for already-created, mis-scoped runs.
   New cross-repository plans use the normal start/continue path (Stage 3).
8. **Terminology.** Human-facing output says plan, stage, gate, repository.
   Executions, slices, manifests, run keys and tokens appear only in Technical
   details / `--json`.

## Design

- **Logical record:** `<home git-common-dir>/agent-sparring/plans/<logical-key>.json`,
  with `snapshot.<ext>` (exact input bytes) and `<logical-key>.obligations.json`
  beside it. Created exclusively, written atomically, changed only through
  append-only `events` (`created`, `slice_created`, `rescoped`), following the
  `managed_run` record rules. Contents: input digest, ordered stages
  (`stage_id`, label, title, **owner**), repository bindings, slices.
- **Execution records:** each managed record (in its own repo's common dir)
  gets an optional `slice: {logical_key, home_common_dir, index}` key, optional
  in schema v1 like `project_dir` (`managed_run._OPTIONAL_KEYS`).
- **Resolution:** any record → `slice.home_common_dir` → logical record → each
  slice's record. Status is **derived** from slice records and run states, never
  stored twice. Single-repo plans create no logical record.
- **Identity:** logical key = first execution's run key. Stage ids are
  namespaced by the logical key in every repository. Execution *k* ≥ 2 has run
  key `<logical>-s<k>` (deterministic, so retries converge); branch
  `managed_run.managed_branch(key)`. Every execution's `plan_digest` is the
  logical input digest. `PlanRunState` gains optional `scope`
  (`{logical_key, stage_ids}`), omitted when absent.
- **Who writes where:** the engine writes only its own records (logical record
  and ledger in the home common dir; execution record in the target common
  dir). Agents work only inside their execution's worktree; nothing runs from
  an earlier worktree. When an obligation's origin `.sparring` has been
  archived by a finish, the answer is recorded in the ledger and the claiming
  run; a removed worktree is never recreated.

## Shared rules (every stage reads this first)

- Unchanged behaviour for plans without declarations, current-checkout runs,
  intake, and manifests without `owner_repository`. Golden tests pin stage ids,
  digests, run keys, run-state and record bytes.
- The `managed-run-worktrees.md` "Never" list applies. Every refusal has a
  stable reason code and leaves everything in place.
- Repository binding: path must be a git work tree, have
  `.sparring/project.toml` committed at the target tip with
  `project = "<name>"`, and not share a common dir with another declared name.
  Codes: `repository_unknown`, `repository_mismatch`, `repository_ambiguous`.
- Tests use the isolated git environment and fake providers in
  `tests/conftest.py`, with two temp repos as home and target. Run
  `.venv/bin/python -m pytest -q` before READY.

## Stage 1 — Stage ownership and plan gates in Markdown

Scope: compiler and validation. No cross-repository execution yet, and no new
gate kind.

- `plan.parse_plan` / `MarkdownPlanSource.stages`: parse the `Repository:`
  line into `PlannedStage.owner`; refuse a malformed or misplaced declaration
  (`owner_declaration_invalid`).
- Markdown plan gates: a `Gate before: <id> — <title>` block directly after the
  `Repository:` line (or heading) maps to the existing v2 `gates_before`
  (`docs/plans.md`, "Version 2: plan-declared gates"), so Markdown and manifest
  plans have the same gate model: an ordinary human gate, released only by
  `resume-plan --deferred-result <instance>:<gate id>=pass`. The gate's reason
  text is shown verbatim, so it can state an exact required setting. Absent
  gates leave digests unchanged.
- `manifest.py`: optional `owner_repository`; `check-plan --json` reports
  per-stage `repository`, gates and derived `executions`.
- Warn (do not refuse) when an undeclared plan names another repository in a
  heading or stage prose (reuse `intake.names_repository`).
- Current-checkout `run-plan` / `start-plan` of a plan with a foreign owner
  refuses `cross_repository_requires_managed`; `_check_managed_source` refuses
  a foreign owner (`cross_repository_not_yet`) until Stage 3.
- Files: `plan_model.py`, `plan.py`, `manifest.py`, `cli.py`, gate evaluation
  module, `tests/test_plan*.py`, `tests/test_manifest*.py`.
- Acceptance/tests: **single-repo plans unchanged** (golden ids, `plan_digest`,
  `manifest_digest`, `new_run_key`); declaration parsed; malformed/misplaced
  refused; manifest key round-trips; a Markdown `Gate before:` produces the same
  obligation, checkpoint and stop as the equivalent v2 manifest gate, and its
  multi-line reason (e.g. a TOML snippet) survives verbatim; warning fires on
  ui-screenshots-shaped text.
- Non-goals: execution across repositories, resolving names to paths, new gate
  kinds or engine-verified gate checks.

## Stage 2 — Logical-plan record and scoped executions

Scope: data model and internals; no new user command.

- New `logical_plan.py`: record schema v1 (Design above) with **per-stage
  resolved owner**; `create` (exclusive), `append_event`, `load`,
  `snapshot_input`, `derive_slices`, `slice_run_key`, `derived_status`.
- `ScopedPlanSource(PlanSource)`: only the stages in scope, original positions
  kept; namespace = logical key; `digest()` = logical input digest; owners read
  from the logical record, never from the source text.
- `PlanRunState.scope` (optional); `plan._drive` / `resume_plan` use the scope
  length as `total`.
- `managed_finish._final_candidate` checks only stages in scope; if the
  record's `input_path` is gone, use the logical snapshot when its digest
  equals `state.plan_digest`.
- `managed_run`: optional `slice` key; `rescoped` added to `EVENTS`.
- `plan_obligations`: context protocol + `LogicalSliceContext`;
  `incomplete_slices` from `derived_status`.
- Files: new `logical_plan.py`, `plan.py`, `plan_obligations.py`,
  `managed_run.py`, `managed_finish.py`, `artifact_ownership.py`.
- Acceptance/tests: scoped digest equals logical digest; owners survive a
  snapshot whose text has no `Repository:` lines; finish eligible once every
  in-scope stage is accepted and refused otherwise; obligation carried at the
  end of slice 1 and claimed at the end of the last; archived-origin write-back
  skipped and recorded; **single-repo run-state and record files
  byte-identical** to before.
- Non-goals: creating executions in other repositories.

## Stage 3 — Cross-repository managed start and authorized continuation

- `run-plan --managed` / `start-plan --managed` with foreign owners: take
  `--repository NAME=PATH` (repeatable), create logical record + snapshot, run
  execution 1 in home (run key = logical key), and at scope end stop with
  "Stage N belongs to <repo>. Finish this part, then continue the plan."
- `resume-plan --run-key <logical>` from any worktree of any involved repo:
  - next execution absent → status (destination repository, target branch,
    `base_sha`, pending stages) + confirm token; refused until the previous
    execution is integrated (`previous_not_integrated`);
  - `--confirm TOKEN [--allow-push-for-run]` → re-check, refuse any
    difference, create the managed record + worktree in the target repo
    (`slice` key; input = snapshot via `managed_run.map_input`), append
    `slice_created`, run in-process as `_run_managed_plan`;
  - next execution present → resume it.
- Token binds repo path, git common dir, `project` name at target tip, target
  branch, `base_sha`, run key, push flag. Push authorization
  (`push_gate.authorization_for_run`) is minted per execution for its own
  repository only.
- Files: `cli.py` (`_run_managed_plan`, `_check_managed_source`, resume
  dispatch), `plan_start.py` (`managed_preflight`, token), `logical_plan.py`,
  `managed_run.py`.
- Acceptance/tests: **wrong/nonexistent repository** (unknown name, not a
  repo, `project` mismatch, two names → one common dir); **dirty target
  checkout** — its changes absent from the new worktree, its files and HEAD
  unchanged; **target main moves before creation** — token refused, new status
  shows the new tip; **another active managed run in the target** — allowed,
  its record/state/worktree bytes unchanged; prepare refused before
  integration; **interruption** between finishing A and creating B, and between
  record creation and `slice_created` — resume reconciles to one execution;
  **retry never duplicates**; **CLI from home and from target resolve the same
  derived status**.
- Non-goals: managed intake, parallel slices, PR hosting, automatic
  transition without authorization.

## Stage 4 — `rescope-run`: audited recovery at an accepted boundary

Recovery for already-created, mis-scoped runs only.

`sparring rescope-run --run-key K --plan PLAN.md --repository NAME=PATH [--confirm TOKEN]`

- Preconditions (each a coded refusal): run is managed and engine-created;
  `status` paused or running with no live runner (`concurrency.py` lock);
  boundary = first stage whose owner in PLAN differs from home and equals
  `current_stage_index` (`rescope_no_boundary` / `rescope_not_at_boundary`);
  every earlier stage ACCEPTED with a candidate; the current stage has no
  candidate, is not FROZEN, has no `untracked_produced`, and HEAD = branch tip
  = previous candidate on a clean worktree (`rescope_unaccepted_work`); no
  `awaiting` push.
- Stage matching: accepted stages must match the run's frozen input (digest =
  `state.plan_digest`) by position, label, title **and brief bytes**
  (`rescope_history_mismatch`). Stages being moved have never run, so their
  definitions — including `Repository:` and gates — are taken from PLAN. The
  logical snapshot is PLAN; the record stores both digests and every stage's
  resolved owner explicitly.
- Apply, in order, idempotent on retry: (1) snapshot PLAN and create the
  logical record (key = K); (2) archive the current stage directory via
  `recovery._archive_root` with a reason file — its open gate instance is kept
  there **unanswered**, marked "withdrawn: stage moved to <repo> by rescope";
  (3) carry plan-completion obligations; (4) set `scope` and
  `status=complete`, current = last accepted stage; (5) append record event
  `rescoped` (`through_stage`, `moved_stages`, `logical_key`, `archived`,
  digests) and activity event `plan.rescoped`.
- Files: `recovery.py` (`rescope_run`), `cli.py`, `logical_plan.py`.
- Acceptance/tests: **refused unless at an accepted boundary**; **refused while
  the current stage has unaccepted work**; **no loss/rewrite of history** —
  stage dirs 1..k-1, branch refs and commits byte-identical; pending stage
  archived, not deleted; afterwards `finish-run --dry-run` eligible; input
  outside the repo snapshotted and finish works after it is removed; crash at
  any step converges on retry; brief drift on an accepted stage refused.
- Non-goals: re-scoping accepted stages; hand-editing `.sparring`.

## Stage 5 — Logical view, docs, skill, end-to-end

- `runs --json` (version bump): each run carries a `logical_plan` ref; new
  `plans` array: ordered stages (label, title, owner repository, state,
  executing run key), gates, and `next` (`{repository, target_branch,
  action: "finish"|"continue"|"resume"|"gate"}`). One function,
  `logical_plan.view`, serves CLI and extension; published as a shared JSON
  fixture for parity.
- Docs: `docs/plans.md` (declaring ownership and gates), `docs/design.md`
  (repository ownership), `docs/reference.md` (flags, codes, `rescope-run` as
  recovery). `skills/sparring-plan/SKILL.md`: declare `Repository:` per stage
  for cross-repo plans; flag undeclared cross-repo plans; describe stages and
  gates, never slices.
- End-to-end test: two temp repos, plan S1@home, S2–S3@target with a Markdown
  `Gate before:` S3: start → finish → continue → stop at gate → released with
  `pass` → finish; ledger obligation answered at the end.
- Acceptance: e2e passes; fixture published; full suite green.

## Outside-plan step (manual, after this plan is implemented)

`~/.claude/agents/sporely-planner.md` is in no repository. Do not edit it until
E1–E5 are merged. Then append:
"Cross-repository plans: put `Repository: <name>` as the first line of every
stage owned by a repository other than the plan's home; run `sparring
check-plan --json` and resolve any cross-repository warning; declare required
human gates with `Gate before:`; describe stages and gates, never runs, slices
or stage ranges."

## Recovery procedure for ui-screenshots (after E1–E5 are merged)

`ROOT=~/Documents/Code/agent-sparring; LAND=~/Documents/Code/sporely/sporely-landing; K=ui-screenshots-f056173b-a19b12a7`

Until step 2, leave run `K` paused and its `stage-4-landing-run-scope` gate
unanswered; `rescope-run` withdraws it.

1. Edit `docs/plans/ui-screenshots.md`, leaving Stages 1–3 byte-identical:
   - Stage 4: add `Repository: sporely-landing`; standardize the documented
     capture command as `npm run screenshots`; add the acceptance criterion
     "`npm run screenshots` reproducibly generates the 1280 px and 375 px
     screenshots and their manifest (run twice from a clean checkout; same
     files, both populated)".
   - Stage 5: add `Repository: sporely-landing` and a human `Gate before:`
     whose reason requires, committed in sporely-landing's
     `.sparring/project.toml`:

     ```toml
     [visual_review]
     command = ["npm", "run", "screenshots"]
     ```

   Commit on main.
2. `sparring rescope-run --repo-root $ROOT --run-key $K --plan docs/plans/ui-screenshots.md --repository sporely-landing=$LAND`, then again with `--confirm <TOKEN>`.
3. `sparring finish-run --repo-root $ROOT --run-key $K --dry-run`, then
   `--allow-merge-commit --push-target` (main has advanced past 7e3a3de).
4. Verify the installed engine has S1–S3 (`visual_evidence.py` on main).
5. `sparring resume-plan --repo-root $ROOT --run-key $K` → "Stage 4 in
   sporely-landing, main @ 8741d93" (or a legitimately newer tip); then
   `--confirm <TOKEN> --allow-push-for-run`.
6. `sparring runs --repo-root $LAND --json`: `$K-s2` present;
   `public-explorer-performance-34ff457f-3a7d53c5` unchanged.
7. Stage 4 runs and is reviewed in its landing worktree. At the gate before
   Stage 5, a human commits the `[visual_review]` setting above, checks it,
   and releases the gate with `--deferred-result <instance>:<gate id>=pass`.
