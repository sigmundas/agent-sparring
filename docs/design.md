# Sparring

Date: 2026-09-10

## Purpose

Build a small, generic system for pairing a stage implementation agent with an
independent sparring agent.

The system exists to make capable coding agents more effective without making
the human act as a message courier.

It is NOT intended to be a watertight workflow/compliance engine.

The core loop is:

    fresh stage agent
          ↕
       sparring
          ↕
    same stage agent
          |
          +---- NEEDS YOU ----> human
          |
          +---- ESCALATE -----> stronger/manual/web sparring
          |
          +---- READY --------> acceptance

A new stage gets:
- a fresh top-level implementation agent;
- a fresh sparring context.

Those two contexts may remain alive and communicate repeatedly for the life of
that stage.

Implementation subagents live inside the stage agent's work. They do not
replace independent sparring.

---

## Principles
## Orchestration ownership

The orchestrator, not an AI agent, owns top-level agent lifecycle.

- Only the orchestrator starts/resumes the stage agent and sparring agent.
- Provider adapters must obtain session/process identity from the provider or
  process itself; never trust an agent's prose claim that it launched another
  agent.
- There is at most one top-level implementation writer for a stage/worktree at
  a time.
- Implementation agents may use bounded specialist subagents, but may not
  create another top-level stage agent or sparrer.
- Prevent two implementation agents from concurrently modifying the same
  working tree. A simple execution lock/worktree ownership mechanism is enough.
- Before an implementation run, verify the expected feature branch. Do not
  permit an unattended stage agent to commit directly to main.
- Sparring is read-only by default. If a sparrer is explicitly allowed to edit,
  it becomes a contributor and loses independent acceptance authority for that
  candidate.
- Routing decisions come from machine-readable artifacts/process results, not
  claims such as "I dispatched another agent".
  
### 1. Sparring, not review

Sparring is conversational and iterative.

The sparrer may:

- inspect actual repository state;
- challenge implementation claims;
- ask questions;
- explain findings;
- request tests;
- defer reasonable checks;
- send bounded corrections back to the stage agent;
- re-check corrections;
- identify choices requiring the human.

A finding such as:

    "You missed this guard; fix it and add this regression."

does not create a formal workflow transition.

It is simply sent back to the current stage agent.

### 2. Keep the implementation agent focused

The stage agent owns one bounded stage.

It may:

- implement;
- test;
- use project-specific implementation subagents;
- receive small same-stage corrections;
- amend repeatedly;
- update implementation notes.

Do not use the coding agent as the main place for broad product discussion,
architecture exploration, UI preference discussions, or unrelated queries.

Those belong with the human + sparrer.

A genuinely new stage gets a fresh implementation context.

### 3. Agents should communicate without the human acting as courier

The system supports:

    stage agent
        -> handoff
        -> sparrer
        -> SEND_BACK
        -> same stage agent
        -> new handoff
        -> same sparrer
        -> ...

The shared communication must remain human-readable.

`run-loop` runs this unattended. The individual steps stay available as
separate commands, so a stage can still be sparred manually or in a web chat.

### 4. Human interruption is exceptional

The loop should stop for the human only when there is something useful for the
human to do.

Standard human-break categories:

    NEEDS YOU — PRODUCT/PREFERENCE
    NEEDS YOU — UI/VISUAL CHECK
    NEEDS YOU — DEVICE/MANUAL CHECK
    NEEDS YOU — EXTERNAL CONDITION
    NEEDS YOU — SCOPE EXPANSION

Examples:

- choose between two valid UX behaviors;
- inspect visual layout;
- test on a real Android device;
- verify Bluetooth/camera/offline/lifecycle behavior;
- wait for store/OAuth/deployment state;
- approve work that materially changes stage scope.

### 5. Checks may be deferred

Tests and manual checks do not all need to block implementation.

A deferred check must say:

- what is deferred;
- why;
- when it becomes required.

Example:

    DEFERRED
    Android 24-hour resume check.
    Requires installed closed-test build.
    Required before release, not before completing this coding stage.

Deferred evidence is documentation, not a workflow state.

### 6. Sparring outcomes

The sparrer should produce one routing outcome when it is ready to hand
control elsewhere:

    SEND_BACK
        Implementation issue within the current stage.
        Resume the same stage agent automatically if possible.

    READY
        No unresolved implementation issue requiring another stage-agent pass.

    NEEDS_YOU
        A concrete human choice/check/action is required.

    ESCALATE
        The issue deserves another/stronger sparring environment, such as GPT
        web chat, rather than being decided inside the automatic loop.

The sparrer may continue discussing before choosing an outcome.

### 7. Independence

A sparrer that only inspects, discusses and sends corrections remains
independent and may continue with the stage.

If the sparrer edits application/test code itself, it becomes a contributor to
that candidate.

A contributor must not independently give final acceptance to its own change.
Use a fresh sparrer for acceptance in that case.

Do not mechanically enforce freshness with elaborate session state unless
experience shows that this is necessary.

#### The human may talk to the sparrer, and that costs some independence

