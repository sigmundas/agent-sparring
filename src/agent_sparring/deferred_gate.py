"""Human verification a reviewer judged safe to owe, rather than to stop for.

:mod:`agent_sparring.human_gate` is the *immediate* gate: NEEDS_YOU, and
every check in it must be completed before that stage may become READY. That
contract is right for a product choice the next stage builds on, and wrong
for "resize the finished dialog and check it is still readable". The second
is real work a person still has to do, and nothing downstream depends on the
answer -- but expressed as an immediate gate it stops the whole
implementation/sparring loop the moment it is raised.

So there is a second shape, and the difference between the two is a
*judgement the reviewer makes*, not a rule this module applies:

    NEEDS_YOU + human_gate            a person must answer before this
                                      stage can be READY
    READY     + deferred_human_gate   no implementation issue blocks this
                                      stage; a person still owes this
                                      verification, and the reviewer has
                                      said why continuing first is low risk

Nothing here inspects a check's category, wording or subject to decide which
one it should have been. A ``UI_VISUAL_CHECK`` is immediate when the
reviewer says so and deferred when the reviewer says so; the engine's job is
the part a reviewer cannot do for itself -- that a deferred obligation is
minted with real identity, persisted where it survives everything, and never
quietly forgotten.

What the engine does enforce
----------------------------

- a deferred gate carries a reviewer-authored ``rationale``. An agent that
  defers without saying why produces an invalid verdict, because the
  rationale is the only thing that makes the decision auditable later;
- its checkpoint is a value this version understands. A reviewer may name
  exactly one, :data:`CHECKPOINT_PLAN_COMPLETION`. The engine itself also
  mints ``before_stage:<stage id>`` obligations for plan-declared manifest
  gates (:func:`before_stage_checkpoint`); that is a new value of the same
  field, not a new format;
- every obligation has an engine-minted gate ``instance_id`` (see
  :mod:`agent_sparring.human_gate`), so an answer belongs to *this asking*
  and an answer to an earlier one cannot silently satisfy it;
- a plan may not complete while any obligation is unresolved
  (:mod:`agent_sparring.plan`).

Identity across the deferral
----------------------------

An obligation keeps the instance the gate was minted with for as long as it
is the same unresolved obligation: deferring it, carrying it across stages
and later promoting it to an immediate pause are all the *same asking*,
answered late. A reviewer that materially reissues a check writes a new
deferred (or immediate) gate, and :func:`~agent_sparring.sparring_exchange.
record_sparring` mints a new instance for it -- so "the reviewer asked
again" and "the reviewer is still waiting" stay different facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from agent_sparring.human_gate import (
    HUMAN_GATE_CATEGORIES,
    HumanCheck,
    HumanGate,
    HumanGateError,
)

#: The marker preceding a deferred gate's canonical JSON in ``sparring.md``.
#: Distinct from :data:`~agent_sparring.human_gate.HUMAN_GATE_MARKER` so the
#: two blocks can never be read for one another.
DEFERRED_GATE_MARKER = "<!-- deferred-human-gate:v1 -->"

#: The one checkpoint v1 implements: the obligation must be answered before
#: the managed plan may report completion.
CHECKPOINT_PLAN_COMPLETION = "before_plan_completion"

#: Every checkpoint this version accepts. A reviewer naming anything else
#: gets a refusal rather than an obligation with a deadline nothing honours.
CHECKPOINTS = (CHECKPOINT_PLAN_COMPLETION,)

#: Prefix of the checkpoint an engine-minted manifest gate is owed by: the
#: answer is due before the named stage is created. Never reviewer-authored.
CHECKPOINT_BEFORE_STAGE_PREFIX = "before_stage:"


def before_stage_checkpoint(stage_id: str) -> str:
    """The checkpoint for a manifest gate owed before ``stage_id``."""

    return f"{CHECKPOINT_BEFORE_STAGE_PREFIX}{stage_id}"


def is_before_stage(checkpoint: str) -> bool:
    return checkpoint.startswith(CHECKPOINT_BEFORE_STAGE_PREFIX)

#: The typed reason a managed run stops for deferred human verification
#: (mirrors :data:`~agent_sparring.push_gate.PUSH_AUTHORIZATION_REQUIRED`).
DEFERRED_VERIFICATION_REQUIRED = "deferred_verification_required"


class DeferredGateError(ValueError):
    """Raised for a malformed or out-of-contract deferred gate/obligation."""


class CheckOutcome(str, Enum):
    """What a person reported about one deferred check.

    ``BLOCKED`` is deliberately *not* a result: it says the check could not
    be performed. It therefore resolves nothing, and a plan holding one stays
    stopped -- the alternative is completing a plan on a verification nobody
    was able to make.
    """

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"

    @classmethod
    def from_str(cls, value: str) -> "CheckOutcome":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise DeferredGateError(
                f"unknown check outcome {value!r}; expected one of: {valid}"
            ) from exc

    @property
    def word(self) -> str:
        """The capitalised word written into ``notes.md``."""

        return {"pass": "Pass", "fail": "Fail", "blocked": "Blocked"}[self.value]


class ObligationStatus(str, Enum):
    """Derived, never stored: see :attr:`DeferredObligation.status`."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class DeferredHumanGate:
    """What a READY verdict carries when verification is owed but not now.

    Wraps a :class:`~agent_sparring.human_gate.HumanGate` rather than
    subclassing or copying it: the category vocabulary, the check shape, the
    uniqueness rule and the instance-identity rule are all exactly the
    immediate gate's, and there must not be a second implementation of them
    that can drift. What this adds is the two facts that make a deferral a
    deferral -- the reviewer's ``rationale`` and the ``checkpoint`` by which
    the answer is owed.
    """

    gate: HumanGate
    rationale: str
    checkpoint: str = CHECKPOINT_PLAN_COMPLETION

    def __post_init__(self) -> None:
        if not isinstance(self.gate, HumanGate):
            raise DeferredGateError(
                f"a deferred human gate must wrap a HumanGate, got {self.gate!r}"
            )
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise DeferredGateError(
                "a deferred human gate must carry a rationale saying why continuing "
                "before this verification is low risk; deferring without one records a "
                "decision nobody can audit"
            )
        if self.checkpoint not in CHECKPOINTS:
            valid = ", ".join(CHECKPOINTS)
            raise DeferredGateError(
                f"deferred human gate checkpoint {self.checkpoint!r} is not one this "
                f"version implements: {valid}"
            )

    @property
    def instance_id(self) -> str | None:
        return self.gate.instance_id

    @property
    def category(self) -> str:
        return self.gate.category

    @property
    def title(self) -> str:
        return self.gate.title

    @property
    def checks(self) -> tuple[HumanCheck, ...]:
        return self.gate.checks

    def asked_again(self, instance_id: str) -> "DeferredHumanGate":
        """This same deferral, as a new asking; see
        :meth:`~agent_sparring.human_gate.HumanGate.asked_again`."""

        return replace(self, gate=self.gate.asked_again(instance_id))

    def to_dict(self) -> dict[str, Any]:
        payload = self.gate.to_dict()
        payload["rationale"] = self.rationale
        payload["checkpoint"] = self.checkpoint
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "DeferredHumanGate":
        if not isinstance(payload, Mapping):
            raise DeferredGateError(f"deferred_human_gate must be an object, got {payload!r}")
        try:
            gate = HumanGate.from_dict(payload)
        except HumanGateError as exc:
            raise DeferredGateError(str(exc)) from exc
        rationale = payload.get("rationale")
        checkpoint = payload.get("checkpoint") or CHECKPOINT_PLAN_COMPLETION
        return cls(
            gate=gate,
            rationale=rationale.strip() if isinstance(rationale, str) else "",
            checkpoint=str(checkpoint).strip(),
        )


