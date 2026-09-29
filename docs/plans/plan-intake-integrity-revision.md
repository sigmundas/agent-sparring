# Plan-intake integrity revision plan

> Historical design record, moved out of the README. Its status lines describe
> the state when it was written, not a promise about the current code; see
> [plan intake](../intake.md) for the behavior that exists.

This is the in-place revision of the existing plan-intake design, not a
second feature plan. Inspection found no standalone plan-intake plan in the
repository: this section and `intake.py`'s module documentation hold its
design. The unrelated, untracked `docs/plans/backend-selection.md` is outside
this work. Status: **Stage A implemented as a review candidate; Stages B and
C pending**. Baseline:
`0d7e6f8` → `e99b1d2` → `77fe125` → `20ff521` → `e23af2b` → `1e8de40`.
The base is `feature/ask-sparrer-dialogue`; that branch **must land first**.
Keep reusing its public `repo_fingerprint`; do not duplicate it.

**Verified diagnosis.** Source inspection at `1e8de40` confirms the ten
reported failure mechanisms. This revision does not claim to have rerun the
reviewer's 16 probes or the reported 70/124/927-test results.

| Finding | Evidence in the current implementation | Required correction |
|---|---|---|
| 1 / A12: approval does not bind execution | `approve_plan` writes `approval.json`; `manifest.load_manifest_source` only parses the supplied file. `plan._verify_plan_unchanged` binds content at run start, not at human approval. | Verify a protected approval before the first provider call and on continuation. |
| 2: later proposal rewrite | `approve_plan` trusts digests, source path, label and run keys from mutable `intake.json`; it never compares the reviewed report. Coordinated edits can satisfy those checks. | Protect the review candidate before any slice runs, then seal each approval outside the worktree. |
| 3 / A1, A5: coverage is not ownership | `check_interpretation.cover` unions ranges; no conflicting-owner check exists. | One deterministic disposition per substantive line. |
| 4 / A2, A16: gate topology | `after_stage` detects an interior boundary, but `run_prerequisites` uses only `blocks_stages`. An empty block list need not protect later slices. | Derive prerequisites from one execution graph. |
| 5: hidden omissions | `render_report` prints gate/exclusion ranges without their source excerpts; an exclusion needs only a nonempty reason. | Show exact removed/deferred text and require a meaningful disposition decision. |
| 6 / A4: context swallows work | Attached context contributes coverage regardless of its source heading or owning stage. | Preserve heading ownership; promotion requires an amendment. |
| 7 / A7, A8: prerequisites are assertions | Ordering uses interpretation-list positions; approval accepts string confirmations without earlier receipts or completion. The report supplies those strings. | Bind prerequisites to engine receipts and appropriate completion/evidence. |
| 8 / A9, A10: repository drift | `prepare_plan` discards before/after fingerprints. Approval checks no current repository snapshot; `expected_branch` takes precedence over the interpreted value; sibling mappings are not compared to inspected repositories. | Persist and verify repository identities and snapshots; forbid silent overrides. |
| 9 / A13: mode changes roles | Parsing limits `mode` to known values, but no source-derived check governs which one the agent chooses. Approval copies it into the manifest. | Bind mode to authored semantics or an explicit reviewed change. |
| 10 / A14: competing semantic authority | `render_brief` appends narrowing prose beneath a source-wins preamble, without validating references. `good_interpretation()` names undefined gate `repair-run`. | Remove semantic `intake_scope`; use explicit amendments. |

Also verified: stage ranges render in supplied order; ranges address the
original source even when an amendment exists; `SourcePlan.lines` and prompt
numbering use `splitlines()`; source label/path come from the mutable record.
These belong in the same identity/ownership corrections, not separate patches.
`scratchpad/probe_intake.py` and the `scratchpad/` directory are absent in
this checkout. Recover the original probe if available; otherwise reconstruct
the documented attacks and explicitly record which original cases are unknown.
Do not invent meanings for A3, A6, A11 or A15 from their numbers.

Preserve strict parsing, re-derived findings and briefs at approval,
amendment-presence derivation, on-disk plan checking, the source reread after
the genuinely read-only provider turn, idempotent approval, duplicate-id
checks, `gate_inside_run`, primary-repository binding, and intake gitignore
enforcement. Keep intake semantics in the engine; the provider addition stays
the generic `start_structured(prompt, output_schema)`.