`sparring ask` (see "Asking the reviewer" in [docs/stages.md](stages.md), and
`agent_sparring/dialogue.py`) lets a person put questions to the sparrer in
its own thread and get prose back. It exists because a `NEEDS_YOU` gate was
able to demand judgement while withholding the material to exercise it: the
evidence and the reasoning were in the provider's conversation, which the
gate could cite and nobody could read. The rule that follows is that **when
the sparrer emits `NEEDS_YOU`, every check must carry enough evidence to
decide, or an interactive path to obtain it.**

The turn is read-only, verified by the same before/after repository
fingerprint an ordinary sparring turn takes, and it writes no workflow state
at all — no verdict, no `state.json`, no `sparring.md`. A recorded verdict
changes only when a real sparring turn writes a new one.

What it does change is this principle, and the change is deliberate rather
than overlooked. Because the exchange happens in the reviewer's own thread,
a later sparring turn has seen it, so a person can argue a sparrer out of a
finding by talking to it. The alternative — a fresh session seeded from the
artifacts — keeps independence perfectly and answers "why did you conclude
that?" with a reconstruction by a reviewer who did not conclude it, which is
worse than useless for the question being asked. So the trade is taken
knowingly: the sparrer stays independent of the *implementation* agent,
which is the separation that matters, while being explicitly open to the
human it is reporting to. The prompt says as much, and says the turn decides
nothing.

The second cost is ordinary but easy to forget: the conversation spends the
reviewer's context window, which is shared with the review. `sparring usage`
counts dialogue turns so that is visible rather than inferred.

### 8. Acceptance is the hard gate

Normal sparring is soft.

Acceptance is strict.

Before a stage is accepted:

- candidate must be a real commit;
- candidate identity must be exact;
- candidate must be available/pushed for the sparrer;
- acceptance must name exactly that candidate SHA.

If code changes after the acceptance candidate is frozen, previous acceptance
is stale.

If evidence changes but code does not, the SAME SHA may be considered again.
No dummy commit is required.

Intermediate sparring findings do not need immutable verdict identities.

### 9. Git history is implementation history

Do not build another forensic history system unless it becomes necessary.

Git records implementation changes.

Stage notes record useful sparring findings and human evidence.

Final acceptance records the candidate that actually matters.

An optional append-only activity log may exist for observability, but it must
not become the authority controlling which workflow transitions are legal.

---

# Generic project separation

The generic Sparring tool must contain no baked-in rules for any particular
application.

A project supplies two distinct kinds of configuration.

## Machine-readable configuration

Project-local:

    .sparring/project.toml

Example:

    project = "my-repo"

    [repo]
    root = "."

    [commands]
    test = "npm test"
    build = "npm run build"

    [agents.stage]
    provider = "claude-cli"

    [agents.sparring]
    provider = "codex-cli"

    [sparring]
    default_mode = "local-auto"

    [stage]
    self_check = false

Provider ids are explicit adapter ids (e.g. `claude-cli`, `codex-cli`), not
bare vendor names: a vendor may later have more than one adapter (CLI vs
API/SDK), and the id in config must say which one is meant. This is a
canonical-vocabulary decision, not a registry: adding a new provider still
means adding a new adapter module and a matching CLI/config branch, not
registering a new string.

`[stage] self_check` (Stage 5) is optional and defaults to `false`. When
`true`, the stage prompt gains one additional prose section asking the
implementation agent to inspect its own work (failure between steps,
resume/retry behavior, stale state, provider/runtime differences,
concurrency issues, ways invariants can be bypassed) before finishing its
turn. It is prose only: no workflow state, no checklist fields, no
pass/fail gate. Independent sparring still runs afterward regardless.

### Model and effort: one resolution, honest per provider

`[agents.<role>]` carries only `provider`: which adapter a role runs, a
project decision. Model and effort are not project settings — they are the
*person's* own preference, shared by every project and kept in one
engine-owned file outside every repository
(`agent_sparring/user_config.py`), keyed by role *and* the provider just
resolved so a preference saved for one provider is never applied to
another. Resolution for every role and every field lives in exactly one
module, `agent_sparring/agent_config.py`, and every orchestration path —
`run-stage`, `run-sparring`, `run-loop`, `run-plan`, `resume-plan`, the
independent-review stage and the finalization turn — builds its adapters
from it. The alternative, each command consulting the config for itself, is
how one path ends up quietly ignoring it.

    provider:        explicit CLI override  >  SPARRING_* environment  >  .sparring/project.toml  >  engine default
    model / effort:  explicit CLI override  >  SPARRING_* environment  >  user preference          >  provider default

