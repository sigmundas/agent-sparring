# Fresh agent sessions: same stage, new provider conversation

Repositories: `agent-sparring` (primary), `agent-sparring-vscode` (sibling,
Stage 3). Status: approved for Stage 1; later stages run one at a time.

Like `/clear`, not reset-stage: the stage, its candidate and every artifact
stay authoritative; only one role's conversational continuity is discarded.

| Operation | Stage | Provider session |
| --- | --- | --- |
| ordinary resume | same | same |
| fresh-session resume | same | new generation |
| reset-stage | new attempt, old archived | none |

## Grounding (verified in code at 8d2deaf)

- `StageState` (`stage.py:270+`) holds one `implementation_session_id` and one
  `sparring_session_id`, overwritten in place; `agents: {role: PinnedAgent}`
  pins provider/model/effort for the whole stage (`stage.py:234-266`),
  enforced by `_apply_pin`/`_stage_agents` (`cli.py:978-1028`).
- The independent reviewer resumes `sparring_session_id` (`review.py:403`);
  `ask` requires it (`dialogue.py:202`).
- No automatic context/max-turn rollover exists; `context_window` and
  `rate_limit_*` are telemetry only. `loop.py:369` keeps a failed session id
  for a deliberate retry.
- `ProviderError` is unclassified; `plan.py:3259` turns any loop failure into
  a failed run, not a pause.
- Resume picks the next actor at `plan.py:3194-3204`: sparring only after
  `--evidence` (with an implementation session), finalization when stopped
  between READY and commit, otherwise the stage agent. There is no persisted
  record of whose turn it is, so a reviewer that crashes after a successful
  implementation turn sends a resume back to the stage agent.
- Fresh prompts are artifact-built; but `stage_prompt.py:168` only includes
  `sparring.md` on resume, so a fresh stage agent after SEND_BACK would not
  see the finding.
- `usage.py:217` treats cumulative tokens as one session per role.
- `prompt_capture` records a capture per turn *start* (seq, role, turn kind),
  not completion.

## Stage 1 — Engine fresh-session primitive and next_turn authority

Implement in `agent-sparring` only. No CLI flags beyond what tests need
(CLI surface is Stage 2), no VS Code work.

### 1. `next_turn`: engine authority for whose turn it is

- Add engine-owned `next_turn` to stage `state.json`: `stage | sparring |
  finalization`, plus `next_turn_candidate` recording the exact candidate it
  refers to when `sparring`: the repository's candidate identity (HEAD SHA
  and whether the reviewed content is a committed candidate or the working
  tree, with a content fingerprint such as the tree/diff digest the engine
  already computes, if one exists) and any declared sibling repository pins.
  Absent fields keep existing `state.json` files byte-identical on read and
  unchanged until the engine writes the marker.
- Transitions, written by the loop only after the preceding authoritative
  operation fully succeeded and its state was persisted:
  - stage start and a recorded SEND_BACK verdict → `stage`;
  - successful implementation turn, after the result, handoff, candidate
    identity and required state are all recorded → `sparring` with its
    candidate;
  - READY → the existing finalization path (`finalization`), then acceptance
    as today;
  - NEEDS_YOU / human gates: current semantics preserved; the marker is not
    advanced past a gate, and existing gate state wins where it already gives
    a stronger authoritative next action.
  - A failed provider turn never advances it.
- Ordinary resume (`resume-plan`, `run-loop`) obeys the marker when present.
  The existing `--evidence` sparrer-first path and finalization detection
  must remain correct (express them via, or reconcile them with, the marker).
- When `next_turn = sparring`, before starting the reviewer verify that HEAD,
  the worktree and sibling pins still match `next_turn_candidate`. On any
  mismatch refuse with a clear message; never fall back to another actor.
- Activity events may be emitted for observability, but activity.jsonl is
  never read as authority.

### 2. Legacy derivation (state written before this change)

- When the marker is absent on resume, derive it once from engine-owned
  records only: the prompt-capture index (turn order), the recorded handoff
  (engine-written after a completed implementation turn, including its
  candidate commit), `sparring.md`'s recorded routing outcome as an
  engine-rendered record, and the current candidate/HEAD. Never activity.jsonl
  or agent prose.
