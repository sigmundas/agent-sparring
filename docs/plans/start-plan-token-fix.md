# start-plan: bind every execution input into the confirmation token

Repository: `agent-sparring`. Status: approved. Branch
`feature/run-plan-compiler`, after the accepted Stage 3 of
`docs/plans/run-plan-compiler.md` (`82e96f1`). An independent review found
that the confirmation does not cover every input that changes what runs.
The engine cannot reopen an accepted stage, so the fix runs here; the
`run-plan-compiler` run stays paused before its Stage 4. Keep it small.

## Stage 1 — Close the start-plan confirmation gaps (M1, L1, L2)

### M1: execution inputs missing from the token

`plan_start.py` builds the token from plan/source digests, branch,
repository heads, models and the push flag, but not from
`--permission-mode`, `--claude-executable` / `--codex-executable` (and any
other provider executable option), or `--max-send-back-cycles`. A status
reviewed with the default permission mode can therefore be confirmed with
`--permission-mode bypassPermissions` or another executable, and the token
still matches — contradicting "what the person saw is what runs".

Required: include every option that changes what runs (at least permission
mode, each provider executable as given, max send-back cycles, and any
other run-affecting option `start-plan` accepts — audit the parser) in the
token for both the direct and the intake route, and show them in the
status the person reviews. A mismatch refuses with the standard refusal
schema and writes nothing.

### L1: direct route time-of-check / time-of-use

After the token check, the direct route re-reads the plan from disk before
running. A plan edited in that window runs unchecked. Required: run exactly
the content that was checked (pass the checked source through, or re-verify
the digest immediately before handing it to the runner and refuse on
mismatch). The intake route already runs its sealed manifest; leave it.

### L2: undeclared `--repository-branch`

A `--repository-branch` naming a repository the selected slice does not
declare is merged into the manifest's `repository_branches`. Required:
refuse it with a clear message naming the unknown repository and the
declared ones; write nothing.

### Tests

- Token changes and `--confirm` refuses when any of permission mode, each
  executable, or max send-back cycles differs from the evaluated status
  (both routes); status output shows them.
- End-to-end: the token changes when an answered decision, the intake
  report, or a manifest gate changes.
- Direct route: a plan edited after the token check is not run.
- Undeclared `--repository-branch` refused, nothing written.
- Existing start-plan, approve-plan, gate and fresh-session tests pass.

Run the full suite with an isolated HOME
(`HOME=$(mktemp -d) .venv/bin/python -m pytest -q`).