A `project.toml` written before this split still parses: `model`/`effort`
under `[agents.<role>]` are read only so they can be reported as obsolete
(`show-config --json`'s `setup_problems`, kind `obsolete-agent-setting`) and
removed by `fix-config`. They are never resolved, and while one is present
every command that would start a provider turn refuses: a stale value is
neither silently honoured nor silently ignored.

The environment layer exists for a one-off: trying a different model in one
shell without changing the saved preference. The `SPARRING_<ROLE>_<FIELD>`
variables set the provider, model or effort and leave nothing behind
anywhere. They sit below the command line, because an explicit flag is
still the most specific thing a person can say, and above the persistent
layer (`project.toml` for the provider, the user preference for model and
effort), because a shell-local choice overriding the saved one is the entire
point. Being applied inside the one resolver means every orchestration path
honours them without any of them knowing they exist.

The user preference file, like the environment, leaves nothing behind in the
repository: after the fact, nothing there says which model ran. That is why
every run records an `agents.resolved` event per role in the stage's
activity log, carrying each value *and the layer that supplied it*, and why
both the environment and the preference file are validated rather than
quietly ignored — a set-but-empty environment variable is an error, because
someone who exported the name meant to select something, and a preference is
validated by `sparring set-config` before it is ever written.

"Provider default" is a real state, not a missing answer: the engine passes
no flag and reports the value as unset with source `provider-default`. It
never writes a guessed model name into that gap, because a provider's
internal default moves without telling us, and a UI that displayed the guess
would be asserting something nobody checked.

Effort is modelled per provider rather than as one engine-wide vocabulary.
The two installed CLIs agree on `low|medium|high|xhigh|max` and disagree
beyond it (Codex additionally has `minimal` and `ultra`), and they express
the setting through unrelated argv: Claude has `--effort`, while Codex has no
such flag and takes `-c model_reasoning_effort=…`. A generic translation
layer — "high means whatever each provider's high-ish setting is" — would be
the engine claiming a capability it has not verified, so an unsupported level
is a configuration error raised before a provider process exists. Claude's
CLI makes that refusal load-bearing rather than pedantic: it accepts an
unknown `--effort`, warns, and runs the turn at its default, so a typo would
otherwise be paid for in full and never noticed.

Model *names* are not enumerated, because both CLIs accept free-form names
and gain new ones between engine releases. The engine validates the shape of
the configuration and the vocabulary it genuinely knows; the provider stays
the authority on its own models. Configuration is typed for the same reason
there is no `extra_args`: a list of raw command fragments in TOML is shell
injection with a schema, and it would let a project reach past the
invariants an adapter exists to hold (the sparrer's read-only sandbox, most
of all).

Resolution happens before a role's first provider turn and is then pinned
for that provider session in the stage's `state.json` (`agents`, and the
current entry of `sessions`): SEND_BACK cycles, resumes in a new process and
the finalization turn reuse it, so a changed preference applies from the
next stage, or from a deliberately started fresh session for that role, and
can never reconfigure a provider session already under way. Only the roles
about to run are pinned: `run-stage` does not lock the reviewer's
configuration. Provider session resume is untouched by any of this:
the session id is the provider's own, recorded in stage state, and never
carried on an adapter object.

`show-config --json` reports the resolved result and the source of each
value. It exists so that a UI never re-derives provider semantics; the
engine owns the question "what would actually run". It reports configuration
only — never environment contents or credentials. Alongside each role it
reports `provider_choices`, the providers implemented for that role: the
engine also owns the question "what could this run with", so a UI offering a
choice is offering the engine's list rather than its own.

### Writing that file, exactly once

`project.toml` is human-owned: the ownership table says so, and managed-run
agents are instructed not to edit it. A UI that wants inline controls
nevertheless has to change it somehow, and there were two ways to do that.
The extension could learn to write TOML, or the engine could expose a typed
mutation and the extension could ask. The first gives the cockpit a second
opinion about the schema — one that drifts the moment a field is added, and
one that can write a file the engine then refuses to load. So
`agent_sparring/config_edit.py` is the single writer, reached through
`sparring set-config`, and the extension shells out to it.

Typed, not generic, for the same reason there is no `extra_args`: a "set any
key to any value" command is the TOML equivalent of shell injection, and it
would let a UI reach past the invariants an adapter exists to hold. The role
is one of the two that take a turn and the fields are the same closed set
`config.py` parses, so a caller cannot express anything the resolver would
not already have validated.

The order of operations is the load-bearing part. The edit is applied to an
in-memory document, the result is re-parsed, and the role is resolved through
`resolve_role_config` — the very function a run calls — and only then is
anything written. A refusal therefore leaves the file byte-for-byte as it
was, including the case that matters most: an existing file that does not
parse is refused rather than replaced with a clean template, because
discarding someone's half-finished edit is not a repair. The write itself
goes through a temporary file in the destination's own directory and an
`os.replace`, so there is no window in which a run could read half a
configuration.

tomlkit is the engine's one runtime dependency, and it is here for a reason
the standard library cannot serve: `tomllib` reads TOML and cannot write it,
and regenerating the file from parsed values would throw away the comments
and ordering its author chose. Round-tripping means a mutation changes the
line it was asked about and leaves the rest alone. An edit whose result
equals the bytes already on disk writes nothing at all — which is both the
"do not rewrite gratuitously" rule and, incidentally, what makes a
double-clicked control harmless.

