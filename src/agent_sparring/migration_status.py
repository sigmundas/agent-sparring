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

Versions are ordered by the adapter's ``order_key`` (for Supabase, the
version string itself, as the Supabase CLI orders them -- see
:mod:`agent_sparring.migration_adapters`). Retimestamp proposals are 14-digit
UTC timestamps, the form ``supabase migration new`` writes, which is the
only adapter this stage supports. A future adapter with a different version
shape would need its own retiming strategy; this module does not try to
anticipate one.
"""

from __future__ import annotations

import posixpath
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

from agent_sparring.config import MigrationsConfig
from agent_sparring.migration_adapters import get_adapter
from agent_sparring.migration_history import (
    HistorySnapshot,
    MigrationHistoryError,
    check_not_future,
    latest_snapshot,
    parse_observed_at,
)
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
CLASS_DUPLICATE_VERSION = "duplicate_version"

FLAG_APPLIED_MODIFIED = "applied_migration_modified"

PROPOSAL_RETIMESTAMP = "propose_retimestamp"
PROPOSAL_RECONCILIATION = "propose_reconciliation_stage"

PROBLEM_DUPLICATE_VERSION = "duplicate_version"
PROBLEM_UNRECOGNISED_FILE = "unrecognised_migration_file"


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
class FileProblem:
    """A problem with the migration files themselves, found while listing
    them -- reported as its own finding rather than resolved by guessing
    (e.g. two files claiming one version: neither is silently preferred)."""

    kind: str
    paths: tuple[str, ...]
    refs: tuple[str, ...]
    message: str
    version: str | None = None
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "paths": list(self.paths),
            "refs": list(self.refs),
            "message": self.message,
            "blocking": self.blocking,
        }


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
    file_problems: tuple[FileProblem, ...] = ()
    version: int = REPORT_VERSION

    def by_classification(self, classification: str) -> tuple[str, ...]:
        return tuple(v.version for v in self.versions if v.classification == classification)

    def has_findings(self) -> bool:
        """Whether this report says anything other than "everything is
        fine": used to choose ``check-migrations``'s exit code."""

        noteworthy = {
            CLASS_MIGRATION_ORDER_STALE,
            CLASS_DEFERRED_TAMPERED,
            CLASS_REMOTE_ONLY,
            CLASS_DUPLICATE_VERSION,
        }
        if any(v.classification in noteworthy for v in self.versions):
            return True
        if any(problem.blocking for problem in self.file_problems):
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
            "file_problems": [p.as_dict() for p in self.file_problems],
        }


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )


@dataclass(frozen=True)
class _RefListing:
    """The migration files found in one ref's migrations directory."""

    #: ``{version: path}``. A version claimed by more than one file maps to
    #: the first of them here only so it is still counted; see ``duplicates``.
    versions: dict[str, str]
    #: ``{version: (path, ...)}`` for every version more than one file claims.
    duplicates: dict[str, tuple[str, ...]]
    #: Files in the directory the adapter does not recognise as migrations
    #: (a bad name, or a file in a subdirectory the migration tool never reads).
    unrecognised: tuple[str, ...]


