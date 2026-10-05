# Set up a project

[← Documentation index](README.md)


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

`sparring init-config` writes exactly that file for you (it never overwrites
an existing one), so the template lives in the engine and nothing else has to
keep a copy of the schema.

### Choosing a model and an effort level

Which **provider** runs a role is the project's decision, in `project.toml`
(above). Which **model** that provider uses, and how hard it thinks, is
*your* decision, and it is not a project setting: it is your own preference,
shared by every project, kept in one file outside every repository so
choosing a model while working in one repo is still the choice the moment
you switch to another that uses the same provider for the same role:

```sh
sparring set-config stage --model claude-opus-5-5 --effort high
sparring set-config sparring --model gpt-5.6-terra --effort xhigh

sparring set-config stage --model-default     # drop the preference; use the provider's
sparring set-config stage --effort-default    # own default again
```

Both fields are optional, and **leaving one out is not the same as writing a
default into it**. No preference means the engine passes no flag at all and
the provider CLI does whatever it normally does; the engine never guesses
which model that turns out to be.

`effort` is deliberately not a single engine-wide vocabulary. Each provider
is validated against what its own CLI accepts:

| Provider | Model flag | Effort | Accepted levels |
| --- | --- | --- | --- |
| `claude-cli` | `--model` | `--effort` | `low`, `medium`, `high`, `xhigh`, `max` |
| `codex-cli` | `--model` | `-c model_reasoning_effort=…` | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |

A level the resolved provider does not accept is a configuration error raised
before any provider process starts, rather than something translated into
"the nearest equivalent". This matters concretely for `claude-cli`: given an
unknown `--effort`, the CLI *warns and runs the turn anyway* at its default
effort, so a typo would otherwise buy a full-price turn at a level nobody
chose. `sparring set-config` refuses it before writing anything.

Model names are not enumerated as a validation boundary — both CLIs accept
free-form names and gain new ones without an engine release — but
`sparring model-choices` offers a suggestion list per role (Codex's own
catalog for `codex-cli`; a short list of exact ids the engine happens to know
for `claude-cli`), never a closed one:

```sh
sparring model-choices                 # every role
sparring model-choices --role stage    # just this role
sparring model-choices --json
```

A saved preference must be an exact model id, not an alias — `set-config`
refuses `opus`, `sonnet`, `fable` and the like as something *saved*, because
a preference is shown as the exact model it names and an alias would
silently change underneath it. A one-off CLI or environment override may
still use an alias; only what gets written down is held to the stricter
rule.

Every command takes overrides, and the precedence is the same everywhere:

```text
provider:        explicit CLI flag  >  SPARRING_* environment  >  .sparring/project.toml  >  the engine's own default
model / effort:  explicit CLI flag  >  SPARRING_* environment  >  your saved preference    >  the provider's own default
```

`run-stage` and `run-sparring` take `--model` / `--effort`; `run-loop`,
`run-plan` and `resume-plan` take `--stage-model` / `--stage-effort` and
`--sparring-model` / `--sparring-effort`. The independent reviewer is the
sparring role, so it uses your sparring preference.

