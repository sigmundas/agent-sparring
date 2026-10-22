# Cross-account isolated execution (issue #5, Plan D)

Agents run under separate macOS accounts that hold no Git credentials and
cannot write anything the engine trusts. The engine stays in the human's own
account, with its credentials, state and VS Code. This is the only plan that
can make a run eligible for a `prevented` claim (decision D1). Plans R, A, B
and C stay correct without it, but every run they produce is `same_user`.

The audit is `docs/audits/issue5-recovery-2026-10-18.md`. The Plan R audit is
`docs/audits/issue5-plan-r-audit-2026-10-22.md`.

**Depends on:**
- Plan R, accepted, tested and merged to `main`.
- Plan A Stage 1 (one launch builder, pinned run-start policy). Plan D's
  launcher is one more thing the builder wraps, never a second launch path.
- Decisions D1 to D5, recorded in the audit.

**Integration branch.** Every stage lands on `issue5/boundary`, as in Plans A
to C. Nothing reaches `main` before the human live verification at the end of
this plan (D3). Until then `main` launches agents as it does today.

**Runner.** Run this plan with a frozen runner installed outside the agent's
worktree (D4). This plan changes the code that would otherwise confine its own
implementer.

## What this plan must achieve, and what it does not rely on

**The boundary.** In an `isolated` run, a stage agent or reviewer, including
any process it starts, cannot:
1. read the engine account's Git credentials (keychain items, credential
   helpers and their sockets, `gh` tokens, SSH keys, the engine's ssh-agent);
2. write engine-owned state: run state, pins, push authorizations,
   acknowledgements, acceptance and review records, the plan snapshot;
3. modify the trusted runner, its configuration, the launcher, or the policy
   it was started with;
4. push to GitHub or GitLab, other than through the engine's own authorized
   push.

**Not relied on as a boundary:**
- model instructions or prompt text;
- any record an agent can write, including handoffs, notes, transcripts and
  verification output;
- provider sandboxes (Plan A). They are defence in depth inside the agent
  account, and their claims stay per Plan C;
- any macOS behaviour this plan has not verified live on the installed
  versions. Each one is marked **unverified** below and checked in the final
  human verification.

**Out of scope:**
- confidentiality of the repository's own content against the agents;
- network egress in general. Agents can still reach the internet, but they
  have no credential to push with. Egress filtering is optional hardening H4;
- a hostile human at the engine account;
- Windows (see "Other platforms").

## Design

### Identities

| Account | Runs | Holds | Can write |
|---|---|---|---|
| Engine: the human's normal login | VS Code, the extension, the `sparring` engine, Git pushes | Git credentials (keychain, `gh`, SSH), the engine repository, engine state, the runner | its own files only |
| `sparring-stage` (the existing `sparring-agent` may be renamed or reused) | implementation turns, candidate verification commands | provider credentials for that role, its workspaces, provider sessions | its own home |
| `sparring-review` | reviewer (sparring) turns, intake reviews | provider credentials for that role, its review checkouts, provider sessions | its own home |
| `root` | nothing at run time | the launcher and the sudoers rule | — |

- **Two agent accounts are the minimum, not hardening.** Under one shared
  account, a stage agent could write the reviewer's user-level provider
  configuration (`~/.claude/CLAUDE.md`, settings, skills, `~/.codex/`) or its
  session files, and so steer the review that accepts its own candidate. That
  defeats engine-owned acceptance without touching an engine file.
- Both agent accounts are standard, non-admin users with no sudo rights,
  never members of `admin` or of any group that owns engine files. Membership
  of file-sharing groups (`com.apple.sharepoint.group.*`) is recorded and
  shown; the setup check fails if any group grants access to an engine path.
- **No second desktop session.** The engine reaches an agent account only
  through `sudo` (below). Agent accounts never need to be logged in, and they
  never need an unlocked login keychain.

### Switching identity: a root-owned launcher behind one sudo rule

**Chosen for the minimum: `sudo` to one fixed launcher.**
- A root-owned launcher, `/usr/local/libexec/agent-sparring/launch-agent`,
  with every directory on its path owned by `root` and not group- or
  world-writable. It runs under `/bin/sh` or `/usr/bin/python3 -I -S`, both
  system binaries.
