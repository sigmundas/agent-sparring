# Stage 4 handoff: Sparring adapter

Date: 2026-09-10

This handoff covers three rounds, all on `feature/sparring-v2`: the initial
Stage 4 implementation, a follow-up round resolving four bounded findings
(including a live-smoke-test-caught bug), and a third round resolving two
further read-only-integrity findings. See "Review round 2" and "Review
round 3" below for each follow-up round's own branch/candidate state and
findings.

## Branch state (round 1, initial implementation)

- Base SHA: `4140e347f579bcbb465eecc769c625dcc7c99eb5` (verified as the exact
  HEAD of `feature/sparring-v2` before starting; no divergence)
- Candidate SHA: `a489a87e9efae89711c91b1f80df57bcd3a15b88`
- Branch: `feature/sparring-v2`, pushed to `origin` (`4140e34..a489a87`)
- Handoff-only follow-up commit: `048bbca3fe43ee00afb3283f8deecdb53a923923`

## Files changed

Production code (new):
- `src/agent_sparring/providers/codex_cli.py` -- Codex CLI sparring adapter
- `src/agent_sparring/sparring_agent.py` -- start/resume orchestration,
  read-only enforcement, verdict parsing, sparring.md bookkeeping
- `src/agent_sparring/sparring_prompt.py` -- bounded sparring prompt assembly

Production code (modified):
- `src/agent_sparring/providers/__init__.py` -- added
  `SparringAgentResult`/`SparringAgentAdapter` (mirrors the existing
  `StageAgentResult`/`StageAgentAdapter` shape)
- `src/agent_sparring/cli.py` -- added `run-sparring` subcommand

Tests (new):
- `tests/test_providers_codex_cli.py` (13 tests)
- `tests/test_sparring_agent.py` (10 tests)
- `tests/test_sparring_prompt.py` (6 tests)

Tests (modified):
- `tests/test_cli.py` -- added `CliRunSparringTests` (4 tests)

Production code changed: **yes**. Tests changed: **yes**.

## Provider capability investigation (required before choosing)

Both `claude` (2.1.236) and `codex` (0.153.4) CLIs are installed locally.
Probed directly rather than assumed:

**Claude Code CLI** (already used for the Stage 3 stage-agent adapter):
`--permission-mode` gates tool use inside the CLI (e.g. `plan` mode avoids
edits) but is a cooperative/tool-gating setting, not an OS-enforced
sandbox -- nothing stops a differently-configured or future permission mode
from allowing a write.

**Codex CLI** -- confirmed by direct invocation in this repo, not assumed:
- `codex exec --sandbox read-only --json "<prompt>"` runs one turn and
  prints JSONL: a `thread.started` event carrying a `thread_id`, then
  `item.*` events, then `turn.completed`/`turn.failed`.
- `--sandbox read-only` is enforced by the OS: asking the model to write a
  file via its own shell tool produced `zsh:1: operation not permitted:
  PROBE_WRITE_TEST.txt` and no file was created, while read-only inspection
  (`git log`, `git status --porcelain`) still worked and returned real
  output.
- `codex exec resume <thread_id> "<prompt>"` continues that same thread and
  reports the same `thread_id` back (verified with a live prompt/response
  round-trip).
- `codex exec resume <unknown-uuid> ...` exits non-zero with a plain-text
  stderr error and no JSONL at all (`no rollout found for thread id ...`).
- `--output-schema <file>` (a strict JSON Schema file) plus
  `-o <file>`/`--output-last-message` produces a schema-validated final
  message written verbatim to that file. Codex's schema validator requires
  every `properties` key to also appear in `required` (an optional field
  must be expressed as a nullable type, not by omission) -- discovered via
  a live 400 error and fixed before the adapter's schema was finalized.

The VS Code Codex extension was explicitly **not** assumed to be
programmatically controllable, and was not probed further once the Codex
CLI's headless `exec`/`exec resume` surface was confirmed sufficient
(`codex exec` is a documented, versioned, scriptable subcommand entirely
separate from the editor extension).

