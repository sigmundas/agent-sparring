# Provider launch boundaries (issue #5, Plan A)

Every Claude and Codex invocation the engine makes is built in one place. It
either runs inside the intended boundary or is refused before any process
starts. Nothing falls back silently, and the configuration is never widened.

This plan replaces the launch half of the failed Stage 2 of
`docs/plans/secure-agent-git-writes.md`. Candidate `1700b4d` is **reference
material only**: reviewed pieces may be ported from it (see the audit §2d), but
nothing is cherry-picked unreviewed. The audit is
`docs/audits/issue5-recovery-2026-10-18.md`.

**Depends on:**
- Plan R, accepted and merged to `main`. Base each run on that `main`.
- Decisions D1, D3 and D4, recorded in the audit.

**Integration branch.**
- Plans A, B and C stages 2 to 3 all land on the integration branch
  `issue5/boundary`.
- That branch merges to `main` only after the complete implementation passes
  Plan C's human live verification (D3). Until then, `main` launches agents as
  it does today.
- Nothing in this plan adds a fail-closed control to `main` early.

**Approved decisions (2026-10-18), recorded in the audit:**
- **D1.** Prevention is claimed only with a demonstrated boundary separating
  agents from credentials, write authority and engine state. Same-user runs
  are a compatibility mode.
- **D3.** Keep this off `main` until live verification passes.
- **D4.** Trusted runner, policy and state must be outside the agent's
  *effective* write authority. Outside the worktree is not enough under the
  same account.
- **R-1.** Each execution path is verified separately.
- **R-2.** Enforcement is separate from evidence.

**Execution modes.**
- A run records one of two modes:
  - `same_user`, the compatibility mode: agents run under the engine's own OS
    account;
  - `isolated`: agents run under a separate account that cannot write the
    trusted runner, policy, authorization state or credentials.
- This plan builds the same boundary in both modes. `isolated` itself, meaning
  the cross-account launch and its ownership checks, is **Plan D (not yet
  drafted)**.
- Until Plan D exists, every run is `same_user`. Under D4, its engine-state
  protection is a provider control, not an OS boundary, and is reported as
  such.

**Runner.** Run this plan with a frozen runner installed outside the agent's
worktree. This is decision D4. This plan changes the launch code that would
otherwise confine its own implementer.

**Not in this plan:**
- credential measurement or recorded guarantees (Plan B);
- live provider evidence (Plan C).

This plan proves its rules deterministically. Every provider-semantics claim
stays **unverified** until Plan C's human live run records it in the capability
table.

## Security invariants (every later refactor must keep these)

- **I-A1. One builder.** A provider process starts only from a `LaunchSpec`
  that the single builder produced for exactly this invocation. A `LaunchSpec`
  holds the provider, role, kind (start, turn, resume), argv, environment,
  settings bytes and a digest. No adapter can start a process without one.
- **I-A2. Built every time.** The builder runs for every invocation, resume
  included. Its inputs come only from the immutable run-start policy in
  engine-protected state, never from the worktree or `HEAD`.
- **I-A3. Enforcement never depends on evidence (R-2).**
  - The builder always applies the full boundary for the provider and role.
    It refuses only when it cannot apply that boundary: a required flag or
    settings key is rejected, the sandbox is unavailable
    (`failIfUnavailable`), or the policy is invalid.
  - The capability table and all verification evidence decide **only which
    guarantees may be claimed**. They never decide whether to launch, and they
    never select a narrower or different configuration.
  - The table is reviewed, committed data. No file the engine writes at
    runtime is read by the builder or by the claim logic.
- **I-A3a. Claims per execution path (R-1).**
  - Every claim is keyed by provider × role × exact version × platform ×
    execution mode × **execution path** × control.
  - The execution paths are `shell`, `file_tools`, `mcp`, `web_tools`,
    `subagents` and `launch_config`.
  - A claim for one path never implies another.
- **I-A4. Engine state is outside every writable root.** Engine state and the
  boundary's own settings sit outside every agent-writable root, and are also
  explicitly denied to the shell and the file tools.
- **I-A5. Allowlisted environment.** The provider environment is an
  allowlist. Nothing is inherited by default.
- **I-A6. Refusal is the only fallback.** Every refusal is typed and happens
  before any provider process starts.

## Stage 1 — One launch builder, pinned run-start policy, reviewed capability table

**Context (audit §2):**
- `cli.py` `_claude_boundary_builder` re-reads remotes and the network policy
  from live sources.
- An unmanaged `resume-plan` without `--run-key`, and `run-stage`, read
  `project.toml` from disk.
- `plan.py` snapshots the network policy from `HEAD`, which an agent can
  commit to.
- `--permission-mode` is free text.
- `record_verified_controls`/`load_verified_controls` let runtime-written JSON
  certify controls.

