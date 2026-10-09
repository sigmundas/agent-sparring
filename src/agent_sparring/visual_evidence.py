"""Visual evidence: the contract between screenshot capture and image review.

This module only defines and checks evidence. Running a capture command is
:mod:`agent_sparring.visual_capture`; handing images to a reviewer is
:mod:`agent_sparring.visual_review`. Nothing here launches a browser, a renderer or a provider. It is deliberately standard library only:
the contract names no capture technology (Playwright, a JavaScript runtime,
React, Qt), so any repository that can write PNG files and a small JSON
manifest can supply evidence.

Four parts, each a separate guarantee:

**Capture manifest** (:class:`EvidenceManifest`, version
:data:`MANIFEST_VERSION`). What a repository's capture command writes::

    {
      "version": 1,
      "screenshots": [
        {"id": "spore-panel-desktop",
         "path": "spore-panel-desktop.png",
         "viewport": {"width": 1280, "height": 800},
         "status": "captured",
         "reference": "docs/mockups/spore-plot-mockup-web.png"},
        {"id": "spore-panel-mobile", "path": null,
         "viewport": {"width": 375, "height": 812},
         "status": "failed", "reference": null}
      ]
    }

``path`` is relative to the evidence directory the engine gave the capture
command; ``reference`` is relative to the repository root (references are
tracked mockups, screenshots are not). Both must stay inside their root. A
``failed`` screenshot has no ``path``; it is recorded so a review can say
what is missing, and it can never count as inspected evidence. Unknown keys
and unknown versions are refused rather than guessed at.

**Image validity** (:func:`read_png`). Only PNG is accepted, and a file is
checked structurally -- signature, chunk types and CRCs, a valid IHDR
(compression, filter and interlace methods included), PLTE placement and
size, consecutive IDATs, IEND, that the image data is one complete zlib
stream decompressing to exactly the size its header promises, and that
every scanline's filter type is one a decoder knows. This is
not optional politeness: verified live against codex-cli 0.160.0, ``codex
exec --image`` given a missing file *or* a file that is not an image exits 0
and completes the turn as if no image had been attached. A reviewer can only
be trusted to have seen an image the engine checked first.

**Candidate binding** (:class:`EvidenceBinding`). Evidence belongs to one
exact candidate: the full primary commit SHA, every declared sibling's
pinned SHA, and the SHA-256 of every screenshot and reference file. The
binding is computed by the engine (:func:`bind_evidence`), never taken
from the capture command's say-so, and :func:`verify_binding` refuses
evidence when the candidate, a sibling pin or any file has changed since.
A verdict over images therefore applies to the same candidate a textual
verdict does; a new candidate needs a new capture, and stale screenshots
cannot be reused for it. The candidate must be committed: an uncommitted
working tree has no identity to bind to.

**Image input capability** (:func:`require_image_input`). A provider adapter
states whether it can deliver images (``supports_image_input``) and may
add a model-level check (``image_input_problem()``). Anything that cannot
be shown to support image input is *unsupported*, and visual review refuses
with :class:`~agent_sparring.providers.ImageInputUnsupported` before any
turn runs -- there is no "unknown, try anyway" state, and no fallback in
which a reviewer that never saw the pixels returns a verdict about them.

Capture is independent of the reviewer: the engine runs the repository's
capture command itself, outside any provider session, and only *reads* the
resulting files into the review. The reviewer keeps its OS-enforced
read-only sandbox (see :mod:`agent_sparring.providers.codex_cli`) and is
never asked or permitted to produce, refresh or repair evidence; nothing in
this contract requires a writable reviewer.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from agent_sparring.git_context import is_full_sha
from agent_sparring.providers import ImageInputUnsupported

MANIFEST_VERSION = 1
BINDING_VERSION = 1

STATUS_CAPTURED = "captured"
STATUS_FAILED = "failed"
CAPTURE_STATUSES: tuple[str, ...] = (STATUS_CAPTURED, STATUS_FAILED)

# Generous ceilings, not style rules: they only stop a runaway capture from
# handing a reviewer something no model can usefully look at.
MAX_VIEWPORT_DIMENSION = 16_384
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_DECODED_BYTES = 512 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Samples per pixel for each PNG colour type; palette images are 1 sample.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}

_MANIFEST_KEYS = frozenset({"version", "screenshots"})
_SCREENSHOT_KEYS = frozenset({"id", "path", "viewport", "status", "reference"})
_VIEWPORT_KEYS = frozenset({"width", "height"})


class VisualEvidenceError(ValueError):
    """Evidence that does not satisfy the contract. Never a partial success."""


# -- images -------------------------------------------------------------


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int


def read_png(path: Path) -> PngInfo:
    """Check ``path`` is a complete, uncorrupted PNG and return its size.

    Raises :class:`VisualEvidenceError` for anything else, including a
    symlink: evidence is a regular file the engine can hash and hand over
    as-is, not a pointer that could change underneath the review.
    """

    if path.is_symlink() or not path.is_file():
        raise VisualEvidenceError(f"{path} is not a regular file")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise VisualEvidenceError(f"{path} is {size} bytes; the limit is {MAX_IMAGE_BYTES}")
    data = path.read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        raise VisualEvidenceError(f"{path} is not a PNG file")

    def bad(reason: str) -> VisualEvidenceError:
        return VisualEvidenceError(f"{path} {reason}")

    offset = len(_PNG_SIGNATURE)
    header: tuple[int, int, int, int, int] | None = None
    palette_entries = 0
    idat: list[bytes] = []
    idat_closed = False  # IDAT chunks must be consecutive
    ended = False
    while offset < len(data):
        if ended:
            raise bad("has data after its IEND chunk")
        if offset + 8 > len(data):
            raise bad("is truncated")
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        crc_bytes = data[offset + 8 + length : offset + 12 + length]
        if length > 0x7FFFFFFF or len(body) != length or len(crc_bytes) != 4:
            raise bad("is truncated")
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in kind):
            raise bad(f"has an invalid chunk type {kind!r}")
        if zlib.crc32(kind + body) & 0xFFFFFFFF != struct.unpack(">I", crc_bytes)[0]:
            raise bad(f"has a corrupt {kind!r} chunk")
        if header is None and kind != b"IHDR":
            raise bad("does not start with an IHDR chunk")
        if idat and kind != b"IDAT":
            idat_closed = True
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise bad("has a malformed IHDR chunk")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if (
                not 0 < width <= 0x7FFFFFFF
                or not 0 < height <= 0x7FFFFFFF
                or depth not in _PNG_BIT_DEPTHS.get(colour, ())
                or compression != 0
                or filtering != 0
                or interlace not in (0, 1)
            ):
                raise bad("has an invalid IHDR chunk")
            header = (width, height, depth, colour, interlace)
        elif kind == b"PLTE":
            colour, depth = header[3], header[2]
            if (
                palette_entries
                or idat
                or colour in (0, 4)
                or length == 0
                or length % 3
                or length // 3 > (1 << depth if colour == 3 else 256)
            ):
                raise bad("has a misplaced or malformed PLTE chunk")
            palette_entries = length // 3
        elif kind == b"IDAT":
            if idat_closed:
                raise bad("has non-consecutive IDAT chunks")
            if header[3] == 3 and not palette_entries:
                raise bad("is a palette image without a PLTE chunk before its data")
            idat.append(body)
        elif kind == b"IEND":
            if length:
                raise bad("has a malformed IEND chunk")
            ended = True
        elif not kind[0] & 0x20:
            # An unknown *critical* chunk: a decoder must refuse the image.
            raise bad(f"has an unknown critical chunk {kind!r}")
        offset += 12 + length

    if header is None or not ended or not idat:
        raise bad("is missing its IHDR, IDAT or IEND chunk")
    width, height, depth, colour, interlace = header
    rows = _png_scanlines(width, height, depth, colour, interlace)
    expected = sum(count * (1 + row_bytes) for count, row_bytes in rows)
    if expected > MAX_DECODED_BYTES:
        raise bad(f"would decode to {expected} bytes; the limit is {MAX_DECODED_BYTES}")
    decoder = zlib.decompressobj()
    try:
        # Bounded: never inflate more than the header promises (plus one
        # byte, to notice an overlong stream).
        pixels = decoder.decompress(b"".join(idat), expected + 1)
    except zlib.error as exc:
        raise bad(f"has corrupt image data: {exc}") from exc
    if len(pixels) != expected or not decoder.eof or decoder.unused_data:
        raise bad("image data does not match its declared size")
    position = 0
    for count, row_bytes in rows:
        for _ in range(count):
            if pixels[position] > 4:
                raise bad(f"has an invalid scanline filter type {pixels[position]}")
            position += 1 + row_bytes
    return PngInfo(width=width, height=height)


def _png_scanlines(
    width: int, height: int, depth: int, colour: int, interlace: int
) -> list[tuple[int, int]]:
    """``(row count, bytes per row excluding the filter byte)`` per pass."""

    bits = _PNG_CHANNELS[colour] * depth
    if interlace == 0:
        return [(height, (width * bits + 7) // 8)]
    # Adam7: (x start, y start, x step, y step) per pass. An empty pass has
    # no scanlines at all, not even filter bytes.
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    result = []
    for x, y, dx, dy in passes:
        w = (width - x + dx - 1) // dx
        h = (height - y + dy - 1) // dy
        if w > 0 and h > 0:
            result.append((h, (w * bits + 7) // 8))
    return result


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- manifest -----------------------------------------------------------


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int


@dataclass(frozen=True)
class Screenshot:
    id: str
    path: str | None
    viewport: Viewport
    status: str
    reference: str | None = None

    @property
    def captured(self) -> bool:
        return self.status == STATUS_CAPTURED


@dataclass(frozen=True)
class EvidenceManifest:
    screenshots: tuple[Screenshot, ...]
    version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "screenshots": [
                {
                    "id": shot.id,
                    "path": shot.path,
                    "viewport": {"width": shot.viewport.width, "height": shot.viewport.height},
                    "status": shot.status,
                    "reference": shot.reference,
                }
                for shot in self.screenshots
            ],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "EvidenceManifest":
        if not isinstance(payload, dict):
            raise VisualEvidenceError("an evidence manifest must be a JSON object")
        _exact_keys(payload, _MANIFEST_KEYS, "evidence manifest")
        version = payload["version"]
        if type(version) is not int or version != MANIFEST_VERSION:
            raise VisualEvidenceError(
                f"unsupported evidence manifest version {version!r}; expected {MANIFEST_VERSION}"
            )
        entries = payload["screenshots"]
        if not isinstance(entries, list) or not entries:
            raise VisualEvidenceError("'screenshots' must be a non-empty list")
        shots = tuple(_screenshot(entry, index) for index, entry in enumerate(entries))
        ids = [shot.id for shot in shots]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise VisualEvidenceError(f"duplicate screenshot id(s): {', '.join(duplicates)}")
        paths = [shot.path for shot in shots if shot.path is not None]
        if len(set(paths)) != len(paths):
            raise VisualEvidenceError("two screenshots name the same path")
        return cls(screenshots=shots, version=version)


def load_manifest(path: Path) -> EvidenceManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualEvidenceError(f"cannot read evidence manifest {path}: {exc}") from exc
    return EvidenceManifest.from_dict(payload)


def _exact_keys(payload: Mapping[str, Any], expected: frozenset[str], what: str) -> None:
    missing = sorted(expected - payload.keys())
    unknown = sorted(payload.keys() - expected)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(map(str, unknown))}")
        raise VisualEvidenceError(f"{what}: {'; '.join(parts)}")


def _screenshot(entry: Any, index: int) -> Screenshot:
    where = f"screenshot {index}"
    if not isinstance(entry, dict):
        raise VisualEvidenceError(f"{where} must be a JSON object")
    _exact_keys(entry, _SCREENSHOT_KEYS, where)
    shot_id = entry["id"]
    if not isinstance(shot_id, str) or not shot_id.strip() or shot_id != shot_id.strip():
        raise VisualEvidenceError(f"{where}: 'id' must be a non-empty string without surrounding space")
    where = f"screenshot {shot_id!r}"
    status = entry["status"]
    if status not in CAPTURE_STATUSES:
        raise VisualEvidenceError(f"{where}: 'status' must be one of {', '.join(CAPTURE_STATUSES)}")
    path = entry["path"]
    if status == STATUS_CAPTURED:
        path = _relative_path(path, f"{where} 'path'")
    elif path is not None:
        raise VisualEvidenceError(f"{where}: a failed screenshot has no 'path'")
    reference = entry["reference"]
    if reference is not None:
        reference = _relative_path(reference, f"{where} 'reference'")
    viewport = entry["viewport"]
    if not isinstance(viewport, dict):
        raise VisualEvidenceError(f"{where}: 'viewport' must be an object")
    _exact_keys(viewport, _VIEWPORT_KEYS, f"{where} viewport")
    for key in ("width", "height"):
        value = viewport[key]
        if type(value) is not int or not 0 < value <= MAX_VIEWPORT_DIMENSION:
            raise VisualEvidenceError(
                f"{where}: viewport {key} must be an integer in 1..{MAX_VIEWPORT_DIMENSION}"
            )
    return Screenshot(
        id=shot_id,
        path=path,
        viewport=Viewport(width=viewport["width"], height=viewport["height"]),
        status=status,
        reference=reference,
    )


def _relative_path(value: Any, what: str) -> str:
    """A normalised POSIX relative path that cannot leave its root by name."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise VisualEvidenceError(f"{what} must be a non-empty POSIX relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise VisualEvidenceError(f"{what} {value!r} must stay inside its root")
    if pure.suffix.lower() != ".png":
        raise VisualEvidenceError(f"{what} {value!r} must name a .png file")
    return pure.as_posix()


