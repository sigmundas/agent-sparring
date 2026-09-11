import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.activity import (
    OPTIONAL_FIELDS,
    SCHEMA_VERSION,
    SUMMARY_MAX_CHARS,
    ActivityLog,
    emit,
    repo_relative_path,
    truncate,
)


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "activity.jsonl"

    def test_envelope_fields_and_schema_version(self):
        log = ActivityLog(self.path)
        log.emit("stage", "turn.started", resumed=False)

        (event,) = _events(self.path)
        self.assertEqual(event["v"], SCHEMA_VERSION)
        self.assertEqual(event["actor"], "stage")
        self.assertEqual(event["event"], "turn.started")
        self.assertIs(event["resumed"], False)
        # ts is UTC ISO 8601 with a Z suffix and millisecond precision.
        self.assertRegex(event["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_append_only_preserves_order_across_instances(self):
        ActivityLog(self.path).emit("loop", "loop.started")
        ActivityLog(self.path).emit("stage", "turn.started")
        ActivityLog(self.path).emit("sparrer", "sparring.started")

        names = [(e["actor"], e["event"]) for e in _events(self.path)]
        self.assertEqual(
            names,
            [("loop", "loop.started"), ("stage", "turn.started"), ("sparrer", "sparring.started")],
        )

    def test_unknown_fields_are_dropped_and_none_values_omitted(self):
        log = ActivityLog(self.path)
        log.emit(
            "stage",
            "file.changed",
            path="src/x.py",
            old_string="secret contents",  # not an allowed field
            command="rm -rf /",  # not an allowed field
            raw={"prompt": "..."},  # not an allowed field
            model=None,
        )

        (event,) = _events(self.path)
        self.assertEqual(event["path"], "src/x.py")
        for forbidden in ("old_string", "command", "raw", "model"):
            self.assertNotIn(forbidden, event)
        self.assertNotIn("secret contents", self.path.read_text(encoding="utf-8"))

    def test_optional_field_set_has_no_payload_or_command_fields(self):
        # The schema is closed on purpose: there is no field a call site
        # could use to carry command text, tool input/output, prompts, or
        # arbitrary provider payloads.
        for name in ("command", "args", "input", "output", "stdout", "stderr",
                     "prompt", "text", "raw", "payload", "data", "detail", "details"):
            self.assertNotIn(name, OPTIONAL_FIELDS)

    def test_summary_is_truncated(self):
        log = ActivityLog(self.path)
        log.emit("loop", "loop.stopped", summary="x" * (SUMMARY_MAX_CHARS * 3))

        (event,) = _events(self.path)
        self.assertLessEqual(len(event["summary"]), SUMMARY_MAX_CHARS)
        self.assertEqual(truncate("a  b\n c"), "a b c")

    def test_write_failure_never_raises_and_disables_the_instance(self):
        # A directory where the file should be: every write fails.
        self.path.mkdir()
        log = ActivityLog(self.path)

        log.emit("stage", "turn.started")  # must not raise
        self.assertTrue(log.disabled)
        log.emit("stage", "turn.finished")  # still must not raise
        self.assertEqual(list(self.path.iterdir()), [])

    def test_missing_parent_directory_never_raises(self):
        log = ActivityLog(Path(self._tmp.name) / "no" / "such" / "dir" / "activity.jsonl")
        log.emit("stage", "turn.started")
        self.assertTrue(log.disabled)

    def test_read_only_file_never_raises(self):
        if os.geteuid() == 0:  # pragma: no cover - root ignores file modes
            self.skipTest("running as root; file modes are not enforced")
        self.path.write_text("", encoding="utf-8")
        self.path.chmod(stat.S_IRUSR)
        self.addCleanup(lambda: self.path.chmod(stat.S_IRUSR | stat.S_IWUSR))

        log = ActivityLog(self.path)
        log.emit("stage", "turn.started")
        self.assertTrue(log.disabled)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_non_json_native_values_do_not_break_the_log(self):
        log = ActivityLog(self.path)
        log.emit("gate", "candidate.frozen", sha=Path("not-a-str-but-harmless"))
        (event,) = _events(self.path)
        self.assertEqual(event["sha"], "not-a-str-but-harmless")
        self.assertFalse(log.disabled)

    def test_bound_emitter_stamps_actor_and_provider(self):
        log = ActivityLog(self.path)
        emitter = log.bind("sparrer", provider="codex-cli")
        emitter.emit("session.observed", session_id="t-1")
        emitter.emit("tool.call", tool="shell", provider="override")

        first, second = _events(self.path)
        self.assertEqual((first["actor"], first["provider"], first["session_id"]),
                         ("sparrer", "codex-cli", "t-1"))
        self.assertEqual(second["provider"], "override")

    def test_module_level_emit_with_none_emitter_is_silent(self):
        emit(None, "tool.call", tool="Bash")  # must not raise
        self.assertFalse(self.path.exists())



class RepoRelativePathTests(unittest.TestCase):
    """Provider-reported file paths are persisted repo-relative with ``/``
    separators, or omitted (``None``) when they cannot be shown to lie
    inside the repository. Purely lexical: nothing here exists on disk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "project"
        self.repo.mkdir()

    def test_absolute_in_repo_path_becomes_relative(self):
        self.assertEqual(
            repo_relative_path(str(self.repo / "src" / "foo.py"), self.repo), "src/foo.py"
        )

    def test_already_relative_path_is_kept_and_normalized(self):
        self.assertEqual(repo_relative_path("src/foo.py", self.repo), "src/foo.py")
        self.assertEqual(repo_relative_path("./src/./foo.py", self.repo), "src/foo.py")
        self.assertEqual(repo_relative_path("src/sub/../foo.py", self.repo), "src/foo.py")

    def test_nested_path(self):
        self.assertEqual(
            repo_relative_path(str(self.repo / "a" / "b" / "c" / "d.txt"), self.repo),
            "a/b/c/d.txt",
        )

    def test_dotdot_escape_and_outside_repo_paths_are_omitted(self):
        self.assertIsNone(repo_relative_path("../secret.txt", self.repo))
        self.assertIsNone(repo_relative_path("src/../../secret.txt", self.repo))
        self.assertIsNone(repo_relative_path(str(Path(self._tmp.name) / "other" / "f"), self.repo))
        self.assertIsNone(repo_relative_path("/etc/passwd", self.repo))
        self.assertIsNone(repo_relative_path(str(Path.home() / ".ssh" / "id"), self.repo))
        # A sibling whose name merely starts with the repo name is outside.
        self.assertIsNone(repo_relative_path(str(self.repo) + "-other/f.py", self.repo))
        # The root itself is not a file path worth recording.
        self.assertIsNone(repo_relative_path(str(self.repo), self.repo))

    def test_relative_repo_root_is_resolved_against_cwd_lexically(self):
        cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, cwd)
        absolute = os.path.join(os.getcwd(), "project", "src", "foo.py")
        self.assertEqual(repo_relative_path(absolute, Path("project")), "src/foo.py")
        self.assertEqual(repo_relative_path(absolute, Path(".") / "project"), "src/foo.py")

    def test_symlinked_root_form_still_matches(self):
        # macOS reports /private/tmp/... for a root given as /tmp/...; only
        # the root directory's real path is consulted, never the target.
        link = Path(self._tmp.name) / "link"
        try:
            link.symlink_to(self.repo, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlinks unavailable")
        real_target = os.path.join(os.path.realpath(self.repo), "src", "foo.py")
        self.assertEqual(repo_relative_path(real_target, link), "src/foo.py")

    def test_windows_style_paths_normalize_with_forward_slashes(self):
        # Exercised lexically on every platform: a Windows-flavoured
        # provider path is matched against a Windows-flavoured root.
        from unittest import mock

        with mock.patch("agent_sparring.activity.os.path.abspath",
                        side_effect=lambda p: p), \
             mock.patch("agent_sparring.activity.os.path.realpath",
                        side_effect=lambda p: p):
            root = Path("C:\\work\\project")
            self.assertEqual(
                repo_relative_path("C:\\work\\project\\src\\foo.py", root), "src/foo.py"
            )
            self.assertEqual(
                repo_relative_path("c:\\Work\\Project\\src\\foo.py", root), "src/foo.py"
            )
            self.assertIsNone(repo_relative_path("D:\\other\\foo.py", root))
            self.assertIsNone(repo_relative_path("C:\\work\\foo.py", root))
        self.assertEqual(repo_relative_path("src\\sub\\foo.py", self.repo), "src/sub/foo.py")

    def test_garbage_is_omitted(self):
        for bad in (None, "", "   ", 42, ["x"]):
            self.assertIsNone(repo_relative_path(bad, self.repo))


if __name__ == "__main__":
    unittest.main()