**Decision:** Codex CLI is the first local sparring provider, using
`codex exec` for read-only enforcement, `--output-schema`/`-o` for the
structured verdict, and `codex exec resume <thread_id>` for the
same-context resume after a SEND_BACK cycle. (Round 2 below corrects how
read-only is actually passed to `codex exec resume` -- the `--sandbox`
flag used in round 1's `codex exec` invocation turned out not to work
there at all.) This also matches the plan's own `project.toml` example,
corrected in round 2 to use the actual adapter ids
(`[agents.stage] provider = "claude-cli"`, `[agents.sparring] provider =
"codex-cli"`) -- round 1's handoff text here previously claimed a match
against `provider = "claude"`/`"codex"`, which the plan said but the CLI
never accepted; that mismatch is fix 4 below.

## Design notes

- `SparringAgentResult`/`SparringAgentAdapter` are new types, deliberately
  not reusing `StageAgentResult`/`StageAgentAdapter`, since a provider may
  support one role and not the other (per the plan's "not every provider
  must initially support every operation").
- `CodexCliAdapter` writes the JSON Schema and `-o` output path to a fresh
  temp directory per invocation and is the only module that knows any
  Codex-specific flag/JSONL/schema detail; `sparring_agent.py` only depends
  on the generic `SparringAgentAdapter` protocol.
- Read-only enforcement is layered, not just trusted from the provider:
  `run_sparring_agent` snapshots `(HEAD commit, dirty paths)` before and
  after the provider turn and raises `SparringAgentRunError` if either
  changed, regardless of what sandboxing the provider itself claims to
  enforce. This is exercised directly by
  `test_read_only_enforcement_refuses_a_provider_that_commits` and
  `..._dirties_the_tree` in `test_sparring_agent.py`, using a fake adapter
  that writes to the repo -- independent of whether Codex itself is
  installed.
- `sparring_session_id` (already a `StageState` field from Stage 1/2) is
  never silently replaced on a resume-id mismatch, mirroring the existing
  stage-agent protection in `stage_agent.py`.
- The manual/web sparring path (`record-sparring` CLI command, `routing.py`,
  `sparring_exchange.py`) is completely untouched; `run-sparring` is a new,
  separate, optional CLI subcommand. A project not configuring
  `[agents.sparring]` and never invoking `run-sparring` sees no behavior
  change at all.
- Stage 5 (the unattended stage<->sparring loop) and Stage 6 (acceptance)
  are explicitly out of scope here and were not started.

## Verification

Full test suite (unittest, no pytest installed in this environment):

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

Result: all 15 test modules pass (`OK`), including the 3 new modules and
the extended `test_cli.py`. No `pytest` available on this machine
(`ModuleNotFoundError`); `unittest`, which every existing test file already
uses, was used instead -- consistent with the rest of the repo.

`git diff --check` on the staged changes: clean (no whitespace errors).

## Round 1 known limitations (superseded/updated by round 2 below)

- ~~`run-sparring` does not enforce an `--expected-branch`~~ -- fixed by
  round 2's fix 3.
- ~~a full live Codex sparring turn against a real stage was not executed
  end-to-end~~ -- done in round 2's live smoke test, which is exactly what
  surfaced the `--sandbox`-on-resume bug below.
- Still true after round 2: the read-only enforcement check
  (`_repo_fingerprint`) is a `(branch, HEAD, dirty paths)` snapshot, not a
  cryptographic guarantee -- a provider that edited a file and then
  restored it byte-for-byte, re-cleaned the index, and checked back out to
  the original branch would not be caught. Still documented explicitly in
  `sparring_agent.py`'s docstring as an accepted, deliberately cheap check.
- Still true after round 2: the adapter's own unit tests use an injectable
  fake `runner` rather than invoking the real CLI in CI, so a future Codex
  CLI release that changes its JSONL/schema/flag contract would only be
  caught by another live probe (see round 2's smoke test finding for a
  concrete example of exactly that kind of drift), not by the test suite.

## Review round 2: bounded fixes from independent review

- Base for this round: `a489a87e9efae89711c91b1f80df57bcd3a15b88`
  (round 1's implementation candidate)
- Round-2 candidate SHA: `f84e8d3c36adea0868eb257129c5197e0213bf8e`
  (fixes 1-4 plus the live-smoke-test fix)
- Branch: `feature/sparring-v2`, pushed to `origin`

Four bounded findings from independent review of round 1's candidate, all
resolved:

### Fix 1 -- preserve real human-readable sparring findings

`RoutingResult` stays tiny (`action`/`summary`/`needs_you_reason`/
`details`), unchanged. The Codex verdict envelope now additionally
requires `findings` (string) and `deferred` (nullable string) --
`providers/codex_cli.py`'s `VERDICT_SCHEMA`. `sparring_agent.py` splits the
envelope: `_build_routing_result()` builds the tiny `RoutingResult` (folding
`deferred`, if present, into `details["deferred"]` -- the same existing
path `sparring_exchange.render_sparring()` already reads for its
`## Deferred` section); `_extract_findings()` pulls the separate detailed
prose. `record_sparring()` is now called with the real `findings` text,
not the whole routing JSON. `sparring_prompt.py`'s verdict instructions
were updated to ask for both fields explicitly, with `summary` reframed as
"a short routing headline, not the whole story."

Regression:
`test_send_back_findings_and_summary_both_reach_sparring_md_and_next_stage_prompt`
in `tests/test_sparring_agent.py` uses a SEND_BACK verdict with a short
`summary` and a long, distinct `findings` string, asserts both land in
`sparring.md`, then calls `build_stage_prompt(..., resume=True)` (the
existing Stage 3 mechanism, unchanged) and asserts the next resumed
stage-agent prompt contains both too. Also added:
`test_missing_findings_field_is_refused` and
`test_deferred_reaches_sparring_md_deferred_section`.

### Fix 2 -- make the Codex sparrer actually read-only

`CodexCliAdapter.__post_init__` now refuses construction outright
(`ProviderError`) if `sandbox` is anything other than `"read-only"` -- no
writable-sandbox escape hatch remains. `cli.py`'s `run-sparring` no longer
exposes a `--sandbox` flag at all. A distinct contributor-sparrer mode (one
that edits and therefore forfeits independent acceptance authority, per
the plan's independence principle) was explicitly not built.

Regressions: `test_workspace_write_sandbox_is_refused_at_construction` and
`test_danger_full_access_sandbox_is_refused_at_construction` in
`tests/test_providers_codex_cli.py` assert `ProviderError`, replacing the
old `test_sandbox_and_model_are_configurable` (which had exercised a
writable sandbox as supported behavior -- exactly what the finding flagged).

**This fix is also where the live smoke test earned its keep** -- see
"Live end-to-end smoke test" below: the mechanism for actually enforcing
read-only on `codex exec resume` turned out to be different from
`codex exec`'s own flag, and round 1's code (and its unit tests, which
only asserted the flag was present in `args`, never that it worked) would
have been silently wrong on every resumed turn.

### Fix 3 -- enforce the expected branch for local sparring

`run_sparring_agent`'s `expected_branch` parameter is now required
(previously `str | None = None`); missing/blank raises
`SparringAgentRunError` up front, mirroring `run_stage_agent`. `cli.py`'s
`run-sparring --expected-branch` is now `required=True`, matching
`run-stage`. `_repo_fingerprint()` now returns `(branch, head, dirty_paths)`
instead of just `(head, dirty_paths)`: the worktree's actual branch is
checked against `expected_branch` before the provider is invoked (refusing
without invoking the provider on a mismatch), and the same three-way
fingerprint is compared before/after the turn, so a branch switch during
the turn is caught by the same integrity check that already caught writes
-- not a new, separate mechanism. This deliberately does not reuse
`branch_guard.ensure_branch_for_unattended_run` (which also bans
`main`/`master` outright): a sparrer may legitimately review `main`, so
only exact-identity matching applies here, not the implementation-side
writer policy.

Regressions in `tests/test_sparring_agent.py`:
`test_missing_expected_branch_is_refused_up_front`,
`test_wrong_branch_refuses_before_invoking_provider` (asserts
`adapter.start_calls == []`), and
`test_branch_changed_during_turn_is_refused_and_not_recorded` (a new
`_BranchSwitchingAdapter` fake checks out `main` mid-turn; asserts
`sparring_session_id` stays `None` and the disallowed verdict never reaches
`sparring.md`). Matching CLI-level coverage added in `test_cli.py`:
`test_run_sparring_without_expected_branch_is_rejected_by_argparse` and
`test_run_sparring_refuses_wrong_branch_before_invoking_provider`.

### Fix 4 -- make documented provider ids match the implementation

Chose the CLI's existing explicit-adapter-id vocabulary (`claude-cli`,
`codex-cli`) as canonical, per the review's stated preference ("future
providers may have CLI/API variants"). Updated the plan's own
`project.toml` example in
`docs/plans/active/2026-09-10-agent-sparring-foundation.md` from
`provider = "claude"`/`"codex"` to `provider = "claude-cli"`/`"codex-cli"`,
with a short note explaining why adapter ids, not bare vendor names, are
canonical. No provider registry was added -- the review's stated
preference was explicit, and a registry would not reduce code here (there
are exactly two providers, each already gated by one `if provider !=
"...-cli"` check in `cli.py`).

Regressions: `test_run_stage_configured_claude_cli_provider_id_is_accepted`
and `test_run_sparring_configured_codex_cli_provider_id_is_accepted` in
`tests/test_cli.py` write a `project.toml` with the documented provider id,
point `--claude-executable`/`--codex-executable` at a nonexistent binary,
and assert the failure is "could not launch ..." (proving the documented
id was accepted and provider resolution succeeded) rather than "unsupported
... provider" (which would prove the opposite).

### Files changed (round 2)

Production code (modified):
- `src/agent_sparring/sparring_agent.py` -- fixes 1 and 3
- `src/agent_sparring/providers/codex_cli.py` -- fixes 1 and 2, plus the
  `--sandbox`-on-resume bug found by the live smoke test
- `src/agent_sparring/sparring_prompt.py` -- fix 1 (verdict instructions)
- `src/agent_sparring/cli.py` -- fixes 2 and 3
- `docs/plans/active/2026-09-10-agent-sparring-foundation.md` -- fix 4

Tests (modified):
- `tests/test_sparring_agent.py` -- fixes 1 and 3 regressions (+5 tests)
- `tests/test_providers_codex_cli.py` -- fix 2 regressions plus the
  `-c sandbox_mode=...` fix (net +3 tests)
- `tests/test_cli.py` -- fixes 3 and 4 regressions (+4 tests)

Production code changed: **yes**. Tests changed: **yes**.

### Live end-to-end smoke test (required by this round, not skipped)

Run against a disposable, throwaway git repo created solely for this
purpose under the session scratchpad -- never against this candidate repo.
Sequence: `git init` a fresh repo, one base commit, one feature-branch
commit, `sparring new-stage` + `sparring handoff` (both real CLI
invocations against the disposable repo), then real `sparring run-sparring`
invocations against the real installed `codex` CLI (no fakes/mocks) twice
in a row -- fresh, then resumed.

Fresh run: exit 0, real `codex` produced a `READY` verdict with genuine
`findings` text referencing the actual diff, `state.json` recorded a real
provider-issued `thread_id`.

**Resumed run initially failed** with the real CLI: `codex exec resume
<id> --sandbox read-only ...` is a hard CLI parse error --
`error: unexpected argument '--sandbox' found` -- because `codex exec
resume`'s own flag set does not include `--sandbox` at all (its `--help`
never lists it). Manually probing further: a resumed session *without* any
sandbox override actually defaults to a **writable** sandbox -- a real
`printf hi > FILE` via the model's shell tool **succeeded** and created the
file when resumed with no override. This would have been a live violation
of fix 2's read-only guarantee on every resumed sparring turn, undetected
by round 1's unit tests (which only checked that a `--sandbox` string
appeared in the built `args` list, not that it had any effect on a real
resume call). The fix, also verified live:
`-c sandbox_mode="read-only"` (a `-c key=value` config override, not the
`--sandbox` flag) is accepted by, and verified to actually enforce
read-only on, *both* `codex exec` and `codex exec resume`. `_build_args()`
now always uses `-c sandbox_mode="..."`, never `--sandbox`, for both start
and resume; `providers/codex_cli.py`'s module docstring records this
finding in full. Unit tests updated to assert `--sandbox` is never present
and the `-c sandbox_mode=...` value is correct, for both start and resume
(`test_default_sandbox_is_read_only`,
`test_resume_args_include_resume_subcommand_and_session_id`).

