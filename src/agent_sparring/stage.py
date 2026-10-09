"""Minimal stage artifact model.

Conceptually, a project-local stage lives at::

    .sparring/stages/<stage-id>/
        brief.md
        notes.md
        state.json
        handoff.md
        sparring.md
        activity.jsonl   (observational only; see agent_sparring.activity)

This module resolves stage directories safely, creates a new stage skeleton,
and reads/writes the minimal machine state as JSON. It does not implement any
workflow engine, provider invocation, or acceptance logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agent_sparring import templates
from agent_sparring.activity import ActivityLog

STAGES_DIRNAME = "stages"
STATE_FILENAME = "state.json"
BRIEF_FILENAME = "brief.md"
NOTES_FILENAME = "notes.md"
HANDOFF_FILENAME = "handoff.md"
SPARRING_FILENAME = "sparring.md"
# Append-only observational telemetry. Never read by orchestration; not
# created by Stage.create (it appears on first emit, if writable).
ACTIVITY_FILENAME = "activity.jsonl"
# Append-only record of the human <-> reviewer side conversation (see
# agent_sparring.dialogue). Like activity.jsonl it is not created by
# Stage.create -- it appears when someone first asks the reviewer something
# -- but unlike activity.jsonl it is provenance rather than telemetry: it is
# the only record of what was said in a conversation that happens inside the
# provider's own thread, where nothing else can see it.
DIALOGUE_FILENAME = "dialogue.jsonl"

# The notes.md heading under which a human's answer/check result/evidence is
# recorded (by `resume-plan --evidence`, or by hand). Prose only: the handoff
# and the stage prompt surface this section verbatim to the agents; nothing
# parses or gates on its content.
HUMAN_EVIDENCE_HEADING = "## Human evidence"

# The notes.md heading under which the engine records what a finalization
# turn actually did to the reviewed candidate (see
# :mod:`agent_sparring.finalization`). Also prose, and also never gated on:
# it exists so a human reading notes.md later can see that a commit turn
# altered work a human had already verified, instead of that fact living
# only in a terminal that has since closed.
FINALIZATION_HEADING = "## Finalization"

_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StageError(ValueError):
    """Raised for invalid stage ids, missing stages, or malformed state."""


class StageStatus(str, Enum):
    """Minimal stage lifecycle. Deliberately small; see the project plan."""

    WORKING = "working"
    FROZEN = "frozen"  # a candidate is prepared for the later hard acceptance gate
    ACCEPTED = "accepted"

    @classmethod
    def from_str(cls, value: str) -> "StageStatus":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise StageError(
                f"unknown stage status {value!r}; expected one of: {valid}"
            ) from exc


class StageMode(str, Enum):
    """What kind of turn a stage is made of -- what actually runs for it.

    Status says *how far along* a stage is; mode says *what it is*. They are
    independent, and a stage's mode never changes while it runs.

    - :attr:`IMPLEMENTATION` is every stage this engine has ever run: a
      stage agent writes code, a sparrer reviews it, and READY leads to a
      freeze of the commit that stage produced.
    - :attr:`INDEPENDENT_REVIEW` is a stage whose whole content is a review
      of work that is *already* accepted. No implementation agent runs for
      it at all: a fresh independent reviewer inspects the accepted
      candidate SHAs and the repository state and routes. It produces no
      commit of its own, because there is nothing for it to implement.

    Mode is declared, never inferred. A stage titled "Independent final
    review" is not a review-only stage because of its title, and a brief
    that asks for a review is not one either -- the prose is what the agent
    reads, not what decides which agent runs. Only an explicit ``mode`` in
    the execution manifest (see :mod:`agent_sparring.manifest`) selects
    :attr:`INDEPENDENT_REVIEW`; everything else, including every plan and
    manifest written before this existed, is :attr:`IMPLEMENTATION`.
    """

    IMPLEMENTATION = "implementation"
    INDEPENDENT_REVIEW = "independent_review"

    @classmethod
    def from_str(cls, value: str) -> "StageMode":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise StageError(
                f"unknown stage mode {value!r}; expected one of: {valid}"
            ) from exc

    @property
    def is_review(self) -> bool:
        """Does this mode mean "no implementation agent runs for this stage"?"""

        return self is StageMode.INDEPENDENT_REVIEW

    @property
    def describe(self) -> str:
        """How the mode is named in refusals and reports."""

        return (
            "independent review (review only)"
            if self.is_review
            else "implementation (stage agent then sparrer)"
        )


def validate_stage_id(stage_id: str) -> str:
    """Validate a stage id is safe to use as a single path segment.

    Rejects empty ids, path separators, and traversal segments (``.``/``..``).
    """

    if not isinstance(stage_id, str) or not _STAGE_ID_RE.match(stage_id):
        raise StageError(
            f"invalid stage id {stage_id!r}: must match {_STAGE_ID_RE.pattern}"
        )
    if stage_id in {".", ".."}:
        raise StageError(f"invalid stage id {stage_id!r}")
    return stage_id


@dataclass(frozen=True)
class CandidateRepository:
    """One *sibling* repository whose reviewed candidate belongs to a stage.

    A stage's candidate normally lives in exactly one repository -- the
    ``repo_root`` every other operation is made against -- and nothing here
    is needed. Some stages are genuinely cross-repository: the reviewed work
    is a desktop change in the primary repository *and* a coupled change in
    another one, and the sparrer's READY verdict depends on both. Pinning
    only the primary SHA would let the sibling move between review and
    acceptance, so acceptance would claim a candidate set that no longer
    exists.

    This is deliberately the smallest representation that closes that hole:
    identity, the branch the candidate must be on, and the exact commit.
    There is no cross-repository merge, no transaction, no dependency graph
    and no remote coordination -- :mod:`agent_sparring.acceptance` simply
    resolves each declared sibling at freeze time and re-verifies it at
    acceptance time, exactly as it already does for the primary repository.

    ``path`` is resolved against the primary ``repo_root`` when relative.
    ``candidate_sha`` is ``None`` while the sibling is only *declared* (it is
    whatever the sibling's branch is at); ``freeze_candidate`` fills it in
    with the resolved HEAD, and from then on it is the pinned candidate. A
    declaration that already carries a ``candidate_sha`` is an assertion:
    freezing refuses if the sibling is not exactly there.
    """

    name: str
    path: str
    branch: str
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "branch": self.branch,
            "candidate_sha": self.candidate_sha,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CandidateRepository":
        if not isinstance(payload, dict):
            raise StageError(f"a repository entry must be a JSON object, got {payload!r}")
        for key in ("name", "path", "branch"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise StageError(f"repository entry field {key!r} must be a non-empty string")
        sha = payload.get("candidate_sha")
        if sha is not None and not isinstance(sha, str):
            raise StageError("repository entry field 'candidate_sha' must be a string or null")
        return cls(
            name=str(payload["name"]),
            path=str(payload["path"]),
            branch=str(payload["branch"]),
            candidate_sha=sha,
        )


def _optional_str_field(payload: dict[str, Any], key: str) -> str | None:
    """A ``str | None`` field read from machine-ingested JSON.

    Raises :class:`StageError` for a present-but-wrong-typed value (e.g. a
    number where downstream code, such as git context gathering, requires a
    string) instead of silently coercing or misrepresenting it.
    """

    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise StageError(f"state.json field {key!r} must be a string or null, got {value!r}")
    return value


@dataclass(frozen=True)
class PinnedAgent:
    """What one role runs with for the whole of one provider session.

    Written once, immediately before the session's first provider turn, and
    read back by every later turn of the same session -- SEND_BACK cycles,
    resumes in a new process, the finalization turn -- so a preference
    changed while the stage is under way applies from the *next* stage (or
    a fresh session, see :mod:`agent_sparring.sessions`) and never switches
    the model under an existing provider session.
    """

    provider: str
    model: str | None
    model_source: str
    effort: str | None
    effort_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any, *, where: str) -> "PinnedAgent":
        if not isinstance(payload, dict):
            raise StageError(f"state.json field {where!r} must be an object")
        provider = _optional_str_field(payload, "provider")
        if not provider:
            raise StageError(f"state.json field '{where}.provider' is required")
        return cls(
            provider=provider,
            model=_optional_str_field(payload, "model"),
            model_source=_optional_str_field(payload, "model_source") or "unknown",
            effort=_optional_str_field(payload, "effort"),
            effort_source=_optional_str_field(payload, "effort_source") or "unknown",
        )


NEXT_TURN_STAGE = "stage"
NEXT_TURN_SPARRING = "sparring"
NEXT_TURN_FINALIZATION = "finalization"
NEXT_TURNS = (NEXT_TURN_STAGE, NEXT_TURN_SPARRING, NEXT_TURN_FINALIZATION)

# Where a recorded ``next_turn`` came from. ``engine`` is the loop writing it
# after an authoritative operation; ``derived`` and ``manual`` are the one-off
# legacy reconstruction (see agent_sparring.next_turn).
NEXT_TURN_SOURCES = ("engine", "derived", "manual")
# How an ambiguous legacy state was resolved (``next_turn_resolution``).
NEXT_TURN_RESOLUTION_SOURCES = ("derived", "manual")


@dataclass(frozen=True)
class SiblingPin:
    """A declared sibling repository's HEAD at the moment a candidate was
    recorded for review."""

    name: str
    head_sha: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "head_sha": self.head_sha}

    @classmethod
    def from_dict(cls, payload: Any) -> "SiblingPin":
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            raise StageError("next_turn_candidate.repositories entries need a string 'name'")
        return cls(name=payload["name"], head_sha=_optional_str_field(payload, "head_sha"))


@dataclass(frozen=True)
class TurnCandidate:
    """The exact candidate a ``next_turn = sparring`` marker refers to.

    ``kind`` is ``commit`` when HEAD holds all candidate content and
    ``worktree`` when some of it is still uncommitted; ``content_digest`` is
    the candidate content digest :mod:`agent_sparring.finalization` already
    computes (stage artifacts excluded), so an uncommitted candidate is
    identified by content rather than only by its HEAD.

    ``tracked_digest`` (the content digest without untracked files) and
    ``untracked`` (each untracked candidate path with its ``"<mode> <blob>"``)
    split that identity so a later resume can tell "previously reviewed
    untracked files were removed, nothing else changed" from any other
    drift. Both derive from ``content_digest``'s inputs, so they take no part
    in equality, and both are ``None`` for markers recorded before they
    existed.

    ``index_digest`` is the digest of what the real index staged (``"unmerged"``
    while a path was unmerged), so a branch advance accepted beneath the
    candidate can show the attempt's staged work unchanged too. It is not
    the candidate (the working tree is), so it takes no part in equality
    either, and is ``None`` for markers recorded before it existed.
    """

    head_sha: str
    kind: str
    content_digest: str
    repositories: tuple[SiblingPin, ...] = ()
    tracked_digest: str | None = field(default=None, compare=False)
    untracked: tuple[tuple[str, str], ...] | None = field(default=None, compare=False)
    index_digest: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "head_sha": self.head_sha,
            "kind": self.kind,
            "content_digest": self.content_digest,
        }
        if self.repositories:
            payload["repositories"] = [repo.to_dict() for repo in self.repositories]
        if self.tracked_digest is not None:
            payload["tracked_digest"] = self.tracked_digest
        if self.untracked is not None:
            payload["untracked"] = [{"path": path, "blob": blob} for path, blob in self.untracked]
        if self.index_digest is not None:
            payload["index_digest"] = self.index_digest
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "TurnCandidate":
        if not isinstance(payload, dict):
            raise StageError("state.json field 'next_turn_candidate' must be an object or null")
        head = _optional_str_field(payload, "head_sha")
        kind = _optional_str_field(payload, "kind")
        digest = _optional_str_field(payload, "content_digest")
        if not head or kind not in ("commit", "worktree") or not digest:
            raise StageError(
                "state.json field 'next_turn_candidate' needs head_sha, kind "
                "(commit|worktree) and content_digest"
            )
        raw = payload.get("repositories") or []
        if not isinstance(raw, list):
            raise StageError("next_turn_candidate.repositories must be a list")
        raw_untracked = payload.get("untracked")
        untracked: tuple[tuple[str, str], ...] | None = None
        if raw_untracked is not None:
            if not isinstance(raw_untracked, list) or not all(
                isinstance(entry, dict)
                and isinstance(entry.get("path"), str)
                and isinstance(entry.get("blob"), str)
                for entry in raw_untracked
            ):
                raise StageError(
                    "next_turn_candidate.untracked must be a list of {path, blob} objects"
                )
            untracked = tuple((entry["path"], entry["blob"]) for entry in raw_untracked)
        return cls(
            head_sha=head,
            kind=kind,
            content_digest=digest,
            repositories=tuple(SiblingPin.from_dict(entry) for entry in raw),
            tracked_digest=_optional_str_field(payload, "tracked_digest"),
            untracked=untracked,
            index_digest=_optional_str_field(payload, "index_digest"),
        )

    def describe(self) -> str:
        text = f"HEAD {self.head_sha} ({self.kind}, content {self.content_digest[:12]})"
        for repo in self.repositories:
            text += f", sibling {repo.name} at {repo.head_sha or 'unresolved'}"
        return text


@dataclass(frozen=True)
class NextTurnResolution:
    """The immutable record of how an ambiguous legacy state was resolved:
    the turn chosen, whether a person chose it (``manual``) or the engine
    derived it (``derived``), when, and the refusal or reason it answered.

    Unlike ``next_turn_source`` -- who wrote the *current* marker, which
    routine transitions overwrite -- this is history: written once, never
    altered or dropped by later marker writes.
    """

    turn: str
    source: str
    recorded_at: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "NextTurnResolution":
        if not isinstance(payload, dict):
            raise StageError("state.json field 'next_turn_resolution' must be an object or null")
        turn = _optional_str_field(payload, "turn")
        source = _optional_str_field(payload, "source")
        recorded_at = _optional_str_field(payload, "recorded_at")
        if turn not in NEXT_TURNS or source not in NEXT_TURN_RESOLUTION_SOURCES or not recorded_at:
            raise StageError(
                "state.json field 'next_turn_resolution' needs turn, source "
                f"({'|'.join(NEXT_TURN_RESOLUTION_SOURCES)}) and recorded_at"
            )
        return cls(
            turn=turn,
            source=source,
            recorded_at=recorded_at,
            reason=_optional_str_field(payload, "reason") or "",
        )


@dataclass
class SessionGeneration:
    """One provider conversation for one role of one stage.

    A role starts at generation 1 and only ever gains a new generation
    through :func:`agent_sparring.sessions.start_fresh_session`. ``agent`` is
    the configuration pinned for this generation (``None`` while a fresh
    generation is pending its first turn); ``session_id`` is ``None`` until
    the provider reports one.
    """

    generation: int
    session_id: str | None = None
    agent: "PinnedAgent | None" = None
    started_at: str | None = None
    start_reason: str = "initial"
    ended_at: str | None = None
    end_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "generation": self.generation,
            "session_id": self.session_id,
            "agent": self.agent.to_dict() if self.agent is not None else None,
            "started_at": self.started_at,
            "start_reason": self.start_reason,
        }
        if self.ended_at is not None or self.end_reason is not None:
            payload["ended_at"] = self.ended_at
            payload["end_reason"] = self.end_reason
        return payload

    @classmethod
    def from_dict(cls, payload: Any, *, where: str) -> "SessionGeneration":
        if not isinstance(payload, dict):
            raise StageError(f"state.json field {where!r} must be an object")
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise StageError(f"state.json field '{where}.generation' must be a positive integer")
        raw_agent = payload.get("agent")
        return cls(
            generation=generation,
            session_id=_optional_str_field(payload, "session_id"),
            agent=(
                PinnedAgent.from_dict(raw_agent, where=f"{where}.agent")
                if raw_agent is not None
                else None
            ),
            started_at=_optional_str_field(payload, "started_at"),
            start_reason=_optional_str_field(payload, "start_reason") or "initial",
            ended_at=_optional_str_field(payload, "ended_at"),
            end_reason=_optional_str_field(payload, "end_reason"),
        )


@dataclass
class StageState:
    """The smallest machine state needed by later stages.

    Stage identity is not duplicated here: it comes from the stage's
    directory (``.sparring/stages/<stage-id>/``), which ``Stage`` itself
    owns. No V1 workflow states (changes_requested, review_attempt,
    ancillary states, migration states, immutable intermediate verdicts) are
    represented here either. See the project plan's "Minimal state" section
    for the rationale.
    """

    status: StageStatus = StageStatus.WORKING
    implementation_session_id: str | None = None
    sparring_session_id: str | None = None
    base_sha: str | None = None
    candidate_sha: str | None = None
    # Sibling repositories whose reviewed candidates are part of this stage.
    # Empty for the ordinary single-repository stage, and absent from
    # state.json in that case, so existing files are unchanged.
    repositories: tuple[CandidateRepository, ...] = ()
    # What kind of stage this actually ran as (see StageMode). Written once,
    # before the first provider turn, by whoever enters the stage; it is the
    # record of what was executed, not of what the plan says today, which is
    # the whole point of having it. Absent from state.json for the default
    # IMPLEMENTATION mode, so every existing file stays byte-identical and
    # reads back as the mode it in fact ran under.
    mode: StageMode = StageMode.IMPLEMENTATION
    # Which managed run *instance* owns this stage instance: that run's key
    # (see :func:`agent_sparring.plan.new_run_key`), written once by the run
    # that creates or deliberately adopts the stage and never rewritten to a
    # different value.
    #
    # This is what makes execution-stage identity ``(run instance, stage)``
    # rather than a globally reusable directory name. Stage ids can collide
    # across runs -- two plans in one worktree both saying "Stage 1 --
    # Foundation" is an ordinary follow-up workflow, and so is running the
    # *same* plan document a second time -- and without a recorded owner an
    # accepted stage of one run would silently answer for the same-named
    # stage of the next one.
    #
    # Note what the value is not. It is a run key, not a plan key: a plan
    # document is the *input* to a run and can be executed more than once,
    # so "owned by plan X" would still let run B inherit run A's accepted
    # stages. Serialized as ``run``; a file carrying the older ``plan`` key
    # is read as that plan document's one legacy run instance, which is
    # exactly what it was (see :func:`agent_sparring.plan.legacy_run_key`).
    #
    # ``None`` means no managed run has claimed this stage: a hand-driven
    # standalone stage, or one written before ownership was recorded. That
    # is the only case adoption may take over, and it is why absence is not
    # an error -- every state.json on disk today reads back as unowned, and
    # stays byte-identical until a run claims it.
    run: str | None = None
    # The agent configuration this stage runs with from its first provider
    # turn to its last, as ``{"stage": PinnedAgent, "sparring": PinnedAgent}``
    # (see :class:`PinnedAgent`). ``None`` until the first turn, and absent
    # from state.json then, so existing files stay byte-identical.
    agents: dict[str, PinnedAgent] | None = None
    # Whose turn it is, owned by the loop (see agent_sparring.next_turn):
    # ``stage``, ``sparring`` or ``finalization``. ``next_turn_candidate``
    # pins the exact candidate a ``sparring`` marker refers to, and
    # ``next_turn_source`` says whether the engine wrote it or it was
    # reconstructed once for legacy state (``derived`` / ``manual``). All
    # three are absent from state.json until the engine first writes the
    # marker, so existing files stay byte-identical.
    next_turn: str | None = None
    next_turn_candidate: TurnCandidate | None = None
    next_turn_source: str | None = None
    # How an ambiguous legacy state was resolved (see NextTurnResolution);
    # written once and kept through every later marker write.
    next_turn_resolution: NextTurnResolution | None = None
    # Untracked, non-ignored candidate paths present before the stage's
    # first implementation turn (recorded with ``base_sha``): what the
    # implementation demonstrably did not produce. ``None`` -- absent from
    # state.json -- for stages that started before it was recorded.
    untracked_baseline: tuple[str, ...] | None = None
    # The exact untracked candidate paths present when the last
    # implementation turn finished (file by file). ``None`` -- absent -- for
    # turns recorded before it existed; the handoff's list is used then.
    untracked_produced: tuple[str, ...] | None = None
    # True from a successful implementation turn until the next write of
    # the marker (a pinned review, a verdict, a reopen, a cycle start):
    # structured evidence that the turn completed and no review has been
    # pinned for it since. Absent from state.json while false.
    implementation_unreviewed: bool = False
    # Provider conversations per role (see agent_sparring.sessions),
    # ``{role: [SessionGeneration, ...]}``. Empty and absent from state.json
    # until a fresh session is first started; until then the recorded
    # session ids above *are* generation 1.
    sessions: dict[str, list[SessionGeneration]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.next_turn is None:
            payload.pop("next_turn", None)
        if self.next_turn_candidate is None:
            payload.pop("next_turn_candidate", None)
        else:
            payload["next_turn_candidate"] = self.next_turn_candidate.to_dict()
        if self.next_turn_source is None:
            payload.pop("next_turn_source", None)
        if self.next_turn_resolution is None:
            payload.pop("next_turn_resolution", None)
        else:
            payload["next_turn_resolution"] = self.next_turn_resolution.to_dict()
        if self.untracked_baseline is None:
            payload.pop("untracked_baseline", None)
        else:
            payload["untracked_baseline"] = list(self.untracked_baseline)
        if not self.implementation_unreviewed:
            payload.pop("implementation_unreviewed", None)
        if self.untracked_produced is None:
            payload.pop("untracked_produced", None)
        else:
            payload["untracked_produced"] = list(self.untracked_produced)
        if not self.sessions:
            payload.pop("sessions", None)
        else:
            payload["sessions"] = {
                role: [generation.to_dict() for generation in generations]
                for role, generations in self.sessions.items()
            }
        if self.agents is None:
            payload.pop("agents", None)
        else:
            payload["agents"] = {role: pin.to_dict() for role, pin in self.agents.items()}
        payload["status"] = self.status.value
        if self.repositories:
            payload["repositories"] = [repo.to_dict() for repo in self.repositories]
        else:
            payload.pop("repositories", None)
        if self.mode is StageMode.IMPLEMENTATION:
            payload.pop("mode", None)
        else:
            payload["mode"] = self.mode.value
        # Omitted while unowned, so a hand-driven stage's state.json is
        # exactly the file it was before ownership existed.
        if self.run is None:
            payload.pop("run", None)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StageState":
        status_raw = payload.get("status", StageStatus.WORKING.value)
        status = StageStatus.from_str(str(status_raw))
        raw_repositories = payload.get("repositories")
        if raw_repositories is None:
            repositories: tuple[CandidateRepository, ...] = ()
        elif isinstance(raw_repositories, list):
            repositories = tuple(CandidateRepository.from_dict(entry) for entry in raw_repositories)
        else:
            raise StageError("state.json field 'repositories' must be a list or null")
        raw_mode = payload.get("mode")
        # A state.json written before modes existed ran the implementation
        # lifecycle, which is exactly what the default says; an unreadable or
        # unknown mode is refused rather than defaulted, because guessing it
        # would decide which agent runs.
        if raw_mode is None:
            mode = StageMode.IMPLEMENTATION
        elif isinstance(raw_mode, str):
            mode = StageMode.from_str(raw_mode)
        else:
            raise StageError("state.json field 'mode' must be a string or null")
        raw_agents = payload.get("agents")
        if raw_agents is None:
            agents: dict[str, PinnedAgent] | None = None
        elif isinstance(raw_agents, dict):
            agents = {
                str(role): PinnedAgent.from_dict(entry, where=f"agents.{role}")
                for role, entry in raw_agents.items()
            }
        else:
            raise StageError("state.json field 'agents' must be an object or null")
        next_turn = _optional_str_field(payload, "next_turn")
        if next_turn is not None and next_turn not in NEXT_TURNS:
            raise StageError(
                f"state.json field 'next_turn' must be one of {list(NEXT_TURNS)}, got {next_turn!r}"
            )
        next_turn_source = _optional_str_field(payload, "next_turn_source")
        if next_turn_source is not None and next_turn_source not in NEXT_TURN_SOURCES:
            raise StageError(
                f"state.json field 'next_turn_source' must be one of {list(NEXT_TURN_SOURCES)}"
            )
        raw_candidate = payload.get("next_turn_candidate")
        raw_resolution = payload.get("next_turn_resolution")
        raw_baseline = payload.get("untracked_baseline")
        raw_produced = payload.get("untracked_produced")
        for name, raw in (("untracked_baseline", raw_baseline), ("untracked_produced", raw_produced)):
            if raw is not None and (
                not isinstance(raw, list) or not all(isinstance(path, str) for path in raw)
            ):
                raise StageError(f"state.json field {name!r} must be a list of paths")
        raw_sessions = payload.get("sessions")
        if raw_sessions is None:
            sessions: dict[str, list[SessionGeneration]] = {}
        elif isinstance(raw_sessions, dict):
            sessions = {}
            for role, entries in raw_sessions.items():
                if not isinstance(entries, list):
                    raise StageError(f"state.json field 'sessions.{role}' must be a list")
                sessions[str(role)] = [
                    SessionGeneration.from_dict(entry, where=f"sessions.{role}[{index}]")
                    for index, entry in enumerate(entries)
                ]
        else:
            raise StageError("state.json field 'sessions' must be an object or null")
        return cls(
            next_turn=next_turn,
            next_turn_candidate=(
                TurnCandidate.from_dict(raw_candidate) if raw_candidate is not None else None
            ),
            next_turn_source=next_turn_source,
            next_turn_resolution=(
                NextTurnResolution.from_dict(raw_resolution)
                if raw_resolution is not None
                else None
            ),
            untracked_baseline=tuple(raw_baseline) if raw_baseline is not None else None,
            untracked_produced=tuple(raw_produced) if raw_produced is not None else None,
            implementation_unreviewed=payload.get("implementation_unreviewed") is True,
            sessions=sessions,
            agents=agents,
            status=status,
            implementation_session_id=_optional_str_field(
                payload, "implementation_session_id"
            ),
            sparring_session_id=_optional_str_field(payload, "sparring_session_id"),
            base_sha=_optional_str_field(payload, "base_sha"),
            candidate_sha=_optional_str_field(payload, "candidate_sha"),
            repositories=repositories,
            mode=mode,
            # The older ``plan`` spelling is read as this stage's owner too.
            # It held a plan key, which named that plan document's only
            # execution -- its legacy run instance -- so reading it as the
            # owning run is not an interpretation, it is what it recorded.
            run=_optional_str_field(payload, "run" if "run" in payload else "plan"),
        )


@dataclass
class Stage:
    """Handle onto one stage's directory and artifacts."""

    stage_id: str
    directory: Path

    # -- resolution -----------------------------------------------------

    @classmethod
    def resolve(cls, sparring_dir: Path, stage_id: str) -> "Stage":
        """Resolve (without requiring existence) the directory for a stage id.

        Raises :class:`StageError` if ``stage_id`` is unsafe.
        """

        validate_stage_id(stage_id)
        stages_root = Path(sparring_dir) / STAGES_DIRNAME
        directory = stages_root / stage_id

        # Defense in depth: even though validate_stage_id already rejects
        # separators and traversal segments, confirm the resolved path stays
        # within stages_root before any filesystem write.
        resolved_root = stages_root.resolve()
        resolved_dir = (stages_root / stage_id).resolve()
        if resolved_root not in resolved_dir.parents and resolved_dir != resolved_root:
            raise StageError(f"stage id {stage_id!r} escapes the stages directory")

        return cls(stage_id=stage_id, directory=directory)

    def exists(self) -> bool:
        return self.directory.is_dir()

    # -- skeleton creation ------------------------------------------------

    def create(
        self,
        *,
        exist_ok: bool = False,
        brief: str | None = None,
        mode: StageMode = StageMode.IMPLEMENTATION,
        run: str | None = None,
    ) -> "Stage":
        """Create a new stage skeleton with initial state and templates.

        ``brief`` is the initial ``brief.md`` content, written verbatim in
        place of the template. Like the template it is only written when
        ``brief.md`` does not exist yet: an existing brief (``exist_ok``) is
        never overwritten. The caller decides what the brief says; this
        method only owns *where* it goes and *when* it is written.

        ``mode`` is recorded in the fresh ``state.json`` (see
        :class:`StageMode`), so a stage that will never run an
        implementation agent says so from the moment it exists rather than
        from the moment something first runs for it. Like the brief it is
        only written with the initial state: an existing ``state.json``
        (``exist_ok``) keeps whatever mode it already ran under.

        ``run`` is the key of the managed run instance creating this stage
        (see :attr:`StageState.run`), recorded with the same initial state
        so the stage is owned from the moment it exists. A stage created
        without one is unowned, which is what a hand-driven
        ``sparring new-stage`` means.

        Raises :class:`StageError` if the stage already exists and
        ``exist_ok`` is False.
        """

        if self.exists() and not exist_ok:
            raise StageError(f"stage {self.stage_id!r} already exists at {self.directory}")

        self.directory.mkdir(parents=True, exist_ok=True)

        state_path = self.directory / STATE_FILENAME
        if not state_path.is_file():
            self.write_state(StageState(mode=mode, run=run))

        initial_brief = (
            brief if brief is not None else templates.BRIEF_TEMPLATE.format(stage_id=self.stage_id)
        )
        for filename, content in (
            (BRIEF_FILENAME, initial_brief),
            (NOTES_FILENAME, templates.NOTES_TEMPLATE.format(stage_id=self.stage_id)),
            (HANDOFF_FILENAME, templates.HANDOFF_TEMPLATE.format(stage_id=self.stage_id)),
            (SPARRING_FILENAME, templates.SPARRING_TEMPLATE.format(stage_id=self.stage_id)),
        ):
            path = self.directory / filename
            if not path.is_file():
                path.write_text(content, encoding="utf-8")

        return self

    # -- machine state ----------------------------------------------------

    def read_state(self) -> StageState:
        path = self.directory / STATE_FILENAME
        if not path.is_file():
            raise StageError(f"no state.json for stage {self.stage_id!r} at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StageError(f"malformed state.json at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StageError(f"state.json at {path} must contain a JSON object")
        return StageState.from_dict(payload)

    def write_state(self, state: StageState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / STATE_FILENAME
        path.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # -- observational activity stream -------------------------------------

    def activity_path(self) -> Path:
        return self.directory / ACTIVITY_FILENAME

    def activity_log(self) -> ActivityLog:
        """A write-only handle onto this stage's ``activity.jsonl``.

        Orchestration code opens the log through this method and only ever
        emits to it; there is no corresponding reader anywhere in the
        package (see :mod:`agent_sparring.activity`).
        """

        return ActivityLog(self.activity_path())

    # -- human-readable artifacts ------------------------------------------

    def _read_text(self, filename: str) -> str:
        path = self.directory / filename
        if not path.is_file():
            raise StageError(f"no {filename} for stage {self.stage_id!r} at {path}")
        return path.read_text(encoding="utf-8")

    def _write_text(self, filename: str, content: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / filename).write_text(content, encoding="utf-8")

    def read_brief(self) -> str:
        return self._read_text(BRIEF_FILENAME)

    def read_notes(self) -> str:
        return self._read_text(NOTES_FILENAME)

    def read_handoff(self) -> str:
        return self._read_text(HANDOFF_FILENAME)

    def read_sparring(self) -> str:
        return self._read_text(SPARRING_FILENAME)

    def dialogue_path(self) -> Path:
        return self.directory / DIALOGUE_FILENAME

    def read_dialogue(self) -> tuple[dict[str, Any], ...]:
        """Every recorded reviewer exchange, oldest first.

        A line that does not parse is skipped rather than raising: this is
        an append-only log that a crash can truncate mid-write, and one
        damaged line must not make the rest of a conversation unreadable.
        """

        path = self.dialogue_path()
        if not path.is_file():
            return ()
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return tuple(records)

    def append_dialogue(self, record: dict[str, Any]) -> None:
        """Append one exchange. Unlike telemetry, a failure here is real.

        activity.jsonl may silently give up on a write because nothing
        depends on it. This file is the conversation's only record, so a
        caller that cannot write it should hear about it rather than
        discover later that an exchange it reported to a person was never
        kept.
        """

        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        with self.dialogue_path().open("a", encoding="utf-8") as handle:
            handle.write(line)

    def append_note(self, heading: str, entry: str) -> None:
        """Append ``entry`` at the end of notes.md's ``heading`` section,
        creating the heading at the end of the file on first use.

        The entry lands inside the named section, not merely at the end of
        the file. That distinction matters as soon as notes.md holds more
        than one such section: a reader of ``## Human evidence`` (the
        handoff and both prompts, via
        :func:`agent_sparring.handoff.extract_section`) stops at the next
        heading, so an entry appended past a later heading would be recorded
        and then never shown again.

        Prose only, like everything else in notes.md: nothing parses these
        sections as machine state.
        """

        lines = self.read_notes().rstrip("\n").splitlines()
        body = entry.strip()
        start = next(
            (i for i, line in enumerate(lines) if line.strip() == heading.strip()), None
        )
        if start is None:
            trailing = "\n".join(lines).rstrip("\n")
            self.write_notes(f"{trailing}\n\n{heading}\n\n{body}\n")
            return

        # extract_section ends a section at the next line starting with '#';
        # the insertion point must agree with it, or the entry would be
        # written outside the section it belongs to.
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines)
        )
        section = lines[:end]
        while section and not section[-1].strip():
            section.pop()
        rest = lines[end:]
        merged = section + ["", body] + (["", *rest] if rest else [])
        self.write_notes("\n".join(merged).rstrip("\n") + "\n")

    def write_brief(self, content: str) -> None:
        self._write_text(BRIEF_FILENAME, content)

    def write_notes(self, content: str) -> None:
        self._write_text(NOTES_FILENAME, content)

    def write_handoff(self, content: str) -> None:
        self._write_text(HANDOFF_FILENAME, content)

    def write_sparring(self, content: str) -> None:
        self._write_text(SPARRING_FILENAME, content)
