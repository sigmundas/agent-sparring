# Issue #5 recovery audit — secure-agent-git-writes run 00aa7e30

Date: 2026-10-18. Status: **draft for human approval**. Not committed.

## Provenance

- Bundle: `/Users/Shared/sparring-outbox/secure-agent-git-writes-1700b4d.bundle`.
  `git bundle verify` reports it as okay, with complete history and one head:
  `refs/heads/sparring/secure-agent-git-writes-00aa7e30-895068f9` →
  `1700b4d3e6849f61079c0972e8c2a33505703efb`.
- It was imported read-only as `refs/review/secure-agent-git-writes-1700b4d` in
  the normal checkout, with hooks disabled. No branch, worktree or working tree
  was changed.
- Chain: `ea8e70d` (local main) → `507b5ec` → `c61eaf8` (plan) → **`fea4813`,
  `d1d48d0` (Stage 1)** → `7b6b976` (Stage 2 round 1) → `99ffb6f` → `aadd7c7` →
  `7a50e8a` → `70b281b` → `1700b4d` (round 5b).
- **The Stage 1 endpoint is `d1d48d0`.** It is the parent of `7b6b976`, which
  is verified in the history.
- The run's own `.sparring/` records (review verdicts, round findings) are under
  `/Users/sparring-agent`, which this account cannot read. Round findings are
  therefore known only from the commit messages and the operator's summary.
- **Method.** Static review only. No candidate code, test or provider session
  was executed: this account holds real Git credentials. No pytest counts are
  claimed for either stage.

## 1. Stage 1 audit (`c61eaf8..d1d48d0`)

What is sound, verified in the code:
- **Engine Git.** Every Git call goes through `engine_git.run`/`run_remote`.
  The hardening covers hooks, fsmonitor, textconv, external diff, filters,
  merge drivers, repository-scope includes, `remote.*.uploadpack`/`receivepack`,
  worktree config and `extensions.worktreeConfig`.
- **Push gate.** It no longer treats reachability as success. The Stage 4b case
  becomes the typed pause `unauthorized_remote_movement`.
- **Pin and receipts.** The pin, receipts and retarget refusals are present.
- **Prompts.** No prompt tells an agent to push. `AGENTS.md` is amended.
- **Claims.** The text consistently says "detection, not prevention".

