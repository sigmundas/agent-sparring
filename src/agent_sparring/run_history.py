"""A run's accepted history, per repository, and the rule that it only grows.

Before runs were local-first, every candidate reached its remote branch by
an ordinary push and remote movement was checked, and that is where
append-only history came from: a rewritten branch could not be pushed over
an accepted candidate without the engine seeing it. With nothing pushed,
the same property has to be kept explicitly, and locally:

    In every repository a run touches -- the primary and each declared
    sibling, separately -- the stage's base and every candidate the run
    has already accepted there must be ancestors of (or equal to) that
    repository's current candidate.

:mod:`agent_sparring.acceptance` and :mod:`agent_sparring.review` refuse a
freeze, an acceptance or an independent review that would break it, with
:data:`RUN_HISTORY_REWRITTEN`, naming the repository and the missing commit.

What a run has accepted is read from two engine-written places and their
union is what counts: the run state's ``accepted_candidates`` (written by
the plan runner as each stage is accepted) and the ``state.json`` of every
accepted stage the run owns (written by the acceptance gate itself), which
also covers a run recorded before ``accepted_candidates`` existed and a
stage accepted by hand. Nothing here reads a remote, a remote's
configuration or any agent-written file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_sparring.git_context import is_full_sha
from agent_sparring.stage import CandidateRepository, StageError, StageState, StageStatus

RUN_HISTORY_REWRITTEN = "run_history_rewritten"

#: How the primary repository is named in a refusal; siblings are named by
#: their recorded name and path.
PRIMARY_LABEL = "the primary repository"


class RunHistoryError(ValueError):
    """Engine-written run history that cannot be read as written."""


@dataclass(frozen=True)
class AcceptedCandidate:
    """One candidate a run accepted, in one repository.

    ``repository`` and ``path`` are ``None`` for the primary repository and
    the sibling's recorded name and path otherwise -- exactly the identity
    a stage declares it by (see :class:`~agent_sparring.stage.CandidateRepository`).
    """

    stage_id: str
    sha: str
    repository: str | None = None
    path: str | None = None

    @property
    def key(self) -> tuple[str | None, str | None]:
        return (self.repository, self.path)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"stage": self.stage_id, "sha": self.sha}
        if self.repository is not None:
            payload["repository"] = self.repository
            payload["path"] = self.path
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "AcceptedCandidate":
        if not isinstance(payload, Mapping):
            raise RunHistoryError(f"an accepted candidate must be an object, not {payload!r}")
        stage_id, sha = payload.get("stage"), payload.get("sha")
        if not isinstance(stage_id, str) or not stage_id or not is_full_sha(sha):
            raise RunHistoryError(
                f"an accepted candidate needs a stage and a full commit id, not {dict(payload)!r}"
            )
        repository, path = payload.get("repository"), payload.get("path")
        if repository is None and path is None:
            return cls(stage_id=stage_id, sha=str(sha))
        if not isinstance(repository, str) or not repository or not isinstance(path, str) or not path:
            raise RunHistoryError(
                f"a sibling's accepted candidate needs its recorded name and path, not {dict(payload)!r}"
            )
        return cls(stage_id=stage_id, sha=str(sha), repository=repository, path=path)


def records_from(payload: Any) -> tuple[AcceptedCandidate, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, (list, tuple)):
        raise RunHistoryError("accepted_candidates must be an array")
    return tuple(AcceptedCandidate.from_dict(entry) for entry in payload)


def accepted_from_stage(stage_id: str, state: StageState) -> tuple[AcceptedCandidate, ...]:
    """Every candidate an ACCEPTED stage accepted: the primary commit and each
    sibling's. Empty for a stage that is not accepted."""

    if state.status is not StageStatus.ACCEPTED or not is_full_sha(state.candidate_sha):
        return ()
    found = [AcceptedCandidate(stage_id=stage_id, sha=str(state.candidate_sha))]
    for repository in state.repositories:
        if is_full_sha(repository.candidate_sha):
            found.append(
                AcceptedCandidate(
                    stage_id=stage_id,
                    sha=str(repository.candidate_sha),
                    repository=repository.name,
                    path=repository.path,
                )
            )
    return tuple(found)


def merged(*groups: Iterable[AcceptedCandidate]) -> tuple[AcceptedCandidate, ...]:
    """The union of ``groups``, in first-seen order, without duplicates."""

    seen: dict[tuple[str, str, str | None, str | None], AcceptedCandidate] = {}
    for group in groups:
        for entry in group:
            seen.setdefault((entry.stage_id, entry.sha, entry.repository, entry.path), entry)
    return tuple(seen.values())


def run_accepted(sparring_dir: Path, run: str) -> tuple[AcceptedCandidate, ...]:
    """Everything run ``run`` has accepted, from its run state and from the
    accepted stages it owns.

    Raises :class:`RunHistoryError` when the run state exists but its
    record cannot be read: history that cannot be read cannot be shown to
    be preserved. A stage directory whose state cannot be read is skipped:
    the acceptance gate refuses that stage on its own, and its candidate,
    once accepted, is also in the run state."""

    recorded: tuple[AcceptedCandidate, ...] = ()
    path = Path(sparring_dir) / "plans" / f"{run}.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunHistoryError(f"cannot read the run state at {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise RunHistoryError(f"the run state at {path} must be a JSON object")
        recorded = records_from(payload.get("accepted_candidates"))
    from_stages: list[AcceptedCandidate] = []
    stages = Path(sparring_dir) / "stages"
    if stages.is_dir():
        for state_path in sorted(stages.glob("*/state.json")):
            try:
                state = StageState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, StageError):
                continue
            if state.run == run:
                from_stages.extend(accepted_from_stage(state_path.parent.name, state))
    return merged(recorded, from_stages)


def describe_repository(repository: CandidateRepository | None) -> str:
    if repository is None:
        return PRIMARY_LABEL
    return f"sibling repository {repository.name!r} ({repository.path})"


def required_ancestors(
    accepted: Iterable[AcceptedCandidate],
    *,
    stage_id: str,
    stage_base: str | None,
    repository: CandidateRepository | None,
) -> tuple[tuple[str, str], ...]:
    """The commits that must be ancestors of one repository's candidate, as
    ``(what, sha)``: that repository's stage base, and every candidate the
    run accepted there in a stage other than ``stage_id``."""

    key = (None, None) if repository is None else (repository.name, repository.path)
    required: list[tuple[str, str]] = []
    if is_full_sha(stage_base):
        required.append((f"the stage base of stage {stage_id!r}", str(stage_base)))
    for entry in accepted:
        if entry.key == key and entry.stage_id != stage_id:
            required.append((f"the candidate accepted by stage {entry.stage_id!r}", entry.sha))
    return tuple(required)


__all__ = [
    "PRIMARY_LABEL",
    "RUN_HISTORY_REWRITTEN",
    "AcceptedCandidate",
    "RunHistoryError",
    "accepted_from_stage",
    "describe_repository",
    "merged",
    "records_from",
    "required_ancestors",
    "run_accepted",
]
