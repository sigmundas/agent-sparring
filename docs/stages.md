# Run a stage

[← Documentation index](README.md)


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

### Feedback and context continuity

Bounded stages put review checkpoints between pieces of work. The aim is to
surface mistakes while their scope is still small, before later stages build
on them. Within a stage, corrections normally resume the implementation and
review conversations separately; each role keeps its own history. A new stage
starts new conversations, and an explicit
[fresh session](reference.md#resume-fresh-session-reset) replaces the selected
role's conversation within an existing stage.

A human check that blocks the current stage can be answered before the plan
advances. The recorded evidence goes back to that stage's reviewer, which can
ask for a correction or further evidence. This keeps the check close to the
work under review. Some checks deliberately belong at a later checkpoint;
[deferred checks](plans.md#a-check-that-is-owed-but-not-now) follow that separate
workflow, so not every manual check is resolved within the stage that raised it.

Context reuse may reduce repeated repository orientation and explanation, but
we have not benchmarked token usage, cost or completion time against workflows
that start new agents for each implementation or review turn. Retained history
still contributes to later requests; conversation length, prompt caching and
correction rounds can change the cost in either direction. The benefit described
here is continuity and frequent opportunities for feedback. Lower token use or
cost remains a hypothesis to measure.

### Activity and usage

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

A role that has had more than one provider conversation (see
[fresh sessions](reference.md#resume-fresh-session-reset)) is reported one
*session generation* at a time: a `---- <role> session generation N (<why
it started>; <provider, model, effort>) ----` boundary, that generation's
own token figures (providers count cumulatively per session, so each
generation is summed separately), and a line totalling all generations. Turn
rows then name the generation, e.g. `sparrer#2`.

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