**Outcome:**
- **The builder module.** A new module, `launch_spec.py`, holds a pure
  function. It takes three inputs:
  - the frozen run policy;
  - provider, role and kind;
  - the detected provider version and platform.

  It returns a `LaunchSpec`, or raises `BoundaryError` with a typed code. Both
  provider adapters take a `LaunchSpec` per call. Constructing an adapter
  without a builder is a `TypeError`. The old `boundary_args` path is removed.
- **The run-start policy.** `start-plan` records it once in engine run state.
  It holds:
  - the network policy, parsed from `.sparring/project.toml` at the
    engine-recorded run base SHA (managed `base_sha`, or the pinned start
    commit), never `HEAD` or disk;
  - the provider executable paths and their sha256;
  - the exact provider versions;
  - the permission mode;
  - the WebFetch flag.
- **Every launch path uses the run policy.** `resume-plan` without `--run-key`
  resolves the run it resumes and uses that run's policy. `run-stage`, `ask`,
  `run-sparring` and intake either use an owning run's policy or refuse with
  `no_run_policy`.
- **Changes after start are refused.** A provider binary whose sha256 or
  version differs from the run-start value refuses with
  `provider_changed_since_start`.
- **Permission mode.** It is an enum, not free text: Claude stage `acceptEdits`;
  `bypassPermissions` and `auto` are refused.
- **The capability table is reviewed data, used for claims only.**
  - It is a committed module, `capability_table.py`, keyed as in I-A3a.
  - Each row is `verified`, `unsupported` or `residual`, with an
    `evidence: "<bundle digest>"` field.
  - It ships with **no verified rows**, so every claim starts as
    `unverified`.
  - The claim function takes the table as an argument. The builder does not
    import it, which a test asserts.
  - `record_verified_controls`, `load_verified_controls` and the
    `VERIFIED_VERSIONS` minimum gate are deleted.
- **Unknown provider version.**
  - The full boundary is still applied.
  - Every claim is `unverified`.
  - A required flag or settings key that the installed CLI rejects refuses
    with `provider_flag_unavailable`. It is never dropped.
- **The run records its mode and claims.**
  - It records its execution mode, which is always `same_user` until Plan D.
  - It records the per-path claim set for each `LaunchSpec`.
- **Runner location (necessary, not sufficient, under D4).**
  - A launch refuses with `runner_inside_agent_root` when the running engine's
    package files, the run policy or the authorization state sit inside any
    agent-writable root. Agent-writable roots are the agent working directory,
    the managed worktree, `$TMPDIR` and every sandbox write root.
  - In `same_user` mode, the run's claims state that these are "protected by
    provider sandbox rules, not an OS boundary".

**Starting points:**
- `cli.py`: adapter construction, `_claude_boundary_builder`,
  `_require_codex_boundary`, `_agent_network`, `run-stage`, `ask`,
  `run-sparring`, intake.
- `plan.py`: `start_plan`, `resume_plan`, the `PlanRunState` network fields.
- `providers/claude_cli.py`, `providers/codex_cli.py`,
  `providers/subprocess_runner.py`, `managed_run.py` (`base_sha`), `config.py`.
- Reference: `agent_boundary.py` at `1700b4d`.

**Boundaries:**
- No new sandbox settings content (Stage 2 and Stage 3).
- No credential probing (Plan B).
- No VS Code changes beyond the existing refusal text.

**Acceptance evidence:**
- **Golden specs.** For each (provider, role, kind), golden tests check the
  exact argv, the environment key set and the settings bytes, given a fixed
  policy and table.
- **No fallback.** An adapter given no `LaunchSpec` cannot launch. A `grep`
  test asserts that no `subprocess`/`run_streaming` call in `providers/` takes
  an argv the builder did not produce.
- **Pinned policy.** A run starts. Then an agent-style commit changes
  `.sparring/project.toml` (to widen the allowlist or enable WebFetch) and the
  disk copy is edited. Resume (with and without `--run-key`) and `run-stage`
  all produce a `LaunchSpec` identical to the run-start one, or refuse. None
  reads the new value.
- **Provider change.** The provider binary is replaced after start (a test
  binary at the same path with a different hash) → `provider_changed_since_start`.
- **Permission mode.** `--permission-mode bypassPermissions` → typed refusal.
- **Enforcement independent of evidence.**
  - The empty table, a table with every row `verified`, and a table with
    arbitrary rows all produce a byte-identical `LaunchSpec` for the same
    inputs. Only the recorded claims differ.
  - No file placed in the user-config directory, `.sparring/` or `$TMPDIR`
    changes either the `LaunchSpec` or the claims.
- **Unknown version.** It gives the full boundary and all-`unverified`
  claims.
- **Rejected flag.** A fake CLI that rejects a required flag gives
  `provider_flag_unavailable` before any provider process starts.
