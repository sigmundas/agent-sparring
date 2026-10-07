---
name: sparring-plan
description: Turn the current discussion (an audit, design conversation or bug investigation) into a staged agent-sparring plan under docs/plans/, grounded in the repository's real code, and validate it with the engine without running it. Invoke explicitly; writing a plan never approves or starts it.
argument-hint: "[plan name or emphasis]"
disable-model-invocation: true
allowed-tools: Read Grep Glob Write Edit Bash(sparring check-plan:*) Bash(sparring check-config:*) Bash(git status:*) Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(git ls-files:*) Bash(git rev-parse:*) Bash(git branch:*)
---

# Write a staged plan

The input is the conversation so far: findings, decisions, constraints and
open questions. The output is one Markdown plan that the engine can run stage
by stage, and that a person will read and approve before anything runs.
`$ARGUMENTS` may name the plan or say what to emphasise.

This skill writes and checks a file. It does **not** approve the plan, create
a branch, commit anything, or start a run. Producing a plan is not a signal
to begin.

## 1. Collect what was decided

List what the discussion actually settled, and separately what is still open.
Leave out ideas the discussion rejected.

An existing plan is evidence of prior intent, not proof that every choice in
it is an approved requirement: preserve the contract; re-evaluate the recipe.
Carry forward its required outcomes, safety and correctness invariants,
explicit product decisions, approved architectural constraints, and project or
external requirements, verified against current code (§2). Unless explicitly
approved as requirements, re-evaluate module, class and file layout, extraction
sequences, stage boundaries, convenience ordering, commit choreography and
labels, temporary or verifier tooling, test-seam strategy, historical
workarounds, audit procedures and estimates. Keep such structure only where a
proven invariant or approved architecture depends on it.

Classify open uncertainty before finalizing the plan:

- Resolve cheap factual uncertainty by inspecting the repository.
- Resolve product or scope questions that change what should be built before
  plan approval; ask the person rather than guessing.
- For an unresolved technical question, ask: would two reasonable answers
  change a downstream acceptance contract, the user or product outcome, a
  repository boundary, a required external action, stage dependencies or
  topology, or which work must exist? If yes, resolve it before approving the
  affected downstream stages; do not write speculative stages behind an
  investigation that may invalidate them. If no, it is implementation freedom
  or bounded investigation within its stage; state the constraints on that
  decision where they matter. Names, helper decomposition, internal API or
  type shapes, module layout and edit sequence are not consequential by
  default; they become so only when the choice changes what downstream work
  must deliver or how it is accepted.

A recommended or default answer is still an unanswered decision. If either
reasonable answer would change a written stage's scope, required files or
artifacts, behavior, hard constraints, acceptance evidence, repositories or
topology, settle it before presenting that stage as ready for approval; choosing
a default does not turn it into an assumption. Questions that alter no stage
contract, such as whether to track an excluded, unrelated issue separately,
optional follow-up, or execution and publishing preferences, need not block
approval.

Agent Sparring has no generic research lifecycle. If a managed investigation
is appropriate, plan a durable, independently reviewable deliverable (for
example, an evidence-backed decision document or a bounded prototype with
recorded results). Approve that result on its own; leave downstream planning
until its consequential uncertainty is resolved. This is pre-execution
planning, not dynamic replanning of a live run.

## 2. Ground it in the repository

Read `.sparring/PROJECT.md` if it exists, then the code the work touches.
Search by symbol and read bounded ranges. Every file, function, command and
test you name in the plan must exist, or be explicitly marked as new. Verify
baseline facts rather than inheriting old plan claims; identify anything that
must be rechecked at execution time. Record the relevant test command and
current baseline if PROJECT.md doesn't already.

## 3. Choose coherent acceptance boundaries

A stage is one coherent result that an independent reviewer can judge and
the engine can accept as a candidate. Choose boundaries by uncertainty and
risk, the boundaries crossed (persistence, auth, concurrency, processes,
repositories, migrations or production state), independent verifiability,
materially different consequences of failure, and human or external gates
that must occur before later work. Order stages so each can meet its own
contract using accepted prerequisites, without later work making it complete.
A single stage is appropriate when the whole change has one acceptance boundary.

