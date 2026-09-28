"""Sealed plan-intake approvals: what ties a person's approval to execution.

:mod:`agent_sparring.intake` prepares a reviewable proposal and, once a
person runs ``approve-plan`` for one run slice, writes that slice's
manifest and approval. This module is what makes the approval *bind* what
later executes, rather than being a note filed beside a manifest that
``run-plan`` would have run anyway.

Trust model
-----------

Human approval here is a workflow-integrity guarantee against ordinary,
accidental drift: a source plan edited after review, an interpretation or
intake record regenerated or hand-edited, a manifest re-emitted, a repository
that moved to another branch or commit, a slice started before the slice it
depends on finished. It is **not** a security boundary against a process
deliberately running engine commands or rewriting engine state under the
user's account -- a managed agent that did that is attacking the engine
itself, which is out of scope here exactly as it is everywhere else in
Agent Sparring. So the approval lives beside the proposal, in the
git-ignored ``.sparring/intake/<id>/runs/<run>/`` directory, and no
sandbox, key or external store is involved.

What that still guarantees:

- An intake manifest runs only through its approval. The manifest is an
  envelope (:data:`~agent_sparring.manifest.INTAKE_ENVELOPE_KEY`) that the
  plain manifest reader refuses; :func:`load_intake_manifest` reads it
  together with the ``approval.json`` next to it and refuses unless the
  manifest's exact bytes, its semantic digest, the intake record, the
  interpretation, the source snapshot and the source plan on disk are all
  still what the person approved. The stages it returns are parsed from
  the very bytes it verified.
- It runs only from the approved primary worktree on the approved branch,
  on start and on every continuation; a fresh run additionally needs the
  approved run key and every recorded repository at the commit it was
  approved at (:meth:`IntakeManifestSource.verify_run_context`).
- ``report.md`` is not rechecked once a slice is approved: it is the view
  the person approved from (approval compared it), not an input to
  execution, so editing it afterwards is inert. Making the report itself
  a protected, re-verifiable review artifact belongs to review-usability
  work (Stage C of the plan).
- The run is pinned to the approval: its recorded ``source`` kind is
  :data:`SOURCE_KIND` and its recorded digest covers the approval's bytes,
  so resume, acceptance and advancement -- which re-read the source and
  compare that digest -- refuse a changed approval, a changed manifest or
  a stripped envelope.
- The identities intake minted cannot be reused by a plain run
  (:func:`refuse_intake_identities`), so re-running the inner manifest by
  hand, or a manifest an earlier unsealed intake produced, does not become
  an unapproved execution of the same stages.

A plain, hand-authored manifest or Markdown plan is unaffected by all of
this: it never had an approval and never needs one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_sparring.branch_guard import is_protected_branch
from agent_sparring.git_context import (
    GitContextError,
    current_branch,
    repository_identity,
    resolve_commit,
)
from agent_sparring.manifest import (
    INTAKE_ENVELOPE_KEY,
    ExecutionManifest,
    ManifestError,
    manifest_digest,
    manifest_from_payload,
)
from agent_sparring.plan_model import PlannedStage, digest_planned_stages

INTAKE_DIRNAME = "intake"
RECORD_FILENAME = "intake.json"
MANIFEST_FILENAME = "manifest.json"
APPROVAL_FILENAME = "approval.json"
#: The engine's record that a slice approved on a protected branch was moved,
#: before any provider turn, to a feature branch at the same commit (see
#: :mod:`agent_sparring.intake_branch`). Written once, beside an approval that
#: is itself never rewritten.
BRANCH_MOVE_FILENAME = "branch-move.json"
BRANCH_MOVE_VERSION = 1
REGISTRY_DIRNAME = "registry"
RUNS_DIRNAME = "runs"

#: The envelope's own version (the value of its ``intake_manifest`` key).
ENVELOPE_VERSION = 1
#: ``approval.json`` version. ``1`` was the unsealed record written before
#: approvals bound execution; it is recognised only to be refused.
APPROVAL_VERSION = 2
#: The :attr:`~agent_sparring.plan_model.PlanSource.kind` of a sealed intake
#: manifest, recorded in the run's state so a resume cannot switch it.
SOURCE_KIND = "intake-manifest"

_ENVELOPE_KEYS = frozenset({INTAKE_ENVELOPE_KEY, "intake_id", "run_id", "run_key", "manifest"})


class IntakeApprovalError(ManifestError):
    """An intake manifest whose approval does not authorize running it."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_digest(text: str) -> str:
    """The digest intake records for a text file: SHA-256 of its UTF-8."""

    return sha256_bytes(text.encode("utf-8"))


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise IntakeApprovalError(f"cannot read {what} {path}: {exc}") from exc


