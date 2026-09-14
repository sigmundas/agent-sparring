"""Manifest v1 parsing, digesting, and the manifest-driven plan run.

The manifest exists so a caller that owns the human plan document can hand
the engine an unambiguous execution list: explicit stage ids, labels like
``3A``/``3B``/``3C``, exact briefs, and an order. These tests hold that
contract, and hold the line that a manifest never becomes a second workflow
engine -- position, status, acceptance and sessions stay where they were.
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.manifest import (
    ManifestError,
    load_manifest_source,
    manifest_digest,
    parse_manifest,
)
from agent_sparring.plan import (
    PlanError,
    PlanRunState,
    PlanRunStatus,
    plan_state_path,
    resume_plan,
    start_plan,
)
from agent_sparring.stage import Stage, StageState, StageStatus
from test_plan import (  # noqa: E402  (shared scripted adapters and verdicts)
    NEEDS_YOU,
    READY,
    SEND_BACK,
    _SparringAdapter,
    _StageAdapter,
    _fixed,
    _run_git,
)

PLAN_LABEL = "docs/plans/active/reported-statistics.md"


def _stage_entry(stage_id: str, label: str, title: str, body: str, **extra) -> dict:
    entry = {
        "stage_id": stage_id,
        "label": label,
        "title": title,
        "brief": f"# Stage brief: {stage_id}\n\n{label} from plan `{PLAN_LABEL}`.\n\n{body}\n",
    }
    entry.update(extra)
    return entry


def manifest_payload(stages: list[dict] | None = None, *, source_digest: str = "sha256:abc") -> dict:
    return {
        "version": 1,
        "plan_label": PLAN_LABEL,
        "source_digest": source_digest,
        "stages": stages
        if stages is not None
        else [
            _stage_entry("stage-3c-cloud-schema", "Stage 3C", "Cloud schema", "Add the RPC."),
            _stage_entry("stage-3d-snapshot-v2", "Stage 3D", "Snapshot v2", "Emit version 2."),
        ],
    }


class ManifestParsingTests(unittest.TestCase):
    def test_parses_labels_that_are_not_numeric(self):
        manifest = parse_manifest(json.dumps(manifest_payload()))
        self.assertEqual([s.label for s in manifest.stages], ["Stage 3C", "Stage 3D"])
        self.assertEqual([s.position for s in manifest.stages], [1, 2])
        self.assertEqual(manifest.plan_label, PLAN_LABEL)

    def test_array_order_is_the_execution_order_not_the_label(self):
        # Labels that sort the other way round must not reorder anything: the
        # caller already decided the order, and that is the whole point.
        payload = manifest_payload(
            [
                _stage_entry("stage-9-later", "Stage 9", "Later", "later"),
                _stage_entry("stage-1-earlier", "Stage 1", "Earlier", "earlier"),
            ]
        )
        manifest = parse_manifest(json.dumps(payload))
        self.assertEqual([s.stage_id for s in manifest.stages], ["stage-9-later", "stage-1-earlier"])
        self.assertEqual([s.position for s in manifest.stages], [1, 2])

    def test_refuses_unsupported_version(self):
        payload = manifest_payload()
        payload["version"] = 2
        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(json.dumps(payload))
        self.assertIn("version", str(ctx.exception))

    def test_refuses_unknown_fields_rather_than_half_reading(self):
        payload = manifest_payload()
        payload["pause_between_stages"] = True
        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(json.dumps(payload))
        self.assertIn("pause_between_stages", str(ctx.exception))

    def test_refuses_an_unknown_stage_field(self):
        payload = manifest_payload([_stage_entry("stage-a", "Stage A", "A", "body", status="done")])
        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(json.dumps(payload))
        self.assertIn("status", str(ctx.exception))

    def test_refuses_empty_stages_missing_brief_and_duplicate_ids(self):
        with self.assertRaises(ManifestError):
            parse_manifest(json.dumps(manifest_payload([])))
        blank = _stage_entry("stage-a", "Stage A", "A", "body")
        blank["brief"] = "   "
        with self.assertRaises(ManifestError):
            parse_manifest(json.dumps(manifest_payload([blank])))
        duplicated = [
            _stage_entry("stage-a", "Stage A", "A", "one"),
            _stage_entry("stage-a", "Stage A again", "A", "two"),
        ]
        with self.assertRaises(ManifestError) as ctx:
            parse_manifest(json.dumps(manifest_payload(duplicated)))
        self.assertIn("unique", str(ctx.exception))

    def test_refuses_an_unsafe_stage_id(self):
        with self.assertRaises(ManifestError):
            parse_manifest(json.dumps(manifest_payload([_stage_entry("../escape", "S", "t", "b")])))

    def test_repositories_are_optional_and_validated(self):
        payload = manifest_payload(
            [
                _stage_entry(
                    "stage-3d",
                    "Stage 3D",
                    "Snapshot v2",
                    "body",
                    repositories=[
                        {
                            "name": "sporely-web",
                            "path": "../sporely-web-worktree",
                            "branch": "feature/cloud",
                            "candidate_sha": None,
                        }
                    ],
                )
            ]
        )
        manifest = parse_manifest(json.dumps(payload))
        self.assertEqual(manifest.stages[0].repositories[0].name, "sporely-web")
        payload["stages"][0]["repositories"][0].pop("branch")
        with self.assertRaises(ManifestError):
            parse_manifest(json.dumps(payload))

    def test_digest_covers_everything_that_would_run(self):
        base = manifest_digest(parse_manifest(json.dumps(manifest_payload())))
        for mutate in (
            lambda p: p["stages"][0].update(brief=p["stages"][0]["brief"] + "extra\n"),
            lambda p: p["stages"][0].update(title="Different"),
            lambda p: p["stages"][0].update(label="Stage 3X"),
            lambda p: p["stages"].reverse(),
            lambda p: p.update(source_digest="sha256:changed"),
            lambda p: p.update(plan_label="docs/other.md"),
        ):
            payload = manifest_payload()
            mutate(payload)
            self.assertNotEqual(manifest_digest(parse_manifest(json.dumps(payload))), base)
        # Re-emitting the identical manifest is byte-stable, so a caller that
        # regenerates it on every invocation does not invalidate its own run.
        self.assertEqual(manifest_digest(parse_manifest(json.dumps(manifest_payload()))), base)


class _ManifestRepoTestCase(unittest.TestCase):
    """A real repo with a real bare remote and the workflow directories
    git-ignored, driven from a manifest rather than a Markdown plan."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True)

        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")

        self.sparring_dir = self.repo / ".sparring"
        self.manifest_path = root / "manifest.json"
        self.write_manifest(manifest_payload())
        self.state_path = plan_state_path(self.sparring_dir, PLAN_LABEL)

    def write_manifest(self, payload: dict) -> None:
        self.manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def source(self):
        return load_manifest_source(self.manifest_path)

    def _start(self, stage_adapter, sparring_adapter, **kwargs):
        return start_plan(
            self.source(), self.sparring_dir, self.repo, _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x", **kwargs,
        )

    def _resume(self, stage_adapter, sparring_adapter, **kwargs):
        return resume_plan(
            self.source(), self.sparring_dir, self.repo, _fixed(stage_adapter, sparring_adapter),
            expected_branch="feature/x", **kwargs,
        )

    def _stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def _plan_state(self) -> PlanRunState:
        return PlanRunState.load(self.state_path)


