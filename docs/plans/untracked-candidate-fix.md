# Unrelated untracked files must not become the reviewed candidate

Repository: `agent-sparring`. Status: approved. Base: `main` at `2e7f893`.
Found while recovering run `run-plan-compiler-ee44f11a-ab9cf1a3` Stage 1
with a fresh reviewer.

## Stage 1 — Clean-candidate rule, narrow recovery, manual next_turn provenance

### Incident

The reviewer said READY over HEAD `7b3ce26` while the worktree also held
unrelated untracked, unignored files (`.DS_Store`, `.sparring/.DS_Store`,
`docs/plans/backend-selection.local.md`). `next_turn.capture_candidate`
counts untracked files as uncommitted content, so the READY candidate was
recorded as `kind: worktree` with those files in its content digest. The
bounded commit turn correctly refused to commit them and acceptance was
refused. Resuming repeats the same commit turn; removing or ignoring the
files makes the repository a clean commit whose digest differs from the
reviewed one, which `resolve_finalization` refuses. The only named way out,
`reset-stage`, would discard the attempt. There is no supported path.

Separately, the explicit `--next-turn stage` choice was recorded as
`manual` but the loop's routine `stage` write at the next cycle start
overwrote `next_turn_source` with `engine`, losing the provenance.

### Required behaviour

1. **Prevention.** Before a reviewer turn starts (managed and standalone),
   if the worktree contains untracked, non-ignored paths that the stage's
   implementation did not produce, refuse clearly, naming the paths and
   saying to remove, commit or ignore them; record nothing and change no
   state. Decide "did not produce" from engine-owned records only (e.g. the
   paths present before the stage's implementation turns, or the handoff's
   recorded changed paths); if that cannot be decided reliably, refuse on
   any untracked non-ignored path that is not in the handoff's changed
   paths. Untracked files the implementation legitimately created remain
   part of the candidate as today.
   Do not silently exclude untracked files from candidate identity: the
   reviewer must see exactly what the identity says it sees.
2. **Narrow recovery.** In `resolve_finalization`, re-record
   `next_turn = sparring` over the clean commit only when ALL hold:
   the reviewed candidate was `kind: worktree`; current HEAD is exactly the
   reviewed `head_sha`; sibling pins are unchanged; tracked content is
   unchanged; the only difference is the disappearance of previously
   reviewed untracked paths; no new untracked paths appeared; the current
   state is a clean commit. The reviewer then rules on exactly that commit
   before acceptance. Never route to implementation, never accept directly.
   Record enough in the candidate to decide this (e.g. tracked-content
   digest separately from the set of untracked paths and their digests).
   Older markers without that detail: allow this recovery only when HEAD
   matches the reviewed `head_sha`, siblings match, and the worktree is now
   clean; say so in the message.
3. **Provenance.** Keep the live `next_turn_source` meaning "who wrote the
   current marker" (routine transitions may set `engine`), and persist a
   separate immutable record of how an ambiguous legacy state was resolved
   (`next_turn_resolution`: chosen turn, source `manual` | `derived`,
   recorded_at, and the reason/refusal it answered). Routine marker writes
   never alter or drop it; it stays inspectable for history and clients.
4. Error messages for both refusals name the concrete paths and the next
   command.

### Tests

- Reviewer start with an unrelated untracked file → refused, nothing
  recorded; with an untracked file the implementation created → proceeds.
- Regression of the incident: READY over `kind: worktree` whose only
  uncommitted content is unrelated untracked files → files removed →
  resume re-records `sparring` over the clean commit → reviewer READY →
  normal acceptance; no implementation turn, no direct acceptance.
- Still refused: any tracked change, a different HEAD, a new untracked
  file, or a modified (not removed) previously reviewed untracked file.
- Manual `--next-turn stage` resolution record survives the cycle-start
  write and later transitions; `next_turn_source` still reflects the
  current marker's writer.

Keep it small: no unrelated cleanup.
- Existing finalization, committed-READY, legacy and fresh-session tests
  keep passing.

Update `docs/reference.md` (candidate identity and finalization) and
`docs/migrations.md`. Run the full suite.
