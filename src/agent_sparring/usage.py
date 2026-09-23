"""A readable report of what a stage's provider turns actually used.

``activity.jsonl`` already records the facts -- which provider and model the
engine selected and where that selection came from, what each turn cost, how
long it took -- but it records them as one JSON object per line, interleaved
across every tool call the providers made. This module is the reader that
turns those lines back into the short answer a person wants: for this stage,
which agents ran with what, how long they took, and how many tokens the
providers reported.

It is a report and nothing else. :mod:`agent_sparring.activity` deliberately
offers no read API, because nothing in orchestration may consult the log to
decide routing, lifecycle status, acceptance, candidate identity or session
identity. That prohibition is about orchestration, not about a person
reading their own telemetry, so reading happens here, in a module no
orchestration path imports. The same non-authority follows from that: a
missing, truncated, or partly corrupt log yields a partial report rather
than an error, and no report ever changes what a run does.

Everything shown is a quotation. A number the provider never reported is
absent, shown as ``-``, and is never inferred, defaulted to zero, or
computed from a model name. The distinction between "the engine asked for
this model" (``requested_model``) and "the provider said it ran this model"
(``model``) is preserved, because a mismatch between the two is exactly the
kind of thing this report exists to make visible.
"""

from __future__ import annotations

import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from agent_sparring.stage import ACTIVITY_FILENAME, Stage

#: The actor each agent role speaks as in the log, and the order roles are
#: reported in. Mirrors the mapping the CLI emits with; kept here as data so
#: a reader of this module can see the whole vocabulary it interprets.
ROLE_ACTORS: tuple[tuple[str, str], ...] = (("stage", "stage"), ("sparring", "sparrer"))

#: Events that begin a provider turn, by actor.
#: ``dialogue.started`` is one of them because a reviewer conversation
#: (``sparring ask``) spends the same context window and the same money as a
#: review turn, and does so inside the reviewer's own thread. Leaving it out
#: would make the report understate what the stage cost, and would hide the
#: one kind of turn whose cost a person chooses to incur directly.
_TURN_STARTED = {"turn.started", "sparring.started", "dialogue.started"}
#: Events that end one, mapped to the outcome word the report prints.
_TURN_ENDED = {
    "turn.finished": "finished",
    "turn.failed": "failed",
    "sparring.failed": "failed",
    "dialogue.finished": "asked",
    "dialogue.failed": "failed",
    "verdict": "verdict",
}


@dataclass
class TurnRecord:
    """One provider turn as the log describes it."""

    actor: str
    started: str | None = None
    ended: str | None = None
    outcome: str | None = None
    duration_ms: int | None = None
    #: True when ``duration_ms`` was derived from the start and end event
    #: timestamps rather than measured by the engine around the provider
    #: call. Logs written before the engine measured turns have only the
    #: timestamps, and a derived figure is slightly wider than the provider
    #: call itself, so the report marks it rather than passing it off as
    #: the measured one.
    duration_derived: bool = False
    resumed: bool | None = None
    session_id: str | None = None
    action: str | None = None
    summary: str | None = None


@dataclass
class RoleUsage:
    """One role's configuration and its latest reported budget."""

    role: str
    actor: str
    provider: str | None = None
    provider_source: str | None = None
    requested_model: str | None = None
    model_source: str | None = None
    requested_effort: str | None = None
    effort_source: str | None = None
    reported_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    context_used_tokens: int | None = None
    context_window: int | None = None
    #: True once any ``agents.resolved`` event was seen for this role, so the
    #: report can distinguish "ran with the provider default" from "this log
    #: predates the event and simply does not say".
    resolved_seen: bool = False


@dataclass
class StageUsage:
    """Everything this report knows about one stage."""

    stage_id: str
    log_path: Path
    log_present: bool = False
    roles: dict[str, RoleUsage] = field(default_factory=dict)
    turns: list[TurnRecord] = field(default_factory=list)
    #: Lines that were not valid JSON objects. Reported as a count so a
    #: damaged log is visible without the report pretending to repair it.
    unreadable_lines: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "log_present": self.log_present,
            "unreadable_lines": self.unreadable_lines,
            "roles": {
                role: {
                    key: value
                    for key, value in vars(usage).items()
                    if key not in ("role", "actor")
                }
                for role, usage in self.roles.items()
            },
            "turns": [vars(turn) for turn in self.turns],
        }


