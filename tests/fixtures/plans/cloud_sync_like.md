# Cloud sync rollout

Status: approved for implementation. Any production action needs the owner's go-ahead.

## Global invariants

- Sync identity is the server row id; never derive it from display names.
- Every stage keeps the offline queue backwards compatible.
- Deletions stay soft until Stage 5 closes them out.

## Repositories

The primary repository is `app`. The optional sibling `web` changes only if
Stage 3B finds that the web client parses the old payload.

---

## Stage 1 — Schema

Add the sync columns and the server row id.

Acceptance: migrations apply cleanly on a copy of the current schema.

## Stage 2 — Sync engine

Implement the push/pull engine behind a disabled flag.

Acceptance: engine tests pass with the flag off and on.

## Stage 3B — Client download path

Depends on Stage 3A's queue format.

If the web client parses the old payload, update `web` in the same candidate;
otherwise `web` stays unchanged.

Acceptance: downloads round-trip through the new queue.

## Stage 3A — Client upload path

Write uploads through the new queue format.

Acceptance: uploads survive an app restart mid-queue.

## Stage 4 — Conflict resolution

Resolve concurrent edits by server row id and revision.

Acceptance: the conflict matrix tests pass.

### Review barrier

Before Stage 5 starts, an independent review of Stages 1–4 confirms that sync
identity never depends on display names.

## Stage 5 — Cleanup

Remove the legacy sync path and hard-delete soft-deleted rows.

Acceptance: no legacy sync code remains.

## Canaries

If Stage 2's sync metrics regress, run a canary on 5% of devices before
Stage 3A starts.

## History

- First draft written after the sync incident review meeting.