Nothing is silently discarded. A provider change that would orphan an effort
already in the file is refused with both values named; the caller can then
set them together in one invocation. Quietly resetting the setting would be
the engine deciding something the person is better placed to decide.

Mutation changes the next turn and nothing else. It does not signal a running
process, does not touch `plans/<run>.json`, and does not alter a captured
prompt: those remain owned by whoever the ownership table says owns them.

Only values that software actually needs belong here.

Do not invent another custom Markdown/front-matter parser for machine state.

## Agent-readable project knowledge

Project-local:

    .sparring/PROJECT.md

This contains project-specific knowledge such as:

- architecture;
- conventions;
- useful commands;
- known test baselines;
- product constraints;
- project-specific subagents;
- common manual checks;
- deployment/testing context.

The generic workflow injects this as agent context but does not interpret its
prose as workflow logic.

Project-specific rules belong here or in the project's own repository plans,
not inside reusable Sparring code or skills.

---

# Stage artifacts

Project-local layout:

    .sparring/
        project.toml
        PROJECT.md

        stages/
            <stage-id>/
                brief.md
                notes.md
                state.json
                handoff.md
                sparring.md
                activity.jsonl

Each file has one responsibility:

brief.md
    Scope and goal for this stage.

notes.md
    Human-readable implementation/evidence/deferred-check notes.

handoff.md
    Latest stage-agent report plus useful Git/test context.

sparring.md
    Human-readable sparring findings and routing outcome.

state.json
    Only minimal machine state needed by the driver.

activity.jsonl
    Append-only observational telemetry. Never read by the driver.

Do not encode project knowledge in state.json.

---

# Activity stream

`activity.jsonl` exists so a person or a UI can watch a stage while it runs.
It is disposable: nothing in orchestration reads it, so deleting, corrupting
or making it unwritable changes no routing, lifecycle, acceptance, resume or
session decision. A write failure is silent and disables further writes for
that log instance; it never raises, prints or changes an exit code. The file
is one of the stage's own artifacts for the acceptance gate's dirty-tree
exemption, so telemetry cannot make a candidate dirty.

One JSON object per line, schema version 1, with a fixed envelope

    {"v": 1, "ts": "<UTC ISO 8601>", "actor": "...", "event": "..."}

and a small closed set of optional fields used only when genuinely known:
`provider`, `session_id`, `model`, `summary`, `action`, `cycle`, `sha`,
`tool`, `path`, `kind`, `exit_code`, `resumed`, `parent_id`, `tool_use_id`,
`role`, `duration_ms`, the resolution fields listed under `agents.resolved`
below, and the budget fields listed under `provider.usage` below.
Anything else passed to the writer is dropped. No prompt text, reasoning,
tool input or output, diff, replacement string or shell command text is ever
recorded.

Actors and events:

    stage    agents.resolved, turn.started, turn.finished, turn.failed,
             handoff.ready (orchestration) and session.observed, tool.call,
             file.changed, command.started, command.finished,
             subagent.started, provider.usage, provider.result (translated
             from the implementation provider)
    sparrer  agents.resolved, sparring.started, sparring.failed, verdict,
             dialogue.started, dialogue.finished, dialogue.failed
             (orchestration) and
             session.observed, tool.call, file.changed, command.started,
             command.finished, subagent.started, provider.usage,
             provider.result, provider.error (translated from the sparring
             provider)
    loop     loop.started, loop.send_back, loop.stopped, loop.runaway
    gate     candidate.frozen, candidate.accepted, gate.refused
    plan     plan.stage.entered, plan.stage.accepted, plan.paused,
             plan.failed, plan.completed, plan.evidence_recorded,
             gate.repeated

`agents.resolved` is written once per role when a stage's adapters are built,
before its first turn, carrying `role`, `provider`, `requested_model`,
`requested_effort` and a `*_source` for each. It is the only durable record
of a selection that came from the environment, which by construction leaves
nothing in the repository (see "Model and effort"). `requested_model` is
deliberately a different field from `model`: `model` is only ever what a
provider said about itself, so the two can be compared and a provider that
ran something other than what it was asked for stays visible.

`turn.finished`, `turn.failed`, `sparring.failed` and `verdict` carry
`duration_ms`: the engine's own monotonic measurement around the provider
call. Unlike the budget fields it is not a provider claim, so it is present
on every outcome including failure.

`plan` lines are written by the plan runner into whichever planned stage's
log is current, in the order things happened: `plan.paused` only for a
routing stop (`NEEDS_YOU`/`ESCALATE`, with `action`), `plan.failed` for a
run that stopped on an error (a fixed phrase, never the exception text),
`plan.evidence_recorded` for the fact that evidence was recorded, never
what it says. Each planned stage has its own `activity.jsonl`; the plan
runner builds the provider adapters per stage so their stream lands in that
stage's file, not the first stage's. None of this is read back: run
position, acceptance, pause, completion, evidence and sessions come from
`.sparring/plans/<key>.json` and each stage's `state.json` alone.

