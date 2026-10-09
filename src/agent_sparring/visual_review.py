"""What an image-aware reviewer is shown, and what it is told about it.

:mod:`agent_sparring.visual_evidence` defines evidence and
:mod:`agent_sparring.visual_capture` produces it; this module hands it to a
sparring turn. Two things, both derived from the engine's own capture
record and never from anything an agent wrote:

- **The images.** :func:`prepare_delivery` reloads the stage's current
  capture (:func:`~agent_sparring.visual_capture.load_current_evidence`
  re-derives the candidate and re-hashes every file), refuses it unless it
  is exactly the capture the caller made for this review, and lists every
  captured screenshot and every distinct reference as numbered
  attachments, in the order the provider attaches them. A screenshot comes
  first and its reference, the first time it appears, right after it.
- **The instructions.** :func:`visual_evidence_section` says which image is
  which, the written visual criteria (``V1``, ``V2``... from
  ``[visual_review] criteria``), how to compare, what counts as a defect
  rather than a permitted variation, how a finding must name the failing
  screenshot and criterion, and what stays a person's decision.

Nothing here decides a verdict. What it guarantees is that a sparring turn
under visual review cannot start without the pixels: the caller
(:func:`agent_sparring.sparring_agent.run_sparring_agent`) requires an
image-capable adapter and a delivery before any provider turn, so a
reviewer's -- or an implementer's -- prose that the UI "looks right" never
stands in for an inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_sparring.prompt_sections import PromptSection, section
from agent_sparring.stage import Stage
from agent_sparring.visual_capture import (
    EVIDENCE_DIRNAME,
    CaptureRecord,
    VisualCaptureError,
    load_current_evidence,
)
from agent_sparring.visual_evidence import VisualEvidenceError, resolve_inside

KIND_SCREENSHOT = "screenshot"
KIND_REFERENCE = "reference"


@dataclass(frozen=True)
class VisualReviewRequest:
    """The capture a sparring turn must be shown, and the criteria with it.

    ``capture`` is the record :func:`~agent_sparring.visual_capture.run_capture`
    returned for the candidate about to be reviewed; ``criteria`` is
    ``[visual_review] criteria``.
    """

    capture: CaptureRecord
    criteria: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attachment:
    """One image attached to the turn. ``number`` is its 1-based position
    among the attached images; ``name`` is the screenshot id or the
    reference's repository path."""

    number: int
    kind: str
    name: str
    path: Path


@dataclass(frozen=True)
class VisualDelivery:
    """Verified evidence ready to attach: the capture, its criteria, and the
    images in attachment order."""

    capture: CaptureRecord
    criteria: tuple[str, ...]
    attachments: tuple[Attachment, ...]

    @property
    def images(self) -> tuple[Path, ...]:
        return tuple(attachment.path for attachment in self.attachments)

    def screenshot_count(self) -> int:
        return sum(1 for a in self.attachments if a.kind == KIND_SCREENSHOT)


def prepare_delivery(repo_root: Path, stage: Stage, request: VisualReviewRequest) -> VisualDelivery:
    """The images for ``request``, refused unless they are still current.

    Raises :class:`~agent_sparring.visual_capture.VisualCaptureError` when
    the stage's current evidence is missing, stale, altered, or a different
    capture from the one requested -- the turn then never starts.
    """

    repo_root = Path(repo_root)
    current = load_current_evidence(repo_root, stage)
    if current != request.capture:
        raise VisualCaptureError(
            f"the stage's current visual evidence is {current.capture_id}, not the capture "
            f"{request.capture.capture_id} made for this review"
        )
    directory = current.directory(stage)
    attachments: list[Attachment] = []
    attached_references: set[str] = set()
    try:
        for shot in current.manifest.screenshots:
            if shot.captured:
                assert shot.path is not None
                attachments.append(
                    Attachment(
                        number=len(attachments) + 1,
                        kind=KIND_SCREENSHOT,
                        name=shot.id,
                        path=resolve_inside(directory, shot.path),
                    )
                )
            if shot.reference is not None and shot.reference not in attached_references:
                attached_references.add(shot.reference)
                attachments.append(
                    Attachment(
                        number=len(attachments) + 1,
                        kind=KIND_REFERENCE,
                        name=shot.reference,
                        path=resolve_inside(repo_root, shot.reference),
                    )
                )
    except VisualEvidenceError as exc:
        raise VisualCaptureError(f"visual evidence is no longer valid: {exc}") from exc
    if not any(a.kind == KIND_SCREENSHOT for a in attachments):
        raise VisualCaptureError("the current capture has no screenshot to show the reviewer")
    return VisualDelivery(
        capture=current, criteria=tuple(request.criteria), attachments=tuple(attachments)
    )


def criterion_id(index: int) -> str:
    return f"V{index + 1}"


