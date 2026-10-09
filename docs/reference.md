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

## Whose turn it is: `next_turn`

Each stage's `state.json` records whose turn it is, written only by the
engine and only after the preceding operation fully succeeded:

| `next_turn` | Written when | Ordinary resume does |
| --- | --- | --- |
| `stage` | a full cycle begins; a SEND_BACK verdict is recorded | an implementation turn |
| `sparring` | an implementation turn, its handoff and its session are all recorded | a review of exactly `next_turn_candidate`, no implementation turn |
| `finalization` | a READY verdict is recorded, with the candidate it was given over | see below |

`next_turn_candidate` pins what a `sparring` marker refers to: HEAD, whether
the candidate is a `commit` or still partly in the `worktree`, a digest of
the candidate content (stage artifacts excluded), and every declared sibling
repository's HEAD; a declared sibling that cannot be read refuses the
capture instead of being pinned as unknown. It also records, separately,
the digest of the tracked content alone (`tracked_digest`) and each
untracked candidate path with its blob (`untracked`); these refine the
identity for recovery and take no part in matching it. Before such a review
starts the engine re-reads all of them, and any difference is refused with both identities named; it never
falls back to running the other agent. A failed provider turn never advances
the marker, and NEEDS_YOU / ESCALATE leave it where it is -- the recorded
gate decides what happens next.

Untracked, non-ignored files are part of the candidate exactly as the
reviewer sees them; they are never excluded from candidate identity. So
before any reviewer turn (`run-plan`, `resume-plan`, `run-loop` and
standalone `run-sparring`) the engine refuses while the worktree holds
untracked paths the stage's implementation did not produce, naming them and
saying to remove, commit or ignore them. The check runs before any review
marker, pinned candidate or `next_turn_resolution` is written (including a
derived or `--next-turn sparring` resolution), so a refusal records nothing
for the review; the CLI prints the exact command to rerun, repeating an
explicit `--next-turn sparring` that was refused before it was recorded and,
for `run-sparring`, the reviewer options given. "Did not
produce" is decided from engine records only: present before the stage's
first implementation turn (`untracked_baseline`, recorded with `base_sha`),
or not among the exact untracked paths the last successful implementation
turn left (`untracked_produced`, file by file). For a turn recorded before
`untracked_produced` existed the handoff's working-tree list stands in, but
a collapsed `dir/` entry vouches for no file under it. Untracked files the
implementation created stay part of the candidate.

When the refusal follows a completed implementation turn, `next_turn` stays
`stage`, but that turn's own structured record says a review is owed:
`implementation_unreviewed`, set once a successful implementation turn and
its handoff are both recorded, and
cleared by every later marker write (the review pin, a SEND_BACK or READY
verdict, a reopen, the next cycle start). A SEND_BACK therefore always owes
an implementation turn, however its prose reads. Once the files are removed, committed or
ignored, the same resume therefore goes straight to the reviewer, which
pins whatever the candidate then is -- no further implementation turn.