- One root-owned rule in `/etc/sudoers.d/agent-sparring`, validated with
  `visudo -c`:
  ```
  Defaults!/usr/local/libexec/agent-sparring/launch-agent env_reset, !setenv
  <engine-user> ALL=(sparring-stage,sparring-review) NOPASSWD: /usr/local/libexec/agent-sparring/launch-agent
  ```
- The engine runs `sudo -n -H -u <agent> /usr/local/libexec/agent-sparring/launch-agent <subcommand> …`.
  `-n` means it never prompts. A sudo failure is the typed refusal
  `isolation_unavailable`, and the run never falls back to `same_user`.
- The launcher does not restrain the engine, which is trusted. Its job is to
  fix the identity, environment and working directory:
  - It starts from an empty environment, then sets `HOME`, `USER`, `LOGNAME`,
    `SHELL`, `PATH` (the agent's pinned tool directories plus system
    directories), `TMPDIR` (an agent-owned directory) and the locale.
  - It never passes on `SSH_AUTH_SOCK`, `GIT_*`, `GH_*`, `GITHUB_*`,
    `GITLAB_*`, any `*_TOKEN`, `*_ASKPASS` or `XDG_*` from the engine.
  - It loads the role's provider credentials from the agent's own
    `~/.config/agent-sparring/provider.env` (mode 600, owned by the agent),
    and nothing else.
  - It refuses any working directory outside the agent's
    `~/sparring/workspaces/`.
  - It sets `DISABLE_AUTOUPDATER=1` for Claude, and the equivalent for Codex
    if one exists, so the pinned provider binary cannot change itself mid-run.
- **Subcommands.** There are fixed subcommands only, and no "run any shell
  command" mode:
  - `seed`: reads a bundle on stdin and creates or updates a workspace at a
    named commit;
  - `provider-turn`: runs `claude` or `codex` with the argv the builder
    produced, at the path and sha256 the run policy pins;
  - `export`: writes a bundle of the workspace `HEAD` to stdout;
  - `observe`: writes bounded, NUL-separated `git status` output to stdout;
  - `verify`: runs one verification command from the reviewed plan, for
    Plan 2;
  - `probe`: the isolation probes (Stage 1);
  - `reap`: ends every process of that agent account.
- All data crosses the boundary through **stdin and stdout pipes**, never
  through a shared writable directory. So there is no file in a shared
  location that an agent could swap for a symlink to an engine file.

**Alternatives considered:**
- **A launchd daemon running as the agent, with a Unix-socket broker.** The
  identity binding is stronger: `getpeereid` names the caller and there is no
  sudo policy. But it is a long-running service with a protocol, which is more
  to build and review. Kept as optional hardening H3.
- **A second, logged-in GUI session (fast user switching).** Rejected. It is
  what the bootstrap kit needed to unlock the agent's keychain, it is fragile,
  and it is unnecessary once provider credentials are file-based.
- **`ssh agent@localhost`.** Rejected. It puts an SSH key in the engine
  account, enables Remote Login, and adds nothing over sudo.
- **A container or VM.** Rejected for the minimum. Docker Desktop on macOS
  shares host paths and the credential helper unless configured carefully,
  and it is heavy for a normal VS Code workflow. It remains the natural route
  on Linux CI.

### Provider authentication and session persistence

- **Provider credentials are the agent's own, and are not privileged.** Each
  agent account holds a provider credential that it is allowed to read. That
  is not one of the four protected properties. Use credentials dedicated to
  the agent accounts, with a spending limit, so a leak has a bounded cost.
- **Claude.** A long-lived token from `claude setup-token` (run once as the
  agent through `sudo -iu`), supplied as `CLAUDE_CODE_OAUTH_TOKEN`, or an
  `ANTHROPIC_API_KEY`. Both come from `provider.env`. **Unverified:** that
  Claude Code 2.1.x authenticates this way under `sudo -H` with a locked or
  absent login keychain, and never reads another account's keychain.
- **Codex.** `cli_auth_credentials_store = "file"` in the agent's
  `~/.codex/config.toml`, so credentials live in `~/.codex/auth.json`.
  **Unverified** on 0.160.x.