**Authority and scope.** The human approves semantics; the engine binds that
decision to bytes, repository identity and state. A hash stored beside
agent-writable data is not a trust anchor. The flow is:

```text
source → read-only intake → proposal → deterministic validation
       → protected review candidate → exact human review
       → sealed slice approval → manifest → verified execution
```

After approval, changing any worktree proposal material must either have no
effect on execution or cause refusal. The ignored intake directory becomes
a disposable review/export cache. It must never authorize execution or the
approval of a later slice. Deterministic validation cannot prove the meaning
of arbitrary prose; unresolved semantics must be visible to the human, not
silently declared safe by coverage or by another model.

**Stage A — Seal review, approval and execution.** Implemented as a review
candidate on `feature/plan-intake`, under the trust decision recorded after
this stage's exit criteria; items 1, 3, 5, 6 and 7 are narrowed by it as
recorded there.

1. Introduce a small, versioned engine approval store **outside all inspected
   worktrees and provider-writable roots**, using the platform user-state
   directory. Store immutable review candidates, slice receipts, gate evidence
   and the exact approved manifest bytes; worktree files are exports only.
   Register these artifacts and their sole engine writer in
   `artifact_ownership.py`. Existing `.sparring/plans/` and stage state remain
   the runtime lifecycle model; do not replace the runner with a new scheduler.
   Filesystem location or an ownership prompt alone is insufficient: prove
   the provider sandbox excludes the store and cannot invoke a privileged
   approval-writing path. Refuse sealed execution for providers/configurations
   that cannot enforce this. The threat model includes a managed agent with
   worktree write access, not a malicious OS administrator or arbitrary
   unsandboxed process acting as the human.
2. At prepare, persist a protected candidate containing source bytes and
   canonical path/label, interpretation, amendment, disposition/graph,
   repository snapshots, run/stage identities, rendered report and briefs.
   Its engine-computed identity covers all review-relevant semantics and the
   renderer/schema version. Persist this before exporting the report. Approval
   loads this candidate by engine identity, never discovers authority from
   `intake.json`. Re-render and compare; refuse modified exports rather than
   approve a different interpretation behind an old report.
3. Make approval a review-and-decision operation over that exact protected
   candidate: present the selected slice, complete semantic report and exact
   briefs before collecting a deliberate human decision tied to its identity.
   A noninteractive UI must obtain the same engine-issued review identity and
   submit the decision against it. No user-maintained hashes. Fail if the
   candidate changes between review and decision. A later slice reviews the
   same protected graph or an explicitly linked successor, never an edited
   worktree copy. Declining or interrupted review creates no approval.
4. Seal a receipt atomically with at least: schema version, candidate/intake
   identity, source/interpretation/amendment identities, slice and run key,
   raw manifest-byte digest, existing semantic manifest digest, repository
   identities/snapshots and expected branches, prerequisite receipt identities,
   review identity, human decision and timestamp. Include exact payloads in
   the store; do not depend on external files remaining readable. A receipt
   ID is an engine reference, not authentication by itself. Validate records
   against protected storage, serialize competing approvals, and make retries
   identical or refuse; never overwrite an earlier decision.
5. Persist logical name, canonical worktree path, Git/common-directory identity,
   branch, HEAD and the shared `repo_fingerprint` for **every** inspected repo,
   including context-only ones. The helper returns branch/HEAD/dirty paths,
   not content hashes. For this first version require clean inspected repos;
   retain before/after checks and explicitly document that ignored material
   and modify-then-restore activity are not attested by this helper. Inputs
   used as execution authority must be captured separately. Approval compares
   all snapshots and source bytes. Unknown repos, path substitutions, branch
   changes and new commits refuse. No drift override in the initial version.
   An intended run branch differing from the inspected branch must be checked
   out and re-prepared; an approval argument cannot silently replace it.
6. Later slices legitimately see changes from earlier work. Refresh repository
   observations through an explicit new review candidate linked to its parent,
   preserving the original source/graph identity and prior receipts where
   unchanged. Show the snapshot delta and re-review the remaining slices.
   Re-run read-only intake when changed evidence affects interpretation; never
   auto-rebase approval onto a new HEAD. Prerequisite receipts must match the
   referenced graph node/revision; changed nodes invalidate dependent approval.
