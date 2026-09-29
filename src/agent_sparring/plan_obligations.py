"""Deferred human verification owed by a whole plan, across its run slices.

A reviewer that defers a check says it is owed ``before_plan_completion``
(:mod:`agent_sparring.deferred_gate`). For a plan run from one Markdown
document or one manifest, the plan and the managed run are the same thing,
and the run's own ledger (``PlanRunState.deferred_human_checks``) is where
the obligation belongs until the run's last stage.

A plan executed through intake is not one run. Intake splits it into *run
slices* -- often in different repositories -- and each slice is its own
managed run. "Plan completion" is then the completion of the plan the
reviewer was reviewing, not of the slice it happened to be reviewing in. So
when a slice reaches its last stage while other slices of its intake are not
yet complete, its unresolved obligations are *carried* to this ledger, the
slice completes, and later slices continue. When the last incomplete slice
reaches its last stage, it *claims* every carried obligation into its own
run ledger and stops for them there, exactly as a single-run plan stops at
its end. That run cannot complete while any is unresolved -- the same rule,
at the right boundary -- and nothing about the check is decided from its
wording, category or subject.

Where it lives::

    <intake home>/.sparring/intake/obligations/<plan key>.json

in the repository the intake was prepared in (the ``.sparring`` directory
that holds the intake), keyed by the plan document rather than by one
intake, so an intake prepared again for the same plan still finds what an
earlier one carried. Nothing that lists intakes reads this directory: it
has no ``intake.json``.

Identity is the gate instance throughout: carrying, claiming and answering
never mint a new one, so an answer still belongs to the asking the reviewer
raised. Every entry keeps the ``.sparring`` directory of the stage that
raised it, which is where an answer is written back (``notes.md``) even when
the run answering it lives in another repository.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from agent_sparring.deferred_gate import DeferredGateError, DeferredObligation

LEDGER_DIRNAME = "obligations"
LEDGER_VERSION = 1


class PlanObligationError(ValueError):
    """The plan-level obligation ledger is unreadable or could not be written."""


@dataclass(frozen=True)
class SliceContext:
    """Where one intake slice's run sits within its plan."""

    intake_dir: Path
    run_id: str
    plan_label: str
    #: Every run slice of the intake, in the intake's own order.
    run_ids: tuple[str, ...]

    @property
    def ledger_path(self) -> Path:
        from agent_sparring.plan import plan_key

        # <home>/.sparring/intake/<intake id> -> <home>/.sparring/intake/obligations/
        return self.intake_dir.parent / LEDGER_DIRNAME / f"{plan_key(self.plan_label)}.json"


@dataclass(frozen=True)
class CarriedObligation:
    """One obligation as the plan ledger holds it."""

    obligation: DeferredObligation
    #: The run whose slice raised it, and that run's ``.sparring`` directory,
    #: where the raising stage (and its ``notes.md``) lives.
    origin_run: str
    origin_sparring_dir: str
    origin_slice: str
    carried_at: str
    #: The run that answered it, once resolved; ``None`` while owed.
    resolved_by: str | None = None

    @property
    def instance_id(self) -> str:
        return self.obligation.instance_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation": self.obligation.to_dict(),
            "origin_run": self.origin_run,
            "origin_sparring_dir": self.origin_sparring_dir,
            "origin_slice": self.origin_slice,
            "carried_at": self.carried_at,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, payload: Any, where: Path) -> "CarriedObligation":
        if not isinstance(payload, dict):
            raise PlanObligationError(f"{where}: an entry must be an object")
        try:
            obligation = DeferredObligation.from_dict(payload.get("obligation"))
        except DeferredGateError as exc:
            raise PlanObligationError(f"{where}: {exc}") from exc
        fields = {}
        for name in ("origin_run", "origin_sparring_dir", "origin_slice", "carried_at"):
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise PlanObligationError(f"{where}: entry {obligation.instance_id} has no {name}")
            fields[name] = value
        resolved_by = payload.get("resolved_by")
        return cls(
            obligation=obligation,
            resolved_by=resolved_by if isinstance(resolved_by, str) and resolved_by else None,
            **fields,
        )


def slice_context(source: Any) -> SliceContext | None:
    """The slice context of an approved intake manifest source, else ``None``.

    A Markdown plan or a hand-authored manifest is a whole plan in one run and
    has none: its own run ledger is the plan's.
    """

    from agent_sparring.intake_approval import IntakeManifestSource

    if not isinstance(source, IntakeManifestSource):
        return None
    # <intake dir>/runs/<slice>/manifest.json
    intake_dir = Path(source.path).resolve().parent.parent.parent
    run_ids = _intake_run_ids(intake_dir)
    return SliceContext(
        intake_dir=intake_dir,
        run_id=str(source.approval.field("run_id")),
        plan_label=source.label,
        run_ids=run_ids,
    )


