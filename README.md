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

`sparring init-config` writes exactly that file for you (it never overwrites
an existing one), so the template lives in the engine and nothing else has to
keep a copy of the schema.

### Choosing a model and an effort level

Each role may also pin the model and the reasoning/effort level its provider
runs at:

```toml
[agents.stage]
provider = "claude-cli"
model = "opus"
effort = "high"

[agents.sparring]
provider = "codex-cli"
model = "gpt-5.6-terra"
effort = "xhigh"
```

Both fields are optional, and **leaving one out is not the same as writing a
default into it**. An omitted `model` or `effort` means the engine passes no
flag at all and the provider CLI does whatever it normally does; the engine
never guesses which model that turns out to be.

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
chose. Model names are not enumerated — both CLIs accept free-form names and
aliases, and gain new ones without an engine release.

Every command takes overrides, and the precedence is the same everywhere:

```text
explicit CLI flag  >  SPARRING_* environment  >  .sparring/project.toml  >  the provider's own default
```

`run-stage` and `run-sparring` take `--model` / `--effort`; `run-loop`,
`run-plan` and `resume-plan` take `--stage-model` / `--stage-effort` and
`--sparring-model` / `--sparring-effort`. The independent reviewer is the
sparring role, so it uses `[agents.sparring]`.

Configuration is resolved when a provider turn is launched, and re-read for
each stage of a plan run. Editing `project.toml` therefore affects the *next*
turn; it never reconfigures or restarts a provider process already running,
and it does not disturb session resume — the session id belongs to the
provider and is recorded in stage state, not on an adapter object.

To see what a turn would actually run with, and where each value came from:

```sh
sparring show-config            # human-readable
sparring show-config --json     # the same answer, machine-readable
```

The JSON form reports `provider`, `model`, `effort` and a `*_source` for each
(`cli`, `env`, `project`, `engine-default` or `provider-default`) per role,
plus the path of the `project.toml` it read. It reports only the resolved
values — it never enumerates the environment and never prints a credential. This is how the VS Code extension shows
the effective configuration, so that "this provider plus this file plus an
omitted model means X" is answered in one place. Each role also reports
`provider_choices`: every provider implemented for that role, with its own
effort vocabulary, so a UI can offer the real choice without keeping a list
of providers that goes stale.

### Trying a different model without touching the repository

`project.toml` is tracked in your repository. That is right for the project's
agreed configuration, but it makes a one-off experiment awkward: editing the
file dirties the working tree, and finalization treats any dirty path that is
not a stage artifact as candidate content, so a model change ends up staged
and committed alongside the work it was only supposed to observe.

Set the same three fields in the environment instead. They leave no trace in
the repository:

```sh
export SPARRING_SPARRING_MODEL=gpt-6-astra    # just this shell
SPARRING_STAGE_EFFORT=high sparring resume-plan docs/plans/active/x.md
```

The names are mechanical — `SPARRING_` plus the role plus the field, upper
case — for the two roles `STAGE` and `SPARRING` and the three fields
`PROVIDER`, `MODEL` and `EFFORT`. (Yes, the sparring role's variables read
`SPARRING_SPARRING_*`.) Per role and per field, so setting a model leaves the
project's effort alone.

They are validated exactly as the file's values are: an effort level the
resolved provider does not accept, or a provider not implemented for that
role, is a configuration error raised before any provider process starts, and
the message names the variable that set it. A variable that is present but
empty is an error too, rather than a silent fall back to the file — unset it
to stop overriding.

