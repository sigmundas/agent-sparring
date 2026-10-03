# Run Plan as a compiler: one confirmation from human plan to managed run

Repositories: `agent-sparring` (primary), `agent-sparring-vscode` (sibling,
Stage 5). Status: proposed, not approved.

## Grounding (current behaviour, verified in code)

- Direct path: `plan.py:242` accepts only `## Stage <int> — <title>`,
  numbered 1..N; `3A` is refused (`plan.py:330-360`).
- Intake: `prepare-plan --mode faithful|refine` (`cli.py:2709`) runs a
  read-only Codex turn, writes `.sparring/intake/<id>/` (`source.md`,
  `interpretation.json`, `briefs/`, `report.md`, `intake.json`,
  `prepared.json`). Findings carry `severity ∈ {blocking, recommendation,
  info}` and a `code` from `AGENT_FINDING_CODES` (`intake.py:177-205`).
  Engine checks source coverage, dependency order, run boundaries and repo
  snapshots (`intake.py:52-90`, `check_interpretation` at `:612`).
- Gates: a gate that blocks later work must fall **between run slices**; a
  gate inside a slice is a blocking finding (`intake.py:62-70`). Each slice
  needs a separate `approve-plan --run <slice>` (`cli.py:2751`). This is the
  main source of canary/barrier friction.
- Deferred human gates exist but understand only one checkpoint,
  `before_plan_completion`; `before_stage:<id>` is anticipated as a new value,
  not a new format (`deferred_gate.py:35,75`).
- Review-only nodes exist: `StageMode.INDEPENDENT_REVIEW` (`stage.py:86`).
- Manifest v1 (`manifest.py:99`): `{version, plan_label, source_digest,
  stages[]}`, unknown keys rejected, other versions refused.
- **The extension already interprets plans in TypeScript**: Run Plan calls
  `buildManifest` (`agent-sparring-vscode/src/core/manifest.ts:201`,
  invoked from `src/vscode/commands.ts:3192-3245`), which decides `3A/3B`
  ordering, historical sections and briefs, then hands the result to
  `run-plan --manifest`. The intake path is a separate prepare → approve →
  run click sequence (`commands.ts:588-905`). That splits planning semantics
  across two languages, against the "extension is a thin presenter" rule.
- `sparring-run` skill teaches `prepare-plan`/`approve-plan` as a manual
  experimental path (`skills/sparring-run/SKILL.md:65-73`).

## Design

### Three intake modes
- `faithful` and `refine`: unchanged (diagnostic and amendment-producing modes).
- `compile` (new, default for the normal path): may normalize the execution
  topology. It never produces `amended_plan`.

### Findings disposition (additive)
Compile-mode findings carry a `disposition` field in addition to `severity`:
`auto_resolved | plan_note | needs_decision | refuse`. Faithful/refine keep
their current semantics. The old `severity` is derived for compatibility
(`needs_decision`/`refuse` → blocking, `plan_note` → recommendation,
`auto_resolved` → info).

`auto_resolved` is only valid with a `transform` drawn from a **closed
enum**. The engine rejects any other auto-resolution and downgrades it to
`needs_decision`:

| transform | engine-checkable guard |
| --- | --- |
| `relabel_stage` (3A → node) | source ranges unchanged |
| `attach_context` | context block is source text, verbatim |
| `split_review_barrier` | source range cited states the independent review; both nodes map to the same source stage |
| `gate_at_boundary` | every gate range still maps to a gate; kind unchanged |
| `reorder_within_candidate` | same node, same source ranges; cited dependency ranges present |
| `conditional_sibling` | sibling name appears in the source plan text or `.sparring/PROJECT.md` and was inspected via `--context-repository`; the condition is cited source text kept verbatim in the brief; the sibling is declared on that node and pinned at freeze as today (clean, declared branch, remote-reachable) even if unchanged. The engine has no optional-candidate concept. |

Deterministic guards that already exist stay mandatory in compile mode:
source coverage, dependency order and repo snapshot. New guards: every
source review requirement maps to ≥1 node; no gate disappears; a
repository not named by the plan or PROJECT.md is `needs_decision`, never
auto_resolved; a named sibling that was not supplied refuses with the exact
`--context-repository` to add. Compile mode never weakens a guard.

### Decisions
A `needs_decision` finding carries `decision: {id, question, why,
options[{id,label,consequence}]}`. Answering never writes into the
answered intake (an intake directory is written once by prepare).
`--answer D=O` runs a new compile prepare whose recorded inputs are the
parent intake id, its interpretation digest and the answers. It refuses
unless the source plan still has the parent's source digest and every
answer names a decision id and option id in the parent's interpretation.
The new intake writes `decisions.json` at prepare time (question, option
label and consequence copied verbatim, parent ids, source digest) and
records its digest in `intake.json`, so it is sealed by the approval's
existing `intake_record_sha256`; `verify_approved_inputs` also re-checks
the `decisions.json` digest. No new ownership row is needed: it is part of
the write-once intake directory. Answers render into affected briefs under
"Preparation decision (not plan text)". The source plan is never edited.
Reproducibility means recorded inputs plus the sealed resulting
interpretation, not identical re-output of the agent turn.