_HOW_TO_REVIEW = """\
### How to review the images

Look at the attached images themselves; that is what this review is for.
You may open the files at the paths above read-only, but do not decode PNG
bytes with a command instead of looking. Compare every screenshot with its
reference (when it has one) and with the criteria, for:

- **layout** -- which elements are present, their order, grouping and
  alignment, and where each sits relative to the others;
- **proportions** -- the relative sizes of regions, columns, charts and
  panels;
- **clipping and overflow** -- truncated or cut-off text and elements,
  overlapping content, unintended scrollbars, content running off the
  viewport;
- **responsive behaviour** -- each viewport shows the layout intended for
  it; compare screenshots of the same view at different viewports with each
  other as well as with their references;
- **legibility** -- text size, contrast, labels that collide or are
  obscured;
- **visual hierarchy** -- headings, emphasis and primary actions read in the
  intended order of importance.

A reference shows design intent, not a pixel target. These are permitted
variations and are not findings: different data values, numbers,
measurements, counts, dates or text content; exact pixel positions and
sizes; font rendering and anti-aliasing; small spacing differences that
leave the arrangement intact; placeholder content in the mockup. These are
structural mismatches and are findings: a missing, extra or reordered
element; a different arrangement (side by side instead of stacked, a
column in the wrong place); materially different proportions between
regions; clipped, overlapping or illegible content; a layout that does not
adapt to its viewport; a loading, empty or error state where populated
content is expected. When it is unclear which kind a difference is, say
which you judged it to be and why.

### Visual findings

State every visual defect in ``findings`` itself -- the summary is only a
headline -- and in each one name the screenshot by id (and image number),
the criterion it fails ({criterion_ref}), what the image shows, and what it
should show instead -- specific enough for the implementer to fix it without
seeing the image you saw. An objective visual defect is an implementation
issue: SEND_BACK.

Judge visual criteria only from these images. A statement in the handoff
or anywhere else that the UI looks right, matches the mockup or was
checked visually is a claim, not evidence: it cannot pass a
criterion the images do not show passing, and cannot overrule what they do
show. A criterion that depends on a screenshot that was not captured is
unverified, never passing; say so in ``findings`` -- SEND_BACK when the
implementation must make that view capturable, NEEDS_YOU only when a person
genuinely has to look instead.

### What a person still decides

Objective checks you have verified in these images -- layout, proportions,
clipping, responsive behaviour, legibility and hierarchy against the
references and criteria -- need no manual layout check. Do not put them in
``human_gate`` or ``deferred_human_gate``: READY on correct evidence is
right. A person remains the judge of subjective product and aesthetic
choices: whether a valid design is the preferred one, taste and brand feel,
or whether a deliberate departure from the mockup is an improvement. Do not
decide those yourself and do not send them back as defects; raise them as
NEEDS_YOU (``PRODUCT_PREFERENCE`` or ``UI_VISUAL_CHECK``) when this stage's
acceptance depends on the answer, or with READY in ``deferred_human_gate``
when it does not."""


def visual_evidence_section(delivery: VisualDelivery, stage: Stage) -> PromptSection:
    """The engine's statement of the attached images and how to judge them."""

    capture = delivery.capture
    capture_dir = f"stages/{stage.stage_id}/{EVIDENCE_DIRNAME}/{capture.capture_id}"
    shots = {shot.id: shot for shot in capture.manifest.screenshots}
    references = {a.name: a for a in delivery.attachments if a.kind == KIND_REFERENCE}

    lines = [
        "## Visual evidence",
        "",
        f"The engine ran this repository's screenshot capture for the candidate you are "
        f"reviewing ({capture.candidate.describe()}) and attached {len(delivery.attachments)} "
        f"image(s) to this turn, in the order listed. Before this turn started it checked "
        f"every file is a valid PNG whose SHA-256 is bound to exactly this candidate (capture "
        f"`{capture.capture_id}`). No agent produced, chose or described these images.",
        "",
        "These images replace every image attached to an earlier turn of this conversation. "
        "Earlier screenshots show an earlier candidate: do not judge this one by them, and "
        "re-check any visual finding you made before against these images before repeating "
        "or dropping it.",
        "",
        "### Attached images",
        "",
    ]
    for attachment in delivery.attachments:
        if attachment.kind == KIND_REFERENCE:
            owners = ", ".join(
                f"`{s.id}`" for s in capture.manifest.screenshots if s.reference == attachment.name
            )
            lines.append(
                f"- Image {attachment.number}: reference mockup `{attachment.name}` (for {owners})."
            )
            continue
        shot = shots[attachment.name]
        reference = references.get(shot.reference) if shot.reference else None
        compare = (
            f"compare with Image {reference.number} (reference `{reference.name}`)"
            if reference is not None
            else "no reference image: judge it against the criteria and the brief"
        )
        lines.append(
            f"- Image {attachment.number}: screenshot `{shot.id}` at viewport "
            f"{shot.viewport.width}x{shot.viewport.height} (`{capture_dir}/{shot.path}`); "
            f"{compare}."
        )
    for shot in capture.manifest.screenshots:
        if not shot.captured:
            lines.append(
                f"- Screenshot `{shot.id}` ({shot.viewport.width}x{shot.viewport.height}) -- "
                "capture FAILED: there is no image for it, so nothing that depends on it has "
                "been seen."
            )

    lines += ["", "### Visual acceptance criteria", ""]
    if delivery.criteria:
        lines += [
            f"- `{criterion_id(index)}` -- {criterion}"
            for index, criterion in enumerate(delivery.criteria)
        ]
        lines += [
            "",
            "These apply to every screenshot, together with every visual requirement the "
            "stage brief states.",
        ]
    else:
        lines.append(
            "No written visual criteria are configured for this project; judge against the "
            "visual requirements the stage brief states and the reference images."
        )
    criterion_ref = (
        "by its id, ``V1``, ``V2``... above, or a stage brief requirement, quoted"
        if delivery.criteria
        else "the stage brief's requirement, quoted"
    )
    lines += ["", _HOW_TO_REVIEW.format(criterion_ref=criterion_ref)]
    return section("Visual evidence", lines)


__all__ = [
    "Attachment",
    "KIND_REFERENCE",
    "KIND_SCREENSHOT",
    "VisualDelivery",
    "VisualReviewRequest",
    "criterion_id",
    "prepare_delivery",
    "visual_evidence_section",
]
