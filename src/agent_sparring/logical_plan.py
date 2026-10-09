"""One logical plan executed as consecutive per-repository managed runs.

A plan whose stages belong to more than one repository is still one plan:
one stage sequence, one obligation ledger, one status. The engine runs each
run of consecutive same-owner stages -- a *slice* -- as its own managed
execution in the owning repository, and keeps the plan itself in a record
in the home repository's common git directory::

    <home git-common-dir>/agent-sparring/plans/<logical-key>.json
    <home git-common-dir>/agent-sparring/plans/<logical-key>.snapshot<ext>
    <home git-common-dir>/agent-sparring/plans/<logical-key>.obligations.json

The record follows the :mod:`agent_sparring.managed_run` record rules: it is
created exclusively and written atomically, and every field other than the
append-only ``events`` (``created``, ``slice_created``, ``rescoped``) is
fixed at creation. It stores each stage's *resolved* owner and every
repository binding, so nothing later re-derives ownership from the frozen
source text. Status is derived from the slices' own managed records and run
states (:func:`derived_status`), never stored here.

Identity: the logical key is the first execution's run key; execution *k*
(1-based) has run key :func:`slice_run_key`. Stage ids are namespaced by the
logical key in every repository, and every execution's ``plan_digest`` is the
logical input digest. A single-repository plan creates no record.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from agent_sparring import managed_run
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord
from agent_sparring.plan_model import PlannedStage, PlanSource, is_repository_name

SCHEMA_VERSION = 1
RECORDS_SUBDIR = Path("agent-sparring") / "plans"
EVENTS = ("created", "slice_created", "rescoped")
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "logical_key",
        "plan_label",
        "input",
        "home",
        "repositories",
        "stages",
        "slices",
        "created_at",
        "created_by",
        "events",
    }
)
_INPUT_KEYS = frozenset({"kind", "path", "digest", "snapshot", "sha256"})


class LogicalPlanError(ManagedRunError):
    """A logical-plan refusal: a stable ``code`` and a human sentence.
    Raised before anything is changed unless the message says otherwise."""


# -- the record --------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryBinding:
    """A declared repository name bound to one checkout, as recorded."""

    name: str
    path: str
    git_common_dir: str
    #: The ``project`` name of its committed ``.sparring/project.toml``.
    project: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "git_common_dir": self.git_common_dir, "project": self.project}


@dataclass(frozen=True)
class LogicalStage:
    """One stage of the logical plan with its resolved owner."""

    position: int
    stage_id: str
    label: str
    title: str
    owner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "stage_id": self.stage_id,
            "label": self.label,
            "title": self.title,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class LogicalSlice:
    """Consecutive stages one repository owns: one managed execution.
    Field names follow ``intake.RunSlice``; ``id`` is the execution's run key."""

    id: str
    primary_repository: str
    stages: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "primary_repository": self.primary_repository, "stages": list(self.stages)}


