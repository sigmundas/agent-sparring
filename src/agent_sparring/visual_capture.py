"""Running a repository's screenshot capture and keeping its evidence honest.

:mod:`agent_sparring.visual_evidence` defines what evidence *is*; this module
produces it. When a project enables ``[visual_review]`` (see
:class:`~agent_sparring.config.VisualReviewConfig`), the loop calls
:func:`run_capture` once before every sparring turn -- so after every
implementation turn, including each correction after ``SEND_BACK`` -- and a
capture that fails stops the loop before the reviewer starts. Nothing here
starts a provider or decides a verdict; a failed capture can only ever be a
stopped run, never a review outcome.

Output contract
---------------

The engine runs ``command`` (an argument vector, no shell) from the
repository root with a fresh, empty evidence directory and these
environment variables:

``SPARRING_EVIDENCE_DIR``
    Absolute path of that directory. Every screenshot goes inside it.
``SPARRING_EVIDENCE_MANIFEST``
    Absolute path the command must write its version-1 manifest to
    (``manifest.json`` inside the evidence directory).
``SPARRING_REPO_ROOT``, ``SPARRING_CANDIDATE_SHA``, ``SPARRING_STAGE_ID``
    Context only: the candidate's HEAD and the stage being reviewed.

A capture succeeds only if the command exits 0 within ``timeout_seconds``,
leaves the candidate exactly as it found it, and writes a manifest that
:mod:`~agent_sparring.visual_evidence` accepts with at least one captured
screenshot; every screenshot must be a valid PNG inside the evidence
directory and every reference a valid PNG inside the repository that is
part of the candidate (not git-ignored). A ``failed`` entry is kept, so a
review can say what is missing.

Where evidence lives
--------------------

``.sparring/stages/<stage>/visual-evidence/`` is engine-owned and must be
git-ignored (``.sparring/stages/`` normally is; see
:mod:`agent_sparring.setup_check`), so screenshots are never candidate
content and cannot move the candidate's identity. Each capture gets its own
directory (its output goes to ``<capture>.log`` beside it);
:data:`RECORD_FILENAME` points at the one that passed validation
and records the engine's binding of it -- the full candidate identity
(:class:`~agent_sparring.stage.TurnCandidate`, which identifies an
uncommitted candidate by content, not just HEAD), every sibling pin and the
SHA-256 of every screenshot and reference.

Staleness
---------

A new capture first deletes the record and every earlier capture, so a
capture that fails leaves no evidence behind to be mistaken for current.
:func:`load_current_evidence` re-derives the candidate and re-hashes every
file, and refuses evidence captured for anything other than the repository
as it is now.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_sparring.config import VisualReviewConfig
from agent_sparring.git_context import GitContextError, is_ignored
from agent_sparring.next_turn import NextTurnError, capture_candidate
from agent_sparring.stage import Stage, StageError, TurnCandidate
from agent_sparring.visual_evidence import (
    EvidenceBinding,
    EvidenceManifest,
    VisualEvidenceError,
    bind_evidence,
    load_manifest,
    verify_binding,
)

EVIDENCE_DIRNAME = "visual-evidence"
RECORD_FILENAME = "current.json"
MANIFEST_FILENAME = "manifest.json"
RECORD_VERSION = 1

# How much of the command's output a failure message quotes; the whole log
# stays in the capture directory.
_LOG_TAIL_LINES = 20


class VisualCaptureError(RuntimeError):
    """A capture that did not produce current, valid evidence. Never partial."""


@dataclass(frozen=True)
class CaptureRecord:
    """The engine's account of one validated capture."""

    capture_id: str
    candidate: TurnCandidate
    binding: EvidenceBinding
    command: tuple[str, ...]
    started_at: str
    finished_at: str
    version: int = RECORD_VERSION

    @property
    def manifest(self) -> EvidenceManifest:
        return self.binding.manifest

    def directory(self, stage: Stage) -> Path:
        return evidence_root(stage) / self.capture_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "capture_id": self.capture_id,
            "candidate": self.candidate.to_dict(),
            "binding": self.binding.to_dict(),
            "command": list(self.command),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "CaptureRecord":
        try:
            if payload["version"] != RECORD_VERSION:
                raise VisualCaptureError(
                    f"unsupported capture record version {payload['version']!r}"
                )
            capture_id = payload["capture_id"]
            if not isinstance(capture_id, str) or not _is_capture_id(capture_id):
                raise VisualCaptureError(f"capture record names no valid capture ({capture_id!r})")
            command = payload["command"]
            if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
                raise VisualCaptureError("capture record has a malformed command")
            return cls(
                capture_id=capture_id,
                candidate=TurnCandidate.from_dict(payload["candidate"]),
                binding=EvidenceBinding.from_dict(payload["binding"]),
                command=tuple(command),
                started_at=str(payload["started_at"]),
                finished_at=str(payload["finished_at"]),
            )
        except VisualCaptureError:
            raise
        except (KeyError, TypeError, StageError, VisualEvidenceError) as exc:
            raise VisualCaptureError(f"malformed capture record: {exc}") from exc


