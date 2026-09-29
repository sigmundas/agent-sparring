# NEEDS_YOU is a structured gate, not a paragraph

[← Documentation index](README.md)


A `NEEDS_YOU` verdict must say, machine-readably, what a human has to finish:

```json
{"action": "NEEDS_YOU",
 "summary": "...", "needs_you_reason": "...", "findings": "...", "deferred": "...",
 "human_gate": {
   "category": "DEVICE_MANUAL_CHECK",
   "title": "A pre-activation desktop must survive a feed containing snapshot v2",
   "checks": [
     {"id": "pre-activation-desktop-v2-feed",
      "instruction": "Run a desktop build with the feature switched off, sync it against an account whose feed already contains a v2 reference, and open the reference library.",
      "pass_criteria": "The library loads, the v2 reference appears with its legacy values, and the sync log reports no error or data loss.",
      "source": "docs/plans/active/foo.md > Stage 3D — Snapshot v2"}
   ]}}
```

`category` is one of `PRODUCT_PREFERENCE`, `UI_VISUAL_CHECK`,
`DEVICE_MANUAL_CHECK`, `EXTERNAL_CONDITION`, `SCOPE_EXPANSION`, `OTHER`.
`checks` must be non-empty, ids must be unique and are stable across turns so
a recorded Pass/Fail/Blocked survives the reviewer restating its gate. The
gate is **required** for `NEEDS_YOU` and must be `null` for every other
action; a `NEEDS_YOU` without one is an unusable verdict, not a stage whose
human checks have to be guessed at.

The scope rule matters more than the shape, and it is what the reviewer
prompt spends its words on: **only work that must be completed before this
stage may become READY**. Production deployment after acceptance, release-
owner actions, rollout gates that stay closed until a later release decision,
future monitoring, and checks the read-only reviewer merely could not run
itself are all real and all belong in `findings`/`deferred` — not in the
gate. Listing them as checks asks a human to "pass" work acceptance does not
depend on.

`sparring.md` carries the gate twice: as readable prose, and as canonical
JSON behind an `<!-- human-gate:v1 -->` marker, which is what a UI should
render its controls from. `record-sparring --human-gate-file <path>` supplies
one for a verdict reached manually or in a web chat.

### A check id is the question; `instance_id` is the asking

A recorded gate also carries an `instance_id` the engine mints when it writes
the verdict:

```json
{"category": "PRODUCT_PREFERENCE", "title": "...",
 "instance_id": "5b222f6b6ea7416885adb22d217caa47",
 "checks": [{"id": "batch-attachment-scope", "…": "…"}]}
```

A reviewer may re-issue a check it has already asked, under the same id,
because the answer it got was not enough — "a Pass alone does not identify
which option you chose". The check id is still right: it is the same subject,
and giving it a new id would throw that away. But the *asking* is new, and a
human's answer belongs to one asking rather than to the id forever. A
consumer that keys recorded evidence on the check id alone marks the
re-issued check as already answered, which leaves it impossible to answer.

So: same `instance_id` **and** check id means recorded evidence answers this
gate. A new `instance_id` means earlier answers are history — still shown,
still in `notes.md`, never rewritten — and the check is open again.

The engine mints the id at the moment it records the verdict, never the
reviewing agent: an agent restating its own gate has every incentive to
repeat the id it saw in its prompt, so whatever it supplies in that field is
discarded. The value is opaque — nothing orders it or reads meaning into it,
and two gates are the same asking exactly when the strings are equal.

`instance_id` is absent from a gate recorded before this existed. A consumer
should treat "cannot prove which asking this evidence answered" as *not
proven*: show the prior evidence as history and let the check be answered
again, rather than claiming a match a matching check id does not establish.