A `finalization` marker is matched against the candidate READY was given
over, by both `resume-plan` and `run-loop`: the same uncommitted candidate
gets the bounded commit/push turn; that exact content already committed (by
an interrupted commit turn, or by hand) and nothing uncommitted is re-marked
`sparring` and reviewed at that commit before acceptance; READY over a
`worktree` whose only difference from the current clean commit is that
previously reviewed untracked files were removed -- same HEAD, same sibling
HEADs, tracked content unchanged, no new untracked path, none modified -- is
likewise re-marked `sparring` and reviewed at that commit (a marker recorded
before `tracked_digest`/`untracked` existed qualifies on same HEAD, same
sibling HEADs and a clean worktree alone, and the resume says so); anything
else is refused, naming what changed (HEAD and the paths it changed,
sibling HEADs, new or modified untracked paths, uncommitted tracked paths)
and, from the CLI, the exact commands to continue (the same resume after
restoring, or `reset-stage` with the run's plan input, repository and
branch). READY over a candidate that was already a commit needs no commit
turn: if that exact commit (HEAD, content, sibling HEADs) is still the
candidate, `resume-plan` runs only the push and acceptance gates for it, with
no agent turn; if it has moved, the resume is refused. `run-loop`, which
never accepts, refuses it as owing no agent turn. A `finalization` marker
never leads to an implementation turn.
A standalone `run-loop` also refuses a recorded NEEDS_YOU / ESCALATE (running an
agent would not answer it) unless an implementation turn is already owed.
`resume-plan --evidence` answering a gate raised over a recorded candidate
(`next_turn = sparring`) is held to that candidate: if HEAD, the uncommitted
content or a sibling HEAD has moved since, the evidence is refused *before*
it is recorded, and nothing runs -- the reviewer never judges an answer
against different content. `--next-turn` cannot re-pin a recorded marker:
restore the recorded candidate, or, if the change was deliberate, choose how
to continue explicitly (for example `sparring reset-stage`). The one re-pin is
`--evidence ... --accept-advanced-head SHA`, answering a recorded NEEDS_YOU
whose branch was deliberately fast-forwarded beneath the uncommitted attempt
(a separately landed prerequisite): accepted only if `SHA` is a SHA prefix of
the current HEAD on the run's branch, the pinned HEAD is its ancestor, no
sibling moved, the untracked candidate files are unchanged, no commit in the
range touches an uncommitted path, and taking the commits back out
reproduces the pinned content and the pinned staged content exactly (a review
pinned before staged content was recorded is accepted only with nothing
staged). It is checked in full before anything is written, cannot be combined
with `--deferred-result`, and everything it writes before the first provider
turn is put back if the resume fails in-process before that turn. (A killed
process can leave the re-pin without its evidence note; the flag is then
refused as still pinned, and the evidence is recorded without it.) If the
reviewer turn itself fails after the re-pin, the evidence is already recorded
and awaiting review: resume without `--evidence`.
A resume without evidence over a recorded NEEDS_YOU / ESCALATE keeps that
pause and runs nothing, whatever the marker says; `--fresh-*` or `--next-turn` alone are
refused there, because neither answers a person. State with no marker keeps
the older evidence behaviour. `activity.jsonl` may mirror these decisions
but is never read to make them.

**State written before the marker existed** is reconstructed once, on the
first resume, from engine-owned records only: the prompt-capture index, the
handoff (its recorded candidate commit and the verdict it embeds), the
routing block of `sparring.md`, and the repository now. It derives
`sparring` only when the last completed implementation turn is later than
the last verdict and its candidate is still HEAD with no uncommitted
candidate content (the handoff records dirty path names, not bytes, so an
uncommitted candidate cannot be matched exactly and is ambiguous), and `stage` when no implementation turn completed
after the last SEND_BACK. Anything else -- for example HEAD having moved
past the handoff's candidate -- is refused with what was found; the caller
then chooses explicitly (`--next-turn stage|sparring` on `resume-plan` /
`run-loop`; `resume_plan(..., next_turn=...)`).
That choice is only for this ambiguous legacy state: it is refused, and
nothing is recorded, whenever a marker is already recorded (whatever its
value), when derivation gives an unambiguous answer (even the chosen one),
or while a recorded NEEDS_YOU / ESCALATE is waiting -- a choice never
overrides or re-pins the engine's record or answers a person.
The result is recorded with `next_turn_source` = `derived` or `manual` and
is never derived again. `next_turn_source` always means "who wrote the
current marker", so the next routine transition sets it back to `engine`.
How the legacy state was resolved is kept separately and immutably in
`next_turn_resolution`: `{turn, source (manual | derived), recorded_at,
reason}`, where `reason` is the refusal or derivation it answered. It is
written once and no later marker write alters or drops it.

## Resume, fresh session, reset

Three different things, from least to most disruptive:

| | Stage attempt | Candidate | Provider conversation | History and verdicts | Configuration |
| --- | --- | --- | --- | --- | --- |
| Resume | same | same | same, for both roles | kept | as locked at each conversation's first turn |
| Fresh session | same | same | new for the one role asked | kept; the new conversation is told where the stage stands | resolved anew for that role and recorded with its generation |
| `reset-stage` | new; the old one archived | starts over | new for both | archived with the old attempt | resolved anew |

- **Resume** (`resume-plan`, `run-loop`) continues the same stage in the
  same provider conversations, with the configuration locked at each
  conversation's first turn. It obeys `next_turn`.
- **A fresh session** (`--fresh-sparrer` / `--fresh-stage-agent` on
  `resume-plan` and `run-loop`, with `--fresh-reason`; in code
  `agent_sparring.sessions.start_fresh_session(stage, role, reason)`) discards one role's conversation and continues the *same*
  stage in a new one: a new *session generation*. Its configuration is
  resolved anew at its first turn, so a changed model, effort or provider
  preference (or an explicit override) takes effect there. Its first prompt
  is the full prompt plus the engine-owned record of where the stage stands:
  a fresh stage agent sees the latest sparring exchange and is told to
  address its SEND_BACK findings without redoing earlier work; a fresh
  reviewer sees its predecessor's exchange as historical evidence, to be
  re-checked independently but not silently dropped. Captured prompts show
  the turn kind `fresh`. Nothing else changes: candidate, base, branch,
  mode, brief, handoff, `sparring.md`, notes, deferred ledger, plan digest,
  sibling pins, freeze/accept state, prompts, activity history and
  `next_turn` are all left as they were, so a fresh session cannot reach
  acceptance without a normal READY review. It is the single path for
  discarding a session; nothing rolls over automatically today.
