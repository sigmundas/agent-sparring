"""Live proof that the configured Codex reviewer inspects image pixels.

Opt-in: it launches the real ``codex`` CLI against the model your resolved
sparring configuration names, costs three model turns and needs network
access. Run it with::

    AGENT_SPARRING_LIVE_IMAGE_TEST=1 .venv/bin/python -m pytest -q tests/test_visual_evidence_live.py

What it proves, through the production :class:`CodexCliAdapter` and nothing
else:

- the colours are chosen at random when the test runs and exist only in the
  pixels; file names are random hex. Reporting them correctly cannot come
  from a file name, the prompt, or a previous run;
- the reviewer runs no command in any turn, so it did not decode the PNG
  bytes with a shell tool either -- it looked at the images;
- a deliberately mismatched candidate is reported as not matching
  (fresh session) and draws SEND_BACK (resumed session), while a candidate
  with identical pixels draws READY on the same resumed thread -- so images
  reach resumed turns too, and are compared with the earlier reference.
"""

import json
import os
import random
import secrets
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401
from png_fixture import COLOURS, QUADRANTS, write_quadrants

from agent_sparring import visual_evidence
from agent_sparring.agent_config import resolve_agent_configs
from agent_sparring.config import load_project_config
from agent_sparring.providers.codex_cli import PROVIDER_ID, CodexCliAdapter

ENABLE = "AGENT_SPARRING_LIVE_IMAGE_TEST"
REPO = Path(__file__).resolve().parent.parent

# tests/conftest.py isolates every test from the developer's HOME and
# preference file. This test exists to use the real ones -- the configured
# reviewer model, and the Codex login under ~/.codex -- so both are captured
# at import, before those fixtures run.
_REAL_HOME = os.environ.get("HOME")
_REAL_SPARRING = None
if os.environ.get(ENABLE) == "1":
    _sparring_dir = REPO / ".sparring"
    _REAL_SPARRING = resolve_agent_configs(
        load_project_config(_sparring_dir) if (_sparring_dir / "project.toml").is_file() else None
    ).sparring

_QUADRANT_SCHEMA = {
    "type": "object",
    "properties": {q: {"type": "string", "enum": sorted(COLOURS)} for q in QUADRANTS},
    "required": list(QUADRANTS),
    "additionalProperties": False,
}
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "first_image": _QUADRANT_SCHEMA,
        "second_image": _QUADRANT_SCHEMA,
        "images_match": {"type": "boolean"},
    },
    "required": ["first_image", "second_image", "images_match"],
    "additionalProperties": False,
}
_NO_TOOLS = (
    "Do not run any shell command and do not read any file: answer only from "
    "what you see in the attached image(s)."
)


def _commands_run(result) -> list[dict]:
    return [
        event
        for event in result.raw.get("events", ())
        if (event.get("item") or {}).get("type") == "command_execution"
    ]


@unittest.skipUnless(os.environ.get(ENABLE) == "1", f"set {ENABLE}=1 to run the live Codex image test")
class LiveCodexImageInspectionTest(unittest.TestCase):
    def setUp(self):
        self.config = _REAL_SPARRING
        if self.config.provider != PROVIDER_ID:
            self.skipTest(f"the configured sparrer is {self.config.provider}, not {PROVIDER_ID}")
        if shutil.which("codex") is None:
            self.fail("codex is not on PATH")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        if _REAL_HOME:
            patcher = mock.patch.dict(os.environ, {"HOME": _REAL_HOME})
            patcher.start()
            self.addCleanup(patcher.stop)

    def _image(self, colours: dict) -> Path:
        return write_quadrants(self.root / f"{secrets.token_hex(6)}.png", colours)

    def test_reviewer_sees_pixels_detects_mismatch_and_keeps_seeing_on_resume(self):
        rng = random.SystemRandom()
        names = list(COLOURS)
        rng.shuffle(names)
        reference = dict(zip(QUADRANTS, names))
        a, b = rng.sample(QUADRANTS, 2)
        mismatched = dict(reference, **{a: reference[b], b: reference[a]})

        adapter = CodexCliAdapter(
            repo_root=self.root,
            model=self.config.model,
            effort=self.config.effort,
            timeout_seconds=600,
        )
        visual_evidence.require_image_input(adapter)
        reference_png = self._image(reference)

        probe = adapter.start_structured(
            "You are checking a rendered screenshot against its reference. The "
            "first attached image is the REFERENCE, the second is a CANDIDATE. "
            "Each is split into four equal quadrants of solid colour. Report the "
            "colour of every quadrant of each image, and whether the candidate "
            f"matches the reference exactly. {_NO_TOOLS}",
            _PROBE_SCHEMA,
            images=[reference_png, self._image(mismatched)],
        )
        answer = json.loads(probe.text)
        self.assertEqual(answer["first_image"], reference, answer)
        self.assertEqual(answer["second_image"], mismatched, answer)
        self.assertIs(answer["images_match"], False, answer)
        self.assertEqual(_commands_run(probe), [])

        def review(colours: dict) -> str:
            result = adapter.resume(
                probe.session_id,
                "A new CANDIDATE screenshot is attached. Compare it with the "
                "REFERENCE image from your first turn in this conversation. "
                "Answer READY if every quadrant has the same colour as in the "
                "reference, otherwise SEND_BACK and name the differing "
                f"quadrants in findings. {_NO_TOOLS} Use null for every "
                "nullable field and an empty list for promote_deferred.",
                images=[self._image(colours)],
            )
            self.assertEqual(result.session_id, probe.session_id)
            self.assertEqual(_commands_run(result), [])
            return json.loads(result.text)["action"]

        self.assertEqual(review(dict(reference)), "READY")
        c, d = rng.sample(QUADRANTS, 2)
        self.assertEqual(review(dict(reference, **{c: reference[d], d: reference[c]})), "SEND_BACK")


if __name__ == "__main__":
    unittest.main()