@dataclass(frozen=True)
class LogicalPlanRecord:
    logical_key: str
    plan_label: str
    input_kind: str
    #: Where the input was read from when the record was created; may be gone.
    input_path: str
    #: The logical input digest (``PlanSource.digest()``) every execution records.
    input_digest: str
    #: The snapshot's file name beside the record, and the sha256 of its bytes.
    snapshot: str
    snapshot_sha256: str
    home: str
    repositories: tuple[RepositoryBinding, ...]
    stages: tuple[LogicalStage, ...]
    slices: tuple[LogicalSlice, ...]
    created_at: str
    created_by: str = "engine"
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def binding(self, name: str) -> RepositoryBinding:
        for binding in self.repositories:
            if binding.name == name:
                return binding
        raise LogicalPlanError("repository_unknown", f"logical plan {self.logical_key} binds no repository {name!r}")

    def owner_of(self, stage_id: str) -> str:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage.owner
        raise LogicalPlanError("stage_unknown", f"logical plan {self.logical_key} has no stage {stage_id!r}")

    def slice_index(self, run_key: str) -> int:
        """The 1-based index of the slice executed as ``run_key``."""

        for index, entry in enumerate(self.slices, start=1):
            if entry.id == run_key:
                return index
        raise LogicalPlanError("slice_unknown", f"{run_key} is not an execution of logical plan {self.logical_key}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "logical_key": self.logical_key,
            "plan_label": self.plan_label,
            "input": {
                "kind": self.input_kind,
                "path": self.input_path,
                "digest": self.input_digest,
                "snapshot": self.snapshot,
                "sha256": self.snapshot_sha256,
            },
            "home": self.home,
            "repositories": {binding.name: binding.to_dict() for binding in self.repositories},
            "stages": [stage.to_dict() for stage in self.stages],
            "slices": [entry.to_dict() for entry in self.slices],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "events": [dict(event) for event in self.events],
        }

    @classmethod
    def from_dict(cls, payload: Any, *, where: str = "logical-plan record") -> "LogicalPlanRecord":
        def bad(detail: str) -> LogicalPlanError:
            return LogicalPlanError("record_malformed", f"{where} is malformed: {detail}")

        if not isinstance(payload, Mapping):
            raise bad("not a JSON object")
        version = payload.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise LogicalPlanError(
                "record_schema_unknown", f"{where} has schema_version {version!r}; this engine reads only {SCHEMA_VERSION}"
            )
        keys = set(payload)
        if keys != _RECORD_KEYS:
            raise bad(f"missing {sorted(_RECORD_KEYS - keys)}, unexpected {sorted(keys - _RECORD_KEYS)}")

        def text(value: Any, name: str) -> str:
            if not isinstance(value, str) or not value:
                raise bad(f"{name} must be a non-empty string")
            return value

        key = text(payload["logical_key"], "logical_key")
        if not managed_run._RUN_KEY_RE.match(key):
            raise bad(f"logical_key {key!r} is not a valid run key")
        source = payload["input"]
        if not isinstance(source, Mapping) or set(source) != _INPUT_KEYS:
            raise bad(f"input must be {{{', '.join(sorted(_INPUT_KEYS))}}}")
        if source["kind"] not in managed_run.INPUT_KINDS:
            raise bad("input.kind must be markdown or manifest")
        if not Path(text(source["path"], "input.path")).is_absolute():
            raise bad("input.path must be absolute")
        snapshot = text(source["snapshot"], "input.snapshot")
        if Path(snapshot).name != snapshot or not snapshot.startswith(f"{key}.snapshot"):
            raise bad("input.snapshot must name the snapshot file beside the record")

        raw_repositories = payload["repositories"]
        if not isinstance(raw_repositories, Mapping) or not raw_repositories:
            raise bad("repositories must be a non-empty object")
        repositories: list[RepositoryBinding] = []
        for name, entry in raw_repositories.items():
            if not is_repository_name(name):
                raise bad(f"repository name {name!r} is not declarable")
            if not isinstance(entry, Mapping) or set(entry) != {"path", "git_common_dir", "project"}:
                raise bad(f"repository {name!r} must be {{path, git_common_dir, project}}")
            binding = RepositoryBinding(
                name=name,
                path=text(entry["path"], f"repositories.{name}.path"),
                git_common_dir=text(entry["git_common_dir"], f"repositories.{name}.git_common_dir"),
                project=text(entry["project"], f"repositories.{name}.project"),
            )
            if not Path(binding.path).is_absolute() or not Path(binding.git_common_dir).is_absolute():
                raise bad(f"repository {name!r} paths must be absolute")
            repositories.append(binding)
        common_dirs = [binding.git_common_dir for binding in repositories]
        if len(set(common_dirs)) != len(common_dirs):
            raise bad("two repository names are bound to one git common dir")
        names = {binding.name for binding in repositories}
        home = text(payload["home"], "home")
        if home not in names:
            raise bad(f"home {home!r} is not a bound repository")

        raw_stages = payload["stages"]
        if not isinstance(raw_stages, list) or not raw_stages:
            raise bad("stages must be a non-empty array")
        stages: list[LogicalStage] = []
        for expected, entry in enumerate(raw_stages, start=1):
            if not isinstance(entry, Mapping) or set(entry) != {"position", "stage_id", "label", "title", "owner"}:
                raise bad(f"stage {expected} must be {{position, stage_id, label, title, owner}}")
            if type(entry["position"]) is not int or entry["position"] != expected:
                raise bad(f"stage {expected} has position {entry['position']!r}")
            if not isinstance(entry["title"], str):
                raise bad(f"stage {expected} title must be a string")
            stage = LogicalStage(
                position=expected,
                stage_id=text(entry["stage_id"], f"stages[{expected}].stage_id"),
                label=text(entry["label"], f"stages[{expected}].label"),
                title=entry["title"],
                owner=text(entry["owner"], f"stages[{expected}].owner"),
            )
            if stage.owner not in names:
                raise bad(f"stage {stage.stage_id} is owned by {stage.owner!r}, which is not a bound repository")
            stages.append(stage)
        if len({stage.stage_id for stage in stages}) != len(stages):
            raise bad("stage ids are not unique")

        raw_slices = payload["slices"]
        if not isinstance(raw_slices, list):
            raise bad("slices must be an array")
        slices: list[LogicalSlice] = []
        for entry in raw_slices:
            if not isinstance(entry, Mapping) or set(entry) != {"id", "primary_repository", "stages"}:
                raise bad("each slice must be {id, primary_repository, stages}")
            if not isinstance(entry["stages"], list) or not all(isinstance(s, str) for s in entry["stages"]):
                raise bad("slice stages must be an array of stage ids")
            slices.append(
                LogicalSlice(
                    id=text(entry["id"], "slices[].id"),
                    primary_repository=text(entry["primary_repository"], "slices[].primary_repository"),
                    stages=tuple(entry["stages"]),
                )
            )
        if tuple(slices) != derive_slices(key, tuple(stages)):
            raise bad("slices are not the consecutive same-owner runs of its stages")

        if text(payload["created_by"], "created_by") != "engine":
            raise bad("created_by must be 'engine'")
        events = payload["events"]
        if not isinstance(events, list):
            raise bad("events must be an array")
        for event in events:
            if (
                not isinstance(event, Mapping)
                or set(event) != {"at", "event", "detail"}
                or event["event"] not in EVENTS
                or not isinstance(event["at"], str)
                or not isinstance(event["detail"], Mapping)
            ):
                raise bad(f"unreadable event {event!r}")
        return cls(
            logical_key=key,
            plan_label=text(payload["plan_label"], "plan_label"),
            input_kind=source["kind"],
            input_path=source["path"],
            input_digest=text(source["digest"], "input.digest"),
            snapshot=snapshot,
            snapshot_sha256=text(source["sha256"], "input.sha256"),
            home=home,
            repositories=tuple(repositories),
            stages=tuple(stages),
            slices=tuple(slices),
            created_at=text(payload["created_at"], "created_at"),
            created_by="engine",
            events=tuple(dict(event) for event in events),
        )


