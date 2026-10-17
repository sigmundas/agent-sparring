# Secure the agent/engine Git write boundary

This plan closes GitHub issue #5 (P0). In Sporely Stage 4b a stage agent ran
`git push` on its first turn and advanced a remote branch to an unreviewed
commit. Three things allowed it:
- its shell had the user's Git credentials;
- the engine's finalization prompt told it to push;
- the engine treats a candidate already reachable on the remote as needing no
  authorization.

**Scope (decided 2026-10-17):**
- **Threat model.** Agents are not assumed malicious, but they must be unable
  to get around recorded permissions for three things:
  - remote Git writes;
  - production operations, insofar as they need credentials the engine
    controls;
  - engine-owned approvals.
- **Use the providers' own sandboxes.** Use the sandboxes the providers
  already have. Verify them on the versions and invocation modes the engine
  uses. Fail closed when a promised control is unavailable.
- **Network.** Agent shells get default-deny outbound access, with narrow
  exceptions the project declares. This is separate from the provider CLI's
  own model connection. Broader production-network isolation stays in
  `docs/plans/verification-network-isolation.md`.

**Capability evidence gathered during planning.** Gathered 2026-10-17 on
macOS 26 with `claude` 2.1.302 and `codex-cli` 0.160.0. A probe script was
run three ways: unsandboxed, under `codex sandbox -P :read-only`, and under
`claude -p` with engine-style `--settings` (sandbox enabled,
`allowUnsandboxedCommands: false`, `strictAllowlist`, `denyRead` for
`~/.ssh` and `~/.config/gh`, `credentials.envVars` deny for
`SSH_AUTH_SOCK`).

| Probe from an agent shell | Unsandboxed | Codex read-only | Claude, user settings loaded | Claude, user settings excluded |
| --- | --- | --- | --- | --- |
| write outside worktree (not tmp) | yes | no | no | no |
| read `~/.ssh/id_ed25519` | yes | **yes** | no | no |
| login keychain (`git credential-osxkeychain get`) | yes | no | **yes** | **yes** |
| `gh auth token` | yes | no | no | no |
| HTTPS to github.com | yes | no | **yes** | no |
| HTTPS to an allowlisted host | yes | no | yes | yes |
| `SSH_AUTH_SOCK` visible | yes | **yes** | no | no |

Findings:
- **User settings widen the network allowlist.** The user setting
  `WebFetch(domain:github.com)` widens Claude's sandbox allowlist even when
  `--settings` sets `strictAllowlist`. Excluding user settings closes this.
- **Claude's sandbox does not block the login keychain.** An outer Seatbelt
  profile that denies the Security-server services does block it. But macOS
  refuses to apply a Seatbelt sandbox inside another one (`sandbox_apply:
  Operation not permitted`), so that outer profile cannot be combined with
  Claude's sandbox, or with Codex's.
- **Conclusion for Claude agents under the same OS user.** Keychain-held
  credentials stay readable by the agent's shell. Network restrictions do not
  count as protecting a credential the agent can retrieve: the agent could
  still disclose or misuse it. Where a write-capable Git credential is
  retrievable, unauthorized remote writes are detected (Stage 1), not
  prevented. Every engine surface must say exactly that (decided
  2026-10-17).
- **Codex loads user MCP servers outside its sandbox.** It currently loads
  them from `~/.codex/config.toml`. On this host they include `computer-use`
  and `cua_repl`, which can drive the desktop.

**Two stages:**
1. Make every action the engine takes with authority correct and
   independently checked.
2. Confine the agents the engine launches, using provider facilities, and
   prove it with scripted and real-provider tests.

## Stage 1 — Engine-owned authorization, pinned pushes and hardened engine Git

