"""Push authorization records, kept so legacy runs still load.

Local-first (Plan L): the engine performs no push and reads no remote
(I-L1). Acceptance is local, and the function that pushed a candidate is
gone; nothing here contacts a remote. What remains is data -- the
:class:`PushAuthorization` and :class:`PushRequired` records a legacy run's
state may hold, which load inertly and never authorize anything -- and the
pure helpers that describe a push target (:func:`push_command`), which the
separate Push to review (Plan P) is specified against. The rest of this
docstring records what the removed push did under Plan R.

The acceptance gate only ever freezes a commit that is *already* reachable
from the run's pinned remote ref (see :mod:`agent_sparring.remote_pin`).
That check is not relaxed here and is not bypassed here.

Reachability alone is not success. Before the pin existed, a candidate that
was already reachable was taken as pushed, whoever had pushed it -- which is
how a stage agent's own push of an unreviewed commit went through as
though the engine had made it (Sporely Stage 4b). Now the pinned ref's tip
must be what the engine's own records explain (its push receipts and a
person's acknowledgements); anything else is an
:class:`~agent_sparring.remote_pin.UnauthorizedRemoteMovement`, and nothing
is pushed, frozen or accepted until a person has looked. A candidate that
is reachable *and* explained -- by a receipt, the start tip, the stage base
or a person's per-commit ``authorized_without_push``, never by an
acknowledgement -- needs no push; one that is reachable but unexplained is
:class:`~agent_sparring.remote_pin.PushedCandidateUnauthorized`.

What this module adds is the missing step in front of it. A reviewer says
READY, the reviewed work is committed, and the commit was never pushed --
typically because the project's own agent instructions forbid an agent from
pushing without a person saying so. The managed run then reached the gate,
the gate refused, and the only thing that could unblock it was a person
pushing by hand. Worse, the reviewer could express "someone must authorize a
push" as a NEEDS_YOU human check, and a human answering *Pass* to that check
authorized nothing: it recorded prose as evidence, the plan resumed, the
reviewer said READY again, and the gate refused again.

So push authorization is a capability of the engine, expressed as data:

    PushAuthorization   what a person has allowed, and for exactly what
    PushRequired        a typed statement that the run is stopped because a
                        verified candidate needs that permission, carrying
                        the exact commit, remote and branch involved

Both are persisted in the plan-run state (:class:`~agent_sparring.plan.
PlanRunState`), so an authorization survives a resume and a reload, and a
consumer can tell "this run is waiting for push permission" from "this run
is waiting for a manual test" without reading a single sentence of prose.

Two scopes, and nothing wider
-----------------------------

``CANDIDATE`` authorizes exactly one commit, of exactly one stage, of this
run. ``RUN`` authorizes the verified candidates this managed run produces
from now on -- the "don't stop for every push" answer -- and still only
within the identity it was granted for. Both bind to:

- this plan run (the state file that holds them *is* the run);
- this repository/worktree, by resolved path;
- the exact expected branch the run was started for;
- the exact remote, remote URL and remote branch the run pinned.

``CANDIDATE`` additionally binds to the stage id and the candidate SHA, so a
candidate that moved is a candidate nobody authorized.

There is deliberately no global switch, no project-level default and no
"always push" mode: an authorization is a fact about one run, granted by a
person, recorded where that run's state lives.

What an authorized push is allowed to be
----------------------------------------

Exactly one command shape, built by :func:`push_command` and by nothing
else::

    git -c push.followTags=false push --porcelain <pinned URL> <candidate sha>:refs/heads/<remote branch>

The destination is the URL the run pinned, never a remote name, so no
``remote.<name>.*`` configuration can redirect it; the source is the exact
authorized commit, not a branch that could move between the check and the
push; the destination ref is fully qualified, so no ``push.default``,
``remote.<name>.push`` or tracking configuration can widen what is sent.
The source is non-empty, so the refspec cannot delete a ref. There is no
``--force``, no ``--force-with-lease``, no ``--tags``, no ``--delete``, no
``--all``, no ``--mirror`` and no ``--prune``; ``push.followTags`` is turned
off for the invocation so a tag cannot ride along. Nothing here checks out,
switches, creates or merges a branch, and nothing here touches a sibling
repository: a declared sibling is a different repository, and authorizing
this run's candidate says nothing about it.

The push reports its own old->new (``--porcelain``). Unless that is exactly
the tip observed before it -> the candidate, someone else wrote the ref in
between, and the outcome is unauthorized remote movement, not a receipt;
so is a push that changed nothing. After the push the pinned ref is read
again at the pinned URL and must be exactly the pushed commit. A push that reported success but left anything
else there is a failure, and the candidate is not accepted. A successful
push yields a :class:`~agent_sparring.remote_pin.RemoteRecord` receipt for
the caller to record. The push itself runs through
:mod:`agent_sparring.engine_git`: no hooks, and no configuration changed
since the run started.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.git_context import is_full_sha, tracking_remote
from agent_sparring.remote_pin import SAFE_REF_RE, RemotePin

#: The typed reason a managed run can be stopped on. One value today; it is
#: a closed field rather than a free-form string so a consumer switches on it
#: instead of matching prose, and so a later reason is a new value rather
#: than a new format.
PUSH_AUTHORIZATION_REQUIRED = "push_authorization_required"

# A remote name or branch that starts with "-" would be read by git as an
# option rather than as the thing it names. Every value here comes from git
# itself (config, symbolic-ref), so this is a guard against a repository
# configured to attack its own tooling, not against ordinary input.
_SAFE_REF_RE = SAFE_REF_RE


class PushError(RuntimeError):
    """The push, or the verification of its result, failed.

    Never raised for *missing permission* -- that is not a failure but a
    question, and it is reported as :class:`PushRequired`.
    """


class PushScope(str, Enum):
    """How far one person's permission reaches."""

    CANDIDATE = "candidate"
    RUN = "run"

    @classmethod
    def from_str(cls, value: str) -> "PushScope":
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in cls)
            raise PushError(
                f"unknown push-authorization scope {value!r}; expected one of: {valid}"
            ) from exc