def _intake_run_ids(intake_dir: Path) -> tuple[str, ...]:
    path = intake_dir / "interpretation.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanObligationError(f"cannot read the intake's run slices from {path}: {exc}") from exc
    runs = payload.get("runs") if isinstance(payload, dict) else None
    ids = tuple(str(run.get("id")) for run in runs or () if isinstance(run, dict) and run.get("id"))
    if not ids:
        raise PlanObligationError(f"{path} names no run slices")
    return ids


def incomplete_slices(context: SliceContext) -> tuple[str, ...]:
    """The other run slices of this intake that are not proven complete.

    Uses the same proof approval uses for an earlier slice (its sealed run
    recorded complete with an accepted final candidate), so "complete" means
    one thing everywhere.
    """

    from agent_sparring.intake import IntakeError, _completed_slice

    pending: list[str] = []
    for run_id in context.run_ids:
        if run_id == context.run_id:
            continue
        try:
            _completed_slice(context.intake_dir, run_id)
        except IntakeError:
            pending.append(run_id)
    return tuple(pending)


def load_ledger(path: Path) -> tuple[CarriedObligation, ...]:
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanObligationError(f"plan obligation ledger {path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != LEDGER_VERSION:
        raise PlanObligationError(f"plan obligation ledger {path} is not version {LEDGER_VERSION}")
    entries = payload.get("obligations")
    if not isinstance(entries, list):
        raise PlanObligationError(f"plan obligation ledger {path} has no obligations array")
    return tuple(CarriedObligation.from_dict(entry, path) for entry in entries)


def _write_ledger(path: Path, plan_label: str, entries: tuple[CarriedObligation, ...]) -> None:
    text = json.dumps(
        {
            "version": LEDGER_VERSION,
            "plan": plan_label,
            "obligations": [entry.to_dict() for entry in entries],
        },
        indent=2,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise ledger writers: slices in two repositories can finish at once."""

    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.name}.lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def carry(
    context: SliceContext,
    obligations: tuple[DeferredObligation, ...],
    *,
    origin_run: str,
    origin_sparring_dir: Path,
) -> None:
    """Record ``obligations`` in the plan ledger. Idempotent per gate instance:
    carrying the same asking twice updates it (its latest results) rather than
    duplicating it."""

    path = context.ledger_path
    with _locked(path):
        entries = list(load_ledger(path))
        index = {entry.instance_id: position for position, entry in enumerate(entries)}
        for obligation in obligations:
            entry = CarriedObligation(
                obligation=obligation,
                origin_run=origin_run,
                origin_sparring_dir=str(Path(origin_sparring_dir).resolve()),
                origin_slice=context.run_id,
                carried_at=_now(),
            )
            if obligation.instance_id in index:
                old = entries[index[obligation.instance_id]]
                entries[index[obligation.instance_id]] = CarriedObligation(
                    obligation=obligation,
                    origin_run=old.origin_run,
                    origin_sparring_dir=old.origin_sparring_dir,
                    origin_slice=old.origin_slice,
                    carried_at=old.carried_at,
                )
            else:
                entries.append(entry)
        _write_ledger(path, context.plan_label, tuple(entries))


def owed(context: SliceContext) -> tuple[CarriedObligation, ...]:
    """Every carried obligation of this plan not yet resolved."""

    return tuple(entry for entry in load_ledger(context.ledger_path) if entry.resolved_by is None)


def origin_of(context: SliceContext | None, instance_id: str) -> CarriedObligation | None:
    if context is None:
        return None
    for entry in load_ledger(context.ledger_path):
        if entry.instance_id == instance_id:
            return entry
    return None


def sync(context: SliceContext, obligations: tuple[DeferredObligation, ...], *, run: str) -> None:
    """Write the current results of carried obligations back to the ledger,
    marking each resolved one as answered by ``run``. Obligations the ledger
    never carried are ignored: they belong to this run alone."""

    path = context.ledger_path
    with _locked(path):
        entries = list(load_ledger(path))
        current = {obligation.instance_id: obligation for obligation in obligations}
        changed = False
        for position, entry in enumerate(entries):
            updated = current.get(entry.instance_id)
            if updated is None:
                continue
            entries[position] = CarriedObligation(
                obligation=updated,
                origin_run=entry.origin_run,
                origin_sparring_dir=entry.origin_sparring_dir,
                origin_slice=entry.origin_slice,
                carried_at=entry.carried_at,
                resolved_by=run if updated.resolved else None,
            )
            changed = True
        if changed:
            _write_ledger(path, context.plan_label, tuple(entries))


__all__ = [
    "CarriedObligation",
    "LEDGER_DIRNAME",
    "PlanObligationError",
    "SliceContext",
    "carry",
    "incomplete_slices",
    "load_ledger",
    "origin_of",
    "owed",
    "slice_context",
    "sync",
]
