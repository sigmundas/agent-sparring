# Credential accessibility and truthful guarantees (issue #5, Plan B)

The engine records, for every provider invocation, what it actually knows about
write-capable Git credentials reachable from the agent. It claims "prevented"
only on evidence. Errors and unknowns never read as absence, and no credential
value ever reaches a log, a record or an exception.

This plan replaces the credential half of the failed Stage 2 of
`docs/plans/secure-agent-git-writes.md`. The audit is
`docs/audits/issue5-recovery-2026-10-18.md`, findings F4 and F5 and §2c.

**Approved decisions (2026-10-18).** See D1, D4, R-1 and R-2 in the audit.
- A verification record never authorizes execution. Probe outcomes and table
  rows decide only what is *claimed*.
- The single exception is `probe_failed`. It refuses the invocation because
  the engine cannot measure, not because evidence is missing.

**Depends on:**
- Plan R, merged.
- Plan A Stage 1, for `LaunchSpec`, the run policy and the capability table.
  Stage B1 needs only Plan R. Stages B2 and B3 need A1.
- Land on `issue5/boundary`.

**Why per-invocation measurement is mostly outside the boundary.** This is a
recorded design decision.
- Claude offers no known model-free way to run a command inside the boundary
  of a real invocation. Running the probe *as* the model would make the agent
  the author of its own evidence.
- The sandbox only removes access. So a complete enumeration outside the
  boundary that finds **no** credential proves none is reachable inside it.
- A credential that **is** present outside can be credited as blocked only by
  a reviewed capability-table row. That row covers this credential class,
  provider, role and exact version, and comes from Plan C's human-run evidence.
- So "measured inside every invocation" is unsupported for Claude on macOS.
  The plan records that limitation instead of approximating it.

## Security invariants (every later refactor must keep these)

- **I-B1. Four outcomes.** Each credential class is `absent`, `present`,
  `indeterminate` or `probe_failed`.
  - `indeterminate` covers any non-zero exit, timeout, unparseable output or
    unknown helper. It never counts as `absent`.
  - `probe_failed` means the probe could not launch at all. It refuses the
    invocation.
- **I-B2. "Prevented" needs evidence and isolation (D1).** The guarantee is
  `prevented` only when both of these hold:
  - the run's execution mode is `isolated`, with a `verified` boundary for
    every execution path;
  - every class is `absent`, or is `present` and covered by a `verified`
    table row for this exact launch and every execution path that could reach
    it.

  A `same_user` run (the compatibility mode) is capped at
  `detected_not_prevented`. That holds even when every class is `absent`,
  because engine state and authorization are not behind an OS boundary
  (D4).
  - Any `present` class that is not covered gives `detected_not_prevented`.
  - Any `indeterminate` class gives `unverified`, unless another class
    already gives `detected_not_prevented`.
- **I-B3. Monotonic guarantee.** The run guarantee is the worst ever recorded
  for that run. Resume cannot improve it.
- **I-B4. Presence only.** Probes never request or print a secret. Commands
  that would print one (`security -w`, a `credential fill` that returns a
  password) are not used, or their output is discarded in-process. Exception
  text carries only a class name and an exception type.
- **I-B5. No repository configuration.** Probes never run repository-scope
  configuration or helpers. They use user- and system-scope configuration
  through `engine_git` hardening, or direct, fixed commands.

## Stage 1 — Complete, presence-only credential enumeration with four outcomes

**Context (at `1700b4d`):**
- `agent_boundary.py:1225-1226` treats a non-zero `git credential fill` as
  absent.
- `:1254` checks only internet passwords.
- `:1146-1147` reports a hostless remote as absent.
- `:1218` runs repository-configured helpers outside the sandbox and without
  hardening.

**Outcome.** A new module, `credential_probe.py`, enumerates these classes for
the pinned remote's host and port:
- **Keychain internet passwords.** `security find-internet-password -s <host>`
  without `-w`/`-g`. The exit code alone decides the outcome.
- **Keychain generic passwords.** Known Git Credential Manager and `gh` service
  names (`git:https://<host>`, `gh:<host>`, and a documented list). Presence
  is read from the exit code.
