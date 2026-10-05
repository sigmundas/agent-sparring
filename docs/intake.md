# Plan intake: preparing a manifest from a human plan

[← Documentation index](README.md)


> **Experimental.** Design status (2026-09-27): Stage A of the
> [plan-intake integrity revision plan](plans/plan-intake-integrity-revision.md)
> -- approval binds execution -- is implemented and described here; Stages B
> and C (source ownership, gate topology, review usability) are not, so their
> integrity claims do not hold yet. A plain Markdown plan
> ([Run a whole plan](plans.md)) needs none of this.

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
blocks ("before Stage 3A", "Stage 3A requires …") and follow the one it
follows ("after Stage 1A"), else `gate_unenforced` refuses; a moved gate's text must evidence its kind and no other; a repository neither the plan nor `PROJECT.md` names is
`needs_decision`; a named one that was not inspected refuses with the
`--context-repository` to add. `intake.json` records the repository names
these were checked against, so approval recomputes the same findings.
`gate_at_boundary` is validated but not yet rendered: a gate inside a slice
is still the blocking `gate_inside_run`. Answering decisions is not
implemented yet; approval goes through the same `approve-plan` path.

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
