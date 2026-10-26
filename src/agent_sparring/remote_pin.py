"""The remote records of legacy runs: pins, receipts and remote pauses.

Local-first (Plan L): no run pins, observes or pushes to a remote any more,
and nothing here reads one (I-L1). These records load inertly from legacy
run state, and the recorded incident pauses stay answerable with the
existing local commands; none of it authorizes, accepts or finishes
anything. The rest of this docstring records the Plan R design they come
from.

A managed run's candidate reaches exactly one remote ref. Before this
module, which ref that was came from ``branch.<b>.remote``/``.merge`` and
``remote.<r>.url`` each time it was needed, so changing the configuration
mid-run retargeted the engine's own push and reachability checks. And the
push gate counted a candidate that was already reachable as success,
whoever had put it there: an agent that pushed an unreviewed commit to the
run's branch was indistinguishable from the engine pushing a reviewed one.

So the destination is a record, and so is every movement of it:

    RemotePin                   remote name, URL(s), branch and remote ref,
                                pinned when the run starts, with the ref's
                                tip then (or its absence) and the trusted
                                snapshot of the repository's own
                                security-relevant configuration
    RemoteRecord                one engine push (a *receipt*), one human
                                acknowledgement of movement the engine did
                                not make, or one person's explicit
                                authorization of a candidate already on the
                                remote without an engine push
    UnauthorizedRemoteMovement  the typed pause: the pinned ref is somewhere
                                the records do not explain
    PushedCandidateUnauthorized the typed pause: the ref is where the records
                                say, but the candidate is already on it and
                                no engine receipt explains it

All of these live in the plan-run state (``.sparring/plans/<run>.json``),
which the engine writes and reads. That the run *was* pinned is recorded
outside the project directory as well (see
:mod:`agent_sparring.managed_run`), so a missing pin is a refusal
(``remote_pin_missing``), never "no pin". Nothing here ever reads prose:
``notes.md``, handoffs, stage output and reviewer output are not authority
for a push, a pin or an acknowledgement, whatever they say.

What the records explain
------------------------

The *expected* tip of the pinned ref is the last record's tip, or the
start tip when there are none. Any other observed tip is unauthorized
movement.

A *candidate* is explained only by an engine push receipt (it is reachable
from a pushed tip), by the pinned start tip, by the stage's own base, or by
a person's ``authorized_without_push`` record naming exactly that commit.
An acknowledgement records an observed movement and nothing else: it clears
the movement pause and becomes the baseline for detecting *further*
movement, but it never authorizes a push and never explains a candidate. A
candidate already reachable from the pinned ref that nothing explains --
the Sporely Stage 4b case, where a stage agent pushed its own unreviewed
commit -- is ``pushed_candidate_unauthorized``, which only the explicit
per-commit action clears (a run-level push grant does not). Neither that
action nor an acknowledgement is a review: acceptance still needs the
independent reviewer's verdict for the exact commit.

For a legacy run, pinned on first resume rather than at run start, the tip
observed then is taken as the start but explains no candidate: it was never
observed before anyone could have pushed to it, so a candidate on it is
unauthorized movement until a person has looked.

This is detection, not prevention. It watches only the pinned ref, and an
agent that can run Git as the user can still write to it, to other refs
and to the engine's own state; confining the agent is a separate step.
The check is an audit trail and a blocking backstop: it stops the push,
the freeze and the acceptance until a person has looked.
"""

from __future__ import annotations

import getpass
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_sparring.engine_git import GitConfigRefusal, GitContextError
from agent_sparring.git_context import (
    config_values,
    is_full_sha,
    resolved_push_url,
    tracking_remote,
)

