# Stage 7 — Sporely pilot record

Date: 2026-09-10
Generic tool: `agent-sparring`, branch `feature/sparring-v2`
Pilot repository: `sporely-web`, branch `feature/sparring-v2-pilot`

V1 at `~/Documents/Code/sporely/.sparring` was left untouched and read-only
throughout. Nothing was migrated, deleted, or mutated. Stage 8 still owns
retirement.

---

## Setup

Added project-local V2 configuration to `sporely-web` (the mobile field
companion at `app.sporely.no` plus its Capacitor Android wrapper), chosen
because it has an active plan, a real test suite, a clean worktree, and
genuine device-gated surfaces:

- `.sparring/project.toml` — machine-readable only: `project`, `[repo] root`,
  `[commands]`, explicit provider ids (`claude-cli`, `codex-cli`),
  `[sparring] default_mode`, and `[stage] self_check = true`.
- `.sparring/PROJECT.md` — durable project knowledge: stack, important
  directories, test/build commands, coding conventions, working-tree hygiene,
  product invariants, device/manual checks, data-handling policy, reading
  discipline, and project-specific subagent guidance.

Every fact in `PROJECT.md` was verified against the working tree rather than
copied forward, including the baseline test counts. No Sporely knowledge was
added to generic `agent-sparring` code.

Committed as `cb68b59` on `feature/sparring-v2-pilot` and pushed.

### Baseline, verified 2026-09-10

`npm test` → 1260 tests, 1216 pass, **8 fail**, 36 skipped, exit 1. The eight
are pre-existing: `src/live-reconnect.test.js` (QA3 Sync-tag assertion),
`src/screens/map.test.js` (`ERR_UNKNOWN_FILE_EXTENSION` on `leaflet.css`), and
six Deno TypeScript edge-function suites `node --test` cannot load at all.
`npm run build` passes. `npx eslint .` → 0 errors, 51 warnings.

Recording these in `PROJECT.md` mattered immediately: without them an agent
told "make the tests pass" would either chase six Deno suites out of scope or
report its own stage as broken.

---

## Stage 1 — `stage-map-test-css-loader`

**Task (real).** `src/screens/map.test.js` failed with
`ERR_UNKNOWN_FILE_EXTENSION` on `leaflet.css`: the test imports
`src/screens/map.js`, which imports Leaflet's stylesheets, and Node's ESM
loader cannot resolve what Vite resolves at build time. `test/css-loader.mjs`
already existed in the repository and was referenced by nothing.

**Route sequence.** `run-loop` → stage agent (fresh session) → sparring
(fresh session) → **NEEDS_YOU** → [generic fix + evidence, no code change] →
`run-sparring` resuming the same session on the same SHA → **READY** →
`freeze-candidate` → `accept-candidate`.

**Candidate.** `9b414acad6b4d46cfbf0efa12952bd64b89f8b33`, base
`cb68b594b180b5359ddd8376dec9066523263376`. Two files, 28 insertions: a
`test/register-css-loader.mjs` helper calling `module.register()`, and five
lines in the test to register before its dynamic import. No production module
changed.

**Session reuse.** Worked. `implementation_session_id`
`a480d808-…` and `sparring_session_id` `01a08d3e-…` were unchanged across the
second sparring turn.

**Acceptance.** Accepted at that exact SHA. Freeze verified the pushed ref
independently: "origin/feature/sparring-v2-pilot is exactly 9b414ac…".

**Independently verified by the orchestrator** (writable environment, Node 22):
focused test 1/1; `npm test` 1261 / 1218 pass / 7 fail (exactly the remaining
baseline seven) / 36 skipped; `npm run build` pass; `npx eslint .` 0 errors, 51
warnings; clean tree.