def slice_run_key(logical_key: str, index: int) -> str:
    """Execution ``index``'s run key (1-based): the logical key itself for the
    first, ``<logical>-s<k>`` after it -- deterministic, so retries converge."""

    if index < 1:
        raise ValueError(f"slice index {index} is not 1-based")
    return logical_key if index == 1 else f"{logical_key}-s{index}"


def derive_slices(logical_key: str, stages: tuple[LogicalStage, ...]) -> tuple[LogicalSlice, ...]:
    """Consecutive same-owner stages, in order, one slice each."""

    groups: list[tuple[str, list[str]]] = []
    for stage in stages:
        if groups and groups[-1][0] == stage.owner:
            groups[-1][1].append(stage.stage_id)
        else:
            groups.append((stage.owner, [stage.stage_id]))
    return tuple(
        LogicalSlice(id=slice_run_key(logical_key, index), primary_repository=owner, stages=tuple(ids))
        for index, (owner, ids) in enumerate(groups, start=1)
    )


def resolve_stages(stages: tuple[PlannedStage, ...], home: str) -> tuple[LogicalStage, ...]:
    """Each planned stage with its owner resolved: declared, else ``home``."""

    return tuple(
        LogicalStage(
            position=index,
            stage_id=stage.stage_id,
            label=stage.label,
            title=stage.title,
            owner=stage.owner or home,
        )
        for index, stage in enumerate(stages, start=1)
    )


# -- files -------------------------------------------------------------------


def records_dir(home_common_dir: Path) -> Path:
    return Path(home_common_dir) / RECORDS_SUBDIR


def record_path(home_common_dir: Path, logical_key: str) -> Path:
    managed_run._check_run_key(logical_key)
    return records_dir(home_common_dir) / f"{logical_key}.json"


def ledger_path(home_common_dir: Path, logical_key: str) -> Path:
    """The plan's obligation ledger (:mod:`agent_sparring.plan_obligations`)."""

    managed_run._check_run_key(logical_key)
    return records_dir(home_common_dir) / f"{logical_key}.obligations.json"


def snapshot_path(home_common_dir: Path, record: LogicalPlanRecord) -> Path:
    return records_dir(home_common_dir) / record.snapshot


def _snapshot_name(logical_key: str, input_path: Path) -> str:
    return f"{logical_key}.snapshot{Path(input_path).suffix}"


def snapshot_input(home_common_dir: Path, logical_key: str, input_path: Path) -> tuple[Path, str]:
    """Copy ``input_path``'s exact bytes beside the record, once.

    Returns ``(snapshot path, sha256 of its bytes)``. A snapshot already there
    is reused only when its bytes are exactly the input's (a retried
    creation); anything else refuses and leaves it in place."""

    managed_run._check_run_key(logical_key)
    try:
        data = Path(input_path).read_bytes()
    except OSError as exc:
        raise LogicalPlanError("input_unreadable", f"cannot snapshot {input_path}: {exc}") from exc
    path = records_dir(home_common_dir) / _snapshot_name(logical_key, Path(input_path))
    digest = hashlib.sha256(data).hexdigest()
    if path.exists():
        if path.read_bytes() != data:
            raise LogicalPlanError(
                "snapshot_exists", f"a different snapshot for logical plan {logical_key} already exists at {path}"
            )
        return path, digest
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".tmp-{os.getpid()}-{path.name}"
    try:
        temp.write_bytes(data)
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise LogicalPlanError(
                    "snapshot_exists", f"a different snapshot for logical plan {logical_key} already exists at {path}"
                ) from None
    finally:
        temp.unlink(missing_ok=True)
    return path, digest


def new_record(
    *,
    logical_key: str,
    source: PlanSource,
    input_path: Path,
    home: str,
    repositories: tuple[RepositoryBinding, ...],
    snapshot: Path,
    snapshot_sha256: str,
) -> LogicalPlanRecord:
    """The record for ``source`` (already namespaced by ``logical_key``):
    every stage's owner resolved against ``home`` and required to be bound."""

    stages = resolve_stages(source.stages(), home)
    names = {binding.name for binding in repositories}
    for stage in stages:
        if stage.owner not in names:
            raise LogicalPlanError(
                "repository_unknown",
                f"stage {stage.stage_id} belongs to {stage.owner!r}, which is not a given repository "
                "(--repository NAME=PATH)",
            )
    record = LogicalPlanRecord(
        logical_key=logical_key,
        plan_label=source.label,
        input_kind=source.kind,
        input_path=str(Path(input_path).resolve()),
        input_digest=source.digest(),
        snapshot=Path(snapshot).name,
        snapshot_sha256=snapshot_sha256,
        home=home,
        repositories=tuple(repositories),
        stages=stages,
        slices=derive_slices(logical_key, stages),
        created_at=managed_run._now(),
    )
    # Every invariant load() checks holds for what create() writes.
    return LogicalPlanRecord.from_dict(record.to_dict(), where=f"logical-plan record {logical_key}")


