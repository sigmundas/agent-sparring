# agent-sparring

Pairs a stage implementation agent with an independent sparring agent, so the
two can exchange corrections without a human acting as courier.

The design, principles and a short build history live in
[docs/design.md](docs/design.md).

The tool is generic: it contains no knowledge of any particular project. A
project supplies its own machine-readable config and its own prose knowledge.

## Install

```sh
pip install -e /path/to/agent-sparring    # provides the `sparring` command
```

## Develop

```sh
uv sync            # creates .venv with the package and pytest
uv run pytest -q   # runs the test suite
```

## Set up a project

Create two files in the repository you want to work on:

```text
<repo>/.sparring/project.toml    machine-readable config
<repo>/.sparring/PROJECT.md      durable project knowledge, injected as agent context
```

A minimal `project.toml`:

```toml
project = "my-repo"

[repo]
root = "."

[agents.stage]
provider = "claude-cli"

[agents.sparring]
provider = "codex-cli"

[stage]
self_check = false
```

`PROJECT.md` is prose the workflow never interprets: stack, directory map,
test and build commands, conventions, product invariants, device/manual
checks, and — worth the effort — the **current baseline test results**. Verify
every fact against the working tree instead of copying it forward. Without a
recorded baseline, an agent told to make the tests pass will either chase
pre-existing failures out of scope or report its own stage as broken.

Then check what the tool sees:

```sh
sparring --sparring-dir <repo>/.sparring check-config
```

`--sparring-dir` is global and goes before the subcommand; it defaults to
`.sparring`, so from inside the repository the subcommand alone is enough.
The commands that touch Git — `handoff`, `run-*`, `freeze-candidate`,
`accept-candidate` — additionally take `--repo-root`.

### Git hygiene — do this during setup, not on your second stage

Track the two configuration files; ignore the per-stage working artifacts:

```gitignore
# agent-sparring working artifacts: per-stage files and plan-run position
# (.sparring/project.toml and .sparring/PROJECT.md stay tracked)
.sparring/stages/
.sparring/plans/
```

This is not optional bookkeeping. `freeze-candidate` requires a clean
worktree, and it exempts only *the current stage's own* artifact files — a
deliberately narrow exemption, so that "everything under `.sparring/`" is
never waved through. Leave `.sparring/stages/` tracked and your first stage
will still work; the **second** one will fail its freeze with a dirty-worktree
error naming an unrelated stage's files, which is a confusing way to learn
this. `run-plan` checks up front that `.sparring/plans/` is ignored and
refuses to start otherwise.

## Run a stage

```sh
cd <repo>
sparring new-stage <stage-id>    # writes brief.md, notes.md, state.json
# edit .sparring/stages/<stage-id>/brief.md — scope and goal for this stage

sparring run-loop <stage-id> --repo-root . --expected-branch feature/x
```

`run-loop` runs stage agent → handoff → sparring agent, and repeats on
`SEND_BACK` with the *same* two sessions, so corrections continue a
conversation rather than restarting one. It stops on `READY`, `NEEDS_YOU` or
`ESCALATE`.

While it runs, `.sparring/stages/<stage-id>/activity.jsonl` receives one
line per observable event (turn started, file edited, command finished,
verdict, ...). It is telemetry for watching a stage, never an input to the
workflow; see the "Activity stream" section of `docs/design.md`.

Then, for a candidate you are satisfied with:

```sh
sparring freeze-candidate <stage-id> --repo-root . --expected-branch feature/x
sparring accept-candidate <stage-id> --repo-root . --expected-branch feature/x
```

Freezing pins an exact pushed SHA; acceptance names that SHA and goes stale if
HEAD moves. Freezing without accepting is a legitimate resting state — it is
what you want when a candidate is correct but still needs a check you cannot
run, such as device QA.

The individual steps are also available on their own: `run-stage`,
`run-sparring`, `handoff` (add `--self-contained` for a packet a sparrer
without repository access can read), and `record-sparring` for a verdict
reached manually or in a web chat. `sparring --help` lists everything.

## Run a whole plan

A reviewed plan with several bounded stages can run from stage to stage
without you launching each one:

```text
reviewed plan
    -> sparring run-plan docs/plans/foo.md --repo-root . --expected-branch feature/x
    -> Stage 1: run-loop … READY -> freeze -> accept
    -> Stage 2: fresh sessions … READY -> freeze -> accept
    -> Stage 3: … NEEDS_YOU -> plan pauses, prints what is required
you do the check / make the decision
    -> sparring resume-plan docs/plans/foo.md --repo-root . --expected-branch feature/x \
           --evidence "Tested on a Pixel 7: resume after 24h works."
    -> Stage 3 resumes with the SAME two sessions … READY -> freeze -> accept
    -> Stage 4 … until the next human gate or the end of the plan
```

`run-plan` takes the same provider flags as `run-loop`. It stops for
`NEEDS_YOU`, `ESCALATE`, a provider/integrity failure, a freeze or accept
refusal, the SEND_BACK runaway limit, and the end of the plan; ordinary
`READY` is accepted automatically through the existing gate, at the exact
pushed SHA, with no human confirmation. The gate itself is unchanged: if it
refuses, the plan stops and nothing is substituted.

Each planned stage gets its own `.sparring/stages/<stage-id>/activity.jsonl`,
with the same provider stream a direct `run-loop` produces (the adapters are
rebuilt per stage so the stream follows the stage), plus `plan.*` lines
saying when the runner entered, accepted, paused on, failed at or completed
a stage, and when evidence was recorded. That is telemetry for watching the
run; the plan's position lives in `.sparring/plans/` and is never read from
`activity.jsonl`.