- **[`reset-stage`](#restarting-a-stage-started-under-the-wrong-mode)**
  archives the attempt and reinitialises the stage itself.

A new session's id is recorded the moment the provider announces it (Claude
and Codex both report it early), so a fresh conversation that fails before
its turn finishes is resumed next time, not replaced. A fresh independent
reviewer gets the same notice and history as a fresh sparrer. A role whose
configuration is pinned but which has never started a conversation has
nothing to replace, and a fresh session for it is refused. A pending fresh
generation is pinned at its own first provider turn, not when a loop builds
its adapters: if an owed implementation turn fails first, the fresh
reviewer stays unpinned and a later preference still applies to it. A fresh
session's first turn is captured as `fresh` even when it is also an
evidence, finalization or finalized-commit review.

On the command line the fresh flags apply to the stage the resume enters,
just before its agent turns and after every check that could still refuse
it; both roles are checked before either is changed. The role-scoped
overrides (`--stage-provider/-model/-effort`,
`--sparring-provider/-model/-effort`) choose the fresh generation's
configuration; for a role *not* given a fresh flag, an override that
conflicts with its active session is still refused. They are refused where
no agent turn is owed (an ACCEPTED stage, a READY commit waiting only for
push/acceptance, a recorded human gate without evidence) and
`--fresh-stage-agent` is refused on a review-only stage. The independent
review of a review-only stage is entered through `resume-plan`, so
`--fresh-sparrer` replaces its reviewer conversation there. Before launching,
`resume-plan` and `run-loop` print each role's resolved configuration: the
adapter (provider and executable), the configured model and effort with
their sources, which conversation and generation it continues (or that a new
or fresh one starts), the model the provider last reported for that
conversation (`not reported` if none; read from `activity.jsonl`, display
only), and the backend/account, which no adapter can read safely today and
is always `not reported`. Those two appear as the "provider-reported model"
and "backend/account not reported" lines. A model name says nothing about
routing: a `gpt-*` model does not mean the turn went through OpenAI or Azure,
nor a `claude-*` one through Anthropic -- the backend is whatever the
provider CLI is configured to use, which the engine cannot see.

`sparring usage` reports each role's
[session generations](stages.md) separately, so the cost of a replaced
conversation and of its fresh successor stay distinguishable.

**Known gap:** an ACCEPTED stage cannot be reopened. Findings from a review
after acceptance (an independent review-only stage, or a person) are fixed
by a follow-up plan, not by a fresh session or `--next-turn` on the
accepted stage; both are refused there.

### Pausing on provider session failures

Two provider failures are classified, from the provider's own error text,
and pause a managed run instead of failing it:

| Class | Recognised from | What to do |
| --- | --- | --- |
| `ProviderSessionUnresumable` | resuming a recorded conversation: Codex `invalid_encrypted_content`, thread/session not found; Claude "No conversation found" | continue in a fresh session for that role |
| `ProviderUnavailable` | quota, usage limit, rate limit, too many requests, overloaded (also a Claude turn that ends `is_error` for that reason) | retry later, or continue in a fresh session |

`resume-plan` raises `ProviderPause` (a `PlanRunError` carrying the role,
kind and whether the role has a conversation to replace) with the run
recorded as paused; the CLI prints the role, the provider's message and the
exact command to continue -- `… --fresh-sparrer --fresh-reason
session-unresumable`, or a plain retry plus a fresh-session alternative --
and exits 1. The command is shell-quoted and self-contained: absolute
`--sparring-dir`, `--repo-root` and plan/manifest path, the run key, branch,
and every provider/model/effort, executable, `--max-send-back-cycles` and
`--stop-after-stage` option the failed invocation was given (one-time
requests -- fresh flags, `--next-turn`, `--evidence`, push grants -- are not
repeated; they were already applied). Evidence whose reviewer turn failed
is not lost: the run state records it (`evidence_pending`, tied to the exact
`sparring.md` it answers), so the printed retry -- plain or fresh -- re-enters
that evidence turn over the same, re-verified candidate, for implementation
and review-only stages alike. Any new verdict retires it. A Claude error
result whose reason is only in its structured `errors` list is classified
the same way. `run-loop` prints the same. The engine never acts on it: no
retry, no session discarded, no candidate touched, `next_turn` unchanged. A
failure before the provider announced a session records no session id, so a
plain retry starts that (possibly pending fresh) conversation again. Any
other provider error still fails the run as before.

