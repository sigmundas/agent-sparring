import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from sparring_v2.stage import (
    Stage,
    StageError,
    StageState,
    StageStatus,
    validate_stage_id,
)


class StageIdValidationTests(unittest.TestCase):
    def test_valid_ids_accepted(self):
        for stage_id in ["001", "stage-1", "stage_1.a", "A1"]:
            self.assertEqual(validate_stage_id(stage_id), stage_id)

    def test_path_traversal_rejected(self):
        for bad in ["..", ".", "../escape", "a/b", "a\\b", "", "/abs"]:
            with self.assertRaises(StageError):
                validate_stage_id(bad)


class StageResolveTests(unittest.TestCase):
    def test_resolve_rejects_traversal_before_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            with self.assertRaises(StageError):
                Stage.resolve(sparring_dir, "../outside")

    def test_resolve_gives_directory_under_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1")
            self.assertEqual(stage.directory, sparring_dir / "stages" / "stage-1")
            self.assertFalse(stage.exists())


class StageSkeletonTests(unittest.TestCase):
    def test_create_writes_state_and_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1").create()
            self.assertTrue(stage.exists())

            state = stage.read_state()
            self.assertEqual(state.stage_id, "stage-1")
            self.assertEqual(state.status, StageStatus.WORKING)
            self.assertIsNone(state.base_sha)
            self.assertIsNone(state.candidate_sha)

            for reader in (stage.read_brief, stage.read_notes, stage.read_handoff, stage.read_sparring):
                text = reader()
                self.assertIn("stage-1", text)

    def test_create_twice_without_exist_ok_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            Stage.resolve(sparring_dir, "stage-1").create()
            with self.assertRaises(StageError):
                Stage.resolve(sparring_dir, "stage-1").create()

    def test_create_twice_with_exist_ok_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1").create()
            stage.write_notes("custom notes")

            stage2 = Stage.resolve(sparring_dir, "stage-1").create(exist_ok=True)
            self.assertEqual(stage2.read_notes(), "custom notes")


class StageStateRoundTripTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1").create()

            state = StageState(
                stage_id="stage-1",
                status=StageStatus.ACCEPTANCE,
                implementation_session_id="impl-abc",
                sparring_session_id="spar-xyz",
                base_sha="deadbeef",
                candidate_sha="cafebabe",
            )
            stage.write_state(state)

            reloaded = stage.read_state()
            self.assertEqual(reloaded, state)

    def test_write_state_rejects_mismatched_stage_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1").create()
            with self.assertRaises(StageError):
                stage.write_state(StageState(stage_id="other-stage"))

    def test_read_state_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1")
            stage.directory.mkdir(parents=True)
            with self.assertRaises(StageError):
                stage.read_state()

    def test_read_state_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1")
            stage.directory.mkdir(parents=True)
            (stage.directory / "state.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(StageError):
                stage.read_state()

    def test_unknown_status_value_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1")
            stage.directory.mkdir(parents=True)
            (stage.directory / "state.json").write_text(
                '{"stage_id": "stage-1", "status": "changes_requested"}',
                encoding="utf-8",
            )
            with self.assertRaises(StageError):
                stage.read_state()

    def test_human_readable_artifacts_survive_state_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparring_dir = Path(tmp)
            stage = Stage.resolve(sparring_dir, "stage-1").create()
            stage.write_notes("important evidence")
            stage.write_handoff("handoff content")
            stage.write_sparring("sparring content")

            stage.write_state(StageState(stage_id="stage-1", status=StageStatus.ACCEPTED))

            self.assertEqual(stage.read_notes(), "important evidence")
            self.assertEqual(stage.read_handoff(), "handoff content")
            self.assertEqual(stage.read_sparring(), "sparring content")


if __name__ == "__main__":
    unittest.main()
