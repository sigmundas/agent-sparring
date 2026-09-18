"""Authorizing, performing and verifying the one push managed acceptance needs.

The acceptance gate only ever freezes a commit that is *already* reachable
from the intended remote branch (:func:`~agent_sparring.git_context.
verify_pushed`). That check is not relaxed here and is not bypassed here.

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
- the exact remote and remote branch the acceptance gate would check.

``CANDIDATE`` additionally binds to the stage id and the candidate SHA, so a
candidate that moved is a candidate nobody authorized.

There is deliberately no global switch, no project-level default and no
"always push" mode: an authorization is a fact about one run, granted by a
person, recorded where that run's state lives.

What an authorized push is allowed to be
----------------------------------------

Exactly one command shape, built by :func:`push_command` and by nothing
else::

    git -c push.followTags=false push <remote> refs/heads/<branch>:refs/heads/<remote branch>

Both sides of the refspec are fully qualified, so no ``push.default``,
``remote.<name>.push`` or tracking configuration can widen what is sent. The
source is non-empty, so the refspec cannot delete a ref. There is no
``--force``, no ``--force-with-lease``, no ``--tags``, no ``--delete``, no
``--all``, no ``--mirror`` and no ``--prune``; ``push.followTags`` is turned
off for the invocation so a tag cannot ride along. Nothing here checks out,
switches, creates or merges a branch, and nothing here touches a sibling
repository: a declared sibling is a different repository, and authorizing
this run's candidate says nothing about it.

After the push, reachability is proven again by the same
:func:`~agent_sparring.git_context.verify_pushed` the gate uses, against the
same remote ref. A push that reported success but left the candidate
unreachable is a failure, and the candidate is not accepted.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from agent_sparring.git_context import (
    GitContextError,
    current_branch,
    is_full_sha,
    resolve_commit,
    tracking_remote,
    verify_pushed,
)

#: The typed reason a managed run can be stopped on. One value today; it is
#: a closed field rather than a free-form string so a consumer switches on it
#: instead of matching prose, and so a later reason is a new value rather
#: than a new format.
PUSH_AUTHORIZATION_REQUIRED = "push_authorization_required"

# A remote name or branch that starts with "-" would be read by git as an
# option rather than as the thing it names. Every value here comes from git
# itself (config, symbolic-ref), so this is a guard against a repository
# configured to attack its own tooling, not against ordinary input.
_SAFE_REF_RE = re.compile(r"^[^-][^\s:^~?*\[\\]*$")


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


@dataclass(frozen=True)
class PushOutcome:
    """The result of asking whether the candidate can reach the remote.

    Exactly one of the three states is true:

    - ``required`` set: a person has to allow it; nothing was pushed;
    - ``pushed`` true: this call pushed it and re-proved reachability;
    - neither: it was already reachable and nothing was done.
    """

    target: PushTarget
    required: PushRequired | None = None
    pushed: bool = False
    detail: str | None = None

    @property
    def blocked(self) -> bool:
        return self.required is not None


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
        target.remote,
        f"refs/heads/{target.branch}:refs/heads/{target.remote_branch}",
    ]


def ensure_candidate_pushed(
    repo_root: Path,
    *,
    stage_id: str,
    expected_branch: str,
    authorization: PushAuthorization | None,
) -> PushOutcome:
    """Make the current candidate reachable from its intended remote branch,
    or say who has to allow that.

    Order matters and is the point:

    1. the worktree must be on ``expected_branch`` and ``HEAD`` must resolve
       (a push is about one exact commit of one exact branch);
    2. if the candidate is *already* reachable, nothing is pushed -- an
       authorization is permission, not an instruction;
    3. otherwise the intended remote ref is resolved and ``authorization`` is
       checked against the exact commit, branch, remote and remote branch. No
       permission means :class:`PushRequired`, and **nothing is pushed**;
    4. only then is :func:`push_command` run, and reachability re-proven
       afterwards. Either failure raises :class:`PushError`, leaving the
       candidate unaccepted.

    Nothing about the acceptance gate is relaxed by any of this: the gate
    runs afterwards and makes its own checks, including this same
    reachability check, for itself.
    """

    try:
        branch = current_branch(repo_root)
    except GitContextError as exc:
        raise PushError(str(exc)) from exc
    if branch != expected_branch:
        raise PushError(
            f"refusing to push: {repo_root} is on {branch!r}, but this run's candidate "
            f"belongs to {expected_branch!r}"
        )
    try:
        candidate_sha = resolve_commit(repo_root, "HEAD", label="candidate")
    except GitContextError as exc:
        raise PushError(str(exc)) from exc

    target = intended_target(repo_root, candidate_sha, branch)

    pushed, detail = verify_pushed(repo_root, candidate_sha, branch)
    if pushed:
        return PushOutcome(target=target, detail=detail)

    if authorization is None or not authorization.covers(
        repo_root=repo_root, branch=branch, target=target, stage_id=stage_id
    ):
        return PushOutcome(
            target=target, required=PushRequired.for_target(stage_id, target, detail)
        )

    result = subprocess.run(
        ["git", "-C", str(repo_root), *push_command(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PushError(
            f"pushing {candidate_sha} to {target.remote}/{target.remote_branch} failed: "
            f"{(result.stderr or result.stdout).strip() or f'git push exited {result.returncode}'}"
        )

    pushed, detail = verify_pushed(repo_root, candidate_sha, branch)
    if not pushed:
        raise PushError(
            f"{target.remote}/{target.remote_branch} was pushed, but {candidate_sha} is still "
            f"not reachable from it ({detail}); the candidate was not accepted"
        )
    return PushOutcome(target=target, pushed=True, detail=detail)


def authorization_for_candidate(
    repo_root: Path, *, stage_id: str, candidate_sha: str, branch: str
) -> PushAuthorization:
    """A one-candidate authorization for exactly ``candidate_sha``."""

    if not is_full_sha(candidate_sha):
        raise PushError(
            f"a one-candidate push authorization needs the exact 40-character commit id, "
            f"got {candidate_sha!r}"
        )
    remote, remote_branch = intended_remote(repo_root, branch)
    return PushAuthorization(
        scope=PushScope.CANDIDATE,
        repo_root=str(Path(repo_root).resolve()),
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        stage_id=stage_id,
        candidate_sha=candidate_sha,
    )


def authorization_for_run(repo_root: Path, *, branch: str) -> PushAuthorization:
    """A run-scoped authorization: this run's future verified candidates, on
    this branch, to this remote branch, in this worktree."""

    remote, remote_branch = intended_remote(repo_root, branch)
    return PushAuthorization(
        scope=PushScope.RUN,
        repo_root=str(Path(repo_root).resolve()),
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
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
    "PushOutcome",
    "PushRequired",
    "PushScope",
    "PushTarget",
    "authorization_for_candidate",
    "authorization_for_run",
    "ensure_candidate_pushed",
    "intended_remote",
    "intended_target",
    "push_command",
]