**A provider session keeps one configuration from its first turn to its
last.** Before a role's first provider turn the engine records what it
resolved to (provider, model, effort and where each came from) in the
stage's `state.json`, under `agents`. Every later turn of that session
reuses it: SEND_BACK cycles, a `resume-plan` in a new process, the
finalization turn. A preference changed while a stage is under way therefore
**applies from the next stage** -- or from a *fresh session* for that role
(see [Resume, fresh session, reset](reference.md#resume-fresh-session-reset))
-- and never switches the model under an existing provider session. A
command-line or environment override that contradicts the recorded value
mid-session is refused, not ignored; drop it to continue, or start a fresh
session for that role. Resetting a stage starts it fresh, configuration
included.

To see what a turn would actually run with, and where each value came from:

```sh
sparring show-config            # human-readable
sparring show-config --json     # the same answer, machine-readable
```

The JSON form reports `provider`, `model`, `effort` and a `*_source` for each
(`cli`, `env`, `project`, `user`, `engine-default` or `provider-default`) per
role, plus the path of the `project.toml` it read and the path of your
preference file (`user_config_path`, with `user_config_exists`). It reports
only the resolved values — it never enumerates the environment and never
prints a credential. This is how the VS Code extension shows the effective
configuration, so that "this provider plus your preference plus an omitted
effort means X" is answered in one place. Each role also reports
`provider_choices`: every provider implemented for that role, with its own
effort vocabulary, so a UI can offer the real choice without keeping a list
of providers that goes stale.

Your preference file lives at:

```text
$SPARRING_USER_CONFIG                                (when set: the file itself)
$XDG_CONFIG_HOME/agent-sparring/config.toml           (when XDG_CONFIG_HOME is set)
%APPDATA%\agent-sparring\config.toml                  (Windows)
~/.config/agent-sparring/config.toml                  (macOS, Linux)
```

both `show-config` and `check-config` print the path in effect. It is never
inside a repository, so a change here never dirties a working tree and
`git status` never sees it. Writes are validated before anything reaches
disk and are atomic; a request that matches what is already stored writes
nothing, and clearing the last preference for a role/provider removes the
file. Nothing outside `sparring set-config` (or a UI that shells out to it)
ever writes to it.

### Trying a different model without touching anything saved

For a one-off — try a different model for one run, without changing the
preference you actually want saved — set the same three fields in the
environment instead:

```sh
export SPARRING_SPARRING_MODEL=gpt-6-astra    # just this shell
SPARRING_STAGE_EFFORT=high sparring resume-plan docs/plans/active/x.md
```

The names are mechanical — `SPARRING_` plus the role plus the field, upper
case — for the two roles `STAGE` and `SPARRING` and the three fields
`PROVIDER`, `MODEL` and `EFFORT`. (Yes, the sparring role's variables read
`SPARRING_SPARRING_*`.) Per role and per field, so setting a model leaves the
effort alone. `PROVIDER` overrides the project's `project.toml`; `MODEL` and
`EFFORT` override your saved preference — neither ever touches the file it
overrides.

They are validated exactly as a saved value is: an effort level the resolved
provider does not accept, or a provider not implemented for that role, is a
configuration error raised before any provider process starts, and the
message names the variable that set it. A variable that is present but empty
is an error too, rather than a silent fall back — unset it to stop
overriding. Unlike a saved preference, an environment override *may* use a
Claude alias such as `opus` — the stricter "exact id only" rule applies to
what gets written down, not to a one-off.

Because an environment override leaves nothing behind, the stage's own
`activity.jsonl` is the only record that it happened. Every run writes an
`agents.resolved` event per role, naming the provider, model and effort it
resolved and which layer supplied each. `sparring usage` reads it back — see
[What a stage actually used](stages.md#what-a-stage-actually-used).

### Changing the file itself

For the times something else needs to change `project.toml`'s provider — the
VS Code cockpit's inline selectors, a script — there is one typed command,
the same `set-config` used for your model/effort preference above:

```sh
sparring set-config stage --provider claude-cli
```

`--provider` is the only field this command writes to `project.toml`; there
is deliberately **no** way to set an arbitrary key to an arbitrary value —
nothing here can reach `[repo].root`, add a key the parser would later
reject, or smuggle a fragment into a provider's argv. `--model` / `--effort`
(and their `-default` clears) go to your preference file instead, under the
role and the provider in effect for it — or `--for-provider`, when you want
to prepare a preference for a provider you are not using yet, or you are
running outside any project at all.

What it guarantees, for both destinations:

- the edit is validated the way a run resolves it — an effort the provider
  does not accept, an unsaveable Claude alias, a provider not implemented for
  the role, an unknown `--for-provider` — *before* anything is written, so a
  rejected change leaves every file exactly as it was;
- a `project.toml` that is already malformed is refused, not replaced: a
  broken file is someone's work in progress, and overwriting it is not a
  repair;
- comments, key order and every unrelated setting in `project.toml` survive;
  only the line asked about changes;
- both writes are atomic, so a reader sees the whole old file or the whole
  new one and a failure part-way leaves the original intact;
- a request a file already satisfies writes nothing at all, which makes a
  repeated or double-clicked change harmless — and clearing the last
  preference for a role/provider removes your preference file's entry
  entirely (and the file itself, if nothing else is left in it);
- a project with no `project.toml` gets the same template `init-config`
  writes, and then the provider change.

`set-config --json` reports the resulting effective configuration in exactly
the shape `show-config --json` uses. A change applies to the **next**
provider turn: it never reconfigures or restarts a turn already running, and
it does not touch recorded run state or any prompt already captured.

### Fixable setup problems: `fix-config`

`show-config --json` also reports `setup_problems`, two kinds. Workflow-state
directories (`.sparring/stages/`, `.sparring/plans/`, `.sparring/intake/`)
that git can still see, each carrying the `.gitignore` line that fixes it and
the engine's full refusal text — when they cannot be determined the payload
has `setup_error` instead. And, kind `obsolete-agent-setting`: a `model` or
`effort` key still sitting under `[agents.<role>]` in `project.toml` from
before this feature — parsed, reported, and **never resolved**. It blocks
work: `check-config` reports it and exits non-zero, and every command that
would start a provider turn (`run-plan`, `resume-plan`, `run-loop`,
`run-stage`, `run-sparring`, `ask`, `prepare-plan`) refuses before any run
state is written or any provider starts — running anyway would use a
different model than the file appears to choose.

`sparring fix-config` repairs both kinds in one pass: it appends exactly the
missing lines to the repository's `.gitignore`, and removes exactly the
obsolete `model`/`effort` keys from `project.toml` — nothing else in either
file changes, a second run writes nothing, and a rule elsewhere that still
overrides the `.gitignore` fix is reported as an error. It does **not** copy
the removed values into your preferences; which repository's old value
should win is your choice, not a deterministic repair — set it afterwards
with `sparring set-config`. Commit `.gitignore` and `project.toml` afterwards;
the engine never stages or commits either.

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
# agent-sparring working artifacts: per-stage files, plan-run position and
# plan intake (.sparring/project.toml and .sparring/PROJECT.md stay tracked)
.sparring/stages/
.sparring/plans/
.sparring/intake/
```

This is not optional bookkeeping. `freeze-candidate` requires a clean
worktree, and it exempts only *the current stage's own* artifact files — a
deliberately narrow exemption, so that "everything under `.sparring/`" is
never waved through. Leave `.sparring/stages/` tracked and your first stage
will still work; the **second** one will fail its freeze with a dirty-worktree
error naming an unrelated stage's files, which is a confusing way to learn
this. `.sparring/plans/` is worse: the plan runner rewrites it at every stage
boundary, so leaving it visible breaks *every* freeze part-way through a run,
after the provider turns are already spent.

So check it before you start, not after:

```sh
sparring check-config
```

It prints `stage artifacts git-ignored: yes` / `plan-run state git-ignored:
yes` / `plan intake git-ignored: yes` and exits non-zero, naming the missing
`.gitignore` line, if any of them is visible to git. (`.sparring/intake/` is
written only by `prepare-plan`/`approve-plan`, never mid-run, but visible
intake files make the next freeze refuse the worktree just the same;
`prepare-plan` refuses to start without it.) `run-plan` makes the same check up front and refuses to start
otherwise; the check is never skippable.