def create(home_common_dir: Path, record: LogicalPlanRecord) -> LogicalPlanRecord:
    """Write ``record`` atomically and exclusively, with its ``created`` event.
    A logical key is claimed once; its snapshot must already be beside it."""

    snapshot = snapshot_path(home_common_dir, record)
    try:
        data = snapshot.read_bytes()
    except OSError as exc:
        raise LogicalPlanError("snapshot_missing", f"logical plan {record.logical_key} has no snapshot at {snapshot}") from exc
    if hashlib.sha256(data).hexdigest() != record.snapshot_sha256:
        raise LogicalPlanError("snapshot_mismatch", f"snapshot {snapshot} does not match the record's sha256")
    created = replace(
        record,
        events=({"at": managed_run._now(), "event": "created", "detail": {"slices": len(record.slices)}},),
    )
    path = record_path(home_common_dir, record.logical_key)
    temp = managed_run._write_temp(path.parent, created.to_dict())
    try:
        os.link(temp, path)
    except FileExistsError as exc:
        raise LogicalPlanError(
            "logical_record_exists", f"a logical-plan record for {record.logical_key} already exists at {path}"
        ) from exc
    finally:
        temp.unlink(missing_ok=True)
    return created


def load(home_common_dir: Path, logical_key: str) -> LogicalPlanRecord:
    path = record_path(home_common_dir, logical_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LogicalPlanError("logical_record_missing", f"no logical-plan record for {logical_key} at {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogicalPlanError("record_unreadable", f"cannot read logical-plan record {path}: {exc}") from exc
    record = LogicalPlanRecord.from_dict(payload, where=f"logical-plan record {path}")
    if record.logical_key != logical_key:
        raise LogicalPlanError(
            "record_malformed", f"logical-plan record {path} names {record.logical_key!r}, not {logical_key!r}"
        )
    return record


def append_event(
    home_common_dir: Path, logical_key: str, event: str, detail: Mapping[str, Any] | None = None
) -> LogicalPlanRecord:
    if event not in EVENTS:
        raise LogicalPlanError("event_unknown", f"unknown logical-plan event {event!r}")
    record = load(home_common_dir, logical_key)
    updated = replace(
        record, events=record.events + ({"at": managed_run._now(), "event": event, "detail": dict(detail or {})},)
    )
    path = record_path(home_common_dir, logical_key)
    temp = managed_run._write_temp(path.parent, updated.to_dict())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return updated


# -- derived status ----------------------------------------------------------


@dataclass(frozen=True)
class SliceStatus:
    index: int
    id: str
    repository: str
    stages: tuple[str, ...]
    #: The execution record's lifecycle; ``None`` while no execution exists,
    #: ``"unreadable"`` when its record cannot be read.
    lifecycle: str | None
    #: The execution's run-state status, when one can be read.
    run_status: str | None
    #: Its record shows ``merged``: integrated into its target branch.
    integrated: bool
    #: Whether this slice is *proven* complete (see :func:`_completion_proof`).
    complete: bool = False
    #: Why it is, or is not.
    proof: str = "no execution exists"


@dataclass(frozen=True)
class LogicalStatus:
    logical_key: str
    slices: tuple[SliceStatus, ...]

    @property
    def complete(self) -> bool:
        return all(entry.complete for entry in self.slices)

    def incomplete(self, *, besides: str | None = None) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.slices if not entry.complete and entry.id != besides)


def _belongs(record: LogicalPlanRecord, index: int, execution: ManagedRunRecord) -> str | None:
    """Why ``execution`` is not slice ``index`` of ``record``, or ``None``.

    The first execution is the logical key's own run (a rescoped run has no
    ``slice`` key); every later one names this logical plan, its home and its
    index in its ``slice`` key."""

    if not execution.owns_git_state:
        return f"its record is {execution.lifecycle}, not an engine-created execution"
    if execution.run_key != slice_run_key(record.logical_key, index):
        return f"its run key is {execution.run_key}, not {slice_run_key(record.logical_key, index)}"
    found = execution.logical_slice
    if found is None:
        return None if index == 1 else "its record does not name this logical plan"
    if found["logical_key"] != record.logical_key or found["index"] != index:
        return f"its record names slice {found['index']} of {found['logical_key']}, not {index} of {record.logical_key}"
    home = Path(record.binding(record.home).git_common_dir).resolve()
    if Path(found["home_common_dir"]).resolve() != home:
        return f"its record names the home {found['home_common_dir']}, not {home}"
    return None


def _completion_proof(record: LogicalPlanRecord, entry: LogicalSlice, execution: ManagedRunRecord, state: Any) -> str | None:
    """Why ``state`` does not prove ``entry`` complete, or ``None``: it must
    be this execution's run of this plan's input over exactly this slice,
    recorded complete, with every stage in scope accepted by it."""

    from agent_sparring.plan import PlanRunStatus
    from agent_sparring.stage import Stage, StageError, StageStatus

    if (state.run or execution.run_key) != entry.id:
        return f"its run state is run {state.run}, not {entry.id}"
    if state.plan_digest != record.input_digest:
        return "its run state executed a different plan input"
    if state.scope != {"logical_key": record.logical_key, "stage_ids": list(entry.stages)}:
        return "its run state is not scoped to exactly this part of the plan"
    if state.status is not PlanRunStatus.COMPLETE:
        return f"its run is {state.status.value}"
    for stage_id in entry.stages:
        stage = Stage.resolve(execution.sparring_dir, stage_id)
        try:
            stage_state = stage.read_state() if stage.exists() else None
        except StageError:
            stage_state = None
        if (
            stage_state is None
            or stage_state.status is not StageStatus.ACCEPTED
            or not stage_state.candidate_sha
            or stage_state.run != entry.id
        ):
            return f"{stage_id} is not accepted by {entry.id}"
    return None


def _slice_status(record: LogicalPlanRecord, index: int, entry: LogicalSlice) -> SliceStatus:
    from agent_sparring.plan import PlanError, PlanRunState

    common = Path(record.binding(entry.primary_repository).git_common_dir)
    lifecycle: str | None = None
    run_status: str | None = None
    integrated = False
    complete = False
    proof = "no execution exists"
    try:
        execution = managed_run.read_record_in(common, entry.id)
    except ManagedRunError as exc:
        execution, lifecycle, proof = None, "unreadable", f"its record is unreadable: {exc}"
    if execution is not None:
        lifecycle = execution.lifecycle
        integrated = any(event["event"] == "merged" for event in execution.events)
        mismatch = _belongs(record, index, execution)
        state_path = _state_path(execution)
        state = None
        if state_path is not None and state_path.is_file():
            try:
                state = PlanRunState.load(state_path)
                run_status = state.status.value
            except PlanError:
                run_status = "unreadable"
        if mismatch is not None:
            proof = mismatch
        elif integrated:
            # Merged only after finish proved this scope accepted; its run
            # state may since have been archived with its worktree.
            complete, proof = True, "integrated into its target branch"
        elif state is None:
            proof = f"its run state is {run_status or 'missing'}"
        else:
            failure = _completion_proof(record, entry, execution, state)
            complete, proof = failure is None, failure or "every stage in scope is accepted"
    return SliceStatus(
        index=index,
        id=entry.id,
        repository=entry.primary_repository,
        stages=entry.stages,
        lifecycle=lifecycle,
        run_status=run_status,
        integrated=integrated,
        complete=complete,
        proof=proof,
    )


def _state_path(execution: ManagedRunRecord) -> Path | None:
    try:
        return execution.sparring_dir / "plans" / f"{execution.run_key}.json"
    except ManagedRunError:
        return None


def derived_status(record: LogicalPlanRecord) -> LogicalStatus:
    """Each slice's status, read from its own execution record and run state
    in its own repository -- never stored in the logical record."""

    return LogicalStatus(
        logical_key=record.logical_key,
        slices=tuple(_slice_status(record, index, entry) for index, entry in enumerate(record.slices, start=1)),
    )


# -- scoped executions -------------------------------------------------------


def snapshot_source(home_common_dir: Path, record: LogicalPlanRecord) -> PlanSource:
    """The logical snapshot read as a plan source, under the record's label
    and namespaced by the logical key."""

    from agent_sparring.manifest import ManifestError, load_manifest_source
    from agent_sparring.plan import PlanError, load_markdown_source

    path = snapshot_path(home_common_dir, record)
    try:
        if record.input_kind == "manifest":
            return load_manifest_source(path)
        return load_markdown_source(path, record.plan_label, namespace=record.logical_key)
    except (PlanError, ManifestError) as exc:
        raise LogicalPlanError("snapshot_unreadable", f"cannot read the logical snapshot {path}: {exc}") from exc


def _namespaced(source: PlanSource, logical_key: str) -> PlanSource:
    in_namespace = getattr(source, "in_namespace", None)
    return in_namespace(logical_key) if callable(in_namespace) else source


@dataclass(frozen=True)
class ScopedPlanSource:
    """A :class:`~agent_sparring.plan_model.PlanSource` over one slice of a
    logical plan: only the stages in scope, at their original positions,
    namespaced by the logical key. ``digest()`` is the whole input's -- the
    logical input digest -- and every stage's owner comes from the record,
    never from the source text."""

    inner: PlanSource
    record: LogicalPlanRecord
    home_common_dir: Path
    #: This execution's run key, which is its slice's id.
    run_key: str

    @property
    def kind(self) -> str:
        return self.inner.kind

    @property
    def label(self) -> str:
        return self.record.plan_label

    @property
    def path(self) -> Path:
        return self.inner.path

    @property
    def namespace(self) -> str:
        return self.record.logical_key

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return self.record.slices[self.record.slice_index(self.run_key) - 1].stages

    @property
    def scope(self) -> dict[str, Any]:
        return {"logical_key": self.record.logical_key, "stage_ids": list(self.stage_ids)}

    @property
    def completion_gates(self) -> tuple[Any, ...]:
        # A manifest's closeout gates are the plan's: owed by its last slice only.
        if self.record.slice_index(self.run_key) != len(self.record.slices):
            return ()
        return tuple(getattr(self.inner, "completion_gates", ()))

    def digest(self) -> str:
        return self.inner.digest()

    def stages(self) -> tuple[PlannedStage, ...]:
        owners = {stage.stage_id: stage.owner for stage in self.record.stages}
        wanted = set(self.stage_ids)
        return tuple(
            replace(stage, owner=owners[stage.stage_id]) for stage in self.inner.stages() if stage.stage_id in wanted
        )

    def reload(self) -> "ScopedPlanSource":
        return replace(self, inner=_namespaced(self.inner.reload(), self.record.logical_key))

    def describe(self) -> str:
        index = self.record.slice_index(self.run_key)
        repository = self.record.slices[index - 1].primary_repository
        return f"plan {self.label} (part {index} of {len(self.record.slices)}, in {repository})"


def scoped_source(
    inner: PlanSource, record: LogicalPlanRecord, home_common_dir: Path, run_key: str
) -> ScopedPlanSource:
    """``inner`` scoped to the slice executed as ``run_key``.

    Refuses unless ``inner`` is the logical input (same digest) and yields
    exactly the record's stages, in order, with the same ids, labels and
    titles -- the record, not the text, is what was resolved and recorded."""

    record.slice_index(run_key)
    inner = _namespaced(inner, record.logical_key)
    if inner.digest() != record.input_digest:
        raise LogicalPlanError(
            "logical_digest_mismatch",
            f"{inner.path} is not the input of logical plan {record.logical_key} (its digest differs)",
        )
    planned = inner.stages()
    found = [(stage.stage_id, stage.label, stage.title) for stage in planned]
    recorded = [(stage.stage_id, stage.label, stage.title) for stage in record.stages]
    if found != recorded:
        raise LogicalPlanError(
            "logical_stages_mismatch",
            f"{inner.path} does not yield the stages logical plan {record.logical_key} recorded",
        )
    return ScopedPlanSource(inner=inner, record=record, home_common_dir=Path(home_common_dir), run_key=run_key)


def home_common_dir_for(execution: ManagedRunRecord | None, repo_root: Path) -> Path:
    """Where an execution's logical record lives: its ``slice`` key's home,
    else (the first execution, or a rescoped run) its own repository's."""

    if execution is not None and execution.logical_slice is not None:
        return Path(execution.logical_slice["home_common_dir"])
    return managed_run.git_common_dir(Path(repo_root))


# -- repository bindings -----------------------------------------------------


def committed_project_name(top: Path, tip: str) -> str | None:
    """The ``project`` of ``.sparring/project.toml`` committed at ``tip``, or
    ``None`` when there is none (not committed, unreadable, no name)."""

    import subprocess
    import tomllib

    shown = subprocess.run(
        ["git", "-C", str(top), "show", f"{tip}:{managed_run.DEFAULT_PROJECT_DIR}/project.toml"],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        return None
    try:
        value = tomllib.loads(shown.stdout.decode("utf-8")).get("project")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def bind_repository(name: str, path: Path, *, target_branch: str | None = None) -> tuple[RepositoryBinding, str, str]:
    """``(binding, target branch, its tip)`` for ``name`` at ``path``: a git
    work tree whose ``.sparring/project.toml`` committed at the target tip
    (``target_branch``, else the branch checked out there) says
    ``project = "<name>"``. Refuses ``repository_mismatch`` otherwise."""

    given = Path(path).expanduser()
    if not given.is_dir():
        raise LogicalPlanError("repository_mismatch", f"repository {name!r}: {given} is not a directory")
    try:
        top = managed_run.worktree_top(given)
        common = managed_run.git_common_dir(top)
        target, tip = managed_run.resolve_target(top, target_branch)
    except ManagedRunError as exc:
        raise LogicalPlanError(
            "repository_mismatch", f"repository {name!r}: {given} is not a usable git work tree ({exc})"
        ) from exc
    project = committed_project_name(top, tip)
    if project != name:
        found = f"says project = {project!r}" if project else "is not committed"
        raise LogicalPlanError(
            "repository_mismatch",
            f"repository {name!r}: {top}'s .sparring/project.toml at {target} ({tip[:12]}) {found}, "
            f"not project = {name!r}",
        )
    return RepositoryBinding(name=name, path=str(top), git_common_dir=str(common), project=project), target, tip


def resolve_bindings(
    home: RepositoryBinding, given: Mapping[str, Path], owners: set[str] | frozenset[str]
) -> tuple[RepositoryBinding, ...]:
    """The home binding plus one per foreign ``owners`` name, from ``given``
    (``--repository NAME=PATH``). Every foreign owner must be given and
    every given name must own a stage (``repository_unknown``); a name given
    for the home must be the home's repository (``repository_mismatch``); two
    names may not share a git common dir (``repository_ambiguous``)."""

    foreign = sorted(set(owners) - {home.name})
    unused = sorted(set(given) - set(owners) - {home.name})
    if unused:
        raise LogicalPlanError(
            "repository_unknown",
            f"--repository names {unused}, which no stage of this plan belongs to; its repositories are "
            f"{sorted(set(owners) | {home.name})}",
        )
    missing = [name for name in foreign if name not in given]
    if missing:
        raise LogicalPlanError(
            "repository_unknown",
            f"stages of this plan belong to {missing}; say where with --repository NAME=PATH",
        )
    bindings = [home]
    if home.name in given:
        try:
            common = managed_run.git_common_dir(managed_run.worktree_top(Path(given[home.name])))
        except ManagedRunError as exc:
            raise LogicalPlanError("repository_mismatch", f"repository {home.name!r}: {exc}") from exc
        if str(common) != home.git_common_dir:
            raise LogicalPlanError(
                "repository_mismatch",
                f"--repository {home.name}={given[home.name]} is not this project's repository ({home.path})",
            )
    for name in foreign:
        bindings.append(bind_repository(name, Path(given[name]))[0])
    seen: dict[str, str] = {}
    for binding in bindings:
        other = seen.setdefault(binding.git_common_dir, binding.name)
        if other != binding.name:
            raise LogicalPlanError(
                "repository_ambiguous",
                f"repositories {other!r} and {binding.name!r} are the same git repository "
                f"({binding.git_common_dir})",
            )
    return tuple(bindings)


def check_binding_now(binding: RepositoryBinding, target_branch: str | None) -> tuple[str, str]:
    """``(target branch, its tip)`` of a recorded binding, re-checked now:
    still the same git repository, its committed project still the name."""

    current, target, tip = bind_repository(binding.name, Path(binding.path), target_branch=target_branch)
    if current.git_common_dir != binding.git_common_dir:
        raise LogicalPlanError(
            "repository_mismatch",
            f"repository {binding.name!r} at {binding.path} is now {current.git_common_dir}, not the recorded "
            f"{binding.git_common_dir}",
        )
    if current.project != binding.project:
        raise LogicalPlanError(
            "repository_mismatch", f"repository {binding.name!r} is now project {current.project!r}, not {binding.project!r}"
        )
    return target, tip


# -- continuation ------------------------------------------------------------


def require_home_first(stages: tuple[LogicalStage, ...], home: str) -> None:
    """A plan across repositories starts in its home: the first part runs
    where it is started. Refuses ``first_stage_not_home`` otherwise."""

    if stages and stages[0].owner != home:
        raise LogicalPlanError(
            "first_stage_not_home",
            f"Stage {stages[0].label} belongs to {stages[0].owner!r}; a plan across repositories starts in the "
            f"repository its first stage belongs to, so start it from {stages[0].owner!r}, not {home!r}",
        )


def pointer_path(common_dir: Path, logical_key: str) -> Path:
    """In another bound repository: where the logical plan's home is named."""

    managed_run._check_run_key(logical_key)
    return records_dir(common_dir) / f"{logical_key}.home.json"


def write_pointers(home_common_dir: Path, record: LogicalPlanRecord) -> None:
    """Name the home in every other bound repository's common dir, once and
    exclusively, so each resolves the plan before any part runs there. An
    existing pointer is kept when it names this home; any other refuses."""

    payload = {"schema_version": SCHEMA_VERSION, "logical_key": record.logical_key,
               "home_common_dir": str(Path(home_common_dir))}
    home = Path(home_common_dir).resolve()
    for binding in record.repositories:
        common = Path(binding.git_common_dir)
        if common.resolve() == home:
            continue
        path = pointer_path(common, record.logical_key)
        temp = managed_run._write_temp(path.parent, payload)
        try:
            os.link(temp, path)
        except FileExistsError:
            if _pointer_home(path, record.logical_key) != home:
                raise LogicalPlanError(
                    "pointer_conflict", f"{path} names another home for logical plan {record.logical_key}"
                ) from None
        finally:
            temp.unlink(missing_ok=True)


def _pointer_home(path: Path, logical_key: str) -> Path | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"schema_version", "logical_key", "home_common_dir"}
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["logical_key"] != logical_key
        or not isinstance(payload["home_common_dir"], str)
        or not Path(payload["home_common_dir"]).is_absolute()
    ):
        return None
    return Path(payload["home_common_dir"]).resolve()