7. Replace free-text prerequisite satisfaction with protected references.
   Slice B needs A's approval receipt **and** engine-recorded successful
   completion/accepted candidate evidence when B depends on A's work; approval
   alone does not mean execution happened. Capture the completion attestation
   through existing acceptance hooks into the protected store, rather than
   trust later edits to worktree state. A manual/production gate requires a
   separate human evidence decision recording the exact gate, candidate/repo,
   actor, time and evidence (e.g. release reference and observation). This is
   an honest human attestation, not a claim that the engine verified production.
   Reuse existing typed human-evidence conventions where practical; no arbitrary
   confirmation string or prefilled command may satisfy these prerequisites.
8. Verify the receipt, exact manifest bytes, run/stage identities, branch,
   repository bindings and prerequisites at `run-plan` entry **before any
   provider runs**, including direct engine entry points and adoption paths.
   Parse and execute the verified in-memory bytes to avoid check/read races.
   First execution rechecks the approved starting snapshots. Pin the receipt
   in run state and recheck its authority on resume, acceptance and advancement.
   Subsequent engine-accepted commits follow existing candidate/branch guards;
   do not compare every resume to the original HEAD and reject normal progress.

Compatibility: ordinary hand-authored v1 manifests and Markdown plans remain
usable without intake approval; their current digests and resume behavior
remain unchanged. Add a versioned intake manifest envelope/reference handled
by the loader, retaining the existing stage payload. Register protected intake
run and stage identities so deleting the envelope, changing the path, omitting
the receipt or invoking adoption cannot downgrade that execution to legacy
mode. A fully rewritten hand-authored plan with new identities is a separate
human-authorized legacy execution, not an intake-approved run. Do not claim
to infer provenance from arbitrary equivalent prose. Test this boundary.

Already-generated intake folders and their adjacent approvals are **unsealed
legacy proposals**, never upgraded by trusting their self-reported hashes.
Require fresh prepare/review/approval; identify legacy intake paths and refuse
their apparent approval at the CLI. Arbitrary copied old v1 files cannot be
distinguished retrospectively from hand-authored v1 manifests; document that
limitation explicitly. Existing legacy runs retain legacy guarantees and must
not be relabelled as sealed. The prior promise that `run-plan` and manifest
format need no change is withdrawn for new intake-produced manifests.

Recovery: atomically publish complete bundles with an index rebuildable from
protected records, and retain append-only decision/transition history. Back
up the store outside provider write access, including candidates, manifests,
receipts, evidence and identity reservations. Restore from that trusted backup
with integrity checks; never reconstruct approval from the ignored directory.
Missing/corrupt authority fails closed. Without a trusted backup, require new
human review and a successor receipt; do not fabricate historical approval or
completion. Cross-machine migration and signed portable receipt exchange can
be deferred; local backup/restore and crash recovery cannot.

Stage A exit: proposal/report/receipt/manifest tampering cannot affect approved
execution; first-start and continuation checks hold; legitimate progress and
later-slice snapshot refresh work; legacy behavior remains covered. Implement
against `intake.py`, CLI, manifest/plan entry points and ownership/state hooks,
without putting feature semantics in the provider adapter.

