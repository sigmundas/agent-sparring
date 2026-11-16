# Issue #5 Plan R2 kit

These scripts run Plan R2
(`docs/plans/issue5-r2-complete-and-verify-engine-git-authority.md`) in
isolation, and test each candidate it produces. They run as the
credential-free macOS account `sparring-agent`, from a separate login session.
The login keychain must be unlocked so the gate's keychain lookups mean
something.

**Installed copies.** They live in `/Users/Shared/sparring-recovery-r2/`.
The owner's account owns that directory, and `sparring-agent` cannot write it.
The copies are byte-identical to this directory; compare them with
`SHA256SUMS` there.

**Files written outside the repository when the kit is installed:**
- **`r2.bundle`.** The `plan/issue5-r2` branch only.
- **`PLAN_SHA`.** Its tip.
- **`BUNDLE_SHA256`.**
- **`MANIFEST_SHA256`.** The sha256 of `docs/plans/issue5-r2.manifest.json`
  at `PLAN_SHA`.
- **`RUNNER_SHA256`.** The frozen-runner fingerprint, the same file as in the
  recovery kit.
- **`evidence/`.** Owner-owned copies of the isolated test evidence, starting
  with the Plan R run of `36e2cea`.

## Steps

| Step | Command (as `sparring-agent`) | Effect |
|---|---|---|
| 0 | `sh /Users/Shared/sparring-recovery-r2/gate.sh baseline` | The presence-only credential gate. Every line must say `ok`. |
| 1 | `sh /Users/Shared/sparring-recovery-r2/setup-r2.sh` | Creates `~/recovery-r2/remote.git` and `~/recovery-r2/agent-sparring`, runs `uv sync --frozen`, and adds Claude permission rules for the test and Git commands. |
| 2 | `sh /Users/Shared/sparring-recovery-r2/preflight-r2.sh` | Proves a stage agent started as the frozen runner starts one can run `uv sync`, and a real-Git pytest file longer than the default Bash timeout, in a worktree without `.venv`. Must print `PASS`. Takes about 10 minutes. |
| 3 | `sh /Users/Shared/sparring-recovery-r2/preview-r2.sh` | The gate, `check-plan` on the plan and on the manifest, the enforced gates and the manifest sha256. Starts nothing. |
| 4 | `sh /Users/Shared/sparring-recovery-r2/run-r2.sh <manifest sha256>` | **Starts Plan R2.** Only after a person has reviewed step 3. |
| 5 | `sh /Users/Shared/sparring-recovery-r2/test-r2.sh <sha>` | At each gate stop: the isolated test of the exact accepted SHA. |
| 6 | `sh /Users/Shared/sparring-recovery-r2/resume-r2.sh --run-key <key> --deferred-result <instance>:<gate id>=pass` | Answers a gate and continues the run. |
| 7 | `sh /Users/Shared/sparring-recovery-r2/export-r2.sh` | After the run stops, bundles the local remote for independent review. |

## Why this differs from the Plan R kit

- **The runner enforces the human checks.**
  - Plan R ran from Markdown, with no stop between stages. Its reviewers
    accepted both stages without a test having run.
  - Plan R2 runs from a version 2 manifest generated from the plan by
    `make-manifest-r2.py`. Before Stage R2.2, before Stage R2.3 and before
    completion, the frozen runner stops at two gates:
    - an isolated-test gate (`manual`);
    - then a go-ahead (`authorization`).
  - Only `resume-plan --deferred-result` releases a gate.
- **The test environment is proven first.**
  - In the Plan R run, the managed worktree had no `.venv`, and Claude
    refused the Bash `pytest` call for approval.
  - Step 1 adds allow rules for exactly the commands the plan requires, and
    a deny rule for `git push`.
  - Step 2 shows that those commands really run, as the runner launches the
    agent.
- **`run-plan` has no dry run.** So the manifest sha256 from step 3 is the
  confirmation token. `run-r2.sh` refuses any other value, or a manifest
  that differs from the kit's pin.

## How to answer an isolated-test gate

1. Wait until the run has stopped. `test-r2.sh` refuses while a provider or
   run process of the account is alive.
2. Run `test-r2.sh` with the full SHA of the accepted candidate. The stop
   message and the run's JSON name it.
3. Copy `/Users/Shared/sparring-outbox/test-r2-<sha>-<time>/` to the owner's
   account and check its `SHA256SUMS`. The outbox is writable by
   `sparring-agent`, so its copy is not durable evidence.
4. Answer `pass` only if `SUMMARY.txt` says "gate answer this supports: pass".
   Otherwise answer `fail`, with the evidence path.

## Known limits (accepted, disclosed)

- **The frozen runner (`c61eaf8`, source `ea8e70d`) predates Plan R.** Its
  stage agents run without a provider boundary under `acceptEdits`. That is
  acceptable only because:
  - the only remote is a local disposable bare repository;
  - the account has no Git credentials;
  - every result is untrusted until it has been tested and reviewed.
- **The tests run in the same account as the agents.**
  - An agent turn could leave something behind that affects a later test in
    that account. The test uses a fresh clone, a fresh uv cache,
    `PYTHONNOUSERSITE=1` and the hash-checked `uv.lock`, and refuses to run
    alongside a live provider process.
  - It does not protect a uv-managed interpreter in the account's home. The
    interpreter path and sha256 are recorded, so they can be compared across
    runs.
  - A separate test account would remove this risk (decision R2-D5).
- **Not prevention.** This is a same-account setup under D1 and D4. It
  demonstrates nothing about prevention.
