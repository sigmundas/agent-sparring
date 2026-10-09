"""Image-aware sparrer review: delivery, instructions and loop behaviour.

The reviewer here is a fake, but one that can only reach a verdict by
looking at the image files it is handed: it compares each attached
screenshot's pixels with the attached reference the prompt pairs it with.
A defect that exists only in the pixels -- the handoff always claims the
UI matches the mockup -- therefore reaches a verdict only if the engine
delivered the right images, in the order its prompt describes.
"""

import contextlib
import io
import json
import re
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401
from png_fixture import write_quadrants
from test_visual_capture import VisualCaptureTestCase, _git

from agent_sparring.cli import main
from agent_sparring.config import ProjectConfigError, VisualReviewConfig, parse_project_config
from agent_sparring.loop import LoopError, run_unattended_loop
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_agent import SparringAgentRunError, run_sparring_agent
from agent_sparring.sparring_prompt import assemble_sparring_prompt
from agent_sparring.visual_capture import VisualCaptureError, evidence_root, run_capture
from agent_sparring.visual_review import (
    KIND_REFERENCE,
    KIND_SCREENSHOT,
    VisualReviewRequest,
    prepare_delivery,
    visual_evidence_section,
)

# Driven by a JSON spec the test (or the fake implementer) rewrites, so a
# "correction" changes what the next capture renders.
SPEC_CAPTURE = textwrap.dedent(
    """
    import json, os, shutil, sys
    from pathlib import Path

    spec = json.loads(Path(sys.argv[1]).read_text())
    out = Path(os.environ["SPARRING_EVIDENCE_DIR"])
    shots = []
    for entry in spec:
        path = None
        if entry["source"]:
            path = entry["id"] + ".png"
            shutil.copy(entry["source"], out / path)
        shots.append({
            "id": entry["id"], "path": path,
            "viewport": {"width": entry["width"], "height": 64},
            "status": "captured" if path else "failed",
            "reference": entry["reference"],
        })
    Path(os.environ["SPARRING_EVIDENCE_MANIFEST"]).write_text(
        json.dumps({"version": 1, "screenshots": shots}))
    """
)

MOCKUP = dict(top_left="red", top_right="blue", bottom_left="green", bottom_right="yellow")
# The right half swapped: same colours, wrong arrangement.
DEFECT = dict(top_left="red", top_right="yellow", bottom_left="green", bottom_right="blue")
CRITERIA = (
    "The panel's four regions keep the mockup's arrangement at every viewport.",
    "No text or chart is clipped.",
)

_SCREENSHOT_LINE = re.compile(
    r"Image (\d+): screenshot `([^`]+)` .*?compare with Image (\d+)"
)


def _gate(category: str = "UI_VISUAL_CHECK") -> dict:
    return {
        "category": category,
        "title": "Choose between two valid panel styles",
        "checks": [
            {
                "id": "panel-style",
                "instruction": "Open the spore panel and pick the preferred accent colour.",
                "pass_criteria": "A colour is chosen. Fail if neither is acceptable.",
                "source": None,
            }
        ],
    }


def _verdict(action: str, findings: str, **extra) -> str:
    payload = {
        "action": action,
        "summary": findings[:80],
        "needs_you_reason": None,
        "findings": findings,
        "deferred": None,
        "human_gate": None,
        "deferred_human_gate": None,
        "promote_deferred": [],
    }
    payload.update(extra)
    return json.dumps(payload)


