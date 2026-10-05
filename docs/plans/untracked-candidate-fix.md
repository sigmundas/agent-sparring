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
2. **Narrow recovery.** In `resolve_finalization`, when the recorded
   reviewed candidate is `kind: worktree`, the current HEAD equals the
   reviewed `head_sha`, sibling pins are unchanged, nothing tracked is
   modified, and the only difference from the reviewed content is that
   untracked paths are gone (the current state is a clean commit), re-record
   `next_turn = sparring` over that clean commit so the reviewer rules on
   exactly it before acceptance. Never route to implementation, never
   accept directly. Requires recording enough in the candidate to tell
   "untracked paths removed" from any other change (e.g. tracked-content
   digest separately from the untracked set); keep older markers without
   that detail working: for them, accept this recovery only when HEAD
   matches and the worktree is now clean, and say so in the message.
3. **Provenance.** A manual `next_turn` choice keeps `manual` provenance
   (for example `next_turn_source` plus a persisted record of the manual
   choice and when it was made) through the loop's routine marker writes in
   the same cycle; later engine transitions may set `engine`, but the
   manual choice must remain inspectable in state.
4. Error messages for both refusals name the concrete paths and the next
   command.

### Tests

- Reviewer start with an unrelated untracked file → refused, nothing
  recorded; with an untracked file the implementation created → proceeds.
- Regression of the incident: READY over `kind: worktree` whose only
  uncommitted content is unrelated untracked files → files removed →
  resume re-records `sparring` over the clean commit → reviewer READY →
  normal acceptance; no implementation turn, no direct acceptance.
- The same with any tracked modification or moved HEAD → still refused.
- Manual `--next-turn stage` provenance survives the cycle-start write.
- Existing finalization, committed-READY, legacy and fresh-session tests
  keep passing.

Update `docs/reference.md` (candidate identity and finalization) and
`docs/migrations.md`. Run the full suite.
