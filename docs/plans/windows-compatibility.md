# Native Windows compatibility for the Agent Sparring engine

Status: deferred, optional future-contributor handoff. Not required for the
initial colleague sharing effort; no Windows work is scheduled by this plan.

Primary repository: `agent-sparring`. Proposed work, not approval to launch a
run or providers. Run here, separately from extension work. Read AGENTS.md.
Extension implementation is `../agent-sparring-vscode/docs/plans/windows-extension.md`.

Review baseline (2026-10-07): `49d2e45`; 103 focused engine tests and live
extension/engine manifest parity passed on macOS. Native Windows is untested.
`concurrency.py` imports `fcntl` unconditionally; preference and plan-obligation
locks silently skip locking when it is unavailable. The provider streaming
runner already uses argument arrays and threads rather than POSIX selectors.

## Stage 1 — Preserve all engine locking guarantees on Windows

**Repository:** `agent-sparring`. **Depends on:** none.

**Goal:** importing the CLI works on Windows and concurrent worktree, preference
and obligation writers retain the same exclusion guarantees as on POSIX.

**Scope:** `src/agent_sparring/concurrency.py`, `user_config.py`,
`plan_obligations.py`, a shared internal file-lock module if needed,
`tests/test_concurrency.py`, `tests/test_user_preferences.py`,
`tests/test_deferred_across_slices.py` and focused new locking tests.

- Add an OS-backed Windows lock backend (standard-library `msvcrt` or another
  justified implementation), retaining `fcntl.flock` on POSIX. Do not replace
  locking with PID-file staleness, existence checks or unlocked fallbacks.
- Preserve immediate refusal for a second worktree writer and serialized
  read-modify-write updates for preferences and the shared obligation ledger.
  A blocking Windows backend must wait intentionally; it must not depend on
  `msvcrt`'s finite implicit retry behavior.
- Keep a stable lock file outside the replaced data file, initialize the lock
  byte if required, lock/unlock the same byte range, and release handles on
  exceptions and process death. Releasing must not unlink another owner's lock.
- Ensure worktree identity maps aliases and case variants correctly on Windows
  without collapsing genuinely distinct worktrees. Different worktrees must
  remain independently runnable.
- Remove the two Windows no-lock branches. Keep atomic data replacement and
  acquire the lock before reading the value to update.
- Test actual contention between subprocesses, independent worktrees, failure
  cleanup, process-death release, alias identity and concurrent updates that
  preserve both writers' changes. A mocked Windows module on macOS is only
  supplementary coverage.

**Acceptance criteria:** Windows Python can import `agent_sparring.cli` and run
`sparring --help`; native Windows and POSIX subprocess tests prove exclusion,
release and no lost preference/ledger updates. Run focused pytest files first,
then the engine suite because locking is shared orchestration. If Windows is
unavailable, record the implementation as awaiting Windows validation.

**Non-goals:** weaken independent review, branch/remote checks, candidate
identity or atomicity; edit the extension; run live user plans. Follow AGENTS.md
and preserve unrelated edits. Use fake providers and temporary repositories.

## Stage 2 — Verify native executable, path and subprocess transport

**Repository:** `agent-sparring`. **Depends on:** Stage 1.

**Goal:** Windows Python launches supported provider distributions and transports
plan paths and prompts correctly without introducing shell interpretation.

**Scope:** `src/agent_sparring/providers/subprocess_runner.py`,
`providers/claude_cli.py`, `providers/codex_cli.py`, relevant executable
resolution call sites located by scoped search, `tests/test_subprocess_runner.py`,
`tests/test_providers_claude_cli.py`, `tests/test_providers_codex_cli.py` and
focused platform tests. Change only failures demonstrated by these checks.

- Audit how native `.exe` versus npm `.cmd`/`.bat` launchers are resolved on
  Windows. Test the installed distribution shapes; do not assume PATHEXT
  discovery makes a launcher safe for a no-shell subprocess invocation.
- Prefer a real executable or verified interpreter-plus-script argument
  array. If a distribution cannot be invoked safely, refuse it explicitly and
  document a supported route; do not add `shell=True` to carry free-text prompts.
- Verify spaces, Unicode, backslashes, trailing separators, quotes, newlines,
  long prompts and shell metacharacters reach a fake provider byte for byte
  after UTF-8 encoding. Use paths on distinct drives where relevant.
- Verify streaming stdout/stderr, nonzero failures, timeouts, process reaping,
  stdin handling and a descendant holding pipes open on native Windows.
  Keep failures classified through the existing provider contract.
- Preserve the fixed read-only Codex sandbox arguments for fresh and resumed
  reviewer sessions. Never substitute a prompt-only read-only promise.

**Acceptance criteria:** native Windows subprocess tests pass with supported
launcher shapes and hostile arguments, without executing shell metacharacters;
POSIX tests still pass. Record supported provider installation routes. Run
focused pytest files and broaden for shared runner changes. This stage does
not establish the real provider's sandbox enforcement; Stage 3 defines that
verification gate.

**Non-goals:** provider feature expansion, unsafe shell wrappers, killing user
processes, live provider turns, changes to engine-owned acceptance/routing.
Read AGENTS.md; preserve unrelated edits and keep extension work separate.

## Stage 3 — Add Windows regression coverage and publish honest support docs

**Repository:** `agent-sparring`. **Depends on:** Stages 1 and 2.

**Goal:** keep Windows compatibility reproducible and distinguish tested engine
behavior from provider behavior requiring a real Windows machine.

**Scope:** new CI configuration under `.github/workflows/` if appropriate,
platform-specific fixture helpers, affected tests, `QUICKSTART.md`, README,
`docs/setup.md` and relevant provider documentation discovered from `docs/README.md`.

- Add native Windows and POSIX CI coverage using supported Python versions.
  Keep mandatory Windows coverage for locks, preferences, obligation updates,
  CLI import/configuration, provider transport and fake-provider plan execution.
  Fix test assumptions about `/bin/sh`, executable permission bits, symlinks,
  locale, line endings and signals where demonstrated; no blanket Windows skip.
- Exercise a complete temporary plan with fake implementation/review providers,
  a temporary local bare remote, pause/evidence/resume and acceptance of the
  exact verified commit. Assert duplicate writers are refused. Never use the
  user's remote, repository or provider account for CI.
- Run the full suite on both OS families. If some tests legitimately require
  POSIX behavior, scope skips to those behaviors and report the coverage gap.
- Write PowerShell installation and run examples. State tested Python/Git and
  provider versions, supported launcher shapes, remote/push requirements and
  the distinction between native Windows and WSL.
- Define a final native Windows provider check: an explicitly authorized test
  in a disposable repository must prove Codex denies candidate-file writes
  on both fresh and resumed review sessions; verify supported Claude launch
  and one pause/resume round. Until that check passes, describe native provider
  integration as awaiting verification even if fake-provider CI is green.

**Acceptance criteria:** native Windows CI proves engine concurrency and
fake-provider plan lifecycle; POSIX suite and manifest parity still pass.
Documentation provides commands verified on Windows. Required live provider
evidence is recorded as passed or explicitly pending; no unqualified native
Windows support claim without it.

**Human check:** only with separate explicit authorization and a Windows
environment/account, perform the disposable real-provider checks above and
record versions, commands and outcomes. If unavailable, report Blocked for
this check; do not fabricate evidence or relax the sandbox.

**Non-goals:** publish/install into users' environments or run existing plans;
edit the extension; rewrite engine state to bypass gates. Follow AGENTS.md.