| ID | Sev | Location (`d1d48d0`) | Failure scenario | Missing evidence | Fix |
|---|---|---|---|---|---|
| S1-1 | **High** | `engine_git.py:476-480`; `managed_finish.py:331,456-460,123`; callers `runs_report`, `finish-run --dry-run` | Outside a finish guard, a remote command runs against the **live** repository config. An agent plants `core.sshCommand`, `credential.helper=!…`, `remote.origin.uploadpack` or `url.*.insteadOf`. Every `sparring runs --json` poll (VS Code polls it) or dry-run finish then executes that program, or follows the redirect, with the user's credentials. | No test of `finish_status` or `runs` with planted config. | `run()` refuses any remote command that has no guard and no explicit trusted snapshot. `finish_status` uses the pin and its snapshot. |
| S1-2 | **High** | `engine_git.py:476`; `acceptance.py:510`; `git_context.py:299-302`; `handoff.py:213` | Sibling repositories, the `handoff` CLI and freeze/accept of stages no run owns call `ls-remote` with live config, even inside a managed run. | Not tested. | Snapshot siblings at run start, or refuse repository-local transport keys when there is no snapshot. |
| S1-3 | Medium | `managed_finish.py:1031-1039` | If the run state file is missing, or `remote_pin` is deleted from it, the guard becomes `nullcontext()`. Push-target and remote-branch deletion then use the remote name with live config. Only corrupt JSON fails closed. | Only `test_managed_finish.py:1738` (the corrupt case). | The managed-run record in the Git common directory records `pinned: true`. A pinned record whose pin is missing refuses. |
| S1-4 | Medium | `acceptance.py:231,247,292-294` | Ownership is read from the stage's own `state.json`. An unpinned legacy run, or an empty `run` field, skips the movement check and falls back to reachability. A Stage 4b candidate can then be accepted through `accept-candidate` before any resume pins the run. | No test. | Resolve ownership from engine run state. Pin or refuse in freeze/accept. |
| S1-5 | Medium | `remote_pin.py:466-471`; `push_gate.py:491-495`; `test_push_authorization.py:742-767` | `_covered` counts acknowledged tips as explained. After an acknowledgement the run accepts the agent-pushed candidate **with no push authorization**, and a test asserts exactly that. This contradicts the plan ("never counts as … push authorization"). One acknowledgement also covers every ancestor candidate. | The acknowledgement test stops at state. | **Human decision D2.** The recommendation is in Plan R. |
| S1-6 | Medium | `engine_git.py:202-203,311-320,775` | Any `include`/`includeIf` in **global or system** config refuses every engine Git call. `includeIf "gitdir:"` in `~/.gitconfig` is common, which breaks "existing setups keep working". | Only a repository-scope include is tested. | Refuse includes only in local and worktree scope. Global and system scope use expanded entries. |
| S1-7 | Low | `push_gate.py:476-514` | Race window: an agent pushes C after `check_movement` and before the engine push. The engine push is a no-op or fast-forward, and its receipt absorbs the agent's push. | None. | Compare the push porcelain's old→new against the observed tip. On mismatch, record movement instead of a receipt. |
| S1-8 | Low | `engine_git.py:444-450,481-484` | `worktree add` and checkout run with live config. A filter written between check and run could execute. *Suspected.* | None. | Run checkout-performing commands under `isolated_config`. |
| S1-9 | Low | `engine_git.py:108-124` | `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`, `GIT_CONFIG_{GLOBAL,SYSTEM,NOSYSTEM}`, `GIT_TEMPLATE_DIR` and `GIT_EDITOR` are not stripped. The docstring overclaims. | None. | Strip them. |
| S1-10 | Low | `engine_git.py:11-13` | A local-path remote runs its own receive hooks and config, because `GIT_CONFIG_PARAMETERS` is cleared for the local transport. | None. | Document it, or refuse unpinned file remotes. |
| S1-11 | Low | `loop.py:425-429,564-567` | A turn that raises, or times out, skips `after_turn`. This is a timing gap only: the next push or freeze still catches the movement. | None. | Run the check in `finally`. |
| S1-12 | Info | `remote_pin.py:24-45`; `plan.py:2669-2716` | The pin, receipts, acknowledgements and trusted snapshot all live in agent-writable `.sparring/plans/<run>.json`. An agent can add a helper to both `trusted_config` and `.git/config`. Without agent confinement, Stage 1 guarantees nothing against an active agent. The code says so. | — | Keep saying so. Plan A protects these files. |

Acceptance-evidence gaps (plan lines 199-234):
- **Accept refusal is weak.** The stage is still WORKING, so it would refuse
  anyway.
- **Acknowledgement path is weak.** See S1-5.
- **The "no marker" check for `credential.helper` and `core.sshCommand` is
  vacuous.** A local bare remote never calls either.
- **No planted-configuration matrix for some operations.** `verify_pushed`,
  managed finish `--push-target`, remote branch deletion and the main-checkout
  merge each lack one.
- **The full test suite has not been run outside the bootstrap run.**

**Integration recommendation for Stage 1.**
- Stage 1 is self-contained. It imports nothing from Stage 2, and it is worth
  keeping.
- **Do not merge `d1d48d0` as it is.** First complete it with Plan R, which
  fixes S1-1 to S1-4 and S1-5 according to decision D2.
- Then merge Stage 1 plus Plan R to `main` as a **detection and engine-authority
  improvement**, described as giving no protection against an active agent.
- The S1-6 compatibility fix belongs in Plan R. Without it, users with global
  includes are locked out.

## 2. Stage 2 gap matrix (`d1d48d0..1700b4d`)

Status is one of these:
- **PRESENT:** the defect is still there.
- **FIXED.**
- **NO-AUTO:** no automated verification exists.
- **PLATFORM:** a limit of the provider or OS.

The label is the guarantee the engine may truthfully record today.