Provider events come from the providers' own structured output, consumed
line by line while the process runs (Claude Code `--output-format
stream-json --verbose`, Codex `--json`). The final provider result is still
parsed exactly as before; the stream is a side channel. A subagent is
recorded only when the provider states one (a Claude `Task`/`Agent` tool
call, a Codex `collab_tool_call` item), never inferred. Model and session
ids are recorded only when the provider output states them.
`session.observed` means the provider stream established this session or
thread identity during this turn; it does not claim the session is new
(Claude Code emits its init line on resumed turns too). Whether a turn
started or resumed a session is what the orchestration's `turn.started`
and `sparring.started` lines say, via `resumed`.

File paths in `file.changed` are repository-relative with `/` separators.
A provider path outside the repository, or one that escapes it via `..`,
is omitted rather than recorded; an absolute path never appears verbatim.

`provider.usage` carries what a provider said about its own budget:
`input_tokens`, `output_tokens`, `total_tokens`, `context_used_tokens`,
`context_window`, and the two rate-limit windows as
`rate_limit_percent`/`rate_limit_window_minutes` and their `_secondary_`
counterparts. Each is written only when that provider's own output states
it; nothing is inferred from a model name, and a reader must render an
absent field as "not stated" rather than as zero.

`context_used_tokens` and `total_tokens` are not interchangeable. The
second is cumulative and only grows; because both CLIs re-send the
conversation on every request it passes `context_window` several times
over in an ordinary session, so only the first is a share of the window.
Occupancy is the latest request's, never a sum over requests: for Claude,
one assistant line's `input_tokens` plus both cache fields plus its
`output_tokens`, skipping subagent lines (`parent_tool_use_id`), whose
conversation is a different and much smaller one; for Codex,
`last_token_usage.total_tokens`.

Codex's `--json` stream states neither its context window nor the
account's rate limits (verified against codex-cli 0.153.4). For the thread
the engine itself started, those are read from the last `token_count`
record of Codex's own rollout file under `CODEX_HOME`. This is a
deliberate, narrow exception to "the provider's stream is the only
source": only that record type's numbers are read, never the prompts,
reasoning or command output in the same file, and any failure to find or
parse it leaves the fields unstated rather than raising.

---

# Handoff

The strongest idea inherited from the predecessor: the sparrer should receive
more than the implementation agent's prose report.

A handoff should make available:

- stage goal;
- implementation-agent claims;
- base/current commit identity;
- branch;
- changed files;
- relevant Git status;
- test/build evidence;
- open/deferred checks;
- previous unresolved sparring findings.

Support two forms.

## Thin handoff

Default when the sparrer has repository access.

Do not dump giant patches unnecessarily. Give identity/context and let the
sparrer inspect actual code.

Suitable for:

- Codex/CLI/local repo-aware sparring;
- GPT web chat with GitHub access.

## Self-contained handoff

Explicit fallback for a sparrer without repository access.

May contain relevant diffs/patches and additional source context.

---

# Provider adapters

Generic orchestration must not assume Claude or Codex semantics everywhere.

Providers sit behind two small adapter protocols, one for the stage agent and
one for the sparrer, each with the same shape:

    start(prompt)               -> fresh session
    resume(session_id, prompt)  -> same session continued

The session id comes from the provider's own machine-readable output, never
from agent prose. A provider that cannot resume raises an error rather than
the caller pretending compatibility exists.

Implemented adapters: Claude Code CLI as the stage agent, Codex CLI as the
sparrer, invoked in the provider's read-only mode and additionally checked
for repository changes afterwards.

Web GPT remains an escalation/manual sparring target rather than something the
local driver assumes it can wake automatically.

---

# Orchestrator

The orchestrator is a small router, not another workflow engine:

    start/resume stage agent
            |
            v
        handoff
            |
            v
       run sparrer
            |
       +----+--------+----------+
       |             |          |
    SEND_BACK      READY     NEEDS_YOU
       |             |          |
       v             v          v
    stage agent   acceptance   stop
                               report

                    ESCALATE
                        |
                        v
                 stop + prepare
                   review packet

The agents decide WHAT needs doing.

The orchestrator only decides WHERE control goes next.

## Plan runner

`run-plan` / `resume-plan` (`agent_sparring.plan`) is the thin loop outside
the router: a reviewed plan's `## Stage <n> — <title>` sections become stages
in order, each with a fresh implementation and sparring session; `READY`
invokes the existing `freeze_candidate` / `accept_candidate` gate on the exact
pushed SHA and the next stage starts; `NEEDS_YOU`, `ESCALATE`, any failure and
the end of the plan stop it. The only state it adds is the run position (plan,
digest of the stage sections, branch, current stage, status) under
`.sparring/plans/`. Human evidence goes into the current stage's `notes.md`
and the same stage resumes. Stage boundaries are a fixed heading convention,
never inferred.