class ManifestRunTests(_ManifestRepoTestCase):
    def test_runs_a_lettered_sequence_end_to_end(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        result = self._start(stage_adapter, _SparringAdapter([READY, READY]))

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(
            [stage_id for stage_id, _ in result.accepted],
            ["stage-3c-cloud-schema", "stage-3d-snapshot-v2"],
        )
        # The brief is the manifest's text, byte for byte.
        brief = self._stage("stage-3c-cloud-schema").read_brief()
        self.assertEqual(brief, manifest_payload()["stages"][0]["brief"])
        self.assertEqual(self._plan_state().source, "manifest")

    def test_run_state_is_keyed_by_the_plan_label_the_manifest_names(self):
        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY, READY]))
        self.assertTrue(self.state_path.is_file())
        self.assertEqual(self._plan_state().plan, PLAN_LABEL)

    def test_changed_manifest_refuses_resume(self):
        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([NEEDS_YOU]))
        payload = manifest_payload()
        payload["stages"][0]["brief"] += "\nand one more thing\n"
        self.write_manifest(payload)

        with self.assertRaises(PlanError) as ctx:
            self._resume(_StageAdapter(self.repo), _SparringAdapter([READY]))
        self.assertIn("executable content", str(ctx.exception))

    def test_changed_source_digest_alone_refuses_resume(self):
        # The plan document was edited, so the caller re-emitted a manifest
        # with a new source digest. Even if every brief happens to be
        # identical, that is not the content this run was started against.
        self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([NEEDS_YOU]))
        self.write_manifest(manifest_payload(source_digest="sha256:edited"))

        with self.assertRaises(PlanError):
            self._resume(_StageAdapter(self.repo), _SparringAdapter([READY]))

    def test_refuses_resuming_a_markdown_run_from_a_manifest(self):
        plan = self.repo / "docs" / "plans" / "active" / "reported-statistics.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("## Stage 1 — Cloud schema\n\nAdd the RPC.\n", encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "plan")
        _run_git(self.repo, "push", "-q", "origin", "feature/x")
        start_plan(
            plan, self.sparring_dir, self.repo,
            _fixed(_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU])),
            expected_branch="feature/x",
        )

        with self.assertRaises(PlanError) as ctx:
            self._resume(_StageAdapter(self.repo), _SparringAdapter([READY]))
        self.assertIn("markdown", str(ctx.exception))

    def test_needs_you_pauses_and_evidence_resumes_the_sparrer_then_advances(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        paused = self._start(stage_adapter, sparring_adapter)
        self.assertIs(paused.status, PlanRunStatus.PAUSED)

        result = self._resume(stage_adapter, sparring_adapter, evidence="Ran the device test: passed.")

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(stage_adapter.resume_calls, [])

    def test_evidence_then_send_back_runs_the_stage_agent(self):
        stage_adapter = _StageAdapter(self.repo, commit=True)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, SEND_BACK, READY, READY])
        self._start(stage_adapter, sparring_adapter)

        result = self._resume(stage_adapter, sparring_adapter, evidence="Checked; found a gap.")

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1"])

    def test_plan_state_must_be_ignored(self):
        (self.repo / ".gitignore").write_text(".sparring/stages/\n", encoding="utf-8")
        with self.assertRaises(PlanError) as ctx:
            self._start(_StageAdapter(self.repo), _SparringAdapter([READY, READY]))
        message = str(ctx.exception)
        self.assertIn("not ignored by git", message)
        self.assertIn(".sparring/plans/", message)
        self.assertIn("sparring check-config", message)


