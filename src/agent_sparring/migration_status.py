"""Migration-order classification: Stage A's read-only detection engine.

Everything this module knows comes from three sources, all read via ``git``
or the local filesystem -- never from a live database, a migration CLI, or a
Stage-B slot/stage declaration:

- the migration files committed on the current branch and on
  ``[migrations].main_ref`` (:func:`_list_versions`, via ``git ls-tree``);
- the most recently *recorded* history snapshot
  (:mod:`agent_sparring.migration_history`) -- what a target actually has
  applied, as of when someone last ran ``sparring record-migration-history``;
- the deferred-migration registry, if configured
  (:mod:`agent_sparring.migration_registry`).

This module never renames, edits, writes, or runs anything. Its output
(:class:`MigrationReport`) is a classification of each version plus a list of
*proposals* -- plain data describing what a person could do, never a command
this engine would execute. In particular, nothing here ever suggests
``migration repair``; a version once seen in a recorded snapshot's applied
list is permanently treated as immutable history.

Version ordering assumes Supabase's own convention (a 14-digit timestamp
that also sorts correctly as a decimal integer), which is the only adapter
this stage supports (see :mod:`agent_sparring.migration_adapters`). A future
adapter with a different version shape would need its own retiming strategy;
this module does not try to anticipate one.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

from agent_sparring.config import MigrationsConfig
from agent_sparring.migration_adapters import get_adapter
from agent_sparring.migration_history import HistorySnapshot, latest_snapshot
from agent_sparring.migration_registry import (
    DeferredRegistry,
    DeferredRegistryError,
    load_deferred_registry,
    sha256_hex,
)

REPORT_VERSION = 1

CLASS_APPLIED = "applied"
CLASS_UNAPPLIED = "unapplied"
CLASS_DEFERRED = "deferred"
CLASS_DEFERRED_TAMPERED = "deferred_tampered"
CLASS_MIGRATION_ORDER_STALE = "migration_order_stale"
CLASS_REMOTE_ONLY = "remote_only"

FLAG_APPLIED_MODIFIED = "applied_migration_modified"

PROPOSAL_RETIMESTAMP = "propose_retimestamp"
PROPOSAL_RECONCILIATION = "propose_reconciliation_stage"


class MigrationStatusError(ValueError):
    """Raised when migration status cannot be determined: a bad ``main_ref``,
    an unreadable migrations directory, a broken deferred registry, or
    similar. Never raised for a clean or a findings-bearing report -- those
    are ordinary results, not errors."""


@dataclass(frozen=True)
class VersionStatus:
    """One migration version's classification."""

    version: str
    path: str | None
    classification: str
    flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": self.path,
            "classification": self.classification,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class RetimestampProposal:
    """Data describing a possible retiming of a stale, unapplied migration.

    Never executed by this engine: it is exactly the information a person (or
    a later stage) would need to actually do it by hand -- the old and new
    version, and every other file that mentions the old one.
    """

    old_version: str
    new_version: str
    references: tuple[str, ...]
    kind: str = PROPOSAL_RETIMESTAMP

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "references": list(self.references),
        }


@dataclass(frozen=True)
class ReconciliationProposal:
    """Data describing an emergency remote-only migration that this
    repository has not yet recorded."""

    version: str
    message: str
    kind: str = PROPOSAL_RECONCILIATION

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "version": self.version, "message": self.message}


Proposal = Union[RetimestampProposal, ReconciliationProposal]


@dataclass(frozen=True)
class MigrationReport:
    """A full read-only migration-order report."""

    target: str
    adapter: str
    remote_head: str | None
    snapshot_observed_at: str | None
    snapshot_age_minutes: float | None
    max_observation_age_minutes: int
    stale_snapshot: bool
    production_history_unknown: bool
    remote_history_not_reconciled: bool
    versions: tuple[VersionStatus, ...]
    proposals: tuple[Proposal, ...]
    version: int = REPORT_VERSION

    def by_classification(self, classification: str) -> tuple[str, ...]:
        return tuple(v.version for v in self.versions if v.classification == classification)

    def has_findings(self) -> bool:
        """Whether this report says anything other than "everything is
        fine": used to choose ``check-migrations``'s exit code."""

        noteworthy = {CLASS_MIGRATION_ORDER_STALE, CLASS_DEFERRED_TAMPERED, CLASS_REMOTE_ONLY}
        if any(v.classification in noteworthy for v in self.versions):
            return True
        if any(FLAG_APPLIED_MODIFIED in v.flags for v in self.versions):
            return True
        return self.production_history_unknown or self.stale_snapshot

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target": self.target,
            "adapter": self.adapter,
            "remote_head": self.remote_head,
            "snapshot_observed_at": self.snapshot_observed_at,
            "snapshot_age_minutes": self.snapshot_age_minutes,
            "max_observation_age_minutes": self.max_observation_age_minutes,
            "stale_snapshot": self.stale_snapshot,
            "production_history_unknown": self.production_history_unknown,
            "remote_history_not_reconciled": self.remote_history_not_reconciled,
            "versions": [v.as_dict() for v in self.versions],
            "proposals": [p.as_dict() for p in self.proposals],
        }


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )


def _list_versions(repo_root: Path, ref: str, directory: str, adapter) -> dict[str, str]:
    """``{version: path}`` for every migration file ``adapter`` recognises in
    ``directory`` at ``ref``, read straight from git's tree -- never from the
    working-tree filesystem and never from a Stage-B slot/stage declaration,
    so an uncommitted or declared-but-absent file can never be counted."""

    result = _run_git(repo_root, "ls-tree", "-r", "--name-only", ref, "--", directory)
    if result.returncode != 0:
        raise MigrationStatusError(
            f"git ls-tree {ref} -- {directory} failed in {repo_root}: "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    versions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        version = adapter.version_of(line)
        if version:
            versions[version] = line
    return versions


def _show(repo_root: Path, ref: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrationStatusError(
            f"git show {ref}:{path} failed in {repo_root}: "
            f"{result.stderr.decode('utf-8', 'replace').strip() or 'unknown error'}"
        )
    return result.stdout


def _references(repo_root: Path, ref: str, version: str, exclude_path: str | None) -> tuple[str, ...]:
    """Every file at ``ref`` mentioning ``version``, excluding ``.sparring``
    and the migration file itself, via ``git grep`` -- read-only, and never
    the working tree (so this works the same whether or not it is checked
    out)."""

    pathspecs = [".", ":(exclude).sparring"]
    if exclude_path:
        pathspecs.append(f":(exclude){exclude_path}")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "grep", "-I", "-l", "-F", version, ref, "--", *pathspecs],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return ()
    if result.returncode not in (0, 1):
        raise MigrationStatusError(
            f"git grep -F {version} {ref} failed in {repo_root}: {result.stderr.strip()}"
        )
    paths = {line.split(":", 1)[1] for line in result.stdout.splitlines() if ":" in line}
    return tuple(sorted(paths))


def _load_registry(repo_root: Path, migrations: MigrationsConfig) -> DeferredRegistry | None:
    if not migrations.deferred_registry:
        return None
    path = Path(repo_root) / migrations.deferred_registry
    try:
        return load_deferred_registry(path)
    except DeferredRegistryError as exc:
        raise MigrationStatusError(f"could not read deferred registry: {exc}") from exc


def classify(
    repo_root: Path,
    migrations: MigrationsConfig,
    *,
    branch_ref: str = "HEAD",
    snapshot: HistorySnapshot | None = None,
    now: datetime | None = None,
) -> MigrationReport:
    """Classify every locally-known and every remotely-recorded migration
    version. Pure with respect to the outside world except for the ``git``
    reads described above and (when ``snapshot`` is not given explicitly)
    reading the latest recorded snapshot; nothing is written."""

    adapter = get_adapter(migrations.adapter)
    if snapshot is None:
        snapshot = latest_snapshot(repo_root)

    branch_versions = _list_versions(repo_root, branch_ref, migrations.directory, adapter)
    main_versions = _list_versions(repo_root, migrations.main_ref, migrations.directory, adapter)
    all_versions = set(branch_versions) | set(main_versions)

    registry = _load_registry(repo_root, migrations)
    deferred_by_version = registry.by_version() if registry else {}

    applied_set = set(snapshot.applied) if snapshot else set()
    remote_head = snapshot.head if snapshot else None
    used_versions = set(all_versions) | applied_set | set(deferred_by_version)

    statuses: list[VersionStatus] = []
    stale_versions: list[str] = []
    for version in sorted(all_versions, key=adapter.order_key):
        path = branch_versions.get(version) or main_versions.get(version)
        flags: list[str] = []
        if version in applied_set:
            classification = CLASS_APPLIED
            if version in branch_versions and version in main_versions:
                branch_content = _show(repo_root, branch_ref, branch_versions[version])
                main_content = _show(repo_root, migrations.main_ref, main_versions[version])
                if branch_content != main_content:
                    flags.append(FLAG_APPLIED_MODIFIED)
        elif version in deferred_by_version:
            entry = deferred_by_version[version]
            content_ref = branch_ref if version in branch_versions else migrations.main_ref
            content = _show(repo_root, content_ref, path) if path else b""
            classification = (
                CLASS_DEFERRED if path and sha256_hex(content) == entry.sha256 else CLASS_DEFERRED_TAMPERED
            )
        elif remote_head is not None and adapter.order_key(version) < adapter.order_key(remote_head):
            classification = CLASS_MIGRATION_ORDER_STALE
            stale_versions.append(version)
        else:
            classification = CLASS_UNAPPLIED
        statuses.append(
            VersionStatus(version=version, path=path, classification=classification, flags=tuple(flags))
        )

    remote_only_versions = sorted(applied_set - all_versions, key=adapter.order_key)
    for version in remote_only_versions:
        statuses.append(VersionStatus(version=version, path=None, classification=CLASS_REMOTE_ONLY))

    proposals: list[Proposal] = []
    if remote_head is not None and stale_versions:
        next_value = int(adapter.order_key(remote_head)) + 1
        for old_version in stale_versions:  # already ascending (sorted above)
            while str(next_value) in used_versions:
                next_value += 1
            new_version = str(next_value)
            used_versions.add(new_version)
            old_path = branch_versions.get(old_version) or main_versions.get(old_version)
            references = _references(repo_root, branch_ref, old_version, old_path)
            proposals.append(
                RetimestampProposal(old_version=old_version, new_version=new_version, references=references)
            )
            next_value += 1

    for version in remote_only_versions:
        proposals.append(
            ReconciliationProposal(
                version=version,
                message=f"record the exact remote-applied version {version} in the repository",
            )
        )

    observed_at = snapshot.observed_at if snapshot else None
    age_minutes: float | None = None
    stale_snapshot = False
    if snapshot is not None:
        moment = now or datetime.now(timezone.utc)
        observed_dt = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
        age_minutes = (moment - observed_dt).total_seconds() / 60.0
        stale_snapshot = age_minutes > migrations.max_observation_age_minutes

    return MigrationReport(
        target=migrations.target,
        adapter=migrations.adapter,
        remote_head=remote_head,
        snapshot_observed_at=observed_at,
        snapshot_age_minutes=age_minutes,
        max_observation_age_minutes=migrations.max_observation_age_minutes,
        stale_snapshot=stale_snapshot,
        production_history_unknown=snapshot is None,
        remote_history_not_reconciled=bool(remote_only_versions),
        versions=tuple(statuses),
        proposals=tuple(proposals),
    )


def render_report(report: MigrationReport) -> str:
    """The plain-words report ``sparring check-migrations`` prints without
    ``--json``."""

    lines: list[str] = []
    if report.production_history_unknown:
        lines.append(
            f"Production history for {report.target} is unknown: no migration-history "
            "snapshot has been recorded yet. Run 'sparring record-migration-history' first."
        )
    else:
        age = f"{report.snapshot_age_minutes:.0f}m ago" if report.snapshot_age_minutes is not None else "unknown"
        lines.append(f"{report.target} is at {report.remote_head} (recorded {age}).")
        if report.stale_snapshot:
            lines.append(
                f"Warning: this snapshot is older than the configured "
                f"{report.max_observation_age_minutes}-minute limit; record a fresh one "
                "before trusting this report."
            )

    stale = report.by_classification(CLASS_MIGRATION_ORDER_STALE)
    if stale:
        lines.append(
            f"{len(stale)} unapplied migration(s) are now older than {report.target} and can "
            f"no longer deploy in order: {', '.join(stale)}"
        )

    tampered = report.by_classification(CLASS_DEFERRED_TAMPERED)
    if tampered:
        lines.append(
            f"{len(tampered)} deferred migration(s) no longer match their registered hash: "
            f"{', '.join(tampered)}"
        )

    modified = [v.version for v in report.versions if FLAG_APPLIED_MODIFIED in v.flags]
    if modified:
        lines.append(
            f"{len(modified)} applied migration(s) differ from {report.target}'s recorded copy "
            f"on the main branch: {', '.join(modified)}"
        )

    remote_only = report.by_classification(CLASS_REMOTE_ONLY)
    if remote_only:
        lines.append(
            f"{report.target} has {len(remote_only)} migration(s) not reconciled in this "
            f"repository: {', '.join(remote_only)}"
        )

    if report.proposals:
        lines.append("Proposed remediation (data only; nothing is applied automatically):")
        for proposal in report.proposals:
            if isinstance(proposal, RetimestampProposal):
                refs = f" (referenced in: {', '.join(proposal.references)})" if proposal.references else ""
                lines.append(f"  - retimestamp {proposal.old_version} -> {proposal.new_version}{refs}")
            else:
                lines.append(f"  - {proposal.message}")

    if not report.has_findings():
        lines.append("No migration-order issues detected.")

    return "\n".join(lines)


__all__ = [
    "CLASS_APPLIED",
    "CLASS_DEFERRED",
    "CLASS_DEFERRED_TAMPERED",
    "CLASS_MIGRATION_ORDER_STALE",
    "CLASS_REMOTE_ONLY",
    "CLASS_UNAPPLIED",
    "FLAG_APPLIED_MODIFIED",
    "PROPOSAL_RECONCILIATION",
    "PROPOSAL_RETIMESTAMP",
    "MigrationReport",
    "MigrationStatusError",
    "ReconciliationProposal",
    "RetimestampProposal",
    "VersionStatus",
    "classify",
    "render_report",
]