One thing stands between a `READY` candidate and that gate, and it is a
permission rather than a check: the gate requires the commit to be reachable
from its intended remote ref, and a reviewed candidate is often not pushed,
because the project's instructions forbid an agent from pushing unasked.
`agent_sparring.push_gate` makes that a capability instead of an impasse. No
authorization is the default; a verified candidate that is not on the remote
pauses the run with a *typed* reason in the run state (`awaiting.kind =
push_authorization_required`, with the exact commit, branch, remote and
remote branch), which is what lets a consumer present a permission question
instead of mining a reviewer's prose for one. A person's answer is recorded
as data in the same state — bound to one candidate, or to the run — and
survives a reload. An authorized push is one non-force, fully-qualified
refspec of the run's own branch, built in one function; reachability is then
re-proven and the gate itself runs unchanged. Nothing about acceptance is
relaxed by any of it, and no sibling repository is ever pushed.

### Deferred human verification

`NEEDS_YOU` with a `human_gate` means *stop now*, and every check in it must
be done before the stage can be `READY`. That contract is right for a product
choice the next stage builds on, and wrong for "resize the finished dialog and
check it is still readable": expressed as an immediate gate, a cosmetic check
stops the whole implementation/sparring loop the moment it is raised, and an
unattended run cannot get past it.

So there is a second shape, and **which one to use is the reviewer's
judgement, not a rule the engine applies**:

    NEEDS_YOU + human_gate            a person must answer before this stage
                                      can be READY
    READY     + deferred_human_gate   no implementation issue blocks this
                                      stage, a person still owes this
                                      verification, and the reviewer recorded
                                      why continuing first is low risk

Nothing in the engine inspects a check's category, wording or subject to
decide which it should have been; a `UI_VISUAL_CHECK` is immediate when the
reviewer says so and deferred when the reviewer says so. The sparring prompt
gives the reviewer the questions to reason with (does the answer change what
subsequent implementation should be; would later work be expensive or
misleading if it failed; is the correction local and bounded), and the four
categorically non-deferrable boundaries — irreversible/destructive approval,
production release authorization, a credential or security boundary, and an
explicit plan requirement for human approval before proceeding.

What the engine owns is the part a reviewer cannot do for itself
(`agent_sparring.deferred_gate`):

- **rationale or refusal.** A deferral without a reviewer-authored rationale
  is an invalid verdict. It is the only record of why an unattended run was
  allowed to continue, and the human, the next reviewer and anyone debugging
  the decision all read it.
- **real asking identity.** A deferred gate is minted an `instance_id` in
  `record_sparring`, exactly like an immediate one and in the same place, so
  an answer to an earlier asking can never satisfy a later one. Deferring,
  carrying across stages and later promoting are all the *same* asking; a
  reviewer that materially reissues writes a new gate and gets a new instance.
- **a durable ledger.** Obligations live on `PlanRunState.deferred_human_checks`
  and nowhere else. A stage's `sparring.md` is rewritten by every `SEND_BACK`
  cycle and an obligation deliberately outlives the stage that raised it; the
  thing that must refuse to finish while one is open is the plan run, which is
  what that file is the state of. An entry is keyed by its gate instance, so
  re-entering an accepted stage records nothing new.
- **a plan may not complete while one is open.** When every executable stage
  is accepted and anything is still owed, the run pauses with a typed reason
  (`awaiting.kind = deferred_verification_required`) instead of reporting
  `complete` — one coherent checkpoint for everything that accumulated, rather
  than four separate interruptions. Answering it completes the plan without
  re-running anything already accepted, because the loop skips accepted stages.
- **a checkpoint value, not a checkpoint convention.** v1 implements
  `before_plan_completion` only, and refuses any other value rather than
  storing a deadline nothing honours; the field exists so a later
  `before_stage:<id>` is a new value and not a new format.

Answers arrive as data, not prose: `resume-plan --deferred-result
'<check>=<pass|fail|blocked>[=<note>]'`, refused unless the run is in fact
stopped on that asking, and addressed as `<gate instance>:<check id>` whenever
a bare id would be ambiguous. Each result is written in two places, neither
derived from the other — the ledger, which decides completion, and the
*originating* stage's `notes.md`, in the same line shape a human-gate answer
uses, so both agents see it on their next turn and provenance survives an
obligation raised by stage 2 and answered after stage 7.

`blocked` resolves nothing. "I could not test it" is not a result, and a plan
completed on one would be a plan completed on a verification nobody made. A
`fail` keeps the plan stopped too, and the engine deliberately does **not**
rewind: un-accepting a stage is a real integrity action that belongs to a
person. What it guarantees is that the plan does not finish, that the failure
is durable, and that it is where the correcting agents will read it.

Deferred is not permanently deferred. Every sparring turn of a managed run is
shown the ledger and may name an entry in `promote_deferred`; the engine then
stops the run before the next stage under the *same* asking. The engine never
promotes anything on its own initiative and never re-evaluates a reviewer's
timing judgement.

A stage accepted with an open obligation is *accepted and still owes a check*,
and both halves are reported. Acceptance copy never claims everything was
verified when something was deliberately deferred.

### A gate a person cannot answer