class ManifestAdoptionTests(_ManifestRepoTestCase):
    """Taking over a sequence that was driven stage by stage before it was
    ever managed -- the real migration case."""

    def _existing(self, stage_id: str, *, brief: str, state: StageState) -> Stage:
        stage = self._stage(stage_id).create(brief=brief)
        stage.write_state(state)
        return stage

    def test_refuses_existing_stages_without_adopt(self):
        self._existing(
            "stage-3c-cloud-schema",
            brief=manifest_payload()["stages"][0]["brief"],
            state=StageState(status=StageStatus.ACCEPTED, candidate_sha="a" * 40),
        )
        with self.assertRaises(PlanError) as ctx:
            self._start(_StageAdapter(self.repo), _SparringAdapter([READY]))
        self.assertIn("--adopt", str(ctx.exception))

    def test_adopts_an_accepted_stage_and_advances_past_it(self):
        # Its brief predates the manifest and does not match; that is fine,
        # acceptance is terminal and nothing will run for it.
        self._existing(
            "stage-3c-cloud-schema",
            brief="# an older, hand-written brief\n",
            state=StageState(status=StageStatus.ACCEPTED, candidate_sha="a" * 40),
        )
        reported: list[str] = []
        stage_adapter = _StageAdapter(self.repo, commit=True)

        result = self._start(
            stage_adapter, _SparringAdapter([READY]), adopt=True, report=reported.append
        )

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(result.accepted[0], ("stage-3c-cloud-schema", "a" * 40))
        # Only Stage 3D actually ran.
        self.assertEqual(len(stage_adapter.start_calls), 1)
        self.assertTrue(any("already ACCEPTED" in line for line in reported))

    def test_adopts_a_working_stage_whose_brief_matches_and_says_what_it_inherits(self):
        self._existing(
            "stage-3c-cloud-schema",
            brief=manifest_payload()["stages"][0]["brief"],
            state=StageState(
                status=StageStatus.WORKING,
                implementation_session_id="impl-old",
                sparring_session_id="spar-old",
            ),
        )
        reported: list[str] = []
        stage_adapter = _StageAdapter(self.repo, commit=True)

        result = self._start(
            stage_adapter, _SparringAdapter([READY, READY]), adopt=True, report=reported.append
        )

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        # The recorded sessions were continued, not replaced.
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-old"])
        inherited = [line for line in reported if "inheriting" in line]
        self.assertTrue(inherited)
        self.assertIn("impl-old", inherited[0])
        self.assertIn("spar-old", inherited[0])

    def test_refuses_a_working_stage_whose_brief_differs(self):
        self._existing(
            "stage-3c-cloud-schema",
            brief="# Stage brief: something else entirely\n",
            state=StageState(status=StageStatus.WORKING, implementation_session_id="impl-old"),
        )
        with self.assertRaises(PlanError) as ctx:
            self._start(_StageAdapter(self.repo), _SparringAdapter([READY]), adopt=True)
        message = str(ctx.exception)
        self.assertIn("stage-3c-cloud-schema", message)
        self.assertIn("differs", message)
        # Nothing was started: no run state was recorded.
        self.assertFalse(self.state_path.is_file())

    def test_refuses_an_accepted_stage_with_no_candidate(self):
        self._existing(
            "stage-3c-cloud-schema",
            brief=manifest_payload()["stages"][0]["brief"],
            state=StageState(status=StageStatus.ACCEPTED),
        )
        with self.assertRaises(PlanError) as ctx:
            self._start(_StageAdapter(self.repo), _SparringAdapter([READY]), adopt=True)
        self.assertIn("no candidate commit", str(ctx.exception))