The same pause is recorded, read-only, in the plan-run state
(`.sparring/plans/<run>.json`) as `provider_pause`, so a client can show it
from authoritative data rather than from stdout:

```json
"provider_pause": {
  "kind": "session-unresumable",
  "role": "sparring",
  "stage_id": "…-stage-2-…",
  "has_session": true,
  "recorded_at": "2026-10-04T12:00:00Z"
}
```

`kind` is `session-unresumable` or `provider-unavailable`; `role` is `stage`
(implementer) or `sparring` (reviewer); `has_session` is false when that
role's current conversation never started; `recorded_at` is UTC ISO 8601.
Only the engine writes it, in the same save that pauses the run. It is
replaced by any other pause or failure (removed when that is not a provider
pause), removed when the run next starts running (plain resume or
`--fresh-*`), and left untouched by a refused resume -- one that stops
before any provider turn at the stage it describes (an inapplicable or
refused fresh session or next-turn choice, a moved or unreadable candidate,
a mode mismatch, an unusable review subject, unbuildable adapters) or that
adopts a recorded human gate without evidence. The
field is absent
otherwise. It is descriptive only: resume never reads it, and `next_turn`,
gates and candidate checks remain the only authority over what runs next.
Standalone `run-loop` has no plan-run state and records nothing; its printed
retry is the whole report.

### Recovery walkthrough

**The reviewer's conversation cannot be resumed.** `resume-plan` pauses with
`provider_pause.kind = session-unresumable`, `role = sparring`, and prints a
retry ending `--fresh-sparrer --fresh-reason session-unresumable`. Run it: a
new reviewer conversation (generation 2) reviews the same candidate, with
the earlier exchange as history, and the loop carries on as normal --
SEND_BACK goes back to the stage agent, READY to push and acceptance.

**The provider is unavailable** (quota, rate limit, overloaded). The pause
records `provider-unavailable`. Either wait and run the printed plain retry,
which continues the same conversation, or run the printed fresh alternative
to start a new conversation for that role (a different model or effort may
be chosen for it). A fresh session cannot switch provider today: each role
supports exactly one (`claude-cli` for the stage agent, `codex-cli` for the
reviewer), and any other `--stage-provider` / `--sparring-provider` is
refused. Moving to another backend or account is configured outside the
engine, in the provider CLI itself; the engine neither does nor sees it.

**A legacy run is stuck and HEAD moved outside the loop.** A stage recorded
before `next_turn` existed, whose handoff's candidate is no longer HEAD, is
refused on resume with what was found. Nothing has
changed. Decide yourself: `--next-turn sparring` to review the current
commit, or `--next-turn stage` for another implementation turn. The choice
is recorded as `manual` and never asked again.

A standalone `run-loop` refuses a legacy READY whose reviewed content is
still uncommitted: there is no pinned candidate to hold a commit turn to, so
resume it as a managed run (`resume-plan`), which finalizes it.

Generations are recorded as `sessions: {role: [...]}` in `state.json`, each
with its `generation`, `session_id` (null until the provider reports one),
pinned `agent`, `started_at`, `start_reason` (`initial` or
`fresh:<reason>`) and, once closed, `ended_at` / `end_reason`.
`implementation_session_id`, `sparring_session_id` and `agents[role]`
always hold the *current* generation's values.

### Compatibility of existing `state.json` files