`blocked` resolving nothing has a consequence on the immediate side too, and
it produced a real deadlock before it was closed. A reviewer that finds no
implementation defect, holding acceptance checks a person has answered
`Blocked`, has no legal action but `NEEDS_YOU` — `READY` is barred by the
unsatisfied checks and `deferred_human_gate` is legal only with `READY`. So it
re-issues the same gate. The person answers `Blocked` again, for the same
structural reason ("I can test this once the candidate is frozen"), and the
exchange repeats. Nothing spun on its own; every round cost a person real time
instead.

Three things close it, and the split matters:

- **Answers are read as data.** `agent_sparring.gate_answers` parses the
  `Pass|Fail|Blocked — … · check … · gate …` lines a UI writes under
  `## Human evidence` back into results per check id and gate instance. It is
  a reader of prose, not a format: `resume-plan --evidence` still takes free
  text, nothing unparseable is rejected, and no outcome is ever inferred from
  a line's wording.
- **The reviewer is told what it cannot see from one turn.** The prompt
  carries the engine's own tally — latest outcome per check, and how many
  askings a person has answered — plus the routes out of a `Blocked` check.
  The usual one is `READY` with the check moved to `deferred_human_gate`: the
  ledger still refuses to complete the plan, so nothing is waived. The
  never-deferrable list is narrowed by one clarification, because it was being
  read as a prohibition it never was: a check the *plan* defines as part of a
  stage's acceptance is not thereby undeferrable. What rules deferral out is
  later work depending on the answer.
- **The run stops instead of the person.** A gate that is *wholly* checks
  already answered `Blocked` twice stops the run (`plan.failed`, still
  resumable) rather than pausing for the same question a third time; the
  message names the routes that change what the next turn sees. A gate that
  merely carries such a check alongside ones the reviewer is still working is
  reported, never stopped — that reviewer is making progress, and stopping it
  would stop the work. Both emit `gate.repeated`.
- **A `Blocked` check cannot be waived by omission.** Told that deferring was
  the way past a Blocked check, the first reviewer to see that guidance chose
  `READY`, reasoned correctly about all three of its checks in `findings`, and
  wrote no `deferred_human_gate` — dropping two checks a person had answered
  Blocked twice. Nobody decided that; it is what falls out of doing half of
  the instruction. So `_carry_blocked_checks` records those checks as an
  obligation at acceptance if no deferral covers them. Its `instance_id` is
  derived from the stage and the check ids, not minted, because reconciliation
  runs on every pass over an accepted stage. This is bookkeeping and not a
  timing judgement: the rationale says in as many words that the engine
  carried it and no reviewer weighed it, and only an explicit `Blocked` is
  carried — a `Fail` is a result a reviewer may accept a stage over, and a
  check nobody answered may have been overtaken by the code.

The runner takes an adapter factory (`make_adapters(stage)`) rather than
finished adapters and calls it once per stage it enters, so each planned
stage's provider telemetry is bound to that stage's own `activity.jsonl`;
the runner itself never learns which providers are behind it. Session
continuity comes from the ids in `state.json` passed to `resume`, not from
keeping an adapter object alive across stages or processes. The runner's
own `plan.*` activity lines (see "Activity stream") are a chronological
mirror, never an input.

---

# Run worktrees (design; not implemented)

The intended flow is *pick a plan → run → watch → merge → clean up*, with
branches and worktrees as implementation details a person never has to
manage. This section fixes who owns them and the invariants any
implementation must keep. Nothing here is implemented yet; the first slices
are listed at the end.

**Today.** The engine never creates a worktree. People and agents create
them; the engine only checks that a run is where it was approved — intake
approval binds a slice to the canonical worktree path and git directory, and
resume repeats that check — and holds one implementation writer per
worktree (`concurrency.py`). Nothing records which worktree belongs to which
run. The VS Code extension finds runs in sibling worktrees by reading `git
worktree list` (read-only) and shows them; it does not own them either.

## Ownership: the engine creates, records and removes

- Only the engine creates or removes a *managed* worktree, and only through
  explicit commands. A worktree it did not create is never a cleanup
  candidate, whatever its name or branch.
- Every managed worktree has a record in the repository's **git common
  directory** — `<git-common-dir>/agent-sparring/worktrees/<run-key>.json`,
  next to the migration history and for the same reason: every worktree of
  the repository must see it, and no worktree's checkout owns it. The
  record holds the run key (and intake/slice when there is one), the
  canonical path, the branch, the base commit, the target branch it will be
  merged into, `created_at` and `created_by: "engine"`. It is the only
  association between a run and a worktree; clients read it and never
  infer one from directory or branch names.
- A record is written before the worktree is used and updated, never
  rewritten, as the run finishes and as cleanup runs. A record whose
  directory has disappeared is reported, never silently dropped.

## Creation: before preparation, because approval binds the path

Intake approval records the primary worktree's canonical path, so isolation
has to happen **before** `prepare`/`approve`, not by moving an approved run:

- `start-plan` (the normal path) gains an opt-in to run in a new managed
  worktree. It creates the worktree at the current HEAD on a new branch,
  writes the record, and then prepares and approves **inside** that
  worktree, exactly as if the person had run `start-plan` there. The
  existing approval checks then apply unchanged.