A stage must be both conceptually coherent and practically reviewable. Ask
whether one fresh reviewer, receiving this candidate and its evidence, can
realistically establish that the contract holds. Split when the changed
surface, interactions, independent risks or evidence burden become too broad
for confident review, even when the work shares one architectural theme.

Do not split merely by file, architectural layer, implementation versus tests,
to reduce diff size, because implementation naturally has several steps, or on
an assumption that smaller is safer. Keep the implementation and evidence
for a coherent behavior together. Separate a refactor only when it has an
independently meaningful contract and can safely be accepted on its own; a
refactor solely enabling one behavior change may belong in that same stage.

Describe contracts rather than implementation recipes: the changed outcome,
invariants, boundaries and non-goals, acceptance evidence, and consequential
implementation constraints already decided. Distinguish:

- **Hard constraints:** requirements of the approved design.
- **Starting points:** verified files, symbols and tests to inspect. These
  are not an exhaustive edit allow-list unless explicitly intended as one.
- **Implementation freedom:** decisions left to the stage agent within the
  contract, including technical investigation that cannot change its outcome.

Implementation freedom cannot weaken required invariants, safety or correctness
properties, hard constraints or required acceptance evidence, even if visible
behavior is unchanged.

Do not prescribe classes, helpers, abstractions, edit sequences or internal
structure unless they are themselves consequential approved requirements.

### Fit the execution route

Direct Markdown execution is sequential: the previous stage must be accepted
before the next starts. Prose does not create a machine-enforced dependency
graph. Use **Prerequisites / ordering rationale** when explanation helps;
state the accepted result a stage needs, not a promise of runtime enforcement.

