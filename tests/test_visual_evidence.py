import ast
import json
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401
from png_fixture import chunk, png_bytes, write_quadrants

from agent_sparring import visual_evidence as ve
from agent_sparring.providers import ImageInputUnsupported, ProviderError
from agent_sparring.providers import codex_cli
from agent_sparring.providers.claude_cli import ClaudeCliAdapter
from agent_sparring.providers.codex_cli import CatalogModel, CodexCliAdapter

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

ALL_RED = {q: "red" for q in ("top_left", "top_right", "bottom_left", "bottom_right")}


def _manifest(**overrides) -> dict:
    shot = {
        "id": "panel-desktop",
        "path": "panel-desktop.png",
        "viewport": {"width": 1280, "height": 800},
        "status": "captured",
        "reference": "mockups/panel.png",
    }
    shot.update(overrides)
    return {"version": 1, "screenshots": [shot]}


class _Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class ReadPngTests(_Tmp):
    def test_valid_png_reports_its_size(self):
        path = write_quadrants(self.root / "a.png", ALL_RED, size=16)
        self.assertEqual(ve.read_png(path), ve.PngInfo(width=16, height=16))

    def test_rejects_missing_non_png_truncated_and_corrupt_files(self):
        good = png_bytes(8, 8, lambda x, y: (1, 2, 3))
        cases = {
            "text.png": b"not an image",
            "truncated.png": good[:-20],
            "badcrc.png": good[:20] + bytes([good[20] ^ 0xFF]) + good[21:],
            "trailing.png": good + b"junk",
        }
        for name, data in cases.items():
            (self.root / name).write_bytes(data)
        for name in [*cases, "missing.png"]:
            with self.subTest(name), self.assertRaises(ve.VisualEvidenceError):
                ve.read_png(self.root / name)

    def test_rejects_image_data_shorter_than_its_header_declares(self):
        good = png_bytes(8, 8, lambda x, y: (1, 2, 3))
        # Same IDAT, but the header now claims 9 rows.
        import struct
        import zlib

        ihdr = struct.pack(">IIBBBBB", 8, 9, 8, 2, 0, 0, 0)
        chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
        (self.root / "lying.png").write_bytes(good[:8] + chunk + good[8 + 25 :])
        with self.assertRaisesRegex(ve.VisualEvidenceError, "declared size"):
            ve.read_png(self.root / "lying.png")

    def test_rejects_symlink(self):
        target = write_quadrants(self.root / "a.png", ALL_RED, size=8)
        (self.root / "link.png").symlink_to(target)
        with self.assertRaises(ve.VisualEvidenceError):
            ve.read_png(self.root / "link.png")


class ManifestTests(unittest.TestCase):
    def test_round_trip(self):
        manifest = ve.EvidenceManifest.from_dict(_manifest())
        self.assertEqual(manifest.version, ve.MANIFEST_VERSION)
        shot = manifest.screenshots[0]
        self.assertEqual(shot.viewport, ve.Viewport(1280, 800))
        self.assertTrue(shot.captured)
        self.assertEqual(manifest.to_dict(), _manifest())

    def test_failed_screenshot_has_no_path_and_is_not_captured(self):
        manifest = ve.EvidenceManifest.from_dict(_manifest(status="failed", path=None))
        self.assertFalse(manifest.screenshots[0].captured)
        with self.assertRaises(ve.VisualEvidenceError):
            ve.EvidenceManifest.from_dict(_manifest(status="failed"))

    def test_rejections(self):
        bad = {
            "version 2": {**_manifest(), "version": 2},
            "version bool": {**_manifest(), "version": True},
            "unknown top key": {**_manifest(), "extra": 1},
            "no screenshots": {"version": 1, "screenshots": []},
            "unknown status": _manifest(status="skipped"),
            "captured without path": _manifest(path=None),
            "absolute path": _manifest(path="/tmp/x.png"),
            "parent path": _manifest(path="../x.png"),
            "backslash": _manifest(path="a\\x.png"),
            "not png": _manifest(path="x.jpg"),
            "reference escape": _manifest(reference="../../etc/x.png"),
            "zero viewport": _manifest(viewport={"width": 0, "height": 800}),
            "float viewport": _manifest(viewport={"width": 1280.0, "height": 800}),
            "viewport extra": _manifest(viewport={"width": 1, "height": 1, "scale": 2}),
            "blank id": _manifest(id=" "),
            "unknown shot key": _manifest(note="x"),
        }
        for name, payload in bad.items():
            with self.subTest(name), self.assertRaises(ve.VisualEvidenceError):
                ve.EvidenceManifest.from_dict(payload)

    def test_duplicate_ids_rejected(self):
        payload = _manifest()
        payload["screenshots"].append(dict(payload["screenshots"][0], path="other.png"))
        with self.assertRaisesRegex(ve.VisualEvidenceError, "duplicate"):
            ve.EvidenceManifest.from_dict(payload)


class BindingTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.evidence = self.root / "evidence"
        self.repo = self.root / "repo"
        write_quadrants(self.evidence / "panel-desktop.png", ALL_RED, size=8)
        write_quadrants(self.repo / "mockups" / "panel.png", ALL_RED, size=8)
        self.manifest = ve.EvidenceManifest.from_dict(_manifest())

    def _bind(self, **kw):
        return ve.bind_evidence(
            self.manifest, evidence_root=self.evidence, repo_root=self.repo, **kw
        )

    def _verify(self, binding, **kw):
        ve.verify_binding(binding, evidence_root=self.evidence, repo_root=self.repo, **kw)

    def test_binding_round_trips_and_verifies_for_same_candidate(self):
        binding = self._bind(candidate_sha=SHA_A, siblings={"web": SHA_B})
        restored = ve.EvidenceBinding.from_dict(json.loads(json.dumps(binding.to_dict())))
        self.assertEqual(restored, binding)
        self._verify(restored, candidate_sha=SHA_A, siblings={"web": SHA_B})

    def test_requires_full_committed_candidate(self):
        for sha in ("abc123", "", "HEAD"):
            with self.subTest(sha), self.assertRaises(ve.VisualEvidenceError):
                self._bind(candidate_sha=sha)
        with self.assertRaises(ve.VisualEvidenceError):
            self._bind(candidate_sha=SHA_A, siblings={"web": "main"})

    def test_other_candidate_or_sibling_is_refused(self):
        binding = self._bind(candidate_sha=SHA_A, siblings={"web": SHA_B})
        with self.assertRaisesRegex(ve.VisualEvidenceError, "captured for"):
            self._verify(binding, candidate_sha=SHA_C, siblings={"web": SHA_B})
        with self.assertRaisesRegex(ve.VisualEvidenceError, "sibling"):
            self._verify(binding, candidate_sha=SHA_A, siblings={"web": SHA_C})
        with self.assertRaisesRegex(ve.VisualEvidenceError, "sibling"):
            self._verify(binding, candidate_sha=SHA_A)

    def test_changed_screenshot_or_reference_is_refused(self):
        binding = self._bind(candidate_sha=SHA_A)
        blue = dict(ALL_RED, top_left="blue")
        for path in (self.evidence / "panel-desktop.png", self.repo / "mockups" / "panel.png"):
            original = path.read_bytes()
            write_quadrants(path, blue, size=8)
            with self.subTest(path.name), self.assertRaisesRegex(ve.VisualEvidenceError, "changed"):
                self._verify(binding, candidate_sha=SHA_A)
            path.write_bytes(original)
        self._verify(binding, candidate_sha=SHA_A)

    def test_missing_corrupt_or_escaping_files_are_refused_at_bind(self):
        (self.evidence / "panel-desktop.png").write_bytes(b"not a png")
        with self.assertRaises(ve.VisualEvidenceError):
            self._bind(candidate_sha=SHA_A)
        (self.evidence / "panel-desktop.png").unlink()
        with self.assertRaises(ve.VisualEvidenceError):
            self._bind(candidate_sha=SHA_A)
        outside = write_quadrants(self.root / "outside.png", ALL_RED, size=8)
        (self.evidence / "panel-desktop.png").symlink_to(outside)
        with self.assertRaisesRegex(ve.VisualEvidenceError, "outside"):
            self._bind(candidate_sha=SHA_A)

    def test_failed_screenshot_is_bound_but_contributes_no_image(self):
        self.manifest = ve.EvidenceManifest.from_dict(_manifest(status="failed", path=None))
        binding = self._bind(candidate_sha=SHA_A)
        self.assertEqual(binding.screenshots, ())
        self.assertEqual(len(binding.references), 1)