class _ImageReviewer:
    """A reviewer that judges only from the pixels it is handed."""

    supports_image_input = True
    provider_id = "fake-image-reviewer"

    def __init__(self, *, verdict=None):
        self.calls = []
        self._verdict = verdict

    def start(self, prompt, *, images=()):
        return self._turn("start", None, prompt, images)

    def resume(self, session_id, prompt, *, images=()):
        return self._turn("resume", session_id, prompt, images)

    def _turn(self, kind, session_id, prompt, images):
        images = [Path(p) for p in images]
        self.calls.append(
            {
                "kind": kind,
                "session_id": session_id,
                "prompt": prompt,
                "images": images,
                "bytes": [p.read_bytes() for p in images],
            }
        )
        text = self._verdict if self._verdict is not None else self._judge(prompt, images)
        self.calls[-1]["verdict"] = json.loads(text)
        return SparringAgentResult(session_id="spar-1", text=text, is_error=False)

    @staticmethod
    def _judge(prompt, images):
        defects = []
        for number, shot_id, reference in _SCREENSHOT_LINE.findall(prompt):
            if images[int(number) - 1].read_bytes() != images[int(reference) - 1].read_bytes():
                defects.append(
                    f"Image {number}, screenshot `{shot_id}`: fails V1 -- the right-hand "
                    f"regions are swapped relative to Image {reference}."
                )
        if defects:
            return _verdict("SEND_BACK", " ".join(defects))
        return _verdict("READY", "Every screenshot matches its reference.")


class _FixingImplementer:
    """Claims success every turn; corrects the capture spec when sent back."""

    def __init__(self, fix=None):
        self.prompts = []
        self._fix = fix

    def start(self, prompt):
        self.prompts.append(prompt)
        return StageAgentResult(
            session_id="impl-1", text="The panel matches the mockup exactly.", is_error=False
        )

    def resume(self, session_id, prompt):
        self.prompts.append(prompt)
        if self._fix is not None:
            self._fix()
        return StageAgentResult(
            session_id="impl-1", text="Fixed; it matches the mockup now.", is_error=False
        )


class VisualReviewTestCase(VisualCaptureTestCase):
    def setUp(self):
        super().setUp()
        (self.tools / "spec_capture.py").write_text(SPEC_CAPTURE, encoding="utf-8")
        write_quadrants(self.tools / "defect.png", DEFECT, size=64)
        self.spec = self.tools / "spec.json"
        self.write_spec(desktop="good.png")

    def write_spec(self, *, desktop="good.png", mobile="good.png", extra=()):
        entries = [
            {"id": "panel-desktop", "source": str(self.tools / desktop), "width": 1280,
             "reference": "docs/ref.png"},
            {"id": "panel-mobile", "source": str(self.tools / mobile) if mobile else None,
             "width": 375, "reference": "docs/ref.png"},
            *extra,
        ]
        self.spec.write_text(json.dumps(entries), encoding="utf-8")

    def visual(self, criteria=CRITERIA) -> VisualReviewConfig:
        return VisualReviewConfig(
            command=(sys.executable, str(self.tools / "spec_capture.py"), str(self.spec)),
            timeout_seconds=60,
            criteria=criteria,
        )

    def request(self, criteria=CRITERIA) -> VisualReviewRequest:
        capture = run_capture(self.repo, self.stage, self.visual(criteria))
        return VisualReviewRequest(capture=capture, criteria=criteria)