*Stage A trust decision (2026-09-27, project owner).* Item 1's provider
isolation cannot be enforced as built: the Claude stage agent runs as the
user with the user's own Claude permissions (typically unrestricted shell
and file writes) and no OS sandbox, and it commits and pushes, so a store
outside the worktree is as writable to it as the worktree. The owner
decided that plan intake v1 approval is a **workflow-integrity guarantee
against accidental drift, not a security boundary** against an agent
deliberately invoking `approve-plan` or rewriting approval state -- the
same scope as an agent attacking the engine itself. No Claude sandbox,
separate OS identity, signing key or external protected store is to be
added. Under that decision Stage A implements (see "What `run-plan
--manifest` verifies" above): approval bound to the exact source,
snapshot, intake record, interpretation and rendered report; an intake
manifest envelope refused by the plain reader and runnable only through
its `approval.json`, verified by raw bytes and semantic digest before any
provider and on resume/accept/advance; the run pinned by source kind and an
approval-covering digest; intake identities refused as plain runs;
repository snapshots of every inspected repository, with no drift override
and no approval-time branch replacement; earlier-slice prerequisites proven
by that slice's approval and completed sealed run, not by name;
exclusive, idempotent, never-rewritten approvals; and version-1 intakes and
approvals refused.

Narrowed or deferred in Stage A, deliberately:
- *Item 1:* the approval lives in the git-ignored intake directory, not an
  external store; no provider-isolation proof is claimed.
- *Item 3:* the human decision is the explicit `approve-plan` command over
  the exact `report.md` intake rendered (re-rendered and compared); there
  is no separate interactive review step or engine-issued review identity.
- *Item 5:* inspected repositories need not be clean; dirty paths are
  recorded, and branch/HEAD/path identity are what is compared.
- *Item 6:* no parent-linked successor candidates. The only allowed
  movement is to a commit an earlier slice of the same intake was accepted
  at; other drift requires a fresh prepare, which mints new identities, so
  a later slice whose earlier slice ran under a previous intake cannot yet
  be approved from the new one.
- *Item 7:* gates remain confirmed by id (a recorded human attestation);
  typed gate evidence (actor, candidate, release reference) is Stage C.
- *Recovery:* approvals are written atomically and exclusively; a missing
  or corrupt approval fails closed and needs a new approval. There is no
  backup/restore tooling or append-only decision history beyond the
  never-rewritten approval file.
- A managed stage agent that edits the source plan or intake files mid-run
  stops the run, as editing a Markdown plan's stage sections already does.

**Stage B — Make source ownership and the graph unambiguous.** Pending.

1. Replace coverage union with a derived line-to-owner index: each substantive
   line has exactly one stage-body, context, gate or exclusion owner. Reuse
   range objects; a new enum is optional. Reject every overlap, including
   duplicate executable ownership and overlapping ranges within one owner.
   Shared context has one owner and multiple attachments, not multiple owners.
   Canonically sort ranges by source position; range reordering cannot change
   a brief. Include the disposition and attachments in candidate identity.
2. Recognize explicit stage-heading spans deterministically, including labels
   `0`, `1A`, `1B`; ignore headings inside fenced examples. In faithful mode,
   stage text, tasks and acceptance criteria stay with that source stage.
   Moving to another stage, gate, exclusion or context is a structural change,
   not a range-selection trick. Ambiguous or unsupported heading ownership
   blocks faithful approval and asks for clarification/refinement. Context
   comes from declared contextual sections (principles, constraints, approved
   decisions, background, definitions); headings alone do not prove harmless
   semantics. Quote all context and show its consumers for human review.
3. In refine mode the amendment is the sole semantic transformation. For the
   smallest coherent first version, retain the existing apply-and-reprepare
   path: propose exact amended source plus a structural diff; the human applies
   it, then prepares/reviews that new source. Never execute an interpretation
   using amended coordinates against original bytes. Link the new candidate
   to the prior source/amendment for provenance. Remove `--without-amendment`
   as a way to execute structural changes against old text. Remove executable
   `intake_scope`; explanatory rationale may remain in the report, never as
   competing instructions in a brief. Splits, task-to-context promotion,
   exclusions of stage-owned requirements, deferrals and role changes must
   appear in the amended source and its review.
4. Build one graph from ordered source stages and explicit boundary gates.
   Default to source order, including across slices. A gate after A is inserted
   after A's slice and is a prerequisite of all subsequent slices in this
   conservative initial model. Derive downstream prerequisites transitively;
   remove independently authoritative `blocks_stages` (or accept it only as
   redundant input that must exactly match derivation). A pre-first gate
   explicitly precedes the first slice. A gate with no downstream work must
   be explicitly terminal verification, with durable evidence required before
   the whole intake is considered complete; otherwise reject it. Validate
   every deferral target, unknown edge and cycle. No gate may be inside a
   slice. Prose dependencies must be shown against graph edges; unresolved
   conditional language blocks review until made explicit. Parallel/nonlinear
   scheduling can wait; deleting `depends_on` must not erase source order or
   gate consequences.
5. Default authored stages to implementation. A review-only lifecycle needs
   an explicit, engine-recognized source declaration; ambiguous review prose
   is not permission to skip implementation. Any role change requires an
   amended declaration and visible review. Render exactly which roles run,
   and bind mode in candidate, receipt and manifest identities.
6. Use one line-coordinate contract for prompting, validation, diffs and
   rendering: LF-delimited lines with explicit CRLF handling and raw source-byte
   hashing. Reject unsupported control/Unicode line separators with a useful
   diagnostic rather than silently changing editor-visible numbering. Compare
   source excerpts exactly under that documented contract. Reject empty or
   punctuation-only exclusion reasons, but do not confuse a longer reason
   with semantic correctness: exclusions require exact-text human review.

Stage B exit: overlap, hidden movement, dangling gates, mode swaps and
ambiguous scope all block before sealing; faithful examples retain their
requirements and refine transformations are visible in the amended source.

**Stage C — Make review usable and prove the boundary.** Pending.

1. Render the report from the protected candidate. Quote gate, exclusion and
   context text verbatim with source coordinates and owning stage before/after;
   show exact briefs, modes/roles, repository snapshots, prerequisites and
   evidence state. Escape/nest source headings so quoted prose cannot masquerade
   as engine decisions. Show a derived semantic summary before exact detail:
   original stages affected, executable stages, gates, repository splits,
   manual pauses and reorders. Counts come from the transformation, not the
   model's narrative. Show terminal obligations and unresolved decisions.
   No prefilled confirmation/evidence values; command hints identify missing
   evidence without asserting it exists.
2. Convert the available adversarial cases into durable regression tests using
   real prepare → review/approve → run flows with deterministic provider fakes.
   Track A1–A16 against the recovered probe or documented reconstructions;
   unknown cases remain explicit, not falsely marked covered. Cover coordinated
   interpretation/digest rewrites after slice 1, stale reports, source-path and
   label substitution, stage/gate/exclusion overlaps, context swallowing,
   empty gate effects, removed dependencies, prerequisite fabrication,
   branch/HEAD/path drift, edited approved briefs, stage-mode flips and dangling
   `repair-run`. Fix the permissive good fixture during implementation, not in
   this plan-only pass.
3. Add invariant-oriented mutation/generated tests: (a) every agent-writable
   proposal mutation is inert or refused after approval; (b) any differing
   manifest bytes, even a semantic-equivalent serialization, cannot use the
   same receipt; (c) each substantive line has one non-conflicting disposition;
   (d) a changed disposition changes identity and visible review;
   (e) downstream work needs verifiable upstream approval and required evidence;
   (f) repo/branch/HEAD drift cannot pass silently; (g) mode changes cannot
   silently change agent roles. Include cache deletion, symlinks/path aliases,
   stripped envelopes, wrong run keys, direct API/adoption/resume paths,
   concurrent approval, crash/restore and candidate changes during review.
   Assert refusal happens before a provider invocation.
4. Retain existing regression coverage for idempotence, strict parsers,
   read-only fingerprinting/source reread, primary binding, coverage gaps,
   gate boundaries and ordinary manifests. Run focused intake/manifest/state
   tests, then the full suite because execution authority changes. Record
   actual commands/results; prior pass counts are historical evidence only.
5. Re-dogfood taxonomy-v3 in faithful and refine modes: all 290 substantive
   lines in the referenced version must have owners, contextual sections stay
   visible, labels and repository splits survive, production actions become
   enforced gates, and unsafe faithful interpretations refuse. Check the
   proposed `2A/2B`, `3P/3W`, `4A/4P/4W`, `6P/6W`, Gates A–E and stale factual
   claims against the actual source version; do not hardcode these outcomes
   into the engine. Record source digest and semantic-review evidence.

**Merge and deferral decisions.** All three stages, their exit checks, and a
fresh independent integrity review block merge, as does the parent-branch
dependency. Implementation and final independent review remain separate
sessions. Update this plan with evidence on each pass. This revision changes
documentation only; its validation is diff inspection and `git diff --check`.

Defer adaptive runtime scheduling, automatic graph replanning, nonlinear gate
topology, dirty-repository snapshot support, silent/automatic drift rebasing,
portable signatures and cross-machine approval sharing. Preserve compatibility
with future adaptation through immutable candidate IDs, parent-linked reviewed
revisions and append-only decisions; never rewrite old approval history.

No product choice requires human input to start implementation: this plan
chooses conservative refusal, clean snapshots, external protected state and
apply-and-reprepare amendments. Stage A must prove the sandbox/storage boundary
before claiming integrity; if the supported provider cannot isolate engine
authority, stop and bring the concrete limitation back for a deployment/trust
decision. A directory outside the worktree is not by itself that proof.