Re-ran the full fresh+resume sequence against a fresh disposable repo after
the fix: fresh run exit 0 (`resumed=False`), resumed run exit 0
(`resumed=True`, identical `thread_id`), both `READY` with real per-run
`findings` text. Confirmed via `git status --porcelain`/`git log
--oneline` on the disposable repo, and separately on this candidate repo,
that no tracked file was touched by any part of the smoke test. The
disposable repo was deleted afterward; it never touched
`feature/sparring-v2` or any file under `/Users/sigmundas/Documents/Code/
agent-sparring`.

### Verification (round 2)

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

All 15 test modules pass (`OK`), plus the live smoke test above.
`git diff --check`: clean.

### Known limitations / unresolved questions (round 2, superseded/updated by round 3 below)

- ~~a real escape hatch remained: `CodexCliAdapter.sandbox` was still a
  mutable field after construction, and `extra_args` could inject an
  arbitrary config override~~ -- fixed by round 3's fix 1.
- ~~the repo-integrity check was skipped when the provider itself
  raised/mismatched/returned an unparseable verdict~~ -- fixed by round
  3's fix 2.
- The live smoke test in round 2 is exactly the kind of check that caught
  a real bug the unit tests missed (a fake `runner` cannot detect that a
  CLI subcommand rejects a flag, or that an unrelated default changed). It
  was run manually in that session, not wired into CI; a future Codex CLI
  release changing this contract again would again only be caught by
  another manual live probe.
