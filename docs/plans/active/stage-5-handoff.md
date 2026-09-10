# Stage 5 handoff: Unattended loop

Date: 2026-09-10

## Branch state

- Base SHA: `79342f79b7213f1ac5c0692bb151a43e0022166c` (verified as the exact
  HEAD of `feature/sparring-v2` before starting; no divergence)
- Candidate SHA: recorded below after commit/push (see "Verification" for
  the exact command sequence used)
- Branch: `feature/sparring-v2`, pushed to `origin`

## Files changed

Production code (new):
- `src/agent_sparring/loop.py` -- the unattended stage<->sparring router:
  `run_unattended_loop`, `LoopResult`, `LoopCycleRecord`, `LoopError`,
  `LoopRunawayError`, `DEFAULT_MAX_SEND_BACK_CYCLES`

Production code (modified):
- `src/agent_sparring/config.py` -- added `ProjectConfig.stage_self_check`
  (parsed from `[stage] self_check`, default `false`)
- `src/agent_sparring/stage_prompt.py` -- `build_stage_prompt` gained an
  optional `self_check: bool = False` parameter that appends one prose
  "## Self-check" section
- `src/agent_sparring/stage_agent.py` -- `run_stage_agent` gained an
  optional `self_check: bool = False` parameter, forwarded verbatim to
  `build_stage_prompt`; never touches `state.json`
- `src/agent_sparring/cli.py` -- refactored `_resolve_stage_provider`/
  `_resolve_sparring_provider` to take an explicit-override parameter
  instead of the whole `argparse.Namespace` (so `run-loop` can resolve two
  independent providers); added `_resolve_self_check`; wired `self_check`
  into `run-stage`'s dry-run and real paths; added the `run-loop`
  subcommand

Tests (new):
- `tests/test_loop.py` (11 tests) -- fake-adapter loop behavior

Tests (modified):
- `tests/test_config.py` -- `[stage] self_check` parsing (+4 tests)
- `tests/test_stage_prompt.py` -- self-check section presence/absence
  (+3 tests)
- `tests/test_stage_agent.py` -- self-check reaches the prompt but never
  `state.json` (+2 tests)

Production code changed: **yes**. Tests changed: **yes**.

## Design notes

### The loop itself

`run_unattended_loop` is a small router, per the plan's "Orchestrator
target": it calls `run_stage_agent` then `run_sparring_agent` in a loop,
and only decides *where control goes next* based on the sparring agent's
`RoutingResult.action` -- it does not decide *what* either agent should do,
and it does not implement any new provider logic itself.

