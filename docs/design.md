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

`[agents.<role>]` also carries optional `model` and `effort`. Resolution for
every role and every field lives in exactly one module,
`agent_sparring/agent_config.py`, and every orchestration path — `run-stage`,
`run-sparring`, `run-loop`, `run-plan`, `resume-plan`, the independent-review
stage and the finalization turn — builds its adapters from it. The
alternative, each command consulting the config for itself, is how one path
ends up quietly ignoring it.

    explicit CLI override  >  .sparring/project.toml  >  provider default

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

Resolution happens when a turn is launched and is re-read per stage, so
editing `project.toml` affects the next turn and cannot reconfigure or
restart one in flight. Provider session resume is untouched by any of this:
the session id is the provider's own, recorded in stage state, and never
carried on an adapter object.

`show-config --json` reports the resolved result and the source of each
value. It exists so that a UI never re-derives provider semantics; the
engine owns the question "what would actually run". It reports configuration
only — never environment contents or credentials.

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
`tool`, `path`, `kind`, `exit_code`, `resumed`, `parent_id`, `tool_use_id`.
Anything else passed to the writer is dropped. No prompt text, reasoning,
tool input or output, diff, replacement string or shell command text is ever
recorded.

Actors and events:

    stage    turn.started, turn.finished, turn.failed, handoff.ready
             (orchestration) and session.observed, tool.call, file.changed,
             command.started, command.finished, subagent.started,
             provider.result (translated from the implementation provider)
    sparrer  sparring.started, sparring.failed, verdict (orchestration) and
             session.observed, tool.call, file.changed, command.started,
             command.finished, subagent.started, provider.result,
             provider.error (translated from the sparring provider)
    loop     loop.started, loop.send_back, loop.stopped, loop.runaway
    gate     candidate.frozen, candidate.accepted, gate.refused
    plan     plan.stage.entered, plan.stage.accepted, plan.paused,
             plan.failed, plan.completed, plan.evidence_recorded

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

The runner takes an adapter factory (`make_adapters(stage)`) rather than
finished adapters and calls it once per stage it enters, so each planned
stage's provider telemetry is bound to that stage's own `activity.jsonl`;
the runner itself never learns which providers are behind it. Session
continuity comes from the ids in `state.json` passed to `resume`, not from
keeping an adapter object alive across stages or processes. The runner's
own `plan.*` activity lines (see "Activity stream") are a chronological
mirror, never an input.

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