**Sparring quality.** Genuinely useful. It corrected two inaccuracies in the
stage agent's own handoff: that the 1260 → 1261 test-count rise was `node
--test` auto-discovering the new helper file rather than a previously failing
file gaining an entry, and that `module.register()` is release-candidate
stability in Node 22, not stable. Both were right.

**Human interventions required.** One, and it was caused by the defect below.
After the fix, none.

### Defect 1 — unrunnable checks were routed to NEEDS_YOU (fixed)

The sparrer reviewed a correct, complete candidate accurately and then returned
`NEEDS_YOU` for one reason: its OS-enforced read-only sandbox could not run the
project's own verification commands. `npm test` failed with EPERM creating the
test runner's temporary directories; `npm run build` was skipped because it
writes artifacts. It filled in `deferred` at the same time, which contradicts
stopping for a human.

The read-only sandbox is a deliberate independence invariant (a sparrer that
writes becomes a contributor), so the sandbox was not the bug. The bug was that
the sparring prompt never conveyed the plan's own Principle 5 — a deferred
check is documentation, not a workflow state. Left alone, the unattended loop
would stop for the human on essentially every real stage whose brief asks for
full-suite or build evidence, which defeats Stage 5 entirely.

**Fix:** `6f3f49e` — one prose section ("Checks you cannot run yourself") added
to the sparring verdict instructions: writes fail by design and describe the
environment rather than the candidate; an unverifiable check goes in `deferred`
alongside the action the code review itself warrants; NEEDS_YOU is reserved for
a genuine human decision, visual/device check, external condition, or scope
approval. Prompt text only — no new workflow state, no routing change, no
config. One regression test added; suite 246 → 247 passing.

**Confirmed effective** on the same stage (READY, with the unrunnable checks
correctly deferred and the supplied numbers explicitly labelled "supplied
evidence, not independently reproduced results") and again on Stage 2 below,
which never produced an environment-driven NEEDS_YOU.

### Cases covered

- **Case 1** — simple implementation → READY → freeze → accepted. Yes.
- **Case 6** — evidence changed with no code change, same SHA reconsidered and
  accepted, no dummy commit. Yes, and it arose naturally out of Defect 1 rather
  than being staged. The stage artifacts are gitignored, so regenerating
  `handoff.md` left the candidate byte-identical.

---

## Stage 2 — `stage-qa3-sync-tag-anchor`

**Task (real).** The remaining non-Deno baseline failure. `src/live-reconnect.
test.js`'s "QA3: Offline pill supersedes the header Sync tag" is a structural
test that anchors on the literal `'async function checkSyncStatus()'` and
slices a window after it. `home.js` now declares that function with an options
parameter, so `indexOf` returned `-1`, `slice(-1, 800)` yielded `''`, and every
assertion failed against an empty string with no hint that the anchor rather
than the behavior had broken. Production code was correct.

The brief set an explicit scope boundary: the same anchor-then-slice pattern
rots silently in five test files, and fixing that class repo-wide was declared
out of scope.

**Route sequence.** `run-loop` → stage agent (fresh session) → sparring (fresh
session) → **READY** on the first cycle → `freeze-candidate` →
`accept-candidate`. No human intervention at all.

**Candidate.** `1059821b366b190a37e77fb6eadb3dd7e4518a5d`, base
`9b414ac…`. One file, +29/−6: a `_sliceAfterAnchor()` helper that asserts the
anchor was found and names it in the failure message, both anchored slices
routed through it, and the gate assertion tightened to pin the guard itself
rather than the mere presence of the `AUTHENTICATED_COMPLETE` constant. The
five-file scope boundary was respected and the systemic version reported rather
than decided unilaterally.

**Session reuse.** A *fresh* pair of sessions for the new stage
(`d84b250a-…` / `01a08d51-…`), which is the designed behavior — reuse is
per-stage, not global.

**Independently verified by the orchestrator:** focused suite 21/21; `npm test`
1261 / 1219 pass / **6 fail** / 36 skipped — only the six Deno suites remain,
so both non-Deno baseline failures are now gone; build pass; eslint 0 errors.

**Sparring quality.** Notably strong. It executed the new helper and the QA3
callback against in-memory source mutations — renaming the declaration,
deleting the guard, replacing hide with show, changing the required state, and
reversing the comparison — and confirmed each one fails as intended, without
modifying any file. That is adversarial verification of the test's own
strength, not just a read-through.

One brief-authoring note against myself: the brief predicted `npm test` would
report 1255 tests. That was wrong arithmetic on my part — the total stays 1261
and the pass count rises. The brief told the agent to state actual observed
numbers and not restate the predicted ones if they differed, and it correctly
did exactly that.

### Cases covered

- **Case 1** again, cleanly and with zero human involvement end to end.

---

## Stage 3 — `stage-finds-incremental-pagination-render`

**Task (real).** Stage 2 of the repository's own active plan,
`docs/plans/active/2026-09-09-finds-smooth-pagination.md`, which the plan
itself names as the next action. Ordinary Finds pagination rebuilt the whole
list on every page (`list.innerHTML = html`), destroying already-visible cards
and images and forcing the media loader to repaint — the white-thumbnail frame
and scroll stall reported from device QA. Plus the in-scope detail-return bug:
the back handler unconditionally called `loadFinds()`, discarding pages 2+ so
scroll restore clamped to page-1 height.

**This is the stage that tests the success criterion**, and it passed it.

**Route sequence — one `run-loop` invocation, three cycles, no human relay:**

```text
stage agent  -> b609404  -> sparring -> SEND_BACK (3 findings)
same agent   -> 654502f  -> sparring -> SEND_BACK (1 finding)
same agent   -> d20aef3  -> sparring -> NEEDS_YOU (DEVICE/MANUAL CHECK)
```

`outcome=NEEDS_YOU cycles=3 send_back_count=2`. I carried nothing between the
two agents at any point.

**Candidate.** `d20aef3a335d8cd0c92ccebf9325d5bea00e241e`, base `1059821…`.
Three files; the first commit alone was +1259/−303 across `finds.js`,
`find_detail.js`, and `finds.test.js`.

**Session reuse.** Worked across all three cycles — same
`implementation_session_id` `d11e1195-…` and `sparring_session_id`
`01a08d64-…` throughout, which is what made the corrections same-session rather
than a fresh agent re-deriving context each time.

**Sparring quality — the strongest evidence in the pilot.** The three
first-round findings were real, specific, and *reproduced in memory before
being reported*, each with the exact resulting symptom:

1. **Ordering mismatch reaching the destructive fallback.** Server pagination
   orders by date/created_at/id but the merge sorts by `captured_at`, so valid
   rows need not form a suffix and `_appendFindsPage` fell back to a full
   `_applyFilter` during ordinary load-more. Demonstrated by changing one
   first-page `captured_at` to an earlier date: page two produced a full-list
   replacement, destroyed the old card, and fetched image batches of 20 then 21
   ids.
2. **Unidentified-species key cannot survive real HTML.** The internal key
   starts with `U+0000` and was emitted directly into `data-species-key`. Real
   HTML parsing replaces NUL with `U+FFFD`, so dataset equality against the
   original key fails in a browser and silently invokes a full render — while
   the test harness's fake parser preserves NUL and hides it. This is precisely
   the class of defect unit tests cannot catch and a device would show, and it
   was found by reasoning about the difference between the fake parser and a
   real one.
3. **Feed page incorporation not serialized.** `loadingMore` cleared before
   awaited profile enrichment, so another scroll could append the following
   page first. Reproduced by holding page-two profile lookup: date groups came
   out September 3, September 1, September 2.

**NEEDS_YOU quality.** Correct and precisely scoped: it named the exact plan
scenarios needing hardware (A/B thumbnail persistence across page boundaries,
E sort/view transitions during loading, H detail return beyond page one,
I refresh-then-paginate) and asked for the device/WebView version to be
recorded. It explicitly said visible smoothness and absence of flashing were
not established. That is a human break worth stopping for.

**Orchestrator verification** (writable environment): `npm test` 1275 tests,
1233 pass, **6 fail** (only the six Deno suites), 36 skipped; `npm run build`
pass; `npx eslint .` 0 errors; `git diff --check` clean. The sparrer's reported
22 failures were its own sandbox EPERM on temporary fixtures, exactly as it
said — confirming its self-assessment was honest rather than optimistic.

**Acceptance.** Frozen at `d20aef3` (status `frozen`, pushed ref verified) and
deliberately **not accepted**: the Android/WebView QA genuinely cannot be run
from this workstation. The gate distinguishing `frozen` from `accepted` did
exactly the right thing here.

**Human interventions actually required.** One, and a legitimate one: the
device QA. It remains outstanding.

### Defect 2 — NEEDS_YOU reason never named a category (fixed)

`sparring_exchange.render_sparring` prints `needs_you_reason` under a literal
`Reason category:` label, and `templates.NOTES_TEMPLATE` tells the reader to
name a category — but the sparring prompt only ever asked for a free-text
"short reason". Both real NEEDS_YOU verdicts in this pilot therefore printed
prose under a label promising a category, and the plan's five standard
human-break categories never surfaced. The Stage 3 case was plainly
DEVICE/MANUAL CHECK and never said so.

**Fix:** `0385eee` — the prompt now asks the sparrer to begin
`needs_you_reason` with whichever of the five standard categories fits, and to
say so plainly if none does. Prompt text only; `routing.py` still deliberately
does not model the category as machine state, so no workflow state was added.
Suite 247 → 248 passing.

### Cases covered

- **Case 2** — SEND_BACK → same implementation session → correction → same
  sparring session → READY-equivalent progress. Yes.
- **Case 3** — several unattended correction cycles. Yes: three cycles, two
  SEND_BACKs, one invocation.
- **Case 5** — NEEDS_YOU for a genuine device/manual check. Yes.

---

## Stage 4 — `stage-anchor-rot-hardening`

**Task (real).** Generalize the Stage 2 fix to the four remaining test files
that anchor on a literal declaration and slice a window after it
(`capability-gates`, `connectivity-loss`, `sync-queue`, `screens/review` —
88 `indexOf` calls between them, not all of them anchors). Real value: each of
those can silently stop covering what it was written to cover.

**Route sequence.** `run-loop` → stage agent → `4c0caac` → sparring →
**SEND_BACK** (2 findings) → same agent → `5786136` → sparring → **READY** →
freeze → accept. `cycles=2 send_back_count=1`. No human relay.

**Candidate.** `57861367f96b1c02b6b11b2a8835c735977607d9`. A shared
`src/anchor-slice.js` helper plus conversions across four test files
(+371/−155), then a two-file correction.

**Case 8 — project-specific implementation subagent: yes, genuinely.** The
stage agent dispatched `sporely-implementer` four times, one per file package,
after deciding the shared-helper contract itself. Verified from the provider
session transcript (`ba99979a-…`), not from the agent's prose.

Worth recording that this was the *only* stage of four that delegated. Stages
1–3 made zero subagent calls — including Stage 3, whose brief explicitly said
delegating the mapping to `Explore` was appropriate and which touched a
2865-line file. So `claude -p` stage agents in this setup do not reach for
subagents merely because they are invited to; the work has to actually split
into packages. That is a reasonable behavior, not a defect, but it means a
project cannot assume delegation will happen just because PROJECT.md documents
it.

**Sparring quality — the delegation payoff.** Both SEND_BACK findings were
about claims the delegated work had produced, and both were right:

1. **A false "already-stale anchor" finding.** The stage agent reported a stale
   anchor in `sync-queue.test.js`. The sparrer showed it was not one:
   `'await insertObservationImage('` is the *forbidden* substring of an
   absence assertion, not a slice anchor. For an absence assertion a missing
   literal means the invariant is holding — the opposite of the dead coverage
   a missing literal implies for a presence assertion. The correction
   reintroduced the call in a probe copy to prove the assertion is live, and
   replaced the misleading comment. The assertion itself was left untouched.
2. **A regression test that did not discriminate its own bug.** The new
   `sliceBetweenAnchors` test used an end literal occurring only *after* the
   start anchor, so a globally-searching (buggy) implementation returns the
   same answer and the test passes either way. The correction uses the same
   `END` literal before and after the anchor, and was verified red against a
   deliberately global probe helper and green against the real one.

Finding 2 is the more valuable of the two: a regression test that passes
against the bug it claims to catch is worse than no test, and nothing in the
automated suite could have surfaced it.

**Orchestrator verification:** `npm test` 1285 tests, 1243 pass, 6 fail (Deno
only), 36 skipped; build pass; eslint 0 errors; clean tree.

**Accepted** at `5786136`.

### Cases covered

- **Case 8** — a stage using a legitimate project-specific implementation
  subagent. Yes.
- **Case 2** again — SEND_BACK → same session → correction → READY.

---

## Practical friction

**Setup burden: low.** Two files, and the only non-obvious step was git
hygiene. The acceptance gate exempts only *the current stage's own* five
artifact files from its clean-worktree check, so once a second stage exists,
leftover artifacts from other stages block a freeze. `.sparring/stages/` had to
be gitignored, with `project.toml` and `PROJECT.md` tracked. That is a
reasonable design (the narrow exemption is what stops "everything under
sparring_dir is exempt"), but it is not discoverable — a project will hit it on
its second stage, not its first, and the error will name an unrelated stage's
files. Worth one line in whatever setup documentation Stage 8 produces.

**`[commands]` and `[sparring].default_mode` are declared but unused.** Both
are parsed, validated, and printed by `check-config`, and consumed by no
workflow code anywhere. The commands actually reached the agents as prose in
`PROJECT.md`, which is what made them effective. Not fixed: the plan asks for
project.toml to hold what software needs, and these currently qualify as
neither harmful nor load-bearing. Flagging rather than removing, because
removing them is a config-schema decision, not a pilot decision.

**Prompts were not missing project context.** The assembled stage prompt was
310 lines / 14.5 KB with `PROJECT.md` injected in full on both sides. The
baseline-failures section earned its place immediately: every stage report
correctly distinguished the six Deno failures from real regressions, and none
of the four stages ever claimed a pre-existing failure as its own or tried to
fix one. Injecting the same `PROJECT.md` into both the stage and sparring
prompt is what let the sparrer check claims against project rules rather than
generic good practice.

One cosmetic wart: the brief's own `# Stage brief: …` H1 lands underneath the
prompt's `## Stage brief`, so heading levels collide. Harmless.

