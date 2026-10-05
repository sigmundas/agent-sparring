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
  project uses. Currently only `"supabase"`: Supabase CLI migration files
  and `supabase migration list`'s `Local | Remote` table. A file is a
  migration when its name matches the CLI's own rule, `^([0-9]+)_(.*)\.sql$`
  (`supabase migration new` writes a 14-digit UTC timestamp, but the CLI
  applies any all-digit version), and versions are ordered as strings, the
  way `supabase db push` compares them. Only files directly in `directory`
  count, because the CLI does not read subdirectories. One legacy CLI rule is
  not mirrored: the CLI skips a *first* file named `<14 digits>_init.sql`
  that is older than `20211209000000`, but this engine counts it.
- `directory` -- where migration files live, relative to the repository root.
  An absolute path, or one that climbs out of the repository with `..`, is a
  configuration error. The same applies to `deferred_registry`.
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
input is refused with a clear error rather than guessed at. `--observed-at`
(default: now) must carry a timezone (`2026-10-01T10:00:00Z` or
`...+02:00`). A timestamp without one is refused. So is one more than 5
minutes (the clock-skew tolerance) in the future: it would be picked as the
latest snapshot over every later recording, and its age would never grow
stale. A snapshot
records what was found; it never compares itself to anything else or decides
what the drift means -- that is `check-migrations`'s job, done fresh each
time from the *latest* recorded snapshot (the one with the latest
`observed_at`). If any snapshot file in the history directory cannot be read,
fails validation, has no timezone, or is dated beyond the tolerance in the
future, `check-migrations` reports that file by name and exits `1` rather
than skipping it or trusting it. Fix or remove the file named. A snapshot
dated slightly ahead of the clock, within the tolerance, is reported with an
age of 0, never a negative age.

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
root), it is read from git at the same ref as the branch's migration files
(`--branch-ref`, default `HEAD`), never from the working tree. An uncommitted
edit to it has no effect. It is read for exactly these fields:

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

The `file` field is checked against the version. An entry whose `file` name
encodes a different version (or is not a migration file name) makes the
registry unusable (exit `1`). A committed migration for that version whose
file name differs from the pinned `file` is `deferred_tampered`, the same as
a hash mismatch.

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
| `duplicate_version` | More than one file on the branch or `main_ref` has this version. None of them is picked: no tamper hash, modified-applied check or retimestamp proposal is computed against either file. |

Problems with the files themselves are listed separately, under
`file_problems` (each has `kind`, `version`, `paths`, `refs`, `message`,
`blocking`):

| Kind | Meaning | Blocking |
| --- | --- | --- |
| `duplicate_version` | Two or more files share one version; every file is named. | yes |
| `unrecognised_migration_file` | A file in the migrations directory that is not a migration by the adapter's rule, or a file in a subdirectory. | only for `.sql` files, which look like migrations the tool will skip; other files (a README, `.gitkeep`) are listed as notes |

For each `migration_order_stale` version, the report includes a
`propose_retimestamp` proposal: an old and a new version. The new version is
a valid 14-digit UTC timestamp (`YYYYMMDDHHMMSS`, carrying over minute, day,
month and year boundaries). It sorts strictly after the recorded head *and*
after every other known version (branch, `main_ref`, applied, deferred), and
the stale versions keep their order relative to each other. The proposal
also lists every other file that mentions the old version (found via
`git grep`, excluding `.sparring` and the migration file itself). For each `remote_only`
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
| `2` | The report has findings: stale, tampered, remote-only, modified or duplicate versions, a blocking file problem, or an unknown or stale snapshot. |
| `1` | The answer could not be determined at all: not configured (no `project.toml` or no `[migrations]`), a bad `main_ref`, an unreadable or inconsistent registry, an unusable snapshot file, and the like. |

With `--json`, an exit-`1` result is `{"version": 1, "configured": ...,
"error": "..."}`. `configured` is `false` only when migration-order detection
is not configured. For a project without `[migrations]`, both migration
commands exit `1` with that message and write nothing. Every other command's
output is unchanged.

## Not a database migration: stage `state.json` format changes

Everything above is about *database* migrations. The engine's own stage
`state.json` also gains fields over time, and needs no migration step: new
fields are optional, so an existing file reads back unchanged and stays
byte-identical until the engine next writes it. The fresh-agent-session work
adds:

| Field | Absent means | Written when |
| --- | --- | --- |
| `next_turn`, `next_turn_candidate`, `next_turn_source` | state from before the marker; the first resume derives it once from engine records, or refuses as ambiguous and takes an explicit choice (`manual`) | the loop advances the marker, a verdict is recorded, or a derived/manual marker is persisted |
| `next_turn_candidate.tracked_digest`, `next_turn_candidate.untracked` | a candidate recorded before the split; a `finalization` resume over it allows the untracked-removal recovery only on same HEAD, same sibling HEADs and a clean worktree, and says so | a candidate is captured |
| `next_turn_resolution` | no ambiguous legacy state was resolved (or it was resolved before this record existed) | a derived or manual marker is first persisted; never rewritten |
| `untracked_produced` | no successful implementation turn since the loop last started one (or one recorded before the field existed: the reviewer-start check then uses the handoff's list, exactly -- a collapsed `dir/` entry covers nothing) | a successful implementation turn finishes; cleared when the loop starts an implementation turn. With `next_turn = stage` it means that turn's review is owed |
| `untracked_baseline` | a stage started before it was recorded; only the last turn's produced paths count | the stage's first implementation turn records `base_sha` |
| `sessions` | the recorded session ids and `agents` pins are generation 1 (`started_at` unknown) | a role's configuration is pinned or its session id is recorded, or a fresh session is started |

The plan-run state (`.sparring/plans/<run>.json`) likewise gains one
optional, engine-written field; a file without it reads back and is rewritten
byte-identically:

| Field | Absent means | Written when |
| --- | --- | --- |
| `provider_pause` | the run is not paused by a classified provider failure | a provider failure pauses the run: `{kind, role, stage_id, has_session, recorded_at}`, `kind` ∈ `session-unresumable` \| `provider-unavailable`, `role` ∈ `stage` \| `sparring`; removed when the run next starts running or pauses/fails for another reason (client contract and semantics: [reference](reference.md#resume-fresh-session-reset)) |

It is descriptive only; resume routing never reads it. Standalone `run-loop`
has no plan-run state and is unchanged.

Resume, fresh session and `reset-stage` are different operations; see
[Resume, fresh session, reset](reference.md#resume-fresh-session-reset).
