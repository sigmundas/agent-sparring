"""Who owns each managed-run artifact, and the prompt section that says so.

Every file a managed run reads or writes has exactly one authority. This
module is the production declaration of that: :data:`ARTIFACTS` is what the
tests are written against, and :func:`ownership_section` is the single source
of the sentence the providers are told it in -- so a stage prompt and a
sparring prompt cannot drift into describing different contracts. The
ownership table in docs/reference.md is the corresponding human-readable summary. It is
hand-written, nothing renders or checks it against this module, so keeping
the two consistent is a manual step when either changes.

Two facts are worth stating plainly, because both are easy to assume
wrongly:

- **No managed-run artifact is provider-writable.** Not ``notes.md``,
  despite its "Implementation notes" template heading. The engine is the
  only writer of every file under ``.sparring/``; an implementation agent's
  claims, test evidence and deferred checks reach the sparrer because the
  engine captures that turn's *result* into ``handoff.md`` (see
  :mod:`agent_sparring.handoff`), and a human's answer reaches both agents
  because ``resume-plan --evidence`` records it under ``notes.md``'s
  ``## Human evidence`` heading (see :mod:`agent_sparring.plan`). Neither
  path asks a provider to open a file. :data:`PROVIDER_WRITABLE` is
  therefore empty, and it is empty deliberately rather than by omission.

- **The source plan is immutable for the lifetime of the run.** It is the
  human's document and the run's execution definition at once, which is
  exactly why a provider must not touch it: the engine re-reads it and
  re-checks its digest on resume, before accepting a candidate and before
  advancing (see :func:`agent_sparring.plan._verify_plan_unchanged`), so an
  edited plan stops the run rather than becoming the new definition.
  Appending an implementation record to it is not a harmless annotation --
  a ``# Implementation record`` section holding a second ``## Stage 1``
  makes the document unparseable as the plan it started as.

The prompt section below exists because that contract belongs to Agent
Sparring and has to travel with the managed prompt. A consuming project's
own agent instructions ordinarily -- and correctly -- tell agents to keep
the active plan updated with their progress; a managed run is the one
context where that is wrong, and the run is the only party that knows it is
a managed run.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The prompt roles the ownership instruction is worded for. An
#: implementation turn is told where its progress reporting *does* belong; a
#: sparring turn is told the same about its verdict.
ROLE_STAGE = "stage"
ROLE_SPARRER = "sparrer"


@dataclass(frozen=True)
class ArtifactOwnership:
    """One managed-run artifact's sole authority.

    ``path`` is written as it appears relative to the project's
    ``.sparring`` directory, except for :data:`SOURCE_PLAN`, which is a
    repository document and not a sparring artifact at all.

    ``owner`` is the single party allowed to write it. ``lifetime`` says
    whether it is immutable, replaced in full, or appended to -- the thing a
    reader actually needs in order to know what they may rely on.
    """

    path: str
    owner: str
    provider_writable: bool
    lifetime: str
    purpose: str


#: The human's plan document. Named separately because several places need
#: to refer to it and it is not under ``.sparring/``.
SOURCE_PLAN = ArtifactOwnership(
    path="the plan document (e.g. docs/plans/active/<plan>.md)",
    owner="human / repository",
    provider_writable=False,
    lifetime="immutable for the lifetime of a managed run",
    purpose=(
        "the run's execution definition; its stage sections are digested at "
        "run start and re-checked before every acceptance and advance"
    ),
)

#: Every artifact a managed run reads or writes, in the order a reader meets
#: them. The ownership table in docs/reference.md summarises this tuple for a
#: reader and is maintained by hand; it is not generated from it.
ARTIFACTS: tuple[ArtifactOwnership, ...] = (
    SOURCE_PLAN,
    ArtifactOwnership(
        path="PROJECT.md",
        owner="human / repository",
        provider_writable=False,
        lifetime="edited between runs by a person",
        purpose="project context embedded verbatim in every provider prompt",
    ),
    ArtifactOwnership(
        path="project.toml",
        owner="human / repository",
        # Still false, and the distinction is the point: an agent taking part
        # in a run cannot write this file. "sparring set-config" can, but it
        # is a person's own command -- run from a terminal or from the
        # cockpit's controls -- never something a managed run invokes.
        provider_writable=False,
        lifetime=(
            "edited between runs by a person, by hand or through "
            "'sparring set-config' on their behalf"
        ),
        purpose=(
            "provider selection and engine configuration (model and effort are "
            "the person's own preferences, outside the repository)"
        ),
    ),
    ArtifactOwnership(
        path="plans/<run>.json",
        owner="engine",
        provider_writable=False,
        lifetime="rewritten on every position/status change",
        purpose="the run's position, expected branch and plan digest",
    ),
    ArtifactOwnership(
        path="intake/<intake>/",
        owner="engine",
        provider_writable=False,
        lifetime=(
            "written once by 'sparring prepare-plan' (the intake agent's answer is its "
            "structured result, recorded by the engine); never rewritten"
        ),
        purpose=(
            "a reviewable interpretation of a human plan -- source snapshot, findings, "
            "run slices, exact briefs, repository snapshots, amendment diff, report; "
            "approval binds its exact bytes, so editing it refuses an approved run"
        ),
    ),
    ArtifactOwnership(
        path="intake/<intake>/runs/<run>/",
        owner="engine ('sparring approve-plan', on a person's explicit decision)",
        provider_writable=False,
        lifetime=(
            "manifest.json may be replaced until approval.json exists; approval.json is "
            "created exactly once and never rewritten"
        ),
        purpose=(
            "one approved run slice: the intake manifest envelope and the approval that "
            "binds its exact bytes, inputs, starting repository state and prerequisite "
            "evidence; 'run-plan --manifest' runs it only through that approval"
        ),
    ),
    ArtifactOwnership(
        path="intake/registry/<run>.json",
        owner="engine ('sparring approve-plan')",
        provider_writable=False,
        lifetime="written at approval in the project that runs the slice",
        purpose=(
            "the run key and stage ids an approved slice owns, so a plain manifest or "
            "plan reusing them is refused rather than run unapproved"
        ),
    ),
    ArtifactOwnership(
        path="stages/<stage>/brief.md",
        owner="engine (a person, for a hand-written stage)",
        provider_writable=False,
        lifetime=(
            "managed-plan stage: generated from the plan section, then immutable; "
            "hand-written stage: authored by a person before execution, then immutable"
        ),
        purpose=(
            "the contract the stage is reviewed against -- the plan section "
            "verbatim in a managed run"
        ),
    ),
    ArtifactOwnership(
        path="stages/<stage>/notes.md",
        owner="engine (and a person editing by hand)",
        provider_writable=False,
        lifetime="skeleton at creation, then appended to by section",
        purpose=(
            "a human's recorded answer or manual-check results "
            "(## Human evidence)"
        ),
    ),
    ArtifactOwnership(
        path="stages/<stage>/handoff.md",
        owner="engine",
        provider_writable=False,
        lifetime="regenerated in full by every implementation turn",
        purpose="the implementation turn's claims, git identity and evidence, for the sparrer",
    ),
    ArtifactOwnership(
        path="stages/<stage>/sparring.md",
        owner="engine",
        provider_writable=False,
        lifetime="rewritten in full by every sparring exchange",
        purpose="the latest verdict and findings, rendered from the structured routing result",
    ),
    ArtifactOwnership(
        path="stages/<stage>/state.json",
        owner="engine",
        provider_writable=False,
        lifetime="rewritten on every lifecycle change",
        purpose="stage status, candidate identity and provider session ids",
    ),
    ArtifactOwnership(
        path="stages/<stage>/dialogue.jsonl",
        owner="engine",
        provider_writable=False,
        lifetime="append-only; one record per human question and reviewer answer",
        purpose=(
            "provenance for the read-only side conversation a person holds "
            "with the reviewer ('sparring ask'), which otherwise exists only "
            "inside the provider's own thread"
        ),
    ),
    ArtifactOwnership(
        path="stages/<stage>/activity.jsonl",
        owner="engine",
        provider_writable=False,
        lifetime="append-only; never read by orchestration",
        purpose="observational telemetry only",
    ),
)

#: The managed-run artifacts a provider may write. Empty, by design: see
#: this module's docstring. A test holds it empty so that adding a
#: provider-write surface has to be a deliberate, reviewed change to this
#: declaration rather than a side effect of a new prompt sentence.
PROVIDER_WRITABLE: tuple[ArtifactOwnership, ...] = tuple(
    artifact for artifact in ARTIFACTS if artifact.provider_writable
)


_HEADING = "## Agent Sparring artifacts"

# Deliberately not "the plan document named in the brief above": the same
# builders assemble prompts for a hand-written single stage (`run-stage`),
# whose brief names no plan. Naming one conditionally would mean inferring
# from the brief's prose whether this turn belongs to a plan run, and the
# engine does not read briefs to decide things. The wording below is true
# for every turn, and the prohibition is correct for every turn.
_PLAN_IS_INPUT = (
    "Any plan document this stage's brief comes from or refers to is "
    "read-only input:"
)

_PLAN_RULES = (
    "- do not edit or annotate it;",
    "- do not append an implementation record, progress log or handoff to it;",
    "- do not mark stages complete in it, renumber stages, or add stage headings.",
)

_PLAN_WHY = (
    "Stage progression and acceptance are the engine's, recorded in its own "
    "state -- not in the plan. The engine re-reads the plan and compares it "
    "against the run it started, so editing it stops this run instead of "
    "redefining it."
)

_ENGINE_OWNED = (
    "Everything under `.sparring/` -- briefs, notes, handoffs, sparring "
    "exchanges and stage state -- is written by the engine. Do not create or "
    "edit any of it. No Agent Sparring artifact is yours to write unless this "
    "prompt says so by name, and none does."
)

_STAGE_REPORTING = (
    "Report what you implemented, what you claim is true, your test evidence "
    "and anything you deferred in this turn's own reply. That reply is what "
    "the engine records for the sparrer; it is the designated place for your "
    "implementation and progress reporting."
)

_SPARRING_REPORTING = (
    "Report everything -- findings, verdict, any human gate and anything "
    "deferred -- in the structured result this prompt asks you for. The "
    "engine records it; you do not write it anywhere yourself."
)


def ownership_section(role: str) -> list[str]:
    """The managed-run artifact-ownership instruction for ``role``.

    Returned as prompt lines, the shape both
    :mod:`agent_sparring.stage_prompt` and
    :mod:`agent_sparring.sparring_prompt` assemble their prompts from.

    One text with one role-dependent sentence: an implementation agent is
    told where its progress reporting *does* belong, and a sparrer is told
    the same about its verdict. Everything else -- that the plan is
    read-only input and that no ``.sparring/`` artifact is
    provider-writable -- is identical for every role, because the contract
    is.
    """

    if role == ROLE_STAGE:
        reporting = _STAGE_REPORTING
    elif role == ROLE_SPARRER:
        reporting = _SPARRING_REPORTING
    else:  # pragma: no cover - defensive; roles are a closed set
        raise ValueError(f"unknown prompt role {role!r}")

    return [
        _HEADING,
        "",
        _PLAN_IS_INPUT,
        "",
        *_PLAN_RULES,
        "",
        _PLAN_WHY,
        "",
        reporting,
        "",
        _ENGINE_OWNED,
    ]


__all__ = [
    "ARTIFACTS",
    "PROVIDER_WRITABLE",
    "ROLE_SPARRER",
    "ROLE_STAGE",
    "SOURCE_PLAN",
    "ArtifactOwnership",
    "ownership_section",
]