class ManifestCliTests(_ManifestRepoTestCase):
    def _main(self, *argv: str, adapters=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        patch = (
            mock.patch("agent_sparring.cli._build_loop_adapters", return_value=adapters)
            if adapters is not None
            else contextlib.nullcontext()
        )
        with patch, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_run_plan_manifest_pauses_and_points_at_the_manifest_to_resume(self):
        adapters = (_StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU]))
        code, out, err = self._main(
            "run-plan", "--manifest", str(self.manifest_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", adapters=adapters,
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"plan paused: {PLAN_LABEL}", out)
        self.assertIn("action: NEEDS_YOU", out)
        self.assertIn(f"sparring resume-plan --manifest {self.manifest_path}", out)
        # The structured gate is printed as a check list, not as prose.
        self.assertIn("required before READY (DEVICE_MANUAL_CHECK)", out)
        self.assertIn("[android-device]", out)
        self.assertIn("pass when: It renders without a crash.", out)
        self.assertIn("stage 1/2 stage-3c-cloud-schema", err)

    def test_both_plan_inputs_at_once_is_refused(self):
        code, _, err = self._main(
            "run-plan", "some-plan.md", "--manifest", str(self.manifest_path),
            "--repo-root", str(self.repo), "--expected-branch", "feature/x",
        )
        self.assertEqual(code, 1)
        self.assertIn("exactly one plan input", err)

    def test_neither_plan_input_is_refused(self):
        code, _, err = self._main(
            "run-plan", "--repo-root", str(self.repo), "--expected-branch", "feature/x"
        )
        self.assertEqual(code, 1)
        self.assertIn("exactly one plan input", err)

    def test_adopt_flag_reaches_the_runner(self):
        stage = self._stage("stage-3c-cloud-schema").create(
            brief=manifest_payload()["stages"][0]["brief"]
        )
        stage.write_state(StageState(status=StageStatus.ACCEPTED, candidate_sha="a" * 40))
        adapters = (_StageAdapter(self.repo, commit=True), _SparringAdapter([READY]))

        code, out, err = self._main(
            "run-plan", "--manifest", str(self.manifest_path), "--adopt",
            "--repo-root", str(self.repo), "--expected-branch", "feature/x", adapters=adapters,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("adopting Stage 3C — Cloud schema", err)
        self.assertIn("plan complete", out)

    def test_check_config_reports_whether_workflow_state_is_ignored(self):
        (self.sparring_dir).mkdir(parents=True, exist_ok=True)
        (self.sparring_dir / "project.toml").write_text('project = "x"\n', encoding="utf-8")
        code, out, _ = self._main("check-config")
        self.assertEqual(code, 0)
        self.assertIn("stage artifacts git-ignored: yes", out)
        self.assertIn("plan-run state git-ignored: yes", out)

        (self.repo / ".gitignore").write_text(".sparring/stages/\n", encoding="utf-8")
        code, out, err = self._main("check-config")
        self.assertEqual(code, 1)
        self.assertIn("plan-run state git-ignored: NO", out)
        self.assertIn(".sparring/plans/", err)


if __name__ == "__main__":
    unittest.main()