- **Configured credential helpers.**
  - These are the helpers in user- and system-scope config, plus the
    run-start trusted snapshot.
  - `osxkeychain` and `manager` are classified by the keychain probes above.
    `store` is classified by checking whether its file exists and holds a
    matching host line; the password is never read.
  - Any other helper is `indeterminate`. Arbitrary helpers are never executed.
- **`gh`.** `hosts.yml` lists the host, or the keychain holds a `gh:` item.
  Nothing contacts the network.
- **SSH.**
  - Private keys in `~/.ssh` (any filename) and every `IdentityFile` named
    in `~/.ssh/config`.
  - `ssh-add -l` exit status: 0 means present, 1 means absent, any other value
    means indeterminate. Its output is discarded.
- **Environment.** Any of `GITHUB_TOKEN`, `GH_TOKEN`, `GITLAB_TOKEN`,
  `CI_JOB_TOKEN`, `HOMEBREW_GITHUB_API_TOKEN` or `GIT_CONFIG_PARAMETERS`/`COUNT`
  in the **launch** environment. By Plan A's allowlist this is expected to be
  absent; the probe checks the `LaunchSpec` environment, not the engine's.
- **Credential files.** `~/.git-credentials`, `~/.config/git/credentials` and
  `~/.netrc`, each checked for a matching host.
- **Local-path remote.**
  - The remote path lies under any agent-writable root → `present` (class
    `writable_local_remote`).
  - Otherwise `absent`.

Each class returns one of the I-B1 outcomes. The module exposes no API that
returns a secret.

**Starting points:**
- `agent_boundary.py` at `1700b4d`, as reference only: `probe_credentials`,
  `_git_credential_helper_has`, `_keychain_has`.
- `engine_git.py` for user- and system-scope config reads.
- `remote_pin.py` for the pinned host.

**Boundaries:**
- No recording or surfaces (Stage 2).
- No in-boundary or model-driven probes.
- Do not edit the real keychain. Tests use stub `security`, `ssh-add` and
  `gh` executables on a test `PATH`, and a test `HOME`.

**Acceptance evidence:**
- **Outcome table.** A test table covers every class × each of: absent,
  present, non-zero exit, timeout, garbage output, unlaunchable binary. It
  asserts the I-B1 outcome.
- **Explicit cases:**
  - A helper that exits 1 → `indeterminate`.
  - An unknown helper in `~/.gitconfig` → `indeterminate`, and its marker
    program is **not** executed.
  - A repository-scope `credential.helper=!<marker>` → never executed, and has
    no influence.
- **Canary hygiene.**
  - Every stub that could emit a secret prints a fixed canary,
    `CANARY-5f1c…`.
  - The test captures every log record, every exception string and the probe's
    return value, and asserts the canary appears in none of them.
  - Only counts are printed.
- **Local-path remote.** A remote inside `$TMPDIR` → `writable_local_remote`
  present. A remote under a non-writable test root → absent.
- **Real subprocess, no credentials.** One test runs on the CI or test account
  with real `security` and `ssh-add` against an empty test `HOME`. The outcome
  is `absent` or `indeterminate`, never a crash. This is the only
  real-subprocess test, and it is skipped if the account holds credentials (a
  presence-only precheck).
- **Test runs.** Focused tests and the full suite pass.

## Stage 2 — Per-invocation ledger, table crediting and recorded guarantee

**Context.** At `1700b4d` the probe runs once at `start_plan`/`resume_plan`
(`plan.py:1600,2121-2130`), not per turn, `run-stage` or reviewer. Resume can
also downgrade "detected" to "prevented".

**Outcome:**
- **Probe before every launch.** The Stage 1 enumeration runs before **every**
  provider invocation that the builder produces: start, turn, resume, reviewer,
  `ask`, `run-sparring`, intake and `run-stage`. `probe_failed` refuses that
  invocation.