- Still true after round 3: the `(branch, HEAD, dirty paths)` fingerprint
  still cannot detect a provider that edits-then-restores a file
  byte-for-byte while also checking back out to the original branch --
  documented, not solved.
- No `.sparring/project.toml`/`PROJECT.md` exists yet for this repo itself
  (Stage 7 in the plan is the Sporely pilot).
- Stage 5 (the unattended stage<->sparring loop) and Stage 6 (acceptance)
  remain explicitly out of scope and were not started in any round.

## Review round 3: remaining read-only-integrity findings

- Base for this round: `f84e8d3c36adea0868eb257129c5197e0213bf8e`
  (round 2's fix candidate)
- Round-3 candidate SHA: `ab031666e0a823e45469b495e6a0054e5cf4b182`
- Branch: `feature/sparring-v2`, pushed to `origin`
  (`c95fad8..ab03166`)

Two further bounded findings from independent review, both resolved:

### Fix 1 -- remove the remaining programmatic write-permission escape hatch

Round 2 made `CodexCliAdapter.__post_init__` reject `sandbox != "read-only"`
at construction, but `sandbox` remained a normal mutable dataclass field
afterward (`adapter.sandbox = "workspace-write"` would silently change what
`_build_args()` used), and `extra_args` was appended after the fixed
sandbox config, so a caller could inject an arbitrary Codex/config flag --
including a conflicting sandbox override -- through it.

`CodexCliAdapter` no longer has a `sandbox` field or an `extra_args`
field at all. The verified read-only config pair is now a module-level
constant, `_SANDBOX_CONFIG_ARG = ("-c", 'sandbox_mode="read-only"')`, that
`_build_args()` always includes; there is no field, parameter, or
attribute anywhere on the class that changes it. Setting an unrelated
same-named attribute on an instance after construction (`adapter.sandbox =
...`) is still possible in plain Python, but has no effect, since
`_build_args()` never reads any such attribute. `model`/`executable`/
`timeout_seconds`/`runner` remain configurable, unchanged. No
flag-sanitization machinery and no contributor-sparrer mode were built.
Module/class docstrings were also corrected: they no longer describe
`--sandbox read-only` as this adapter's current invocation (that flag is
what the very first exploratory probe used on a fresh `codex exec` call,
before round 2 discovered `codex exec resume` rejects it outright); the
verified, always-used mechanism is `-c sandbox_mode="read-only"` for both
start and resume.