def find(repo_root: Path, logical_key: str) -> tuple[Path, LogicalPlanRecord] | None:
    """``(home common dir, record)`` of logical plan ``logical_key`` as seen
    from any worktree of an involved repository: its record in this
    repository's common dir (the home), else the home named by this
    repository's pointer or by an execution of it recorded here -- trusted
    only when that record binds this repository. ``None`` when none exists."""

    managed_run._check_run_key(logical_key)
    common = managed_run.git_common_dir(Path(repo_root))
    if record_path(common, logical_key).is_file():
        return common, load(common, logical_key)
    homes: list[Path] = []
    pointer = pointer_path(common, logical_key)
    if pointer.is_file():
        home = _pointer_home(pointer, logical_key)
        if home is None:
            raise LogicalPlanError("pointer_unreadable", f"{pointer} does not name a home for {logical_key}")
        homes.append(home)
    try:
        executions = managed_run.list_records(Path(repo_root))
    except ManagedRunError:
        executions = ()
    for execution in executions:
        found = execution.logical_slice
        if found is not None and found["logical_key"] == logical_key:
            homes.append(Path(found["home_common_dir"]))
    for home in homes:
        record = load(home, logical_key)
        if not any(Path(binding.git_common_dir).resolve() == common for binding in record.repositories):
            raise LogicalPlanError(
                "repository_unknown", f"logical plan {logical_key} at {home} does not bind this repository ({common})"
            )
        return home, record
    return None