**Session resume: reliable, 5 for 5.** Both session ids were unchanged across
every resumed turn — the three-cycle Stage 3 run and the two-cycle Stage 4 run
included — and a fresh stage correctly got a fresh pair. No id was ever
invented or lost.

**Provider CLI behavior: no surprises, two notes.**

- `claude -p --output-format json` and `codex exec ... resume` both behaved as
  the adapters document. Nested `claude -p` from inside a Claude Code session
  works fine.
- `ClaudeCliAdapter.timeout_seconds` defaults to `None` and no CLI flag exposes
  it, so a wedged provider would hang an unattended loop indefinitely. It did
  not happen in four runs, so this is an observation, not a defect — but a
  genuinely unattended overnight run has no upper bound today.
- The sparrer ran on Node 25.8.2 while the project pins Node 22. It said so
  every time, unprompted, which is the right behavior — but it means sparrer
  test results are never authoritative for this project even when the sandbox
  does let a command run.

**Did NEEDS_YOU / ESCALATE stop at the right time?** After Defect 1 was fixed,
yes. Three of four stages reached READY with no human involvement at all; the
one NEEDS_YOU was a real device check that genuinely blocks acceptance. Before
the fix, no — the loop stopped for an environment limitation, which would have
made Stage 5 useless in practice.

