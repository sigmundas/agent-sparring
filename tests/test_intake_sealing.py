"""Plan-intake Stage A: an approval binds what executes.

Every test here drives the real prepare -> approve -> run-plan path with
scripted providers. The invariant under test: after a person approves a run
slice, changing the proposal, the manifest, the source plan or the
repositories either has no effect on what runs or makes the run refuse --
and a refusal happens before any provider is invoked.

The trust model is accidental drift, not a hostile agent (see
:mod:`agent_sparring.intake_approval`): nothing here rewrites the approval
record itself to forge a consistent one.
"""

import contextlib
import io
import json
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.intake import IntakeError, approve_plan
from agent_sparring.intake_approval import SOURCE_KIND, write_exclusive
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    load_plan_source,
    resume_plan,
    run_state_path,
    start_plan,
)
from agent_sparring.providers import ProviderError, StageAgentResult
from agent_sparring.stage import Stage, StageStatus
from test_intake import PLAN, _git, _Repo, good_interpretation
from test_plan import READY, _SparringAdapter

APP_BRANCH = "feature/widgets"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class _Committer:
    """A stage agent that commits and pushes one file per turn on ``branch``,
    optionally doing something else first (the ``during`` hook)."""

    def __init__(self, repo: Path, branch: str, *, during=None, fail_at=None):
        self.repo, self.branch, self.during, self.fail_at = repo, branch, during, fail_at
        self.calls = 0

    def _turn(self, session_id):
        self.calls += 1
        if self.fail_at == self.calls:
            raise ProviderError("stage provider boom")
        if self.during:
            self.during()
        path = self.repo / f"impl-{uuid.uuid4().hex[:8]}.txt"
        path.write_text("work\n", encoding="utf-8")
        _git(self.repo, "add", path.name)
        _git(self.repo, "commit", "-q", "-m", f"turn {self.calls}")
        _git(self.repo, "push", "-q", "origin", self.branch)
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt):
        return self._turn(f"impl-{self.calls + 1}")

    def resume(self, session_id, prompt):
        return self._turn(session_id)


class _Forbidden:
    """An adapter factory that fails the test if a provider is ever needed."""

    def __init__(self, test):
        self.test = test

    def __call__(self, stage):
        self.test.fail(f"a provider was requested for {stage.stage_id} after a refusal was due")


class _Sealed(_Repo):
    def setUp(self):
        super().setUp()
        for repo, branch in ((self.repo, APP_BRANCH), (self.web, "feature/web")):
            remote = self.root / f"{repo.name}.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-q", "-u", "origin", branch)

    def approve(self, intake_dir, run="app", **kwargs):
        repo = self.repo if run == "app" else self.web
        kwargs.setdefault("primary_repository", run)
        return approve_plan(
            intake_dir, run_id=run, repo_root=repo, sparring_dir=repo / ".sparring", **kwargs
        )

    def prepared_and_approved(self):
        intake = self.prepare()
        return intake, self.approve(intake.directory)

    def source(self, approval, repo=None):
        return load_plan_source(approval.manifest_path, repo or self.repo, manifest=True)

    def start(self, approval, adapters, *, repo=None, branch=APP_BRANCH, **kwargs):
        repo = repo or self.repo
        return start_plan(
            self.source(approval, repo), repo / ".sparring", repo, adapters, expected_branch=branch, **kwargs
        )

    def run_app(self, approval, **committer):
        stage = _Committer(self.repo, APP_BRANCH, **committer)
        sparrer = _SparringAdapter([READY] * 4)
        return self.start(approval, lambda s: (stage, sparrer)), stage

    def refuses_before_providers(self, approval, pattern, **kwargs):
        with self.assertRaisesRegex(PlanError, pattern):
            self.start(approval, _Forbidden(self), **kwargs)
        self.assertFalse((self.sparring_dir / "plans").exists() and any((self.sparring_dir / "plans").iterdir()))