- Names are automatic and deterministic from the plan and run key: the
  directory is a sibling of the main worktree, `<repo>-sparring-<run-key>`,
  and the branch is `sparring/<plan-slug>-<short-run-key>`. A collision is
  refused, never resolved by reusing or overwriting; a branch name is never
  reused for a different run.
- The target branch is recorded at creation: the branch checked out where
  `start-plan` was invoked. It is not inferred later.
- First slice: single-repository runs only. A run with declared sibling
  repositories is refused isolation until sibling worktrees have the same
  ownership rules.

## Finish: one engine command, dry run first

Merging and cleanup are one explicit engine command over one run, for
example `sparring finish-run <run-key> [--merge] [--push] [--dry-run] [--json]`.
`--dry-run --json` reports every check below with pass/fail and the reason;
without `--dry-run` it performs only what the checks allow, in order, and
stops at the first refusal. Every check is re-made at execution time; a dry
run is advice, never a token.

A managed worktree may be removed only when **all** of these hold:

1. the record exists and says the engine created it;
2. the plan run is `complete` with every stage accepted, nothing in
   `awaiting`, and no open deferred human verification;
3. no runner holds the worktree (the concurrency lock is free and no live
   process is recorded for it);
4. the worktree is clean: no modified, staged or untracked non-ignored
   files;
5. the branch tip is the run's accepted final candidate;
6. the target branch contains that tip (ancestry, not message or patch
   matching). With `--merge`, the engine first merges into the target —
   fast-forward, or a merge commit if the project allows it — from a clean
   target worktree; it never rebases or rewrites either branch;
7. when the repository has a remote, the commit the target now points at is
   reachable on it. With `--push`, the engine pushes the target first,
   under the existing push rules; it never force-pushes;
8. no other worktree has the branch checked out.

Removal is then `git worktree remove` without `--force`, followed by
deleting the branch with `git branch -d` (never `-D`), and marking the
record finished. A failure part-way leaves everything that was not yet
removed in place and says what remains.

**Never automatic.** Nothing is removed on completion, on a timer or on
window close. A dirty, paused, unaccepted, unmerged or unpushed worktree is
never removed, and no engine path uses `--force`, `-D` or a force push.
Abandoning a run is a separate, explicitly confirmed action that keeps the
branch.

## Prunable engine state

Engine-owned state that no run still uses — intake directories of
superseded or finished intakes, stage directories of runs whose worktree
was removed, manifest bindings for removed worktrees, worktree records whose
directory is gone, migration history beyond its retention — is **reported**
by a read-only `sparring prune --dry-run --json`, each item with why it is
believed unused. Deleting any of it is a later, separate decision.

## Clients

A client (the VS Code extension) reads the records and the run state, shows
*Merge & clean up* only when `finish-run --dry-run --json` reports the run
eligible, shows that report in the confirmation, and runs the command the
person confirmed. It never runs git itself to merge or remove, never
decides eligibility, and keeps branch and worktree details under
diagnostics. Opening a managed worktree in a window is offered, never
required.

## First slices

1. The worktree record format and `start-plan` isolation for
   single-repository runs (creation only).
2. `finish-run --dry-run --json`: every check above, read-only.
3. `finish-run` execution: merge, push and removal under those checks.
4. `prune --dry-run --json`.

---

# History

The tool was built in eight bounded stages during September 2026, each one
implemented by a stage agent and checked by an independent sparring agent
before the next began:

1. generic skeleton: config loading, stage artifacts, routing outcomes;
2. handoff generation and the `sparring.md` exchange, thin and self-contained
   packets, Git context;
3. a resumable stage-agent adapter (Claude Code CLI);
4. a resumable sparring adapter (Codex CLI), with manual and web sparring
   still supported;
5. the unattended `run-loop` router with loop limits;
6. the acceptance gate: `freeze-candidate` and `accept-candidate` pinned to an
   exact pushed SHA;
7. a real-project pilot that exercised READY, SEND_BACK, NEEDS_YOU, same-SHA
   reconsideration/acceptance and project-specific subagents, and verified
   the ESCALATE self-contained packet mechanism (no genuine ESCALATE verdict
   occurred);
8. retirement of the project-specific predecessor workflow this tool replaced.

The predecessor was reference material, not the architecture. Its exact-session
handoff capture, Git and test context gathering, packet concepts and pushed
candidate verification carried over as ideas. Its durable review states,
review-attempt machinery, front-matter workflow state and large prose contract
tests deliberately did not.

Per-stage handoff and review records are working artifacts. They live in
`.sparring/stages/` in the project being worked on, not in this repository.

---

# Success criterion

The important measure is not line count.

The system succeeds when this requires no human relay:

    implementation agent finishes
    sparrer finds a bounded issue
    implementation agent fixes it
    sparrer checks again
    another issue is fixed
    sparrer says READY

and the human appears only when there is an actual human decision/check or an
intentional escalation.

The workflow code should remain much easier to understand than the application
code it coordinates.