def resolve_inside(root: Path, relative: str) -> Path:
    """``root / relative``, refused if any symlink carries it outside ``root``."""

    base = root.resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise VisualEvidenceError(f"{relative!r} resolves outside {root}")
    return target


# -- candidate binding ---------------------------------------------------


@dataclass(frozen=True)
class EvidenceBinding:
    """The exact candidate a set of evidence files was captured for.

    ``siblings`` is ``((name, sha), ...)`` sorted by name; ``screenshots``
    and ``references`` are ``((relative path, sha256), ...)``. Written by
    the engine only.

    A binding is checked on construction, restored or not: its hashes must
    cover *exactly* the manifest's captured screenshots (in manifest order)
    and its distinct references (sorted), each with a well-formed digest.
    Otherwise a binding that omitted a file would verify vacuously -- there
    would be nothing left to compare against the candidate.
    """

    candidate_sha: str
    siblings: tuple[tuple[str, str], ...]
    manifest: EvidenceManifest
    screenshots: tuple[tuple[str, str], ...]
    references: tuple[tuple[str, str], ...]
    version: int = BINDING_VERSION

    def __post_init__(self) -> None:
        if self.version != BINDING_VERSION:
            raise VisualEvidenceError(f"unsupported evidence binding version {self.version!r}")
        if not isinstance(self.manifest, EvidenceManifest):
            raise VisualEvidenceError("an evidence binding needs its manifest")
        _check_candidate(self.candidate_sha, self.siblings)
        names = [name for name, _ in self.siblings]
        if names != sorted(set(names)):
            raise VisualEvidenceError("binding siblings must be unique and sorted by name")
        expected_shots = [s.path for s in self.manifest.screenshots if s.captured]
        expected_refs = sorted({s.reference for s in self.manifest.screenshots if s.reference})
        for what, recorded, expected in (
            ("screenshot", self.screenshots, expected_shots),
            ("reference", self.references, expected_refs),
        ):
            paths = [entry[0] for entry in recorded]
            if paths != expected:
                missing = sorted(set(expected) - set(paths))
                extra = sorted(set(paths) - set(expected))
                raise VisualEvidenceError(
                    f"binding {what} hashes do not match its manifest "
                    f"(missing: {', '.join(missing) or 'none'}; "
                    f"unexpected: {', '.join(extra) or 'none'}; or duplicated/reordered)"
                )
            for path, digest in recorded:
                if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                    raise VisualEvidenceError(f"binding {what} {path!r} has no valid sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate_sha": self.candidate_sha,
            "siblings": [{"name": n, "candidate_sha": s} for n, s in self.siblings],
            "manifest": self.manifest.to_dict(),
            "screenshots": [{"path": p, "sha256": h} for p, h in self.screenshots],
            "references": [{"path": p, "sha256": h} for p, h in self.references],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "EvidenceBinding":
        try:
            if payload["version"] != BINDING_VERSION:
                raise VisualEvidenceError(f"unsupported evidence binding version {payload['version']!r}")
            binding = cls(
                candidate_sha=payload["candidate_sha"],
                siblings=tuple((e["name"], e["candidate_sha"]) for e in payload["siblings"]),
                manifest=EvidenceManifest.from_dict(payload["manifest"]),
                screenshots=tuple((e["path"], e["sha256"]) for e in payload["screenshots"]),
                references=tuple((e["path"], e["sha256"]) for e in payload["references"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, VisualEvidenceError):
                raise
            raise VisualEvidenceError(f"malformed evidence binding: {exc!r}") from exc
        return binding


def _check_candidate(candidate_sha: str, siblings: Iterable[tuple[str, str]]) -> None:
    if not is_full_sha(candidate_sha):
        raise VisualEvidenceError(
            f"evidence must be bound to a full committed candidate SHA, got {candidate_sha!r}"
        )
    for name, sha in siblings:
        if not isinstance(name, str) or not name or not is_full_sha(sha):
            raise VisualEvidenceError(f"sibling {name!r} has no pinned candidate SHA ({sha!r})")


def bind_evidence(
    manifest: EvidenceManifest,
    *,
    evidence_root: Path,
    repo_root: Path,
    candidate_sha: str,
    siblings: Mapping[str, str] | None = None,
) -> EvidenceBinding:
    """Validate every file the manifest names and bind them to the candidate.

    Every captured screenshot and every reference must be a valid PNG inside
    its root (see :func:`read_png`, :func:`resolve_inside`).
    """

    pinned = tuple(sorted((siblings or {}).items()))
    _check_candidate(candidate_sha, pinned)
    screenshots: list[tuple[str, str]] = []
    references: dict[str, str] = {}
    for shot in manifest.screenshots:
        if shot.captured:
            assert shot.path is not None
            path = resolve_inside(evidence_root, shot.path)
            read_png(path)
            screenshots.append((shot.path, sha256_file(path)))
        if shot.reference is not None and shot.reference not in references:
            path = resolve_inside(repo_root, shot.reference)
            read_png(path)
            references[shot.reference] = sha256_file(path)
    return EvidenceBinding(
        candidate_sha=candidate_sha,
        siblings=pinned,
        manifest=manifest,
        screenshots=tuple(screenshots),
        references=tuple(sorted(references.items())),
    )


def verify_binding(
    binding: EvidenceBinding,
    *,
    evidence_root: Path,
    repo_root: Path,
    candidate_sha: str,
    siblings: Mapping[str, str] | None = None,
) -> None:
    """Refuse evidence that does not belong to *this* candidate as it is now."""

    pinned = tuple(sorted((siblings or {}).items()))
    if binding.candidate_sha != candidate_sha:
        raise VisualEvidenceError(
            f"evidence was captured for {binding.candidate_sha}, not candidate {candidate_sha}"
        )
    if binding.siblings != pinned:
        raise VisualEvidenceError("evidence was captured for different sibling candidates")
    for root, recorded in ((evidence_root, binding.screenshots), (repo_root, binding.references)):
        for relative, digest in recorded:
            path = resolve_inside(root, relative)
            read_png(path)
            if sha256_file(path) != digest:
                raise VisualEvidenceError(f"{relative} changed after it was bound to the candidate")


# -- image input capability ------------------------------------------------


def image_input_problem(adapter: object) -> str | None:
    """Why ``adapter`` cannot be trusted to inspect images, or ``None``.

    An adapter that does not declare ``supports_image_input = True`` is
    unsupported. One that does may still refuse for its configured model
    through an ``image_input_problem()`` method.
    """

    name = getattr(adapter, "provider_id", type(adapter).__name__)
    if getattr(adapter, "supports_image_input", False) is not True:
        return f"provider {name} cannot deliver images to its model"
    check = getattr(adapter, "image_input_problem", None)
    return check() if callable(check) else None


def require_image_input(adapter: object) -> None:
    """Raise :class:`ImageInputUnsupported` unless images can be inspected."""

    problem = image_input_problem(adapter)
    if problem is not None:
        raise ImageInputUnsupported(f"visual review is unavailable: {problem}")


__all__ = [
    "BINDING_VERSION",
    "CAPTURE_STATUSES",
    "EvidenceBinding",
    "EvidenceManifest",
    "ImageInputUnsupported",
    "MANIFEST_VERSION",
    "PngInfo",
    "STATUS_CAPTURED",
    "STATUS_FAILED",
    "Screenshot",
    "Viewport",
    "VisualEvidenceError",
    "bind_evidence",
    "image_input_problem",
    "load_manifest",
    "read_png",
    "require_image_input",
    "resolve_inside",
    "sha256_file",
    "verify_binding",
]