def evidence_root(stage: Stage) -> Path:
    return stage.directory / EVIDENCE_DIRNAME


def _is_capture_id(value: str) -> bool:
    return (
        value.startswith("capture-")
        and len(value) <= 64
        and all(c.isalnum() or c in "-" for c in value)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _candidate(repo_root: Path, stage: Stage) -> TurnCandidate:
    try:
        return capture_candidate(repo_root, stage, stage.read_state())
    except (NextTurnError, StageError) as exc:
        raise VisualCaptureError(f"could not read the candidate identity: {exc}") from exc


def _siblings(candidate: TurnCandidate) -> dict[str, str]:
    return {pin.name: pin.head_sha for pin in candidate.repositories}


def _invalidate(root: Path) -> None:
    """Remove the record and every earlier capture: nothing older than the
    capture about to run may remain to be mistaken for current evidence."""

    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise VisualCaptureError(f"{root} must be a directory the engine owns, not a link or file")
    if root.exists():
        shutil.rmtree(root)


def _require_ignored(repo_root: Path, directory: Path) -> None:
    """Refuse an evidence directory git would count as candidate content."""

    try:
        relative = directory.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return  # outside the repository: never candidate content
    try:
        ignored = is_ignored(repo_root, Path(relative.as_posix() + "/"))
    except GitContextError as exc:
        raise VisualCaptureError(f"could not check whether {relative} is ignored: {exc}") from exc
    if not ignored:
        raise VisualCaptureError(
            f"the evidence directory {relative.as_posix()} is not git-ignored, so screenshots "
            "would become part of the candidate. Ignore the stages directory (for example "
            "'.sparring/stages/' in .gitignore; 'sparring check-setup' reports it) and rerun."
        )


def _log_tail(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    tail = lines[-_LOG_TAIL_LINES:]
    return ("\ncapture output (last lines):\n" + "\n".join(tail)) if tail else ""


def _kill_group(process: subprocess.Popen, *, posix: bool) -> None:
    if posix:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif process.poll() is None:
        process.kill()


def _run_command(
    config: VisualReviewConfig, *, cwd: Path, env: dict[str, str], log: Path
) -> None:
    posix = sys.platform != "win32"
    with log.open("wb") as output:
        try:
            process = subprocess.Popen(
                list(config.command),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                # Its own process group, so a timeout kills whatever the
                # command started (a browser, a dev server), not just it.
                start_new_session=posix,
                creationflags=0 if posix else subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            raise VisualCaptureError(
                f"could not start the capture command {config.command[0]!r}: {exc}"
            ) from exc
        try:
            returncode = process.wait(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process, posix=posix)
            process.wait()
            raise VisualCaptureError(
                f"the capture command did not finish within {config.timeout_seconds}s and was "
                "killed" + _log_tail(log)
            ) from None
        # Anything it left running could still write evidence after it is
        # hashed; the capture is over once the command is.
        _kill_group(process, posix=posix)
    if returncode != 0:
        raise VisualCaptureError(
            f"the capture command exited with status {returncode}" + _log_tail(log)
        )


def _refuse_links(root: Path, relative: str) -> None:
    """Every screenshot is a regular file under its own name. A link -- even
    one that stays inside the directory -- would let the file a review is
    told about differ from the file that was hashed."""

    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise VisualEvidenceError(f"{relative!r} is not a regular file: {path.name} is a link")


def _check_references(repo_root: Path, manifest: EvidenceManifest) -> None:
    """A reference must be candidate content: an ignored file could change
    without the candidate changing, so its hash would describe nothing."""

    for reference in sorted({s.reference for s in manifest.screenshots if s.reference}):
        try:
            ignored = is_ignored(repo_root, Path(reference))
        except GitContextError as exc:
            raise VisualCaptureError(f"could not check reference {reference!r}: {exc}") from exc
        if ignored:
            raise VisualCaptureError(
                f"reference {reference!r} is git-ignored, so it is not part of the candidate"
            )


def run_capture(repo_root: Path, stage: Stage, config: VisualReviewConfig) -> CaptureRecord:
    """Run the capture command for the current candidate and record its evidence.

    Raises :class:`VisualCaptureError` -- with no record left behind -- if
    the command cannot start, times out, exits non-zero, changes the
    candidate, or leaves evidence the contract refuses.
    """

    repo_root = Path(repo_root)
    root = evidence_root(stage)
    _invalidate(root)
    candidate = _candidate(repo_root, stage)
    capture_id = (
        "capture-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + candidate.head_sha[:12]
        + "-"
        + secrets.token_hex(3)
    )
    directory = root / capture_id
    directory.mkdir(parents=True)
    expected_directory = directory.resolve()
    _require_ignored(repo_root, directory)
    manifest_path = directory / MANIFEST_FILENAME
    env = dict(os.environ)
    env.update(
        {
            "SPARRING_EVIDENCE_DIR": str(expected_directory),
            "SPARRING_EVIDENCE_MANIFEST": str(expected_directory / MANIFEST_FILENAME),
            "SPARRING_REPO_ROOT": str(repo_root.resolve()),
            "SPARRING_CANDIDATE_SHA": candidate.head_sha,
            "SPARRING_STAGE_ID": stage.stage_id,
        }
    )
    started_at = _now()
    _run_command(config, cwd=repo_root, env=env, log=root / f"{capture_id}.log")
    finished_at = _now()

    after = _candidate(repo_root, stage)
    if after != candidate:
        raise VisualCaptureError(
            "the capture command changed the candidate: it was "
            f"{candidate.describe()} and is now {after.describe()}. Capture must not write "
            "candidate content; write only inside SPARRING_EVIDENCE_DIR."
        )
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or directory.resolve() != expected_directory
        or root.is_symlink()
    ):
        raise VisualCaptureError("the capture command replaced the evidence directory")
    try:
        if manifest_path.is_symlink():
            raise VisualEvidenceError(f"{manifest_path} is a symlink, not the manifest")
        manifest = load_manifest(manifest_path)
        if not any(shot.captured for shot in manifest.screenshots):
            raise VisualEvidenceError("the manifest records no captured screenshot")
        _check_references(repo_root, manifest)
        for shot in manifest.screenshots:
            if shot.path is not None:
                _refuse_links(directory, shot.path)
        binding = bind_evidence(
            manifest,
            evidence_root=directory,
            repo_root=repo_root,
            candidate_sha=candidate.head_sha,
            siblings=_siblings(candidate),
        )
    except VisualEvidenceError as exc:
        raise VisualCaptureError(f"the capture produced invalid evidence: {exc}") from exc

    record = CaptureRecord(
        capture_id=capture_id,
        candidate=candidate,
        binding=binding,
        command=config.command,
        started_at=started_at,
        finished_at=finished_at,
    )
    pending = root / (RECORD_FILENAME + ".tmp")
    pending.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, root / RECORD_FILENAME)
    return record


def load_current_evidence(repo_root: Path, stage: Stage) -> CaptureRecord:
    """The recorded capture, refused unless it is evidence for the candidate
    the repository holds now and every file is unchanged since capture."""

    repo_root = Path(repo_root)
    path = evidence_root(stage) / RECORD_FILENAME
    if path.is_symlink() or not path.is_file():
        raise VisualCaptureError(f"stage {stage.stage_id!r} has no current visual evidence")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualCaptureError(f"cannot read {path}: {exc}") from exc
    record = CaptureRecord.from_dict(payload)
    current = _candidate(repo_root, stage)
    if record.candidate != current:
        raise VisualCaptureError(
            f"visual evidence was captured for {record.candidate.describe()}, not the current "
            f"candidate {current.describe()}; it is stale and must be captured again"
        )
    try:
        for relative, _ in record.binding.screenshots:
            _refuse_links(record.directory(stage), relative)
        verify_binding(
            record.binding,
            evidence_root=record.directory(stage),
            repo_root=repo_root,
            candidate_sha=current.head_sha,
            siblings=_siblings(current),
        )
    except VisualEvidenceError as exc:
        raise VisualCaptureError(f"visual evidence is no longer valid: {exc}") from exc
    return record


__all__ = [
    "CaptureRecord",
    "EVIDENCE_DIRNAME",
    "MANIFEST_FILENAME",
    "RECORD_FILENAME",
    "VisualCaptureError",
    "evidence_root",
    "load_current_evidence",
    "run_capture",
]
