# Reference: cross-repository candidates, review-only stages, artifact ownership

[← Documentation index](README.md)

## Cross-repository candidates

Some stages are genuinely two repositories: a desktop change here and a
coupled migration there, and the sparrer's `READY` depends on both. Pinning
only the primary commit would let the sibling move between review and
acceptance, so the stage would claim a candidate set that no longer exists.

A stage may therefore declare sibling repositories. The declaration belongs in
the plan input — a manifest stage's `repositories` — and the plan runner
writes it into that stage's `state.json` before anything runs, so the
standalone `freeze-candidate`/`accept-candidate` commands see the same
complete candidate set:

```json
"repositories": [
  {"name": "sporely-web", "path": "../sporely-web-worktree",
   "branch": "feature/cloud-transport", "candidate_sha": null}
]
```

(That is also the on-disk shape, but treat it as the engine's record rather
than as something to maintain by hand: declare it where the stage is defined,
and let the run write it. The VS Code extension has a command for this.)

`freeze-candidate` then treats each sibling exactly as it treats the primary
repository — right branch, clean worktree, commit pushed and reachable — and
records the resolved commit as the pin. `accept-candidate` re-verifies every
pin: a sibling that moved refuses acceptance as stale, and a dirty sibling
refuses it too. `path` is resolved against the primary repo root when
relative; a declared `candidate_sha` is an assertion, and freezing refuses if
the sibling is not exactly there.

That is the whole feature. There is no cross-repository merge, no
transaction and no remote coordination — the requirement is only that
acceptance pins and verifies the complete reviewed candidate set.

## Review-only stages

A plan's last stage is often not work at all: a fresh independent reviewer
verifies the candidates the earlier stages accepted, checks every gate, and
the plan's activation decision is taken on that. Run through the ordinary
lifecycle, such a stage gets an implementation agent that has nothing to
implement — it opens a session, reads around, and sooner or later writes
something to try an idea out, at which point the reviewer of the work is
also its author.

So a stage can declare what it *is*, in its manifest entry:

```json
{"stage_id": "stage-5-independent-final-review", "label": "Stage 5",
 "title": "Independent final review and activation decision",
 "brief": "…", "mode": "independent_review"}
```

`mode` is optional and defaults to `implementation`, so every manifest
written before it existed means what it always meant. Nothing infers it:
a stage titled "Independent final review" runs the implementation lifecycle
unless its manifest says otherwise, because which agent runs is not a thing
to read off a heading. The Markdown convention has no way to declare a mode
and always means `implementation`.

A review-only stage's lifecycle has no implementation turn in it:

    enter the stage → pin exactly what is under review → one fresh
    independent reviewer → READY / NEEDS_YOU / defect

All four outcomes are terminal. `SEND_BACK` is a **defect report**, not a
correction cycle: there is no stage agent behind this stage to send work
back to, so the plan stops, unaccepted, and you decide where the fix belongs
— a new stage, a reopened earlier one, or outside the plan. Nothing turns
the review stage into an implementation stage on its own. `NEEDS_YOU` is the
ordinary structured gate: `resume-plan --evidence` records your answer and
the *same* reviewer session judges it.

What the reviewer is told is assembled for the job. There is no `handoff.md`
to show it (no stage agent ran), so in its place the engine states the exact
accepted candidate commits, per stage, across every declared repository —
verified against the repositories immediately before the turn. The reviewer
is told plainly that it has no write access and that a defect it finds is
not its to fix. Its captured prompt is recorded under the `reviewer` role,
so `prompts/0001-reviewer-original.md` is what you read to see whether the
active actor really is a fresh independent reviewer.

Completion does not manufacture a commit. What the stage is judged against
is pinned into its `state.json` when it is *entered* — `base_sha` is the
primary commit, `repositories` the sibling candidates — and READY re-verifies
exactly that set: right branch, HEAD still at the reviewed commit, worktree
clean, the commit still on the remote, every sibling still at its pin. Only
then is the stage ACCEPTED, with `candidate_sha` equal to `base_sha`: the
accurate statement that this stage added no commit of its own. The
implementation path's `freeze-candidate`/`accept-candidate` is untouched and
is not reachable from review mode, nor the other way round. Nothing is
merged, here or anywhere else.

### Restarting a stage started under the wrong mode

A stage can end up having run through the wrong lifecycle: the run reached it
before the mode was declared, or against a plan input that did not carry it.
It then holds an implementation session, possibly something the stage agent
left in the worktree, and no independent review — and none of that can
become the authoritative review. The engine refuses to continue such a stage
rather than adopting that attempt, and names one command:

```sh
sparring reset-stage stage-5-independent-final-review \
  --manifest .../manifest.json --mode independent_review \
  --repo-root . --expected-branch feature/x
```