Regressions in `tests/test_providers_codex_cli.py`:
`test_sandbox_is_not_a_supported_constructor_parameter` and
`test_extra_args_is_not_a_supported_constructor_parameter` (both assert
`TypeError` -- an unknown constructor keyword, not a validated-and-rejected
one), and `test_mutating_sandbox_attribute_after_construction_has_no_effect`
(sets `adapter.sandbox = "workspace-write"` after construction, then
asserts the built argv still carries the fixed read-only config and never
`--sandbox`). The existing `test_default_sandbox_is_read_only` (fresh) and
`test_resume_args_include_resume_subcommand_and_session_id` (resume) were
kept as the "both fresh/resume argv contain the fixed verified read-only
config" coverage the finding asked for.

### Fix 2 -- run the repo-integrity check even when the provider fails

Round 2's `run_sparring_agent` only computed the after-turn fingerprint
inside the success path: a `ProviderError` from `adapter.start()`/
`resume()`, a resume session-id mismatch, or an unparseable verdict all
raised *before* the repository was ever re-fingerprinted, so the
independent read-only backstop covered only a successful-looking turn --
exactly the case it matters least for.

`run_sparring_agent` now captures the provider call's outcome without
immediately raising (`result: SparringAgentResult | None`,
`provider_error: ProviderError | None`), always recomputes the
`(branch, HEAD, dirty_paths)` fingerprint next regardless of which
happened, and only then decides what to raise: an integrity violation is
raised first (folding in a note that the provider also failed, if it
did), then a bare provider failure, then a resume session-id mismatch,
then a verdict-parse failure -- in that order, all strictly after the
unconditional integrity check. Nothing is rolled back or reset in any
case; the fingerprint itself is unchanged (still the same deliberately
cheap `(branch, HEAD, dirty_paths)` snapshot, not a forensic audit).