class DeliveryTests(VisualReviewTestCase):
    def test_screenshots_are_attached_with_their_reference_once_in_order(self):
        self.write_spec(
            mobile=None,
            extra=[{"id": "legend", "source": str(self.tools / "good.png"), "width": 640,
                    "reference": None}],
        )
        request = self.request()
        delivery = prepare_delivery(self.repo, self.stage, request)
        directory = request.capture.directory(self.stage).resolve()

        self.assertEqual(
            [(a.number, a.kind, a.name) for a in delivery.attachments],
            [
                (1, KIND_SCREENSHOT, "panel-desktop"),
                (2, KIND_REFERENCE, "docs/ref.png"),
                (3, KIND_SCREENSHOT, "legend"),
            ],
        )
        self.assertEqual(
            delivery.images,
            (
                directory / "panel-desktop.png",
                (self.repo / "docs/ref.png").resolve(),
                directory / "legend.png",
            ),
        )

        text = visual_evidence_section(delivery, self.stage).text
        self.assertIn(
            "Image 1: screenshot `panel-desktop` at viewport 1280x64 "
            f"(`stages/stage-1/visual-evidence/{request.capture.capture_id}/panel-desktop.png`); "
            "compare with Image 2 (reference `docs/ref.png`).",
            text,
        )
        self.assertIn("Screenshot `panel-mobile` (375x64) -- capture FAILED", text)
        self.assertIn("Image 3: screenshot `legend` at viewport 640x64", text)
        self.assertIn("no reference image", text)
        self.assertIn(
            "Image 2: reference mockup `docs/ref.png` (for `panel-desktop`, `panel-mobile`).", text
        )

    def test_section_carries_criteria_and_review_instructions(self):
        text = visual_evidence_section(
            prepare_delivery(self.repo, self.stage, self.request()), self.stage
        ).text
        text = " ".join(text.split())
        self.assertIn(f"- `V1` -- {CRITERIA[0]}", text)
        self.assertIn(f"- `V2` -- {CRITERIA[1]}", text)
        for aspect in ("layout", "proportions", "clipping and overflow", "responsive behaviour",
                       "legibility", "visual hierarchy"):
            self.assertIn(f"**{aspect}**", text)
        self.assertIn("permitted variations and are not findings", text)
        self.assertIn("structural mismatches and are findings", text)
        self.assertIn("name the screenshot by id", text)
        self.assertIn("is a claim, not evidence", text)
        self.assertIn("need no manual layout check", text)
        self.assertIn("subjective product and aesthetic", text)
        self.assertIn("replace every image attached to an earlier turn", text)

    def test_without_criteria_the_brief_and_references_are_the_standard(self):
        text = visual_evidence_section(
            prepare_delivery(self.repo, self.stage, self.request(criteria=())), self.stage
        ).text
        self.assertIn("No written visual criteria are configured", text)
        self.assertNotIn("`V1`", text)

    def test_a_superseded_capture_is_refused(self):
        old = self.request()
        self.request()
        with self.assertRaisesRegex(VisualCaptureError, "no current visual evidence|not the capture"):
            prepare_delivery(self.repo, self.stage, old)

    def test_altered_or_stale_evidence_is_refused(self):
        request = self.request()
        write_quadrants(request.capture.directory(self.stage) / "panel-desktop.png", DEFECT, size=64)
        with self.assertRaisesRegex(VisualCaptureError, "changed after it was bound"):
            prepare_delivery(self.repo, self.stage, request)

        request = self.request()
        (self.repo / "f.txt").write_text("a new candidate\n", encoding="utf-8")
        with self.assertRaisesRegex(VisualCaptureError, "stale"):
            prepare_delivery(self.repo, self.stage, request)

    def test_a_prompt_without_visual_review_has_no_visual_section(self):
        prompt = assemble_sparring_prompt(self.stage, self.sparring_dir, resume=False)
        self.assertNotIn("Visual evidence", [s.heading for s in prompt.sections])