def next_slice(status: LogicalStatus) -> SliceStatus | None:
    """The first slice, in order, not proven complete; ``None`` when done."""

    return next((entry for entry in status.slices if not entry.complete), None)


def previous_integration(record: LogicalPlanRecord, status: LogicalStatus, index: int) -> str | None:
    """Why slice ``index`` may not be prepared yet, or ``None``: the slice
    before it must be integrated -- its record shows ``merged`` and its
    target branch contains the merged candidate."""

    if index == 1:
        return None
    previous = status.slices[index - 2]
    if not previous.integrated:
        return (
            f"part {previous.index} of the plan ({previous.repository}) is not integrated into its target "
            f"branch yet: finish it first (sparring finish-run --run-key {previous.id})"
        )
    binding = record.binding(previous.repository)
    execution = managed_run.read_record_in(Path(binding.git_common_dir), previous.id)
    merged = [event for event in execution.events if event["event"] == "merged"] if execution else []
    candidate = merged[-1]["detail"].get("candidate") if merged else None
    if not isinstance(candidate, str) or execution is None:
        return f"part {previous.index}'s merge names no candidate"
    contained = managed_run._git(
        Path(binding.path), "merge-base", "--is-ancestor", candidate, f"refs/heads/{execution.target_branch}"
    )
    if contained.returncode != 0:
        return f"{execution.target_branch} in {binding.name} does not contain part {previous.index}'s candidate {candidate}"
    return None


