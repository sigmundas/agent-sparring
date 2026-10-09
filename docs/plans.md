# Run a whole plan

[← Documentation index](README.md)


A reviewed plan with several bounded stages can run from stage to stage
without you launching each one:

```text
reviewed plan
    -> sparring start-plan docs/plans/foo.md --repo-root . --expected-branch feature/x
    -> review the summary, then run its confirmation command
    -> Stage 1: run-loop … READY -> freeze -> accept
    -> Stage 2: fresh sessions … READY -> freeze -> accept
    -> Stage 3: … NEEDS_YOU -> plan pauses, prints the checks it needs
you do the checks
    -> sparring resume-plan docs/plans/foo.md --repo-root . --expected-branch feature/x \
           --evidence "Tested on a Pixel 7: resume after 24h works."
    -> Stage 3: the SPARRER resumes against the unchanged candidate
       … READY -> freeze -> accept
    -> Stage 4 … until the next human gate or the end of the plan
```

**The normal way to start a plan is `sparring start-plan`**, for any
human plan, staged with `## Stage <n> — <title>` or not:

```sh
sparring start-plan docs/plans/foo.md --expected-branch feature/x [--json]
# ready: prints the stages, any pauses for gates, and the command to confirm:
sparring start-plan docs/plans/foo.md --expected-branch feature/x --confirm <token>
```

