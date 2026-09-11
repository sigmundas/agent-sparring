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
# agent-sparring per-stage working artifacts
# (.sparring/project.toml and .sparring/PROJECT.md stay tracked)
.sparring/stages/
```

This is not optional bookkeeping. `freeze-candidate` requires a clean
worktree, and it exempts only *the current stage's own* artifact files — a
deliberately narrow exemption, so that "everything under `.sparring/`" is
never waved through. Leave `.sparring/stages/` tracked and your first stage
will still work; the **second** one will fail its freeze with a dirty-worktree
error naming an unrelated stage's files, which is a confusing way to learn
this.

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