UNAUTHORIZED_REMOTE_MOVEMENT = "unauthorized_remote_movement"
PUSHED_CANDIDATE_UNAUTHORIZED = "pushed_candidate_unauthorized"
PUSH_RECEIPT = "push_receipt"
MOVEMENT_ACKNOWLEDGED = "movement_acknowledged"
AUTHORIZED_WITHOUT_PUSH = "authorized_without_push"
#: A run the engine's record says was pinned has no readable pin.
REMOTE_PIN_MISSING = "remote_pin_missing"

#: How an acknowledgement names a ref that the observed state says is gone.
ABSENT = "absent"

# A remote name or branch that starts with "-" would be read by git as an
# option rather than as the thing it names.
SAFE_REF_RE = re.compile(r"^[^-][^\s:^~?*\[\\]*$")


class RemotePinError(GitContextError):
    """A malformed record, or a pin that cannot be made."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def who() -> str:
    """The person running this process, as the operating system names them."""

    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RemotePinError(f"field {key!r} must be a non-empty string")
    return value


def _optional(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RemotePinError(f"field {key!r} must be a string or null")
    return value or None


def _optional_sha(payload: Mapping[str, Any], key: str) -> str | None:
    value = _optional(payload, key)
    if value is not None and not is_full_sha(value):
        raise RemotePinError(f"field {key!r} must be a full commit id or null, got {value!r}")
    return value


@dataclass(frozen=True)
class RemotePin:
    """Where this run's candidates go, fixed for the life of the run."""

    branch: str
    remote: str
    remote_branch: str
    #: ``remote.<remote>.url`` as configured at pin time; ``None`` when the
    #: remote had no URL then, in which case nothing can be pushed or
    #: verified for this run.
    url: str | None
    #: ``remote.<remote>.pushurl`` at pin time, when one was configured: the
    #: trusted snapshot's own push destination.
    push_url: str | None
    #: The pinned ref's commit at pin time; ``None`` when it did not exist.
    start_tip: str | None
    pinned_at: str
    #: True for a run recorded before pins existed and pinned on its first
    #: resume: weaker, because its start tip was observed only then.
    legacy: bool = False
    #: The repository's own watched configuration at pin time (see
    #: :func:`agent_sparring.engine_git.repository_snapshot`).
    trusted_config: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: Where ``git push <remote>`` actually wrote at pin time: the push URL
    #: after Git's own rewriting (``pushInsteadOf``, ``insteadOf``), made
    #: absolute. The engine pushes to and reads from exactly this, with no
    #: further rewriting.
    resolved: str | None = None

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.remote_branch}"

    @property
    def destination(self) -> str | None:
        """The one repository this run writes to -- and therefore the one it
        reads the pinned ref from, so a push and its verification can never
        be about two different repositories."""

        return self.resolved or self.push_url or self.url

    @property
    def push_destination(self) -> str | None:
        return self.destination

    @property
    def describe(self) -> str:
        tip = self.start_tip or "absent"
        where = self.url or "no URL"
        extra = f", pushes to {self.destination}" if self.destination != self.url else ""
        return f"{self.remote} ({where}{extra}) {self.ref}, tip then {tip}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "remote": self.remote,
            "remote_branch": self.remote_branch,
            "url": self.url,
            "push_url": self.push_url,
            "start_tip": self.start_tip,
            "pinned_at": self.pinned_at,
            "legacy": self.legacy,
            "trusted_config": [[key, value] for key, value in self.trusted_config],
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RemotePin":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"remote_pin must be an object, got {payload!r}")
        legacy = payload.get("legacy", False)
        if not isinstance(legacy, bool):
            raise RemotePinError("field 'legacy' must be true or false")
        raw_config = payload.get("trusted_config") or []
        if not isinstance(raw_config, list) or not all(
            isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
            for item in raw_config
        ):
            raise RemotePinError("field 'trusted_config' must be a list of [key, value] pairs")
        return cls(
            branch=_text(payload, "branch"),
            remote=_text(payload, "remote"),
            remote_branch=_text(payload, "remote_branch"),
            url=_optional(payload, "url"),
            push_url=_optional(payload, "push_url"),
            start_tip=_optional_sha(payload, "start_tip"),
            pinned_at=_text(payload, "pinned_at"),
            legacy=legacy,
            trusted_config=tuple((key, value) for key, value in raw_config),
            resolved=_optional(payload, "resolved"),
        )