class ApprovedExecutionTests(_Sealed):
    def test_an_unmodified_approved_manifest_runs_to_completion(self):
        _, approval = self.prepared_and_approved()
        result, _ = self.run_app(approval)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(result.run, approval.run_key)
        state = PlanRunState.load(run_state_path(self.sparring_dir, approval.run_key))
        self.assertEqual(state.source, SOURCE_KIND)
        self.assertEqual(state.plan_digest, self.source(approval).digest())

    def test_the_run_key_defaults_to_and_must_equal_the_approved_one(self):
        _, approval = self.prepared_and_approved()
        self.refuses_before_providers(approval, "was approved as run", run_key="some-other-run")

    def test_a_run_branch_argument_cannot_replace_the_approved_branch(self):
        _, approval = self.prepared_and_approved()
        self.refuses_before_providers(approval, "was approved for branch", branch="feature/other")


class ManifestTamperingTests(_Sealed):
    def test_an_edited_brief_refuses_before_any_provider(self):
        _, approval = self.prepared_and_approved()
        path = approval.manifest_path
        path.write_text(path.read_text(encoding="utf-8").replace("Emit reviewed", "Emit unreviewed"), encoding="utf-8")
        self.refuses_before_providers(approval, "bytes changed after approval")

    def test_a_semantically_identical_reserialization_refuses(self):
        _, approval = self.prepared_and_approved()
        payload = json.loads(approval.manifest_path.read_text(encoding="utf-8"))
        approval.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        self.refuses_before_providers(approval, "bytes changed after approval")

    def test_a_missing_approval_refuses(self):
        _, approval = self.prepared_and_approved()
        approval.approval_path.unlink()
        self.refuses_before_providers(approval, "has no approval.json beside it")

    def test_a_copied_manifest_and_approval_refuse_outside_their_intake(self):
        _, approval = self.prepared_and_approved()
        elsewhere = self.root / "copy" / "runs" / "app"
        elsewhere.mkdir(parents=True)
        for name in ("manifest.json", "approval.json"):
            (elsewhere / name).write_bytes((approval.manifest_path.parent / name).read_bytes())
        with self.assertRaisesRegex(PlanError, "is not in the intake it was approved in"):
            load_plan_source(elsewhere / "manifest.json", self.repo, manifest=True)

    def test_the_stripped_inner_manifest_cannot_run_as_a_plain_one(self):
        _, approval = self.prepared_and_approved()
        inner = json.loads(approval.manifest_path.read_text(encoding="utf-8"))["manifest"]
        plain = self.root / "plain.json"
        plain.write_text(json.dumps(inner), encoding="utf-8")
        source = load_plan_source(plain, self.repo, manifest=True)
        self.assertEqual(source.kind, "manifest")
        with self.assertRaisesRegex(PlanError, "belong\\(s\\) to plan intake"):
            start_plan(source, self.sparring_dir, self.repo, _Forbidden(self), expected_branch=APP_BRANCH)
        # Nor with a fresh run key: the stage ids are intake's.
        with self.assertRaisesRegex(PlanError, "belong\\(s\\) to plan intake"):
            start_plan(
                source, self.sparring_dir, self.repo, _Forbidden(self), expected_branch=APP_BRANCH,
                run_key="hand-run-0001",
            )

    def test_the_plain_reader_refuses_an_envelope_by_name(self):
        _, approval = self.prepared_and_approved()
        from agent_sparring.manifest import ManifestError, load_manifest_source

        with self.assertRaisesRegex(ManifestError, "intake-approved manifest"):
            load_manifest_source(approval.manifest_path)


