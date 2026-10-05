# Fresh agent sessions: structured provider pause reason

Repository: `agent-sparring`. Status: approved. Follows accepted
`docs/plans/fresh-agent-session-fixes-2.md` (candidate `8da577a`). Needed
before the VS Code fresh-session UX: the extension may only render engine
structured state, never activity or prose.

## Stage 1 — Persist the provider pause reason in plan-run state

### Gap

`plan._fail_or_pause_for_provider` knows why a managed run paused (kind
`session-unresumable` | `provider-unavailable`, role, stage id, whether the
role has a session) but persists only the paused status; the details reach
only stdout and an activity summary. No client can show "the reviewer
session cannot be resumed" from authoritative data.

### Required behaviour

- Add an engine-owned, optional field to the plan-run state JSON (e.g.
  `provider_pause`): `{kind, role, stage_id, has_session, recorded_at}`.
  Written in the same save that pauses the run, only by
  `_fail_or_pause_for_provider`. Omitted when absent, so existing plan-run
  files round-trip byte-identically.
- Cleared when the run next leaves the paused state for any reason (a resume
  that starts running, with or without `--fresh-*`), and replaced by any
  other pause or failure. A refused resume leaves it untouched.
- It is descriptive only: it never authorizes or selects a transition, and
  resume behaviour must not read it. `next_turn`, gates and candidate checks
  remain the only authority. Document this.
- Expose it read-only where clients already read run state (the plan-run
  JSON, and any existing `--json` status output if one exists for plan
  runs), and document the field shape in `docs/` and `docs/migrations.md`
  as a client contract, including the exact `kind` and `role` values.
- `artifact_ownership.py`: no new file; confirm the plan-run state entry
  still names the engine as sole writer.
- Standalone `run-loop` has no plan-run state; leave it unchanged (its
  printed retry is enough) and say so in the docs.

### Tests

- Unresumable reviewer failure → plan-run state records
  `{kind: "session-unresumable", role: "sparring", has_session: true, ...}`.
- Provider-unavailable failure on the stage role recorded likewise;
  `has_session: false` when the role's first session never started.
- A subsequent resume (plain and `--fresh-sparrer`) clears it once running;
  a refused resume keeps it; a different pause/failure replaces it.
- Ordinary non-provider failures and NEEDS_YOU pauses never write it.
- Absent field round-trips byte-identically; resume routing is identical
  with and without the field present.

Run the full suite.