**Is the output understandable without reading framework internals?** Mostly
yes. `sparring.md` reads as a review: findings, one routing outcome, and a
deferred list. Two rough edges: every non-selected action section is printed
with "(not applicable)", which is four-fifths noise; and the action section
repeats only the one-line `summary`, so the real content always lives up in
"Finding / discussion" and the "## SEND BACK TO STAGE" heading never actually
contains the instruction being sent back. Neither blocked anything, and I
would not change them without more evidence.

**Did V2 start recreating V1 bureaucracy?** No. `state.json` stayed at five
fields across all four stages. No review-attempt counters, no verdict
identities, no amendment protocol, no front-matter parsing. Both defects found
were fixed with prose in one prompt and zero new machine state — which is
itself the strongest signal that the architecture is holding. The one place
pressure exists is the NEEDS_YOU category, and it was deliberately kept as
prose rather than promoted to an enum.

---

## Cases not yet exercised

**Case 4 — NEEDS_YOU for a genuine product/preference decision.** Not
exercised. No product or preference question actually arose in four stages:
three were test-infrastructure work with a single defensible outcome, and
Stage 3's open questions were all correctness or device-visibility, not
preference. `sporely-web`'s one documented pending product decision (whether
APK/AAB size justifies a dedicated R8 shrinking test release, in `PLAN.md`) is
a release-management call with no implementation stage attached, so putting a
stage in front of it would have been staging a case rather than finding one.
The mechanism is not in doubt — NEEDS_YOU routing works, proven by Stage 3 —
only this particular category is unproven.