Because an environment override leaves nothing behind, the stage's own
`activity.jsonl` is the only record that it happened. Every run writes an
`agents.resolved` event per role, naming the provider, model and effort it
resolved and which layer supplied each. `sparring usage` reads it back — see
[What a stage actually used](#what-a-stage-actually-used).

### Changing the file itself

For the times something else needs to change `project.toml` — the VS Code
cockpit's inline selectors, a script — there is one typed command:

```sh
sparring set-config stage --model opus --effort high
sparring set-config sparring --model gpt-5.6-terra --effort xhigh

sparring set-config stage --model-default     # drop the override; use the provider's
sparring set-config stage --effort-default    # own default again
```

The role is `stage` or `sparring` and the fields are `--provider`, `--model`
and `--effort`. There is deliberately **no** way to set an arbitrary key to an
arbitrary value: nothing here can reach `[repo].root`, add a key the parser
would later reject, or smuggle a fragment into a provider's argv.

What it guarantees:

- the edit is validated the way a run resolves it — an effort the provider
  does not accept, a provider not implemented for the role — *before* anything
  is written, so a rejected change leaves the file exactly as it was;
- a file that is already malformed is refused, not replaced: a broken file is
  someone's work in progress, and overwriting it is not a repair;
- comments, key order and every unrelated setting survive; only the line asked
  about changes;
- the write is atomic, so a reader sees the whole old file or the whole new
  one and a failure part-way leaves the original intact;
- a request the file already satisfies writes nothing at all, which makes a
  repeated or double-clicked change harmless;
- a project with no `project.toml` gets the same template `init-config`
  writes, and then the change.

Nothing is ever silently discarded. If changing a role's provider would leave
an effort already in the file unusable, the command says so and stops rather
than resetting it for you — set the provider and the effort together in one
invocation if that is what you meant.

`set-config --json` reports the resulting effective configuration in exactly
the shape `show-config --json` uses. A change applies to the **next** provider
turn: it never reconfigures or restarts a turn already running, and it does
not touch recorded run state or any prompt already captured.

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

## Run a stage

```sh
cd <repo>
sparring new-stage <stage-id>    # writes brief.md, notes.md, state.json
# edit .sparring/stages/<stage-id>/brief.md — scope and goal for this stage
# or: sparring new-stage <stage-id> --brief-file scope.md   # that Markdown becomes brief.md verbatim

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

One of those events is `provider.usage`: what a provider said about its own
budget — tokens in/out, its cumulative total, how full the context window
is, the size of that window, and the account's rate-limit windows as
percentages. Every field is optional, and each one is written **only when
that provider's own output states it**. Codex reports all of them; the
Claude CLI reports no rate limits, so those are simply absent for it.

`context_used_tokens` and `total_tokens` are different facts and only the
first is a share of `context_window`. Both CLIs re-send the conversation on
every request, so the cumulative total passes the window several times over
in an ordinary session; the occupancy is the tokens the model was holding on
its latest request, cached prompt included. For Claude that means summing
`input_tokens` with the two cache fields — `input_tokens` alone is the
newest slice of the prompt and reports a 200k-token session as eighteen
tokens. Subagent messages are skipped: a `Task` subagent is a separate,
much smaller conversation, and letting one land would make the figure
collapse whenever work was delegated.

Codex's `--json` stream carries neither its context window nor its rate
limits (verified against codex-cli 0.153.4). Those are read, for the thread
the engine itself started, from the `token_count` record in Codex's own
rollout file under `CODEX_HOME`. Only the numbers of that one record type
are read; the transcript in the same file is not touched, and a missing or
changed file means "not reported" rather than an error.

That absence is load-bearing. Nothing infers a context window from a model
name and nothing defaults a percentage to zero, because a denominator
guessed from `claude-opus-5` would be an invention presented as a
measurement — and the two models behind that name do not share one. A
reader showing these numbers must render "not stated" differently from
"zero".

### What a stage actually used

`activity.jsonl` holds the facts, but it holds them as one JSON object per
line, interleaved with every tool call the providers made. `sparring usage`
reads them back as the short answer:

```sh
sparring usage                      # every stage, oldest first
sparring usage <stage-id>           # one stage
sparring usage <stage-id> --json    # the same records, machine-readable
```

For each role it reports the provider, model and effort the turn ran with
and **where each was set** (`cli`, `env`, `project`, `engine-default` or
`provider-default`), then the token totals the providers reported, then one
row per provider turn: when it started, how long it took, whether it resumed
an existing session, and how it ended (`finished`, `failed`, or the verdict's
action).

Two distinctions are kept rather than smoothed over. The model the engine
*asked for* and the model the provider *said it ran* are separate fields, so
a disagreement between them is visible instead of hidden behind one number.
And a duration the engine measured is printed plainly, while one derived
from a start and end timestamp — all a log written before the engine
measured turns can offer — is prefixed with `~`.

The report is a reader, not an authority: it decides nothing, and a missing,
truncated or partly corrupt log produces a partial report rather than an
error.

### Asking the reviewer

A `NEEDS_YOU` gate asks for your judgement. Until you can see what the
reviewer saw, you cannot exercise it — and the reviewer's evidence and
reasoning live in its own provider thread, where the gate can cite them but
nothing can show them to you. `sparring ask` is the way back into that
thread:

```sh
sparring ask <stage-id> --message "Show me the evidence for 53482 -> 39ZCL."
sparring ask <stage-id> --check-id review-nortaxa-53482-bridge \
    --message "If I pass this, what changes and what happens next?"
```

It **resumes the recorded sparring session**, so the answer comes from the
reviewer that wrote the verdict, quoting the evidence it actually used —
not a fresh reviewer reconstructing a plausible rationale from artifacts.
With `--check-id` the named check is quoted into the prompt verbatim from
the recorded gate; an id the gate does not ask is refused, listing the ones
it does.

It changes nothing. No verdict, no `state.json`, no `sparring.md`, no plan
run. A recorded verdict still changes only when a real sparring turn writes
a new one, and the prompt tells the reviewer so: if you show it something
that changes its mind, it says what it would conclude instead rather than
claiming to have revised anything. Read-only is verified rather than
trusted — the worktree is fingerprinted before and after the turn, the same
check an ordinary sparring turn makes — and the turn holds the worktree
lock, so asking while a provider turn is in flight is refused rather than
answered against a tree being edited.

Two costs are worth knowing, because both are real:

- the exchange **enters the reviewer's thread**, so a later sparring turn
  has seen it. That is deliberate — a reviewer that will not reconsider
  when shown something it missed is not much of a reviewer — but it means
  you can argue one out of a finding, which narrows the independence the
  sparring role otherwise has;
- it **spends the reviewer's context window**, which is shared with the
  review itself.

`sparring usage` reports both: a dialogue turn appears in the turn table
with outcome `asked`, and the role's token totals include it.

Every exchange is appended to the stage's `dialogue.jsonl` with the check
and gate instance it belongs to, and the prompt is captured under
`prompts/` like any other turn's.

### What each agent was actually told

Every turn writes the exact prompt it handed to its provider into the
stage's own `prompts/` directory, at the moment it handed it over:

```text
.sparring/stages/<stage-id>/prompts/
    0001-stage-original.md
    0002-sparrer-original.md
    0003-stage-resume.md
    index.jsonl
```

Nothing there is ever overwritten. That matters more than it sounds: a
prompt is assembled from files that keep changing — `sparring.md` is
rewritten every exchange, `handoff.md` is regenerated by every stage turn —
so re-rendering a prompt afterwards produces a plausible prompt rather than
the one that was sent. `run-stage --dry-run` still prints what the *next*
turn would be given the stage as it stands right now; it is not a record of
a turn that already ran, and while a first turn is still in flight it will
report that turn as a resume, because the provider's session id is recorded
as soon as the provider issues it.

`index.jsonl` is append-only and purely derived — one line per capture, with
the turn's role and kind (`original`, `resume`, `evidence_review`,
`finalization`, `finalized_review`) and the section table: each section's
heading, whether it came from a file or from this engine, which file, and
its character span in the captured prompt. A reader slices the captured
bytes rather than re-parsing them, so a sectioned view and the exact prompt
cannot disagree. Losing or deleting the index costs nothing; the sequence
number is read from the directory itself.

Captured prompts are stage artifacts, not telemetry: `activity.jsonl`'s
event schema still excludes prompt text on purpose, and always will. They
are also exempt from the acceptance gate's dirty check the way the other
stage artifacts are — but only for the exact filenames above, directly in
`prompts/`, so a stray file dropped in there still blocks a freeze.

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
    -> Stage 3: … NEEDS_YOU -> plan pauses, prints the checks it needs
you do the checks
    -> sparring resume-plan docs/plans/foo.md --repo-root . --expected-branch feature/x \
           --evidence "Tested on a Pixel 7: resume after 24h works."
    -> Stage 3: the SPARRER resumes against the unchanged candidate
       … READY -> freeze -> accept
    -> Stage 4 … until the next human gate or the end of the plan
```

`run-plan` takes the same provider flags as `run-loop`. It stops for
`NEEDS_YOU`, `ESCALATE`, a provider/integrity failure, a freeze or accept
refusal, the SEND_BACK runaway limit, and the end of the plan; ordinary
`READY` is accepted automatically through the existing gate, at the exact
pushed SHA, with no human confirmation. The gate itself is unchanged: if it
refuses, the plan stops and nothing is substituted.

Use `--stop-after-stage <stage-id>` to take a plan one stage at a time: the
run accepts that stage and then pauses with its position already advanced,
without running, creating or briefing the next one. The plan is left exactly
as any other pause leaves it, so the next `resume-plan` continues normally.

### A stage whose candidate is still uncommitted

Some stages are deliberately left uncommitted until a human has verified
them — a project convention for work that needs a real look before it
becomes a commit. `READY` then arrives over a working tree, not a commit,
and the acceptance gate has nothing to freeze. The runner does not treat
such a `READY` as the end:

```text
Stage 4: implementation, deliberately left uncommitted
    -> NEEDS_YOU: five manual checks
you do the checks
    -> resume-plan --evidence "…"
    -> the SPARRER resumes against the unchanged candidate … READY
    -> ONE bounded turn: commit that exact tree on the expected branch, push,
       report the SHA — and change nothing else
    -> the engine compares the committed content against the reviewed tree,
       path by path
    -> the sparrer reviews that exact commit … READY -> freeze -> accept
```

The comparison is the point: a turn that rewrote a file and committed the
rewrite leaves a working tree just as clean as one that committed the
reviewed work untouched, so "clean now" proves nothing. If the committed
content is not the content that was reviewed, the run stops without
accepting and records which paths diverged in the stage's `notes.md` —
because a person's manual check described the tree they inspected, and it
does not carry forward onto different work. Nothing is rolled back; the
commit and both sessions are left as they are.

Stage artifact files (`state.json`, `brief.md`, `notes.md`, `handoff.md`,
`sparring.md`, `activity.jsonl`) are outside that comparison, as they are
outside candidate identity everywhere else: the engine rewrites them on
every turn. The freeze's own dirty-tree rules are not relaxed for any of
this.

A run that is *already* stopped between a recorded `READY` and the commit —
which is where any run killed at that moment stops — is resumed the same
way: `resume-plan` recognises it and enters at that one bounded turn instead
of spending an implementation turn on a tree a human has already verified.

Each planned stage gets its own `.sparring/stages/<stage-id>/activity.jsonl`,
with the same provider stream a direct `run-loop` produces (the adapters are
rebuilt per stage so the stream follows the stage), plus `plan.*` lines
saying when the runner entered, accepted, paused on, failed at or completed
a stage, and when evidence was recorded. That is telemetry for watching the
run; the plan's position lives in `.sparring/plans/` and is never read from
`activity.jsonl`.

### A check that is owed, but not now

Not every manual check has to interrupt the run. A product choice the next
stage builds on does; "resize the finished dialog and check it is still
readable" does not — nothing downstream depends on the answer, and a failure
would be a bounded local fix. The reviewer makes that call, and there are two
shapes for it:

```text
NEEDS_YOU + human_gate            stop now; this must be answered before the
                                  stage can be READY
READY + deferred_human_gate       the stage is accepted, a person still owes
                                  this check, and the reviewer said why
                                  continuing first is low risk
```

The second one keeps the run going:

```text
Stage 1 … READY, with "check comparison readability" deferred
    -> stage 1 accepted; 1 manual check deferred until plan completion
Stage 2 … READY                 (nobody was interrupted)
Stage 3 … READY                 (nobody was interrupted)
    -> every stage accepted, and one check is still owed
    -> the plan PAUSES instead of completing
```

The engine will not forget it and will not complete the plan on it. The
obligation is recorded in the run's own state — the stage that raised it, the
reviewer's rationale, the checks, and the engine-minted gate instance an
answer must belong to — so it survives the next stage, a process exit, a
reload, further `SEND_BACK` cycles and `resume-plan`. Deferring is not
waiving: the stage is *accepted **and** still owes a check*, and both are
reported.

A deferral without a rationale is refused. So is a checkpoint this version
does not implement; today that is `before_plan_completion` only.

The final pause is typed, like the push one:

```json
"awaiting": {
  "kind": "deferred_verification_required",
  "reason": "plan_completion",
  "instance_ids": ["5b22e1c0…"]
}
```

Answer it, and the plan finishes without re-running anything already
accepted:

```sh
sparring resume-plan … \
  --deferred-result 'resize-readability=pass=legible down to 700px'
```

Outcomes are `pass`, `fail` and `blocked`. `blocked` records that the check
could not be performed, which resolves nothing — a plan completed on it would
be a plan completed on a verification nobody made. A `fail` also keeps the
plan open, and is written into the originating stage's `notes.md`. The engine
does not rewind a stage by itself, because un-accepting accepted work is a
person's decision — see [repairing a check that
failed](#repairing-a-check-that-failed) for making that decision. Where the
same check id is owed by more than one asking, address it as
`'<gate instance>:<check id>=pass'`; a bare ambiguous id is refused rather
than guessed.

Deferred is not permanently deferred. Every sparring turn of a managed run is
shown what the run already owes, and may decide that a later stage now depends
on one of those answers; naming its gate instance in `promote_deferred` stops
the run for it before the next stage, under the same asking.

### Repairing a check that failed

Only a `pass` settles an obligation, and the run loop skips accepted stages.
So at the checkpoint, where every stage is already accepted, reporting a
`fail` on its own goes nowhere: the run records it, stops again, and asks the
same question. Nothing is wrong about that — the plan genuinely is not
verified — but it is not a way forward either, and the way forward is not to
make the check pass by hand.

`reopen-stage` is the decision to put the work back in front of the agents:

```sh
sparring reopen-stage 5b22e1c0 docs/plan.md \
  --repo-root . --expected-branch feature/x
```

It names the *asking*, not the stage, because the asking is what failed. Two
things change, and nothing else does:

- the stage goes from `ACCEPTED` back to `WORKING`, keeping its candidate,
  both sessions and its notes. Re-entering a stage whose `sparring.md` records
  `READY` is an ordinary path: the sparrer confirms it on its own terms before
  the hard acceptance gate runs again. The stage agent reads the `fail` first,
  because the engine already wrote it into this stage's `notes.md`;
- the failed asking is **withdrawn** from the ledger. It was a question about
  a candidate that is about to be replaced, and if it still applies to the
  repaired one the new review raises it again — as a new asking, with a new
  gate instance, because it is a new question about new work. The answer that
  failed stays in `notes.md`, which is never rewritten.

This is not [`reset-stage`](#restarting-a-stage-started-under-the-wrong-mode),
which solves a different problem: that one archives the whole attempt,
quarantines what it wrote and requires the repository to be back at the
preceding stage's candidate, because the work there ran through the wrong
lifecycle. Here the work is not wrong, it is incomplete.

Only the run's **current** stage can be reopened. An obligation raised by an
earlier stage is refused, because the stages after it were built on its
acceptance and restarting it would rewrite their history; that repair belongs
in a follow-up stage, and the refusal says so. Also refused: an asking this
checkpoint is not stopped on, and one nobody has reported failing — reopening
a stage over a question that was never answered would throw away an accepted
candidate for nothing.

### Pushing a verified candidate

Acceptance only ever freezes a commit that is already reachable from the
branch's intended remote ref. That rule is not new and is not relaxed. What
*is* new is what happens when a reviewed candidate has not been pushed —
usually because the project's own agent instructions forbid an agent from
pushing without being told:

```text
    -> the sparrer reviews the commit … READY
    -> the commit is not on origin/<branch>, and nothing has authorized a push
    -> the run PAUSES and asks, naming the exact commit
```

It pauses; it does not fail, and it does not push. The pause is recorded in
the run's own state as a typed reason, so a tool reading it knows this is a
permission question rather than a manual test:

```json
"awaiting": {
  "kind": "push_authorization_required",
  "stage_id": "…-stage-4-…",
  "candidate_sha": "f2e455c…",
  "branch": "feature/add-reference-dialog",
  "remote": "origin",
  "remote_branch": "feature/add-reference-dialog"
}
```

Two ways to answer it, and a third that needs nothing from the engine:

```sh
# allow exactly this commit, then continue
sparring resume-plan … --allow-push-candidate f2e455c…

# allow it and stop being asked again for this run
sparring resume-plan … --allow-push-candidate f2e455c… --allow-push-for-run

# or push it yourself and resume normally
git push origin feature/add-reference-dialog
sparring resume-plan …
```

`--allow-push-for-run` can also be given to `run-plan`, which records the
permission as part of creating the run.

An authorized push is exactly one thing:

```sh
git -c push.followTags=false push <remote> refs/heads/<branch>:refs/heads/<remote branch>
```

Ordinary, non-force, one fully qualified refspec, so no `push.default` or
`remote.*.push` configuration can widen it. No `--force`, no
`--force-with-lease`, no tags, no ref deletion, no branch switching, no
other branch, and no sibling repository — a declared sibling is a different
repository, and authorizing this run's candidate says nothing about it. The
engine then re-proves that the commit really is reachable from that remote
ref, and only then runs the unchanged acceptance gate. A push that fails,
or one that reports success without landing, stops the run with nothing
accepted.

Permission is bound to what it was granted for: this run, this worktree,
this branch, that one remote branch, and — for `--allow-push-candidate` —
that one stage and that one commit. `--allow-push-candidate` is refused
unless the run is in fact waiting for permission to push exactly that
commit, so a surface that has been open a while cannot authorize a candidate
the run has since replaced. The default is no authorization at all, which is
also what a run recorded before any of this existed loads as.

### Marking stages in a plan — or handing over an execution manifest

There are two ways to tell `run-plan` what the stages are. The Markdown
convention below is the built-in one. If your plan does not fit it — labels
like `3A`/`3B`/`3C`, historical handoff sections that define nothing, a
sequence already half-executed under other stage ids — the second way is to
interpret the document yourself and hand over an **execution manifest**:

```sh
sparring run-plan --manifest .../manifest.json --repo-root . --expected-branch feature/x
sparring resume-plan --manifest .../manifest.json --repo-root . --expected-branch feature/x
```

```json
{
  "version": 1,
  "plan_label": "docs/plans/active/foo.md",
  "source_digest": "sha256:… of the plan document as you read it",
  "stages": [
    {
      "stage_id": "stage-3c-cloud-schema-rpc-and-sync-transport",
      "label": "Stage 3C",
      "title": "Cloud schema/RPC and sync transport",
      "brief": "… the exact brief.md content …",
      "mode": "implementation",
      "repositories": [
        {"name": "sporely-web", "path": "../sporely-web-worktree",
         "branch": "feature/cloud-transport", "candidate_sha": null}
      ]
    }
  ]
}
```

The array order *is* the execution order; labels are display strings and are
never parsed, so numeric `1..N` is not required in manifest mode. A manifest
carries no status, position, transition or verdict — those stay in
`.sparring/plans/` and each stage's `state.json`, exactly as for a Markdown
plan, and both inputs run through the same code. Unknown fields are refused
rather than ignored, so a manifest written against a later contract fails
loudly. `plan_label` is which document the run executes and what it is
reported by; the run's own identity is its `--run-key`, so a manifest and the
plan it was built from describe the same run rather than merely sharing a
name.

The digest covers everything executable *and* `source_digest`, so re-emitting
a manifest from an edited plan refuses to continue an existing run — the same
protection the Markdown path gets. Re-emitting an unchanged one is stable, so
a tool may regenerate the file on every invocation.

`repositories` is for a stage whose reviewed candidate spans more than one
repository; see "Cross-repository candidates" below. `mode` is for a stage
that is a review and nothing else; see "Review-only stages" below.

`stage_id` is the caller's to choose, and a caller that builds manifests from
plan documents should namespace fresh ids by the *run* the way the Markdown
convention does (`<run key>-stage-<label>-<slug>`), passing that run key to
`run-plan --run-key` so the run is filed under it. Otherwise two runs in one
worktree — two plans that both define "Stage 1 — Foundation", or two runs of
one plan — name the same directory, and the second run is refused as reaching
into the first run's stages
— see "Whose stage is whose" below. A stage that already exists keeps
whatever id it was created under; only new stages need the namespace.

#### Preparing a manifest from a human plan: plan intake

**Design status (2026-09-27): merge blocked.** The following usage describes
the implementation at `1e8de40`, not a sealed approval boundary. The
[integrity revision plan below](#plan-intake-integrity-revision-plan) governs
the remaining work on `feature/plan-intake` and supersedes conflicting
integrity claims here and in `intake.py`'s module documentation.

A plan that is useful to a person is often not executable as written:
stages labelled `0`, `1A`, `1B`; constraints that live above the stage
headings; stages in different repositories; implementation mixed with a
later production action; dependencies stated in prose. The Markdown parser
deliberately does not guess at any of that. Plan intake is a pre-execution
step that does, with a person approving the result:

```sh
sparring prepare-plan docs/plans/active/foo.md [--mode faithful|refine] \
    [--context-repository web=../web]
# review .sparring/intake/<id>/report.md, briefs/, amendment.diff
sparring approve-plan .sparring/intake/<id> --run <slice> \
    [--expected-branch B] [--repository web=../web --repository-branch web=B] \
    [--confirm-prerequisite <gate or earlier slice>] [--without-amendment]
# prints the exact: sparring run-plan --manifest … --run-key … --expected-branch …
```

`prepare-plan` runs **one fresh, read-only** agent turn (the sparring role;
`codex-cli` only, because its read-only sandbox is OS-enforced) that reads
the whole plan, `PROJECT.md` and the named repositories, and answers in a
fixed schema: run slices and their ordered stages, reusable context blocks,
gates, exclusions, findings and a verdict. Nothing is executed or approved,
and the plan document is never written. The engine then checks the answer
and writes, under `.sparring/intake/<id>/`: a snapshot of the plan, the
prompt, the interpretation, one brief per stage, `report.md`, and, in refine
mode, `amendment.diff`. None of these is in manifest format.

- **Briefs are assembled from the plan's own text.** The agent picks line
  ranges; the engine renders each brief as the attached context blocks
  verbatim, then the stage's own text verbatim, then an optional
  `Intake scoping` note that says it is intake's, may only narrow, make a
  boundary explicit or defer to a gate, and yields to the plan text. A
  substantive change goes in refine mode's proposed amendment, which is
  shown as a diff and never applied.
- **Source coverage.** Every non-blank line of the plan (horizontal rules
  aside) must be stage text, context that at least one stage attaches, a
  gate, or an exclusion with a reason. Anything else is a
  blocking finding, so a plan-wide constraint cannot silently drop out of
  the briefs.
- **Run slices.** A manifest runs in one primary repository on one branch;
  a sibling declaration (see "Cross-repository candidates") pins a coupled
  candidate but does not move a stage's work. Intake groups stages into
  slices by primary repository, and a slice is approved and run from its own
  project.
- **Gates are boundaries.** The runtime cannot wait for a deployment or a
  go-ahead between two stages of one run, so a gate that blocks later work
  must fall between slices: a gate may follow only a slice's last stage, and
  may block only the first stage of a slice. Anything else is a blocking
  finding.
  Approving a slice requires `--confirm-prerequisite` for every gate it
  waits for and every earlier slice it depends on; the confirmations are
  recorded in `approval.json`.
- **Approval** refuses on any blocking finding, a refusal verdict, a plan
  or interpretation that changed since intake, an unconfirmed prerequisite,
  a missing branch or sibling path, or an unacknowledged amendment. It
  writes `runs/<slice>/manifest.json` and `approval.json` once, validated by
  the engine's own manifest parser; approving again must produce the same
  manifest.

From there the manifest is an ordinary manifest: digest pinned, briefs
frozen, no agent able to edit it, every existing gate in force. The
interpretation is a reviewed file, never workflow state — nothing after
approval reads it, and the runtime never asks a model which stage is next.
`prepare-plan` exits 2 when its report has blocking findings.

##### Plan-intake integrity revision plan

This is the in-place revision of the existing plan-intake design, not a
second feature plan. Inspection found no standalone plan-intake plan in the
repository: this section and `intake.py`'s module documentation hold its
design. The unrelated, untracked `docs/plans/backend-selection.md` is outside
this work. Status: **planned; no integrity revision implemented**. Baseline:
`0d7e6f8` → `e99b1d2` → `77fe125` → `20ff521` → `e23af2b` → `1e8de40`.
The base is `feature/ask-sparrer-dialogue`; that branch **must land first**.
Keep reusing its public `repo_fingerprint`; do not duplicate it.

**Verified diagnosis.** Source inspection at `1e8de40` confirms the ten
reported failure mechanisms. This revision does not claim to have rerun the
reviewer's 16 probes or the reported 70/124/927-test results.

| Finding | Evidence in the current implementation | Required correction |
|---|---|---|
| 1 / A12: approval does not bind execution | `approve_plan` writes `approval.json`; `manifest.load_manifest_source` only parses the supplied file. `plan._verify_plan_unchanged` binds content at run start, not at human approval. | Verify a protected approval before the first provider call and on continuation. |
| 2: later proposal rewrite | `approve_plan` trusts digests, source path, label and run keys from mutable `intake.json`; it never compares the reviewed report. Coordinated edits can satisfy those checks. | Protect the review candidate before any slice runs, then seal each approval outside the worktree. |
| 3 / A1, A5: coverage is not ownership | `check_interpretation.cover` unions ranges; no conflicting-owner check exists. | One deterministic disposition per substantive line. |
| 4 / A2, A16: gate topology | `after_stage` detects an interior boundary, but `run_prerequisites` uses only `blocks_stages`. An empty block list need not protect later slices. | Derive prerequisites from one execution graph. |
| 5: hidden omissions | `render_report` prints gate/exclusion ranges without their source excerpts; an exclusion needs only a nonempty reason. | Show exact removed/deferred text and require a meaningful disposition decision. |
| 6 / A4: context swallows work | Attached context contributes coverage regardless of its source heading or owning stage. | Preserve heading ownership; promotion requires an amendment. |
| 7 / A7, A8: prerequisites are assertions | Ordering uses interpretation-list positions; approval accepts string confirmations without earlier receipts or completion. The report supplies those strings. | Bind prerequisites to engine receipts and appropriate completion/evidence. |
| 8 / A9, A10: repository drift | `prepare_plan` discards before/after fingerprints. Approval checks no current repository snapshot; `expected_branch` takes precedence over the interpreted value; sibling mappings are not compared to inspected repositories. | Persist and verify repository identities and snapshots; forbid silent overrides. |
| 9 / A13: mode changes roles | Parsing limits `mode` to known values, but no source-derived check governs which one the agent chooses. Approval copies it into the manifest. | Bind mode to authored semantics or an explicit reviewed change. |
| 10 / A14: competing semantic authority | `render_brief` appends narrowing prose beneath a source-wins preamble, without validating references. `good_interpretation()` names undefined gate `repair-run`. | Remove semantic `intake_scope`; use explicit amendments. |

Also verified: stage ranges render in supplied order; ranges address the
original source even when an amendment exists; `SourcePlan.lines` and prompt
numbering use `splitlines()`; source label/path come from the mutable record.
These belong in the same identity/ownership corrections, not separate patches.
`scratchpad/probe_intake.py` and the `scratchpad/` directory are absent in
this checkout. Recover the original probe if available; otherwise reconstruct
the documented attacks and explicitly record which original cases are unknown.
Do not invent meanings for A3, A6, A11 or A15 from their numbers.

Preserve strict parsing, re-derived findings and briefs at approval,
amendment-presence derivation, on-disk plan checking, the source reread after
the genuinely read-only provider turn, idempotent approval, duplicate-id
checks, `gate_inside_run`, primary-repository binding, and intake gitignore
enforcement. Keep intake semantics in the engine; the provider addition stays
the generic `start_structured(prompt, output_schema)`.

**Authority and scope.** The human approves semantics; the engine binds that
decision to bytes, repository identity and state. A hash stored beside
agent-writable data is not a trust anchor. The flow is:

```text
source → read-only intake → proposal → deterministic validation
       → protected review candidate → exact human review
       → sealed slice approval → manifest → verified execution
```

After approval, changing any worktree proposal material must either have no
effect on execution or cause refusal. The ignored intake directory becomes
a disposable review/export cache. It must never authorize execution or the
approval of a later slice. Deterministic validation cannot prove the meaning
of arbitrary prose; unresolved semantics must be visible to the human, not
silently declared safe by coverage or by another model.

**Stage A — Seal review, approval and execution.** Pending.

1. Introduce a small, versioned engine approval store **outside all inspected
   worktrees and provider-writable roots**, using the platform user-state
   directory. Store immutable review candidates, slice receipts, gate evidence
   and the exact approved manifest bytes; worktree files are exports only.
   Register these artifacts and their sole engine writer in
   `artifact_ownership.py`. Existing `.sparring/plans/` and stage state remain
   the runtime lifecycle model; do not replace the runner with a new scheduler.
   Filesystem location or an ownership prompt alone is insufficient: prove
   the provider sandbox excludes the store and cannot invoke a privileged
   approval-writing path. Refuse sealed execution for providers/configurations
   that cannot enforce this. The threat model includes a managed agent with
   worktree write access, not a malicious OS administrator or arbitrary
   unsandboxed process acting as the human.
2. At prepare, persist a protected candidate containing source bytes and
   canonical path/label, interpretation, amendment, disposition/graph,
   repository snapshots, run/stage identities, rendered report and briefs.
   Its engine-computed identity covers all review-relevant semantics and the
   renderer/schema version. Persist this before exporting the report. Approval
   loads this candidate by engine identity, never discovers authority from
   `intake.json`. Re-render and compare; refuse modified exports rather than
   approve a different interpretation behind an old report.
3. Make approval a review-and-decision operation over that exact protected
   candidate: present the selected slice, complete semantic report and exact
   briefs before collecting a deliberate human decision tied to its identity.
   A noninteractive UI must obtain the same engine-issued review identity and
   submit the decision against it. No user-maintained hashes. Fail if the
   candidate changes between review and decision. A later slice reviews the
   same protected graph or an explicitly linked successor, never an edited
   worktree copy. Declining or interrupted review creates no approval.
4. Seal a receipt atomically with at least: schema version, candidate/intake
   identity, source/interpretation/amendment identities, slice and run key,
   raw manifest-byte digest, existing semantic manifest digest, repository
   identities/snapshots and expected branches, prerequisite receipt identities,
   review identity, human decision and timestamp. Include exact payloads in
   the store; do not depend on external files remaining readable. A receipt
   ID is an engine reference, not authentication by itself. Validate records
   against protected storage, serialize competing approvals, and make retries
   identical or refuse; never overwrite an earlier decision.
5. Persist logical name, canonical worktree path, Git/common-directory identity,
   branch, HEAD and the shared `repo_fingerprint` for **every** inspected repo,
   including context-only ones. The helper returns branch/HEAD/dirty paths,
   not content hashes. For this first version require clean inspected repos;
   retain before/after checks and explicitly document that ignored material
   and modify-then-restore activity are not attested by this helper. Inputs
   used as execution authority must be captured separately. Approval compares
   all snapshots and source bytes. Unknown repos, path substitutions, branch
   changes and new commits refuse. No drift override in the initial version.
   An intended run branch differing from the inspected branch must be checked
   out and re-prepared; an approval argument cannot silently replace it.
6. Later slices legitimately see changes from earlier work. Refresh repository
   observations through an explicit new review candidate linked to its parent,
   preserving the original source/graph identity and prior receipts where
   unchanged. Show the snapshot delta and re-review the remaining slices.
   Re-run read-only intake when changed evidence affects interpretation; never
   auto-rebase approval onto a new HEAD. Prerequisite receipts must match the
   referenced graph node/revision; changed nodes invalidate dependent approval.
7. Replace free-text prerequisite satisfaction with protected references.
   Slice B needs A's approval receipt **and** engine-recorded successful
   completion/accepted candidate evidence when B depends on A's work; approval
   alone does not mean execution happened. Capture the completion attestation
   through existing acceptance hooks into the protected store, rather than
   trust later edits to worktree state. A manual/production gate requires a
   separate human evidence decision recording the exact gate, candidate/repo,
   actor, time and evidence (e.g. release reference and observation). This is
   an honest human attestation, not a claim that the engine verified production.
   Reuse existing typed human-evidence conventions where practical; no arbitrary
   confirmation string or prefilled command may satisfy these prerequisites.
8. Verify the receipt, exact manifest bytes, run/stage identities, branch,
   repository bindings and prerequisites at `run-plan` entry **before any
   provider runs**, including direct engine entry points and adoption paths.
   Parse and execute the verified in-memory bytes to avoid check/read races.
   First execution rechecks the approved starting snapshots. Pin the receipt
   in run state and recheck its authority on resume, acceptance and advancement.
   Subsequent engine-accepted commits follow existing candidate/branch guards;
   do not compare every resume to the original HEAD and reject normal progress.

Compatibility: ordinary hand-authored v1 manifests and Markdown plans remain
usable without intake approval; their current digests and resume behavior
remain unchanged. Add a versioned intake manifest envelope/reference handled
by the loader, retaining the existing stage payload. Register protected intake
run and stage identities so deleting the envelope, changing the path, omitting
the receipt or invoking adoption cannot downgrade that execution to legacy
mode. A fully rewritten hand-authored plan with new identities is a separate
human-authorized legacy execution, not an intake-approved run. Do not claim
to infer provenance from arbitrary equivalent prose. Test this boundary.

Already-generated intake folders and their adjacent approvals are **unsealed
legacy proposals**, never upgraded by trusting their self-reported hashes.
Require fresh prepare/review/approval; identify legacy intake paths and refuse
their apparent approval at the CLI. Arbitrary copied old v1 files cannot be
distinguished retrospectively from hand-authored v1 manifests; document that
limitation explicitly. Existing legacy runs retain legacy guarantees and must
not be relabelled as sealed. The prior promise that `run-plan` and manifest
format need no change is withdrawn for new intake-produced manifests.

Recovery: atomically publish complete bundles with an index rebuildable from
protected records, and retain append-only decision/transition history. Back
up the store outside provider write access, including candidates, manifests,
receipts, evidence and identity reservations. Restore from that trusted backup
with integrity checks; never reconstruct approval from the ignored directory.
Missing/corrupt authority fails closed. Without a trusted backup, require new
human review and a successor receipt; do not fabricate historical approval or
completion. Cross-machine migration and signed portable receipt exchange can
be deferred; local backup/restore and crash recovery cannot.

Stage A exit: proposal/report/receipt/manifest tampering cannot affect approved
execution; first-start and continuation checks hold; legitimate progress and
later-slice snapshot refresh work; legacy behavior remains covered. Implement
against `intake.py`, CLI, manifest/plan entry points and ownership/state hooks,
without putting feature semantics in the provider adapter.

**Stage B — Make source ownership and the graph unambiguous.** Pending.

1. Replace coverage union with a derived line-to-owner index: each substantive
   line has exactly one stage-body, context, gate or exclusion owner. Reuse
   range objects; a new enum is optional. Reject every overlap, including
   duplicate executable ownership and overlapping ranges within one owner.
   Shared context has one owner and multiple attachments, not multiple owners.
   Canonically sort ranges by source position; range reordering cannot change
   a brief. Include the disposition and attachments in candidate identity.
2. Recognize explicit stage-heading spans deterministically, including labels
   `0`, `1A`, `1B`; ignore headings inside fenced examples. In faithful mode,
   stage text, tasks and acceptance criteria stay with that source stage.
   Moving to another stage, gate, exclusion or context is a structural change,
   not a range-selection trick. Ambiguous or unsupported heading ownership
   blocks faithful approval and asks for clarification/refinement. Context
   comes from declared contextual sections (principles, constraints, approved
   decisions, background, definitions); headings alone do not prove harmless
   semantics. Quote all context and show its consumers for human review.
3. In refine mode the amendment is the sole semantic transformation. For the
   smallest coherent first version, retain the existing apply-and-reprepare
   path: propose exact amended source plus a structural diff; the human applies
   it, then prepares/reviews that new source. Never execute an interpretation
   using amended coordinates against original bytes. Link the new candidate
   to the prior source/amendment for provenance. Remove `--without-amendment`
   as a way to execute structural changes against old text. Remove executable
   `intake_scope`; explanatory rationale may remain in the report, never as
   competing instructions in a brief. Splits, task-to-context promotion,
   exclusions of stage-owned requirements, deferrals and role changes must
   appear in the amended source and its review.
4. Build one graph from ordered source stages and explicit boundary gates.
   Default to source order, including across slices. A gate after A is inserted
   after A's slice and is a prerequisite of all subsequent slices in this
   conservative initial model. Derive downstream prerequisites transitively;
   remove independently authoritative `blocks_stages` (or accept it only as
   redundant input that must exactly match derivation). A pre-first gate
   explicitly precedes the first slice. A gate with no downstream work must
   be explicitly terminal verification, with durable evidence required before
   the whole intake is considered complete; otherwise reject it. Validate
   every deferral target, unknown edge and cycle. No gate may be inside a
   slice. Prose dependencies must be shown against graph edges; unresolved
   conditional language blocks review until made explicit. Parallel/nonlinear
   scheduling can wait; deleting `depends_on` must not erase source order or
   gate consequences.
5. Default authored stages to implementation. A review-only lifecycle needs
   an explicit, engine-recognized source declaration; ambiguous review prose
   is not permission to skip implementation. Any role change requires an
   amended declaration and visible review. Render exactly which roles run,
   and bind mode in candidate, receipt and manifest identities.
6. Use one line-coordinate contract for prompting, validation, diffs and
   rendering: LF-delimited lines with explicit CRLF handling and raw source-byte
   hashing. Reject unsupported control/Unicode line separators with a useful
   diagnostic rather than silently changing editor-visible numbering. Compare
   source excerpts exactly under that documented contract. Reject empty or
   punctuation-only exclusion reasons, but do not confuse a longer reason
   with semantic correctness: exclusions require exact-text human review.

Stage B exit: overlap, hidden movement, dangling gates, mode swaps and
ambiguous scope all block before sealing; faithful examples retain their
requirements and refine transformations are visible in the amended source.

**Stage C — Make review usable and prove the boundary.** Pending.

1. Render the report from the protected candidate. Quote gate, exclusion and
   context text verbatim with source coordinates and owning stage before/after;
   show exact briefs, modes/roles, repository snapshots, prerequisites and
   evidence state. Escape/nest source headings so quoted prose cannot masquerade
   as engine decisions. Show a derived semantic summary before exact detail:
   original stages affected, executable stages, gates, repository splits,
   manual pauses and reorders. Counts come from the transformation, not the
   model's narrative. Show terminal obligations and unresolved decisions.
   No prefilled confirmation/evidence values; command hints identify missing
   evidence without asserting it exists.
2. Convert the available adversarial cases into durable regression tests using
   real prepare → review/approve → run flows with deterministic provider fakes.
   Track A1–A16 against the recovered probe or documented reconstructions;
   unknown cases remain explicit, not falsely marked covered. Cover coordinated
   interpretation/digest rewrites after slice 1, stale reports, source-path and
   label substitution, stage/gate/exclusion overlaps, context swallowing,
   empty gate effects, removed dependencies, prerequisite fabrication,
   branch/HEAD/path drift, edited approved briefs, stage-mode flips and dangling
   `repair-run`. Fix the permissive good fixture during implementation, not in
   this plan-only pass.
3. Add invariant-oriented mutation/generated tests: (a) every agent-writable
   proposal mutation is inert or refused after approval; (b) any differing
   manifest bytes, even a semantic-equivalent serialization, cannot use the
   same receipt; (c) each substantive line has one non-conflicting disposition;
   (d) a changed disposition changes identity and visible review;
   (e) downstream work needs verifiable upstream approval and required evidence;
   (f) repo/branch/HEAD drift cannot pass silently; (g) mode changes cannot
   silently change agent roles. Include cache deletion, symlinks/path aliases,
   stripped envelopes, wrong run keys, direct API/adoption/resume paths,
   concurrent approval, crash/restore and candidate changes during review.
   Assert refusal happens before a provider invocation.
4. Retain existing regression coverage for idempotence, strict parsers,
   read-only fingerprinting/source reread, primary binding, coverage gaps,
   gate boundaries and ordinary manifests. Run focused intake/manifest/state
   tests, then the full suite because execution authority changes. Record
   actual commands/results; prior pass counts are historical evidence only.
5. Re-dogfood taxonomy-v3 in faithful and refine modes: all 290 substantive
   lines in the referenced version must have owners, contextual sections stay
   visible, labels and repository splits survive, production actions become
   enforced gates, and unsafe faithful interpretations refuse. Check the
   proposed `2A/2B`, `3P/3W`, `4A/4P/4W`, `6P/6W`, Gates A–E and stale factual
   claims against the actual source version; do not hardcode these outcomes
   into the engine. Record source digest and semantic-review evidence.

**Merge and deferral decisions.** All three stages, their exit checks, and a
fresh independent integrity review block merge, as does the parent-branch
dependency. Implementation and final independent review remain separate
sessions. Update this plan with evidence on each pass. This revision changes
documentation only; its validation is diff inspection and `git diff --check`.

Defer adaptive runtime scheduling, automatic graph replanning, nonlinear gate
topology, dirty-repository snapshot support, silent/automatic drift rebasing,
portable signatures and cross-machine approval sharing. Preserve compatibility
with future adaptation through immutable candidate IDs, parent-linked reviewed
revisions and append-only decisions; never rewrite old approval history.

No product choice requires human input to start implementation: this plan
chooses conservative refusal, clean snapshots, external protected state and
apply-and-reprepare amendments. Stage A must prove the sandbox/storage boundary
before claiming integrity; if the supported provider cannot isolate engine
authority, stop and bring the concrete limitation back for a deployment/trust
decision. A directory outside the worktree is not by itself that proof.

#### The Markdown convention

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
`<run key>-stage-<n>-<slugified title>`, where the run key is this execution's
identity — the plan's file stem, a short hash of its path, and a short hash
for the run itself (for example `foo-3f9a2c1b-91af03d4`) — so two runs with
the same headings never share stage artifacts, whether they are two plans or
two runs of one plan.
Nothing is inferred from prose: a plan with no such headings, a heading that
starts with `## Stage` but does not fit, a numbering gap or duplicate, or an
empty section is refused before any agent runs. A reviewed plan written
another way either needs a small edit to mark its stages, or a manifest.

### Pause and resume

Run position lives in `.sparring/plans/<run key>.json`: the plan document, a
digest of its executable content, the branch, the current stage, which kind of
input it runs from, this run's own key, and a status
(`running`/`paused`/`complete`). Candidate SHAs, sessions and acceptance stay
in each stage's own `state.json`.

`resume-plan` continues a recorded run. Given a plan document with exactly one
open run it continues that one; `--run-key <key>` says which, and is needed
only for a document with several open runs.

#### Run instances: a plan document is an input, not a run

A plan document can be executed more than once. Each execution is a **run
instance** with a key of its own, `<plan key>-<8 hex>` (for example
`mosaic-fix-7c1e42a9-3b4d0f16`), minted per `run-plan`:

```
repository
  plan.md                    <- a document. Input to a run.
      run A                  <- .sparring/plans/mosaic-fix-7c1e42a9-3b4d0f16.json
          stage 1, stage 2
      run B                  <- .sparring/plans/mosaic-fix-7c1e42a9-91af03d4.json
          stage 1, stage 2
```

**`run-plan` always starts a new run.** The same repository, the same branch,
the same plan file and the same `Stage 1` headings as a run that already
finished do not change that: it is new work, it starts fresh Stage-agent and
Sparrer sessions, and it does not advance past the earlier run's accepted
stages. The earlier run stays on disk as inspectable history, and nothing has
to be removed for the next one to start.

The one thing that *is* refused is a second **open** run of the same document
in the same worktree — running or paused. That is not about identity; two live
managed runs would compete for the same candidate. The refusal names the open
run and how to continue it. A complete run never refuses a fresh one.

#### Whose stage is whose

A stage instance belongs to the managed **run instance** that made it. That
run's key is recorded in the stage's own `state.json` as `run`, written once
when the run creates or deliberately adopts the stage and never repointed, so
execution-stage identity is `(run instance, stage)` rather than a directory
name that anything may claim.

This is what makes both ordinary workflows work. Finish a plan, stay on the
same branch, and start a *different* plan whose sections are numbered
`Stage 1` again — or run the *same* plan again. Either way the new run's
stages are new work. The run-key prefix in each stage id keeps them apart on
disk, and ownership keeps them apart even when something generates colliding
ids: the run is refused, naming the stages, rather than quietly answering new
work with an old run's accepted work. `--adopt` does not override this and no
flag does.

Note that the owner is a *run* key and not a plan key. "Owned by plan X" would
still let a second run of X inherit the first run's accepted stages, which is
the same defect one step removed.

A `state.json` with no owner is **unowned**: a stage driven by hand with
`new-stage`, or one written before ownership was recorded. Unowned is the only
thing `--adopt` may take over, which is precisely what it is for.

#### Runs recorded before run instances existed

Nothing needs migrating. A run recorded at `.sparring/plans/<plan key>.json`
with no `run` field is read as that document's one legacy run instance, keyed
by the plan key — which is exactly what it was, since at the time a document
had a single execution. Its stages record that same key (under the older
`plan` spelling, which is read as the owner), so they stay owned by it, and a
fresh run of the same document gets stage ids and stage instances of its own.
Neither file is rewritten to say so.

#### Adopting a sequence that is already under way

`--adopt` is the deliberate way to take over stages that already exist —
typically a sequence that was driven stage by stage before it was managed.
It means "these stages were executed independently and I want this managed
run to adopt them", never "a directory with this generated id exists, so
reuse it": an existing stage another managed run owns is refused first, and a
caller must not infer `--adopt` from finding stage state on disk. It plays no
part in an ordinary *select repository -> select plan -> run*, which needs no
adoption decision at all. Each
remaining stage is checked, and every adoption is reported:

* **accepted**, with a real candidate commit → adopted and advanced past. Its
  brief is history and is not compared: the work is already through the hard
  gate, and a wording change since then cannot affect anything.
* **not accepted**, brief identical to the plan's → adopted, continuing its
  recorded sessions and candidate. The report says what is being inherited.
* **anything else** — unreadable state, missing brief, a brief that differs →
  refused, naming the stage. A stage that ran against a different brief is
  not this plan's stage, and re-briefing it silently would throw away the
  context its sessions hold.

Note what the second rule compares: the stage's `brief.md` against **the plan
input's** text for it — not against whatever the plan document says today. A
stage that has already run is defined by the brief the work was implemented
and reviewed against, and the plan section it came from is a living document
that is usually rewritten afterwards to record what was built. So an adoption
manifest should carry an already-executed stage's existing `brief.md`
verbatim, and brief only the stages that do not exist yet from the plan's
current section. One manifest then describes both the preserved history and
the future execution, and adopting a sequence never requires deleting a stage
or rolling the plan document back. (The Markdown input has no way to say this,
so a plan whose sections have moved on is a case for a manifest.)

##### A stage that is already waiting for you keeps waiting

The stage a hand-driven sequence is usually adopted *at* is one that stopped
for a person: a `NEEDS_YOU` gate, or an `ESCALATE`. Entering it is not
allowed to answer that question, so the run doesn't try. It adopts the pause
exactly as it stands — same candidate, same sessions, same `sparring.md`,
same gate — reports `already NEEDS_YOU and waiting for you`, and stops there
with the run recorded as paused at that stage. Nothing is run and nothing is
rewritten, so a human who was midway through a manual check is not asked to
start over or to produce the gate again.

The way out is the way it always was: `resume-plan --evidence`. The same rule
applies to a plain `resume-plan` that answers nothing — the pause is a real
state, not a step to be stepped over. A recorded `SEND_BACK` is the opposite
case: there is implementation work, so the loop takes it.

A `sparring.md` written before human gates were structured is read too: the
verdict is what it says, and the gate is simply absent. Anything unreadable
or unrecognised counts as no recorded verdict at all, because a half-read
verdict must never decide whether an agent runs.

#### Answering a human gate

`--evidence` is appended to the current stage's `notes.md` under
`## Human evidence`; you can also edit that section by hand. That file is the
one canonical place a human's answer lives — the sparring prompt reads the
section live and the stage prompt embeds it, so nothing has to be mirrored
anywhere for either agent to see it.

The same stage then resumes, **at the sparrer**. The human satisfied a review
gate: the code did not change, the evidence did, and the question is whether
the reviewer now accepts the same candidate. Starting an implementation turn
to carry the answer would spend a turn with nothing to implement and move the
very commit under review. What the sparrer says next decides:

| verdict     | what happens                                                    |
| ----------- | --------------------------------------------------------------- |
| `READY`     | freeze and accept at the same SHA, then the next stage starts — unless the reviewed candidate is not a commit yet, which adds one bounded commit/push turn and a review of that commit first (see above) |
| `SEND_BACK` | there *is* work: the ordinary loop takes over from the stage agent |
| `NEEDS_YOU` | still paused, with the new gate                                    |
| `ESCALATE`  | still paused                                                       |

An answer never creates a new stage, and with no code change the same SHA is
simply sparred again — no dummy commit. A stage that has never implemented
anything has no candidate to spar, so it starts normally instead.

The digest of the plan's executable content is re-checked on `resume-plan`,
before every acceptance and before every advance, so a stage section edited
during a run, even by the implementation agent, even committed, pauses the
plan instead of being accepted or executed; for a Markdown plan, prose
outside the stage sections may change freely.

After `ESCALATE`, spar the stage elsewhere with the printed handoff/packet
commands, then either accept it by hand (`freeze-candidate`,
`accept-candidate`) and `resume-plan` — an already-accepted current stage is
advanced past — or `resume-plan --evidence` with the external verdict.

## NEEDS_YOU is a structured gate, not a paragraph

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

## Cross-repository candidates

Some stages are genuinely two repositories: a desktop change here and a
coupled migration there, and the sparrer's `READY` depends on both. Pinning
only the primary commit would let the sibling move between review and
acceptance, so the stage would claim a candidate set that no longer exists.

A stage may therefore declare sibling repositories. The declaration belongs in
the plan input — a manifest stage's `repositories` — and the plan runner
writes it into that stage's `state.json` before anything runs, so the
standalone `freeze-candidate`/`accept-candidate` commands see the same
complete candidate set:

```json
"repositories": [
  {"name": "sporely-web", "path": "../sporely-web-worktree",
   "branch": "feature/cloud-transport", "candidate_sha": null}
]
```

(That is also the on-disk shape, but treat it as the engine's record rather
than as something to maintain by hand: declare it where the stage is defined,
and let the run write it. The VS Code extension has a command for this.)

`freeze-candidate` then treats each sibling exactly as it treats the primary
repository — right branch, clean worktree, commit pushed and reachable — and
records the resolved commit as the pin. `accept-candidate` re-verifies every
pin: a sibling that moved refuses acceptance as stale, and a dirty sibling
refuses it too. `path` is resolved against the primary repo root when
relative; a declared `candidate_sha` is an assertion, and freezing refuses if
the sibling is not exactly there.

That is the whole feature. There is no cross-repository merge, no
transaction and no remote coordination — the requirement is only that
acceptance pins and verifies the complete reviewed candidate set.

## Review-only stages

A plan's last stage is often not work at all: a fresh independent reviewer
verifies the candidates the earlier stages accepted, checks every gate, and
the plan's activation decision is taken on that. Run through the ordinary
lifecycle, such a stage gets an implementation agent that has nothing to
implement — it opens a session, reads around, and sooner or later writes
something to try an idea out, at which point the reviewer of the work is
also its author.

So a stage can declare what it *is*, in its manifest entry:

```json
{"stage_id": "stage-5-independent-final-review", "label": "Stage 5",
 "title": "Independent final review and activation decision",
 "brief": "…", "mode": "independent_review"}
```

`mode` is optional and defaults to `implementation`, so every manifest
written before it existed means what it always meant. Nothing infers it:
a stage titled "Independent final review" runs the implementation lifecycle
unless its manifest says otherwise, because which agent runs is not a thing
to read off a heading. The Markdown convention has no way to declare a mode
and always means `implementation`.

A review-only stage's lifecycle has no implementation turn in it:

    enter the stage → pin exactly what is under review → one fresh
    independent reviewer → READY / NEEDS_YOU / defect

All four outcomes are terminal. `SEND_BACK` is a **defect report**, not a
correction cycle: there is no stage agent behind this stage to send work
back to, so the plan stops, unaccepted, and you decide where the fix belongs
— a new stage, a reopened earlier one, or outside the plan. Nothing turns
the review stage into an implementation stage on its own. `NEEDS_YOU` is the
ordinary structured gate: `resume-plan --evidence` records your answer and
the *same* reviewer session judges it.

What the reviewer is told is assembled for the job. There is no `handoff.md`
to show it (no stage agent ran), so in its place the engine states the exact
accepted candidate commits, per stage, across every declared repository —
verified against the repositories immediately before the turn. The reviewer
is told plainly that it has no write access and that a defect it finds is
not its to fix. Its captured prompt is recorded under the `reviewer` role,
so `prompts/0001-reviewer-original.md` is what you read to see whether the
active actor really is a fresh independent reviewer.

Completion does not manufacture a commit. What the stage is judged against
is pinned into its `state.json` when it is *entered* — `base_sha` is the
primary commit, `repositories` the sibling candidates — and READY re-verifies
exactly that set: right branch, HEAD still at the reviewed commit, worktree
clean, the commit still on the remote, every sibling still at its pin. Only
then is the stage ACCEPTED, with `candidate_sha` equal to `base_sha`: the
accurate statement that this stage added no commit of its own. The
implementation path's `freeze-candidate`/`accept-candidate` is untouched and
is not reachable from review mode, nor the other way round. Nothing is
merged, here or anywhere else.

### Restarting a stage started under the wrong mode

A stage can end up having run through the wrong lifecycle: the run reached it
before the mode was declared, or against a plan input that did not carry it.
It then holds an implementation session, possibly something the stage agent
left in the worktree, and no independent review — and none of that can
become the authoritative review. The engine refuses to continue such a stage
rather than adopting that attempt, and names one command:

```sh
sparring reset-stage stage-5-independent-final-review \
  --manifest .../manifest.json --mode independent_review \
  --repo-root . --expected-branch feature/x
```

It archives the attempt to
`.sparring/stages/.archive/<stage-id>/<n>-<mode>-<timestamp>/` — the stage
directory, its captured prompts and its session ids all preserved as
history, and stripped of their authority, since nothing resumes an archive.
Files the attempt wrote are read from its own activity log and quarantined
into the same archive (including a compiled `__pycache__` copy of a test
file it created, which git never showed you and a later test run would still
import). Then the stage is recreated in place, under the same id, with the
plan input's brief and a fresh `state.json` — no session, no candidate — and
the run's plan digest is re-recorded, since declaring a mode changes what
the plan executes.

What it refuses, before moving anything: a stage that is not the run's
current stage, or is accepted; a plan input that declares the mode the stage
already ran under, or disagrees with `--mode`; a preceding stage that is not
accepted with a real candidate; a repository that is not on the expected
branch and at the preceding accepted candidate; an attempt that modified
tracked, committed content; and any dirty path it cannot account for — your
uncommitted work is never swept aside to make a recovery possible.

## Who owns which artifact

Every file a run reads or writes has exactly one authority. Paths are
relative to the project's `.sparring/` directory, except the plan document
itself, which is an ordinary repository file.

| Artifact | Owner | Provider-writable? | Lifetime | Purpose |
| --- | --- | --- | --- | --- |
| the plan document (`docs/plans/.../<plan>.md`) | human / repository | no | immutable for the lifetime of a managed run | the run's execution definition |
| `PROJECT.md` | human / repository | no | edited between runs by a person | project context embedded in every prompt |
| `project.toml` | human / repository | no | edited between runs by a person, by hand or through `sparring set-config` on their behalf | provider, model and effort selection, and engine configuration |
| `plans/<run>.json` | engine | no | rewritten on every position/status change | the run's position, expected branch, plan digest, recorded push authorization and typed pause |
| `intake/<intake>/` | engine | no | written once by `prepare-plan` (the intake agent's answer is its structured result); `runs/<slice>/` written once by `approve-plan`; read by no managed run | a reviewable interpretation of a human plan and, per approved slice, the manifest passed to `run-plan --manifest` |
| `stages/<stage>/brief.md` | engine (a person, for a hand-written stage) | no | generated from the plan section, or hand-written before execution; then immutable | what the stage is reviewed against — the plan section verbatim in a managed run |
| `stages/<stage>/notes.md` | engine (and a person editing by hand) | no | skeleton at creation, then appended to by section | a human's recorded answer or check results (`## Human evidence`) |
| `stages/<stage>/handoff.md` | engine | no | regenerated in full by every implementation turn | that turn's claims, git identity and evidence, for the sparrer |
| `stages/<stage>/sparring.md` | engine | no | rewritten in full by every sparring exchange | the latest verdict, rendered from the structured routing result |
| `stages/<stage>/state.json` | engine | no | rewritten on every lifecycle change | status, candidate identity, provider session ids |
| `stages/<stage>/dialogue.jsonl` | engine | no | append-only; one record per question and answer | provenance for the read-only conversation a person holds with the reviewer (`sparring ask`) |
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