`next_turn`, `next_turn_candidate` (and its `tracked_digest` / `untracked`),
`next_turn_source`, `next_turn_resolution`, `untracked_baseline`, `untracked_produced`, `implementation_unreviewed` and
`sessions` are all optional and absent until first written: an existing file reads back
unchanged and stays byte-identical until the engine writes the marker or
records a session. New stages record generation 1 (with `started_at`) at its
first pin or session id; a recorded session id with no `sessions` list is
generation 1 with `started_at` unknown, materialized when the list is first
written.
A stage that has had any session generation cannot change its mode, exactly
as one with a recorded session could not before.

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
| `intake/<intake>/decisions.json` | engine (`start-plan --answer`, on a person's answers) | no | written once at prepare time, inside the write-once intake; the answered intake is never written | a person's answers to an earlier intake's decisions, verbatim; `intake.json` records its digest, so approval seals it and `run-plan` re-checks it |
| `intake/<intake>/runs/<slice>/` | engine (`approve-plan`, on a person's decision) | no | `manifest.json` replaceable until `approval.json` exists; `approval.json` created once, never rewritten | the intake manifest envelope and the approval `run-plan --manifest` verifies before running it |
| `intake/registry/<run>.json` | engine (`approve-plan`) | no | written at approval in the project that runs the slice | the run key and stage ids an approved slice owns, so a plain run cannot reuse them |
| `stages/<stage>/brief.md` | engine (a person, for a hand-written stage) | no | generated from the plan section, or hand-written before execution; then immutable | what the stage is reviewed against — the plan section verbatim in a managed run |
| `stages/<stage>/notes.md` | engine (and a person editing by hand) | no | skeleton at creation, then appended to by section | a human's recorded answer or check results (`## Human evidence`) |
| `stages/<stage>/handoff.md` | engine | no | regenerated in full by every implementation turn | that turn's claims, git identity and evidence, for the sparrer |
| `stages/<stage>/sparring.md` | engine | no | rewritten in full by every sparring exchange | the latest verdict, rendered from the structured routing result |
| `stages/<stage>/state.json` | engine | no | rewritten on every lifecycle change | status, candidate identity, whose turn it is, provider session generations |
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

## Managed-run JSON

Every payload carries `schema_version` (currently `1`); a client refuses a
version it does not know. Behavior: [plans.md](plans.md#managed-runs).

**Record** — `<git-common-dir>/agent-sparring/worktrees/<run-key>.json`.
Fields other than `events` never change; lifecycle is derived from the
append-only events (`created`, `merged` `{mode, target_sha}`,
`target_pushed`, `state_archived`, `worktree_removed`, `branch_deleted`,
`finished`; no `created` event means `creating`).

```json
{"schema_version": 1, "run_key": "…", "plan_label": "docs/plans/foo.md",
 "input": {"kind": "markdown | manifest", "path": "/abs/path"},
 "worktree_path": "/abs/<repo>-sparring-<run-key>", "branch": "sparring/<slug>-<hex>",
 "target_branch": "main", "base_sha": "<40 hex>", "remote": "origin | null",
 "created_at": "<UTC ISO-8601>", "created_by": "engine",
 "events": [{"at": "…", "event": "created", "detail": {}}]}
```

**`runs --json`** — `{"schema_version", "runs": [run]}`, each run:
`run_key`, `plan_label`, `managed: true`, `worktree_path`,
`worktree_exists`, `branch`, `target_branch`, `base_sha`, `created_at`,
`lifecycle` (`creating | creation_failed | created | merged |
worktree_removed | finished`), `run_status` (the run state's status, or
`missing` / `unreadable`), and the `git` and `finish` objects below.

**`git`** — `head`, `branch_tip`, `clean`, `final_candidate`,
`candidate_pushed`, `target_tip`, `target_contains_candidate`,
`target_checked_out_at`; each `null` when it could not be determined.

**`finish-run --dry-run --json`** (the `finish` object):

```json
{"schema_version": 1, "run_key": "…", "managed": true,
 "eligible": {"merge": true, "cleanup": true},
 "merge_mode": "already_merged | fast_forward | merge_commit | null",
 "actions": ["…"], "checks": [{"code": "…", "ok": true, "detail": "…"}],
 "deleted_ignored_paths": ["…"],
 "kept": [{"action": "keep_remote_branch", "code": "remote_delete_unavailable", "detail": "…"}],
 "summary": "…"}
```

**`finish-run --json`** (execution):

```json
{"schema_version": 1, "run_key": "…",
 "completed_steps": ["merge", "push_target", "archive_state", "remove_worktree", "delete_branch", "finished"],
 "stopped_at": "checks | <step> | null", "reason": "… | null",
 "remaining": ["…"], "deleted_ignored_paths": ["…"], "kept": [{"action": "…", "code": "…", "detail": "…"}]}
```

Exit codes: `0` done (or eligible, for a dry run), `3` refused by its
checks, `1` a step or the command failed.

**`prune --dry-run --json`** — read-only; nothing is deleted:

```json
{"schema_version": 1, "dry_run": true,
 "items": [{"kind": "record | archive", "run_key": "…", "path": "/abs/…",
            "reason": "finished | worktree_missing | archived", "detail": "…"}],
 "engine_snapshots": []}
```

Only engine-recorded state appears. `engine_snapshots` is always empty
today: no snapshot is recorded, and an unrecorded worktree is never named
(see `docs/design.md`, "Engine snapshots").