def _list_versions(repo_root: Path, ref: str, directory: str, adapter) -> _RefListing:
    """Every migration file ``adapter`` recognises in ``directory`` at
    ``ref``, read straight from git's tree -- never from the working-tree
    filesystem and never from a Stage-B slot/stage declaration, so an
    uncommitted or declared-but-absent file can never be counted."""

    result = _run_git(repo_root, "ls-tree", "-r", "--name-only", ref, "--", directory)
    if result.returncode != 0:
        raise MigrationStatusError(
            f"git ls-tree {ref} -- {directory} failed in {repo_root}: "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    top = posixpath.normpath(directory)
    by_version: dict[str, list[str]] = {}
    unrecognised: list[str] = []
    for line in sorted(result.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Only direct children count: a migration tool reads the directory
        # itself, not its subdirectories (the Supabase CLI skips them).
        version = adapter.version_of(line) if posixpath.dirname(line) == top else None
        if version:
            by_version.setdefault(version, []).append(line)
        else:
            unrecognised.append(line)
    return _RefListing(
        versions={version: paths[0] for version, paths in by_version.items()},
        duplicates={
            version: tuple(paths) for version, paths in by_version.items() if len(paths) > 1
        },
        unrecognised=tuple(unrecognised),
    )


def _duplicate_problems(listings: dict[str, _RefListing]) -> list[FileProblem]:
    """One :class:`FileProblem` per version that more than one file claims,
    in any of ``listings`` (``{ref: listing}``), naming every such file."""

    paths_by_version: dict[str, set[str]] = {}
    refs_by_version: dict[str, list[str]] = {}
    for ref, listing in listings.items():
        for version, paths in listing.duplicates.items():
            paths_by_version.setdefault(version, set()).update(paths)
            refs_by_version.setdefault(version, []).append(ref)
    return [
        FileProblem(
            kind=PROBLEM_DUPLICATE_VERSION,
            version=version,
            paths=tuple(sorted(paths_by_version[version])),
            refs=tuple(refs_by_version[version]),
            message=(
                f"{len(paths_by_version[version])} migration files share version {version}; "
                "the migration tool cannot apply both, and this report will not pick one"
            ),
        )
        for version in sorted(paths_by_version)
    ]


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


def _unrecognised_problems(listings: dict[str, _RefListing], directory: str) -> list[FileProblem]:
    """One :class:`FileProblem` per file in the migrations directory (in any
    of ``listings``) that is not a recognised migration. A ``.sql`` file is
    blocking -- it looks like a migration the tool will never apply -- while
    anything else (a README, a ``.gitkeep``) is reported without blocking."""

    refs_by_path: dict[str, list[str]] = {}
    for ref, listing in listings.items():
        for path in listing.unrecognised:
            refs_by_path.setdefault(path, []).append(ref)
    top = posixpath.normpath(directory)
    problems: list[FileProblem] = []
    for path in sorted(refs_by_path):
        blocking = path.endswith(".sql")
        if posixpath.dirname(path) != top:
            why = "is in a subdirectory, which the migration tool does not read"
        elif blocking:
            why = "does not match the migration file naming, so the migration tool will skip it"
        else:
            why = "is not a migration file"
        problems.append(
            FileProblem(
                kind=PROBLEM_UNRECOGNISED_FILE,
                paths=(path,),
                refs=tuple(refs_by_path[path]),
                message=f"unrecognised migration file: {path} {why}",
                blocking=blocking,
            )
        )
    return problems


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
    moment = now or datetime.now(timezone.utc)
    if snapshot is None:
        try:
            snapshot = latest_snapshot(repo_root, now=moment)
        except MigrationHistoryError as exc:
            raise MigrationStatusError(str(exc)) from exc

    branch_listing = _list_versions(repo_root, branch_ref, migrations.directory, adapter)
    main_listing = _list_versions(repo_root, migrations.main_ref, migrations.directory, adapter)
    branch_versions = branch_listing.versions
    main_versions = main_listing.versions
    all_versions = set(branch_versions) | set(main_versions)
    listings = {branch_ref: branch_listing, migrations.main_ref: main_listing}
    duplicate_problems = _duplicate_problems(listings)
    duplicate_versions = {problem.version for problem in duplicate_problems}
    file_problems = duplicate_problems + _unrecognised_problems(listings, migrations.directory)

    registry = _load_registry(repo_root, migrations)
    deferred_by_version = registry.by_version() if registry else {}
    for entry in deferred_by_version.values():
        # The registry pins a file as well as a version; an entry whose own
        # file name encodes some other version cannot be trusted either way.
        file_version = adapter.version_of(Path(entry.file).name)
        if file_version != entry.version:
            raise MigrationStatusError(
                f"deferred registry entry for version {entry.version} names file "
                f"{entry.file!r}, which {'encodes version ' + file_version if file_version else 'is not a migration file name'}"
            )

    applied_set = set(snapshot.applied) if snapshot else set()
    remote_head = snapshot.head if snapshot else None
    used_versions = set(all_versions) | applied_set | set(deferred_by_version)

    statuses: list[VersionStatus] = []
    stale_versions: list[str] = []
    for version in sorted(all_versions, key=adapter.order_key):
        path = branch_versions.get(version) or main_versions.get(version)
        flags: list[str] = []
        if version in duplicate_versions:
            # Which file is "the" migration is ambiguous, so nothing that
            # depends on its content (tamper hash, modified-applied check,
            # a retimestamp proposal) is computed against either one.
            classification = CLASS_DUPLICATE_VERSION
            path = None
        elif version in applied_set:
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
            # Deferred only if the committed file is exactly the pinned one:
            # same name and same content hash.
            pinned = (
                path is not None
                and Path(path).name == Path(entry.file).name
                and sha256_hex(content) == entry.sha256
            )
            classification = CLASS_DEFERRED if pinned else CLASS_DEFERRED_TAMPERED
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
        try:
            observed_dt = parse_observed_at(snapshot.observed_at)
            check_not_future(observed_dt, now=moment, what="the latest history snapshot")
        except MigrationHistoryError as exc:
            raise MigrationStatusError(f"the latest history snapshot is unusable: {exc}") from exc
        # Never negative: anything further ahead than the clock-skew
        # tolerance was refused just above, and an observation within it is
        # treated as taken "now" rather than reported with a negative age.
        age_minutes = max(0.0, (moment - observed_dt).total_seconds() / 60.0)
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
        file_problems=tuple(file_problems),
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

    for problem in report.file_problems:
        prefix = "" if problem.blocking else "Note: "
        lines.append(f"{prefix}{problem.message}: {', '.join(problem.paths)}")

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
    "CLASS_DUPLICATE_VERSION",
    "CLASS_MIGRATION_ORDER_STALE",
    "CLASS_REMOTE_ONLY",
    "CLASS_UNAPPLIED",
    "FLAG_APPLIED_MODIFIED",
    "PROBLEM_DUPLICATE_VERSION",
    "PROBLEM_UNRECOGNISED_FILE",
    "PROPOSAL_RECONCILIATION",
    "PROPOSAL_RETIMESTAMP",
    "FileProblem",
    "MigrationReport",
    "MigrationStatusError",
    "ReconciliationProposal",
    "RetimestampProposal",
    "VersionStatus",
    "classify",
    "render_report",
]
