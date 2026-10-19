"""The remote a managed run pushes to, pinned, and what explains its tip.

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
    RemoteRecord                one engine push (a *receipt*) or one human
                                acknowledgement of movement the engine did
                                not make
    UnauthorizedRemoteMovement  the typed pause: the pinned ref is somewhere
                                the records do not explain

All three live in the plan-run state (``.sparring/plans/<run>.json``),
which the engine writes and reads. Nothing here ever reads prose:
``notes.md``, handoffs, stage output and reviewer output are not authority
for a push, a pin or an acknowledgement, whatever they say.

What the records explain
------------------------

The *expected* tip of the pinned ref is the last record's tip, or the
start tip when there are none. Any other observed tip is unauthorized
movement. A candidate that is already reachable from the pinned ref but is
covered by no record (and is not part of the stage's own base) is too --
the Sporely Stage 4b case, where a stage agent pushed its own unreviewed
commit. For a legacy run, pinned on first resume rather than at run start,
the tip observed then is taken as the start but explains no candidate: it
was never observed before anyone could have pushed to it.

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

from agent_sparring import engine_git
from agent_sparring.engine_git import GitConfigRefusal, GitContextError
from agent_sparring.git_context import (
    config_values,
    is_ancestor,
    is_full_sha,
    remote_tip,
    resolved_push_url,
    tracking_remote,
)

UNAUTHORIZED_REMOTE_MOVEMENT = "unauthorized_remote_movement"
PUSH_RECEIPT = "push_receipt"
MOVEMENT_ACKNOWLEDGED = "movement_acknowledged"

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
    acknowledgement is not a review, an acceptance or a push authorization.
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
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "RemoteRecord":
        if not isinstance(payload, Mapping):
            raise RemotePinError(f"a remote record must be an object, got {payload!r}")
        kind = _text(payload, "kind")
        if kind not in (PUSH_RECEIPT, MOVEMENT_ACKNOWLEDGED):
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
        )
        if kind == PUSH_RECEIPT and record.tip is None:
            raise RemotePinError("a push receipt must name the pushed commit")
        if kind == MOVEMENT_ACKNOWLEDGED and not record.by:
            raise RemotePinError("an acknowledgement must record who made it")
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


class RemoteMovementDetected(Exception):
    """Raised from an after-turn check so a driver can pause on it."""

    def __init__(self, movement: UnauthorizedRemoteMovement) -> None:
        super().__init__(movement.describe)
        self.movement = movement


def expected_tip(pin: RemotePin, records: Sequence[RemoteRecord]) -> str | None:
    return records[-1].tip if records else pin.start_tip


def _refuse(code: str, key: str, message: str) -> GitConfigRefusal:
    return GitConfigRefusal(code, key, message)


def pin_remote(repo_root: Path, branch: str, *, legacy: bool = False) -> RemotePin:
    """Pin ``branch``'s remote target as configured right now."""

    remote, remote_branch = tracking_remote(repo_root, branch)
    for value, what in ((remote, "remote"), (remote_branch, "remote branch"), (branch, "branch")):
        if not SAFE_REF_RE.match(value):
            raise RemotePinError(
                f"refusing to pin: the {what} is named {value!r}, which git would not read as a "
                "plain name"
            )
    snapshot = engine_git.capture_snapshot(Path(repo_root))
    urls = config_values(repo_root, f"remote.{remote}.url")
    push_urls = config_values(repo_root, f"remote.{remote}.pushurl")
    if len(urls) > 1 or len(push_urls) > 1:
        raise RemotePinError(
            f"refusing to pin: remote {remote!r} has {len(urls)} URL(s) and {len(push_urls)} push "
            "URL(s); the engine pushes to and verifies exactly one repository, so configure one"
        )
    url = urls[0] if urls else None
    push_url = push_urls[0] if push_urls else None
    ref = f"refs/heads/{remote_branch}"
    # The effective push destination -- after pushInsteadOf/insteadOf, and
    # absolute so a relative local path means the same repository from any
    # directory -- is what is pinned, pushed to and read from.
    destination = resolved_push_url(repo_root, remote) if (url or push_url) else None
    start_tip = remote_tip(repo_root, destination, ref, trusted=snapshot) if destination else None
    return RemotePin(
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        url=url,
        push_url=push_url,
        start_tip=start_tip,
        pinned_at=now(),
        legacy=legacy,
        trusted_config=snapshot,
        resolved=destination,
    )


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


def _require_url(pin: RemotePin) -> str:
    if pin.destination is None:
        raise RemotePinError(
            f"this run pinned no URL for remote {pin.remote!r} (none was configured when it was "
            "pinned), so nothing can be pushed to or verified on it"
        )
    return pin.destination


