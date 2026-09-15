"""Immutable per-turn capture of the exact provider-facing prompt.

Every stage-agent and sparring-agent turn assembles a prompt and hands it to
a provider adapter. This module writes those exact bytes down, once, at the
moment they are handed over, so that "what was this agent actually told?"
has an answer that does not depend on re-deriving anything later.

Why capture rather than re-render
---------------------------------

The obvious alternative -- rebuild the prompt on demand, the way
``run-stage --dry-run`` does -- cannot answer the question honestly. A
prompt is assembled from files that keep changing (``sparring.md`` is
rewritten every exchange, ``notes.md`` grows, ``handoff.md`` is regenerated
by every stage turn), and the turn kind is re-derived from ``state.json``,
which also moves: :func:`agent_sparring.stage_agent.run_stage_agent`
records a provider-issued session id *during* a first turn, so a re-render
while that turn is still running reports it as a resume and embeds a
``sparring.md`` the live turn never saw. A re-render is a plausible prompt,
not the prompt.

Immutable per turn
------------------

Each capture is a new file; nothing is ever overwritten::

    stages/<id>/prompts/0001-stage-original.md
    stages/<id>/prompts/0002-sparrer-original.md
    stages/<id>/prompts/0003-stage-resume.md
    stages/<id>/prompts/index.jsonl

Overwriting a single "latest" file per role would destroy the ability to
answer why an agent did something three turns ago, which is most of the
value here, and would leave a record that cannot be trusted to describe the
turn it is read next to. ``index.jsonl`` is append-only and purely derived
-- a convenience for a UI that wants the latest turn per role without
listing and parsing filenames. The sequence number is taken from the
directory itself, so a lost or truncated index costs nothing.

Each index line carries the turn's role, kind, and the section table from
:mod:`agent_sparring.prompt_sections`, including each section's character
span in the captured file. A reader slices the captured bytes rather than
re-parsing them for headings, so the readable sectioned view and the exact
prompt are quite literally the same bytes.

Not telemetry, and never fatal
------------------------------

This is deliberately *not* :mod:`agent_sparring.activity`, whose event
schema excludes prompt text on purpose and stays that way. These are stage
artifacts, in the stage directory, assembled from files that already live in
that same directory -- so capturing them exposes nothing that was not
already on disk beside them.

Like the activity log, a failure here is never an orchestration event:
:func:`capture_prompt` swallows its own errors and returns ``None``. A full
disk or a read-only stage directory must not take down a provider turn that
was about to run, and a missing capture costs only this inspector.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_sparring.prompt_sections import AssembledPrompt

SCHEMA_VERSION = 1

PROMPTS_DIRNAME = "prompts"
INDEX_FILENAME = "index.jsonl"

# A captured prompt's filename: sequence, role, turn kind. Matched rather
# than merely prefixed, so that a file which is not one of ours in this
# directory is still recognised as foreign (see is_prompt_artifact).
_CAPTURE_RE = re.compile(r"^(\d{4})-(stage|sparrer)-[a-z_]+\.md$")

_SEQUENCE_RE = re.compile(r"^(\d{4})-")


def is_prompt_artifact(name: str) -> bool:
    """Is ``name`` a file this module writes into a stage's ``prompts/``?

    Used by the acceptance gate to exempt captured prompts from its
    "no unrepresented dirty changes" check. It matches exact shapes, never a
    prefix or a whole subtree: a stray file dropped into ``prompts/`` must
    still block a freeze, exactly as one dropped beside ``brief.md`` does.
    """

    return name == INDEX_FILENAME or bool(_CAPTURE_RE.match(name))


@dataclass(frozen=True)
class CapturedPrompt:
    """Where one turn's prompt was written, and under what sequence number."""

    seq: int
    path: Path
    filename: str


def prompts_dir(stage_directory: Path) -> Path:
    """The directory holding one stage's captured prompts."""

    return Path(stage_directory) / PROMPTS_DIRNAME


def _next_sequence(directory: Path) -> int:
    """One past the highest sequence already present in ``directory``.

    Read from the filenames rather than from ``index.jsonl`` so that a
    missing, truncated or hand-edited index can never cause an existing
    capture to be overwritten.
    """

    highest = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 1
    for entry in entries:
        match = _SEQUENCE_RE.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def capture_prompt(stage_directory: Path, assembled: AssembledPrompt) -> CapturedPrompt | None:
    """Write ``assembled`` to a new, never-reused file and index it.

    Returns where it landed, or ``None`` if anything at all went wrong --
    this function raises nothing. Call it immediately before handing the
    prompt to a provider adapter, so what is on disk is what was sent.
    """

    try:
        directory = prompts_dir(stage_directory)
        directory.mkdir(parents=True, exist_ok=True)
        text = assembled.text

        # Exclusive creation, retried on the (only expected) collision of a
        # sequence number that appeared between the scan and the write. The
        # file is never opened for writing in a mode that could truncate an
        # existing capture.
        seq = _next_sequence(directory)
        for attempt in range(seq, seq + 25):
            filename = f"{attempt:04d}-{assembled.role}-{assembled.turn_kind}.md"
            try:
                with open(directory / filename, "x", encoding="utf-8") as handle:
                    handle.write(text)
            except FileExistsError:
                continue
            _append_index(directory, assembled, seq=attempt, filename=filename, chars=len(text))
            return CapturedPrompt(seq=attempt, path=directory / filename, filename=filename)
        return None
    except Exception:  # noqa: BLE001 -- a capture failure is never fatal
        return None


def _append_index(
    directory: Path,
    assembled: AssembledPrompt,
    *,
    seq: int,
    filename: str,
    chars: int,
) -> None:
    """Append one line describing this capture to ``index.jsonl``.

    Derived data only. Nothing reads the index to decide anything, and a
    caller that cannot write it still has the captured prompt itself.
    """

    spans = assembled.spans()
    entry = {
        "v": SCHEMA_VERSION,
        "seq": seq,
        "ts": _utc_now_iso(),
        "role": assembled.role,
        "stage_id": assembled.stage_id,
        "turn_kind": assembled.turn_kind,
        "resumed": assembled.resumed,
        "expected_branch": assembled.expected_branch,
        "file": filename,
        "chars": chars,
        "sections": [
            {
                "heading": part.heading,
                "origin": part.origin,
                "source": part.source,
                "start": start,
                "end": end,
            }
            for part, (start, end) in zip(assembled.sections, spans)
        ],
    }
    try:
        with open(directory / INDEX_FILENAME, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return


__all__ = [
    "CapturedPrompt",
    "INDEX_FILENAME",
    "PROMPTS_DIRNAME",
    "SCHEMA_VERSION",
    "capture_prompt",
    "is_prompt_artifact",
    "prompts_dir",
]
