# Issue #5 Plan L kit

These scripts run Plan L (`docs/plans/issue5-l-local-first-engine.md`, from the
version 2 manifest `docs/plans/issue5-l.manifest.json`) in isolation, and test
each candidate it produces. They run as the credential-free macOS account
`sparring-agent`, from a separate login session, with that account's login
keychain unlocked.

They are derived from the reviewed Plan R2 kit (`docs/audits/issue5-r2-kit/`).
- The R2 kit, its installed copy and the Plan R evidence in
  `/Users/Shared/sparring-recovery-r2/` stay unchanged.
- The gate checks that this account cannot write either of them.

**Installed copies** live in `/Users/Shared/sparring-recovery-l/`. The
owner's account owns that directory, and `sparring-agent` cannot write it. The
copies are byte-identical to this directory; compare them with `SHA256SUMS`
there.
- `l.bundle` holds the `plan/issue5-local-first` branch only.
- `PLAN_SHA` is its tip. `BUNDLE_SHA256`, `MANIFEST_SHA256` and
  `RUNNER_SHA256` (the frozen runner's fingerprint) pin the rest.

## Steps

| Step | Command (as `sparring-agent`) | Effect |
|---|---|---|
| 0 | `sh /Users/Shared/sparring-recovery-l/gate.sh baseline` | The presence-only credential gate. Every line must say `ok`. |
| 1 | `sh /Users/Shared/sparring-recovery-l/setup-l.sh` | Creates `~/recovery-l/remote.git` (a local bare remote) and `~/recovery-l/agent-sparring`, runs `uv sync --frozen`, and adds Claude allow rules and raised Bash timeouts. |
| 2 | `sh /Users/Shared/sparring-recovery-l/preflight-l.sh` | Proves the stage agent, started as the frozen runner starts it, can run `uv sync` and a real-Git test file longer than the default timeout. Also self-tests the probe harness in this account. Must print `PASS`. About 12 minutes. |
| 3 | `sh /Users/Shared/sparring-recovery-l/preview-l.sh` | The gate, `check-plan` on the plan and on the manifest, the ten enforced gates, the per-gate test commands and the manifest sha256. Starts nothing. |
| 4 | `sh /Users/Shared/sparring-recovery-l/run-l.sh <manifest sha256>` | **Starts Plan L.** Only after a person has approved step 3. |
| 5 | `sh /Users/Shared/sparring-recovery-l/test-l.sh <sha> <accepted stage> [--require-ids FILE]` | At each gate stop: the isolated test of the exact accepted SHA, plus the kit's probes. |
| 6 | `sh /Users/Shared/sparring-recovery-l/resume-l.sh --run-key <key> --deferred-result <instance>:<gate id>=pass` | Answers a gate and continues the run. |
| 7 | `sh /Users/Shared/sparring-recovery-l/export-l.sh` | After the run stops, bundles the local remote for independent review. |

## The ten gates

The frozen runner enforces these from the manifest. Only
`resume-plan --deferred-result` releases them.

| Checkpoint | Gate ids | Test command |
|---|---|---|
| Before L2 | `isolated-tests-before-issue5-l-stage-2-local-acceptance-and-history` (manual), `go-ahead-issue5-l-stage-2-local-acceptance-and-history` (authorization) | `test-l.sh <L1 sha> 1` |
| Before L3 | `isolated-tests-before-issue5-l-stage-3-unconditional-remote-refusal`, `go-ahead-issue5-l-stage-3-unconditional-remote-refusal` | `test-l.sh <L2 sha> 2` |
| Before L4 | `isolated-tests-before-issue5-l-stage-4-local-finish-and-legacy-migration`, `go-ahead-issue5-l-stage-4-local-finish-and-legacy-migration` | `test-l.sh <L3 sha> 3` |
| Before L5 | `isolated-tests-before-issue5-l-stage-5-local-git-hardening`, `go-ahead-issue5-l-stage-5-local-git-hardening` | `test-l.sh <L4 sha> 4` |
| Before completion | `isolated-tests-final`, `go-ahead-complete` | `test-l.sh <final sha> 5` |

**Answering a gate.** Answer `pass` only if `SUMMARY.txt` says "gate answer this
supports: pass". That requires all of these:
- the focused set and the full suite exit 0 at the exact SHA, with `HEAD`
  unchanged and no tracked file changed;
- every kit probe up to that stage passes;
- every id in `--require-ids` is present and passing;
- either no weakening flag is raised, or each one matches the handoff.

Copy the evidence directory to the owner's account before answering.

## The kit's probes (`probe-l.py`)

These are independent black-box checks, written outside the run, that drive the
candidate's own `sparring` CLI:
- in disposable repositories;
- with fake `claude` and `codex` executables, using the protocol of the
  reviewed tests (`tests/test_plan_activity.py` at `ea8e70d`);
- with a recording `git` wrapper first on `PATH`.

Legacy run state is produced by **the frozen pre-Plan L runner itself**, so
migration is tested against a real `push_authorization_required` pause.

| Probe | From stage | What it shows |
|---|---|---|
| `L2-local-run` | 2 | A two-stage run is accepted with no remote at all, and again with a bare remote whose refs and hook stay untouched. No remote Git command is recorded. |
| `L2-deprecated-flags` | 2 | `--allow-push-for-run` refuses with `push_not_part_of_runs`: nothing is created, and no provider starts. `--allow-push-candidate <sha>`, on a real legacy pause, refuses with `push_permission_deprecated`: `.sparring`, refs, `HEAD`, worktrees and status are byte-identical, and no provider starts. |
| `L2-history-primary` | 2 | A legitimate advance is accepted (control). A reset that rewrites the accepted stage-1 commit refuses with `run_history_rewritten`. |
| `L2-history-sibling` | 2 | The same in a sibling repository, through a manifest with `repositories`. The primary advances legitimately while only the sibling is rewritten. |
| `L2-capability` | 2 | `capabilities --json` and `runs --json` list `local_first`, and `schema_version` is 1. |
| `L3-remote-refusal` | 3 | `engine_git.run` refuses `push`, `fetch`, `ls-remote`, `pull`, and forms using `-c`, `--git-dir` and `-C`, with `remote_operation_not_allowed`. The bare remote is never reached. Control: plain `git push` reaches it. |
| `L3-planted-config` | 3 | Planted `core.sshCommand`, `credential.helper`, `url.*.insteadOf` and an `ssh://` origin: the run completes, `runs --json` works, no remote command is recorded and no planted program runs. Control: plain `git ls-remote` runs the planted `sshCommand`. |
| `L4-legacy-migration` | 4 | A plain `resume-plan` converts a qualifying legacy pause: the stage is accepted locally, `legacy_push_pause_converted` is recorded, no implementation turn runs, and nothing is pushed. A moved `HEAD`, a dirty tree or an edited plan each keeps the pause, accepts nothing and pushes nothing. |
| `L4-local-finish` | 4 | `finish-run --push-target` refuses with `remote_finish_removed`, and nothing is merged. A local finish merges the target, its JSON offers no remote step and lists `local_first`, and no remote command runs. |

**Self-test.** `probe-l.py --self-test` points every probe at the frozen
pre-Plan L engine.
- The harness controls must pass, and every local-first probe must fail a
  property while its own controls pass. That proves each probe tells the two
  engines apart for the right reason.
- `L3-remote-refusal` cannot be self-tested this way, because the frozen
  engine has no `engine_git`. Only its plain-Git control is checked.

**Not covered by the probes.** These are covered by the stage's own tests,
named in the handoff and enforced with `--require-ids`:
- the L-D1 conditions that need verdict or gate state the fake reviewer does
  not produce: a non-`READY` verdict, a promoted obligation, an unsafe
  deferred gate, and an unanswered human check;
- a moved sibling at conversion;
- R-4 and R-6 to R-8 (Stage 5).

## Limits: what the engine's remote refusal does and does not stop

- **What it stops.**
  - Plan L makes the **engine's own** Git calls refuse every remote
    operation.
  - That covers status polling, review, acceptance and finish. A repository's
    planted configuration then cannot make the engine contact a remote or run
    a transport helper.
- **What it does not stop.** It does nothing about a stage agent's own shell.
  - Claude's `Bash` tool, or Codex's shell, can run `git push` directly, with
    whatever credentials the operating-system account holds.
  - The Claude deny rule `Bash(git push:*)` that `setup-l.sh` adds is a
    workflow setting. It can be bypassed: through another wrapper, a script,
    `git -c …`, a different binary, or a network client.
- **In this kit,** that is contained only because:
  - `sparring-agent` holds no Git credentials, which the presence-only gate
    checks;
  - its only remote is a local disposable bare repository;
  - every result is untrusted until tested and reviewed.
- **In the user's normal account,** none of this holds. Preventing agent pushes
  there is Plan D (separate OS accounts).
- **The run itself still pushes.** The frozen runner is the pre-Plan L engine.
  It still pushes each accepted candidate, but only to the local disposable
  remote, under `--allow-push-for-run`. Plan L changes the candidate, not the
  runner that executes the run.

## Known limits (accepted, disclosed)

- **The tests run in the agents' own account.** The mitigations are a fresh
  clone, a fresh uv cache, no user site, a refusal while a provider process
  is alive, and evidence copied to the owner. Plan R2's decision R2-D5
  carries over.
- **The probes are written against the plan's CLI contract** (codes, flags,
  JSON fields) before the code exists.
  - A probe can fail because the implementation chose a compatible but
    different form, for example where a code appears in the output.
  - Such a failure blocks the gate until a person decides whether the probe
    or the candidate is wrong.
  - A probe is changed only through a new kit commit, never during a run.
- **Not prevention.** This is a same-account setup under D1 and D4.