def observe(repo_root: Path, pin: RemotePin) -> str | None:
    """The pinned ref's tip right now, read at the pinned destination only
    -- the repository the engine pushes to -- in an isolated repository
    configured from the run's trusted snapshot."""

    require_pinned_target(repo_root, pin)
    return remote_tip(repo_root, _require_url(pin), pin.ref, trusted=pin.trusted_config)


def _covered(
    repo_root: Path,
    pin: RemotePin,
    records: Sequence[RemoteRecord],
    candidate_sha: str,
    stage_base: str | None,
) -> bool:
    explained = [record.tip for record in records if record.tip]
    if pin.start_tip and not pin.legacy:
        explained.append(pin.start_tip)
    if stage_base:
        explained.append(stage_base)
    return any(is_ancestor(repo_root, candidate_sha, sha) for sha in explained)


def check_movement(
    repo_root: Path,
    pin: RemotePin,
    records: Sequence[RemoteRecord],
    *,
    stage_id: str,
    candidate_sha: str | None = None,
    stage_base: str | None = None,
) -> tuple[str | None, UnauthorizedRemoteMovement | None]:
    """``(observed tip, deviation or None)`` for the pinned ref.

    Raises :class:`GitContextError` (including the typed
    :class:`GitConfigRefusal`) when the target was retargeted, the
    configuration changed, or the remote cannot be read.
    """

    observed = observe(repo_root, pin)
    expected = expected_tip(pin, records)

    def movement(detail: str) -> UnauthorizedRemoteMovement:
        return UnauthorizedRemoteMovement(
            stage_id=stage_id,
            remote=pin.remote,
            url=pin.destination,
            ref=pin.ref,
            expected_tip=expected,
            observed_tip=observed,
            candidate_sha=candidate_sha,
            detail=detail,
        )

    if observed != expected:
        return observed, movement(
            "the pinned ref moved and no engine push receipt or acknowledgement explains it"
        )
    if (
        candidate_sha is not None
        and observed is not None
        and is_ancestor(repo_root, candidate_sha, observed)
        and not _covered(repo_root, pin, records, candidate_sha, stage_base)
    ):
        return observed, movement(
            f"candidate {candidate_sha} is already reachable from the pinned ref, but no engine "
            "push receipt or acknowledgement covers it"
        )
    return observed, None


def verify_pinned(repo_root: Path, pin: RemotePin, candidate_sha: str) -> tuple[bool, str]:
    """Is ``candidate_sha`` reachable from the pinned ref, read at the pinned
    URL? The pinned counterpart of :func:`~agent_sparring.git_context.
    verify_pushed`."""

    tip = observe(repo_root, pin)
    where = f"{pin.remote}/{pin.remote_branch} (pinned {pin.destination})"
    if tip is None:
        return False, f"{where} does not exist"
    if tip == candidate_sha:
        return True, f"{where} is exactly {candidate_sha}"
    if is_ancestor(repo_root, candidate_sha, tip):
        return True, f"{candidate_sha} is reachable from {where} ({tip})"
    return False, f"{candidate_sha} is not reachable from {where} ({tip})"


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


def pin_sibling(root: Path, branch: str) -> SiblingRemotePin:
    """Pin a declared sibling repository as configured right now, exactly
    as :func:`pin_remote` pins the primary."""

    return SiblingRemotePin(path=_resolved(root), pin=pin_remote(Path(root), branch))


@dataclass(frozen=True)
class RunRemoteState:
    """The remote records of one plan run, as its engine state holds them."""

    run: str
    pin: RemotePin | None
    records: tuple[RemoteRecord, ...]
    movement: UnauthorizedRemoteMovement | None
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
        UnauthorizedRemoteMovement.from_dict(awaiting)
        if isinstance(awaiting, Mapping) and awaiting.get("kind") == UNAUTHORIZED_REMOTE_MOVEMENT
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
    "MOVEMENT_ACKNOWLEDGED",
    "PUSH_RECEIPT",
    "SAFE_REF_RE",
    "UNAUTHORIZED_REMOTE_MOVEMENT",
    "RemoteMovementDetected",
    "RemotePin",
    "RemotePinError",
    "RemoteRecord",
    "RunRemoteState",
    "SiblingRemotePin",
    "UnauthorizedRemoteMovement",
    "acknowledgement",
    "check_movement",
    "expected_tip",
    "find_sibling_pin",
    "load_run_remote_state",
    "observe",
    "pin_remote",
    "pin_sibling",
    "push_receipt",
    "records_from",
    "require_pinned_target",
    "sibling_pins_from",
    "verify_pinned",
    "who",
]
