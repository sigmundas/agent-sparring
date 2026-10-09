"""Live proof that an image-aware sparring review catches a visual defect.

Opt-in, like ``tests/test_visual_evidence_live.py``: it launches the real
``codex`` CLI against the model your resolved sparring configuration names,
costs two model turns and needs network access. Run it with::

    AGENT_SPARRING_LIVE_IMAGE_TEST=1 .venv/bin/python -m pytest -q tests/test_visual_review_live.py

Unlike that test, which proves the adapter can carry pixels, this one goes
through the production review path end to end: the engine runs a capture
command, binds the evidence, and :func:`run_sparring_agent` attaches it to
an ordinary sparring turn with the production prompt -- brief, handoff,
visual-evidence section and verdict instructions. The handoff claims the
panel matches the mockup every time, so only the pixels can tell the turns
apart:

- a screenshot whose quadrants are swapped draws SEND_BACK, with findings
  that name the screenshot and the criterion it fails;
- after the capture is corrected, the *resumed* reviewer is shown the new
  capture and returns READY with no human gate and no deferred human
  verification -- correct evidence needs no manual layout check.

The colours are chosen at random when the test runs, so a verdict cannot
come from a file name, the prompt, or a previous run.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401
from png_fixture import COLOURS, QUADRANTS, write_quadrants

from agent_sparring.agent_config import resolve_agent_configs
from agent_sparring.config import VisualReviewConfig, load_project_config
from agent_sparring.providers.codex_cli import PROVIDER_ID, CodexCliAdapter
from agent_sparring.routing import RoutingAction
from agent_sparring.sparring_agent import run_sparring_agent
from agent_sparring.stage import Stage
from agent_sparring.visual_capture import run_capture
from agent_sparring.visual_review import VisualReviewRequest

ENABLE = "AGENT_SPARRING_LIVE_IMAGE_TEST"
REPO = Path(__file__).resolve().parent.parent

# Captured at import, before tests/conftest.py isolates HOME (see
# tests/test_visual_evidence_live.py for why).
_REAL_HOME = os.environ.get("HOME")
_REAL_SPARRING = None
if os.environ.get(ENABLE) == "1":
    _sparring_dir = REPO / ".sparring"
    _REAL_SPARRING = resolve_agent_configs(
        load_project_config(_sparring_dir) if (_sparring_dir / "project.toml").is_file() else None
    ).sparring

CAPTURE = textwrap.dedent(
    """
    import json, os, shutil, sys
    from pathlib import Path
    source = Path(sys.argv[1]).read_text().strip()
    out = Path(os.environ["SPARRING_EVIDENCE_DIR"])
    shutil.copy(source, out / "panel-desktop.png")
    Path(os.environ["SPARRING_EVIDENCE_MANIFEST"]).write_text(json.dumps({
        "version": 1,
        "screenshots": [{"id": "panel-desktop", "path": "panel-desktop.png",
                         "viewport": {"width": 1280, "height": 800},
                         "status": "captured", "reference": "docs/panel-mockup.png"}]}))
    """
)

BRIEF = """\
# Stage brief: panel

## Goal

Render the dashboard panel so that its four coloured regions sit exactly
where the mockup `docs/panel-mockup.png` puts them, at desktop width.

## Acceptance

- The rendered panel matches the mockup's arrangement of regions.

This stage's only deliverable is the rendered panel, and it is judged from
the captured screenshot; there is no source code or test suite to review.
"""

HANDOFF = """\
# Handoff: panel

## Claims

The panel is implemented and renders correctly. I checked the screenshot
myself and it matches the mockup exactly: every region is in the right
place. No visual issues remain.

## Test / build evidence

All tests pass.
"""

CRITERIA = (
    "Each of the four coloured regions occupies the same quadrant, with the same "
    "colour, as in the reference mockup.",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@unittest.skipUnless(os.environ.get(ENABLE) == "1", f"set {ENABLE}=1 to run the live visual review test")
class LiveVisualReviewTest(unittest.TestCase):
    def setUp(self):
        self.config = _REAL_SPARRING
        if self.config.provider != PROVIDER_ID:
            self.skipTest(f"the configured sparrer is {self.config.provider}, not {PROVIDER_ID}")
        if shutil.which("codex") is None:
            self.fail("codex is not on PATH")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text(".sparring/\n", encoding="utf-8")

        rng = random.SystemRandom()
        names = list(COLOURS)
        rng.shuffle(names)
        self.mockup = dict(zip(QUADRANTS, names))
        a, b = rng.sample(QUADRANTS, 2)
        self.defect = dict(self.mockup, **{a: self.mockup[b], b: self.mockup[a]})
        write_quadrants(self.repo / "docs" / "panel-mockup.png", self.mockup, size=256)
        (self.repo / "panel.txt").write_text("the panel\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "panel")
        _git(self.repo, "checkout", "-q", "-b", "feature/panel")

        self.tools = base / "tools"
        self.tools.mkdir()
        (self.tools / "capture.py").write_text(CAPTURE, encoding="utf-8")
        write_quadrants(self.tools / "good.png", self.mockup, size=256)
        write_quadrants(self.tools / "defect.png", self.defect, size=256)
        self.source = self.tools / "source.txt"

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "panel").create()
        (self.stage.directory / "brief.md").write_text(BRIEF, encoding="utf-8")
        (self.stage.directory / "handoff.md").write_text(HANDOFF, encoding="utf-8")

        if _REAL_HOME:
            patcher = mock.patch.dict(os.environ, {"HOME": _REAL_HOME})
            patcher.start()
            self.addCleanup(patcher.stop)

    def review(self, adapter, source: str):
        self.source.write_text(str(self.tools / source), encoding="utf-8")
        visual = VisualReviewConfig(
            command=(sys.executable, str(self.tools / "capture.py"), str(self.source)),
            criteria=CRITERIA,
        )
        capture = run_capture(self.repo, self.stage, visual)
        return run_sparring_agent(
            self.stage,
            self.sparring_dir,
            self.repo,
            adapter,
            expected_branch="feature/panel",
            visual=VisualReviewRequest(capture=capture, criteria=CRITERIA),
        )

    def test_defect_is_sent_back_and_corrected_evidence_is_ready(self):
        adapter = CodexCliAdapter(
            repo_root=self.repo,
            model=self.config.model,
            effort=self.config.effort,
            timeout_seconds=900,
        )

        first = self.review(adapter, "defect.png")
        payload = json.loads(first.result.text)
        self.assertEqual(first.routing.action, RoutingAction.SEND_BACK, payload)
        self.assertIn("panel-desktop", payload["findings"], payload)
        self.assertIn("V1", payload["findings"], payload)

        second = self.review(adapter, "good.png")
        payload = json.loads(second.result.text)
        self.assertTrue(second.resumed)
        self.assertEqual(second.result.session_id, first.result.session_id)
        self.assertEqual(second.routing.action, RoutingAction.READY, payload)
        self.assertIsNone(second.routing.human_gate, payload)
        self.assertIsNone(second.routing.deferred_human_gate, payload)


if __name__ == "__main__":
    unittest.main()
