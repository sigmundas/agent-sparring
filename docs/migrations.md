# Migration-order detection (Stage A)

[← Documentation index](README.md)

This is **detection only**. Nothing described here runs a database migration,
touches credentials, or invokes a migration CLI. Everything it knows about
what is actually applied to a target comes from an explicit, saved snapshot
that a person records by hand from output they already captured -- there is
no live probe, and there never will be one added quietly: a `[migrations]`
table that sets a `probe` key (or any other key this page does not document)
is refused as a configuration error at load time.

## Opting in: `[migrations]` in `project.toml`

```toml
[migrations]
adapter = "supabase"
directory = "supabase/migrations"
main_ref = "origin/main"
target = "production"                          # optional, default shown
target_ref = "origin/main"                     # optional
deferred_registry = "supabase/deploy-exceptions.json"  # optional
max_observation_age_minutes = 60               # optional, default shown
```

The table is entirely optional. When it is absent, nothing changes: no new
files, no new output, no new setup problems -- a project written before this
feature existed behaves identically. When it is present, its keys are parsed
closed: an unknown key (including a hypothetical `probe`) is a configuration
error, the same as an unknown `[agents.stage]` key is today.

- `adapter` -- which migration tool's file-naming and CLI output this
  project uses. Currently only `"supabase"` (Supabase CLI migration files,
  named `<14-digit timestamp>_name.sql`, and `supabase migration list`'s
  `Local | Remote` table).
- `directory` -- where migration files live, relative to the repository root.
- `main_ref` -- the git ref this engine treats as "what is actually on the
  trunk" (e.g. `origin/main`). Local migration files are always read from
  git (this ref, and the branch being checked), never from the working-tree
  filesystem and never from a Stage-B slot/stage declaration.
- `target` -- a display label for the thing `main_ref` is deployed to
  (default `"production"`).
- `target_ref`, `deferred_registry`, `max_observation_age_minutes` -- see
  below.

## Recording what a target actually has: snapshots

`sparring check-migrations` never asks a database or a CLI what is applied.
Instead, someone runs the real command by hand (e.g. `supabase migration
list`), and records the captured output:

```sh
supabase migration list > /tmp/migration-list.txt
sparring record-migration-history --file /tmp/migration-list.txt
```

This parses the file with the configured adapter and writes a new, versioned
snapshot under `<git-common-dir>/agent-sparring/migrations/history/` --
alongside this engine's other workflow state, shared by every linked worktree
of the same clone, and never inside the tracked working tree. Unparseable
input is refused with a clear error rather than guessed at. A snapshot
records what was found; it never compares itself to anything else or decides
what the drift means -- that is `check-migrations`'s job, done fresh each
time from the *latest* recorded snapshot.

Each snapshot is a small JSON document (schema version `1`):

```json
{
  "version": 1,
  "adapter": "supabase",
  "target_ref": "origin/main",
  "observed_at": "2026-09-30T18:30:00Z",
  "source": "recorded",
  "raw_sha256": "...",
  "applied": ["20260910100000", "20260925160000"],
  "head": "20260925160000"
}
```

## The deferred-migration registry

Some projects deliberately defer a migration -- it is committed, but not yet
applied to the target, on purpose, for a documented reason -- and pin the
exact file and its hash so the gap cannot be silently widened. If
`deferred_registry` names such a file (a path relative to the repository
root), it is read for exactly these fields:

```json
{
  "deferredMigrations": [
    {
      "version": "20260914090000",
      "file": "20260914090000_extend_reference_snapshots_to_version_2.sql",
      "sha256": "...",
      "reason": "..."
    }
  ]
}
```

Any other field in the file (a project-ref, a `doc` pointer, ...) is ignored:
this format is owned by the project that writes it, and reading only the
fields this engine actually uses means an extra field there never breaks
`check-migrations`.

## `sparring check-migrations [--json]`

Classifies every migration version the current branch, `main_ref`, and the
latest recorded snapshot know about, into one of:

| Class | Meaning |
| --- | --- |
| `applied` | The target has it. Immutable: never proposed for edit or retiming. Flagged `applied_migration_modified` if the local file differs from `main_ref`'s copy. |
| `unapplied` | Local-only, newer than the recorded head -- still deployable in order. |
| `deferred` | In the registry, with a matching hash. Not drift. |
| `deferred_tampered` | In the registry, but the file's hash no longer matches. |
| `migration_order_stale` | Local-only, not deferred, and *older* than the recorded head -- a plain `db push` would now refuse or apply it out of order. |
| `remote_only` | The target has it, but neither `main_ref` nor the branch does (an out-of-band/emergency apply not yet reconciled in the repository). Sets the repo-level `remote_history_not_reconciled` flag. |

For each `migration_order_stale` version, the report includes a
`propose_retimestamp` proposal: an old and a new version (strictly after the
recorded head, preserving the stale versions' relative order to each other),
plus every other file that mentions the old version (found via `git grep`,
excluding `.sparring` and the migration file itself). For each `remote_only`
version, it includes a `propose_reconciliation_stage` proposal. **Proposals
are data only** -- this engine never renames, edits, or runs anything, and
`migration repair` is never suggested anywhere in its output.

The repo-level summary also reports the recorded head, the snapshot's age,
`production_history_unknown` when nothing has been recorded yet, and a
staleness warning once the snapshot is older than `max_observation_age_minutes`.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Clean: nothing to report. |
| `2` | The report has findings (stale/tampered/remote-only/modified, or an unknown/stale snapshot). |
| `1` | The answer could not be determined at all: missing `[migrations]`, a bad `main_ref`, an unreadable registry, and the like. |