@dataclass(frozen=True)
class CheckResult:
    """One person's answer to one deferred check, as the ledger holds it."""

    check_id: str
    outcome: CheckOutcome
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"check_id": self.check_id, "outcome": self.outcome.value}
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "CheckResult":
        if not isinstance(payload, Mapping):
            raise DeferredGateError(f"a deferred check result must be an object, got {payload!r}")
        check_id = payload.get("check_id")
        if not isinstance(check_id, str) or not check_id.strip():
            raise DeferredGateError("a deferred check result must name a check_id")
        note = payload.get("note")
        return cls(
            check_id=check_id.strip(),
            outcome=CheckOutcome.from_str(str(payload.get("outcome", ""))),
            note=note.strip() if isinstance(note, str) and note.strip() else None,
        )


@dataclass(frozen=True)
class DeferredObligation:
    """One durable, engine-owned record of verification a person still owes.

    ``status`` is derived from ``results`` rather than stored, so there is no
    second place a resolution could be claimed from. ``promoted`` is the one
    piece of workflow judgement the ledger holds, and it is the reviewer's:
    it says a later turn decided this obligation now has to be answered
    before the run goes any further, and it never changes the obligation's
    identity -- same asking, answered sooner.
    """

    #: Which stage's review raised it. Provenance, and where the resolution
    #: is written back so both agents see it on their next turn.
    stage_id: str
    gate: HumanGate
    rationale: str
    checkpoint: str = CHECKPOINT_PLAN_COMPLETION
    promoted: bool = False
    results: tuple[CheckResult, ...] = field(default_factory=tuple)
    #: The engine minted this for a plan-declared manifest gate (see
    #: :mod:`agent_sparring.manifest`), not a reviewer's deferral. Such an
    #: obligation belongs to its run slice and is never carried to the plan.
    manifest_gate: bool = False

    def __post_init__(self) -> None:
        if not self.gate.instance_id:
            raise DeferredGateError(
                "a recorded deferred obligation must carry the gate instance the engine "
                "minted for it; without it no answer can be attributed to this asking"
            )

    @property
    def instance_id(self) -> str:
        assert self.gate.instance_id is not None  # __post_init__ guarantees it
        return self.gate.instance_id

    def result_for(self, check_id: str) -> CheckResult | None:
        for result in self.results:
            if result.check_id == check_id:
                return result
        return None

    @property
    def unanswered(self) -> tuple[HumanCheck, ...]:
        """Checks with no recorded outcome, and checks nobody could test."""

        return tuple(
            check
            for check in self.gate.checks
            if (found := self.result_for(check.id)) is None
            or found.outcome is CheckOutcome.BLOCKED
        )

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is CheckOutcome.FAIL)

    @property
    def status(self) -> ObligationStatus:
        if self.failed:
            return ObligationStatus.FAILED
        if self.unanswered:
            return ObligationStatus.PENDING
        return ObligationStatus.PASSED

    @property
    def resolved(self) -> bool:
        """Is this obligation finished, so a plan may complete over it?"""

        return self.status is ObligationStatus.PASSED

    def with_result(self, result: CheckResult) -> "DeferredObligation":
        """This obligation with ``result`` recorded, replacing any earlier
        answer to the same check of the same asking.

        Replacing, not appending: the ledger holds what is true now, and a
        person who fixes a failure and re-answers the check has made the
        earlier Fail untrue of the current behaviour. The history is not
        lost -- every answer, including the superseded one, is written to the
        originating stage's ``notes.md`` when it is recorded, and that file
        is never rewritten.
        """

        if all(check.id != result.check_id for check in self.gate.checks):
            raise DeferredGateError(
                f"check {result.check_id!r} is not part of gate instance "
                f"{self.instance_id!r}"
            )
        kept = tuple(r for r in self.results if r.check_id != result.check_id)
        return replace(self, results=kept + (result,))

    def promote(self) -> "DeferredObligation":
        """The same obligation, now due immediately. Identity is unchanged:
        this is the same asking, not a new one."""

        return replace(self, promoted=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "gate": self.gate.to_dict(),
            "rationale": self.rationale,
            "checkpoint": self.checkpoint,
            "promoted": self.promoted,
            "results": [result.to_dict() for result in self.results],
            **({"manifest_gate": True} if self.manifest_gate else {}),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "DeferredObligation":
        if not isinstance(payload, Mapping):
            raise DeferredGateError(f"a deferred obligation must be an object, got {payload!r}")
        stage_id = payload.get("stage_id")
        if not isinstance(stage_id, str) or not stage_id.strip():
            raise DeferredGateError("a deferred obligation must name its originating stage")
        try:
            gate = HumanGate.from_dict(payload.get("gate"))
        except HumanGateError as exc:
            raise DeferredGateError(str(exc)) from exc
        raw_results = payload.get("results") or []
        if not isinstance(raw_results, (list, tuple)):
            raise DeferredGateError("a deferred obligation's 'results' must be an array")
        rationale = payload.get("rationale")
        return cls(
            stage_id=stage_id.strip(),
            gate=gate,
            rationale=rationale.strip() if isinstance(rationale, str) else "",
            checkpoint=str(payload.get("checkpoint") or CHECKPOINT_PLAN_COMPLETION).strip(),
            promoted=bool(payload.get("promoted")),
            results=tuple(CheckResult.from_dict(entry) for entry in raw_results),
            manifest_gate=payload.get("manifest_gate") is True,
        )

    @classmethod
    def from_gate(cls, stage_id: str, deferred: DeferredHumanGate) -> "DeferredObligation":
        """The ledger record for a gate the engine has just minted an
        instance for."""

        return cls(
            stage_id=stage_id,
            gate=deferred.gate,
            rationale=deferred.rationale,
            checkpoint=deferred.checkpoint,
        )

    @property
    def describe(self) -> str:
        return f"{self.gate.title} ({self.gate.category}, raised by {self.stage_id})"


@dataclass(frozen=True)
class DeferredAnswer:
    """One person's answer to one deferred check, as a caller supplies it.

    ``check_ref`` is either a bare check id, or ``<gate instance>:<check
    id>`` when a bare id would name checks of more than one asking. The
    qualified form is always accepted; the bare form is a convenience, and
    it is refused rather than guessed when it is ambiguous.
    """

    check_ref: str
    outcome: CheckOutcome
    note: str | None = None

    #: Separates the gate instance from the check id in a qualified ref. A
    #: gate instance never contains one (``human_gate._INSTANCE_ID_RE``), so
    #: the left half is unambiguous; a reviewer's check id is free text and
    #: may contain one, which is why the plan runner tries the bare reading
    #: as well as the split one rather than trusting this to partition
    #: cleanly.
    QUALIFIER = ":"

    @property
    def instance_id(self) -> str | None:
        head, sep, _ = self.check_ref.partition(self.QUALIFIER)
        return head if sep else None

    @property
    def check_id(self) -> str:
        _, sep, tail = self.check_ref.partition(self.QUALIFIER)
        return tail if sep else self.check_ref

    @classmethod
    def parse(cls, text: str) -> "DeferredAnswer":
        """``<ref>=<pass|fail|blocked>[=<note>]``.

        Split at most twice, so a note may contain ``=`` freely -- which
        notes written by a person reporting what they saw routinely do.
        """

        parts = str(text).split("=", 2)
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            raise DeferredGateError(
                f"a deferred result must be written '<check id>=<pass|fail|blocked>' with an "
                f"optional '=<note>', got {text!r}"
            )
        note = parts[2].strip() if len(parts) == 3 else ""
        return cls(
            check_ref=parts[0].strip(),
            outcome=CheckOutcome.from_str(parts[1]),
            note=note or None,
        )


@dataclass(frozen=True)
class DeferredVerificationRequired:
    """A typed statement that a managed run is stopped for verification a
    person already owes -- the deferred counterpart of
    :class:`~agent_sparring.push_gate.PushRequired`.

    It names only the askings it is about; the obligations themselves live in
    the run's ledger, which is the one authority for their content and their
    status. ``reason`` says which checkpoint stopped the run, so a consumer
    can tell "the plan is finished apart from this" from "a later reviewer
    decided this cannot wait any longer" without reading prose.
    """

    kind: str
    reason: str
    instance_ids: tuple[str, ...]

    #: ``reason`` values. Closed, for the same reason ``kind`` is.
    PLAN_COMPLETION = "plan_completion"
    PROMOTED = "promoted"
    #: Stopped on a plan-declared gate between two stages of the run.
    BEFORE_STAGE = "before_stage"

    @classmethod
    def for_obligations(
        cls, obligations: tuple["DeferredObligation", ...], *, reason: str
    ) -> "DeferredVerificationRequired":
        return cls(
            kind=DEFERRED_VERIFICATION_REQUIRED,
            reason=reason,
            instance_ids=tuple(obligation.instance_id for obligation in obligations),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "instance_ids": list(self.instance_ids),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "DeferredVerificationRequired":
        if not isinstance(payload, Mapping):
            raise DeferredGateError(f"awaiting must be an object, got {payload!r}")
        kind = payload.get("kind")
        if kind != DEFERRED_VERIFICATION_REQUIRED:
            raise DeferredGateError(f"unknown awaiting kind {kind!r}")
        raw_ids = payload.get("instance_ids") or []
        if not isinstance(raw_ids, (list, tuple)):
            raise DeferredGateError("awaiting 'instance_ids' must be an array")
        return cls(
            kind=DEFERRED_VERIFICATION_REQUIRED,
            reason=str(payload.get("reason") or cls.PLAN_COMPLETION),
            instance_ids=tuple(str(value) for value in raw_ids if str(value).strip()),
        )

    @property
    def describe(self) -> str:
        count = len(self.instance_ids)
        what = "manual check" if count == 1 else "manual checks"
        when = {
            self.PLAN_COMPLETION: "before this plan can complete",
            self.BEFORE_STAGE: "before the next stage may start",
        }.get(self.reason, "before this run goes any further")
        return f"{count} deferred {what} must be answered {when}"


__all__ = [
    "CHECKPOINTS",
    "CHECKPOINT_BEFORE_STAGE_PREFIX",
    "CHECKPOINT_PLAN_COMPLETION",
    "DEFERRED_GATE_MARKER",
    "DEFERRED_VERIFICATION_REQUIRED",
    "HUMAN_GATE_CATEGORIES",
    "CheckOutcome",
    "CheckResult",
    "DeferredAnswer",
    "DeferredGateError",
    "DeferredHumanGate",
    "DeferredObligation",
    "DeferredVerificationRequired",
    "ObligationStatus",
    "before_stage_checkpoint",
    "is_before_stage",
]
