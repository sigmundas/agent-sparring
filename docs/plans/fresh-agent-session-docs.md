# Fresh agent sessions: Stage 4 (engine) — docs, walkthrough, review leftovers

Repository: `agent-sparring`. Status: approved. Stage 4 of
`docs/plans/fresh-agent-session.md`, run as its own plan: the original run
`fresh-agent-session-s1` stays parked at its Stage 3, because Stage 3 was
completed and accepted in `agent-sparring-vscode` (`d128d76`) by a separate
managed run and the engine has no mechanism to satisfy a plan stage from
another repository's run. Engine baseline: `f9dd7f3`. The VS Code half of
Stage 4 (skills, README, extension test gaps) is a separate plan in that
repository.

## Stage 1 — Fresh-session documentation and review leftovers

### Documentation

- User-facing docs (`docs/plans.md`, `docs/reference.md`, index in
  `docs/README.md` if needed): one section comparing
  - ordinary resume: same stage, same session;
  - fresh-session resume (`--fresh-sparrer` / `--fresh-stage-agent`): same
    stage and candidate, new provider conversation for one role, history and
    verdicts preserved, configuration resolved anew and recorded;
  - reset-stage: new stage attempt, existing attempt archived.
- `next_turn` as engine authority; `--next-turn` only for ambiguous legacy
  state; what refuses and why.
- Recovery walkthrough: reviewer session cannot be resumed
  (`provider_pause` kind `session-unresumable`) → printed retry → fresh
  reviewer → normal loop; provider unavailable → wait or fresh on another
  provider; legacy stuck run with HEAD moved outside the loop → refused
  derivation → explicit `--next-turn` choice.
- Provider identity lines ("provider-reported model", "backend/account not
  reported") and that a model name does not imply OpenAI/Azure routing.
- Session generations in `sparring usage`.
- Note the known gap: an accepted stage cannot be reopened for findings from
  a post-acceptance review; such fixes run as a follow-up plan.

### Review leftovers (from independent reviews of Stages 1–2, fixes and
pause reason)

- Adopting an existing human pause on a resume without evidence
  (`plan.py`, the `_pause` adoption path) drops `provider_pause` although no
  provider turn ran; keep it (as refused resumes do) and test it.
- Standalone `run-loop` routes legacy uncommitted READY to finalization
  without a pinned candidate; either refuse in `run-loop` (managed resume is
  the supported path) or document it explicitly; test the chosen behaviour.
- Fix the stale docstring in `next_turn.py` (`resolve_legacy_ready`) that
  describes the managed routing as if it applied to `run-loop`.
- Add the missing tests: committed legacy READY + `--next-turn`; review-only
  and ACCEPTED stage refusals with byte-identical plan-run and stage state;
  `--fresh-stage-agent` when committed legacy READY is re-routed to review.

Do not change any other behaviour. Run the full suite.