class SparringTurnTests(VisualReviewTestCase):
    def spar(self, adapter, request):
        return run_sparring_agent(
            self.stage, self.sparring_dir, self.repo, adapter,
            expected_branch="feature/x", visual=request,
        )

    def test_images_reach_a_fresh_and_a_resumed_turn(self):
        reviewer = _ImageReviewer()
        first = self.spar(reviewer, self.request())
        self.assertEqual(first.routing.action, RoutingAction.READY)
        second = self.spar(reviewer, self.request())
        self.assertTrue(second.resumed)

        start, resume = reviewer.calls
        self.assertEqual((start["kind"], resume["kind"]), ("start", "resume"))
        self.assertEqual(resume["session_id"], "spar-1")
        for call in reviewer.calls:
            self.assertEqual(len(call["images"]), 3)  # two screenshots, one shared reference
            self.assertIn("## Visual evidence", call["prompt"])
        self.assertNotEqual(start["images"][0].parent, resume["images"][0].parent)

    def test_an_adapter_that_cannot_show_images_is_refused_before_anything(self):
        class Blind(_ImageReviewer):
            supports_image_input = False

        reviewer = Blind()
        request = self.request()
        before = self.stage.read_state()
        with self.assertRaisesRegex(SparringAgentRunError, "cannot review .* visually"):
            self.spar(reviewer, request)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.stage.read_state(), before)

    def test_altered_evidence_never_reaches_the_reviewer(self):
        reviewer = _ImageReviewer()
        request = self.request()
        write_quadrants(request.capture.directory(self.stage) / "panel-mobile.png", DEFECT, size=64)
        with self.assertRaisesRegex(SparringAgentRunError, "without its current visual evidence"):
            self.spar(reviewer, request)
        self.assertEqual(reviewer.calls, [])
        self.assertIsNone(self.stage.read_state().sparring_session_id)

    def test_an_independent_review_cannot_take_visual_evidence(self):
        with self.assertRaisesRegex(SparringAgentRunError, "independent review"):
            run_sparring_agent(
                self.stage, self.sparring_dir, self.repo, _ImageReviewer(),
                expected_branch="feature/x", visual=self.request(),
                review_candidate_set="a set",
            )


class LoopVisualReviewTests(VisualReviewTestCase):
    def loop(self, implementer, reviewer, visual):
        return run_unattended_loop(
            self.stage, self.sparring_dir, self.repo, implementer, reviewer,
            expected_branch="feature/x", visual_review=visual,
        )

    def test_a_defective_screenshot_is_sent_back_and_the_correction_recaptured(self):
        self.write_spec(desktop="defect.png")
        reviewer = _ImageReviewer()
        implementer = _FixingImplementer(fix=lambda: self.write_spec(desktop="good.png"))

        result = self.loop(implementer, reviewer, self.visual())

        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(result.send_back_count, 1)
        first, second = reviewer.calls
        # The finding names the failing screenshot and criterion, reaches
        # the implementer, and is shown to the same reviewer session again.
        finding = "Image 1, screenshot `panel-desktop`: fails V1"
        self.assertEqual(first["verdict"]["action"], "SEND_BACK")
        self.assertIn(finding, first["verdict"]["findings"])
        self.assertIn(finding, implementer.prompts[1])
        self.assertEqual((second["kind"], second["session_id"]), ("resume", "spar-1"))
        self.assertIn(finding, second["prompt"])
        # The resumed review saw the new capture only: different files, the
        # corrected pixels, and the first capture already gone.
        self.assertNotEqual(first["images"][0].parent, second["images"][0].parent)
        self.assertFalse(first["images"][0].parent.exists())
        self.assertEqual(second["bytes"][0], (self.tools / "good.png").read_bytes())
        self.assertNotEqual(first["bytes"][0], second["bytes"][0])
        capture_ids = re.findall(r"capture `(capture-[^`]+)`", first["prompt"] + second["prompt"])
        self.assertEqual(len(set(capture_ids)), 2)

    def test_correct_evidence_is_ready_without_manual_checks(self):
        reviewer = _ImageReviewer()
        result = self.loop(_FixingImplementer(), reviewer, self.visual())
        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(result.send_back_count, 0)
        self.assertIsNone(result.routing.human_gate)
        self.assertIsNone(result.routing.deferred_human_gate)
        self.assertEqual(len(reviewer.calls), 1)

    def test_a_reviewer_that_cannot_see_images_stops_the_loop_before_any_turn(self):
        class Blind(_ImageReviewer):
            supports_image_input = False

        implementer = _FixingImplementer()
        with self.assertRaisesRegex(LoopError, "cannot be shown images"):
            self.loop(implementer, Blind(), self.visual())
        self.assertEqual(implementer.prompts, [])
        self.assertFalse(evidence_root(self.stage).exists())

    def test_a_subjective_choice_still_stops_for_a_person(self):
        reviewer = _ImageReviewer(
            verdict=_verdict(
                "NEEDS_YOU",
                "Layout matches; the accent colour is a product choice.",
                needs_you_reason="PRODUCT/PREFERENCE -- choose the accent colour",
                human_gate=_gate("PRODUCT_PREFERENCE"),
            )
        )
        result = self.loop(_FixingImplementer(), reviewer, self.visual())
        self.assertEqual(result.outcome, RoutingAction.NEEDS_YOU)
        self.assertEqual(result.routing.human_gate.category, "PRODUCT_PREFERENCE")
        self.assertEqual(len(reviewer.calls[0]["images"]), 3)

    def test_deferred_human_verification_is_unchanged(self):
        deferred = dict(
            _gate(),
            rationale="Later stages do not depend on the accent colour.",
            checkpoint="before_plan_completion",
        )
        reviewer = _ImageReviewer(
            verdict=_verdict("READY", "Objective checks pass.", deferred_human_gate=deferred)
        )
        result = self.loop(_FixingImplementer(), reviewer, self.visual())
        self.assertEqual(result.outcome, RoutingAction.READY)
        self.assertEqual(result.routing.deferred_human_gate.category, "UI_VISUAL_CHECK")