def plan_slice_execution(
    home_common_dir: Path, record: LogicalPlanRecord, index: int, *, target_branch: str | None, base_sha: str | None = None
) -> managed_run.ManagedRunPlan:
    """Preflight execution ``index`` in its own repository; writes nothing.
    Its input is the logical snapshot, its run key :func:`slice_run_key`, its
    record names this logical plan (``slice``)."""

    entry = record.slices[index - 1]
    binding = record.binding(entry.primary_repository)
    target, _ = check_binding_now(binding, target_branch)
    snapshot = snapshot_path(home_common_dir, record)

    def source_label(read_path: Path, mapped: Path) -> str:
        source = snapshot_source(home_common_dir, record)
        if source.digest() != record.input_digest:
            raise LogicalPlanError("logical_digest_mismatch", f"the logical snapshot {snapshot} is not this plan's input")
        return record.plan_label

    prepared = managed_run.plan_managed_run(
        Path(binding.path),
        Path(binding.path) / managed_run.DEFAULT_PROJECT_DIR,
        source_label=source_label,
        input_kind=record.input_kind,
        input_path=snapshot,
        target_branch=target,
        run_key=entry.id,
        new_run_key=lambda label: entry.id,
        base_sha=base_sha,
    )
    logical_slice = {"logical_key": record.logical_key, "home_common_dir": str(Path(home_common_dir)), "index": index}
    return replace(prepared, record=replace(prepared.record, logical_slice=logical_slice))


