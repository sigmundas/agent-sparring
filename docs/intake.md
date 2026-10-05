# Plan intake: preparing a manifest from a human plan

[← Documentation index](README.md)


> **Experimental.** Design status (2026-09-27): Stage A of the
> [plan-intake integrity revision plan](plans/plan-intake-integrity-revision.md)
> -- approval binds execution -- is implemented and described here; Stages B
> and C (source ownership, gate topology, review usability) are not, so their
> integrity claims do not hold yet. A plain Markdown plan
> ([Run a whole plan](plans.md)) needs none of this. The normal way in is
> [`sparring start-plan`](#one-command-start-sparring-start-plan), which
> prepares, reuses and approves an intake for you; `prepare-plan` and
> `approve-plan` below remain as the advanced, step-by-step path.

A plan that is useful to a person is often not executable as written:
stages labelled `0`, `1A`, `1B`; constraints that live above the stage
headings; stages in different repositories; implementation mixed with a
later production action; dependencies stated in prose. The Markdown parser
deliberately does not guess at any of that. Plan intake is a pre-execution
step that does, with a person approving the result:

```sh
# check out the branch the slice runs on first
sparring prepare-plan docs/plans/active/foo.md [--mode faithful|refine|compile] \
    [--context-repository web=../web]
# review .sparring/intake/<id>/report.md, briefs/, amendment.diff
sparring approve-plan .sparring/intake/<id> --run <slice> \
    [--repository web=../web --repository-branch web=B] \
    [--confirm-prerequisite <gate>] [--without-amendment]
# prints the exact: sparring run-plan --manifest … --run-key … --expected-branch …
```

`prepare-plan` runs **one fresh, read-only** agent turn (the sparring role;
`codex-cli` only, because its read-only sandbox is OS-enforced) that reads
the whole plan, `PROJECT.md` and the named repositories, and answers in a
fixed schema: run slices and their ordered stages, reusable context blocks,
gates, exclusions, findings and a verdict. Nothing is executed or approved,
and the plan document is never written. The engine then checks the answer
and writes, under `.sparring/intake/<id>/`: a snapshot of the plan, the
prompt, the interpretation, one brief per stage, `report.md`, and, in refine
mode, `amendment.diff`. None of these is in manifest format. The very last
write is `prepared.json` (named by `intake.json`'s `completion_marker` key):
an intake interrupted after `intake.json` was written but before this file
looks complete but is not, and `approve-plan` refuses it until `prepare-plan`
is run again.

- **Briefs are assembled from the plan's own text.** The engine renders each
  brief with a `## Goal` paragraph first, from the stage's own structured
  `rationale` (or its `title` when the agent gave no rationale) — intake's
  understanding of the stage, never a heuristic re-read of the quoted text
  below it — then the attached context blocks verbatim, then the stage's own
  text verbatim, then an optional `Intake scoping` note that says it is
  intake's, may only narrow, make a boundary explicit or defer to a gate, and
  yields to the plan text. A substantive change goes in refine mode's
  proposed amendment, which is shown as a diff and never applied.
- **Source coverage.** Every non-blank line of the plan (horizontal rules
  aside) must be stage text, context that at least one stage attaches, a
  gate, or an exclusion with a reason. Anything else is a
  blocking finding, so a plan-wide constraint cannot silently drop out of
  the briefs.
- **Run slices.** A manifest runs in one primary repository on one branch;
  a sibling declaration (see [Cross-repository candidates](reference.md#cross-repository-candidates)) pins a coupled
  candidate but does not move a stage's work. Intake groups stages into
  slices by primary repository, and a slice is approved and run from its own
  project.
- **Gates are boundaries.** The runtime cannot wait for a deployment or a
  go-ahead between two stages of one run, so a gate that blocks later work
  must fall between slices: a gate may follow only a slice's last stage, and
  may block only the first stage of a slice. Anything else is a blocking
  finding.
  Approving a slice requires `--confirm-prerequisite <gate id>` for every
  gate it waits for, recorded in `approval.json`. An earlier slice it
  depends on is **not** confirmed by name: approval requires that slice's
  own approval and its sealed run recorded complete with an accepted final
  candidate, and records that evidence.
- **Repository snapshots.** Intake records every inspected repository,
  context-only ones included: canonical worktree path, common git
  directory, branch, HEAD and dirty paths (from the shared
  `repo_fingerprint`, so contents, ignored files and modify-then-restore
  are not attested). Approval refuses if any inspected repository is
  another repository or at another commit — except the exact final
  candidate of a slice of this intake that the approved slice depends on,
  directly or through other slices (for `A1 → B1 → A2`, A2 accepts A1's
  candidate), proven complete and accepted — and if a context repository is
  on another branch (except the branch such a slice ran on). The approval
  records the direct prerequisites under `earlier_slices` and the indirect
  ones it counted under `indirect_slices`. There is no drift override.
- **The branch is chosen at approval.** A slice runs on the branch its
  primary repository has checked out when the slice is *approved*, at the
  inspected (or earlier-accepted) commit, so a later slice does not inherit
  whatever branch prepare-plan happened to see. A slice with an
  implementation stage is refused on a protected branch (`main`, `master`),
  because run-plan's branch guard never runs a stage agent there. A plan
  naming its own branch still refuses until intake saw it checked out, and
  `--expected-branch` at approval may only repeat the branch checked out.
- **`sparring slice-branch <intake> --run <slice> [--json]`** says whether
  a slice needs a feature branch (`needs_branch`, `action`,
  `suggested_branch`, and the engine's `problem` sentence), which is what the
  VS Code extension shows instead of "ready to start". `--create BRANCH`
  checks out a new branch at the current commit (`git switch -c`, keeping
  the worktree as it is).
- **One recovery for an approval sealed on a protected branch** (before
  approval refused one): `--create BRANCH`, or `--move` onto a feature
  branch checked out by hand. `approval.json` is not rewritten; the engine
  writes `runs/<slice>/branch-move.json` once, bound to the approval's exact
  bytes, and every reader of the approval applies it. It refuses unless the
  slice runs an implementation agent on the protected branch it was approved
  on, the worktree is the approved one at the approved commit with no dirty
  paths beyond those recorded at approval, every other recorded repository
  is unmoved, no provider turn has happened for any of its stages, and its
  run (if started) is its sealed run paused before its first stage with no
  push authorization. The paused run's recorded branch is updated to match,
  so `resume-plan` continues it on the new branch. It is not a general
  branch-change: an approval on a branch where it can run is never moved.
- **Approval** refuses on any blocking finding, a refusal verdict, a plan,
  snapshot, interpretation or `report.md` that is not what intake produced,
  an unconfirmed gate, an unproven earlier slice, repository drift, a
  sibling path that is not the inspected repository, or an unacknowledged
  amendment. It writes `runs/<slice>/manifest.json` — the ordinary manifest
  inside an intake envelope — and then creates `approval.json` exactly
  once. Repeating an identical approval returns it; a conflicting one
  refuses; neither rewrites it.

**Compile mode** (`--mode compile`, Stage 1 of the
[run-plan compiler plan](plans/run-plan-compiler.md)). The agent may
normalize the execution topology, never what the plan asks for, and never
proposes an amendment. Each agent finding carries a `disposition` instead of
a severity — `auto_resolved` (info), `plan_note` (recommendation),
`needs_decision` or `refuse` (both blocking) — and a `needs_decision` one a
`decision` (id, question, why, at least two options). `auto_resolved` is
kept only with a `transform` from a closed set (`relabel_stage`,
`attach_context`, `split_review_barrier`, `gate_at_boundary`,
`reorder_within_candidate`, `conditional_sibling`) whose deterministic guard
holds; otherwise the engine downgrades it to `needs_decision` and says why
in `report.md`. Every check above still applies, plus: a source line
stating an independent review must reach a node; a paragraph that states a
gate (a stage reference with an approval word such as "go-ahead", or with
a gate-like event and a prerequisite word such as "before" or "requires") must be carried by a declared gate, not by a brief or an
exclusion, and that gate must still block the stage the sentence says it
blocks ("before Stage 3A", "Stage 3A requires …", "do not start Stage
3A …") and follow the one it follows ("after Stage 1A", "until Stage 5 is
complete"), else `gate_unenforced` refuses; a moved gate's text must evidence its kind and no other; a repository neither the plan nor `PROJECT.md` names is
`needs_decision`; a named one that was not inspected refuses with the
`--context-repository` to add. `intake.json` records the repository names
these were checked against, so approval recomputes the same findings.
In compile mode a gate inside a slice is not `gate_inside_run`: it lands in
that slice's manifest as `gates_before` of the earliest stage it must
precede, and a gate after the slice's last stage that blocks nothing becomes
its `completion_gates` (manifest version 2; see [plans.md](plans.md)). Such a
gate is not a `--confirm-prerequisite`; the run stops for it instead,
including a gate blocking the slice's first stage (the run stops before
creating anything). A slice with
no such gate still gets a version-1 manifest.

**Intake modes, side by side.** `faithful` interprets the staging as
written; `refine` may also propose different boundaries and an amendment
(shown as a diff, never applied); `compile` (the mode `start-plan` uses)
may normalize the topology through the transforms above, never amends, and
gives each finding one of four dispositions:

| disposition | severity | what happens |
| --- | --- | --- |
| `auto_resolved` | info | applied; only with a guarded `transform` |
| `plan_note` | recommendation | shown; does not block |
| `needs_decision` | blocking | a person answers its `decision` (below) |
| `refuse` | blocking | the plan must change |

**Answering decisions.** Answering never writes into the intake that asked
(an intake directory is written once). `start-plan --answer D=O` (or
`prepare_plan` with an answered parent) runs a **new** compile prepare. It
refuses, before any provider turn, unless the asking intake is a finished
compile intake, the source plan still has that intake's source digest, and
every answer names a decision its interpretation asks and one of that
decision's option ids. The new intake writes `decisions.json` — the parent
intake id and interpretation digest, the source digest, and per answer the
question, `why`, option id, label and consequence copied verbatim, the
stages its finding named and the intake that asked it — and records its
digest in `intake.json`, so the approval's `intake_record_sha256` seals it;
`run-plan --manifest` also re-checks the file's digest before every run.
An answered intake's own answers are carried forward when it is answered in
turn. The agent is given the answers; each answer renders into the briefs
of the stages its finding named (every stage when it named none) under
`# Preparation decision (not plan text)`, and `report.md` lists them. The
source plan is never edited. Reproducibility means these recorded inputs
plus the sealed resulting interpretation, not an identical re-run of the
agent turn.

## One-command start: `sparring start-plan`

```sh
sparring start-plan <plan.md> --expected-branch B [--json] \
    [--context-repository NAME=PATH ...] [--repository-branch NAME=BRANCH ...] \
    [--answer DECISION=OPTION ...] [--confirm TOKEN] [--allow-push-for-run]
```

It takes the same provider, model and executable flags as `run-plan`.

1. **Preflight**, before any provider turn: the checked-out branch must be
   `--expected-branch` and not protected (start-plan never checks out or
   creates a branch — use `git switch -c`, or `slice-branch --create` for an
   approved slice), the tree must be clean, no run of the plan may be open,
   and both roles' provider/model/effort must resolve.
2. **Route.** A plan `plan.py` parses (`## Stage <n> — <title>`) takes the
   **direct** route: no intake, no approval file, and `--confirm` runs
   exactly `run-plan <plan.md>`. Anything else takes the **intake** route:
   the newest finished compile intake of the plan whose source digest,
   primary and context repositories and answers match — and that still
   describes the repositories (each inspected one where it saw it, or a
   slice of it already approved) — is reused; otherwise a compile prepare
   runs (one read-only provider turn). With `--answer`, the prepare answers
   the newest matching intake that asks the remaining decisions.
3. **Without `--confirm`** it writes nothing beyond a prepare's own intake.
   For the intake route it chooses the first run slice of this project that
   is not proven complete, and computes its manifest and approval with
   `build_approval` — the pure half of `approve-plan`, which writes nothing —
   so every approval refusal is reported now. Declared siblings come from
   `--context-repository`, on the branch intake saw unless
   `--repository-branch` says otherwise.
4. **With `--confirm TOKEN`** it never prepares (no matching intake
   refuses), recomputes the status and token from disk, and refuses any
   difference. Then it seals through `seal_approval` (the same write
   `approve-plan` makes), recording `approved_via: "start-plan"` outside the
   idempotence decision — so a slice already approved by `approve-plan` is
   the same approval, not a conflict — and runs the sealed manifest through
   the same in-process entry as `run-plan --manifest`. If the run refuses
   after sealing, retrying the same `--confirm` reuses the identical approval.

Exit status: 0 `ready` (or the run's own status after `--confirm`),
2 `needs_decision`, 1 `refused`. Multi-repository plans: start-plan handles
this project's first unfinished slice and lists the later ones; each is
started from the project of its own repository.

**The confirm token** is the SHA-256 of canonical JSON (sorted keys, no
whitespace) of `token_version` (1) and, for the intake route: `route`,
`intake_id`, `intake_record_sha256` (of `intake.json`'s bytes),
`report_digest`, `interpretation_digest`, `decisions_digest` (or null),
`run_id`, `run_key`, `manifest_digest` (this slice only, so it covers every
gate), `source_digest`, `expected_branch`, `repositories` (`name`, `path`,
`git_common_dir`, `branch`, `head` — now — of the primary and every declared
sibling), `models` (`{stage|sparring: {provider, model, effort}}`) and
`allow_push_for_run`. For the direct route: `route`, `plan_label`,
`plan_digest` (the digest a run records), `source_digest` (the file's
text), `expected_branch`, the primary repository, `models` and
`allow_push_for_run`. Any change — a moved sibling HEAD, another answer,
another report, a gate — is another token.

**`--json` status** (schema version 1). Every key is always present:

```json
{
  "schema_version": 1,
  "status": "refused | needs_decision | ready",
  "route": "direct | intake | null",
  "plan": {"path": "/abs/plan.md", "label": "docs/plans/foo.md"},
  "expected_branch": "feature/x",
  "intake": null,
  "slice": null,
  "later_slices": [],
  "decisions": [],
  "findings": [],
  "confirm_token": null,
  "error": null
}
```

- `intake` (intake route): `{id, directory, report, mode, reused, answers}`
  — `report` is the `report.md` to link as preparation details; `answers`
  maps decision id to option id.
- `slice` (ready): `{run_id, run_key, manifest_version, stages,
  completion_gates}`; each stage is `{stage_id, label, title, mode,
  plan_stage_label, gates_before}`, gates are `{id, title, kind, reason}`.
  `plan_stage_label` is the human plan's stage label the node maps to (a
  display mapping, not a manifest field). The direct route has `run_id`,
  `run_key`, `manifest_version` and each `stage_id` null (a run mints them).
- `later_slices`: `[{run_id, primary_repository, stages: [label]}]`.
- `decisions` (needs_decision): `[{id, question, why, options: [{id, label,
  consequence}], stages, finding}]`, answered with `--answer id=option`.
- `findings` (intake route): every finding as `{code, severity, disposition,
  origin, message, stages}`.
- `confirm_token`: only when `ready`; `error`: only when `refused`.

With `--confirm`, a refusal is reported in the same shape; once it runs,
the output is `run-plan`'s.

**What `run-plan --manifest` verifies.** An intake envelope runs only with
the `approval.json` beside it, and before anything is recorded or any
provider runs the engine refuses unless the manifest's exact bytes (not
merely an equivalent serialization) and semantic digest are the approved
ones; the envelope's intake, slice and run key match; it is still in the
intake it was approved in; `intake.json`, `interpretation.json`,
`source.md` and the source plan on disk are unchanged since approval; the
run is in the approved primary worktree (same canonical path and git
directory, so another worktree or clone is refused) on the approved branch;
and a fresh run uses the approved run key with **every** repository the
approval recorded -- context and sibling ones included -- still the same
repository on the same branch at the commit it was approved at. The stages
executed are parsed from the bytes that were verified. The run records
source kind `intake-manifest` and a digest that covers the approval;
resume and `reset-stage` repeat the worktree and branch check, and resume,
acceptance and advancement re-run the input verification -- but none of
them compares commits, so the run's own accepted work is normal progress.
`report.md` is compared at approval and not afterwards: it is the view the
person approved from, not an execution input, so a later edit is inert
(making it a protected review artifact is Stage C). A plain manifest or Markdown plan whose run key or stage ids an
intake minted is refused, so the inner manifest cannot be re-run
unapproved.

**Trust model.** Approval is a workflow-integrity guarantee against
accidental drift, not a security boundary against a managed agent that
deliberately runs `approve-plan` or rewrites approval files under your
account — that is out of scope exactly as an agent attacking the engine
itself is. The approval therefore lives in the git-ignored intake
directory; no sandbox, key or external store is involved.

**Compatibility.** Hand-authored manifests and Markdown plans are unchanged
and need no approval. An intake prepared before approvals were sealed
(`intake.json` version 1) cannot be approved, a version-1 `approval.json`
authorizes nothing, and its stage ids are refused as a plain run: prepare
and approve it again. A copy of such a manifest with fresh identities
cannot be told from a hand-authored one; that is a separate, human-authorized
plain run. Runs already started from one keep their existing guarantees.

The interpretation is a reviewed file, never workflow state, and the
runtime never asks a model which stage is next. `prepare-plan` exits 2 when
its report has blocking findings.
