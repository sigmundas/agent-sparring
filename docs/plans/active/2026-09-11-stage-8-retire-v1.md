# Stage 8 — Retire V1

Date: 2026-09-11
Generic tool: `agent-sparring`, branch `feature/sparring-v2`, base
`ebd698896824aa2edbb54d8bd08b8f734f52273c`
Retired system: the Sporely V1 staged workflow at
`~/Documents/Code/sporely/.sparring`, its own Git repository
(`sigmundas/sporely-agent-workflow`)

V1 was retired outright. No compatibility framework was built, no V1 code was
migrated into `agent-sparring`, and no old command routes silently to V2.

---

## Caller inventory (taken before any deletion)

**Live executable entry points.**

| Caller | What it did |
| --- | --- |
| `~/.claude/skills/sporely-{stage,handoff,sparring,review-result}/SKILL.md` | symlinks into V1 `skills/*/SKILL.md` — the four Claude skills |
| `~/.agents/skills/sporely-{stage,handoff,sparring,review-result}` | symlinks into the same V1 skill directories, for Codex |
| `~/.claude/commands/stage.md` | `/stage`, a thin alias delegating to `sporely-stage` |
| `~/.claude/agents/sporely-implementer.md:10,27-28` | told implementers to follow `sporely-stage` and to route acceptance to `sporely-sparring` |

**Checked-in guidance carrying live V1 instructions.**

- `sporely/AGENTS.md:13-14` — `.sparring/` paths "may be read or written for the
  staged workflow".
- `sporely/CLAUDE.md:78` — local `.sparring` prompts/state as reviewer
  references.
- `sporely-web/docs/plans/active/2026-09-09-finds-smooth-pagination.md:7` —
  status field: "ready for staged implementation with `sporely-sparring`".

**Historical-only, deliberately left alone.** Roughly thirty `.sparring/prompts/…`
path citations inside dated plan entries under `sporely-py/docs/plans/active/`
(`2026-09-03-reference-system-ui-unification.md`,
`2026-08-23-cloud-sync-extraction.md`, `2026-09-07-conflict-resolution-spore-mosaic.md`).
These are records of work already done, not instructions; rewriting them would
falsify the record. `~/.codex/config.toml:56` holds a `[projects."…/.sparring"]`
trust entry — inert, harmless, left in place.

**No callers found** in repository source code, CI, Git hooks, `settings.json`
hooks, or any shell script. Nothing breaks at runtime from the removal.

---

## Deleted from V1

All in the V1 repository, so all recoverable via `git show <commit>:<path>`.

- **Orchestration and machinery** — `scripts/select_stage.py` (817),
  `scripts/workflow_transitions.py` (1790), `scripts/review_result.py` (160),
  `scripts/archive_stage.py` (66), `scripts/run_handoff.py` (61),
  `handoff.py` (943). This is the whole of the transition engine, the
  review-result/history routing, the ancillary classification, the
  attempt/reopen-review machinery, the front-matter parsing, and the V1 handoff
  compatibility modes.
- **Tests** — all seven files including `test_workflow_transitions.py` (1989)
  and the phrase-based `test_workflow_contracts.py` (156), plus fixtures.
- **Skills** — `sporely-stage`, `sporely-handoff`, `sporely-sparring`,
  `sporely-review-result`: four `SKILL.md` (1267 lines total) and four
  `agents/openai.yaml`.
- **Stale prose** — `IMPLEMENTATION_PROGRESS.md` (341),
  `prompts/PROMPT_SCHEMA.md` (447), `web_review.md` (73, a spent packet),
  `sporely-project-knowledge.md` (377).
- **`README.md`** replaced with a 32-line retirement notice.

`sporely-project-knowledge.md` is the one judgement call worth naming. It mixed
genuine Sporely orientation (repo map, the stale-worktree hazard, invariants,
testing philosophy) with V1 review protocol, and its stated authority anchor was
the now-deleted `sporely-sparring` skill — a guaranteed drift hazard. Its
durable half belongs in a per-repository `PROJECT.md` written fresh against the
working tree, which is what Stage 7 did for `sporely-web`. The stale-worktree
hazard, the only piece with no other home, is already stated in
`sporely/AGENTS.md`.

## Retained from V1, and why

- `prompts/` and `handoffs/` — inert Markdown and JSON kept purely as
  historical evidence, because the sporely-py plan entries above cite those
  paths and the citations should keep resolving. Nothing reads or writes them.
- Nothing else. No V1 utility was migrated: every primitive worth having
  (Git identity and status gathering, thin and self-contained handoff packets,
  pushed-candidate verification) already exists in V2 from Stages 2 and 6, so
  there was no V1 copy with a current demonstrated caller.

## Live references migrated

- Deleted the four `~/.claude/skills/sporely-*` directories and the four
  `~/.agents/skills/sporely-*` symlinks. No dangling symlink remains under
  either tree.
- `~/.claude/commands/stage.md` rewritten (9 lines → 16) as an explicit
  retirement notice. It names V2 and then **stops**; it is instructed not to
  translate the request into V2 commands, so it is a tombstone rather than a
  shim. `/stage` was the one entry point with enough muscle memory to deserve
  an explanation instead of an unknown-command error.