Regression: `test_integrity_check_still_catches_a_write_when_provider_also_fails`
in `tests/test_sparring_agent.py` uses a new `_WritingThenFailingAdapter`
fake that writes and commits a file and then raises `ProviderError`.
Asserts: the raised `SparringAgentRunError` message contains "read-only
contract violated" (the integrity violation, not just a provider-crashed
message) and "provider also failed"; `sparring_session_id` stays `None`;
and `sparring.md` is byte-for-byte unchanged from before the call.

### Verification (round 3)

```
cd /Users/sigmundas/Documents/Code/agent-sparring/tests
for f in test_*.py; do python3 "$f"; done
```

All 15 test modules pass (`OK`). `git diff --check`: clean.

No live Codex smoke test was run in this round: per the review's own
guidance, neither fix altered the actual Codex invocation shape beyond
hard-coding the config arg pair round 2 had already verified live
(`-c sandbox_mode="read-only"`); fix 2 is pure orchestration-layer control
flow with no provider-facing change at all.

### Known limitations / unresolved questions (round 3)

- The `(branch, HEAD, dirty paths)` fingerprint still cannot detect a
  provider that edits-then-restores a file byte-for-byte while also
  checking back out to the original branch. Unchanged from round 1/2;
  still documented, not solved, in `sparring_agent.py`'s module docstring.
- `CodexCliAdapter`'s own unit tests still use an injectable fake `runner`
  rather than the real CLI in CI, per rounds 1-2's note; round 2's live
  smoke test is the concrete precedent for how contract drift here would
  actually be caught.
- No `.sparring/project.toml`/`PROJECT.md` exists yet for this repo itself
  (Stage 7 in the plan is the Sporely pilot).
- Stage 5 (the unattended stage<->sparring loop) and Stage 6 (acceptance)
  remain explicitly out of scope and were not started in this round.