@dataclass(frozen=True)
class PushAuthorization:
    """What a person allowed, and for exactly what.

    ``repo_root`` is the resolved worktree the permission was granted in. A
    state file that ends up somewhere else (a copied checkout, a moved
    worktree) therefore carries an authorization that does not apply, and the
    run asks again rather than pushing from a repository nobody authorized.
    """

    scope: PushScope
    repo_root: str
    branch: str
    remote: str
    remote_branch: str
    #: CANDIDATE only: the stage and commit the permission is about.
    stage_id: str | None = None
    candidate_sha: str | None = None
    #: The pinned push destination (URL) the permission was granted for.
    #: ``None`` only in authorizations recorded before pins existed, which
    #: then bind by remote name and remote branch as they always did.
    url: str | None = None

    def __post_init__(self) -> None:
        if self.scope is PushScope.CANDIDATE and not (self.stage_id and self.candidate_sha):
            raise PushError(
                "a one-candidate push authorization must name the stage and the exact "
                "candidate commit it authorizes"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": self.scope.value,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "remote": self.remote,
            "remote_branch": self.remote_branch,
        }
        if self.scope is PushScope.CANDIDATE:
            payload["stage_id"] = self.stage_id
            payload["candidate_sha"] = self.candidate_sha
        if self.url is not None:
            payload["url"] = self.url
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "PushAuthorization":
        if not isinstance(payload, Mapping):
            raise PushError(f"push_authorization must be an object, got {payload!r}")
        scope = PushScope.from_str(str(payload.get("scope", "")))
        return cls(
            scope=scope,
            repo_root=_text(payload, "repo_root"),
            branch=_text(payload, "branch"),
            remote=_text(payload, "remote"),
            remote_branch=_text(payload, "remote_branch"),
            stage_id=_optional_text(payload, "stage_id"),
            candidate_sha=_optional_text(payload, "candidate_sha"),
            url=_optional_text(payload, "url"),
        )

    @property
    def describe(self) -> str:
        if self.scope is PushScope.RUN:
            return (
                f"every verified candidate of this run may be pushed to "
                f"{self.remote}/{self.remote_branch}"
            )
        return (
            f"candidate {self.candidate_sha} of stage {self.stage_id!r} may be pushed to "
            f"{self.remote}/{self.remote_branch}"
        )

    def covers(self, *, repo_root: Path, branch: str, target: "PushTarget", stage_id: str) -> bool:
        """Does this permission allow pushing ``target`` right now?

        Every field is compared; nothing is inferred and nothing is
        near-matched. A permission for another worktree, another branch,
        another remote, another remote branch, another stage or another
        commit does not apply, and a run holding one asks again.
        """

        if str(Path(repo_root).resolve()) != self.repo_root:
            return False
        if branch != self.branch:
            return False
        if target.remote != self.remote or target.remote_branch != self.remote_branch:
            return False
        if self.url is not None and target.url != self.url:
            return False
        if self.scope is PushScope.RUN:
            return True
        return stage_id == self.stage_id and target.candidate_sha == self.candidate_sha