A direct Markdown plan declares neither sibling candidates nor review-only
mode nor plan-level external gates. For those needs, write an intake-oriented
plan and hand it over for [plan intake](../../docs/intake.md) or an explicit
[execution manifest](../../docs/plans.md#marking-stages-in-a-plan--or-handing-over-an-execution-manifest).
`start-plan` chooses the direct route whenever the stage headings parse; prose
about repositories, gates or review does not force intake. Use non-direct
labels such as `1A` when the plan requires intake.

For cross-repository work, name the primary repository for each run slice and
how coupled candidates relate. A sibling declaration pins a reviewed candidate;
it does not move implementation into that repository or coordinate merges.
A review-only stage requires manifest `mode: independent_review`; a title
alone still runs implementation. Add a separate review stage only when it
serves a meaningful acceptance purpose beyond each stage's normal review.

A plan defines which candidate is acceptable; local isolation and publication
or integration policy are separate concerns. Merge cadence, pull request versus
direct merge, squash policy, branch naming and merge queues belong to
execution, repository or team policy, not plan design, unless the requested
task concerns them. State a Git constraint only when correctness or evidence
depends on it, for example keeping a candidate based on a particular reviewed
commit, not rebasing onto unrelated changes during a sensitive migration, or
not force-pushing a reviewed branch.

Keep implementation separate from production/manual actions. Real external
prerequisites need supported mechanisms: intake prerequisites confirmed at
run-slice approval, or manifest v2 `gates_before` / `completion_gates` emitted
by compile intake or declared explicitly. Say what each gate blocks and what
evidence satisfies it; document order or a prose dependency is insufficient.
Human checks are only for verification agents cannot adequately perform,
not merely checks a read-only reviewer cannot run. Consult
[human gates](../../docs/gates.md) for immediate checks and
[deferred verification](../../docs/plans.md#a-check-that-is-owed-but-not-now)
for checks still owed. Do not invent gates for automatable checks or move
unresolved product scope into execution.

## 4. Write a lightweight, self-contained plan

Write `docs/plans/<slug>.md` (don't overwrite an existing file; pick a new
name). For a direct Markdown plan, use this lightweight shape:

```markdown
# <Plan title>

<Summary for the person approving the plan.>

## Stage 1 — <Title>

**Outcome:** the behavior or result this stage delivers.
**Boundaries / non-goals:** the scope of that result and what is excluded.
**Acceptance evidence:** concrete observations, tests or commands that
demonstrate the outcome and required invariants, with expected results.

## Stage 2 — <Title>
...
```

These are essential concepts, not mandatory field labels. Add invariants,
hard constraints, starting points, implementation freedom, prerequisites /
ordering rationale, uncertainty or human checks only where they materially
clarify the stage. For a necessary human check, state the action, pass criteria
and whether later work needs its answer before proceeding, or verification is
still owed without a downstream implementation dependency. A reviewer may
accept a stage while retaining a deferred human verification obligation that
must be satisfied before plan completion. Do not decide or promise deferral
in the plan: timing remains a reviewer/runtime decision. A manual, device or
visual check alone does not require an immediate boundary. Later production
actions belong to their own supported gates, not that stage's acceptance checks.

Durable project-wide knowledge belongs in `.sparring/PROJECT.md`, separately
supplied to stage agents and reviewers as normal project context. Direct
Markdown stage agents receive only their own stage section plus that normal
context: prose above the first stage heading is not sent to them. Put every
plan-specific invariant or constraint required
by a stage inside that stage, even when the person-facing summary states it
too. Refer to accepted prerequisite results explicitly without relying on the
agent receiving earlier or later sections.

Structural rules the engine enforces for direct Markdown:

- Headings are exactly `## Stage <n> — <title>`, numbered 1..N in order
  (an en dash, hyphen or colon also works as the separator).
- Every stage has content. Deeper headings (`###`) belong to the stage.
- The section ends at the next `#` or `##` heading; only that section becomes
  its brief. Keep each stage self-contained.

Direct Markdown's execution digest binds parsed stage numbers, titles and
sections, not out-of-stage prose. Do not edit an actively executing plan to
change its contracts; changed executable contracts need a newly reviewed
plan/run identity, not dynamic replanning or rewriting engine state.

## 5. Challenge the draft semantically

Before `check-plan`, review the draft as if implementing and independently
reviewing each stage with only its own brief and normal project context. Use
the intake quality vocabulary where useful:

- Missing acceptance criteria, requirements with no corresponding acceptance
  evidence, and omitted global context.
- Dependency/order mismatch; stages needing later stages to become internally
  complete; arbitrary boundaries; stages too broad for one coherent candidate
  or for one fresh reviewer to confidently establish their contract; adjacent
  stages that would form a clearer single acceptance boundary.
- Implementation mixed with production/manual action; cross-repository
  ambiguity or a missing candidate strategy; unnecessary human gates.
- Repeated or contradictory requirements (distinguish necessary shared
  invariants from redundant instructions), stale baseline facts, open questions
  presented as instructions or settled by a default, and ambiguous source or
  unresolved decisions.
- Unnecessary implementation prescription, including recipe inherited from an
  earlier plan or Git workflow presented as a candidate contract, or
  starting-point suggestions accidentally written as hard scope restrictions.

Revise the draft when this review finds a problem. Resolve consequential open
questions before downstream approval rather than turning them into agent
instructions. Structural validation cannot establish plan quality.

## 6. Validate without running

```sh
sparring check-plan docs/plans/<slug>.md
```

`check-plan` is deterministic structural validation: it lists the direct
Markdown stages the engine will see, runs nothing and spends no model turn.
For a direct plan, fix structural errors and re-check until it passes.

For an intentionally intake-oriented plan, run the check and report that the
non-direct headings require intake; do not relabel it just to make the check
pass and silently lose required execution semantics. `check-plan` does not
validate an intake interpretation or enforce prose prerequisites. Do not run
`start-plan` or `prepare-plan` from this skill: they can start an agent turn.
Execution preparation belongs to `/agent-sparring:sparring-run`.

## 7. Hand over

Report the path, the stage list, the required execution route and validation
result, and any unresolved questions with their effect on approval. Do not
present downstream work as ready for approval while consequential questions
remain. Say plainly that the plan still needs the person's review, and that
running it is a separate step (`/agent-sparring:sparring-run` or VS Code's
**Run Plan**). Leave the file uncommitted unless asked.