@dataclass(frozen=True)
class RemoteRecord:
    """One explained movement of the pinned ref.

    ``PUSH_RECEIPT``: the engine pushed ``tip`` over ``previous_tip``.
    ``MOVEMENT_ACKNOWLEDGED``: a person, ``by``, acknowledged that the ref
    was at ``tip`` when the records explained ``previous_tip``. An
    acknowledgement is not a review, an acceptance or a push authorization,
    and it explains no candidate.
    ``AUTHORIZED_WITHOUT_PUSH``: a person, ``by``, authorized exactly
    ``candidate_sha`` of ``stage_id``, already on the ref at ``tip``, as
    though the engine had pushed it. Not a review and not an acceptance.
    """

    kind: str
    run: str
    stage_id: str | None
    remote: str
    url: str | None
    ref: str
    previous_tip: str | None
    tip: str | None
    recorded_at: str
    by: str | None = None
    #: ``AUTHORIZED_WITHOUT_PUSH`` only: the one commit it authorizes.
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "run": self.run,
            "stage_id": self.stage_id,
            "remote": self.remote,
            "url": self.url,
            "ref": self.ref,
            "previous_tip": self.previous_tip,
            "tip": self.tip,
            "recorded_at": self.recorded_at,
        }
        if self.by is not None:
            payload["by"] = self.by
        if self.candidate_sha is not None:
            payload["candidate_sha"] = self.candidate_sha
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "RemoteRecord":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"a remote record must be an object, got {payload!r}")
        kind = _text(payload, "kind")
        if kind not in (PUSH_RECEIPT, MOVEMENT_ACKNOWLEDGED, AUTHORIZED_WITHOUT_PUSH):
            raise RemotePinError(f"unknown remote record kind {kind!r}")
        record = cls(
            kind=kind,
            run=_text(payload, "run"),
            stage_id=_optional(payload, "stage_id"),
            remote=_text(payload, "remote"),
            url=_optional(payload, "url"),
            ref=_text(payload, "ref"),
            previous_tip=_optional_sha(payload, "previous_tip"),
            tip=_optional_sha(payload, "tip"),
            recorded_at=_text(payload, "recorded_at"),
            by=_optional(payload, "by"),
            candidate_sha=_optional_sha(payload, "candidate_sha"),
        )
        if kind == PUSH_RECEIPT and record.tip is None:
            raise RemotePinError("a push receipt must name the pushed commit")
        if kind in (MOVEMENT_ACKNOWLEDGED, AUTHORIZED_WITHOUT_PUSH) and not record.by:
            raise RemotePinError(f"a {kind} record must record who made it")
        if kind == AUTHORIZED_WITHOUT_PUSH and not (record.candidate_sha and record.stage_id):
            raise RemotePinError(
                "an authorization without push must name the stage and the exact commit"
            )
        if kind != AUTHORIZED_WITHOUT_PUSH and record.candidate_sha is not None:
            raise RemotePinError(f"a {kind} record names no candidate")
        return record


