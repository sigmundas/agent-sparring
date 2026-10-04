# Fresh agent sessions: --next-turn legacy-gate bypass fix

Repository: `agent-sparring`. Status: approved. Follows accepted
`docs/plans/fresh-agent-session-fixes.md` (candidate `ff63839`, full suite
1254 passed). An independent review found one remaining blocking authority
bypass. Tightly scoped: no redesign.

## Stage 1 — Separate "no turn owed" from "ambiguous" and make --next-turn validation unconditional

### Defect

In `next_turn.resolve_resume_turn`, both `AmbiguousNextTurn` and a `None`
from `derive_next_turn` become `derived = None` and let an explicit choice
through. `derive_next_turn` returns `None` both for "the recorded outcome is
a gate (READY / NEEDS_YOU / ESCALATE) with no later implementation turn" and
for "nothing has run yet". The standalone gate check covers only
NEEDS_YOU/ESCALATE, and the managed path skips `_awaiting_finalization` and
the committed-READY check whenever `next_turn_choice` is given
(`plan.py` ~3575, ~3639). So on legacy state with READY over an uncommitted
worktree, `resume-plan --next-turn stage` records a manual `stage` marker
and grants an unrestricted implementation turn on reviewed work; and on an
untouched stage `--next-turn sparring` sends the base tree to review with no
implementation turn.

### Required semantics

- Derivation distinguishes explicitly (no overloaded `None`):
  `stage`, `sparring`, **no turn owed** (a recorded READY / NEEDS_YOU /
  ESCALATE with nothing after it — gate state is authoritative), and
  **ambiguous / cannot derive** (raised). An untouched stage derives
  `stage` unambiguously.
- An explicit `--next-turn` choice is legal only for the ambiguous case
  with no recorded marker. Every other case refuses with a clear message and
  records nothing:
  - legacy READY with nothing after it: refused; managed resume continues
    through the existing READY / finalization / committed-READY path; a
    manual selection never reopens reviewed work;
  - legacy NEEDS_YOU / ESCALATE with nothing after it: refused; gate state
    preserved;
  - untouched stage: `--next-turn stage` refused as an unnecessary override,
    `--next-turn sparring` refused as wrong.
- Managed READY / finalization and committed-READY checks run regardless of
  `--next-turn`; the override never suppresses them.
- `--next-turn` validation and refusal happen before any plan-level state
  change (saving RUNNING, clearing a pause / `awaiting`, declaring
  repositories). A refused resume leaves plan-run state and stage state
  byte-identical.
- `--fresh-*` and `--next-turn` are orthogonal: resolve and validate the
  authoritative turn first, then apply the fresh-session request to the role
  that will actually run. Freshness changes conversation identity only and
  never makes an otherwise-invalid turn selection legal.

### Tests (add all; keep existing regressions passing)

1. Legacy READY (uncommitted and committed candidate) + `--next-turn stage`
   and `sparring`: refused on `resume-plan` and `run-loop`, nothing
   recorded; without the flag the existing finalization path runs.
2. Untouched stage + `--next-turn stage` and `sparring`: both refused;
   derivation yields `stage`.
3. NEEDS_YOU / ESCALATE through `resume-plan` + `--next-turn`: refused, pause
   kept, plan state unchanged.
4. `--fresh-*` combined with `--next-turn`: valid fresh-session use on the
   ambiguous-legacy path works; invalid turn overrides with a fresh flag are
   refused exactly as without it.
5. Refused `--next-turn` leaves plan-run state (status, awaiting,
   repositories) unchanged.

Existing regressions must remain: recorded marker refusal, unambiguous
legacy derivation refusal, ambiguous legacy derivation requiring explicit
choice, candidate mismatch, SEND_BACK → implementation → failed reviewer →
review without implementation, and SEND_BACK with no fix → stage first.

Run the full suite.