- Derive `sparring` only when the last completed implementation turn is later
  than the last recorded verdict and its recorded candidate matches the
  current HEAD/worktree; derive `stage` when the last authoritative record is
  a SEND_BACK with no later completed implementation. Anything else
  (including HEAD having moved past the handoff's candidate) is ambiguous:
  refuse, explaining what was found, and accept an explicit
  `next_turn` choice (`stage | sparring`) supplied by the caller (Stage 2
  exposes it as `--next-turn`; Stage 1 provides the engine parameter).
- Record a derived or manually selected marker with provenance
  (`derived` / `manual`) and persist it, so later resumes never derive again.

### 3. Session generations and per-session configuration lock

- Add `sessions: {role: [generation...]}` to `state.json` (omitted when
  empty). Each generation records: `generation` number, `session_id` (null
  until the provider reports one), the pinned agent (provider, model,
  model_source, effort, effort_source), `started_at`, `start_reason`
  (`initial` or `fresh:<reason>`), and when closed `ended_at`/`end_reason`.
  Existing state with a session id but no list is treated as generation 1,
  materialized when the list is first written.
- Keep `implementation_session_id`, `sparring_session_id` and `agents[role]`
  as the *current generation's* values so existing readers keep working.
- New module `sessions.py` with one primitive,
  `start_fresh_session(stage, role, reason, ...)`: under the existing stage /
  worktree locking it closes the current generation, clears that role's
  current session id, opens a pending generation and allows its agent
  configuration to be resolved anew (preference or explicit override,
  including a different provider that supports the role). It changes nothing
  else: not candidate, base, branch, mode, brief, handoff, sparring.md,
  notes, deferred ledger, plan digest/manifest, siblings, freeze/accept
  state, prompts or activity. It never changes `next_turn`.
- This is the single path for discarding a session; any future automatic
  rollover must call it with `reason="auto:<cause>"`. Do not add automatic
  rollover now.
- Configuration lock moves from per-stage to per-session: `_apply_pin` checks
  the current generation's pin with today's refusals (message wording updated
  to say a fresh session is how to change it); only a pending fresh
  generation resolves configuration anew. Ordinary SEND_BACK/resume still
  reuses the session and its locked configuration exactly as today. Stage
  mode changes remain refused once any generation has existed.
- The recorded-id mismatch checks in `stage_agent.py` / `sparring_agent.py`
  apply to the current generation.
- The new session id is recorded through the existing early-record path.

### 4. Fresh-session prompts

- A fresh stage agent (a role with an earlier generation but no current
  session) gets the full prompt plus the latest sparring exchange
  (`sparring.md`, routing outcome and findings) and a notice: this is a new
  conversation continuing the same stage; do not redo accepted earlier
  stages; the current SEND_BACK findings are the work to address.
- A fresh sparrer gets the full prompt plus its previous sparring exchange as
  historical evidence and this notice: "You are a fresh reviewer replacing an
  earlier reviewer conversation. Prior findings are historical evidence, not
  conclusions you must blindly accept. Re-check the candidate independently,
  but do not lose unresolved findings without explicitly resolving them."
- Deferred checks and human evidence continue to be included as today. Only
  engine-owned artifacts; never provider-hidden reasoning. Captured prompts
  must show the fresh turn kind distinctly (e.g. `turn_kind` `fresh`).

### 5. Usage

- `sparring usage` groups each role by session generation, sums per
  generation (cumulative counters are per session), and prints a visible
  boundary with the generation's reason and configuration.

### Tests (required)

Use and extend the existing fake adapters (`tests/test_stage_agent.py`,
`tests/test_sparring_agent.py`, `tests/test_loop.py`, `tests/test_plan.py`).

- Stuck-run regression: SEND_BACK → implementation fixes candidate →
  reviewer begins and fails before a verdict (adapter raises `ProviderError`)
  → `next_turn` stays `sparring` with the fixed candidate → ordinary resume
  reviews that candidate directly with no implementation turn; a fresh
  sparrer session does the same, as generation 2.
- Inverse: SEND_BACK with no later successful implementation → `next_turn =
  stage`; a fresh sparrer does not skip the implementation turn.
- `next_turn = sparring` with HEAD / worktree / sibling mismatch is refused.
- Transactional: a failure while recording handoff/candidate/state leaves
  `next_turn` at `stage`.
- Legacy derivation: unambiguous `sparring`, unambiguous `stage`, ambiguous
  (HEAD moved past the handoff candidate) refused, explicit choice recorded
  as `manual` and not re-derived.
- Reviewer fresh session: SEND_BACK → fix → fresh sparrer → new reviewer
  prompt contains the prior unresolved finding and current handoff → READY →
  normal acceptance.
- Model switch: generation 1 pinned model A; preference becomes B; ordinary
  resume still uses A; fresh session records B; both generations reported.
- Provider switch across two fake providers.
- Fresh stage agent continuing after SEND_BACK sees `sparring.md`.
- Integrity: a fresh session does not change candidate SHA, plan digest,
  pending human gate, SEND_BACK obligation, sibling pins, prompts/activity
  history, and cannot reach acceptance without a normal READY review.
- Ordinary resumes reuse sessions exactly as before; existing tests pass.
- Usage reports generation boundaries and per-generation totals.

Update `docs/` reference pages and `docs/migrations.md` for the new state
fields and the resume / fresh-session / reset-stage distinction.

## Stage 2 — CLI, recovery diagnostics and pause-on-session-failure

- `--fresh-sparrer` / `--fresh-stage-agent` on `resume-plan`, `run-loop` and
  the independent review entry point, with role-scoped provider/model/effort
  overrides and `--fresh-reason`; `--next-turn stage|sparring` for ambiguous
  legacy state. Print the resolved configuration before launching. Overrides
  without `--fresh-*` that conflict with the active session stay refused.
- Classify `ProviderSessionUnresumable` (Codex `invalid_encrypted_content`,
  thread/session not found; Claude "No conversation found") and
  `ProviderUnavailable` (quota/rate limit). Both pause the run with the
  candidate untouched and print the exact fresh-session retry command; never
  act automatically.
- Provider identity: report adapter, configured model, provider-reported
  model and backend/account (`not reported` unless the CLI exposes it safely).

## Stage 3 — VS Code fresh-session UX (agent-sparring-vscode)

- Overview actions "Start fresh reviewer…" / "Start fresh implementation
  agent…" when paused or after a session failure, with a quick pick for the
  current preference or another model/provider, and a confirmation stating
  same stage and candidate, new conversation, history preserved, model,
  effort. A "Reviewer session cannot be resumed" card. Engine commands only;
  no `.sparring` writes.

## Stage 4 — Docs and integration

- User-facing docs and skill updates; end-to-end recovery walkthrough.
