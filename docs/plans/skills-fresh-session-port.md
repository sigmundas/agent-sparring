# Skills: port fresh-session guidance and tidy the start-plan skill

Repository: `agent-sparring`. Status: approved. Branch
`feature/run-plan-compiler` after the accepted Stage 4 of
`docs/plans/run-plan-compiler.md` (`4db67bf`). Documentation only.

## Stage 1 — Fresh-session guidance in sparring-run, start-plan accuracy

### Port (reconcile, do not paste)

Commit `6e7e05c` on `feature/fresh-agent-session` added fresh-session
guidance to `skills/sparring-run/SKILL.md`, but it never reached `main` or
this branch, and Stage 4 has since rewritten that file. Read it with
`git show 6e7e05c -- skills/sparring-run/SKILL.md` and carry its substance
into the current file in the current structure:

- ordinary resume vs fresh-session resume (`--fresh-sparrer` /
  `--fresh-stage-agent`, optional `--fresh-reason`, role-scoped model /
  effort) vs `reset-stage` (ask first); one provider per role today, so a
  different provider is refused;
- the engine decides whose turn runs next (`next_turn`); `--next-turn` only
  for a legacy stage the engine refused to derive, chosen by the person;
- provider pauses recorded as `provider_pause` (`session-unresumable`,
  `provider-unavailable`, `has_session`), printed retry commands, never
  edited.

Verify every flag and value against the engine's current `--help` and
`docs/reference.md`; drop anything the engine does not do.

### Stage 4 accuracy fixes

- Each `--answer` rerun may spend a further read-only preparation turn; say
  so where the first preparation notice is given.
- Mention `--repository-branch NAME=BRANCH` for a sibling on a non-default
  branch, and that an undeclared repository is refused.
- State plainly that the confirmation token binds every execution input
  shown (models, effort, permission mode, executables, max send-back
  cycles, push, sparring dir, repositories, answers) and that any change
  requires a fresh dry run.
- State that plan gates still pause the run after launch; relay their
  NEEDS_YOU as today.
- `docs/README.md`: update the "(experimental)" label on Plan intake so it
  matches `start-plan` being the default path (or explain what remains
  experimental).

No code changes. Check with `git diff --check`.