class ProposalTamperingTests(_Sealed):
    def _rewrite_interpretation_consistently(self, intake_dir: Path):
        """Edit what a stage says and make every digest in intake.json agree
        again -- the coordinated rewrite approval-time checks cannot see."""

        from agent_sparring.intake import sha256_text

        path = intake_dir / "interpretation.json"
        text = path.read_text(encoding="utf-8").replace('"App change"', '"App change, widened"')
        path.write_text(text, encoding="utf-8")
        record_path = intake_dir / "intake.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["interpretation_digest"] = sha256_text(text)
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def test_a_consistent_rewrite_of_the_proposal_refuses_the_approved_run(self):
        intake, approval = self.prepared_and_approved()
        self._rewrite_interpretation_consistently(intake.directory)
        self.refuses_before_providers(approval, "intake.json changed after run slice 'app' was approved")

    def test_an_edited_interpretation_alone_refuses_the_approved_run(self):
        intake, approval = self.prepared_and_approved()
        path = intake.directory / "interpretation.json"
        path.write_text(path.read_text(encoding="utf-8").replace("Audit", "Audit!"), encoding="utf-8")
        self.refuses_before_providers(approval, "interpretation.json changed after")

    def test_a_changed_source_plan_after_approval_refuses(self):
        _, approval = self.prepared_and_approved()
        self.plan.write_text(PLAN + "\nLate requirement.\n", encoding="utf-8")
        self.refuses_before_providers(approval, "source plan .* changed after run slice")

    def test_a_changed_source_snapshot_refuses(self):
        intake, approval = self.prepared_and_approved()
        (intake.directory / "source.md").write_text(PLAN + "\nx\n", encoding="utf-8")
        self.refuses_before_providers(approval, "source.md changed after approval")

    def test_an_edited_report_cannot_be_approved(self):
        intake = self.prepare()
        report = intake.directory / "report.md"
        report.write_text(report.read_text(encoding="utf-8").replace("Audit", "Audit (trust me)"), encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "is not the report this intake renders"):
            self.approve(intake.directory)

    def test_a_consistent_rewrite_before_approval_is_caught_by_the_report(self):
        intake = self.prepare()
        self._rewrite_interpretation_consistently(intake.directory)
        with self.assertRaisesRegex(IntakeError, "no longer renders|is not the report"):
            self.approve(intake.directory)
        self.assertFalse((intake.directory / "runs").exists())

    def test_proposal_edits_during_a_run_stop_it_before_acceptance(self):
        # A stage turn that (accidentally) regenerates the proposal.
        intake, approval = self.prepared_and_approved()
        path = intake.directory / "interpretation.json"

        def edit():
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

        with self.assertRaisesRegex(PlanRunError, "interpretation.json changed after"):
            self.run_app(approval, during=edit)
        first = Stage.resolve(self.sparring_dir, approval_stage_ids(approval)[0]).read_state()
        self.assertIsNot(first.status, StageStatus.ACCEPTED)


def approval_stage_ids(approval):
    return json.loads(approval.approval_path.read_text(encoding="utf-8"))["stage_ids"]


