"""``sparring new-stage``: the template brief by default, ``--brief-file``
for a caller-supplied brief, and no half-created stage when that file
cannot be read."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import templates
from agent_sparring.cli import main
from agent_sparring.stage import Stage, StageStatus


class NewStageBriefFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sparring_dir = self.root / ".sparring"

    def _new_stage(self, *extra: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["--sparring-dir", str(self.sparring_dir), "new-stage", "stage-1", *extra])
        return code, stderr.getvalue()

    def _stage(self) -> Stage:
        return Stage.resolve(self.sparring_dir, "stage-1")

    def test_without_brief_file_the_template_is_unchanged(self):
        code, _ = self._new_stage()
        self.assertEqual(code, 0)
        self.assertEqual(
            self._stage().read_brief(), templates.BRIEF_TEMPLATE.format(stage_id="stage-1")
        )

    def test_brief_file_becomes_brief_md_verbatim(self):
        content = "## Stage 3C — Cloud schema\n\nCloud side.\n\n- item\n"
        brief_file = self.root / "brief-3c.md"
        brief_file.write_text(content, encoding="utf-8")
        code, _ = self._new_stage("--brief-file", str(brief_file))
        self.assertEqual(code, 0)
        stage = self._stage()
        self.assertEqual(stage.read_brief(), content)
        # The rest of the skeleton is the normal one.
        self.assertEqual(stage.read_state().status, StageStatus.WORKING)
        self.assertIn("stage-1", stage.read_notes())
        self.assertIn("stage-1", stage.read_handoff())
        self.assertIn("stage-1", stage.read_sparring())

    def test_unicode_and_newlines_are_preserved_exactly(self):
        content = "# Stage 3C — Synkronisering «ø/æ/å» 🚀\r\n\r\nLinje 1\n\n\nLinje 2\ttab\n\n"
        brief_file = self.root / "brief.md"
        brief_file.write_bytes(content.encode("utf-8"))
        code, _ = self._new_stage("--brief-file", str(brief_file))
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.sparring_dir / "stages" / "stage-1" / "brief.md").read_bytes(),
            content.encode("utf-8"),
        )

    def test_nonexistent_brief_file_fails_cleanly_without_creating_the_stage(self):
        code, err = self._new_stage("--brief-file", str(self.root / "missing.md"))
        self.assertEqual(code, 1)
        self.assertIn("could not create stage: could not read --brief-file", err)
        self.assertIn("missing.md", err)
        self.assertFalse(self._stage().exists())
        self.assertFalse((self.sparring_dir / "stages").exists())

    def test_undecodable_brief_file_fails_cleanly_without_creating_the_stage(self):
        brief_file = self.root / "latin1.md"
        brief_file.write_bytes(b"caf\xe9\n")
        code, err = self._new_stage("--brief-file", str(brief_file))
        self.assertEqual(code, 1)
        self.assertIn("could not read --brief-file", err)
        self.assertFalse(self._stage().exists())

    @unittest.skipIf(os.geteuid() == 0, "root can read unreadable files")
    def test_unreadable_brief_file_fails_cleanly_without_creating_the_stage(self):
        brief_file = self.root / "locked.md"
        brief_file.write_text("secret\n", encoding="utf-8")
        brief_file.chmod(0)
        self.addCleanup(brief_file.chmod, stat.S_IRUSR | stat.S_IWUSR)
        code, err = self._new_stage("--brief-file", str(brief_file))
        self.assertEqual(code, 1)
        self.assertIn("could not read --brief-file", err)
        self.assertFalse(self._stage().exists())

    def test_existing_stage_collision_semantics_are_unchanged(self):
        brief_file = self.root / "brief.md"
        brief_file.write_text("first\n", encoding="utf-8")
        self.assertEqual(self._new_stage("--brief-file", str(brief_file))[0], 0)

        code, err = self._new_stage("--brief-file", str(brief_file))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        # --exist-ok still succeeds and never overwrites the brief that is there.
        brief_file.write_text("second\n", encoding="utf-8")
        code, _ = self._new_stage("--exist-ok", "--brief-file", str(brief_file))
        self.assertEqual(code, 0)
        self.assertEqual(self._stage().read_brief(), "first\n")

    def test_stage_create_writes_brief_only_when_absent(self):
        stage = self._stage().create(brief="planned\n")
        self.assertEqual(stage.read_brief(), "planned\n")
        stage.write_brief("edited by hand\n")
        Stage.resolve(self.sparring_dir, "stage-1").create(exist_ok=True, brief="planned again\n")
        self.assertEqual(stage.read_brief(), "edited by hand\n")


if __name__ == "__main__":
    unittest.main()
