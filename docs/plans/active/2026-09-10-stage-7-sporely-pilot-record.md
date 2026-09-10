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
