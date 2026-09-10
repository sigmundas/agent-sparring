# Stage 4 handoff: Sparring adapter

Date: 2026-09-10

## Branch state

- Base SHA: `4140e347f579bcbb465eecc769c625dcc7c99eb5` (verified as the exact
  HEAD of `feature/sparring-v2` before starting; no divergence)
- Candidate SHA: `a489a87e9efae89711c91b1f80df57bcd3a15b88`
- Branch: `feature/sparring-v2`, pushed to `origin` (`4140e34..a489a87`)

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
`codex exec --sandbox read-only` for read-only enforcement,
`--output-schema`/`-o` for the structured verdict, and `codex exec resume
<thread_id>` for the same-context resume after a SEND_BACK cycle. This also
matches the plan's own `project.toml` example
(`[agents.stage] provider = "claude"`, `[agents.sparring] provider =
"codex"`).

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

## Known limitations / unresolved questions

- The Codex CLI probes above were run live against real `codex`/`claude`
  installs on this machine during this stage, but the new adapter's own
  test suite uses an injectable fake `runner` (same pattern as
  `ClaudeCliAdapter`) rather than invoking the real CLI in CI -- consistent
  with the existing Stage 3 adapter's test strategy, but it means a future
  Codex CLI release that changes its JSONL/schema contract would only be
  caught by another live probe, not by the test suite.
- The read-only enforcement check (`_repo_fingerprint`) is a `(HEAD, dirty
  paths)` snapshot, not a cryptographic guarantee: a provider that edited a
  file and then restored it byte-for-byte and re-cleaned the index would
  not be caught. Documented explicitly in `sparring_agent.py`'s docstring
  as an accepted, deliberately cheap check rather than a formal proof.
- `run-sparring` does not enforce an `--expected-branch` the way
  `run-stage` enforces a branch guard, since the sparrer never writes;
  `--expected-branch` is optional context included in the prompt only.
- No `.sparring/project.toml`/`PROJECT.md` exists yet for this repo itself
  (Stage 7 in the plan is the Sporely pilot); `run-sparring --dry-run` was
  exercised in tests but a full live Codex sparring turn against a real
  stage was not executed end-to-end in this session beyond the standalone
  CLI probes above.