### Marking stages in a plan

Stages are level-2 headings numbered 1..N in document order:

```markdown
## Stage 1 — Foundation
...
## Stage 2 — Incremental rendering
...
## Stage 3 — Prefetch and enrichment
...
```

A hyphen, en dash or colon works as the separator too. Everything from the
heading to the next `#`/`##` heading is that stage's section, and becomes
the stage's `brief.md` verbatim; deeper headings belong to the stage. The
stage id is derived deterministically as
`<plan key>-stage-<n>-<slugified title>`, where the plan key is the plan's
file stem plus a short hash of its path (for example `foo-3f9a2c1b`), so two
plans with the same headings never share stage artifacts.
Nothing is inferred from prose: a plan with no such headings, a heading that
starts with `## Stage` but does not fit, a numbering gap or duplicate, or an
empty section is refused before any agent runs. A reviewed plan written
another way needs a small edit to mark its stages; that is deliberate.

### Pause and resume

Run position lives in `.sparring/plans/<plan key>.json`: the plan, a digest
of its stage sections, the branch, the current stage and a status
(`running`/`paused`/`complete`). Candidate SHAs, sessions and acceptance
stay in each stage's own `state.json`.

A fresh `run-plan` means fresh stages. It refuses if a run is already
recorded or if any of the plan's stage directories already exist, and names
what an earlier or abandoned run left behind. Nothing is deleted for you: to
genuinely start over, remove the run-state file *and* those stage directories
deliberately, otherwise old sessions or an old `accepted` status would be
inherited.

`--evidence` is appended to the current stage's `notes.md` under
`## Human evidence`; you can also edit that section by hand. Both agents see
it on the next turn. The same stage then resumes: an answer never creates a
new stage, and if no code changed the same SHA is sparred again and can be
accepted. The digest of the plan's stage sections is re-checked on
`resume-plan`, before every acceptance and before every advance, so a stage
section edited during a run, even by the implementation agent, even
committed, pauses the plan instead of being accepted or executed; prose
outside the stage sections may change freely.

After `ESCALATE`, spar the stage elsewhere with the printed handoff/packet
commands, then either accept it by hand (`freeze-candidate`,
`accept-candidate`) and `resume-plan` — an already-accepted current stage is
advanced past — or `resume-plan --evidence` with the external verdict.

## Who owns which artifact

Every file a run reads or writes has exactly one authority. Paths are
relative to the project's `.sparring/` directory, except the plan document
itself, which is an ordinary repository file.

| Artifact | Owner | Provider-writable? | Lifetime | Purpose |
| --- | --- | --- | --- | --- |
| the plan document (`docs/plans/.../<plan>.md`) | human / repository | no | immutable for the lifetime of a managed run | the run's execution definition |
| `PROJECT.md` | human / repository | no | edited between runs by a person | project context embedded in every prompt |
| `project.toml` | human / repository | no | edited between runs by a person | provider selection and engine configuration |
| `plans/<run>.json` | engine | no | rewritten on every position/status change | the run's position, expected branch and plan digest |
| `stages/<stage>/brief.md` | engine (a person, for a hand-written stage) | no | generated from the plan section, or hand-written before execution; then immutable | what the stage is reviewed against — the plan section verbatim in a managed run |
| `stages/<stage>/notes.md` | engine (and a person editing by hand) | no | skeleton at creation, then appended to by section | a human's recorded answer or check results (`## Human evidence`) |
| `stages/<stage>/handoff.md` | engine | no | regenerated in full by every implementation turn | that turn's claims, git identity and evidence, for the sparrer |
| `stages/<stage>/sparring.md` | engine | no | rewritten in full by every sparring exchange | the latest verdict, rendered from the structured routing result |
| `stages/<stage>/state.json` | engine | no | rewritten on every lifecycle change | status, candidate identity, provider session ids |
| `stages/<stage>/activity.jsonl` | engine | no | append-only, never read by orchestration | observational telemetry only |

**No artifact is provider-writable, including `notes.md`.** Despite its
`## Implementation notes` template heading, nothing asks a provider to open
it: an implementation turn's claims, evidence and deferred checks reach the
sparrer because the engine captures that turn's *result* into `handoff.md`,
and a human's answer reaches both agents because `resume-plan --evidence`
records it under `## Human evidence`. A provider's designated output is its
own reply, which the engine records; the files are how the engine keeps it.
`src/agent_sparring/artifact_ownership.py` is the production declaration the
prompts and tests are built on, and is also where the sentence the providers
are told it in lives, so the stage and sparring prompts cannot drift apart.
The table above is the human-readable summary of that declaration; nothing
generates or checks it, so the two are kept consistent by hand.

**The plan is immutable while a run executes.** The engine digests the plan's
stage sections at run start and re-reads the document and re-checks that
digest on resume, before accepting a candidate, and before advancing. An
edited plan therefore stops the run rather than becoming its new definition,
and so does a plan that can no longer be parsed as the one it started as —
appending an `# Implementation record` whose own `## Stage 1` heading follows
the plan's `Stage 1..3` is exactly that case. Nothing is ever reverted for
you: restore the document, or deliberately start the run over.

A consuming project's agent instructions may well tell agents to keep the
active plan updated with their progress, which is right everywhere except
inside a managed run — so the restriction travels with the managed prompt
rather than depending on the project's own wording.