Routing:

    SEND_BACK -> count it against the runaway limit; if still within
                 limit, loop again (same stage session, same sparring
                 session, both entirely via run_stage_agent/
                 run_sparring_agent's own state.json-driven resume logic)
    READY     -> return LoopResult(outcome=READY, ...)
    NEEDS_YOU -> return LoopResult(outcome=NEEDS_YOU, ...)
    ESCALATE  -> return LoopResult(outcome=ESCALATE, ...)

`LoopResult.outcome` is validated in `__post_init__` to be one of
READY/NEEDS_YOU/ESCALATE only -- SEND_BACK can never be a `LoopResult`'s
outcome, it only ever continues the loop.

### Orchestration-ownership invariants (all preserved, none re-implemented)

- **The orchestrator owns all top-level agent launch/resume decisions.**
  `run_unattended_loop` is the only caller of `run_stage_agent`/
  `run_sparring_agent` in the new code path; it never lets either agent's
  own output decide whether another top-level agent is launched -- routing
  comes only from the parsed `RoutingResult.action` (a
  `~agent_sparring.routing.RoutingAction` enum member), never from prose.
- **No agent can dispatch another top-level implementation or sparring
  agent.** Unchanged from Stage 3/4: this was already enforced by
  `stage_prompt.py`'s "Scope reminder" ("do not ... launch another
  top-level implementation or sparring agent") and by the loop only ever
  calling the two `run_*` functions itself. Stage 5 adds no new surface
  for an agent to trigger another agent.
- **One-writer-per-worktree.** Unchanged: `run_stage_agent` still acquires
  `agent_sparring.concurrency.worktree_lock` internally; the loop does not
  bypass or duplicate that lock, it just calls `run_stage_agent` once per
  cycle like any other caller would.
- **Expected-branch protections.** Unchanged: both `run_stage_agent` and
  `run_sparring_agent` still independently enforce `expected_branch` (branch
  guard before/after the stage turn; exact-branch check before/after the
  sparring turn). The loop passes the same `expected_branch` string to both
  on every cycle; it does not re-implement or weaken either check.
- **Provider-issued session identity.** Unchanged: `state.json`'s
  `implementation_session_id`/`sparring_session_id` are still written only
  by `run_stage_agent`/`run_sparring_agent` from the provider's own
  returned id, and both still refuse a resume that returns a *different*
  session id than the one requested. The loop never reads or writes a
  session id itself -- it has no session-identity logic of its own at all.
- **Same implementation session across SEND_BACK cycles / same sparring
  session across SEND_BACK cycles / fresh stage gets fresh sessions.** This
  falls out entirely from calling `run_stage_agent`/`run_sparring_agent`
  every cycle with the same `Stage` object: each one reads
  `state.implementation_session_id`/`sparring_session_id` from
  `state.json` and resumes if set, starts fresh if not. The loop adds *no*
  additional session bookkeeping -- see
  `test_send_back_resumes_same_implementation_and_sparring_sessions` and
  `test_multiple_send_back_cycles_preserve_identity` in `tests/test_loop.py`,
  both of which assert the exact same provider-returned session id is used
  on every resume call.

### Runaway limit

`max_send_back_cycles` (default `DEFAULT_MAX_SEND_BACK_CYCLES = 5`) counts
SEND_BACK verdicts, not total cycles: a fresh stage's very first cycle
always runs regardless of the limit. After a SEND_BACK, the counter is
incremented and compared with `>` against the limit; only if it is now
*over* the limit does the loop raise `LoopRunawayError` -- before running
another cycle, not after silently running one extra turn past the
configured limit. `max_send_back_cycles < 1` is rejected up front by
`LoopError` (a config error, not a runaway) without invoking either
adapter at all.

This is deliberately not a workflow state machine: one integer counter,
compared to one configured limit, inside the loop's own local variables --
nothing new in `state.json`.

There is no CLI flag to disable the limit outright; the smallest
computationally meaningful value is `1` (one SEND_BACK tolerated, then
stop).

### Stopping cleanly on provider/integrity/config errors

`StageAgentRunError` and `SparringAgentRunError` (branch guard failures,
worktree-lock contention, provider `ProviderError`s, read-only integrity
violations, resume session-id mismatches, unparseable verdicts -- all
already implemented and tested in Stage 3/4) are caught at the loop level
and re-raised as `LoopError`, with the original message preserved in the
new message's text. No retry, rollback, or alternate recovery path is
invented anywhere in `loop.py`. `tests/test_loop.py` exercises both a
stage-agent-side provider failure
(`test_stage_provider_failure_stops_the_loop_without_invoking_sparring`,
plus a second-call/resume-time failure variant) and a sparring-agent-side
read-only integrity violation
(`test_sparring_read_only_integrity_failure_stops_the_loop`, using a fake
adapter that writes to the repo -- the same technique Stage 4's own tests
use, independent of whether Codex is installed).

### `self_check` (the optional addition discussed outside the original plan)

Config-only, per the task's explicit preference -- no `--self-check`/
`--no-self-check` CLI flag was added, since it would not simplify anything
here (a project either wants this for every unattended run or does not):

    [stage]
    self_check = true

Default `false` when the key or the whole `[stage]` table is absent; a
non-boolean value raises `ProjectConfigError` (mirrors every other
project.toml field's validation style, via a new `_optional_bool` helper
in `config.py`).

`_resolve_self_check` in `cli.py` reads it once per invocation and passes
it straight through: `run-stage --dry-run` and the real `run-stage` path
both use it; `run-loop` resolves it once and forwards the same value to
every `run_stage_agent` call across all cycles of that loop run.

When enabled, `build_stage_prompt` appends exactly one new section, prose
only, right before the existing "Scope reminder":

    ## Self-check

    Before finishing this turn, inspect your own implementation for:

    - failure between steps;
    - resume/retry behavior;
    - stale or partially written state;
    - provider/runtime differences;
    - concurrency issues;
    - ways important invariants can be bypassed.

    Where this stage depends on external-tool behavior, exercise the real
    tool when practical rather than assume its contract. Fix issues you
    find before handing off, and report what you checked or deliberately
    deferred. This is a self-check, not a substitute for independent
    sparring, which still runs afterward.

Deliberately lightweight, matching every constraint from the task:
- prose instruction only -- no new field is read back or interpreted by
  any code;
- no new workflow state -- `self_check` never reaches `state.json` (see
  `test_self_check_true_reaches_prompt_but_not_state_json` in
  `tests/test_stage_agent.py`, which asserts the key is absent from
  `StageState.to_dict()` even after a run with `self_check=True`);
- no mandatory checklist fields and no pass/fail gate -- the section is
  plain prose, not a schema the agent must fill in;
- no duplicated sparring logic -- nothing here parses or validates what
  the agent reports checking; independent sparring still runs exactly as
  before, unaware this section exists.

### CLI: `run-loop`

Added as the orchestrator's actual entry point for the unattended loop,
mirroring `run-stage`/`run-sparring`'s existing flag conventions but with
two independent provider/model/executable options (`--stage-provider`/
`--sparring-provider`, `--claude-executable`/`--codex-executable`,
`--stage-model`/`--sparring-model`) since one invocation now drives both
roles. `--max-send-back-cycles` (default `DEFAULT_MAX_SEND_BACK_CYCLES`)
is the only loop-specific flag. `--expected-branch` is required, matching
`run-stage`/`run-sparring`. There is no `--dry-run` for `run-loop` --
`run-stage --dry-run`/`run-sparring --dry-run` already cover inspecting
either prompt in isolation, and a combined dry-run would not exercise
anything the loop itself is responsible for (it does no prompt assembly of
its own).

`_resolve_stage_provider`/`_resolve_sparring_provider` were refactored to
take an explicit-override string instead of the whole `argparse.Namespace`
(both call sites in `_cmd_run_stage`/`_cmd_run_sparring` updated to pass
`args.provider`), purely so `run-loop` can resolve two independent
providers through the same two functions without a namespace attribute
collision.

## Deliberate deviations from a literal reading of the task

- No CLI `--self-check`/`--no-self-check` flag, per the task's own stated
  preference for config-only control.
- No Stage 6 acceptance/freeze semantics were touched or added; `Stage`'s
  existing `StageStatus.FROZEN`/`ACCEPTED` values are untouched and unused
  by this stage's code.
- No dummy commits were made anywhere, including during the live smoke
  test (see below) -- the disposable repo's one real commit was made by
  the real Claude CLI actually implementing the trivial brief, not by this
  session.
- `LoopResult.cycles` and `LoopCycleRecord` (resumed flags + routing per
  cycle) were added beyond the plan's bare minimum because a caller
  driving `run-loop` unattended has no other way to see how many
  SEND_BACK corrections actually happened without re-reading `sparring.md`
  history that Stage 5 does not keep (only the latest exchange is ever
  written, by design, per Stage 2/3). This is returned data, not new
  persisted state -- nothing is written to disk beyond what
  `run_stage_agent`/`run_sparring_agent` already write.

## Verification

Full test suite (unittest, no pytest installed in this environment):

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

Result: all 16 test modules pass (`OK`), including the new
`tests/test_loop.py` (11 tests) and the extended `test_config.py`,
`test_stage_prompt.py`, `test_stage_agent.py`.

`git diff --check` (staged): clean (no whitespace errors).

## Live end-to-end smoke test (performed, not skipped)

Run against a disposable, throwaway git repo created solely for this
purpose under the session scratchpad -- never against this candidate repo
(verified by `git status --porcelain` on this repo immediately after
cleanup: only the intended staged Stage 5 changes were present, nothing
from the disposable repo leaked in).

Sequence, using the real installed `claude` (2.1.236) and `codex`
(0.153.4) CLIs, no fakes/mocks:

1. `git init` a fresh disposable repo, one base commit on `main`, checked
   out `feature/x`.
2. `sparring new-stage stage-1` (real CLI invocation against the
   disposable repo) with a trivial, unambiguous brief: create
   `GREETING.txt` containing one fixed line, commit it, touch nothing
   else.
3. `sparring run-loop stage-1 --repo-root <disposable> --expected-branch
   feature/x --max-send-back-cycles 1` -- one real Claude CLI turn
   (implementation), then one real Codex CLI turn (read-only sparring).

Result: exit 0, `outcome=READY cycles=1 send_back_count=0`. Inspected
afterward, before cleanup:

- `git log` in the disposable repo showed one real new commit,
  `4e97c87 Add GREETING.txt for stage-1`, on top of the base commit --
  made by the real Claude CLI, not this session.
- `GREETING.txt` contained exactly the requested line.
- `state.json` recorded two distinct, real, provider-issued ids:
  `implementation_session_id` (a Claude session UUID) and
  `sparring_session_id` (a Codex thread id, visibly a different UUID
  shape) -- never invented, exactly as `run_stage_agent`/
  `run_sparring_agent` are supposed to record them.
- `sparring.md` contained genuine per-run prose from the real Codex
  turn referencing the actual candidate commit SHA and the actual diff
  content (not a canned string), plus a `READY` routing outcome.
- The disposable repo's own worktree was clean except for the untracked
  `.sparring/` directory -- no read-only violation.

The disposable repo was deleted immediately afterward. This is a genuine
one-cycle READY smoke test, not a SEND_BACK/NEEDS_YOU/ESCALATE/runaway
exercise -- those four paths are covered by `tests/test_loop.py`'s fake
adapters instead, per the task's own "if practical" framing for the live
leg (a live SEND_BACK/NEEDS_YOU/ESCALATE run would require either an
adversarial brief or costly, non-deterministic real-model behavior to
trigger reliably, and would not exercise any code the fake-adapter tests
don't already cover).

## Known limitations / unresolved questions

- The loop's own error path is a thin wrapper (`LoopError`/
  `LoopRunawayError`) around Stage 3/4's existing error types; it does not
  add any new integrity check of its own. Everything Stage 3/4's handoffs
  already documented as a known limitation (e.g. the `(branch, HEAD, dirty
  paths)` fingerprint not detecting an edit-then-restore) is unchanged and
  still applies here.
- `LoopResult.cycles`/`LoopCycleRecord` are returned in-memory only; they
  are not persisted anywhere. A caller that wants a durable multi-cycle
  history beyond the latest `sparring.md` exchange has none from this
  stage.
- No `.sparring/project.toml`/`PROJECT.md` exists yet for this repo itself
  (Stage 7 in the plan is the Sporely pilot); the live smoke test's
  disposable repo also had no `project.toml`, so `run-loop` there used
  every provider default (`claude-cli`/`codex-cli`) and `self_check`'s
  config default (`false`) -- self-check's prompt section itself is only
  exercised by the unit tests in `test_stage_prompt.py`/
  `test_stage_agent.py`, not by the live smoke test.
- Stage 6 (acceptance/freeze) remains explicitly out of scope and was not
  started.