@dataclass(frozen=True)
class UnauthorizedRemoteMovement:
    """The typed pause: the pinned ref is not where the records say.

    Blocks the engine's push, the freeze and the acceptance until a person
    acknowledges the observed tip (``sparring acknowledge-remote-movement``).
    """

    stage_id: str
    remote: str
    url: str | None
    ref: str
    expected_tip: str | None
    observed_tip: str | None
    candidate_sha: str | None = None
    detail: str | None = None
    kind: str = UNAUTHORIZED_REMOTE_MOVEMENT

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "stage_id": self.stage_id,
            "remote": self.remote,
            "url": self.url,
            "ref": self.ref,
            "expected_tip": self.expected_tip,
            "observed_tip": self.observed_tip,
        }
        if self.candidate_sha:
            payload["candidate_sha"] = self.candidate_sha
        if self.detail:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "UnauthorizedRemoteMovement":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"awaiting must be an object, got {payload!r}")
        if payload.get("kind") != UNAUTHORIZED_REMOTE_MOVEMENT:
            raise RemotePinError(f"not an {UNAUTHORIZED_REMOTE_MOVEMENT} pause: {payload!r}")
        return cls(
            stage_id=_text(payload, "stage_id"),
            remote=_text(payload, "remote"),
            url=_optional(payload, "url"),
            ref=_text(payload, "ref"),
            expected_tip=_optional_sha(payload, "expected_tip"),
            observed_tip=_optional_sha(payload, "observed_tip"),
            candidate_sha=_optional_sha(payload, "candidate_sha"),
            detail=_optional(payload, "detail"),
        )

    @property
    def observed_label(self) -> str:
        return self.observed_tip or ABSENT

    @property
    def describe(self) -> str:
        return (
            f"{self.remote} {self.ref} is at {self.observed_tip or 'nothing (absent)'}, but the "
            f"engine's records explain {self.expected_tip or 'nothing (absent)'}"
            + (f" ({self.detail})" if self.detail else "")
        )


@dataclass(frozen=True)
class PushedCandidateUnauthorized:
    """The typed pause: ``candidate_sha`` is already reachable from the
    pinned ref, which is where the records say, but no engine receipt, start
    tip or stage base explains the candidate itself.

    Cleared only by a person's explicit per-commit action naming the run and
    this exact commit (``sparring authorize-pushed-candidate``), recorded as
    ``authorized_without_push``. A run-level push grant does not clear it,
    and an acknowledgement does not explain it.
    """

    stage_id: str
    candidate_sha: str
    remote: str
    url: str | None
    ref: str
    observed_tip: str | None
    detail: str | None = None
    kind: str = PUSHED_CANDIDATE_UNAUTHORIZED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "stage_id": self.stage_id,
            "candidate_sha": self.candidate_sha,
            "remote": self.remote,
            "url": self.url,
            "ref": self.ref,
            "observed_tip": self.observed_tip,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "PushedCandidateUnauthorized":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"awaiting must be an object, got {payload!r}")
        if payload.get("kind") != PUSHED_CANDIDATE_UNAUTHORIZED:
            raise RemotePinError(f"not a {PUSHED_CANDIDATE_UNAUTHORIZED} pause: {payload!r}")
        candidate = _optional_sha(payload, "candidate_sha")
        if candidate is None:
            raise RemotePinError("a pushed_candidate_unauthorized pause must name its candidate")
        return cls(
            stage_id=_text(payload, "stage_id"),
            candidate_sha=candidate,
            remote=_text(payload, "remote"),
            url=_optional(payload, "url"),
            ref=_text(payload, "ref"),
            observed_tip=_optional_sha(payload, "observed_tip"),
            detail=_optional(payload, "detail"),
        )

    @property
    def describe(self) -> str:
        return (
            f"candidate {self.candidate_sha} is already on {self.remote} {self.ref} (at "
            f"{self.observed_tip or 'nothing (absent)'}), but no engine push receipt explains it"
        )


#: Either typed pause about the pinned ref.
RemotePause = UnauthorizedRemoteMovement | PushedCandidateUnauthorized

REMOTE_PAUSE_KINDS = (UNAUTHORIZED_REMOTE_MOVEMENT, PUSHED_CANDIDATE_UNAUTHORIZED)


def remote_pause_from_dict(payload: Any) -> "RemotePause":
    if isinstance(payload, Mapping) and payload.get("kind") == PUSHED_CANDIDATE_UNAUTHORIZED:
        return PushedCandidateUnauthorized.from_dict(payload)
    return UnauthorizedRemoteMovement.from_dict(payload)