- **Session persistence.**
  - Provider sessions live in the agent's home (`~/.claude/projects/<cwd>`,
    `~/.codex/sessions`), so resuming requires the same account and the same
    working directory.
  - Each (run, role) gets one stable workspace path, and the engine records
    only the session id.
  - A session file is agent-writable data. It is never evidence and never
    read by the engine.
  - A role switch, or a provider switch, starts a fresh session, as today.
- The reviewer's workspace and sessions are in `sparring-review`'s home, which
  `sparring-stage` cannot read or write.

### Where everything lives

| Item | Owner | Location | Agent access |
|---|---|---|---|
| Engine repository, its `.git` and `<common>/agent-sparring/` records | engine | as today | none (the engine's home is mode 700) |
| Engine-owned managed worktree and `.sparring/` run state | engine | as today: `<repo>-sparring-<key>` | none. In `isolated` mode no provider is ever given this path |
| Runner (frozen or installed) | engine, or root | outside every agent root | none |
| Launcher and sudoers rule | root | `/usr/local/libexec/agent-sparring/`, `/etc/sudoers.d/` | read and execute only |
| Stage workspace | `sparring-stage` | `~sparring-stage/sparring/workspaces/<engine-uid>/<repo-id>/<run-key>/stage` | read and write |
| Review checkout | `sparring-review` | `~sparring-review/sparring/workspaces/<engine-uid>/<repo-id>/<run-key>/review` | read and write |
| Provider binaries | the agent (minimum) or root (H2) | the agent's `~/.local/bin` | minimum: writable, but sha256-pinned on every launch |

**An agent workspace is an independent clone, never a linked worktree.**
- A linked worktree shares the main repository's common directory. That
  includes `.git/config` (and so `includeIf`, `core.fsmonitor`,
  `core.hooksPath`, `credential.helper`, `url.*.insteadOf`), the refs of every
  branch, and `<common>/agent-sparring/`, where the engine keeps its run
  records. A process that can make commits in a linked worktree can therefore
  write all of them.
- So in `isolated` mode the agent edits a separate repository it owns. Only
  objects cross to the engine, as bundles.

### Moving candidates across the boundary

1. **Seeding.**
   - The engine creates `git bundle create - <base>` from its own repository
     and pipes it to `launch-agent seed`.
   - The agent clones it into its workspace. The workspace has no `origin`
     and no other remote.
2. **After every agent turn.** This covers a `READY`, a `SEND_BACK`, a
   failed, raised or interrupted turn, and every review-only turn. The order
   is fixed:
   1. `reap`.
   2. `export`, which writes the bundle to a file in the engine's own
      private `$TMPDIR`.
   3. `git bundle verify`.
   4. `git fetch` into the engine repository, with `transfer.fsckObjects=true`
      and the hermetic configuration below, into
      `refs/sparring/incoming/<run>/<turn>`.
   5. The bundle must hold exactly one head. Anything else is the typed
      refusal `export_invalid`.
3. **Engine-side checks are unchanged.** Plan R's pin and movement checks run
   on the imported head as they do today. In `isolated` mode an agent has no
   push credential, so a movement on the real remote is still checked. It is
   treated as a boundary failure (`isolation_breached`), not as an agent's
   push.
4. **Review checkouts.** The engine pipes a bundle at the exact candidate SHA
   to `sparring-review`'s `seed`. The reviewer never sees the stage
   workspace.
5. **Uncommitted work.** It is never imported. A candidate is a commit, as
   today. `observe` shows dirty files to the human as untrusted, bounded data.

### Engine-side handling of agent-authored content

The four properties also fail if the **engine account** executes something an
agent wrote. So the following are part of the minimum:
- **No candidate code runs as the engine.** Tests, builds and verification
  commands for a candidate run only through `launch-agent verify` as
  `sparring-stage`. This is how `test-r.sh` in the recovery kit already
  works.
- **Hermetic local Git.**
  - Every engine Git operation on candidate-derived content (fetch from a
    bundle, checkout into the engine worktree, `diff`, `log`, `status`) runs
    with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<an engine-owned minimal
    file>`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`,
    `submodule.recurse=false`, `GIT_LFS_SKIP_SMUDGE=1` and no `filter.*`,
    `diff.*.textconv` or `merge.*.driver` entries.
  - An agent-written `.gitattributes`, `.gitmodules` or `.lfsconfig` then
    names no command that runs as the engine.
  - Only the authorized push step loads the human's real configuration and
    credentials. It pushes from refs in the engine's bare objects, without a
    work tree.
- **No credential in argv.** Every account can read every process's command
  line (`ps`). The engine never puts a token in a URL or an argument.
- **`safe.directory`.** Git refuses repositories owned by another account
  unless `safe.directory` lists them. The setup check fails if the engine's
  global or system configuration sets `safe.directory = *` or lists any agent
  path. The engine never adds one.
- **VS Code and status polling.**
  - The extension's status, overview and polling read only engine-owned data
    and the engine repository. They never run `git` in an agent workspace,
    which an agent's config would turn into code running as the human.
  - The extension stops opening candidate checkouts as trusted workspaces
    (`runsView.ts` `vscode.openFolder`). A candidate's `.vscode/` tasks,
    interpreter paths, terminal profiles and `git.path` would otherwise run
    as the human.
  - Opening a candidate means showing diffs from engine Git objects, or opening
    it explicitly in Restricted Mode with a warning.
  - These changes are in `agent-sparring-vscode`. This plan defines the
    engine side and the required behaviour. The extension change is a
    companion plan in that repository, and it is listed as a blocker for the
    final verification.

### Claims

- A run is `isolated` only when the Stage 1 probes pass at run start, with
  positive controls. Otherwise the run is `same_user`, or it refuses if
  `isolated` was requested. The run never silently degrades.
- `isolated` makes a path eligible for `prevented` under Plan B, but only
  after Plan C's live run records each property for that path (R-1).
  Probe results are observations. They never authorize a launch, a push or an
  acceptance (R-2).

### Minimum secure implementation versus optional hardening

**Minimum. All of these are required for any `prevented` claim.**
- M1. Two dedicated non-admin agent accounts, with no Git credentials. The
  presence-only gate (as in the recovery kit) passes for both at every run
  start.
- M2. The root-owned launcher and one NOPASSWD sudo rule for it, with an
  empty starting environment and no engine credential variables.
- M3. Independent agent clones. Transfer is by bundle over pipes, with fsck
  and exactly one head. The engine never runs any tool inside an agent
  workspace.
- M4. Engine state, the runner, its configuration and the launcher are not
  writable by either agent account, proven at run start by probes run as each
  agent.
- M5. Every provider launch in an `isolated` run goes through the launcher.
  This covers stage, reviewer, intake, `ask` and resume. A same-account
  launch spec in an `isolated` run is the typed refusal
  `isolation_launch_mismatch`.
- M6. Hermetic engine Git on candidate content. No credential in argv. The
  `safe.directory` check.
- M7. `reap` before every export, and one active turn per agent account
  (a lock the engine holds).
- M8. VS Code status reads engine-owned data only, and candidates are never
  auto-opened as trusted workspaces (companion plan).
- M9. Provider binaries are sha256-pinned per launch, and auto-update is off.

**Optional hardening:**
- H1. Agent accounts as hidden role accounts with no password, so they cannot
  log in at all.
- H2. Provider binaries installed root-owned, outside the agent's home.
- H3. A launchd broker with `getpeereid`, instead of sudo.
- H4. Egress rules for the agent accounts, for example a `pf` `user` match.
  They deny Git push endpoints even if a credential reached an agent through
  repository content. **Unverified** on macOS 26.
- H5. Plan A's provider sandboxes, applied inside the agent accounts as well.
- H6. Per-run agent accounts, or a fresh `HOME` per run, so nothing carries
  over between runs.

### Other platforms

- **Linux.** The same design works with the same mechanisms: separate UIDs,
  a root-owned launcher, one sudo rule, POSIX permissions, file-based
  provider credentials, and no keychain. It is supported by the code paths in
  this plan, but every claim stays `unverified` until a Plan C live run on
  Linux records it.
- **WSL.** It counts as Linux only with Windows interop disabled
  (`[interop] enabled=false`). Otherwise a Linux process can run Windows
  executables, including Git Credential Manager, as the Windows user.
  Without that, it is unsupported.
- **Windows.** Not supported. A separate account needs a stored password or
  an elevated service, and the ACL model differs. Runs are `same_user`,
  capped at `detected_not_prevented`.

## Security invariants (every later refactor must keep these)

- **I-D1. No engine-identity agent process.** In an `isolated` run, no
  provider, verification command or agent-started process runs with the
  engine's UID.
- **I-D2. Nothing agent-writable is trusted.** The engine never reads
  authorization, acceptance, pin, policy or run state from a path an agent
  account can write. It never executes a file such a path contains.
- **I-D3. Objects only.** Agent output reaches the engine only as bundle
  objects, provider stdout and bounded `observe` output, all over pipes. It
  is never a file the engine opens in a shared or agent-owned directory.
- **I-D4. No engine tool in an agent workspace.** The engine, the extension
  and their Git calls never use an agent workspace as their working directory
  or Git directory.
- **I-D5. Credentials stay put.** No engine credential variable, socket or
  file reaches the launcher's environment or argv. Only the authorized push
  step loads the human's Git configuration.
- **I-D6. Fail closed, never degrade.** Missing sudo, failed probes, a
  changed launcher or a pinned binary mismatch refuse with a typed code. A
  requested `isolated` run never continues as `same_user`.
- **I-D7. Evidence never authorizes (R-2).** Probe results decide only what
  may be claimed.

## Stage 1 — Isolation configuration, launcher and run-start probes

**Context:**
- Every run today is `same_user`. Plan A Stage 1 records the mode, but
  nothing can make it `isolated`.
- The recovery kit's `gate.sh` is the reviewed presence-only credential gate.
  Reuse its rules, not its code.

**Outcome:**
- **An isolation configuration.** It is engine-owned and never read from the
  repository or the worktree. It lives in the engine's user configuration
  directory:
  - the agent accounts per role;
  - the launcher path and its expected sha256;
  - the per-role workspace root.
  `sparring isolation show` prints it.
- **The launcher source** lives in the repository at
  `packaging/macos/launch-agent`, with an install script and the sudoers
  fragment. The repository copy is reference only. The engine trusts only
  the installed copy, by owner, mode, path chain and sha256.
- **Run-start probes** (`launch-agent probe`, run as each agent account). The
  run records each probe's result; any failure refuses an `isolated` run with
  `isolation_unavailable` and names the probe.
  - **Credentials.** The presence-only gate: keychain items for each
    configured Git host, credential helpers, credential files, `gh`
    `hosts.yml`, SSH keys and identities, and token variables by name. An
    indeterminate result fails.
  - **Engine sockets.** `ssh-add -l` with the engine's own `SSH_AUTH_SOCK`
    value passed explicitly fails with a permission error.
  - **Write denial.** Each must be denied:
    - every engine state directory for the run;
    - the engine repository's common directory and `.git/config`;
    - the engine-owned managed worktree;
    - the runner's package directory;
    - the isolation configuration;
    - the launcher.
  - **Read denial.** The engine's home directory and keychain directory
    cannot be listed.
  - **Positive controls.** The agent can write its own workspace root, and
    the probe can detect a deliberately agent-writable control directory as
    writable. Without these, a denial proves nothing.
  - **Cross-role.** `sparring-stage` cannot read or write
    `sparring-review`'s home, or the other way round.
  - **Groups.** Neither account is in `admin`, and no group of either
    account owns an engine path.
  - **Launcher integrity.** It is owned by root, with a root-only path chain
    and the expected sha256. The sudoers rule names exactly this path.
- **`sparring isolation check`** runs the same probes on demand and prints
  `ok` or `FAIL` per line, like the recovery kit gate. It is observation
  only.

**Starting points:**
- `cli.py` (new `isolation` command group); `user_config.py`; Plan A's
  `launch_spec.py` and the run-start policy in `plan.py`.
- `docs/audits/issue5-recovery-kit/gate.sh` (the reviewed rules).

**Boundaries:**
- No provider launch changes (Stage 3). No transfer changes (Stage 2).
- No account creation, sudoers installation or keychain change by the
  engine. Those are human admin steps, documented in `docs/setup.md`.

**Acceptance evidence:**
- **Probe logic.** Unit tests feed recorded outputs and exit codes to the
  probe classifier. Absent passes; present fails; every other code fails as
  indeterminate.
- **Real permissions.** Same-account tests on a temporary tree prove the
  write-denial and positive-control logic: a `chmod 555` directory is
  reported as denied, and a writable one as writable.
- **Refusals.** A missing sudo rule (a fake `sudo` exiting 1), a launcher
  with a wrong sha256 and a launcher in a user-writable directory each refuse
  with `isolation_unavailable` before any provider starts.
- **Never degrade.** A requested `isolated` run never records `same_user`.
- **Evidence never authorizes.** A probe record placed in any state directory
  changes neither the mode nor any claim. Only the live probe result does.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass.

## Stage 2 — Independent agent clones and bundle transfer

**Context:**
- Managed runs create `<repo>-sparring-<key>` with `git worktree add`, which
  shares the engine repository's common directory and `.sparring/` state with
  the agent (`managed_run.py` `plan_managed_run`).

**Outcome:**
- **Isolated layout.** In an `isolated` run, the engine-owned managed
  worktree and its `.sparring/` state are created exactly as today. The
  provider's working directory becomes the agent workspace, which is seeded
  by bundle over `launch-agent seed`.
- **Import.** After every turn, in the fixed order from the Design section
  (`reap`, `export`, `verify`, fsck'd `fetch`), the head lands in
  `refs/sparring/incoming/<run>/<turn>`. The engine-owned managed branch and
  worktree advance only through the existing acceptance path.
  - The imported head is the candidate the existing Plan R checks see.
  - `export_invalid` covers bundles with no head, several heads, a head that
    does not descend from the seeded base, or fsck errors.
- **Review checkouts.** The reviewer's checkout is seeded at the exact
  candidate SHA in `sparring-review`'s workspace.
- **Hermetic engine Git** (M6) for every import, checkout, diff and status on
  candidate-derived content.
- **Movement on the real remote in an isolated run** is still checked. It
  stops the run with `isolation_breached`.

**Starting points:**
- `managed_run.py` (`plan_managed_run`, worktree creation), `loop.py`
  (turn boundaries), `plan.py` (turn bookkeeping), `engine_git.py`,
  `remote_pin.py`, `handoff.py`.

**Boundaries:**
- `same_user` runs keep today's linked worktree, unchanged.
- No provider launch changes (Stage 3).

**Acceptance evidence:**
- **Real Git, no mocks for transfer.** Bundles are built from real
  repositories:
  - a single head is imported;
  - two heads, a non-descendant head and a corrupt pack each refuse with
    `export_invalid`;
  - the head is visible under `refs/sparring/incoming/…`;
  - no other engine ref changed.
- **Planted content.** The candidate tree contains:
  - `.gitattributes` with `filter=lfs` and a custom filter;
  - `.gitmodules` pointing at a local path;
  - `.lfsconfig`;
  - a `.git/config` in the agent workspace with `core.fsmonitor` and
    `core.hooksPath` set to a marker script.

  Import, checkout into the engine worktree, `diff` and `status` run none of
  them. A marker file proves it.
- **Every turn kind** (`READY`, `SEND_BACK`, raised, interrupted,
  review-only) performs `reap` then `export`, and imports, in that order. This
  is proved with a fake launcher that records the calls.
- **Isolated remote movement.** A real bare "remote" moved during an isolated
  turn stops the run with `isolation_breached`.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass.

## Stage 3 — Every provider launch through the launcher

**Context:**
- After Plan A Stage 1, all launches come from one builder producing a
  `LaunchSpec`.

**Outcome:**
- **Wrapping.** In an `isolated` run, the builder wraps every `LaunchSpec` as
  `sudo -n -H -u <role account> <launcher> provider-turn …`. This covers
  stage, reviewer, intake, `ask` and every resume. The engine-side
  environment it passes to sudo is empty.
  - A same-account spec in an `isolated` run is the typed refusal
    `isolation_launch_mismatch`.
  - `--fresh-stage-agent` and `--fresh-sparrer` keep the same account and
    start a new session.
- **Pinned binaries.** The launcher checks the provider binary's sha256
  against the run policy before every exec (M9), and sets the provider's
  auto-update opt-outs.
- **Session ids** are recorded as today. Each (run, role) has a stable
  workspace path, so resume finds the session.
- **Timeouts and cancellation** signal the sudo process, then run `reap` for
  that account. A cancelled turn is still exported and checked (Stage 2).
- **One active turn per agent account,** under an engine-held lock.

**Starting points:**
- Plan A's `launch_spec.py`, `providers/subprocess_runner.py`,
  `providers/claude_cli.py`, `providers/codex_cli.py`, `cli.py`
  (`ask`, intake, run commands), `concurrency.py`.

**Boundaries:**
- No change to provider boundary content (Plan A Stages 2 to 3).
- No VS Code changes beyond the existing refusal text.

**Acceptance evidence:**
- **Golden specs.** For each (provider, role, kind) in `isolated` mode, the
  exact sudo argv and the empty environment.
- **No bypass.** A test asserts that no `subprocess`/`run_streaming` call in
  `providers/` starts a provider without the wrapper in `isolated` mode.
- **Environment.** With `SSH_AUTH_SOCK`, `GH_TOKEN`, `GIT_ASKPASS`,
  `GIT_CONFIG_PARAMETERS` and `XDG_CONFIG_HOME` set in the engine, the
  launcher's recorded environment, from a fake launcher, contains none of
  them.
- **Pinning.** A provider binary replaced after start refuses with
  `provider_changed_since_start`.
- **Lifecycle.** Timeout and cancellation run `reap` and then the Stage 2
  export.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass.

## Stage 4 — Engine credential hygiene and status surfaces

**Outcome:**
- **No credential in argv.** No engine Git call passes a credential in argv
  or in a URL. A test scans the argv builders, and a runtime assertion
  rejects URLs with userinfo.
- **The authorized push** is the only operation that loads the human's
  global and system Git configuration. It runs with no work tree, from refs.
- **The `safe.directory` check** is part of `sparring isolation check` and of
  run start.
- **`sparring status` and the JSON the extension polls** read engine-owned
  state and the engine repository only. They report the agent workspace's
  dirty state only from the last `observe` output, labelled untrusted.
- **Candidate verification commands** (the hook Plan 2 will use) run only
  through `launch-agent verify` in `isolated` runs.
- **Documentation.** `docs/setup.md` gains the isolated setup: creating the
  accounts, installing the launcher and the sudoers rule, `claude
  setup-token` and the Codex file credential store as each agent, and
  `sparring isolation check`. `docs/reference.md` gains the refusal codes.

**Starting points:**
- `engine_git.py`, `push_gate.py`, `managed_finish.py`, `cli.py` (status),
  `docs/setup.md`, `docs/reference.md`.

**Boundaries:**
- VS Code extension changes are in the companion plan in
  `agent-sparring-vscode`.

**Acceptance evidence:**
- **Status safety.** `sparring status` in an isolated run never spawns `git`
  with an agent path as its working directory or Git directory. A spawn
  recorder proves it.
- **Hermetic local operations.** A push-free path given a global config with
  `core.fsmonitor` and a credential helper set to marker scripts runs neither
  of them.
- **Push configuration.** The authorized push sees the configured helper
  (real Git, a local bare remote, a fake helper that records it was asked).
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass.

## After Plan D is accepted (human)

These are human steps. Each needs admin or real credentials, so no stage can
perform or certify them.
1. **Companion extension plan.** Plan and run it in `agent-sparring-vscode`
   (M8): status polling uses engine data only, and candidates are not
   auto-opened as trusted workspaces.
2. **Install.** Create or adapt `sparring-stage` and `sparring-review`,
   install the launcher and the sudoers rule, and set up provider credentials
   as each agent.
3. **Live verification (Plan C harness, D5).** Use synthetic Git
   credentials only. Record for each execution path:
   - sudo works without a password or a TTY from a VS Code-started engine;
   - Claude and Codex authenticate and resume under `sudo -H` with no
     unlocked agent keychain;
   - agent processes cannot read the engine's keychain items, ssh-agent or
     `gh` token, cannot write any engine path, and cannot push;
   - a stage agent cannot change the reviewer's configuration or sessions;
   - every negative result has its positive control.
4. **Then decide claims.** Only then may Plan B record `prevented` for the
   paths that passed. Merge `issue5/boundary` to `main` per D3.