A plan this page's Markdown convention parses runs directly, exactly as
`run-plan` below. Any other plan is prepared once in compile mode (one
read-only provider turn, reused while it still matches) and, if it needs
your choice, the dry run says `needs_decision` and lists the questions:
answer with `--answer <decision>=<option>` and run it again. The confirm
token binds exactly what was shown — plan, preparation, this slice's
stages and gates, repository commits, models, push choice — so a confirm
after anything changed refuses. start-plan never switches or creates a
branch and refuses a dirty tree. See [start-plan in the intake
reference](intake.md#one-command-start-sparring-start-plan) for routes,
the token and the `--json` schema; `run-plan`, `prepare-plan` and
`approve-plan` stay available as the step-by-step path.

`run-plan` takes the same provider flags as `run-loop`. It stops for
`NEEDS_YOU`, `ESCALATE`, a provider/integrity failure, a freeze or accept
refusal, the SEND_BACK runaway limit, and the end of the plan; ordinary
`READY` is accepted automatically through the existing gate, at the exact
pushed SHA, with no human confirmation. The gate itself is unchanged: if it
refuses, the plan stops and nothing is substituted.

Use `--stop-after-stage <stage-id>` to take a plan one stage at a time: the
run accepts that stage and then pauses with its position already advanced,
without running, creating or briefing the next one. The plan is left exactly
as any other pause leaves it, so the next `resume-plan` continues normally.

### A stage whose candidate is still uncommitted

Some stages are deliberately left uncommitted until a human has verified
them — a project convention for work that needs a real look before it
becomes a commit. `READY` then arrives over a working tree, not a commit,
and the acceptance gate has nothing to freeze. The runner does not treat
such a `READY` as the end:

```text
Stage 4: implementation, deliberately left uncommitted
    -> NEEDS_YOU: five manual checks
you do the checks
    -> resume-plan --evidence "…"
    -> the SPARRER resumes against the unchanged candidate … READY
    -> ONE bounded turn: commit that exact tree on the expected branch, push,
       report the SHA — and change nothing else
    -> the engine compares the committed content against the reviewed tree,
       path by path
    -> the sparrer reviews that exact commit … READY -> freeze -> accept
```

The comparison is the point: a turn that rewrote a file and committed the
rewrite leaves a working tree just as clean as one that committed the
reviewed work untouched, so "clean now" proves nothing. If the committed
content is not the content that was reviewed, the run stops without
accepting and records which paths diverged in the stage's `notes.md` —
because a person's manual check described the tree they inspected, and it
does not carry forward onto different work. Nothing is rolled back; the
commit and both sessions are left as they are.

Stage artifact files (`state.json`, `brief.md`, `notes.md`, `handoff.md`,
`sparring.md`, `activity.jsonl`) are outside that comparison, as they are
outside candidate identity everywhere else: the engine rewrites them on
every turn. The freeze's own dirty-tree rules are not relaxed for any of
this.

A run that is *already* stopped between a recorded `READY` and the commit —
which is where any run killed at that moment stops — is resumed the same
way: `resume-plan` recognises it and enters at that one bounded turn instead
of spending an implementation turn on a tree a human has already verified.

Each planned stage gets its own `.sparring/stages/<stage-id>/activity.jsonl`,
with the same provider stream a direct `run-loop` produces (the adapters are
rebuilt per stage so the stream follows the stage), plus `plan.*` lines
saying when the runner entered, accepted, paused on, failed at or completed
a stage, and when evidence was recorded. That is telemetry for watching the
run; the plan's position lives in `.sparring/plans/` and is never read from
`activity.jsonl`.

### A check that is owed, but not now

Not every manual check has to interrupt the run. A product choice the next
stage builds on does; "resize the finished dialog and check it is still
readable" does not — nothing downstream depends on the answer, and a failure
would be a bounded local fix. The reviewer makes that call, and there are two
shapes for it:

```text
NEEDS_YOU + human_gate            stop now; this must be answered before the
                                  stage can be READY
READY + deferred_human_gate       the stage is accepted, a person still owes
                                  this check, and the reviewer said why
                                  continuing first is low risk
```

The second one keeps the run going:

```text
Stage 1 … READY, with "check comparison readability" deferred
    -> stage 1 accepted; 1 manual check deferred until plan completion
Stage 2 … READY                 (nobody was interrupted)
Stage 3 … READY                 (nobody was interrupted)
    -> every stage accepted, and one check is still owed
    -> the plan PAUSES instead of completing
```

The engine will not forget it and will not complete the plan on it. The
obligation is recorded in the run's own state — the stage that raised it, the
reviewer's rationale, the checks, and the engine-minted gate instance an
answer must belong to — so it survives the next stage, a process exit, a
reload, further `SEND_BACK` cycles and `resume-plan`. Deferring is not
waiving: the stage is *accepted **and** still owes a check*, and both are
reported.

A deferral without a rationale is refused. So is a checkpoint this version
does not implement; today that is `before_plan_completion` only.

The final pause is typed, like the push one:

```json
"awaiting": {
  "kind": "deferred_verification_required",
  "reason": "plan_completion",
  "instance_ids": ["5b22e1c0…"]
}
```

Answer it, and the plan finishes without re-running anything already
accepted:

```sh
sparring resume-plan … \
  --deferred-result 'resize-readability=pass=legible down to 700px'
```

Outcomes are `pass`, `fail` and `blocked`. `blocked` records that the check
could not be performed, which resolves nothing — a plan completed on it would
be a plan completed on a verification nobody made. A `fail` also keeps the
plan open, and is written into the originating stage's `notes.md`. The engine
does not rewind a stage by itself, because un-accepting accepted work is a
person's decision — see [repairing a check that
failed](#repairing-a-check-that-failed) for making that decision. Where the
same check id is owed by more than one asking, address it as
`'<gate instance>:<check id>=pass'`; a bare ambiguous id is refused rather
than guessed.

Deferred is not permanently deferred. Every sparring turn of a managed run is
shown what the run already owes, and may decide that a later stage now depends
on one of those answers; naming its gate instance in `promote_deferred` stops
the run for it before the next stage, under the same asking.

#### A check owed by a plan that runs as intake slices

An intake runs one plan as several managed runs — its run slices, often in
different repositories. "Before plan completion" then means before the
*plan* completes, not before the slice that raised the check does. So when a
slice reaches its last stage while other slices of its intake are not yet
complete, its unanswered deferred checks are **carried** to the plan's
ledger, unanswered and unwaived, and the slice completes; later slices can be
approved and run. The ledger lives with the intake, in the repository the
intake was prepared in:

```text
.sparring/intake/obligations/<plan key>.json
```

It is keyed by the plan document, so an intake prepared again for the same
plan still finds it. Whichever slice turns out to be the last one to
complete **claims** every carried check at its end and stops for them there,
exactly as a one-run plan stops at its end — the plan cannot complete while
any is owed. "Complete" is the same proof approval uses for an earlier slice:
its sealed run recorded complete with an accepted final candidate. An answer
is written into the `notes.md` of the stage that raised the check, in that
stage's own repository, and the ledger records which run answered it. Can't
test resolves nothing here either. A slice that an earlier engine stopped at
its own end for such a check is recovered by resuming it: it completes
without re-running anything and carries the check on.

### Repairing a check that failed

Only a `pass` settles an obligation, and the run loop skips accepted stages.
So at the checkpoint, where every stage is already accepted, reporting a
`fail` on its own goes nowhere: the run records it, stops again, and asks the
same question. Nothing is wrong about that — the plan genuinely is not
verified — but it is not a way forward either, and the way forward is not to
make the check pass by hand.

`reopen-stage` is the decision to put the work back in front of the agents:

```sh
sparring reopen-stage 5b22e1c0 docs/plan.md \
  --repo-root . --expected-branch feature/x
```

It names the *asking*, not the stage, because the asking is what failed. Two
things change, and nothing else does:

- the stage goes from `ACCEPTED` back to `WORKING`, keeping its candidate,
  both sessions and its notes. Re-entering a stage whose `sparring.md` records
  `READY` is an ordinary path: the sparrer confirms it on its own terms before
  the hard acceptance gate runs again. The stage agent reads the `fail` first,
  because the engine already wrote it into this stage's `notes.md`;
- the failed asking is **withdrawn** from the ledger. It was a question about
  a candidate that is about to be replaced, and if it still applies to the
  repaired one the new review raises it again — as a new asking, with a new
  gate instance, because it is a new question about new work. The answer that
  failed stays in `notes.md`, which is never rewritten.

This is not [`reset-stage`](reference.md#restarting-a-stage-started-under-the-wrong-mode),
which solves a different problem: that one archives the whole attempt,
quarantines what it wrote and requires the repository to be back at the
preceding stage's candidate, because the work there ran through the wrong
lifecycle. Here the work is not wrong, it is incomplete.

Only the run's **current** stage can be reopened. An obligation raised by an
earlier stage is refused, because the stages after it were built on its
acceptance and restarting it would rewrite their history; that repair belongs
in a follow-up stage, and the refusal says so. Also refused: an asking this
checkpoint is not stopped on, and one nobody has reported failing — reopening
a stage over a question that was never answered would throw away an accepted
candidate for nothing.

### Pushing a verified candidate

Acceptance only ever freezes a commit that is already reachable from the
branch's intended remote ref. That rule is not new and is not relaxed. What
*is* new is what happens when a reviewed candidate has not been pushed —
usually because the project's own agent instructions forbid an agent from
pushing without being told:

```text
    -> the sparrer reviews the commit … READY
    -> the commit is not on origin/<branch>, and nothing has authorized a push
    -> the run PAUSES and asks, naming the exact commit
```

It pauses; it does not fail, and it does not push. The pause is recorded in
the run's own state as a typed reason, so a tool reading it knows this is a
permission question rather than a manual test:

```json
"awaiting": {
  "kind": "push_authorization_required",
  "stage_id": "…-stage-4-…",
  "candidate_sha": "f2e455c…",
  "branch": "feature/add-reference-dialog",
  "remote": "origin",
  "remote_branch": "feature/add-reference-dialog"
}
```

Two ways to answer it, and a third that needs nothing from the engine:

```sh
# allow exactly this commit, then continue
sparring resume-plan … --allow-push-candidate f2e455c…

# allow it and stop being asked again for this run
sparring resume-plan … --allow-push-candidate f2e455c… --allow-push-for-run

# or push it yourself and resume normally
git push origin feature/add-reference-dialog
sparring resume-plan …
```

`--allow-push-for-run` can also be given to `run-plan`, which records the
permission as part of creating the run.

An authorized push is exactly one thing:

```sh
git -c push.followTags=false push <remote> refs/heads/<branch>:refs/heads/<remote branch>
```

Ordinary, non-force, one fully qualified refspec, so no `push.default` or
`remote.*.push` configuration can widen it. No `--force`, no
`--force-with-lease`, no tags, no ref deletion, no branch switching, no
other branch, and no sibling repository — a declared sibling is a different
repository, and authorizing this run's candidate says nothing about it. The
engine then re-proves that the commit really is reachable from that remote
ref, and only then runs the unchanged acceptance gate. A push that fails,
or one that reports success without landing, stops the run with nothing
accepted.

Permission is bound to what it was granted for: this run, this worktree,
this branch, that one remote branch, and — for `--allow-push-candidate` —
that one stage and that one commit. `--allow-push-candidate` is refused
unless the run is in fact waiting for permission to push exactly that
commit, so a surface that has been open a while cannot authorize a candidate
the run has since replaced. The default is no authorization at all, which is
also what a run recorded before any of this existed loads as.

## Managed runs

A **managed** run gets its own branch and worktree, created, recorded,
resumed in, merged from and removed by the engine; you never pick a branch
or clean up a worktree yourself. Runs without `--managed` behave exactly as
before, and a worktree the engine did not record is never touched.

```sh
# start: a new worktree and branch from --target-branch (default: the branch
# checked out here); your checkout is not switched or modified
sparring start-plan docs/plans/foo.md --managed [--target-branch main]
sparring run-plan docs/plans/foo.md --repo-root . --managed      # without the confirmation step
# resume from any checkout of the repository
sparring resume-plan --run-key <run-key> [--evidence "..."]
# list, check, finish
sparring runs [--json]
sparring finish-run --run-key <run-key> --dry-run [--json]       # exit 0 eligible, 3 not
sparring finish-run --run-key <run-key> [--push-target] [--allow-merge-commit] [--merge-only] [--json]
# what is believed unused (--dry-run reports; deleting needs a selector)
sparring prune --dry-run [--json]
sparring prune [--dry-run] [--older-than DAYS] [--keep N] [--json]
```

- The record lives at `<git-common-dir>/agent-sparring/worktrees/<run-key>.json`;
  it is the only link between a run and its worktree. The worktree is a
  sibling directory `<repo>-sparring-<run-key>` on branch
  `sparring/<plan-slug>-<suffix>`. Only Markdown plans and plain manifests
  run managed, and only in a single repository.
- `finish-run` re-makes every check, then in order: merges into the target
  (fast-forward; a merge commit only with `--allow-merge-commit`; never a
  rebase), with `[finish] remove_plan = true` commits the plan file's
  removal on the target (below), pushes the target with `--push-target`
  (never forced; one push carries the merge and the removal), archives
  the worktree's `.sparring/` state to `<git-common-dir>/agent-sparring/runs/<run-key>/`,
  removes the worktree (`git worktree remove`, never `--force`), deletes the
  local branch (`git branch -d`, never `-D`), with `[finish]
  delete_remote_branch = true` deletes the remote branch, and records the
  run finished. Each step records its event; it stops at the first failure,
  leaving the rest in place and saying what remains; running it again
  continues, skipping only steps whose event is recorded. `--merge-only`
  runs the merge, the plan removal and the push.
- Both `[finish]` options default to false and are read from the reviewed
  candidate's own `.sparring/project.toml`.
- **Plan removal** (`plan_removal.code`): the run snapshots the plan file's
  exact bytes when it starts (the manifest file for a manifest run, never
  its sidecars). At finish the plan is removed only when the target's bytes
  equal that snapshot byte for byte — no stage-digest comparison. The
  commit is `sparring: remove finished plan <label> (run <run-key>)` with a
  `Sparring-Run: <run-key>` trailer, under your git identity; in the
  checkout that has the target it needs an empty index and an unmodified
  plan (other unstaged edits are left alone, nothing is stashed), and with
  no such checkout it is built and compare-and-swapped onto the target.
  The snapshot's sha256 is also kept in the record's `created` event,
  outside the worktree; the target bytes, the snapshot and that digest must
  all agree. Only a regular file (mode 100644/100755) is removed.
  `plan_removed` on success; `plan_snapshot_missing` (no snapshot, or no
  digest in the record), `plan_snapshot_mismatch`, `plan_not_tracked`,
  `plan_absent`, `plan_changed`, `target_checkout_dirty`,
  `target_operation_in_progress` and `git_identity_missing` leave the file
  and are reported without stopping the finish. Such a refusal is final: it
  is recorded (`plan_removal_refused`) and a resumed or replayed finish
  reports it and never retries. Only `plan_commit_failed` stops the finish
  (and is retried). `plan_removal_disabled` when the option is off,
  `finish_config_unreadable` when the candidate's config cannot be read.
- **Remote branch** (`remote_branch.code`): deleted only as a
  compare-and-delete, `git push --force-with-lease=refs/heads/<b>:<candidate>
  <remote> --delete refs/heads/<b>`, so the server refuses unless it is
  exactly the merged candidate. First the remote is read at its push URL:
  an absent branch is `remote_branch_absent` (success, no push), and if the
  remote target does not yet contain the candidate (no `--push-target`, say)
  the branch is kept with `remote_target_missing_candidate` — push the
  target and re-run. A refused delete is classified from the push's own
  status: `remote_lease_mismatch` (stale info), `remote_delete_rejected`
  (the remote refused) or `remote_unreachable`. Every one of these keeps
  the branch, stops the finish at `delete_remote_branch` unfinished, and a
  re-run resumes. `remote_branch_deleted` on success.
  `remote_delete_disabled` when the option is off (the branch is kept),
  `no_remote` when the record has none. While kept it is also listed in
  `kept` as `keep_remote_branch`; with the option off its code is
  `remote_delete_unavailable`, a compatibility alias kept for one release
  and removed in the next.
- `prune --dry-run` (no selector) reports finished records, records whose
  worktree directory is gone and finish archives, each with its reason,
  exactly as before. With `--older-than DAYS` and/or `--keep N` it selects
  finished runs (records with lifecycle finished, archives whose record is
  finished or missing) by their `finished` time (else the archive's mtime);
  `--keep N` keeps the N most recently finished, and with both an item must
  meet both. Without `--dry-run` it first appends one summary line per run
  to `<git-common-dir>/agent-sparring/runs/pruned.jsonl` (fsynced; if that
  fails nothing is deleted), then deletes the selected records and
  archives. Unfinished runs are never touched; a record whose worktree is
  gone stays `report_only`. Nothing runs automatically. It names nothing
  the engine did not record.

**Finish checks** (each reported with `ok` and a sentence; any failure makes
the run ineligible to merge, except `unarchived_project_state`, which only
blocks cleanup — `eligible` reports `merge` and `cleanup` separately): `unmanaged` (no engine-created record that owns a
branch and worktree), `run_not_complete` (not complete, or a stage not
accepted at the final candidate), `human_gate_pending`, `runner_live`,
`worktree_missing`, `branch_mismatch`, `worktree_dirty`,
`candidate_mismatch` (HEAD, branch tip and accepted candidate differ),
`candidate_not_pushed`, `branch_in_use`, `target_checkout_dirty`,
`target_operation_in_progress`, `target_advanced` (target moved past the
base: needs `--allow-merge-commit`, or the target is missing),
`merge_conflict`, and `unarchived_project_state` (cleanup only: the run's
project state cannot be archived; the merge may still go ahead).

**Start and resume refusals** (`could not run plan: …` / `could not resume
plan: …`, everything left in place): `expected_branch_with_managed`,
`input_kind_unsupported` (an intake input), `sibling_repositories`,
`detached_head`, `project_not_committed`, `path_exists` / `branch_exists` /
`run_key_used` / `record_exists` (a collision is never resolved by reuse),
`worktree_add_failed`, `creation_failed` / `creation_incomplete`,
`target_moved`, `input_mismatch` / `input_kind_mismatch` /
`repository_mismatch` (a resume naming a different input or repository),
`managed_record_missing` / `managed_record_mismatch` /
`managed_record_unowned` / `worktree_branch_mismatch` /
`worktree_unregistered` (run state and record disagree: an integrity
refusal), and `record_malformed` / `record_unreadable` /
`record_schema_unknown`. Finish execution steps report `stopped_at` as one
of `merge`, `remove_plan`, `push_target`, `archive_state`, `remove_worktree`,
`delete_branch`, `delete_remote_branch`. JSON shapes: [reference](reference.md#managed-run-json).

## Marking stages in a plan — or handing over an execution manifest

There are two ways to tell `run-plan` what the stages are. The Markdown
convention below is the built-in one. If your plan does not fit it — labels
like `3A`/`3B`/`3C`, historical handoff sections that define nothing, a
sequence already half-executed under other stage ids — the second way is to
interpret the document yourself and hand over an **execution manifest**:

```sh
sparring run-plan --manifest .../manifest.json --repo-root . --expected-branch feature/x
sparring resume-plan --manifest .../manifest.json --repo-root . --expected-branch feature/x
```

```json
{
  "version": 1,
  "plan_label": "docs/plans/active/foo.md",
  "source_digest": "sha256:… of the plan document as you read it",
  "stages": [
    {
      "stage_id": "stage-3c-cloud-schema-rpc-and-sync-transport",
      "label": "Stage 3C",
      "title": "Cloud schema/RPC and sync transport",
      "brief": "… the exact brief.md content …",
      "mode": "implementation",
      "repositories": [
        {"name": "sporely-web", "path": "../sporely-web-worktree",
         "branch": "feature/cloud-transport", "candidate_sha": null}
      ]
    }
  ]
}
```

The array order *is* the execution order; labels are display strings and are
never parsed, so numeric `1..N` is not required in manifest mode. A manifest
carries no status, position, transition or verdict — those stay in
`.sparring/plans/` and each stage's `state.json`, exactly as for a Markdown
plan, and both inputs run through the same code. Unknown fields are refused
rather than ignored, so a manifest written against a later contract fails
loudly. `plan_label` is which document the run executes and what it is
reported by; the run's own identity is its `--run-key`, so a manifest and the
plan it was built from describe the same run rather than merely sharing a
name.

The digest covers everything executable *and* `source_digest`, so re-emitting
a manifest from an edited plan refuses to continue an existing run — the same
protection the Markdown path gets. Re-emitting an unchanged one is stable, so
a tool may regenerate the file on every invocation.

**Version 2: plan-declared gates.** A `"version": 2` manifest is version 1
plus `gates_before: [{"id", "title", "kind", "reason"}]` on any stage,
and/or top-level `completion_gates` of the same shape; it must use at
least one, and gate ids are unique. After the preceding stage is accepted the
run mints one obligation per gate (checkpoint `before_stage:<stage id>`, or
`before_plan_completion` for completion gates) and stops with
`deferred_verification_required` (reason `before_stage` or `plan_completion`)
before the gated stage is created or before reporting COMPLETE. A gate on the
first stage stops a new run before anything is created; its answer is kept in
the run ledger. Only
`resume-plan --deferred-result <instance>:<gate id>=pass` releases it;
`fail`/`blocked` keep the run stopped, reaching a gate never satisfies it, and
`--evidence` is refused at a `before_stage` stop. The v2 digest covers every
gate field and its position; version-1 digests are unchanged.

`repositories` is for a stage whose reviewed candidate spans more than one
repository; see [Cross-repository candidates](reference.md#cross-repository-candidates). `mode` is for a stage
that is a review and nothing else; see [Review-only stages](reference.md#review-only-stages).

`stage_id` is the caller's to choose, and a caller that builds manifests from
plan documents should namespace fresh ids by the *run* the way the Markdown
convention does (`<run key>-stage-<label>-<slug>`), passing that run key to
`run-plan --run-key` so the run is filed under it. Otherwise two runs in one
worktree — two plans that both define "Stage 1 — Foundation", or two runs of
one plan — name the same directory, and the second run is refused as reaching
into the first run's stages
— see "Whose stage is whose" below. A stage that already exists keeps
whatever id it was created under; only new stages need the namespace.

### The Markdown convention

Stages are level-2 headings numbered 1..N in document order:

```markdown
## Stage 1 — Foundation
...
## Stage 2 — Incremental rendering
...
## Stage 3 — Prefetch and enrichment
...
```

A hyphen, en dash or colon works as the separator too. Everything from the
heading to the next `#`/`##` heading is that stage's section, and becomes
the stage's `brief.md` verbatim; deeper headings belong to the stage. The
stage id is derived deterministically as
`<run key>-stage-<n>-<slugified title>`, where the run key is this execution's
identity — the plan's file stem, a short hash of its path, and a short hash
for the run itself (for example `foo-3f9a2c1b-91af03d4`) — so two runs with
the same headings never share stage artifacts, whether they are two plans or
two runs of one plan.
Nothing is inferred from prose: a plan with no such headings, a heading that
starts with `## Stage` but does not fit, a numbering gap or duplicate, or an
empty section is refused before any agent runs. A reviewed plan written
another way either needs a small edit to mark its stages, or a manifest.

## Pause and resume

Run position lives in `.sparring/plans/<run key>.json`: the plan document, a
digest of its executable content, the branch, the current stage, which kind of
input it runs from, this run's own key, and a status
(`running`/`paused`/`complete`). Candidate SHAs, sessions and acceptance stay
in each stage's own `state.json`.

`resume-plan` continues a recorded run. Given a plan document with exactly one
open run it continues that one; `--run-key <key>` says which, and is needed
only for a document with several open runs.

Where inside the current stage it continues is the engine's own record,
`next_turn` in the stage's `state.json` (see
[whose turn it is](reference.md#whose-turn-it-is-next_turn)). When an
implementation turn completed and the reviewer then failed before a verdict,
resuming reviews that exact candidate directly; it does not spend another
implementation turn. Evidence, a recorded human gate and a pending
finalization keep their existing precedence. `--next-turn stage|sparring`
exists only for legacy state the engine refuses to read; it is refused
whenever the engine already knows.

A plain resume continues the same conversations; `--fresh-sparrer` /
`--fresh-stage-agent` continue the same stage and candidate in a new
conversation for one role; `sparring reset-stage` starts a new attempt. See
[resume, fresh session, reset](reference.md#resume-fresh-session-reset) and
its [recovery walkthrough](reference.md#recovery-walkthrough).

#### Run instances: a plan document is an input, not a run

A plan document can be executed more than once. Each execution is a **run
instance** with a key of its own, `<plan key>-<8 hex>` (for example
`mosaic-fix-7c1e42a9-3b4d0f16`), minted per `run-plan`:

```
repository
  plan.md                    <- a document. Input to a run.
      run A                  <- .sparring/plans/mosaic-fix-7c1e42a9-3b4d0f16.json
          stage 1, stage 2
      run B                  <- .sparring/plans/mosaic-fix-7c1e42a9-91af03d4.json
          stage 1, stage 2
```

**`run-plan` always starts a new run.** The same repository, the same branch,
the same plan file and the same `Stage 1` headings as a run that already
finished do not change that: it is new work, it starts fresh Stage-agent and
Sparrer sessions, and it does not advance past the earlier run's accepted
stages. The earlier run stays on disk as inspectable history, and nothing has
to be removed for the next one to start.

The one thing that *is* refused is a second **open** run of the same document
in the same worktree — running or paused. That is not about identity; two live
managed runs would compete for the same candidate. The refusal names the open
run and how to continue it. A complete run never refuses a fresh one.

#### Whose stage is whose

A stage instance belongs to the managed **run instance** that made it. That
run's key is recorded in the stage's own `state.json` as `run`, written once
when the run creates or deliberately adopts the stage and never repointed, so
execution-stage identity is `(run instance, stage)` rather than a directory
name that anything may claim.

This is what makes both ordinary workflows work. Finish a plan, stay on the
same branch, and start a *different* plan whose sections are numbered
`Stage 1` again — or run the *same* plan again. Either way the new run's
stages are new work. The run-key prefix in each stage id keeps them apart on
disk, and ownership keeps them apart even when something generates colliding
ids: the run is refused, naming the stages, rather than quietly answering new
work with an old run's accepted work. `--adopt` does not override this and no
flag does.

Note that the owner is a *run* key and not a plan key. "Owned by plan X" would
still let a second run of X inherit the first run's accepted stages, which is
the same defect one step removed.

A `state.json` with no owner is **unowned**: a stage driven by hand with
`new-stage`, or one written before ownership was recorded. Unowned is the only
thing `--adopt` may take over, which is precisely what it is for.

#### Runs recorded before run instances existed

Nothing needs migrating. A run recorded at `.sparring/plans/<plan key>.json`
with no `run` field is read as that document's one legacy run instance, keyed
by the plan key — which is exactly what it was, since at the time a document
had a single execution. Its stages record that same key (under the older
`plan` spelling, which is read as the owner), so they stay owned by it, and a
fresh run of the same document gets stage ids and stage instances of its own.
Neither file is rewritten to say so.

#### Adopting a sequence that is already under way

`--adopt` is the deliberate way to take over stages that already exist —
typically a sequence that was driven stage by stage before it was managed.
It means "these stages were executed independently and I want this managed
run to adopt them", never "a directory with this generated id exists, so
reuse it": an existing stage another managed run owns is refused first, and a
caller must not infer `--adopt` from finding stage state on disk. It plays no
part in an ordinary *select repository -> select plan -> run*, which needs no
adoption decision at all. Each
remaining stage is checked, and every adoption is reported:

* **accepted**, with a real candidate commit → adopted and advanced past. Its
  brief is history and is not compared: the work is already through the hard
  gate, and a wording change since then cannot affect anything.
* **not accepted**, brief identical to the plan's → adopted, continuing its
  recorded sessions and candidate. The report says what is being inherited.
* **anything else** — unreadable state, missing brief, a brief that differs →
  refused, naming the stage. A stage that ran against a different brief is
  not this plan's stage, and re-briefing it silently would throw away the
  context its sessions hold.

Note what the second rule compares: the stage's `brief.md` against **the plan
input's** text for it — not against whatever the plan document says today. A
stage that has already run is defined by the brief the work was implemented
and reviewed against, and the plan section it came from is a living document
that is usually rewritten afterwards to record what was built. So an adoption
manifest should carry an already-executed stage's existing `brief.md`
verbatim, and brief only the stages that do not exist yet from the plan's
current section. One manifest then describes both the preserved history and
the future execution, and adopting a sequence never requires deleting a stage
or rolling the plan document back. (The Markdown input has no way to say this,
so a plan whose sections have moved on is a case for a manifest.)

##### A stage that is already waiting for you keeps waiting

The stage a hand-driven sequence is usually adopted *at* is one that stopped
for a person: a `NEEDS_YOU` gate, or an `ESCALATE`. Entering it is not
allowed to answer that question, so the run doesn't try. It adopts the pause
exactly as it stands — same candidate, same sessions, same `sparring.md`,
same gate — reports `already NEEDS_YOU and waiting for you`, and stops there
with the run recorded as paused at that stage. Nothing is run and nothing is
rewritten, so a human who was midway through a manual check is not asked to
start over or to produce the gate again.

The way out is the way it always was: `resume-plan --evidence`. The same rule
applies to a plain `resume-plan` that answers nothing — the pause is a real
state, not a step to be stepped over. A recorded `SEND_BACK` is the opposite
case: there is implementation work, so the loop takes it.

A `sparring.md` written before human gates were structured is read too: the
verdict is what it says, and the gate is simply absent. Anything unreadable
or unrecognised counts as no recorded verdict at all, because a half-read
verdict must never decide whether an agent runs.

#### Answering a human gate

`--evidence` is appended to the current stage's `notes.md` under
`## Human evidence`; you can also edit that section by hand. That file is the
one canonical place a human's answer lives — the sparring prompt reads the
section live and the stage prompt embeds it, so nothing has to be mirrored
anywhere for either agent to see it.

The same stage then resumes, **at the sparrer**. The human satisfied a review
gate: the code did not change, the evidence did, and the question is whether
the reviewer now accepts the same candidate. Starting an implementation turn
to carry the answer would spend a turn with nothing to implement and move the
very commit under review. What the sparrer says next decides:

| verdict     | what happens                                                    |
| ----------- | --------------------------------------------------------------- |
| `READY`     | freeze and accept at the same SHA, then the next stage starts — unless the reviewed candidate is not a commit yet, which adds one bounded commit/push turn and a review of that commit first (see above) |
| `SEND_BACK` | there *is* work: the ordinary loop takes over from the stage agent |
| `NEEDS_YOU` | still paused, with the new gate                                    |
| `ESCALATE`  | still paused                                                       |

An answer never creates a new stage, and with no code change the same SHA is
simply sparred again — no dummy commit. A stage that has never implemented
anything has no candidate to spar, so it starts normally instead.

The digest of the plan's executable content is re-checked on `resume-plan`,
before every acceptance and before every advance, so a stage section edited
during a run, even by the implementation agent, even committed, pauses the
plan instead of being accepted or executed; for a Markdown plan, prose
outside the stage sections may change freely.

After `ESCALATE`, spar the stage elsewhere with the printed handoff/packet
commands, then either accept it by hand (`freeze-candidate`,
`accept-candidate`) and `resume-plan` — an already-accepted current stage is
advanced past — or `resume-plan --evidence` with the external verdict.