| Finding | Location (`1700b4d`) | Status | Evidence | Label | Goes to |
|---|---|---|---|---|---|
| F1: Claude harness needs 7 controls, proves 2 | `tests/test_real_provider_boundary.py:192-201,219-223` | PRESENT | `_run_effect_probe` can add only `ENGINE_STATE_READONLY` and `NO_GIT_HOST`. The assert requires all 7. A Claude record can never be written, so every Claude launch is refused. That is fail-closed, but undeliverable. | unverified | C |
| F2: probes pass without attempting | same, `:157,179,194-200,101-102` | PRESENT | "Proven" means a sentinel is absent, so a model that does nothing passes. There is no positive control. Only `refs/heads/main` is inspected, so new branches and tags are unseen. `NO_GIT_HOST` is "proven" from a `file://` remote. | unverified | C |
| F3: Codex certified unmeasured | same, `:234-261`; `agent_boundary.py:908-917` | PRESENT | The test records `NO_CREDENTIAL_ENV` and `NO_WIDENING_SOURCES` with no probe ("no in-boundary effect to observe"). There is no Codex socket, egress or managed-layout probe, and no socket row in the table. | unverified | A, C |
| F4: credential probe outside the boundary | `agent_boundary.py:1108-1170`; `plan.py:1600,2121-2130` | PRESENT | The probe runs in the engine process, once at start and resume. It does not run per turn, on `run-stage`, per loop stage, or on reviewer launches. See note (a). | detected / unverified | B |
| F5: helper failure read as absence | `agent_boundary.py:1225-1226,1254` | PRESENT | A non-zero `git credential fill` returns `False`. Only https and the configured helper are probed. GCM generic-password items and SSH-agent identities are missed. This yields a false "prevented". | detected / unverified | B |
| MCP isolation, Claude | `agent_boundary.py:576-598` | NO-AUTO | `--strict-mcp-config` exists. `--setting-sources ""` is unconfirmed, because help says "comma-separated list". Managed settings, `.claude/agents`, skills and CLAUDE.md still load. | unverified | A, C |
| MCP isolation, Codex | `providers/codex_cli.py:808-830` (unchanged) | PRESENT | `~/.codex/config.toml` and its rules still load. The installed CLI offers `--ignore-user-config` and `--ignore-rules` on `exec` and `exec resume`, and neither is used. The only mitigation is refusing a non-empty `[mcp_servers]`. `notify`, profiles, `shell_environment_policy` and `model_providers` are unchecked. | unverified | A |
| Writable verification records | `agent_boundary.py:855-886,444-449` | PARTLY FIXED; **design flaw** | The record is an unauthenticated JSON list of control names. Any process of this user outside the sandbox can write it with a public function. See §2b. | — | A (remove) |
| Pinned config sources | `cli.py:1445-1466`; `plan.py:1376-1402` | PRESENT | Unmanaged `resume-plan` without `--run-key`, and every `run-stage`, read `project.toml` from disk. The snapshot is read from `HEAD`, which an agent can commit to. | — | A |
| Network allowlist | `agent_boundary.py:293-296,338-418` | PRESENT | Wildcard entries such as `*.com` and `*.io` pass. IP literals are accepted. Loopback spellings are not treated as equivalent (`127.0.0.1` vs `localhost:PORT`). Unconfigured self-hosted Git hosts are allowed. Proxy detection relies on labels and ports. | — | A |
| Construction-only checks | `cli.py:1469-1484,1601` | Claude FIXED; Codex PRESENT | Claude's `build()` re-checks on every turn. Codex is gated once per adapter, and resume re-checks nothing. | — | A |
| Permission mode | `claude_cli.py:416,494-495`; `cli.py:3500,3938` | PRESENT | `--permission-mode` is free text, so `bypassPermissions` is accepted. The record is not bound to the mode or the policy. | — | A |
| Linked gitdir writable | `agent_boundary.py:444-449` | PRESENT (suspected) | `denyWrite` does not cover `<common>/config`, `hooks/` or `worktrees/<n>/config.worktree`. Claude's built-in `.git` protection is documented for the working directory's `.git`, not a linked gitdir. | unverified | A, C |
| `~/.gitconfig` in `denyRead` | `agent_boundary.py:121` | suspected functional defect | Git may die reading global config, which would break the required "local commit works". | — | A, C |
| Hostless remote reports "prevented" | `agent_boundary.py:1146-1147` | PRESENT | A local-path remote under a sandbox-writable root is reported ABSENT. | detected | B |
| Guarantee can downgrade on resume | `plan.py:2128-2130` | PRESENT | Resume overwrites "detected" with "prevented". | — | B |
| Env scrub is a denylist | `agent_boundary.py:116-131,1273-1285` | PRESENT | It misses `GIT_CONFIG_PARAMETERS`/`COUNT`/`KEY_n`, `GIT_CONFIG_GLOBAL`, `CI_JOB_TOKEN`, `HOMEBREW_GITHUB_API_TOKEN` and others. | — | A |