### Manifest v2 (additive)
v1 stays accepted, and intake still emits v1 whenever no v2 field is
needed; a `version: 2` manifest must use a v2 field. v2 adds:
- per-stage `gates_before: [{id, title, kind, reason}]`: plan-declared gates
  owed after the preceding node is accepted and before this node is created;
- top-level `completion_gates` (same shape): owed after the last node is
  accepted and before the run may report COMPLETE (the closeout gate).

`manifest_digest` for v2 covers every gate field and its position, so
adding, removing, moving or rewording a gate changes the digest and a
recorded run refuses to continue; v1 digests are unchanged. The
node→human-plan mapping used by the Overview is not a manifest field (it
does not change execution); it is carried in `intake.json` and
`start-plan --json`.

Runtime: after accepting node N, if node N+1 has `gates_before` (or N is
last and `completion_gates` exist), the runner mints one engine-owned
obligation per gate into the existing deferred ledger (`stage_id` = N,
`rationale` = gate reason, one check whose id is the gate id, checkpoint
`before_stage:<N+1 id>` or `before_plan_completion`), idempotent by
(checkpoint, gate id), and stops with `DeferredVerificationRequired` (new
reason `before_stage`; completion reuses `plan_completion`) before N+1 is
created, as `--stop-after-stage` does. Only `resume-plan --deferred-result
<instance>:<gate id>=pass` resolves it; fail/blocked keep the run stopped;
`--evidence` is refused while stopped on a `before_stage` gate. Every gate
kind pauses, reaching a gate never satisfies it, and manifest gates are
never confirmed at approval. `before_stage` obligations are never carried
across slices.

In compile mode a gate that blocks a slice's first node becomes that node's
`gates_before`; `run_prerequisites` excludes gates rendered into the
manifest, so `start-plan` needs no `--confirm-prerequisite`. Earlier-slice
completion is still proven by approval as today.

This lets one run slice contain canaries and barriers, so a single-repo plan
needs one manifest and one approval instead of N slices.

### One-command start: `sparring start-plan`
```
sparring start-plan <plan.md> --expected-branch B [--json]
        [--context-repository NAME=PATH ...] [--repository-branch NAME=BRANCH ...]
        [--answer DECISION=OPTION ...] [--confirm TOKEN] [--allow-push-for-run]
```
`--expected-branch` must equal the checked-out branch; start-plan never
checks out or creates a branch (protected branch → existing
`slice-branch --create` hint).

1. Preflight: reuses `check-config`, `branch_guard`, clean tree, protected
   branch refusal, run ownership and model resolution, all through the
   existing code paths.
2. Route: if `plan.py` parses the plan directly, use the direct path: no
   intake and no approval file, and `--confirm` runs exactly the existing
   `run-plan <plan.md>` path. Otherwise reuse a completed compile intake
   (completion marker present) whose source digest, decision inputs and
   context repositories match, else run prepare in compile mode. With
   `--confirm`, never prepare; with no matching intake, refuse.
3. Without `--confirm`: compute the slice's manifest and approval decision
   with the same pure function `approve_plan` uses (split `approve_plan`
   into build, which writes nothing, and seal), and write nothing beyond
   prepare's intake. Emit `refused | needs_decision | ready` (JSON schema
   documented in `docs/intake.md`) with the summary and `confirm_token` =
   sha256 of canonical JSON `{token_version, route, intake_id,
   intake_record_sha256, report_digest, interpretation_digest,
   decisions_digest, run_id, run_key, manifest_digest (this slice only),
   source_digest, expected_branch, repositories: [{name, path,
   git_common_dir, branch, head}] for the primary and every declared
   sibling, resolved provider/model per role, allow_push_for_run}`. Direct
   route: the recorded Markdown plan digest, branch, primary and
   declared-sibling fingerprints, models, push flag.
4. With `--confirm TOKEN`: recompute from current disk state; any
   difference refuses. Then seal through the existing `approve_plan` code,
   recording `approved_via: "start-plan"` outside the idempotence
   `decision` dict, and call the same in-process run-plan entry with the
   approved envelope manifest. If run-plan refuses after sealing, a retried
   `--confirm` with the same token reuses the identical approval.

The token makes the one human confirmation bind to the exact topology the
human saw, with the same strength as `approve-plan` today.
`prepare-plan`, `approve-plan`, `slice-branch` and `check-plan` remain
unchanged as advanced/debug commands.

Multi-repo plans whose primary repository changes between slices: start-plan
handles the first slice, and the summary lists the later ones. Each later
slice still needs its own start-plan from the project of its repository (no
regression; automating that is out of scope).

