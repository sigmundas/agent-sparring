# Push to review (issue #5, Plan P) — stub

**Status.** This is a stub. It records the requirements agreed on 2026-10-23.
It is not executable yet. It gets stages, and its own security review, only
after Plan L (`docs/plans/issue5-l-local-first-engine.md`) is integrated.

**Purpose.** Plan P is an optional, explicitly human-triggered way to publish
one exact local commit to a remote branch, so an external reviewer (for
example ChatGPT in a web chat) can read it.
- It is never part of implementation, review, acceptance or finish.
- It never implies acceptance.

## Requirements

- **Explicit and exact.**
  - A preview writes nothing. It shows:
    - the repository path;
    - the commit SHA and subject;
    - the remote name and URL, resolved from a trusted snapshot of user and
      system configuration only;
    - the destination ref and its current tip;
    - the outcome: a new branch, a fast-forward, or a refusal.
  - A confirmation token binds all of these. The push re-reads everything,
    and refuses with `publish_preview_changed` if anything differs.
  - In VS Code it needs an explicit click, and it is labelled "not
    acceptance".
- **Never rewrites remote history.**
  - No `+` refspec, no `--force`, no `--force-with-lease` (which can permit
    non-fast-forward updates), no `--mirror`, no delete and no tags.
  - The destination must be either:
    - absent (a new branch); or
    - at exactly the previewed tip, with that tip present locally and an
      ancestor of the SHA (an ordinary fast-forward).
  - Divergence refuses with `publish_not_fast_forward`, and suggests a new
    review branch.
  - After the push, the porcelain output must show exactly the expected old
    tip → SHA, compared as full SHAs. Otherwise it reports `publish_race`.
- **New review branches by default.**
  - The default destination is `review/<run-key>/<stage>-<sha12>`, or
    `review/<branch>-<sha12>` outside a run.
  - Version 1 refuses the default branch outright.
- **Hardened transport.** It uses Plan R's retained isolated remote primitive
  (`engine_git.run_remote`) and its form allowlist. Planted `pushInsteadOf`,
  `pushurl`, `insteadOf`, `sshCommand` and credential-helper keys in the
  repository's own configuration never redirect the push, and never run.
- **Separate from acceptance.**
  - A publication receipt goes under
    `<git-common-dir>/agent-sparring/publications/`.
  - No acceptance, review or finish code reads or imports it.
  - Publishing changes no stage, run or acceptance state.
- **Tests carried over from Plan L.** These are the `pushInsteadOf` and
  hidden-destination regressions, which Plan L Stage 1 repaired. Plan P also
  takes over the "no remote write without an intact snapshot" property of
  the original `FinishWithoutThePinTests`.
- **Not a boundary.** Until Plan D, a same-user agent can run any `sparring`
  command or `git push` itself. The token protects against accidents and
  drift, not against a malicious agent.