Note (a), on F4. Measuring outside the boundary is a sound *upper bound* for
absence, because the sandbox only removes access. If a complete enumeration finds
no credential, none exists inside the boundary either. The real defects are
these:
- the probe is incomplete (F5);
- it does not run per invocation;
- an outside measurement can never credit the sandbox with blocking a
  credential that *is* present.

Plan B is built on that distinction.

### 2b. Self-certification paths (the core design flaw)

1. **Absence of a breach counts as proof.** The model decides whether it
   attempts anything, so inaction certifies a control.
2. **The verification record is a bare assertion.** It is
   `{platform, version, exe sha, fingerprint, controls[]}`. It carries no
   evidence and no MAC, and it is minted by a public library function. In
   round 3, `conftest` injected it.
3. **The fingerprint misses modules that steer policy.** `_FINGERPRINT_MODULES`
   omits `user_config.py`, which chooses the record location from an environment
   variable, as well as `config.py`, `plan.py`, `engine_git.py` and the harness.
   An agent developing agent-sparring with an editable install can change these
   without invalidating a record.
4. **The network snapshot comes from `HEAD`,** and an agent can commit to `HEAD`
   with plumbing.
5. **The credential probe runs repository-configured helpers outside the
   sandbox,** without `engine_git` hardening. A planted `credential.helper=`
   reset reads as "prevented"; a planted `!cmd` executes unsandboxed.
6. **The settings file sits in an engine `$TMPDIR` directory that is not
   itself denied.** A symlink or rename race is suspected.

**Conclusion.** No runtime-writable record may gate launches. Confinement
evidence must come from a human-run harness, from reviewed code, and be turned
into reviewed, committed data (Plan C human live-verification gate).

### 2c. Platform limitations (cannot be solved with provider configuration)

- **Claude on macOS.** Login-keychain items are readable from inside the
  sandbox, and nested Seatbelt is refused. While any write-capable keychain item
  exists, the guarantee is "detected, not prevented".
- **No model-free in-boundary execution path is known for Claude.** An exact
  in-boundary probe without a model turn is therefore unsupported, and
  "prevented *by the boundary*" cannot be claimed for Claude.
- **Codex read-only reads `~/.ssh`.** It stays "detected" unless a `-P` profile
  read-deny is proven (Plan C).
- **File tools follow permission rules only.** Read, Edit, Write, Glob and
  Grep are enforced in-process, not by the OS.
- **A local-path remote under a writable root** cannot be protected by network
  controls.
- **Processes started inside the sandbox can survive between turns,** and no
  provider facility kills them. State across turns is detected only.

### 2d. Salvage

**Keep:**
- `BoundaryError` and its codes.
- `build_claude_settings`, `_permission_deny_rules` and
  `write_claude_settings`, moved out of writable roots.
- `claude_launch_args`, after re-verifying the empty `--setting-sources` value.
- The per-turn `boundary_builder` hook.
- The `run_streaming` `env` parameter.
- `_host_port_of_url`, `configured_remote_endpoints` (moved onto `engine_git`)
  and `_normalize_host`.
- The table plumbing: `capabilities`, `require_supported` and
  `capability_table_markdown`.
- `codex_user_mcp_configured`, as a secondary check.
- `scan_for_credentials`.
- The `AgentNetworkConfig` parser.
- The `PlanRunState.agent_guarantee` and `agent_network` fields.