def _read_text_digest(path: Path, what: str) -> str:
    try:
        return text_digest(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise IntakeApprovalError(f"cannot read {what} {path}: {exc}") from exc


def envelope_text(*, intake_id: str, run_id: str, run_key: str, manifest: Mapping[str, Any]) -> str:
    """The exact bytes of an approved slice's ``manifest.json``."""

    payload = {
        INTAKE_ENVELOPE_KEY: ENVELOPE_VERSION,
        "intake_id": intake_id,
        "run_id": run_id,
        "run_key": run_key,
        "manifest": dict(manifest),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def is_envelope(payload: Any) -> bool:
    return isinstance(payload, Mapping) and INTAKE_ENVELOPE_KEY in payload


# -- the approval record ----------------------------------------------------------


@dataclass(frozen=True)
class IntakeApproval:
    """A parsed ``approval.json``, with the exact bytes it was read from."""

    path: Path
    raw: bytes
    payload: Mapping[str, Any]
    #: A verified :data:`BRANCH_MOVE_FILENAME` record, when the slice was moved.
    move: Mapping[str, Any] | None = None

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.raw)

    @property
    def approved_branch(self) -> str:
        """The branch the person approved on, as ``approval.json`` records it."""

        return str(self.payload["expected_branch"])

    def field(self, key: str) -> Any:
        return self.payload[key]

    @property
    def run_key(self) -> str:
        return str(self.payload["run_key"])

    @property
    def expected_branch(self) -> str:
        """The branch the slice runs on: the approved one, or the feature
        branch a recorded move put it on (the same commit either way)."""

        return str(self.move["to_branch"]) if self.move else self.approved_branch

    @property
    def starting(self) -> Mapping[str, Any]:
        starting = self.payload["starting_snapshot"]
        return {**starting, "branch": self.expected_branch} if self.move else starting

    @property
    def at_approval(self) -> Mapping[str, Mapping[str, Any]]:
        """Every inspected repository as it was when the slice was approved
        (the primary one on the branch a recorded move put it on)."""

        seen = self.payload["repositories"]["at_approval"]
        if not self.move:
            return seen
        primary = str(self.payload["primary_repository"])
        return {
            name: ({**snapshot, "branch": self.expected_branch} if name == primary else snapshot)
            for name, snapshot in seen.items()
        }


_APPROVAL_STRINGS = (
    "decision",
    "intake_id",
    "intake_dir",
    "run_id",
    "run_key",
    "expected_branch",
    "primary_repository",
    "intake_record_sha256",
    "interpretation_digest",
    "report_digest",
    "manifest_sha256",
    "manifest_digest",
)


def parse_approval(raw: bytes, path: Path) -> IntakeApproval:
    """Validate ``approval.json``'s shape, or refuse. Refuses the unsealed
    records written before approvals bound execution by name."""

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeApprovalError(f"approval {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise IntakeApprovalError(f"approval {path} is not a JSON object")
    version = payload.get("version")
    if version != APPROVAL_VERSION:
        raise IntakeApprovalError(
            f"approval {path} has version {version!r}: it is an unsealed approval from before "
            "approvals bound execution, and it authorizes nothing. Run prepare-plan and "
            "approve-plan again"
        )
    for key in _APPROVAL_STRINGS:
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise IntakeApprovalError(f"approval {path} has no valid {key!r}")
    if payload["decision"] != "approved":
        raise IntakeApprovalError(f"approval {path} records decision {payload['decision']!r}, not 'approved'")
    source = payload.get("source")
    if not isinstance(source, dict) or not all(
        isinstance(source.get(k), str) and source.get(k) for k in ("path", "label", "digest")
    ):
        raise IntakeApprovalError(f"approval {path} has no valid 'source'")
    starting = payload.get("starting_snapshot")
    if not isinstance(starting, dict) or not all(
        isinstance(starting.get(k), str) and starting.get(k)
        for k in ("path", "git_common_dir", "branch", "head")
    ):
        raise IntakeApprovalError(f"approval {path} has no valid 'starting_snapshot'")
    repositories = payload.get("repositories")
    at_approval = repositories.get("at_approval") if isinstance(repositories, dict) else None
    if not isinstance(at_approval, dict) or not at_approval or not all(
        isinstance(seen, dict)
        and all(isinstance(seen.get(k), str) and seen.get(k) for k in ("path", "git_common_dir", "branch", "head"))
        for seen in at_approval.values()
    ):
        raise IntakeApprovalError(f"approval {path} has no valid 'repositories.at_approval'")
    return IntakeApproval(path=Path(path), raw=raw, payload=payload)


def read_approval(path: Path) -> IntakeApproval:
    approval = parse_approval(_read_bytes(path, "approval"), Path(path))
    move_path = Path(path).parent / BRANCH_MOVE_FILENAME
    if not move_path.exists():
        return approval
    return IntakeApproval(path=approval.path, raw=approval.raw, payload=approval.payload, move=parse_branch_move(approval, move_path))


def parse_branch_move(approval: IntakeApproval, path: Path) -> Mapping[str, Any]:
    """Verify a branch-move record against the approval it amends, or refuse.

    It must name this approval's exact bytes, move from its approved branch,
    and keep its starting commit: a move changes which branch the approved
    commit runs on, never which commit or which repository.
    """

    try:
        move = json.loads(_read_bytes(path, "branch move").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeApprovalError(f"branch move {path} is not valid JSON: {exc}") from exc
    starting = approval.payload["starting_snapshot"]
    expected = {
        "version": BRANCH_MOVE_VERSION,
        "run_id": approval.payload["run_id"],
        "run_key": approval.payload["run_key"],
        "approval_sha256": approval.sha256,
        "from_branch": approval.approved_branch,
        "head": starting["head"],
    }
    if not isinstance(move, dict) or any(move.get(key) != value for key, value in expected.items()):
        raise IntakeApprovalError(
            f"branch move {path} does not belong to the approval beside it ({approval.path}); "
            "the slice will not run"
        )
    to_branch = move.get("to_branch")
    if not isinstance(to_branch, str) or not to_branch or to_branch == approval.approved_branch:
        raise IntakeApprovalError(f"branch move {path} names no valid 'to_branch'")
    if is_protected_branch(to_branch):
        raise IntakeApprovalError(f"branch move {path} moves the slice to protected branch {to_branch!r}")
    return move


# -- the sealed plan source -------------------------------------------------------


@dataclass(frozen=True)
class IntakeManifestSource:
    """A :class:`~agent_sparring.plan_model.PlanSource` for an approved slice.

    Only :func:`load_intake_manifest` builds one, and only after verifying
    it, so holding one means its approval covered these exact stages.
    """

    path: Path
    manifest: ExecutionManifest
    approval: IntakeApproval
    kind: str = SOURCE_KIND

    @property
    def label(self) -> str:
        return self.manifest.plan_label

    def digest(self) -> str:
        """The manifest's own digest, bound to the approval's exact bytes:
        the run pins this, so a changed approval is a changed plan."""

        return digest_planned_stages(SOURCE_KIND, manifest_digest(self.manifest), self.approval.sha256)

    def stages(self) -> tuple[PlannedStage, ...]:
        return self.manifest.stages

    def reload(self) -> "IntakeManifestSource":
        return load_intake_manifest(self.path)

    def describe(self) -> str:
        return f"approved intake manifest {self.path} (run slice {self.approval.field('run_id')!r})"

    def bind_fresh_run(self, repo_root: Path, *, run_key: str | None, expected_branch: str) -> str:
        """The run key a fresh run of this slice must use, or refuse.

        Everything :meth:`verify_run_context` checks for a fresh run, plus
        the run key itself.
        """

        approval = self.approval
        if run_key is not None and run_key != approval.run_key:
            raise IntakeApprovalError(
                f"run slice {approval.field('run_id')!r} was approved as run {approval.run_key}, "
                f"not {run_key}; run it with --run-key {approval.run_key}"
            )
        self.verify_run_context(repo_root, expected_branch=expected_branch, fresh=True)
        return approval.run_key

    def verify_run_context(self, repo_root: Path, *, expected_branch: str, fresh: bool) -> None:
        """Refuse unless this slice is being run where, and as, it was approved.

        Always: the approved branch, from the approved primary worktree (same
        canonical path and git directory, so another worktree or clone of
        the repository is refused), which is on that branch.

        ``fresh`` -- a run that has not started -- additionally requires
        every repository the approval recorded, context and sibling ones
        included, to be the same repository on the same branch at the same
        commit it was approved at. A continuation does not compare commits:
        the run's own accepted work moves HEAD, and from then on the
        ordinary candidate and branch guards govern.
        """

        approval = self.approval
        run_id = approval.field("run_id")
        if expected_branch != approval.expected_branch:
            raise IntakeApprovalError(
                f"run slice {run_id!r} was approved for branch {approval.expected_branch!r}, not "
                f"{expected_branch!r}; a run argument does not replace the approved branch"
            )
        starting = approval.starting
        try:
            path, common = repository_identity(Path(repo_root))
            branch = current_branch(Path(repo_root))
        except GitContextError as exc:
            raise IntakeApprovalError(f"cannot inspect {repo_root}: {exc}") from exc
        if (path, common) != (starting["path"], starting["git_common_dir"]):
            raise IntakeApprovalError(
                f"run slice {run_id!r} was approved in {starting['path']} (git directory "
                f"{starting['git_common_dir']}), not {path} (git directory {common}); run it "
                "from the repository that was approved"
            )
        if branch != approval.expected_branch:
            raise IntakeApprovalError(
                f"{path} is on {branch!r}, but run slice {run_id!r} runs on "
                f"{approval.expected_branch!r}"
            )
        if not fresh:
            return
        moved = []
        for name, seen in sorted(approval.at_approval.items()):
            try:
                now_path, now_common = repository_identity(Path(seen["path"]))
                now_branch = current_branch(Path(seen["path"]))
                now_head = resolve_commit(Path(seen["path"]), "HEAD", label="HEAD")
            except GitContextError as exc:
                moved.append(f"{name}: cannot inspect {seen['path']}: {exc}")
                continue
            if (now_path, now_common) != (seen["path"], seen["git_common_dir"]):
                moved.append(f"{name}: {seen['path']} is now a different repository")
            elif (now_branch, now_head) != (seen["branch"], seen["head"]):
                moved.append(
                    f"{name}: approved at {seen['branch']}@{seen['head'][:12]}, now "
                    f"{now_branch}@{now_head[:12]}"
                )
        if moved:
            raise IntakeApprovalError(
                f"repositories moved since run slice {run_id!r} was approved:\n- "
                + "\n- ".join(moved)
                + "\nThe approval describes them as they were; run prepare-plan and approve-plan "
                "again for this state"
            )


def load_intake_manifest(path: Path, raw: bytes | None = None) -> IntakeManifestSource:
    """Verify an approved slice's manifest and return it, or refuse.

    ``raw`` is the manifest's bytes when the caller already read them, so a
    file that changes between reading and verifying cannot pass on one
    reading and execute another.
    """

    path = Path(path)
    if raw is None:
        raw = _read_bytes(path, "manifest")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeApprovalError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not is_envelope(envelope):
        raise IntakeApprovalError(f"manifest {path} is not an intake-approved manifest")
    unknown = sorted(set(envelope) - _ENVELOPE_KEYS)
    missing = sorted(_ENVELOPE_KEYS - set(envelope))
    if unknown or missing:
        raise IntakeApprovalError(
            f"intake manifest {path} is malformed (unknown keys {unknown}, missing {missing})"
        )
    if envelope[INTAKE_ENVELOPE_KEY] != ENVELOPE_VERSION:
        raise IntakeApprovalError(
            f"unsupported intake manifest version {envelope[INTAKE_ENVELOPE_KEY]!r}; this engine "
            f"reads version {ENVELOPE_VERSION}"
        )

    approval_path = path.parent / APPROVAL_FILENAME
    if not approval_path.is_file():
        raise IntakeApprovalError(
            f"intake manifest {path} has no {APPROVAL_FILENAME} beside it. An intake manifest "
            "runs only with the approval approve-plan wrote for it; approve the slice (again)"
        )
    approval = read_approval(approval_path)
    if sha256_bytes(raw) != approval.field("manifest_sha256"):
        raise IntakeApprovalError(
            f"{path} is not the manifest that was approved: its bytes changed after approval. "
            "An approval covers one exact manifest; run prepare-plan and approve-plan again"
        )
    for key in ("intake_id", "run_id", "run_key"):
        if envelope[key] != approval.field(key):
            raise IntakeApprovalError(
                f"intake manifest {path} names {key} {envelope[key]!r}, but its approval is for "
                f"{approval.field(key)!r}"
            )
    try:
        manifest = manifest_from_payload(envelope["manifest"])
    except ManifestError as exc:
        raise IntakeApprovalError(f"intake manifest {path}: {exc}") from exc
    if manifest_digest(manifest) != approval.field("manifest_digest"):
        raise IntakeApprovalError(f"{path} does not execute what its approval records")

    intake_dir = path.resolve().parent.parent.parent
    if str(intake_dir) != approval.field("intake_dir") or path.resolve().parent.parent.name != RUNS_DIRNAME:
        raise IntakeApprovalError(
            f"intake manifest {path} is not in the intake it was approved in "
            f"({approval.field('intake_dir')}); run it from where approve-plan wrote it"
        )
    verify_approved_inputs(approval, intake_dir)
    return IntakeManifestSource(path=path, manifest=manifest, approval=approval)


def verify_approved_inputs(approval: IntakeApproval, intake_dir: Path) -> None:
    """Refuse when anything the approval was decided on has changed: the
    intake record, the interpretation, the source snapshot or the source
    plan itself."""

    intake_dir = Path(intake_dir)
    if sha256_bytes(_read_bytes(intake_dir / RECORD_FILENAME, "intake record")) != approval.field(
        "intake_record_sha256"
    ):
        raise IntakeApprovalError(
            f"{intake_dir / RECORD_FILENAME} changed after run slice "
            f"{approval.field('run_id')!r} was approved; the approval no longer describes it"
        )
    if _read_text_digest(intake_dir / "interpretation.json", "interpretation") != approval.field(
        "interpretation_digest"
    ):
        raise IntakeApprovalError(
            f"{intake_dir / 'interpretation.json'} changed after run slice "
            f"{approval.field('run_id')!r} was approved; the approval no longer describes it"
        )
    source = approval.field("source")
    if _read_text_digest(intake_dir / "source.md", "source snapshot") != source["digest"]:
        raise IntakeApprovalError(f"{intake_dir / 'source.md'} changed after approval")
    if _read_text_digest(Path(source["path"]), "source plan") != source["digest"]:
        raise IntakeApprovalError(
            f"the source plan {source['path']} changed after run slice "
            f"{approval.field('run_id')!r} was approved. The approval covers the plan as it was "
            "reviewed; restore it, or run prepare-plan and approve-plan again"
        )


# -- writing ----------------------------------------------------------------------


def _temporary(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    return Path(name)


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step: never half-written."""

    temporary = _temporary(Path(path), text)
    os.replace(temporary, path)


def write_exclusive(path: Path, text: str) -> bool:
    """Create ``path`` with ``text`` atomically unless it already exists.

    Returns whether this call created it. Two concurrent writers cannot both
    succeed, and a crash never leaves a partial file under ``path``.
    """

    temporary = _temporary(Path(path), text)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


# -- identities intake minted -----------------------------------------------------


def _json_files(directory: Path, pattern: str) -> Iterable[tuple[Path, Mapping[str, Any]]]:
    for path in sorted(directory.glob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield path, payload


def minted_identities(sparring_dir: Path) -> Iterable[tuple[Path, set[str], set[str]]]:
    """``(where, run keys, stage ids)`` for every intake recorded in this
    project: the intakes prepared here, sealed or not, and the slices
    approved to run here (whose intake may live in another repository)."""

    root = Path(sparring_dir) / INTAKE_DIRNAME
    for path, record in _json_files(root, f"*/{RECORD_FILENAME}"):
        keys = record.get("run_keys")
        briefs = record.get("briefs")
        yield (
            path.parent,
            {str(v) for v in keys.values()} if isinstance(keys, dict) else set(),
            {str(k) for k in briefs} if isinstance(briefs, dict) else set(),
        )
    for path, entry in _json_files(root / REGISTRY_DIRNAME, "*.json"):
        stage_ids = entry.get("stage_ids")
        yield (
            Path(str(entry.get("intake_dir") or path)),
            {str(entry.get("run_key"))} if entry.get("run_key") else set(),
            {str(s) for s in stage_ids} if isinstance(stage_ids, list) else set(),
        )


def refuse_intake_identities(sparring_dir: Path, *, run_key: str | None, stage_ids: Iterable[str]) -> None:
    """Refuse a fresh plain run that reuses a run key or stage id intake
    minted: those stages run only from their approved manifest."""

    wanted = set(stage_ids)
    for where, keys, stages in minted_identities(sparring_dir):
        clash = sorted((wanted & stages) | ({run_key} & keys if run_key else set()))
        if clash:
            raise IntakeApprovalError(
                f"{', '.join(clash)} belong(s) to plan intake {where}. Its stages run only from "
                "their approved intake manifest (approve-plan prints the command); a plain "
                "manifest or plan with the same identities would be an unapproved execution of "
                "them. A manifest from an intake prepared before approvals were sealed must be "
                "prepared and approved again"
            )


__all__ = [
    "APPROVAL_FILENAME",
    "APPROVAL_VERSION",
    "BRANCH_MOVE_FILENAME",
    "BRANCH_MOVE_VERSION",
    "ENVELOPE_VERSION",
    "INTAKE_DIRNAME",
    "IntakeApproval",
    "IntakeApprovalError",
    "IntakeManifestSource",
    "MANIFEST_FILENAME",
    "RECORD_FILENAME",
    "REGISTRY_DIRNAME",
    "RUNS_DIRNAME",
    "SOURCE_KIND",
    "envelope_text",
    "is_envelope",
    "load_intake_manifest",
    "minted_identities",
    "parse_approval",
    "parse_branch_move",
    "read_approval",
    "refuse_intake_identities",
    "sha256_bytes",
    "text_digest",
    "verify_approved_inputs",
    "write_atomic",
    "write_exclusive",
]