**Case 7 — ESCALATE producing a packet for web/manual sparring.** No genuine
ESCALATE verdict arose. Nothing in four stages exceeded what the local sparrer
could decide; the one thing it could not decide, it correctly routed to
NEEDS_YOU instead, which was the right call. Manufacturing an escalation would
have proved nothing about routing.

The *packet mechanism* was verified directly, on the real Stage 3 candidate:
`handoff --self-contained` produced 2656 lines / 126 KB containing stage goal,
claims, git identity, changed files, test evidence, open/deferred checks,
previous unresolved sparring findings, and the embedded diff — every element
the plan requires. The thin form of the same handoff is 12.8 KB. So a web
packet is pasteable, though 126 KB (~30k tokens) for a 1259-insertion stage is
near the practical edge of a chat window; a larger stage would need the thin
form plus repository access rather than the self-contained form.

What remains unproven for case 7 is only the routing decision itself — whether
a sparrer chooses ESCALATE when it should, rather than grinding on or
over-escalating.

---

## Verdict on Stage 8 readiness

The success criterion in the plan is that this sequence needs no human relay:

```text
implementation agent finishes -> sparrer finds a bounded issue ->
implementation agent fixes it -> sparrer checks again -> another issue is
fixed -> sparrer says READY
```

Stage 3 ran exactly that shape for three cycles and Stage 4 for two, each from
a single command, with the human appearing only for a real device check. That
criterion is met on real work, not on a rehearsal.

Supporting evidence: four real stages on a live product repository, three
accepted at exact pushed SHAs, one correctly frozen-not-accepted pending
hardware; six SHAs total; two genuine generic defects found, fixed with
prompt-only changes, and confirmed effective on later stages; suite 246 → 248.
The sparring was not ceremonial — it caught a NUL-vs-U+FFFD DOM bug invisible
to the test harness, a regression test that passed against its own bug, and a
false finding produced by delegated work.

Against that: cases 4 and 7 are unexercised, and the two provider-level
observations (no adapter timeout, sparrer on the wrong Node) are real but were
not blocking.

**Recommendation: V2 is ready for Stage 8.** The unexercised cases are gaps in
coverage, not known problems, and neither depends on V1 continuing to exist —
V1 has no ESCALATE or product-decision machinery that would fill them. Nothing
in the pilot required falling back to V1, and V1 was never touched.

V1 remains untouched and read-only. Retirement is Stage 8's decision, not this
record's.