def _records(path: Path) -> Iterator[tuple[dict[str, Any] | None, None]]:
    """Yield each line's parsed object, or ``None`` when it is unusable."""

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                yield None, None
                continue
            yield (parsed if isinstance(parsed, dict) else None), None


def collect_stage_usage(stage: Stage) -> StageUsage:
    """Read one stage's activity log into a :class:`StageUsage`.

    Never raises for a missing or damaged log: the returned object simply
    says so.
    """

    log_path = stage.directory / ACTIVITY_FILENAME
    usage = StageUsage(
        stage_id=stage.stage_id,
        log_path=log_path,
        log_present=log_path.is_file(),
        roles={
            role: RoleUsage(role=role, actor=actor) for role, actor in ROLE_ACTORS
        },
    )
    if not usage.log_present:
        return usage

    by_actor = {actor: role for role, actor in ROLE_ACTORS}
    open_turn: dict[str, TurnRecord] = {}

    for record, _ in _records(log_path):
        if record is None:
            usage.unreadable_lines += 1
            continue
        actor = record.get("actor")
        event = record.get("event")
        if not isinstance(actor, str) or not isinstance(event, str):
            usage.unreadable_lines += 1
            continue
        role_name = by_actor.get(actor)
        role = usage.roles.get(role_name) if role_name else None

        if role is not None:
            if role.reported_model is None:
                model = record.get("model")
                if isinstance(model, str) and model:
                    role.reported_model = model
            if role.provider is None:
                # Every adapter binds its provider id onto each event it
                # emits, so a log written before ``agents.resolved`` existed
                # still says which provider ran. The selection's *source*
                # genuinely is not recoverable from such a log, and stays
                # blank rather than being guessed at.
                provider = record.get("provider")
                if isinstance(provider, str) and provider:
                    role.provider = provider

        if event == "agents.resolved" and role is not None:
            role.resolved_seen = True
            role.provider = record.get("provider") or role.provider
            role.provider_source = record.get("provider_source")
            role.requested_model = record.get("requested_model")
            role.model_source = record.get("model_source")
            role.requested_effort = record.get("requested_effort")
            role.effort_source = record.get("effort_source")
            continue

        if event == "provider.usage" and role is not None:
            # Cumulative over the session, so the last one seen is the
            # current total. Each field is carried over independently:
            # a later event that omits a number has not reset it to zero,
            # it simply did not restate it.
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "context_used_tokens",
                "context_window",
            ):
                value = record.get(key)
                if isinstance(value, int):
                    setattr(role, key, value)
            model = record.get("model")
            if isinstance(model, str) and model:
                role.reported_model = model
            continue

        if event in _TURN_STARTED:
            turn = TurnRecord(
                actor=actor,
                started=record.get("ts"),
                resumed=record.get("resumed"),
                session_id=record.get("session_id"),
            )
            open_turn[actor] = turn
            usage.turns.append(turn)
            continue

        if event in _TURN_ENDED:
            turn = open_turn.pop(actor, None)
            if turn is None:
                # An end without a start: a log that begins mid-turn, or one
                # whose opening line was lost. Recorded as its own turn
                # rather than dropped, so the count stays honest.
                turn = TurnRecord(actor=actor)
                usage.turns.append(turn)
            turn.ended = record.get("ts")
            turn.outcome = _TURN_ENDED[event]
            duration = record.get("duration_ms")
            if isinstance(duration, int):
                turn.duration_ms = duration
            else:
                turn.duration_ms = _span_ms(turn.started, turn.ended)
                turn.duration_derived = turn.duration_ms is not None
            if record.get("session_id"):
                turn.session_id = record.get("session_id")
            if record.get("action"):
                turn.action = record.get("action")
            if record.get("summary"):
                turn.summary = record.get("summary")

    return usage