class RemoteMovementDetected(Exception):
    """Raised from an after-turn check so a driver can pause on it."""

    def __init__(self, movement: "RemotePause") -> None:
        super().__init__(movement.describe)
        self.movement = movement


def expected_tip(pin: RemotePin, records: Sequence[RemoteRecord]) -> str | None:
    return records[-1].tip if records else pin.start_tip


def _refuse(code: str, key: str, message: str) -> GitConfigRefusal:
    return GitConfigRefusal(code, key, message)


def require_pinned_target(repo_root: Path, pin: RemotePin) -> None:
    """Refuse, naming the key, if tracking or URL configuration no longer
    says what it said when the run was pinned. The engine never follows a
    retarget; it stops."""

    remote, remote_branch = tracking_remote(repo_root, pin.branch)
    if remote != pin.remote:
        raise _refuse(
            "remote_retargeted",
            f"branch.{pin.branch}.remote",
            f"refusing: {pin.branch} now tracks remote {remote!r}, but this run is pinned to "
            f"{pin.remote!r}",
        )
    if remote_branch != pin.remote_branch:
        raise _refuse(
            "remote_retargeted",
            f"branch.{pin.branch}.merge",
            f"refusing: {pin.branch} now tracks {remote}/{remote_branch}, but this run is pinned "
            f"to {pin.remote}/{pin.remote_branch}",
        )
    for key, pinned in (
        (f"remote.{pin.remote}.url", pin.url),
        (f"remote.{pin.remote}.pushurl", pin.push_url),
    ):
        current = config_values(repo_root, key)
        if current != ((pinned,) if pinned is not None else ()):
            raise _refuse(
                "remote_retargeted",
                key,
                f"refusing: {key} is now {current!r}, but this run is pinned to {pinned!r}",
            )
    if pin.resolved is not None:
        # The same URLs can still resolve elsewhere through a rewrite.
        now_resolved = resolved_push_url(repo_root, pin.remote)
        if now_resolved != pin.resolved:
            raise _refuse(
                "remote_retargeted",
                f"remote.{pin.remote}.pushurl",
                f"refusing: a push to {pin.remote} would now go to {now_resolved!r}, but this run "
                f"is pinned to {pin.resolved!r}",
            )


def push_receipt(
    *, run: str, stage_id: str, pin: RemotePin, previous_tip: str | None, pushed_sha: str
) -> RemoteRecord:
    return RemoteRecord(
        kind=PUSH_RECEIPT,
        run=run,
        stage_id=stage_id,
        remote=pin.remote,
        url=pin.push_destination,
        ref=pin.ref,
        previous_tip=previous_tip,
        tip=pushed_sha,
        recorded_at=now(),
    )


def acknowledgement(
    *, run: str, movement: UnauthorizedRemoteMovement, by: str
) -> RemoteRecord:
    return RemoteRecord(
        kind=MOVEMENT_ACKNOWLEDGED,
        run=run,
        stage_id=movement.stage_id,
        remote=movement.remote,
        url=movement.url,
        ref=movement.ref,
        previous_tip=movement.expected_tip,
        tip=movement.observed_tip,
        recorded_at=now(),
        by=by,
    )


def authorization_without_push(
    *, run: str, pause: PushedCandidateUnauthorized, by: str
) -> RemoteRecord:
    """A person's per-commit authorization of the candidate ``pause`` names.
    Its tip is the tip observed with it, so it is also the baseline for
    detecting further movement -- whatever happens to an earlier
    acknowledgement."""

    return RemoteRecord(
        kind=AUTHORIZED_WITHOUT_PUSH,
        run=run,
        stage_id=pause.stage_id,
        remote=pause.remote,
        url=pause.url,
        ref=pause.ref,
        previous_tip=pause.observed_tip,
        tip=pause.observed_tip,
        recorded_at=now(),
        by=by,
        candidate_sha=pause.candidate_sha,
    )