- `~/.claude/agents/sporely-implementer.md` — three edits removing the
  dependency on the deleted skills: stage ownership stated directly, the brief
  located at `.sparring/stages/<stage-id>/brief.md`, "do not archive stage
  prompts" restated as "do not freeze or accept candidates", and acceptance
  described as an independent sparring pass rather than a named skill.
- `sporely/AGENTS.md` — the `.sparring/` bullet now says the parent's
  `.sparring/` is retired and non-executable, and points at `agent-sparring`.
- `sporely/CLAUDE.md` — the reviewer-reference line now points at the
  repository's own `.sparring/stages/…`, plus a short new "Staged workflow
  tooling" section recording per-repository V2 status and that dated
  `.sparring/prompts/…` citations are history, not instructions.
- `sporely-web/docs/plans/active/2026-09-09-finds-smooth-pagination.md` — status
  field now names `agent-sparring` (`sparring run-loop`).

## Added to agent-sparring

One file: `README.md`, 116 lines. It covers install, the two project-local
files, `check-config`, the run/freeze/accept commands — and the Stage 7 setup
trap as its own headed subsection: track `project.toml` and `PROJECT.md`,
gitignore `.sparring/stages/`, because the freeze gate exempts only the current
stage's own artifacts and a project therefore hits this on its *second* stage
with an error naming an unrelated stage's files.

No production code changed. No new configuration, state, or workflow concept
was introduced.

## Size

| | Files | Lines |
| --- | --- | --- |
| V1 tracked before | 42 | 12,868 |
| V1 tracked after | 15 | 1,798 |
| Deleted | 27 | 11,102 |
| Added (V1 retirement notice) | — | 32 |
| Added (`agent-sparring/README.md`) | 1 | 116 |
| Changed elsewhere | 4 | +27 / −8 |

Net: roughly **11,100 lines removed, 175 added**.

## Verification

1. **Re-searched for live V1 references** across `sporely/{AGENTS,CLAUDE}.md`,
   all five Sporely repositories, `~/.claude/{agents,commands,CLAUDE.md,settings*.json}`,
   `~/.agents/skills`. Every remaining hit is either one of the retirement
   notices written above or an unrelated `_preview_result` identifier in
   `sporely-py/ui`. No dangling symlink under `~/.claude` or `~/.agents` other
   than the pre-existing, unrelated `~/.claude/debug/latest`.
2. **sporely-web V2 config still works** —
   `sparring --sparring-dir …/sporely-web/.sparring check-config` prints
   `project: sporely-web`, both providers, `default_sparring_mode: local-auto`,
   `PROJECT.md present: True`.
3. **Full agent-sparring suite** — `python -m pytest -q` → **248 passed**,
   unchanged from the Stage 7 count.
4. **`git diff --check`** clean in both `agent-sparring` and the V1 repository.
5. **Cheap V2 check against sporely-web** — the `check-config` run in (2) is
   that check; no agent-invoking command was run, to avoid disturbing the
   frozen stage.
6. **Frozen Stage 3 candidate untouched** — `d20aef3a335…` still resolves to
   tree `98e878e…` and is still reachable from `feature/sparring-v2-pilot`; its
   `state.json` still reads `"status": "frozen"` with the same base SHA and both
   session ids; all five artifact files retain their pre-session mtimes.

## Still requires manual cleanup

- Four **untracked** paths remain in the V1 repository:
  `prompts/sporely-web/stage-finds-incremental-pagination-render.md`,
  `prompts/sporely-web/stage-finds-server-search-pagination.review.json`,
  `prompts/sporely-web/completed/stage-finds-server-search-pagination.md`, and
  `prompts/sporely-web/completed/review-history/`. They are V1 review-result
  output that was never committed, so deleting them is unrecoverable. Left in
  place for the user to decide.
- `~/Documents/Code/sporely` is **not a Git repository**, so the `AGENTS.md` and
  `CLAUDE.md` edits there are not version-controlled and could not be committed.
- `sporely-py`, `sporely-landing` and `sporely-admin` have no V2 configuration.
  Each needs its own `.sparring/project.toml` and `.sparring/PROJECT.md` written
  fresh, with a verified test baseline, before it can run a stage.

## Follow-up observations (not Stage 8 work, and not implemented)

Carried forward from the Stage 7 record; none of these blocked retirement and
none of them turned out to be required by the deletion work.

- `[commands]` in `project.toml` is parsed, validated and printed, and consumed
  by no workflow code. The commands reach the agents as prose in `PROJECT.md`.
- `[sparring].default_mode` is likewise parsed and unused.
- `ClaudeCliAdapter.timeout_seconds` defaults to `None` with no CLI flag, so a
  wedged provider would hang an unattended loop without bound.
- `NEEDS_YOU` for a product/preference decision (case 4) and a genuine
  `ESCALATE` verdict (case 7) are still unexercised. Neither depended on V1.
- Cosmetic: `sparring.md` prints every non-selected action section as
  "(not applicable)", and the selected action section repeats only the one-line
  summary, so the real content always lives further up under
  "Finding / discussion".
