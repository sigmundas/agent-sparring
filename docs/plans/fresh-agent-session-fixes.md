# Fresh agent sessions: Stage 2 review fixes

Repository: `agent-sparring`. Status: approved. Follows the accepted Stage 2
of `docs/plans/fresh-agent-session.md` (candidate `f5f1f96`). An independent
post-acceptance review found two blocking defects; the engine cannot reopen
an accepted stage for review findings, so they are fixed here.

## Stage 1 — Close the --next-turn authority bypass and conservative error classification

### B1: `--next-turn` must not override a recorded marker

`next_turn.resolve_resume_turn` records the caller's `choice` as `manual`
and returns it even when `state.next_turn` is already recorded, and
`standalone_start_with` checks the human gate only after that. As a result:

- `run-loop <stage> --next-turn stage` on a NEEDS_YOU/ESCALATE stage passes
  the gate check and runs the stage agent (and writes the manual marker
  before any refusal);
- with a recorded `stage` marker (SEND_BACK), `--next-turn sparring` re-pins
  the current tree and sends it to review, skipping the owed implementation
  turn (`run-loop` and `resume-plan`);
- with a recorded `finalization` marker, `--next-turn stage` replaces it and
  gives an unrestricted implementation turn on reviewed work.

Required behaviour:

- Honour an explicit `next_turn` choice only when no marker is recorded and
  legacy derivation is ambiguous (or absent). Whenever a marker is recorded,
  or derivation yields an unambiguous answer different from the choice,
  refuse with a clear message naming the recorded marker; record nothing.
- Check recorded human gate state (NEEDS_YOU / ESCALATE) before recording
  any marker, in both standalone and managed paths; a choice can never
  answer or bypass a gate.
- Update help text, docstrings and docs to match ("only for ambiguous legacy
  state").

Tests (negative and positive):

- `--next-turn sparring` with recorded `stage` refused, state unchanged
  (both `run-loop` and `resume-plan`).
- `--next-turn stage` with recorded `sparring` refused; with recorded
  `finalization` refused.
- `run-loop --next-turn stage` under NEEDS_YOU refused with no marker
  written.
- Ambiguous legacy state still accepts the explicit choice and records it as
  `manual` (existing behaviour preserved).

### B2: classify only provider error data, never agent output

`classify_provider_error` substring-matches `str(exc)`, and several adapter
messages embed agent-generated content: Claude's no-JSON-result message
includes the whole stdout transcript (`claude_cli.py` ~544), the
missing-session-id message embeds the payload, the `is_error` branch
includes result text, `loop.py` classifies `stage_run.result.text`, and
Codex's no-thread path falls back to stdout. A transcript containing e.g.
"session not found" or "quota" makes the engine recommend discarding a
working session.

Required behaviour:

- Classify only structured provider error data: Claude's `errors` /
  `subtype` fields of the final result event, Codex `turn.failed` /
  `error` event payloads, and process stderr. Carry that data explicitly on
  the `ProviderError` (e.g. a dedicated attribute), not by re-parsing the
  message. Never classify stdout transcripts, tool output or agent result
  text.
- Tighten markers to provider-specific forms (`invalid_encrypted_content`,
  explicit missing-thread/conversation errors on resume, explicit rate-limit
  / usage-limit error codes or types). Unknown errors stay plain
  `ProviderError` and keep today's failure behaviour.

Tests:

- False positives: a transcript or result text containing "session not
  found", "No conversation found", "quota", "rate limit", "overloaded",
  `invalid_encrypted_content` is NOT classified, for both adapters and for
  the loop's `is_error` path.
- True positives still classified from structured fields and stderr.

Keep all other Stage 2 behaviour unchanged. Run the full suite.