**Context (Sporely Stage 4b, issue #5):**
- **What happened.** A stage agent pushed
  `origin/feature/ingestion-location-provenance-stage-4` to an unreviewed
  commit while `push_authorization` was null.
- **Push gate.** `push_gate.ensure_candidate_pushed` returns success as soon
  as `git_context.verify_pushed` finds the candidate reachable. It does this
  before it looks at the authorization.
- **Acceptance.** `acceptance.py` (`freeze_candidate` and both
  `_accept_*_locked` paths) checks reachability only.
- **Prompt.** `stage_prompt._finalization_section` tells the agent to
  "Commit … and push".
- **Target remote.** `git_context.tracking_remote` re-reads the target remote
  and ref from `branch.<b>.remote` and `branch.<b>.merge`.
- **Engine Git calls.** The engine runs `git` with the user's credentials,
  outside any agent sandbox. That covers push, fetch, `ls-remote`, remote
  branch deletion and `managed_finish` merges in the main checkout. It also
  covers local `status` and `diff`, which can run `core.fsmonitor`, textconv
  and external diff drivers.
- **Engine-owned state.** It lives in `.sparring/`: run state
  `plans/<run>.json`, stage artifacts, approvals, gate answers and recorded
  review outcomes. Managed-run records live under
  `<git-common-dir>/agent-sparring/`. `artifact_ownership.py` declares the
  engine to be the only writer of every artifact.

Agents may run arbitrary commands as the user. Confining them is the next
stage. This stage makes the engine's own authority correct.

**Outcome:**
- Push authorizations, pinned destinations, push receipts and remote-movement
  acknowledgements are engine-owned records in the engine's own state. The
  engine reads them from there only. Prose, `notes.md`, handoffs, stage output
  and reviewer output are never authority.
- The engine pushes only the authorized SHA to the pinned
  repository/remote/ref.
- Remote movement that the records do not explain becomes a typed, blocking
  deviation.
- No engine Git invocation can be made to run, or be redirected by,
  repository hooks or configuration that changed after the run started.

**Required invariants:**
- **Pinned target.** At run start the engine pins the remote name, URL and
  remote ref, and records the ref's observed tip (or its absence).
  - Push, reachability and deviation checks use only the pinned values.
  - A later change to the URL or tracking configuration is a typed refusal.
    The engine never follows it.
  - A legacy run with no pin is pinned on first resume from the current
    config. The engine shows the person the pinned values and says this is
    weaker than a run-start pin.
- **Receipts and deviations.**
  - Each engine push records a receipt: run, stage, remote, ref, previous tip
    and pushed SHA.
  - The engine compares the pinned ref's tip with what its records explain
    at three points: after every agent turn, before every engine push, and
    before freeze or accept.
  - A tip the records do not explain gives a typed
    `unauthorized_remote_movement` pause carrying the remote, ref, expected
    tip and observed tip. This includes the Stage 4b case: a candidate
    already reachable before any authorized push. The pause blocks push,
    freeze and accept.
  - One explicit human CLI action, naming the run and the observed SHA,
    clears the pause and is recorded with who and when. It never counts as
    review, acceptance or push authorization.
  - The engine never resets, force-pushes or deletes remote history.
- **Push path.**
  - `ensure_candidate_pushed` no longer treats reachability as success.
  - An authorized push uses only the existing single `push_command` shape:
    no force, tags, deletion or widening.
  - Push permission remains distinct from acceptance and release.
- **Hardened engine Git.**
  - Every engine Git invocation goes through one engine path. It never runs
    repository hooks, `core.fsmonitor`, textconv, or external diff, merge or
    filter drivers, and it never loads `include`d configuration.
  - Remote operations use only the pinned URL. Their credential and transport
    configuration comes from user-level or system configuration, or from the
    repository settings captured at run start (the trusted snapshot), so
    existing setups keep working.
  - A later change to repo-local `credential.helper`, `core.sshCommand`,
    `url.*.insteadOf`, `pushInsteadOf`, `remote.*.pushurl` or
    `protocol.*.allow` is a typed refusal that names the key.
  - Edits to `.gitattributes` alone are not a change, because a driver runs
    only if config defines it.
- **Prompts and instructions.**
  - Stage, finalization and reviewer prompts no longer tell agents to push.
    They state that the engine pushes after authorization.
  - Prose in a plan or brief grants nothing.
  - In this repository's `AGENTS.md`, the Git policy lines "Agents may create
    branches, commit, push…" and "commit and push completed stage work" are
    amended: inside a managed stage, the engine pushes. Interactive use
    outside managed runs is unchanged.
- **Behavior kept.**
  - These keep working: `--allow-push-candidate`, `--allow-push-for-run`
    (already part of the single `start-plan` confirmation), `finish-run
    --push-target`, `resume-plan --accept-advanced-head`, and every branch,
    clean-tree and sibling-pin check.
  - A stage pauses on the existing push-authorization pause unless the run
    holds a run-level grant.
- **Detection, not prevention.** Receipts and remote-movement checks are an
  audit trail and a blocking backstop. They never substitute for removing
  write capability, and no output may describe them as prevention.
- **Scope of detection.** This stage watches only the pinned ref. Stage 2 is
  what stops agents writing to engine state and to other refs, and the
  report says so.

**Starting points:** `push_gate.py`, `git_context.py` (`_run` and callers),
`managed_run.py` (`_git`), `acceptance.py`, `managed_finish.py`, `plan.py`
(`PlanRunState`, `_grant_push_authorization`, typed pauses),
`branch_guard.py`, `finalization.py`, `intake_branch.py`,
`artifact_ownership.py`, `stage_prompt.py`, `sparring_prompt.py`,
`review_prompt.py`, `cli.py`, `AGENTS.md`. Enumerate the Git call sites with
`rg -n '"git"' src/agent_sparring`. Tests: `tests/test_acceptance.py`,
`tests/test_accept_advanced_head.py`,
`tests/test_sparring_acceptance_concurrency.py`, and the push-gate and
managed-finish tests (`rg -l "push_gate|managed_finish" tests`).

**Implementation freedom:**
- The record schema and the spelling of the acknowledgement.
- How the hardened path neutralizes configuration, for example `-c`
  overrides, `--no-ext-diff`/`--no-textconv`, a sanitized environment, and a
  comparison against the snapshot.

**Boundaries / non-goals:**
- No changes to how agents are launched (Stage 2), and no general sandbox or
  network work.
- No VS Code changes. Note how the new pause renders as a follow-up.
- No Sporely changes, no hand edits of `.sparring` state, and no real
  remotes.

**Acceptance evidence:** use disposable local bare remotes only.
- **Stage 4b.** A candidate pushed outside the engine before review gives
  `unauthorized_remote_movement`.
  - Freeze and accept are refused.
  - A push authorization does not clear it.
  - The acknowledgement clears it, is recorded, and accepts nothing.
- **Push authorization.**
  - An engine push without authorization is refused.
  - With authorization, it advances only the pinned ref to the authorized
    SHA.
  - A wrong SHA, ref, remote or repository fails.
- **Retargeting.** Changing `branch.<b>.merge` or `remote.origin.url` after
  start gives a refusal. No push reaches the new target.
- **Forged authorizations.** An authorization or acknowledgement written into
  `notes.md`, stage files or agent output is ignored.
- **Planted configuration.** Each of the following is planted, with a marker
  file to detect execution:
  - a `pre-push` hook and `core.hooksPath`;
  - `credential.helper=!…`, `pushInsteadOf`, `remote.origin.pushurl` and
    `core.sshCommand`;
  - `core.fsmonitor`;
  - an `include`;
  - a diff driver, a merge driver and a textconv.

  Then these engine operations run: push, `verify_pushed`, freeze/accept,
  managed finish with `--push-target`, remote branch deletion, and engine
  `status` and diff. No marker appears, nothing is written except to the
  pinned remote, and each change is a typed refusal.
- **Snapshot.** A credential helper present in the run-start snapshot still
  works for an authorized push.
- **Prompts and legacy state.**
  - Prompt tests assert that no prompt instructs pushing.
  - A legacy fixture loads, is pinned with a visible notice, and turns an
    unexplained reachable candidate into a deviation.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass. The
  report gives the commands, counts and candidate SHA.

## Stage 2 — Confine engine-launched agents using provider sandboxes, verified

**Context:** The earlier accepted stage of this plan provides the following:
- engine-owned authority records in engine state (`.sparring/`, and
  `<git-common-dir>/agent-sparring/` for managed runs);
- pushes only to a pinned destination;
- unexplained remote movement as a blocking pause;
- engine Git calls that ignore configuration changed after the run started.

How agents are launched today:
- `providers/claude_cli.py` `_build_args` passes `--permission-mode
  acceptEdits` and nothing that controls the sandbox, setting sources or MCP.
- `providers/subprocess_runner.py` starts processes with the full
  environment.
- `providers/codex_cli.py` hard-codes `-c sandbox_mode="read-only"`.
- VS Code's Run Plan always runs managed. There, the agent's working directory
  is a sibling worktree, so `.sparring/` and the Git common directory are
  outside it. In unmanaged runs, `.sparring/` is inside the agent's working
  directory.

A probe on 2026-10-17 (macOS 26, `claude` 2.1.302, `codex-cli` 0.160.0)
found the following about Claude's native sandbox, configured through
`--settings`:
- It denies writes outside the worktree and tmp.
- It denies credential files through `filesystem.denyRead`.
- It hides `SSH_AUTH_SOCK` through `credentials.envVars`.
- It enforces a strict network allowlist only when user settings are
  excluded. A user `WebFetch(domain:github.com)` allow widened it despite
  `strictAllowlist`.
- It does not block the login keychain: `git credential-osxkeychain get`
  succeeded.
- An outer keychain-deny Seatbelt profile cannot be combined with it, because
  macOS refuses nested Seatbelt sandboxes.
- `ps eww` cannot read another process's environment.

The same probe found the following about Codex's read-only sandbox:
- It denies writes, all network, the keychain and `gh auth token`.
- It still allows reading `~/.ssh/id_ed25519` and passes `SSH_AUTH_SOCK`.
- It loads user MCP servers from `~/.codex/config.toml` (here `computer-use`
  and `cua_repl`), which run outside the sandbox.

The Claude documentation (code.claude.com/docs/en/sandboxing) says:
- The OS sandbox covers Bash and its children.
- Read, Edit, Write, WebFetch, hooks and MCP servers are governed by
  permission rules, not by the OS sandbox.
- `.git` hooks and config and `.claude` settings are always write-protected
  for sandboxed commands.
- With `allowUnsandboxedCommands: false` in `--settings`, repository settings
  cannot loosen the sandbox.
- `denyWrite` can exclude a subdirectory of a writable root.
- Unix-socket access is opt-in (`allowUnixSockets`).

**Outcome:** The engine launches a stage or reviewer agent only inside a
boundary it builds from the provider's own facilities and verifies at
launch.
- **What the agent can do:** read the repository, edit and commit in its
  worktree, run tests, and reach only the network exceptions the project
  declared.
- **What the agent cannot do:**
  - reach a Git host or any other undeclared host;
  - connect to local Unix sockets such as SSH or Docker agents;
  - read credential files or credential environment variables;
  - write to remotes;
  - write any engine-owned state or the settings that define its own
    boundary.
- **Fail closed.** If a required control cannot be established for a role,
  provider, version or platform, the engine refuses that launch with a typed
  reason.
- **Capability table.** One table states what is supported and what residual
  exposure remains.

**Hard constraints:**
- **Provider facilities only.**
  - The Claude stage agent runs under Claude's native sandbox, with
    engine-supplied settings: `failIfUnavailable: true`,
    `allowUnsandboxedCommands: false`, a strict network allowlist, empty
    Unix-socket access, credential `denyRead`/`envVars`, and `denyWrite` for
    engine state.
  - The Codex reviewer keeps its read-only sandbox.
  - No outer or custom sandbox.
- **File tools follow permission rules, not the OS sandbox.** The engine also
  supplies `permissions.deny` rules covering the same protected reads and
  writes for Read, Edit and Write. It keeps or replaces `acceptEdits` only
  where those deny rules demonstrably take precedence.
- **Settings sources.** No user, project or local settings source that could
  widen the boundary is loaded for agent sessions. That covers:
  - user permission allows (which widen the network allowlist);
  - hooks;
  - plugins;
  - MCP servers;
  - claude.ai connectors;
  - any settings file an agent can write after the run starts.

  WebFetch is disabled unless the project enables it. If it is enabled, it
  is listed as a residual risk. Subagents inherit the boundary.
- **Engine state is not writable by agents.** This holds in managed and
  unmanaged runs: `.sparring/`, `<git-common-dir>/agent-sparring/` and the
  engine-generated settings, through both the shell and the file tools.
  Running the `sparring` CLI from an agent shell therefore cannot record an
  authorization or approval. The agent can still write the Git storage that
  local commits need: objects, refs, logs, and the worktree's own
  `index`/`HEAD`.
- **Codex.**
  - User MCP servers are not loaded.
  - Credential environment variables are not passed.
  - Codex's read-only sandbox can read files such as `~/.ssh` private keys,
    and no network or write restriction substitutes for that. If the engine
    can supply a read restriction that Codex honours, it uses it. Otherwise
    the same presence-only launch probe applies to the reviewer, and a
    retrievable write-capable credential sets that run's guarantee to
    "detected, not prevented".
- **Network.**
  - Agent shells default to denying all outbound access. Exceptions are
    declared in `.sparring/project.toml`, for example package registries.
    Loopback exceptions name specific ports for local test services. Find
    the provider setting that actually permits outbound loopback
    connections; `allowLocalBinding` may only cover listening.
  - The engine reads the exceptions from the run-start committed project,
    not the agent's worktree.
  - It refuses any entry that matches a configured Git remote by host and
    port, or a Git-hosting web, API or SSH host.
  - It also refuses proxy- or tunnel-like entries (HTTP/SOCKS proxies,
    generic tunnel hosts), because they would add routes to a Git host.
    This only narrows network routes; it is not credential protection. If a
    project needs such an entry, that entry is unsupported. Project-enabled
    WebFetch is listed as a residual risk.
  - A network failure caused by the allowlist names the `project.toml` key.
  - Agents do not fetch from Git hosts; the engine refreshes the refs a turn
    needs.
- **Prevention versus detection.** Claude's sandbox cannot hide
  login-keychain items, and macOS forbids adding an outer sandbox. Network
  restrictions are not credential protection.
  - At launch, a presence-only probe runs inside the exact boundary the
    engine built. It checks whether an agent shell can retrieve a credential
    for the pinned remote's host through the keychain helper, `gh`,
    credential files or the environment.
  - If a credential is retrievable, the run's recorded guarantee is
    "unauthorized remote writes: detected, not prevented". Otherwise it is
    "prevented".
  - The recorded guarantee is stored in the run state. It is shown in the
    `start-plan` confirmation line, in `check-config` and in `sparring runs
    --json`. It is not a separate prompt.
  - Nothing describes a "detected" run as protected. The capability table
    marks the single control "write-capable Git credentials unreachable from agent shells"
    per provider and role, measured by the launch probe. On macOS it is
    unsupported for Claude while such a credential is in the login keychain.
    For the Codex reviewer it is unsupported while one is in a readable file,
    unless an engine-supplied read restriction is proven.
  - This stage is not weakened to fit: its acceptance never counts network
    denial as credential isolation.
- **Re-established every turn.** The boundary is rebuilt for every turn,
  resumed ones included, and is never weaker than at run start.
- **Capability table.**
  - One engine-owned table covers provider × role × version × platform, per
    control: supported, unsupported or residual.
  - It is documented in `docs/setup.md` and reported by `sparring
    check-config` for the configured roles.
  - A launch is refused if any required control is unsupported.
  - "write-capable Git credentials unreachable from agent shells" is the one control reported
    rather than required. A run without it is launched but is recorded,
    shown and documented as "detected, not prevented", never as protected.
  - Anything not proven by this stage's evidence counts as unsupported.

**Starting points:** `providers/claude_cli.py`, `providers/codex_cli.py`,
`providers/subprocess_runner.py`, `providers/__init__.py`, `stage_agent.py`,
`sparring_agent.py`, `cli.py` (adapter construction, `check-config`),
`agent_config.py`, `config.py`, `managed_run.py` (`committed_project`),
`artifact_ownership.py`, `setup_check.py`, `docs/setup.md`. Claude:
code.claude.com/docs/en/sandboxing, /settings, /permissions (`sandbox.*`,
`--settings`, `--setting-sources`, `--strict-mcp-config`,
`--disallowedTools`). Codex: `codex sandbox --help` (`-P :read-only`),
`-c` overrides.

**Implementation freedom:**
- The exact settings and permission rules, the launch-time probe, the
  allowlist syntax and the table format.
- If a Claude setting does not behave as documented, find a working
  combination or mark that control unsupported, with evidence.

**Boundaries / non-goals:**
- No general verification or production-network policy, and no custom
  sandbox.
- No Linux or Windows support beyond an honest "unsupported" entry.
- No Sporely changes. No VS Code changes beyond showing existing refusal
  text.
- No writes to real remotes. Read-only connectivity probes are allowed.
- Do not edit the user's global Claude, Codex or Git configuration.

**Acceptance evidence:**
- **Generated rules check.** A deterministic test asserts that the generated
  Claude settings and permission rules contain every required control. It
  also asserts that no user, project or local source is loaded.
- **Scripted probes.** These run inside the exact engine-built launch
  (arguments, settings, environment), for each supported provider and role,
  in a disposable repository. The remotes are a local bare repository and a
  localhost-served disposable remote with working credentials. Tested in
  both managed and unmanaged layouts.
  - Must fail, leaving both remote refs and engine state unchanged:
    - `git push` to those remotes, to an explicit URL, and with
      `-c credential.helper=…`;
    - `gh api` ref updates;
    - egress to a Git host;
    - connecting to the SSH agent and Docker sockets by path;
    - reading credential files and credential environment variables;
    - writing `.git/config`, `hooks/` or `config.worktree`;
    - writing `.sparring/`, the managed record or user configuration;
    - `sparring resume-plan --allow-push-candidate`.
  - Must work: a declared allowlisted host, a loopback test service on a
    declared port (never the disposable Git remote's port), and a local
    commit and test run.
  - Must be refused at configuration time: an allowlist entry for the
    disposable remote's host and port, and a proxy-like entry.
  - The credential-reachability probe is run in two setups. In one, a
    disposable test credential is retrievable; in the other, none is. The
    probe yields "detected, not prevented" and "prevented" respectively, and
    the recorded run guarantee matches.
- **Real-provider sessions.** An opt-in live test runs a real engine-launched
  Claude stage session and a real Codex review session. Each is told to
  attempt the same denied actions, through the shell and through non-shell
  tools (Read, Edit, Write).
  - The Claude session also attempts `dangerouslyDisableSandbox`, editing
    worktree `.claude/settings*.json` and resuming, WebFetch and subagents.
  - Transcripts show each attempt. Where the model declined, the scripted
    probe or rule check is the evidence.
  - Afterwards an engine-authorized push advances only the approved ref.
- **Credential hygiene.** No test may put a credential value into a model
  transcript, log, report or terminal output.
  - Probes report presence or absence only. Credential command output is
    discarded inside the probe.
  - Real-provider live sessions run only under an OS account whose
    keychain, Git and `gh` configuration and readable files hold no real
    Git-host credentials. The test checks this with the presence-only probe
    before starting the session, and refuses to start otherwise. In an
    account that holds real credentials, only the scripted presence-only
    probes and the generated-rules check run, with no model turn. A real
    token is therefore unreachable to the model by construction, not by
    instruction.
  - Every transcript and log a test produces is scanned for credential
    patterns (GitHub token prefixes, private-key headers, the disposable test
    secret). The scan prints counts only and never the matched text.
  - Any hit for a real credential fails the run and is reported as needing
    revocation.
- **Refusals.** Each of the following is refused before any agent process
  starts: an unsupported or degraded configuration, a failed launch probe,
  an unsupported CLI version, a Git-host allowlist entry, and loaded user
  MCP servers that cannot be suppressed.
- **Report.** It contains the completed capability table with residual
  risks, the runner and candidate SHAs, and the exact opt-in commands and
  their output. Independent review inspects these records, not only the
  code.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass. If a
  required control cannot be demonstrated, report a blocker. Do not claim
  READY.