- **Per-path claims.** A `verified` row for `shell` does not make the same
  control `verified` for `file_tools` or `mcp`.
- **Runner location.** Each of the following gives
  `runner_inside_agent_root`:
  - running from an editable install inside the worktree;
  - a policy file under `$TMPDIR`.
- **Test runs.** Focused tests and `.venv/bin/python -m pytest -q` pass.

## Stage 2 — Claude boundary content: sources, engine state and environment

**Context.** The Claude settings at `1700b4d` have several defects:
- they live in an engine `$TMPDIR` directory that is not itself denied;
- they do not deny the linked gitdir's `config`, `hooks/` or
  `config.worktree`;
- they put `~/.gitconfig` in `denyRead`, which may break local commits;
- they scrub the environment with a denylist.

`--setting-sources ""` is unconfirmed. Managed settings, `.claude/agents`,
skills and `CLAUDE.md` still load.

**Outcome:**
- **Settings location.** The builder writes Claude settings into an
  engine-owned directory outside every agent-writable root, with mode `0700`.
  It writes them with `O_NOFOLLOW` and atomic replace, and denies that directory
  through `denyWrite` and `permissions.deny`.
- **Protected engine state.** Each path is denied to the Bash sandbox
  (`filesystem.denyWrite`) and to the file tools (`permissions.deny` for Edit,
  Write and NotebookEdit). The paths are:
  - `.sparring/`;
  - `<git-common-dir>/agent-sparring/` (which includes Plan R's `runs/`);
  - the settings directory;
  - the user-config directory;
  - `<git-common-dir>/config`, `<git-common-dir>/hooks/`, `info/attributes`,
    and every `worktrees/<n>/config.worktree`;
  - in an unmanaged layout, `.git/config`, `.git/hooks/` and
    `.git/config.worktree`.

  Writes to objects, refs, logs and the worktree's own index/HEAD stay allowed.
- **Credential reads.** `~/.gitconfig` is no longer denied. Credential-bearing
  paths stay denied: `~/.ssh`, `~/.config/gh`, `~/.git-credentials`,
  `~/.config/git/credentials`, `~/.netrc`, `~/.docker/config.json`, cloud CLI
  credential dirs, and any `credential.helper=store --file` path found in the
  snapshot.
- **Execution paths (R-1).** The settings handle each Claude path explicitly,
  and `docs/setup.md` states which mechanism governs each one:
  - `shell`: the OS sandbox;
  - `file_tools` (Read, Edit, Write, NotebookEdit, Glob, Grep): permission
    rules only;
  - `mcp`: no servers loaded;
  - `web_tools` (WebFetch, WebSearch): disallowed unless the project enables
    them;
  - `subagents`: whether they inherit the boundary;
  - `launch_config`: setting sources.
- **Settings sources.**
  - The builder uses one documented mechanism for loading no user, project or
    local settings, plugins, hooks, MCP, agent definitions, skills or memory
    files.
  - The candidates are `--setting-sources` with an explicit value the CLI help
    documents, `--strict-mcp-config`, and `--bare` if it suppresses hooks,
    plugins and `CLAUDE.md`.
  - The choice and its documented basis go in `docs/setup.md`. Its live effect
    is a Plan C obligation, marked `unverified` until then.
- **Environment.** Allowlisted: `PATH` (fixed), `HOME`, `TMPDIR`, `LANG`,
  `LC_*`, `TERM`, and `USER`/`LOGNAME`. Nothing else is inherited. The provider
  API connection uses only what the CLI needs, with no `GIT_*`, `*_TOKEN`,
  `SSH_*` or cloud credentials.

**Starting points:** `launch_spec.py` from Stage 1. Port and correct
`build_claude_settings`, `_permission_deny_rules`, `write_claude_settings` and
`claude_launch_args` from `1700b4d`. `managed_run.py` provides the gitdir
layout. Claude docs: `/sandboxing`, `/settings`, `/permissions`, `claude --help`.

**Boundaries:**
- No network-policy changes (Stage 4).
- No live provider sessions.
- Do not edit the user's Claude, Codex or Git configuration.

**Acceptance evidence:**
- **Generated rules check.** A deterministic rules check over the generated
  settings, for managed and unmanaged layouts, asserts every protected path
  above, for both the shell and the file-tool rules.
- **Mutation tests.**
  - Each required deny entry, the source-suppression flag and each sandbox key
    is removed from the builder output in turn.
  - The rules check fails each time, naming the missing control. This proves
    the check can fail.
- **Settings tampering.** A symlink is pre-placed at the settings path, and
  another file is placed in the settings directory. The write refuses
  (`settings_path_tampered`) or replaces it atomically without following the
  link.
- **Environment.** A parent environment containing `GIT_CONFIG_PARAMETERS`,
  `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `AWS_SECRET_ACCESS_KEY` and an arbitrary
  `FOO_TOKEN` produces a `LaunchSpec` environment containing only allowlisted
  keys.
- **Local commit.** A local commit test runs plain `git commit` with an
  environment and `HOME` matching the `LaunchSpec`. It runs without the sandbox,
  as a check that the deny list itself does not break Git.
- **Test runs.** Focused tests and the full suite pass.

## Stage 3 — Codex boundary on every invocation

**Context.**
- `providers/codex_cli.py:808-830` was unchanged by Stage 2. It passes only
  `-c sandbox_mode="read-only"`.
- `~/.codex/config.toml` and its rules still load.
- The boundary is checked once per adapter.
- The installed CLI offers `--ignore-user-config` and `--ignore-rules` on
  `exec` and `exec resume`, and `-P` permission profiles.

**Outcome:**
- **Builder.** Codex `exec` and `exec resume` argv come from the Stage 1
  builder on every call. The argv includes the read-only sandbox,
  `--ignore-user-config`, `--ignore-rules`, and `-c` overrides that empty
  `mcp_servers` and disable `notify`. A flag missing from the installed
  version's `--help` refuses with `provider_flag_unavailable`.
- **Gating.** Every Codex call gets the builder and table gate: reviewer turns,
  `ask`, `run-sparring`, the loop, intake and resume.
  `codex_user_mcp_configured` remains as an additional fail-closed check.
- **SSH read restriction.** If a `-P` permission profile can deny reading
  `~/.ssh` and the other credential paths, the builder uses it. Its effect is
  `unverified` until Plan C. Otherwise the Codex claim for "credential files
  unreadable" on the `shell` path is `unsupported`, and Plan B records the
  consequence.
- **Environment.** The same allowlist as Claude.

**Starting points:** `providers/codex_cli.py` (`_build_args`, resume),
`launch_spec.py`, `cli.py` (every `CodexCliAdapter` construction:
`rg -n CodexCliAdapter src`), `codex exec --help`, `codex exec resume --help`,
`codex sandbox --help`.

**Boundaries:**
- No live sessions.
- Do not edit `~/.codex/config.toml`.

**Acceptance evidence:**
- **Golden argv.** Golden argv for start and resume, reviewer and ask.
- **Mutation tests.** Dropping `--ignore-user-config` or the MCP override fails
  the rules check.
- **Every call site.** Each CLI path that constructs a Codex adapter is driven
  with a fake `codex` executable that records its argv and environment. Every
  recorded invocation, resume included, carries the full boundary and an
  allowlisted environment.
- **Missing flag.** A fake `codex --help` without `--ignore-user-config` gives
  a refusal before any `exec`.
- **Test runs.** Focused tests and the full suite pass.

## Stage 4 — Network policy validation

**Context (`agent_boundary.py:293-296,338-418` at `1700b4d`):**
- Wildcard suffixes such as `*.com` pass.
- IP literals are accepted.
- Loopback spellings are not treated as equivalent.
- Unconfigured self-hosted Git hosts are allowed.
- Proxy detection relies on labels and ports.

**Outcome:**
- **Validator.** `validate_network` refuses:
  - any wildcard whose suffix is a public suffix or contains a known Git host
    or a configured remote host;
  - IP literals, except loopback entries in `loopback_ports`;
  - bare loopback without ports;
  - any loopback port equal to a configured remote's port, whatever the
    spelling (`localhost`, `127.0.0.0/8`, `::1`, `[::1]`);
  - known Git-hosting web, API and SSH hosts;
  - proxy- or tunnel-like entries (label- and port-based detection kept, plus
    a documented list).
- **Inputs.** Remotes come from the run pin and the trusted snapshot (Plan R),
  not live config.
- **Refusals.** Each refusal names the `project.toml` key and entry.
- **Runtime denials.** A runtime denial cannot be attributed by the engine. The
  docs say so, and the agent's stage prompt names the `project.toml` key to
  change.

**Starting points:** `agent_boundary.py` at `1700b4d` (`validate_network`,
`_suffix_match`, `_normalize_host`, `configured_remote_endpoints`), `config.py`
(`AgentNetworkConfig`), `launch_spec.py`, `docs/setup.md`.

**Boundaries.** No DNS resolution at validation time. Do not resolve hosts to
IPs; document that as a residual risk.

**Acceptance evidence:**
- **Table-driven tests.** Every refusal class above is covered, plus accepted
  examples (`pypi.org`, `files.pythonhosted.org`, a loopback test port).
  Explicit cases are `*.com`, `*.io`, `140.82.112.3`, `[::1]` against
  `localhost:PORT`, and `127.0.0.2` against `localhost:PORT`.
- **Policy pin.** The validator runs on the pinned policy at start, and again
  on every `LaunchSpec` build as a defence-in-depth check.
- **Test runs.** The full suite passes.