class RepositoryDriftTests(_Sealed):
    def test_a_context_repository_that_moved_refuses_approval(self):
        intake = self.prepare()
        (self.web / "new.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", "new.txt")
        _git(self.web, "commit", "-q", "-m", "moved on")
        with self.assertRaisesRegex(IntakeError, "web: feature/web moved from"):
            self.approve(intake.directory)
        self.assertFalse((intake.directory / "runs").exists())

    def test_a_branch_switch_refuses_approval(self):
        intake = self.prepare()
        _git(self.repo, "checkout", "-q", "-b", "feature/elsewhere")
        with self.assertRaisesRegex(IntakeError, "branch 'feature/widgets' is now 'feature/elsewhere'"):
            self.approve(intake.directory)

    def test_a_commit_between_approval_and_first_run_refuses(self):
        _, approval = self.prepared_and_approved()
        (self.repo / "late.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "late.txt")
        _git(self.repo, "commit", "-q", "-m", "late")
        self.refuses_before_providers(approval, "app: approved at feature/widgets@")

    def test_another_worktree_of_the_same_repository_refuses(self):
        _, approval = self.prepared_and_approved()
        other = self.root / "app-other"
        _git(self.repo, "worktree", "add", "-q", "-b", "other", str(other))
        _git(other, "checkout", "-q", "--detach")
        _git(self.repo, "checkout", "-q", "--detach")
        _git(other, "checkout", "-q", APP_BRANCH)
        (other / ".sparring").mkdir()
        with self.assertRaisesRegex(PlanError, "run it from the repository that was approved"):
            start_plan(
                load_plan_source(approval.manifest_path, other, manifest=True),
                other / ".sparring", other, _Forbidden(self), expected_branch=APP_BRANCH,
            )

    def test_a_context_repository_that_moved_after_approval_refuses_the_fresh_run(self):
        _, approval = self.prepared_and_approved()
        (self.web / "later.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", "later.txt")
        _git(self.web, "commit", "-q", "-m", "moved after approval")
        self.refuses_before_providers(approval, "web: approved at feature/web@")

    def test_a_context_repository_on_another_branch_refuses_the_fresh_run(self):
        _, approval = self.prepared_and_approved()
        _git(self.web, "checkout", "-q", "-b", "feature/other")
        self.refuses_before_providers(approval, "now feature/other@")

    def test_a_paused_sealed_run_resumes_only_from_its_approved_worktree(self):
        _, approval = self.prepared_and_approved()
        stage = _Committer(self.repo, APP_BRANCH, fail_at=1)
        with self.assertRaises(PlanRunError):
            self.start(approval, lambda s: (stage, _SparringAdapter([READY])))
        state = PlanRunState.load(run_state_path(self.sparring_dir, approval.run_key))
        self.assertIsNot(state.status, PlanRunStatus.COMPLETE)

        clone = self.root / "app-clone"
        subprocess.run(
            ["git", "clone", "-q", "-b", APP_BRANCH, str(self.repo), str(clone)],
            check=True, capture_output=True,
        )
        worktree = self.root / "app-worktree"
        _git(self.repo, "worktree", "add", "-q", "-b", "feature/widgets-copy", str(worktree))
        for other in (clone, worktree):
            with self.subTest(other=other.name):
                with self.assertRaisesRegex(PlanError, "run it from the repository that was approved"):
                    resume_plan(
                        load_plan_source(approval.manifest_path, other, manifest=True),
                        self.sparring_dir, other, _Forbidden(self), expected_branch=APP_BRANCH,
                        run_key=approval.run_key,
                    )

        # The approved worktree still resumes: continuation checks its
        # identity and branch, not the starting commit.
        result = resume_plan(
            self.source(approval), self.sparring_dir, self.repo,
            lambda s: (_Committer(self.repo, APP_BRANCH), _SparringAdapter([READY] * 2)),
            expected_branch=APP_BRANCH,
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_a_paused_sealed_run_does_not_resume_off_its_branch(self):
        _, approval = self.prepared_and_approved()
        with self.assertRaises(PlanRunError):
            self.start(approval, lambda s: (_Committer(self.repo, APP_BRANCH, fail_at=1), _SparringAdapter([])))
        _git(self.repo, "checkout", "-q", "-b", "feature/detour")
        with self.assertRaisesRegex(PlanError, "is on 'feature/detour'"):
            resume_plan(
                self.source(approval), self.sparring_dir, self.repo, _Forbidden(self),
                expected_branch=APP_BRANCH,
            )

    def test_normal_progress_inside_the_run_is_not_drift(self):
        # Stage 0 is accepted (HEAD moves) and stage 1A's provider fails.
        # Resuming compares the pinned approval, not the starting commit.
        _, approval = self.prepared_and_approved()
        started = _head(self.repo)
        with self.assertRaises(PlanRunError):
            self.run_app(approval, fail_at=2)
        self.assertNotEqual(_head(self.repo), started)
        result = resume_plan(
            self.source(approval), self.sparring_dir, self.repo,
            lambda s: (_Committer(self.repo, APP_BRANCH), _SparringAdapter([READY] * 2)),
            expected_branch=APP_BRANCH,
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_there_is_no_drift_override(self):
        import inspect

        parameters = inspect.signature(approve_plan).parameters
        self.assertFalse([name for name in parameters if "drift" in name or "override" in name or "allow" in name])


class ApprovalRecordTests(_Sealed):
    def test_repeating_an_identical_approval_is_idempotent(self):
        intake, approval = self.prepared_and_approved()
        before = approval.approval_path.read_bytes()
        again = self.approve(intake.directory)
        self.assertFalse(again.created)
        self.assertEqual(approval.approval_path.read_bytes(), before)
        self.assertEqual(again.run_key, approval.run_key)

    def test_a_repeat_after_the_manifest_changed_refuses(self):
        intake, approval = self.prepared_and_approved()
        approval.manifest_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "changed after run slice 'app' was approved"):
            self.approve(intake.directory)

    def test_a_conflicting_decision_refuses(self):
        payload = good_interpretation()
        payload["amended_plan"] = PLAN + "\nMore.\n"
        intake = self.prepare(payload, mode="refine")
        self.approve(intake.directory, without_amendment=True)
        record = json.loads((intake.directory / "runs/app/approval.json").read_text(encoding="utf-8"))
        record_path = intake.directory / "runs/app/approval.json"
        record["without_amendment"] = False
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "already approved with"):
            self.approve(intake.directory, without_amendment=True)

    def test_an_approval_is_created_exactly_once(self):
        target = self.root / "once.json"
        self.assertTrue(write_exclusive(target, "first\n"))
        self.assertFalse(write_exclusive(target, "second\n"))
        self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
        self.assertEqual([p.name for p in self.root.glob(".once.json.*")], [])

    def test_an_unsealed_legacy_intake_cannot_be_approved_or_run(self):
        intake = self.prepare()
        record_path = intake.directory / "intake.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["version"] = 1
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "unsealed intake prepared before approvals"):
            self.approve(intake.directory)
        # What such an intake used to write: a plain v1 manifest of its
        # stages, next to an unsealed approval.
        legacy = intake.directory / "runs" / "app"
        legacy.mkdir(parents=True)
        stage_id = next(iter(record["briefs"]))
        (legacy / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "plan_label": "docs/plan.md",
                    "source_digest": "sha256:x",
                    "stages": [{"stage_id": stage_id, "label": "Stage 0", "title": "Audit", "brief": "b"}],
                }
            ),
            encoding="utf-8",
        )
        source = load_plan_source(legacy / "manifest.json", self.repo, manifest=True)
        with self.assertRaisesRegex(PlanError, "belong\\(s\\) to plan intake"):
            start_plan(source, self.sparring_dir, self.repo, _Forbidden(self), expected_branch=APP_BRANCH)