@dataclass(frozen=True)
class PushTarget:
    """The exact commit, and the exact remote ref it has to reach."""

    candidate_sha: str
    branch: str
    remote: str
    remote_branch: str
    #: The pinned push destination. ``None`` only for a target built without
    #: a pin; :func:`push_command` then names the remote, as it used to.
    url: str | None = None


@dataclass(frozen=True)
class PushRequired:
    """A verified candidate that cannot be accepted until a person allows one
    push -- recorded in the plan-run state so the pause is typed, durable and
    renderable without reading prose.

    ``detail`` is git's own account of why the candidate is not on the remote
    yet. It is diagnostic; the four identifying fields above it are what a
    consumer renders and what an authorization is checked against.
    """

    kind: str
    stage_id: str
    candidate_sha: str
    branch: str
    remote: str
    remote_branch: str
    detail: str | None = None

    @classmethod
    def for_target(cls, stage_id: str, target: PushTarget, detail: str | None) -> "PushRequired":
        return cls(
            kind=PUSH_AUTHORIZATION_REQUIRED,
            stage_id=stage_id,
            candidate_sha=target.candidate_sha,
            branch=target.branch,
            remote=target.remote,
            remote_branch=target.remote_branch,
            detail=detail,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "stage_id": self.stage_id,
            "candidate_sha": self.candidate_sha,
            "branch": self.branch,
            "remote": self.remote,
            "remote_branch": self.remote_branch,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "PushRequired":
        if not isinstance(payload, Mapping):
            raise PushError(f"awaiting must be an object, got {payload!r}")
        kind = _text(payload, "kind")
        if kind != PUSH_AUTHORIZATION_REQUIRED:
            raise PushError(f"unknown awaiting kind {kind!r}")
        return cls(
            kind=kind,
            stage_id=_text(payload, "stage_id"),
            candidate_sha=_text(payload, "candidate_sha"),
            branch=_text(payload, "branch"),
            remote=_text(payload, "remote"),
            remote_branch=_text(payload, "remote_branch"),
            detail=_optional_text(payload, "detail"),
        )

    @property
    def describe(self) -> str:
        return (
            f"candidate {self.candidate_sha} of stage {self.stage_id!r} is verified but is "
            f"not on {self.remote}/{self.remote_branch}, and nothing has authorized a push"
        )


def intended_remote(repo_root: Path, branch: str) -> tuple[str, str]:
    """The remote and remote branch the acceptance gate checks against.

    Exactly what :func:`~agent_sparring.git_context.verify_pushed` resolves,
    read from the branch's own tracking configuration with ``origin`` and the
    branch's own name as the fallback. The intended remote is therefore never
    chosen here and never guessed: it is whatever the gate already looks at.
    """

    remote, remote_branch = tracking_remote(repo_root, branch)
    for value, what in ((remote, "remote"), (remote_branch, "remote branch"), (branch, "branch")):
        if not _SAFE_REF_RE.match(value):
            raise PushError(
                f"refusing to push: the intended {what} is named {value!r}, which git would "
                "not read as a plain name"
            )
    return remote, remote_branch


def intended_target(repo_root: Path, candidate_sha: str, branch: str) -> PushTarget:
    """Where the acceptance gate will look for ``candidate_sha``."""

    remote, remote_branch = intended_remote(repo_root, branch)
    return PushTarget(
        candidate_sha=candidate_sha, branch=branch, remote=remote, remote_branch=remote_branch
    )


def push_command(target: PushTarget) -> list[str]:
    """The one git invocation an authorized push is ever allowed to be.

    Built here and nowhere else, so what is permitted can be read in one
    place and asserted by one test. See the module docstring for why each
    part is the way it is.
    """

    return [
        "-c",
        "push.followTags=false",
        "push",
        "--porcelain",
        target.url or target.remote,
        f"{target.candidate_sha}:refs/heads/{target.remote_branch}",
    ]


def pinned_target(pin: RemotePin, candidate_sha: str) -> PushTarget:
    """Where ``candidate_sha`` goes for a run pinned to ``pin``."""

    return PushTarget(
        candidate_sha=candidate_sha,
        branch=pin.branch,
        remote=pin.remote,
        remote_branch=pin.remote_branch,
        url=pin.push_destination,
    )


def _binding(repo_root: Path, branch: str, pin: RemotePin | None) -> tuple[str, str, str | None]:
    if pin is None:
        remote, remote_branch = intended_remote(repo_root, branch)
        return remote, remote_branch, None
    if pin.branch != branch:
        raise PushError(f"this run is pinned for branch {pin.branch!r}, not {branch!r}")
    return pin.remote, pin.remote_branch, pin.push_destination


def authorization_for_candidate(
    repo_root: Path,
    *,
    stage_id: str,
    candidate_sha: str,
    branch: str,
    pin: RemotePin | None = None,
) -> PushAuthorization:
    """A one-candidate authorization for exactly ``candidate_sha``, bound to
    the run's pinned destination when there is one."""

    if not is_full_sha(candidate_sha):
        raise PushError(
            f"a one-candidate push authorization needs the exact 40-character commit id, "
            f"got {candidate_sha!r}"
        )
    remote, remote_branch, url = _binding(repo_root, branch, pin)
    return PushAuthorization(
        scope=PushScope.CANDIDATE,
        repo_root=str(Path(repo_root).resolve()),
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        stage_id=stage_id,
        candidate_sha=candidate_sha,
        url=url,
    )


def authorization_for_run(
    repo_root: Path, *, branch: str, pin: RemotePin | None = None
) -> PushAuthorization:
    """A run-scoped authorization: this run's future verified candidates, on
    this branch, to this pinned destination, in this worktree."""

    remote, remote_branch, url = _binding(repo_root, branch, pin)
    return PushAuthorization(
        scope=PushScope.RUN,
        repo_root=str(Path(repo_root).resolve()),
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        url=url,
    )


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PushError(f"field {key!r} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PushError(f"field {key!r} must be a string or null")
    return value.strip() or None


__all__ = [
    "PUSH_AUTHORIZATION_REQUIRED",
    "PushAuthorization",
    "PushError",
    "PushRequired",
    "PushScope",
    "PushTarget",
    "authorization_for_candidate",
    "authorization_for_run",
    "intended_remote",
    "intended_target",
    "pinned_target",
    "push_command",
]