class RunSparringCommandTests(VisualReviewTestCase):
    def setUp(self):
        super().setUp()
        command = json.dumps(list(self.visual().command))
        (self.sparring_dir / "project.toml").write_text(
            'project = "p"\n\n[agents.sparring]\nprovider = "codex-cli"\n\n'
            f"[visual_review]\nenabled = true\ncommand = {command}\n"
            'criteria = ["Nothing is clipped."]\n',
            encoding="utf-8",
        )
        (self.repo / ".git" / "info" / "exclude").write_text(
            ".sparring/project.toml\n", encoding="utf-8"
        )

    def run_sparring(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--sparring-dir", str(self.sparring_dir), "run-sparring", "stage-1",
                    "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                    "--codex-executable", "/nonexistent/codex",
                ]
            )
        return code, stderr.getvalue()

    def test_a_standalone_review_is_shown_a_fresh_capture(self):
        reviewer = _ImageReviewer()
        with mock.patch("agent_sparring.cli.CodexCliAdapter", return_value=reviewer):
            code, stderr = self.run_sparring()
        self.assertEqual(code, 0, stderr)
        (call,) = reviewer.calls
        self.assertEqual(len(call["images"]), 3)
        self.assertIn("- `V1` -- Nothing is clipped.", call["prompt"])

    def test_a_standalone_review_refuses_a_reviewer_that_cannot_see(self):
        code, stderr = self.run_sparring()
        self.assertEqual(code, 1)
        self.assertIn("visual review is unavailable", stderr)
        self.assertFalse(evidence_root(self.stage).exists())


class CriteriaConfigTests(unittest.TestCase):
    def parse(self, table: str):
        return parse_project_config('project = "p"\n' + table).visual_review

    def test_criteria_are_parsed_in_order(self):
        config = self.parse(
            '[visual_review]\nenabled = true\ncommand = ["x"]\n'
            'criteria = ["  No clipping. ", "Legend under the chart."]\n'
        )
        self.assertEqual(config.criteria, ("No clipping.", "Legend under the chart."))
        self.assertEqual(self.parse('[visual_review]\nenabled = true\ncommand = ["x"]\n').criteria, ())

    def test_malformed_criteria_are_refused(self):
        for value in ('"No clipping."', '[""]', "[1]", '["ok", "  "]'):
            with self.subTest(value=value):
                with self.assertRaises(ProjectConfigError):
                    self.parse(f'[visual_review]\nenabled = true\ncommand = ["x"]\ncriteria = {value}\n')


if __name__ == "__main__":
    unittest.main()