class CapabilityTests(unittest.TestCase):
    def test_adapter_without_declaration_is_unsupported(self):
        with self.assertRaisesRegex(ImageInputUnsupported, "cannot deliver images"):
            ve.require_image_input(object())

    def test_claude_cli_is_explicitly_unsupported(self):
        adapter = ClaudeCliAdapter(repo_root=Path("."))
        self.assertIs(adapter.supports_image_input, False)
        with self.assertRaises(ImageInputUnsupported):
            ve.require_image_input(adapter)

    def test_unsupported_is_a_provider_error(self):
        self.assertTrue(issubclass(ImageInputUnsupported, ProviderError))


def _catalog(*models: CatalogModel):
    return mock.patch.object(codex_cli, "list_models", return_value=models)


def _model(name: str, modalities=("text", "image")) -> CatalogModel:
    return CatalogModel(name, None, ("medium",), "medium", tuple(modalities))


class CodexImageInputTests(_Tmp):
    def _runner(self):
        calls = []

        def runner(args, cwd, timeout_seconds, on_line=None):
            calls.append(args)
            Path(args[args.index("-o") + 1]).write_text('{"ok": true}', encoding="utf-8")
            stdout = json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")

        return runner, calls

    def test_model_image_support_comes_from_the_catalog(self):
        cases = {
            None: "no sparring model is pinned",
            "unlisted": "does not list 'unlisted'",
            "text-only": "does not list image input",
        }
        with _catalog(_model("text-only", ("text",)), _model("vision")):
            self.assertIsNone(CodexCliAdapter(repo_root=self.root, model="vision").image_input_problem())
            for model, reason in cases.items():
                adapter = CodexCliAdapter(repo_root=self.root, model=model)
                with self.subTest(model), self.assertRaisesRegex(ImageInputUnsupported, reason):
                    ve.require_image_input(adapter)

    def test_unreadable_catalog_is_unsupported(self):
        with mock.patch.object(codex_cli, "list_models", side_effect=ProviderError("boom")):
            problem = CodexCliAdapter(repo_root=self.root, model="vision").image_input_problem()
        self.assertIn("could not be read", problem)

    def test_images_are_validated_and_passed_on_start_resume_and_structured(self):
        a = write_quadrants(self.root / "a.png", ALL_RED, size=8)
        b = write_quadrants(self.root / "shots" / "b.png", ALL_RED, size=8)
        runner, calls = self._runner()
        adapter = CodexCliAdapter(repo_root=self.root, model="vision", runner=runner)
        with _catalog(_model("vision")):
            adapter.start("look", images=[a, Path("shots/b.png")])
            adapter.resume("t-1", "again", images=[b])
            adapter.start_structured("s", {"type": "object"}, images=[a])
        expected = [
            [f"--image={a.resolve()}", f"--image={b.resolve()}"],
            [f"--image={b.resolve()}"],
            [f"--image={a.resolve()}"],
        ]
        for args, images in zip(calls, expected):
            self.assertEqual(args[-1 - len(images) : -1], images)
            self.assertNotIn("-i", args)
        self.assertEqual(calls[1][:4], ["codex", "exec", "resume", "t-1"])

    def test_no_images_means_no_catalog_lookup_and_no_image_args(self):
        runner, calls = self._runner()
        with mock.patch.object(codex_cli, "list_models", side_effect=AssertionError("looked up")):
            CodexCliAdapter(repo_root=self.root, runner=runner).start("look")
        self.assertFalse(any(arg.startswith("--image") for arg in calls[0]))

    def test_bad_image_or_unsupported_model_fails_before_any_process(self):
        good = write_quadrants(self.root / "a.png", ALL_RED, size=8)
        (self.root / "bad.png").write_bytes(b"nope")
        runner, calls = self._runner()
        with _catalog(_model("vision"), _model("text-only", ("text",))):
            adapter = CodexCliAdapter(repo_root=self.root, model="vision", runner=runner)
            for image in (self.root / "bad.png", self.root / "missing.png"):
                with self.subTest(image.name), self.assertRaises(ImageInputUnsupported):
                    adapter.start("look", images=[good, image])
            text_only = CodexCliAdapter(repo_root=self.root, model="text-only", runner=runner)
            with self.assertRaises(ImageInputUnsupported):
                text_only.resume("t-1", "look", images=[good])
        self.assertEqual(calls, [])

    def test_catalog_parses_input_modalities(self):
        payload = {
            "models": [
                {"slug": "vision", "visibility": "list", "input_modalities": ["text", "image"]},
                {"slug": "plain", "visibility": "list"},
            ]
        }
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(codex_cli.subprocess, "run", return_value=done):
            models = {m.model: m for m in codex_cli.list_models()}
        self.assertEqual(models["vision"].input_modalities, ("text", "image"))
        self.assertEqual(models["plain"].input_modalities, ())