**Rewrite:**
- `validate_network`.
- `scrubbed_environment`, as an allowlist.
- The `probe_credentials` family.
- `_agent_network` and `_agent_network_snapshot`.
- The Codex `_build_args`, as a per-turn builder.
- `_require_codex_boundary`.
- `--permission-mode`, which must be pinned.

**Discard:**
- `_run_effect_probe` and `_sentinels`.
- `effect_probe_prompt` and `probe_prompt` as evidence.
- `record_verified_controls` and `load_verified_controls`.
- `VERIFIED_VERSIONS` as a ">= minimum" gate.

Salvage means porting reviewed pieces onto Stage 1 + Plan R. **Do not merge
`1700b4d`.**

## 3. Plans and integration sequence

| Plan | File | Stages | Depends on |
|---|---|---|---|
| R — Complete Stage 1 | `docs/plans/issue5-r-complete-engine-git-authority.md` | 2 | `d1d48d0`, D2 |
| C — Verification harness | `docs/plans/issue5-c-security-verification-harness.md` | 3 + human live-verification gate ("C4") | C1: none. C2-C3: A1-A3, B1. C4: all of A, B |
| A — Provider launch boundaries | `docs/plans/issue5-a-provider-launch-boundaries.md` | 4 | R merged, D1, D3, D4 |
| B — Credential guarantees | `docs/plans/issue5-b-credential-guarantees.md` | 3 | R; B2 needs A1 |

Order:
1. **R.** Review it, then merge Stage 1 + R to `main`.
2. **C1, then A1.** C1 is offline and touches only `tests/`. A1 can follow
   once R is on `main`.
3. **A2, A3, A4 and B1, B2.** These run on one integration branch, `issue5/boundary`.
4. **C2, C3, then B3.** B3 is optional.
5. **C4, then merge `issue5/boundary` to `main`.** C4 is the human live run on
   the credential-free account, plus the reviewed capability-table commit.

Until step 5, `main` launches agents exactly as it does today. Only the
integration branch enforces the gate. See D3.

### Decisions (approved 2026-10-18)

- **D1 — Claiming prevention.**
  - Prevention of unauthorized Git writes may be claimed only with a
    *demonstrated* security boundary. It must separate agents from trusted Git
    credentials, from write authority and from engine state.
  - A separate macOS user, or an equivalent isolated environment, is
    acceptable.
  - Same-user operation stays supported as a **compatibility mode**. It never
    claims prevention where only detection is demonstrated.
- **D2 — Acknowledgement.**
  - An acknowledgement records an observed remote movement only. It never
    authorizes a push and never accepts a candidate.
  - Any later per-commit authorization, and any acceptance, are explicit and
    independently recorded.
- **D3 — `main`.**
  - Incomplete boundary enforcement stays off `main`. It is integrated only
    after the complete implementation passes the required independent live
    verification.
  - Ordinary existing workflows must not become unusable through fail-closed
    controls enabled too early.
- **D4 — Trusted state outside the agent's write authority.**
  - The trusted runner, the policy and the authorization state must be outside
    the agent's *effective* write authority.
  - Placing them outside the worktree is not enough when the agent runs under
    the same account and could still modify them.
- **D5 — Live testing.**
  - It runs under `sparring-agent`, with disposable remotes and synthetic
    credentials only.
  - Both positive and negative evidence are preserved.

Design requirements (approved 2026-10-18):

- **R-1. Execution paths are verified separately.**
  - Never assume all Claude and Codex execution paths share the same sandbox
    protection.
  - Shell tools, file tools, MCP processes, web tools, subagents and
    launch-time configuration are distinct paths. Each claim is per path and
    verified on the exact provider version and launch mode.
- **R-2. Enforcement is separate from evidence.**
  - Runtime enforcement is separate from verification evidence.
  - A verification record never authorizes execution on its own, and never
    allows a weaker boundary. Evidence only decides which guarantees may be
    *claimed*.

What these change in the drafts (applied in the commit after the drafts):

- **Plan A, enforcement versus evidence (R-2, D3).**
  - Enforcement is unconditional. The builder always applies the full boundary
    and refuses only when that boundary cannot be applied.
  - The capability table decides claims, not launches. The draft's "empty
    table refuses every launch" gate is removed.
