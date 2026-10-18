# Issue #5 recovery kit

These scripts record the Stage 1 baseline and run Plan R in isolation. They run
as the credential-free macOS account `sparring-agent`.

Installed copies live in `/Users/Shared/sparring-recovery/`. The owner's account
owns that directory and `sparring-agent` cannot write it. The copies are
byte-identical to this directory: compare them with `SHA256SUMS` there.

Files written outside the repository when the kit is installed:
- **`recovery.bundle`.** The `plan/issue5-recovery` branch: `d1d48d0` plus
  documentation-only commits. It does not contain the Stage 2 candidate
  `1700b4d`, which stays reference material on the owner's side only.
- **`PLAN_SHA`.** The bundle's branch tip.
- **`RUNNER_SHA256`.** A fingerprint of the frozen runner's `.py` sources. The
  runner is `/Users/Shared/sparring-bootstrap/runner`, installed from
  `c61eaf8`, whose `src/` equals `ea8e70d`.

## Steps

| Step | Command (as `sparring-agent`) | Effect |
|---|---|---|
| 0 | `sh /Users/Shared/sparring-recovery/gate.sh baseline` | Presence-only credential gate. Every line must say `ok`. |
| 1 | `sh /Users/Shared/sparring-recovery/baseline.sh` | Fresh clone at `d1d48d0`. Runs the focused tests and the full suite unmodified. Writes evidence to `/Users/Shared/sparring-outbox/baseline-d1d48d0-*/`. |
| 3a | `sh /Users/Shared/sparring-recovery/setup-r.sh` | Creates `~/recovery/remote.git`, a local bare remote, and `~/recovery/agent-sparring`. |
| 3b | `sh /Users/Shared/sparring-recovery/preview-r.sh` | Runs the gate, then `check-plan`, then `start-plan` **without** `--confirm`. This is a dry run, saved to the outbox. |
| 3c | `sh /Users/Shared/sparring-recovery/run-r.sh <token>` | **Starts Plan R.** Only after a person has reviewed the 3b preview. |
| 3d | `sh /Users/Shared/sparring-recovery/export-r.sh` | After the run stops, bundles the local remote to the outbox for independent review. |

## What the gate checks, and how it differs from the bootstrap gate

**Credentials, presence only.** Nothing prints, requests or keeps a secret.
- Keychain internet and generic items (`gh:`, `git:https://`) for each Git
  host. `security` exit 44 means absent and exit 0 means present. Any other
  exit is **FAIL**, not absent.
- Credential helpers in user and system configuration. `osxkeychain` is
  covered by the keychain checks. Any other helper is **FAIL** and is never run.
  The bootstrap gate ran `git credential fill` and read a failing helper as
  absent, which is audit finding F5's flaw.
- Credential files and `gh` `hosts.yml`, by existence only.
- `ssh-add -l`: exit 1 or 2 means no identities, exit 0 means identities,
  anything else is FAIL.
- SSH private keys, by header and `IdentityFile`.
- Token environment variables, by name only.

**Isolation.**
- `sparring-agent` cannot list the owner's home.
- It cannot write the kit, the runner's `bin/` or the runner's package.
- The runner's source fingerprint matches.

**Tools.** `claude` 2.1.302 and `codex` 0.160.0, from the account's own
`~/.local/bin`.

**Run mode only.** The checkout is on the plan branch at `PLAN_SHA` and clean.
The local remote holds only that branch. `origin` is the only remote and points
exactly at the local remote. There is no repository transport or credential
configuration and no hooks. The Stage 1 baseline evidence exists.

## Known limits (accepted, disclosed)

- **The frozen runner predates Stage 1.**
  - It still treats a candidate reachable on the remote as pushed.
  - Its stage agents run without a provider boundary, under `--permission-mode
    acceptEdits`.
  - This is acceptable only because:
    - the only remote is a local disposable bare repository;
    - the account holds no Git credentials and cannot read the owner's files;
    - every result is untrusted until it has been independently reviewed.
- **Not prevention.** Under D1 and D4 this run is a same-account setup. It
  demonstrates nothing about prevention.
- **No step between the two stages.** `start-plan` has no
  `--stop-after-stage`, so the run continues into Stage R2 after Stage R1 is
  accepted. A human checkpoint between them would need `run-plan --run-key …
  --stop-after-stage …`, which has no preview or confirm token.
- **A lower send-back limit.** `--max-send-back-cycles 3` is lower than the
  default 5. After three send-backs the run stops for a human, which is the
  retrospective's lesson.
- **Gate before the first start only.** The run-mode gate requires the local
  remote to hold only the plan branch, so it applies before the first start.
  Resuming a paused run is a separate, reviewed step.