- **The ledger.** An append-only credential ledger is kept in engine-protected
  state (Plan A Stage 2's protected paths). Each entry holds:
  - the invocation id;
  - provider, role and kind;
  - the `LaunchSpec` digest;
  - per-class outcomes (no values);
  - credited classes, with the table row ids;
  - the resulting guarantee.
- **Mode cap.**
  - The guarantee computation takes the run's execution mode.
  - `same_user` caps the result at `detected_not_prevented`.
  - Until Plan D lands, no run reaches `prevented`. The surfaces then say
    "compatibility mode: detection only".
- **Crediting.**
  - Crediting uses only reviewed table rows (Plan A Stage 1), and only for
    the execution paths the row names.
  - Claude rows for keychain classes on macOS are `unsupported` by
    construction. The table module asserts this in a test, citing the platform
    limitation.
- **Run guarantee.** The worst value in the ledger. It is shown in the
  `start-plan` confirmation, `check-config`, `sparring runs --json` and each
  stage report.
- **Wording.**
  - `detected_not_prevented` is worded "unauthorized remote writes: detected,
    not prevented".
  - `unverified` is worded "credential reachability: unverified".
  - Neither word "protected" nor "prevented" appears for those states.

**Starting points:** `credential_probe.py`, `launch_spec.py`, `plan.py`
(`PlanRunState.agent_guarantee`, `start_plan`, `resume_plan`), `loop.py` (per
turn), `cli.py` (`check-config`, `runs`, `run-stage`, `ask`, `run-sparring`),
`managed_finish.py` (`runs_report`).

**Boundaries:** No VS Code changes. Existing JSON fields are extended, not
renamed.

**Acceptance evidence:**
- **Ledger entry per launch.** With fake provider executables, a three-turn
  run with one resume and a reviewer turn gives exactly one ledger entry per
  provider invocation. The test asserts the entry count against the fake
  executables' own invocation log.
- **Monotonic.** A credential is present at turn 1 and removed before resume.
  The run guarantee stays `detected_not_prevented`.
- **Indeterminate.** At turn 2 → the run guarantee becomes `unverified`, and
  the surfaces show the unverified wording.
- **Probe failure.** An unlaunchable probe at turn 3 refuses that turn, and no
  provider process starts.
- **Crediting.**
  - In a test-constructed `isolated` mode, a `present` SSH-key class with
    `verified` rows for `credential_files_unreadable` on **every** path
    yields `prevented`. It is computed by the pure function; nothing at
    runtime can select `isolated` before Plan D.
  - The same with only the `shell` row `verified` yields
    `detected_not_prevented`.
  - `same_user` with every class `absent` and every row `verified` yields
    `detected_not_prevented`.
  - The same with the row removed yields `detected_not_prevented`.
  - Nothing else in the environment can make it `prevented`.
- **Wording.** A test greps the output of every surface for "protected" and
  "prevented" in non-prevented states.
- **Test runs.** The full suite passes.

## Stage 3 — Credential hygiene across all outputs, and documentation

**Outcome:**
- **One scanner.** `scan_for_credentials` (ported from `1700b4d`) runs over
  every engine-written artifact of a run: logs, ledgers, stage outputs,
  reports and provider transcripts the engine stores. It covers GitHub and
  GitLab token prefixes, private-key headers, AWS keys and the test canary. It
  reports counts only.
- **On a hit.** A real-credential pattern fails the run with
  `credential_in_output`, and the report says the credential needs revocation.
- **Documentation.** `docs/setup.md` gets a "Credential guarantees" section:
  - the four outcomes;
  - the crediting rule;
  - the Claude/macOS keychain limitation;
  - Codex `~/.ssh` status;
  - why "prevented" requires the `isolated` mode: a demonstrated boundary
    separating agents from credentials, write authority and engine state
    (D1, D4). Same-user runs are a detection-only compatibility mode.

**Acceptance evidence:**
- **Planted canary.** A canary planted in a fake provider transcript fails the
  run with counts only. No matched text appears in any output, which the test
  checks with a second scan.
- **Docs.** A docs test asserts that the setup section exists and that it names
  each outcome.
- **Test runs.** The full suite passes.