def _png(
    width=2,
    height=2,
    *,
    depth=8,
    colour=2,
    compression=0,
    filter_method=0,
    interlace=0,
    scanlines=None,
    before_idat=(),
    idat=None,
    after_idat=(),
):
    """Assemble a PNG chunk by chunk, so one field at a time can be wrong."""

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    row = (width * channels * depth + 7) // 8
    if scanlines is None:
        scanlines = b"".join(b"\x00" + bytes(row) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, compression, filter_method, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + b"".join(before_idat)
        + (idat if idat is not None else chunk(b"IDAT", zlib.compress(scanlines)))
        + b"".join(after_idat)
        + chunk(b"IEND", b"")
    )


def _unterminated(data: bytes) -> bytes:
    """All of ``data``, flushed but with no end-of-stream marker."""

    compressor = zlib.compressobj()
    return compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)


class PngDecoderConstraintTests(_Tmp):
    """Images a decoder would refuse must be refused here first: Codex drops
    an undecodable attachment silently (see providers/codex_cli.py)."""

    def _check(self, data: bytes):
        path = self.root / "x.png"
        path.write_bytes(data)
        return ve.read_png(path)

    def test_well_formed_variants_are_accepted(self):
        palette = chunk(b"PLTE", bytes(range(12)))
        # Adam7 2x2: pass 1 is the top-left pixel, pass 6 the top-right,
        # pass 7 the bottom row; passes 2-5 are empty and contribute nothing.
        adam7 = b"\x00" + bytes(3) + b"\x00" + bytes(3) + b"\x00" + bytes(6)
        accepted = {
            "rgb": _png(),
            "all filter types": _png(width=1, height=5, scanlines=b"".join(bytes([f, 0, 0, 0]) for f in range(5))),
            "palette": _png(colour=3, before_idat=(palette,)),
            "1-bit palette": _png(colour=3, depth=1, before_idat=(chunk(b"PLTE", bytes(6)),)),
            "interlaced": _png(interlace=1, scanlines=adam7),
            "split IDAT": _png(idat=chunk(b"IDAT", zlib.compress(bytes(14))[:5]) + chunk(b"IDAT", zlib.compress(bytes(14))[5:])),
            "ancillary chunk": _png(after_idat=(chunk(b"tEXt", b"k\x00v"),)),
            "rgb suggested palette": _png(before_idat=(palette,)),
        }
        for name, data in accepted.items():
            with self.subTest(name):
                self.assertEqual(self._check(data), ve.PngInfo(width=data[19], height=data[23]))

    def test_decoder_relevant_violations_are_refused(self):
        palette = chunk(b"PLTE", bytes(range(12)))
        rejected = {
            "scanline filter 5": _png(scanlines=b"\x05" + bytes(6) + b"\x00" + bytes(6)),
            "late scanline filter 9": _png(scanlines=b"\x00" + bytes(6) + b"\x09" + bytes(6)),
            "compression method 1": _png(compression=1),
            "filter method 1": _png(filter_method=1),
            "interlace method 2": _png(interlace=2),
            "palette without PLTE": _png(colour=3),
            "PLTE after IDAT": _png(colour=3, before_idat=(palette,), after_idat=(palette,)),
            "PLTE in greyscale": _png(colour=0, before_idat=(palette,)),
            "PLTE not a multiple of 3": _png(colour=3, before_idat=(chunk(b"PLTE", bytes(4)),)),
            "PLTE too long for 1-bit": _png(colour=3, depth=1, before_idat=(chunk(b"PLTE", bytes(9)),)),
            "two PLTE chunks": _png(colour=3, before_idat=(palette, palette)),
            "unknown critical chunk": _png(before_idat=(chunk(b"ABCD", b""),)),
            "invalid chunk type": _png(before_idat=(chunk(b"ab1d", b""),)),
            "non-consecutive IDAT": _png(after_idat=(chunk(b"tEXt", b"k\x00v"), chunk(b"IDAT", b""))),
            "trailing zlib data": _png(idat=chunk(b"IDAT", zlib.compress(bytes(14)) + b"xx")),
            "unterminated zlib stream": _png(idat=chunk(b"IDAT", _unterminated(bytes(14)))),
            "IEND with a body": _png()[:-12] + chunk(b"IEND", b"x"),
            "invalid bit depth": _png(depth=4),
            "absurd declared size": _png(width=0x7FFFFFFF, height=0x7FFFFFFF, scanlines=bytes(14)),
        }
        for name, data in rejected.items():
            with self.subTest(name), self.assertRaises(ve.VisualEvidenceError):
                self._check(data)

    def test_adapter_refuses_each_invalid_png_before_running_codex(self):
        rejected = {
            "filter5.png": _png(scanlines=b"\x05" + bytes(6) + b"\x00" + bytes(6)),
            "compression1.png": _png(compression=1),
            "interlace2.png": _png(interlace=2),
            "nopalette.png": _png(colour=3),
        }
        calls = []

        def runner(args, cwd, timeout_seconds, on_line=None):
            calls.append(args)
            raise AssertionError("codex must not run")

        good = write_quadrants(self.root / "good.png", ALL_RED, size=8)
        adapter = CodexCliAdapter(repo_root=self.root, model="vision", runner=runner)
        with _catalog(_model("vision")):
            for name, data in rejected.items():
                (self.root / name).write_bytes(data)
                for call in (
                    lambda: adapter.start("look", images=[good, self.root / name]),
                    lambda: adapter.resume("t-1", "look", images=[self.root / name]),
                    lambda: adapter.start_structured("s", {"type": "object"}, images=[self.root / name]),
                ):
                    with self.subTest(name), self.assertRaisesRegex(ImageInputUnsupported, "refusing to attach"):
                        call()
        self.assertEqual(calls, [])