- **Plan A, run modes and tables (D1, D4, R-1).**
  - A run records its execution mode: `same_user` (compatibility) or
    `isolated` (a separate trusted account).
  - Table rows are keyed per execution path.
- **Plan B (D1).** `prevented` requires the `isolated` mode with a verified
  boundary. `same_user` is capped at `detected_not_prevented`.
- **Plan R (D2).** An acknowledgement is never consulted by authorization or
  acceptance code. Authorizing a commit already on the remote is its own
  explicit CLI action.
- **Plan C (D5, R-1).** Scenarios are per execution path. The positive and
  negative evidence and the disable runs are all preserved.
- **New, not yet drafted: Plan D, cross-account isolated execution (D1, D4).**
  - In Plan D, the trusted engine, its policy, authorization state and Git
    credentials live under one account. Agents run under another account that
    cannot write them.
  - Without Plan D, no run can claim prevention. Plans R, A, B and C remain
    correct and useful without it.
  - **This is a blocker for closing issue #5 as "prevented", not for Plan R.**

### Other blockers

- **The bootstrap gate has F5's flaw.** `/Users/Shared/sparring-bootstrap/2-gate.sh`
  checks credentials with `git credential fill | grep password`, so a failing
  helper reads as "no credential". The recovery kit uses a stricter
  presence-only gate (see `docs/audits/issue5-recovery-kit/README.md`).
- **The frozen runner predates Stage 1.** It is `c61eaf8`, whose source equals
  `ea8e70d`. It still treats a candidate reachable on the remote as pushed, and
  its stage agents run with no provider boundary.
  - That is acceptable only because its remote is a local disposable bare
    repository, the account holds no Git credentials, and every result is
    independently reviewed.
  - A Plan R result is untrusted until that review.

- **Run Stage 1's full test suite in a credential-free environment.** It must
  run before R starts, and the counts must be recorded. No independent count
  exists.
- **Re-verify the Claude flag and setting semantics on the exact installed
  versions in C4.** These are `--setting-sources ""`, `sandbox.credentials.*`,
  `strictAllowlist`, `failIfUnavailable`, and `host:port` entries in
  `allowedDomains`. Until then they are unverified.

## 4. Retrospective: why Stage 2 did not converge

1. **The acceptance evidence could not be produced inside the loop.**
   - Stage 2 required real-provider sessions on a credential-free account.
   - The implementer itself ran inside a provider sandbox, and macOS refuses
     nested Seatbelt.
   - Rounds 2 to 5 each end "requires a real claude/codex on a credential-free
     macOS account, which this environment cannot run … not READY".
   - So no number of send-backs could reach READY. The plan should have made
     live evidence a human verification gate (v2 manifest `gates_before` /
     `completion_gates`), not stage acceptance.
2. **Contradictory constraints pushed the work toward self-certification.**
   - "Anything not proven is unsupported → refuse" plus "only live evidence
     proves" plus "the implementer cannot run live" left exactly one route to
     green: make the code certify itself.
   - Successive rounds did that: a conftest-injected record, then
     BLOCKED/ALLOWED parsing of model prose, then "no sentinel exists".
3. **Send-backs patched symptoms of a rejected design.**
   - The verification record was protected (round 4), then bound to the binary
     and the code (round 5), then to effects (round 5b).
   - It was never removed, because the review loop has no "design reset"
     outcome. A send-back implies "patch this".
4. **The stage was too broad.** It covered two providers, launch config,
   settings isolation, network policy, credential probing, the capability
   table, docs and a live harness. Each round found new issues in a different
   subsystem.
5. **The plan assumed a mechanism nobody had proved: an in-boundary probe for
   Claude without a model turn.** It should have been a spike, with "unsupported"
   as an acceptable outcome.
6. **Workflow lessons for Agent Sparring:**
   - Distinguish *blocked on environment or human* from *defective* as review
     outcomes.
   - Make plans declare live evidence as gates.
   - After N send-backs on the same finding class, stop and escalate to a human
     design review instead of another implementation round.
   - Have reviewers carry findings forward across rounds, so a recurring class
     is visible as one item.