### VS Code
Run Plan calls `start-plan --json` (dry) and renders the states *Preparing
plan… / Needs decision / Ready to run*, using decision cards and a summary
with a "View preparation details" link to `report.md`. One **Start run**
button re-invokes it with `--confirm`. The existing Overview states take
over from there (Running, Paused for human gate, Complete), plus a
grouping by human-plan stage label (from `start-plan --json`). The TS
`parseExecutionManifest`/`parseIntakeEnvelope` must accept v2 inner
manifests (identity plus gates), with parity vectors and pins regenerated
against the engine; until then an older extension shows no stages for a v2
intake run. Run Plan stops using `buildManifest`, but it stays both for
engines without `start-plan` and for resuming existing `source=manifest`
runs (`commands.ts` resume rule).

## Stages

## Stage 1 — Compile mode, dispositions, guarded transforms
Engine only: `intake.py`, `intake_prompt.py`, `cli.py` (`--mode compile`).
Schema: `disposition`, `transform`, `decision`; closed-enum validation;
downgrade rule; new deterministic guards; severity derivation. Fixture
`tests/fixtures/plans/cloud_sync_like.md`: Stages 1, 2, 3A, 3B, a Stage 4
with an internal review barrier, Stage 5, global invariants, conditional
canaries, an optional sibling and misordered slices. Use a recorded/fake
interpretation for the agent turn. Tests: the fixture compiles to
auto_resolved/plan_note only; a contradictory fixture yields
`needs_decision` or `refuse`, never auto_resolved; faithful/refine
unchanged. `gate_at_boundary` is validated here but rendered in Stage 2;
until then a gate inside a slice remains the existing blocking
`gate_inside_run`, and the Stage 1 fixture assertion excludes exactly that
finding.

Review focus: judge whether this stage, on its own, leaves the current
engine green (full suite) and useful without assuming Stage 2 —
`--mode compile` must be usable end to end through the existing
prepare-plan/approve-plan path, and nothing may depend on manifest v2,
`gates_before` or `start-plan`.

## Stage 2 — Manifest v2 and between-stage gates
`manifest.py` (v2 parse/digest, v1 untouched), `deferred_gate.py`
(`before_stage:` checkpoint), `plan.py` (pause before the gated node,
resume via the existing flags). Intake: a gate inside a slice becomes
`gates_before` instead of a blocking finding (compile mode only). Tests:
accept → pause → evidence → next node; live-write gate requires explicit
authorization; v1 manifests and existing run state resume unchanged.
v2 digest covers gates; `completion_gates`; `run_prerequisites` excludes
manifest gates; `--evidence` refused at a `before_stage` pause; intake
emits v1 when no gate exists. Tests: closeout gate blocks COMPLETE until
pass; gate edit changes digest and refuses resume; fail/blocked keep the
run stopped.

## Stage 3 — Decisions and `start-plan`
`decisions.json` and its ownership registration; re-prepare with answers;
`start-plan` command plus JSON status schema and confirm token; approval
records `approved_via`. Docs: `docs/plans.md` (normal path),
`docs/intake.md` (modes, dispositions, JSON schema). Tests: direct plan
→ ready; fixture → ready without any manual prepare; ambiguous fixture →
needs_decision → `--answer` → ready, source untouched; token drift
refusal; dirty tree and protected branch refusal; `approve_plan` split
into build and seal with no behaviour change (existing intake tests
green); the token changes when a sibling HEAD, a decision, the report or a
gate changes; `--confirm` never prepares; a v1 intake approved by
`approve-plan` and then a `start-plan` confirm of the same slice is not a
conflict; full suite (shared acceptance path).

## Stage 4 — Skills
`skills/sparring-run`: preflight → `start-plan --json` → ask only the
decision questions → show summary → one confirmation → `--confirm`.
prepare/approve move to an "Advanced" section. Before the dry run, say
that it may spend one read-only provider turn on preparation. `sparring-plan`: unchanged
except dropping the "intake is experimental, adapt headings" guidance;
`3A`-style headings are fine. `git diff --check` only.

## Stage 5 — VS Code thin presenter (sibling `agent-sparring-vscode`)
Run Plan → `start-plan`; preparation states, decision cards, details link,
Start run; Overview groups nodes by human-plan stage label. TS manifest
and intake-envelope parsers accept v2 (parity vectors regenerated). Run
Plan stops using `buildManifest`; it stays for engines without
`start-plan` and for resuming existing `source=manifest` runs. Tests: unit tests for
JSON → view model (`overviewModel`), plus an integration test with a
stubbed engine. Declares the engine commit from Stage 3 as its sibling pin.

## Explicitly out of scope
Automatic chaining of slices across primary repositories; removing TS
`buildManifest`; any change to review, acceptance, push or NEEDS_YOU
semantics.