class BindingCoverageTests(_Tmp):
    """A restored binding must hash exactly what its manifest names."""

    def setUp(self):
        super().setUp()
        self.evidence = self.root / "evidence"
        self.repo = self.root / "repo"
        payload = _manifest()
        payload["screenshots"].append(
            {
                "id": "panel-mobile",
                "path": "panel-mobile.png",
                "viewport": {"width": 375, "height": 812},
                "status": "captured",
                "reference": "mockups/panel-mobile.png",
            }
        )
        write_quadrants(self.evidence / "panel-desktop.png", ALL_RED, size=8)
        write_quadrants(self.evidence / "panel-mobile.png", ALL_RED, size=8)
        write_quadrants(self.repo / "mockups" / "panel.png", ALL_RED, size=8)
        write_quadrants(self.repo / "mockups" / "panel-mobile.png", ALL_RED, size=8)
        manifest = ve.EvidenceManifest.from_dict(payload)
        self.good = ve.bind_evidence(
            manifest, evidence_root=self.evidence, repo_root=self.repo, candidate_sha=SHA_A
        ).to_dict()

    def _restore(self, mutate):
        payload = json.loads(json.dumps(self.good))
        mutate(payload)
        return ve.EvidenceBinding.from_dict(payload)

    def test_the_sendback_reproduction_is_refused(self):
        def empty_hashes(p):
            p["screenshots"] = []
            p["references"] = []

        with self.assertRaisesRegex(ve.VisualEvidenceError, "do not match its manifest"):
            binding = self._restore(empty_hashes)
            # Were it restored, it must not verify against roots that do not exist.
            ve.verify_binding(
                binding,
                evidence_root=self.root / "nowhere",
                repo_root=self.root / "nowhere",
                candidate_sha=SHA_A,
            )

    def test_incomplete_extra_duplicate_or_reordered_hashes_are_refused(self):
        digest = "0" * 64

        def drop_first_shot(p):
            del p["screenshots"][0]

        def drop_reference(p):
            del p["references"][1]

        def extra_shot(p):
            p["screenshots"].append({"path": "extra.png", "sha256": digest})

        def duplicate_shot(p):
            p["screenshots"][1] = dict(p["screenshots"][0])

        def reorder_shots(p):
            p["screenshots"].reverse()

        def manifest_path_changed(p):
            p["manifest"]["screenshots"][0]["path"] = "elsewhere.png"

        def manifest_reference_changed(p):
            p["manifest"]["screenshots"][1]["reference"] = "mockups/other.png"

        def manifest_reference_dropped(p):
            p["manifest"]["screenshots"][1]["reference"] = None

        def bad_digest(p):
            p["screenshots"][0]["sha256"] = "not-a-digest"

        def uppercase_digest(p):
            p["references"][0]["sha256"] = p["references"][0]["sha256"].upper()

        def missing_digest(p):
            del p["references"][0]["sha256"]

        def unsorted_siblings(p):
            p["siblings"] = [{"name": "b", "candidate_sha": SHA_B}, {"name": "a", "candidate_sha": SHA_C}]

        def duplicate_siblings(p):
            p["siblings"] = [{"name": "a", "candidate_sha": SHA_B}, {"name": "a", "candidate_sha": SHA_C}]

        def short_candidate(p):
            p["candidate_sha"] = "abc123"

        def wrong_version(p):
            p["version"] = 2

        def siblings_not_objects(p):
            p["siblings"] = ["a"]

        for mutate in (
            drop_first_shot, drop_reference, extra_shot, duplicate_shot, reorder_shots,
            manifest_path_changed, manifest_reference_changed, manifest_reference_dropped,
            bad_digest, uppercase_digest, missing_digest, unsorted_siblings,
            duplicate_siblings, short_candidate, wrong_version, siblings_not_objects,
        ):
            with self.subTest(mutate.__name__), self.assertRaises(ve.VisualEvidenceError):
                self._restore(mutate)

    def test_direct_construction_is_checked_too(self):
        good = ve.EvidenceBinding.from_dict(self.good)
        with self.assertRaisesRegex(ve.VisualEvidenceError, "do not match its manifest"):
            ve.EvidenceBinding(
                candidate_sha=SHA_A,
                siblings=(),
                manifest=good.manifest,
                screenshots=(),
                references=good.references,
            )

    def test_complete_binding_still_round_trips_and_verifies(self):
        binding = ve.EvidenceBinding.from_dict(self.good)
        ve.verify_binding(binding, evidence_root=self.evidence, repo_root=self.repo, candidate_sha=SHA_A)
        self.assertEqual(binding.to_dict(), self.good)


class ContractIndependenceTests(unittest.TestCase):
    def test_contract_module_imports_no_capture_technology(self):
        source = Path(ve.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(
            imported,
            {"__future__", "hashlib", "json", "re", "struct", "zlib", "dataclasses", "pathlib", "typing", "agent_sparring"},
        )


if __name__ == "__main__":
    unittest.main()