def record_slice_created(home_common_dir: Path, logical_key: str, index: int, run_key: str) -> LogicalPlanRecord:
    """Append ``slice_created`` for ``index`` once: a retried or interrupted
    creation reconciles to one event."""

    record = load(home_common_dir, logical_key)
    if any(event["event"] == "slice_created" and event["detail"].get("index") == index for event in record.events):
        return record
    return append_event(home_common_dir, logical_key, "slice_created", {"index": index, "run_key": run_key})


__all__ = [
    "EVENTS",
    "LogicalPlanError",
    "LogicalPlanRecord",
    "LogicalSlice",
    "LogicalStage",
    "LogicalStatus",
    "RepositoryBinding",
    "ScopedPlanSource",
    "SliceStatus",
    "append_event",
    "bind_repository",
    "check_binding_now",
    "committed_project_name",
    "create",
    "derive_slices",
    "derived_status",
    "find",
    "home_common_dir_for",
    "ledger_path",
    "load",
    "new_record",
    "next_slice",
    "plan_slice_execution",
    "pointer_path",
    "previous_integration",
    "record_path",
    "require_home_first",
    "record_slice_created",
    "resolve_bindings",
    "resolve_stages",
    "scoped_source",
    "slice_run_key",
    "snapshot_input",
    "snapshot_path",
    "snapshot_source",
    "write_pointers",
]