class RecoveryTests(_Sealed):
    def test_a_crash_after_the_manifest_but_before_the_approval_fails_closed(self):
        intake, approval = self.prepared_and_approved()
        approval.approval_path.unlink()  # as if the process died between the two writes
        self.refuses_before_providers(approval, "has no approval.json beside it")
        again = self.approve(intake.directory)
        self.assertTrue(again.created)
        result, _ = self.run_app(again)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_a_restarted_process_resumes_under_the_same_approval_and_refuses_a_changed_one(self):
        _, approval = self.prepared_and_approved()
        stage = _Committer(self.repo, APP_BRANCH, fail_at=1)
        sparrer = _SparringAdapter([READY] * 4)
        with self.assertRaises(PlanRunError):
            self.start(approval, lambda s: (stage, sparrer))

        # A later process: only what is on disk.
        original = approval.manifest_path.read_bytes()
        approval.manifest_path.write_bytes(original.replace(b"Emit reviewed", b"Emit other"))
        with self.assertRaisesRegex(PlanError, "bytes changed after approval"):
            self.source(approval)
        approval.manifest_path.write_bytes(original)
        result = resume_plan(
            self.source(approval), self.sparring_dir, self.repo,
            lambda s: (_Committer(self.repo, APP_BRANCH), sparrer), expected_branch=APP_BRANCH,
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_a_sealed_run_cannot_be_resumed_from_the_stripped_manifest(self):
        _, approval = self.prepared_and_approved()
        stage = _Committer(self.repo, APP_BRANCH, fail_at=1)
        with self.assertRaises(PlanRunError):
            self.start(approval, lambda s: (stage, _SparringAdapter([READY])))
        inner = json.loads(approval.manifest_path.read_text(encoding="utf-8"))["manifest"]
        plain = self.root / "plain.json"
        plain.write_text(json.dumps(inner), encoding="utf-8")
        with self.assertRaisesRegex(PlanError, "different kind of input"):
            resume_plan(
                load_plan_source(plain, self.repo, manifest=True), self.sparring_dir, self.repo,
                _Forbidden(self), expected_branch=APP_BRANCH, run_key=approval.run_key,
            )


class PrerequisiteTests(_Sealed):
    def approve_web(self, intake_dir, **kwargs):
        kwargs.setdefault("confirmed_prerequisites", ["release"])
        return self.approve(intake_dir, run="web", **kwargs)

    def test_a_dependent_slice_needs_the_earlier_slice_approved_and_completed(self):
        intake, approval = self.prepared_and_approved()
        with self.assertRaisesRegex(IntakeError, "approved but has not been run"):
            self.approve_web(intake.directory)
        stage = _Committer(self.repo, APP_BRANCH, fail_at=2)
        with self.assertRaises(PlanRunError):
            self.start(approval, lambda s: (stage, _SparringAdapter([READY] * 4)))
        with self.assertRaisesRegex(IntakeError, "has not completed"):
            self.approve_web(intake.directory)

        result = resume_plan(
            self.source(approval), self.sparring_dir, self.repo,
            lambda s: (_Committer(self.repo, APP_BRANCH), _SparringAdapter([READY] * 4)),
            expected_branch=APP_BRANCH,
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

        # app's HEAD moved -- to exactly the commit its slice was accepted
        # at, which is the one movement approval allows, and records.
        web = self.approve_web(intake.directory)
        record = json.loads(web.approval_path.read_text(encoding="utf-8"))
        (evidence,) = record["prerequisites"]["earlier_slices"]
        self.assertEqual(evidence["run_id"], "app")
        self.assertEqual(evidence["final_candidate_sha"], _head(self.repo))
        self.assertEqual(evidence["approval_sha256"], self.source(approval).approval.sha256)
        self.assertEqual(record["prerequisites"]["gates_confirmed"], ["release"])
        self.assertEqual(record["repositories"]["at_approval"]["app"]["head"], _head(self.repo))

        # ...and any further movement is drift again.
        (self.repo / "extra.txt").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "extra.txt")
        _git(self.repo, "commit", "-q", "-m", "unrelated")
        web.approval_path.unlink()
        with self.assertRaisesRegex(IntakeError, "not a commit an earlier slice was accepted at"):
            self.approve_web(intake.directory)

    def test_a_fabricated_completed_run_is_not_proof(self):
        intake, approval = self.prepared_and_approved()
        state_path = run_state_path(self.sparring_dir, approval.run_key)
        stage_ids = approval_stage_ids(approval)
        PlanRunState(
            plan="docs/plan.md",
            plan_digest="made-up",
            expected_branch=APP_BRANCH,
            current_stage_index=1,
            current_stage=stage_ids[-1],
            status=PlanRunStatus.COMPLETE,
            source=SOURCE_KIND,
            run=approval.run_key,
        ).save(state_path)
        with self.assertRaisesRegex(IntakeError, "is not the sealed run of earlier slice 'app'"):
            self.approve_web(intake.directory)

    def test_a_hand_written_approval_for_the_earlier_slice_is_not_proof(self):
        intake = self.prepare()
        forged = intake.directory / "runs" / "app"
        forged.mkdir(parents=True)
        (forged / "approval.json").write_text(json.dumps({"version": 2, "decision": "approved"}), encoding="utf-8")
        (forged / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "earlier run slice 'app' has no valid approval"):
            self.approve_web(intake.directory)


class CliTests(_Sealed):
    def _main(self, *argv, sparring_dir=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(sparring_dir or self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_run_plan_refuses_a_tampered_intake_manifest_before_any_provider(self):
        _, approval = self.prepared_and_approved()
        approval.manifest_path.write_text(
            approval.manifest_path.read_text(encoding="utf-8").replace("Audit", "Audit!"), encoding="utf-8"
        )
        with mock.patch("agent_sparring.cli._build_loop_adapters", side_effect=AssertionError("provider")) as made:
            code, _, err = self._main(
                "run-plan", "--manifest", str(approval.manifest_path), "--repo-root", str(self.repo),
                "--expected-branch", APP_BRANCH, "--run-key", approval.run_key,
            )
        self.assertEqual(code, 1, err)
        self.assertIn("bytes changed after approval", err)
        made.assert_not_called()

    def test_run_plan_refuses_a_moved_context_repository_before_any_provider(self):
        _, approval = self.prepared_and_approved()
        (self.web / "later.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", "later.txt")
        _git(self.web, "commit", "-q", "-m", "moved after approval")
        with mock.patch("agent_sparring.cli._build_loop_adapters", side_effect=AssertionError("provider")) as made:
            code, _, err = self._main(
                "run-plan", "--manifest", str(approval.manifest_path), "--repo-root", str(self.repo),
                "--expected-branch", APP_BRANCH, "--run-key", approval.run_key,
            )
        self.assertEqual(code, 1, err)
        self.assertIn("repositories moved since run slice 'app' was approved", err)
        made.assert_not_called()
        self.assertFalse(run_state_path(self.sparring_dir, approval.run_key).exists())


if __name__ == "__main__":
    unittest.main()
