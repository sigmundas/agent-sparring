"""The structure of an assembled provider prompt.

Both prompt builders (:mod:`agent_sparring.stage_prompt` and
:mod:`agent_sparring.sparring_prompt`) assemble their prompt as an ordered
list of :class:`PromptSection` blocks, and the flat prompt text is nothing
but those blocks joined. That is the whole point of this module: there is
one assembly path, so a reader that wants the prompt broken into labelled,
sourced sections and a reader that wants the exact bytes sent to the
provider cannot disagree about what the prompt was.

A section records where its content came from. ``origin="file"`` names a
real file (``source`` is its path relative to the project's ``.sparring``
directory, e.g. ``stages/<id>/brief.md`` or ``PROJECT.md``), so a UI can
offer to open it. ``origin="engine"`` is text this package itself wrote --
the branch constraint, the scope reminder, the self-check, the finalization
instruction, the sparrer's verdict instruction. That distinction is worth
recording precisely because it is easy to lose: a stage whose brief asks
for a review while the engine's own framing asks for an implementation is
exactly the kind of mistake that is invisible until you can see which words
came from where.

Nothing here is workflow state. No orchestration decision reads a section,
a turn kind, or anything else in this module.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sections are separated by a blank line, which is what makes the joined
# result an ordinary Markdown document rather than a run-together one.
SECTION_SEPARATOR = "\n\n"

ORIGIN_FILE = "file"
ORIGIN_ENGINE = "engine"

# Which turn of a stage's life this prompt was assembled for. These are
# recorded alongside a captured prompt so a reader does not have to infer
# the turn from state that has since moved on -- see
# :mod:`agent_sparring.prompt_capture` for why inference is not good enough.
TURN_ORIGINAL = "original"
TURN_RESUME = "resume"
TURN_FINALIZATION = "finalization"
TURN_EVIDENCE_REVIEW = "evidence_review"
TURN_FINALIZED_REVIEW = "finalized_review"
# The first turn of a fresh session generation (agent_sparring.sessions).
TURN_FRESH = "fresh"

ROLE_STAGE = "stage"
ROLE_SPARRER = "sparrer"
# The independent reviewer of a review-only stage (see
# :mod:`agent_sparring.review`). Deliberately its own role rather than
# ``sparrer``: it reviews an already-accepted candidate set instead of one
# stage agent's work, there is no implementation turn beside it, and an
# inspector that showed it as a sparring turn would be describing a pairing
# that does not exist for that stage.
ROLE_REVIEWER = "reviewer"


@dataclass(frozen=True)
class PromptSection:
    """One labelled block of an assembled prompt.

    ``text`` is the block exactly as it appears in the prompt, heading line
    included, with no trailing whitespace -- build one with :func:`section`
    rather than constructing it directly, so that invariant holds.
    """

    heading: str
    origin: str
    source: str | None
    text: str


def section(heading: str, lines: list[str], *, source: str | None = None) -> PromptSection:
    """One section from its already-rendered lines.

    ``source`` names the file the content came from, relative to the
    project's ``.sparring`` directory; omitting it marks the section as
    engine-authored text.
    """

    return PromptSection(
        heading=heading,
        origin=ORIGIN_FILE if source else ORIGIN_ENGINE,
        source=source,
        text="\n".join(lines).rstrip(),
    )


@dataclass(frozen=True)
class AssembledPrompt:
    """An assembled prompt: its sections, and what turn it was built for.

    :attr:`text` is the exact string handed to the provider adapter. It is
    derived from :attr:`sections` and never stored separately, so the two
    can never describe different prompts.
    """

    role: str
    stage_id: str
    turn_kind: str
    resumed: bool
    expected_branch: str | None
    sections: tuple[PromptSection, ...]

    @property
    def text(self) -> str:
        """The flat prompt: every section, joined, with one trailing newline."""

        return SECTION_SEPARATOR.join(part.text for part in self.sections).rstrip() + "\n"

    def spans(self) -> tuple[tuple[int, int], ...]:
        """Each section's ``(start, end)`` character offsets into :attr:`text`.

        These let a reader slice the captured prompt itself into its
        sections instead of re-parsing it for headings. Because every
        section's text is right-stripped on construction, the join carries
        no trailing whitespace and the offsets below are exact.
        """

        offsets: list[tuple[int, int]] = []
        cursor = 0
        for index, part in enumerate(self.sections):
            if index:
                cursor += len(SECTION_SEPARATOR)
            offsets.append((cursor, cursor + len(part.text)))
            cursor += len(part.text)
        return tuple(offsets)


def stage_turn_kind(*, resume: bool, finalize_only: bool, fresh: bool = False) -> str:
    """The turn kind for a stage-agent prompt.

    A turn carrying a human's recorded answer is deliberately not its own
    kind. Evidence reaches the sparrer first (the loop's
    ``start_with="sparring"``), and a stage agent only ever sees it on an
    ordinary correction turn -- so the honest record of "this turn had the
    human's answer in it" is the presence of the human-evidence *section*,
    not a different name for the turn.
    """

    if finalize_only:
        return TURN_FINALIZATION
    if fresh:
        return TURN_FRESH
    return TURN_RESUME if resume else TURN_ORIGINAL


def sparring_turn_kind(
    *,
    resume: bool,
    evidence_first: bool = False,
    finalization: bool = False,
    fresh: bool = False,
) -> str:
    """The turn kind for a sparring-agent prompt.

    ``evidence_first`` is the loop entering at the sparrer against an
    unchanged candidate because a human just answered a NEEDS_YOU gate;
    ``finalization`` is the turn that reviews the commit a bounded
    commit/push turn just produced. Both are things only the caller knows,
    which is why neither is guessed here.
    """

    if finalization:
        return TURN_FINALIZED_REVIEW
    if evidence_first:
        return TURN_EVIDENCE_REVIEW
    if fresh:
        return TURN_FRESH
    return TURN_RESUME if resume else TURN_ORIGINAL


def review_turn_kind(*, resume: bool, evidence_first: bool = False, fresh: bool = False) -> str:
    """The turn kind for an independent reviewer's prompt.

    A review-only stage has no implementation turn and therefore no
    finalization: its reviewer's first turn is ``original``, a turn carrying
    a human's answer to its own gate is ``evidence_review``, and anything
    else that continues the same review is ``resume``. ``evidence_first`` is
    something only the caller knows, so -- as on the sparring side -- it is
    passed in rather than guessed, and it cannot apply to a first turn,
    which by definition has no gate to have answered.
    """

    if fresh and not evidence_first:
        return TURN_FRESH
    if not resume:
        return TURN_ORIGINAL
    return TURN_EVIDENCE_REVIEW if evidence_first else TURN_RESUME


__all__ = [
    "AssembledPrompt",
    "ORIGIN_ENGINE",
    "ORIGIN_FILE",
    "PromptSection",
    "ROLE_REVIEWER",
    "ROLE_SPARRER",
    "ROLE_STAGE",
    "SECTION_SEPARATOR",
    "TURN_EVIDENCE_REVIEW",
    "TURN_FINALIZATION",
    "TURN_FINALIZED_REVIEW",
    "TURN_FRESH",
    "TURN_ORIGINAL",
    "TURN_RESUME",
    "review_turn_kind",
    "section",
    "sparring_turn_kind",
    "stage_turn_kind",
]
