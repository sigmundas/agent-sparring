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

Do not encode project knowledge in state.json.

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