def _span_ms(started: str | None, ended: str | None) -> int | None:
    """Milliseconds between two log timestamps, or ``None`` if either is
    missing or unparseable. Used only as a fallback for logs written before
    the engine measured turns itself; a negative span (a clock adjustment
    between the two events) is discarded rather than reported."""

    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        begin = datetime.fromisoformat(started.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    span = int((finish - begin).total_seconds() * 1000)
    return span if span >= 0 else None


def _thousands(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _sourced(value: str | None, source: str | None, *, seen: bool) -> str:
    """One configured field as "value (where it was set)"."""

    if not seen:
        return "-"
    if value is None:
        return f"provider default ({source})" if source else "provider default"
    return f"{value} ({source})" if source else value


def format_duration(milliseconds: int | None) -> str:
    """A compact human duration: ``-``, ``4.2s``, ``3m 07s`` or ``1h 04m``."""

    if milliseconds is None:
        return "-"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _timestamp(raw: str | None) -> str:
    """The log's ISO timestamp trimmed to minutes, for a narrow column."""

    if not isinstance(raw, str) or len(raw) < 16:
        return "-"
    return raw[:16].replace("T", " ")


def render_stage_usage(usage: StageUsage) -> str:
    """The human-readable report for one stage."""

    lines: list[str] = [f"stage {usage.stage_id}"]
    if not usage.log_present:
        lines.append(f"  no activity log at {usage.log_path}")
        return "\n".join(lines)

    for role, _ in ROLE_ACTORS:
        entry = usage.roles[role]
        provider = entry.provider or "-"
        if entry.provider_source:
            provider = f"{provider} ({entry.provider_source})"
        lines.append(f"  {role + ' agent':<15} provider {provider}")
        lines.append(
            f"  {'':<15} model    "
            f"{_sourced(entry.requested_model, entry.model_source, seen=entry.resolved_seen)}"
        )
        lines.append(
            f"  {'':<15} effort   "
            f"{_sourced(entry.requested_effort, entry.effort_source, seen=entry.resolved_seen)}"
        )
        if entry.reported_model and entry.reported_model != entry.requested_model:
            # Worth its own line: the provider ran something other than what
            # the engine named, which no other artifact would reveal.
            lines.append(
                f"  {'':<15} provider reported model {entry.reported_model}"
            )
        lines.append(
            f"  {'':<15} tokens   in {_thousands(entry.input_tokens)}"
            f"  out {_thousands(entry.output_tokens)}"
            f"  total {_thousands(entry.total_tokens)}"
        )
        if entry.context_used_tokens is not None or entry.context_window is not None:
            lines.append(
                f"  {'':<15} context  {_thousands(entry.context_used_tokens)}"
                f" of {_thousands(entry.context_window)}"
            )

    if usage.turns:
        lines.append("")
        lines.append(
            f"  {'#':>2}  {'role':<8} {'started (UTC)':<17} "
            f"{'duration':>9}  {'resumed':<7} outcome"
        )
        for index, turn in enumerate(usage.turns, start=1):
            outcome = turn.outcome or "in progress / unfinished"
            if turn.action:
                outcome = f"{outcome}: {turn.action}"
            resumed = "-" if turn.resumed is None else ("yes" if turn.resumed else "no")
            shown = format_duration(turn.duration_ms)
            if turn.duration_derived:
                shown = "~" + shown
            lines.append(
                f"  {index:>2}  {turn.actor:<8} {_timestamp(turn.started):<17} "
                f"{shown:>9}  {resumed:<7} {outcome}"
            )
        total = sum(turn.duration_ms or 0 for turn in usage.turns)
        measured = sum(1 for turn in usage.turns if turn.duration_ms is not None)
        if measured:
            note = "" if measured == len(usage.turns) else f" (over {measured} of {len(usage.turns)} turns)"
            if any(turn.duration_derived for turn in usage.turns):
                note += "; ~ derived from event timestamps"
            lines.append(f"  {'':>2}  total provider time {format_duration(total)}{note}")
    else:
        lines.append("")
        lines.append("  no provider turns recorded")

    if usage.unreadable_lines:
        lines.append(
            f"  note: {usage.unreadable_lines} unreadable log line(s) were skipped"
        )
    return "\n".join(lines)


def render_report(usages: Sequence[StageUsage]) -> str:
    """The report for one or more stages."""

    return "\n\n".join(render_stage_usage(usage) for usage in usages)


__all__ = [
    "ROLE_ACTORS",
    "RoleUsage",
    "StageUsage",
    "TurnRecord",
    "collect_stage_usage",
    "format_duration",
    "render_report",
    "render_stage_usage",
]