It archives the attempt to
`.sparring/stages/.archive/<stage-id>/<n>-<mode>-<timestamp>/` — the stage
directory, its captured prompts and its session ids all preserved as
history, and stripped of their authority, since nothing resumes an archive.
Files the attempt wrote are read from its own activity log and quarantined
into the same archive (including a compiled `__pycache__` copy of a test
file it created, which git never showed you and a later test run would still
import). Then the stage is recreated in place, under the same id, with the
plan input's brief and a fresh `state.json` — no session, no candidate — and
the run's plan digest is re-recorded, since declaring a mode changes what
the plan executes.

What it refuses, before moving anything: a stage that is not the run's
current stage, or is accepted; a plan input that declares the mode the stage
already ran under, or disagrees with `--mode`; a preceding stage that is not
accepted with a real candidate; a repository that is not on the expected
branch and at the preceding accepted candidate; an attempt that modified
tracked, committed content; and any dirty path it cannot account for — your
uncommitted work is never swept aside to make a recovery possible.

## Who owns which artifact

Every file a run reads or writes has exactly one authority. Paths are
relative to the project's `.sparring/` directory, except the plan document
itself, which is an ordinary repository file.

| Artifact | Owner | Provider-writable? | Lifetime | Purpose |
| --- | --- | --- | --- | --- |
| the plan document (`docs/plans/.../<plan>.md`) | human / repository | no | immutable for the lifetime of a managed run | the run's execution definition |
| `PROJECT.md` | human / repository | no | edited between runs by a person | project context embedded in every prompt |
| `project.toml` | human / repository | no | edited between runs by a person, by hand or through `sparring set-config` on their behalf | provider selection and engine configuration (model and effort are the person's own preferences, outside the repository) |
| `plans/<run>.json` | engine | no | rewritten on every position/status change | the run's position, expected branch, plan digest, recorded push authorization and typed pause |
| `intake/<intake>/` | engine | no | written once by `prepare-plan` (the intake agent's answer is its structured result); never rewritten | a reviewable interpretation of a human plan; approval binds its exact bytes, so editing it refuses an approved run |
| `intake/<intake>/runs/<slice>/` | engine (`approve-plan`, on a person's decision) | no | `manifest.json` replaceable until `approval.json` exists; `approval.json` created once, never rewritten | the intake manifest envelope and the approval `run-plan --manifest` verifies before running it |
| `intake/registry/<run>.json` | engine (`approve-plan`) | no | written at approval in the project that runs the slice | the run key and stage ids an approved slice owns, so a plain run cannot reuse them |
| `stages/<stage>/brief.md` | engine (a person, for a hand-written stage) | no | generated from the plan section, or hand-written before execution; then immutable | what the stage is reviewed against — the plan section verbatim in a managed run |
| `stages/<stage>/notes.md` | engine (and a person editing by hand) | no | skeleton at creation, then appended to by section | a human's recorded answer or check results (`## Human evidence`) |
| `stages/<stage>/handoff.md` | engine | no | regenerated in full by every implementation turn | that turn's claims, git identity and evidence, for the sparrer |
| `stages/<stage>/sparring.md` | engine | no | rewritten in full by every sparring exchange | the latest verdict, rendered from the structured routing result |
| `stages/<stage>/state.json` | engine | no | rewritten on every lifecycle change | status, candidate identity, provider session ids |
| `stages/<stage>/dialogue.jsonl` | engine | no | append-only; one record per question and answer | provenance for the read-only conversation a person holds with the reviewer (`sparring ask`) |
| `stages/<stage>/activity.jsonl` | engine | no | append-only, never read by orchestration | observational telemetry only |

**No artifact is provider-writable, including `notes.md`.** Despite its
`## Implementation notes` template heading, nothing asks a provider to open
it: an implementation turn's claims, evidence and deferred checks reach the
sparrer because the engine captures that turn's *result* into `handoff.md`,
and a human's answer reaches both agents because `resume-plan --evidence`
records it under `## Human evidence`. A provider's designated output is its
own reply, which the engine records; the files are how the engine keeps it.
`src/agent_sparring/artifact_ownership.py` is the production declaration the
prompts and tests are built on, and is also where the sentence the providers
are told it in lives, so the stage and sparring prompts cannot drift apart.
The table above is the human-readable summary of that declaration; nothing
generates or checks it, so the two are kept consistent by hand.

**The plan is immutable while a run executes.** The engine digests the plan's
stage sections at run start and re-reads the document and re-checks that
digest on resume, before accepting a candidate, and before advancing. An
edited plan therefore stops the run rather than becoming its new definition,
and so does a plan that can no longer be parsed as the one it started as —
appending an `# Implementation record` whose own `## Stage 1` heading follows
the plan's `Stage 1..3` is exactly that case. Nothing is ever reverted for
you: restore the document, or deliberately start the run over.

A consuming project's agent instructions may well tell agents to keep the
active plan updated with their progress, which is right everywhere except
inside a managed run — so the restriction travels with the managed prompt
rather than depending on the project's own wording.