def records_from(payload: Any) -> tuple[RemoteRecord, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise RemotePinError("remote_records must be an array")
    return tuple(RemoteRecord.from_dict(entry) for entry in payload)


@dataclass(frozen=True)
class SiblingRemotePin:
    """A declared sibling repository's remote target and trusted snapshot,
    pinned when the run started -- the same :class:`RemotePin` shape as
    the primary's, for the repository at ``path`` (resolved)."""

    path: str
    pin: RemotePin

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "pin": self.pin.to_dict()}

    @classmethod
    def from_dict(cls, payload: Any) -> "SiblingRemotePin":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"a sibling pin must be an object, got {payload!r}")
        return cls(path=_text(payload, "path"), pin=RemotePin.from_dict(payload.get("pin")))


def sibling_pins_from(payload: Any) -> tuple[SiblingRemotePin, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise RemotePinError("sibling_pins must be an array")
    return tuple(SiblingRemotePin.from_dict(entry) for entry in payload)


def _resolved(path: Path | str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def find_sibling_pin(
    pins: Sequence[SiblingRemotePin], root: Path, branch: str
) -> RemotePin | None:
    """The run-start pin of the sibling repository at ``root`` for
    ``branch``, or ``None`` when the run pinned none."""

    wanted = _resolved(root)
    for entry in pins:
        if _resolved(entry.path) == wanted and entry.pin.branch == branch:
            return entry.pin
    return None


@dataclass(frozen=True)
class RunRemoteState:
    """The remote records of one plan run, as its engine state holds them."""

    run: str
    pin: RemotePin | None
    records: tuple[RemoteRecord, ...]
    movement: "RemotePause | None"
    #: The declared sibling repositories pinned at run start.
    siblings: tuple[SiblingRemotePin, ...] = ()


def load_run_remote_state(sparring_dir: Path, run_key: str) -> RunRemoteState | None:
    """Read a run's pin, records and pending movement pause from its
    engine-written state file, ``<sparring_dir>/plans/<run>.json``.
    ``None`` when there is no such run state."""

    path = Path(sparring_dir) / "plans" / f"{run_key}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemotePinError(f"cannot read plan-run state at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RemotePinError(f"plan-run state at {path} must be a JSON object")
    pin_payload = payload.get("remote_pin")
    awaiting = payload.get("awaiting")
    movement = (
        remote_pause_from_dict(awaiting)
        if isinstance(awaiting, Mapping) and awaiting.get("kind") in REMOTE_PAUSE_KINDS
        else None
    )
    return RunRemoteState(
        run=run_key,
        pin=RemotePin.from_dict(pin_payload) if pin_payload else None,
        records=records_from(payload.get("remote_records")),
        movement=movement,
        siblings=sibling_pins_from(payload.get("sibling_pins")),
    )


__all__ = [
    "ABSENT",
    "AUTHORIZED_WITHOUT_PUSH",
    "MOVEMENT_ACKNOWLEDGED",
    "PUSHED_CANDIDATE_UNAUTHORIZED",
    "PUSH_RECEIPT",
    "REMOTE_PAUSE_KINDS",
    "REMOTE_PIN_MISSING",
    "SAFE_REF_RE",
    "UNAUTHORIZED_REMOTE_MOVEMENT",
    "PushedCandidateUnauthorized",
    "RemoteMovementDetected",
    "RemotePause",
    "RemotePin",
    "RemotePinError",
    "RemoteRecord",
    "RunRemoteState",
    "SiblingRemotePin",
    "UnauthorizedRemoteMovement",
    "acknowledgement",
    "authorization_without_push",
    "expected_tip",
    "find_sibling_pin",
    "load_run_remote_state",
    "push_receipt",
    "records_from",
    "remote_pause_from_dict",
    "require_pinned_target",
    "sibling_pins_from",
    "who",
]
