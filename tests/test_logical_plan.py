"""One logical plan across repositories: its record, scoped executions,
finish eligibility of a part, and the obligation ledger between parts."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import logical_plan, managed_finish, managed_run
from agent_sparring.deferred_gate import DeferredAnswer, DeferredVerificationRequired
from agent_sparring.logical_plan import LogicalPlanError, RepositoryBinding
from agent_sparring.managed_run import ManagedRunError, ManagedRunRecord
from agent_sparring.plan import (
    PlanError,
    PlanRunState,
    PlanRunStatus,
    load_markdown_source,
    resume_plan,
    run_state_path,
    start_plan,
)
from agent_sparring.plan_obligations import LogicalSliceContext, load_ledger, slice_context
from agent_sparring.stage import Stage, StageStatus
from test_deferred_human_verification import READY_WITH_DEFERRAL
from test_intake_sealing import _Committer
from test_managed_run import _git, _ManagedRepoTestCase
from test_plan import PLAN, READY, _PlanRepoTestCase, _run_git, _SparringAdapter

K = "plan-logical"
K2 = f"{K}-s2"
WEB_BRANCH = "feature/web"
CHECK = "resize-readability"
OWNED_PLAN = PLAN.replace("Render incrementally.", "Repository: web\n\nRender incrementally.")


def _created(record: ManagedRunRecord) -> ManagedRunRecord:
    return replace(record, events=({"at": "2026-10-09T00:00:00Z", "event": "created", "detail": {}},))


class _LogicalCase(_PlanRepoTestCase):
    """Home ``repo`` (stage 1) and ``web`` (stage 2), each with an engine
    record whose worktree is the checkout itself."""

    plan_text = OWNED_PLAN

    def setUp(self):
        super().setUp()
        if self.plan_text != PLAN:
            self.plan_path.write_text(self.plan_text, encoding="utf-8")
            _run_git(self.repo, "commit", "-qam", "owned plan")
            _run_git(self.repo, "push", "-q", "origin", "feature/x")
        root = self.repo.parent
        self.web_remote = root / "web.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.web_remote)], check=True, capture_output=True)
        self.web = root / "web"
        self.web.mkdir()
        _run_git(self.web, "init", "-q", "-b", "main")
        _run_git(self.web, "config", "user.email", "test@example.com")
        _run_git(self.web, "config", "user.name", "Test")
        _run_git(self.web, "remote", "add", "origin", str(self.web_remote))
        (self.web / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        _run_git(self.web, "add", ".")
        _run_git(self.web, "commit", "-q", "-m", "base")
        _run_git(self.web, "checkout", "-q", "-b", WEB_BRANCH)
        _run_git(self.web, "push", "-q", "-u", "origin", WEB_BRANCH)

        self.home_common = managed_run.git_common_dir(self.repo)
        self.web_common = managed_run.git_common_dir(self.web)
        self.record = self.make_record()
        self.snapshot = logical_plan.snapshot_path(self.home_common, self.record)

    def bindings(self):
        return (
            RepositoryBinding("repo", str(self.repo.resolve()), str(self.home_common), "repo"),
            RepositoryBinding("web", str(self.web.resolve()), str(self.web_common), "web"),
        )

    def make_record(self, **overrides):
        snapshot, sha = logical_plan.snapshot_input(self.home_common, K, self.plan_path)
        source = load_markdown_source(self.plan_path, "docs/plan.md", namespace=K)
        record = logical_plan.new_record(
            logical_key=K, source=source, input_path=self.plan_path, home="repo",
            repositories=self.bindings(), snapshot=snapshot, snapshot_sha256=sha,
        )
        return logical_plan.create(self.home_common, replace(record, **overrides) if overrides else record)

    def execution(self, checkout: Path, key: str, branch: str, *, input_path: Path, index: int):
        record = _created(
            ManagedRunRecord(
                run_key=key, plan_label="docs/plan.md", input_kind="markdown", input_path=str(input_path),
                worktree_path=str(managed_run.worktree_top(checkout)), branch=branch, target_branch="main",
                base_sha=_git(checkout, "rev-parse", "HEAD"), remote="origin", created_at="2026-10-09T00:00:00Z",
                logical_slice=(
                    {"logical_key": K, "home_common_dir": str(self.home_common), "index": index} if index > 1 else None
                ),
            )
        )
        managed_run.create_record(checkout, record)
        return record

    def scoped(self, run_key: str, *, snapshot: bool = False):
        inner = (
            logical_plan.snapshot_source(self.home_common, self.record)
            if snapshot
            else load_markdown_source(self.plan_path, "docs/plan.md")
        )
        return logical_plan.scoped_source(inner, self.record, self.home_common, run_key)


class RecordTests(_LogicalCase):
    def test_the_record_holds_each_stages_resolved_owner_and_its_slices(self):
        self.assertEqual([s.owner for s in self.record.stages], ["repo", "web"])
        self.assertEqual(
            [(s.id, s.primary_repository, len(s.stages)) for s in self.record.slices], [(K, "repo", 1), (K2, "web", 1)]
        )
        self.assertEqual(logical_plan.slice_run_key(K, 1), K)
        self.assertEqual(logical_plan.slice_run_key(K, 3), f"{K}-s3")
        self.assertEqual([e["event"] for e in self.record.events], ["created"])
        self.assertEqual(logical_plan.load(self.home_common, K), self.record)
        self.assertEqual(self.snapshot.read_bytes(), self.plan_path.read_bytes())

    def test_creation_is_exclusive_and_events_only_append(self):
        before = logical_plan.record_path(self.home_common, K).read_bytes()
        with self.assertRaises(LogicalPlanError) as ctx:
            logical_plan.create(self.home_common, self.record)
        self.assertEqual(ctx.exception.code, "logical_record_exists")
        self.assertEqual(logical_plan.record_path(self.home_common, K).read_bytes(), before)
        updated = logical_plan.append_event(self.home_common, K, "slice_created", {"index": 2})
        self.assertEqual([e["event"] for e in updated.events], ["created", "slice_created"])
        self.assertEqual(replace(updated, events=()), replace(self.record, events=()))
        with self.assertRaises(LogicalPlanError):
            logical_plan.append_event(self.home_common, K, "finished")

    def test_a_snapshot_is_never_replaced_by_different_bytes(self):
        self.plan_path.write_text(self.plan_text + "\nmore\n", encoding="utf-8")
        with self.assertRaises(LogicalPlanError) as ctx:
            logical_plan.snapshot_input(self.home_common, K, self.plan_path)
        self.assertEqual(ctx.exception.code, "snapshot_exists")

    def test_hand_edited_records_are_refused(self):
        path = logical_plan.record_path(self.home_common, K)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for mutate in (
            lambda p: p["stages"][1].update(owner="elsewhere"),
            lambda p: p["slices"].pop(),
            lambda p: p.update(extra=1),
            lambda p: p["repositories"]["web"].update(git_common_dir=p["repositories"]["repo"]["git_common_dir"]),
        ):
            broken = json.loads(json.dumps(payload))
            mutate(broken)
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(LogicalPlanError):
                logical_plan.load(self.home_common, K)

    def test_an_owner_without_a_binding_is_refused(self):
        source = load_markdown_source(self.plan_path, "docs/plan.md", namespace="other")
        snapshot, sha = logical_plan.snapshot_input(self.home_common, "other", self.plan_path)
        with self.assertRaises(LogicalPlanError) as ctx:
            logical_plan.new_record(
                logical_key="other", source=source, input_path=self.plan_path, home="repo",
                repositories=self.bindings()[:1], snapshot=snapshot, snapshot_sha256=sha,
            )
        self.assertEqual(ctx.exception.code, "repository_unknown")


class ScopedSourceTests(_LogicalCase):
    def test_scoped_digest_is_the_logical_input_digest(self):
        whole = load_markdown_source(self.plan_path, "docs/plan.md").digest()
        for key in (K, K2):
            for snapshot in (False, True):
                self.assertEqual(self.scoped(key, snapshot=snapshot).digest(), self.record.input_digest)
        self.assertEqual(self.record.input_digest, whole)

    def test_only_the_stages_in_scope_at_their_original_positions_namespaced_by_the_logical_key(self):
        first, second = self.scoped(K).stages(), self.scoped(K2, snapshot=True).stages()
        self.assertEqual([(s.position, s.stage_id) for s in first], [(1, f"{K}-stage-1-foundation")])
        self.assertEqual([(s.position, s.stage_id) for s in second], [(2, f"{K}-stage-2-incremental-rendering")])
        self.assertEqual(self.scoped(K2).scope, {"logical_key": K, "stage_ids": [second[0].stage_id]})
        self.assertEqual(self.scoped(K2).reload().stages(), second)

    def test_another_input_is_refused(self):
        other = self.repo / "docs" / "other.md"
        other.write_text(self.plan_text.replace("Lay the groundwork", "Dig"), encoding="utf-8")
        with self.assertRaises(LogicalPlanError) as ctx:
            logical_plan.scoped_source(load_markdown_source(other, "docs/plan.md"), self.record, self.home_common, K)
        self.assertEqual(ctx.exception.code, "logical_digest_mismatch")
        with self.assertRaises(LogicalPlanError):
            self.scoped("plan-logical-s9")


class OwnersFromTheRecordTests(_LogicalCase):
    """The record, not the text, says who owns a stage."""

    plan_text = PLAN  # no Repository: line anywhere

    def make_record(self):
        record = super().make_record()
        path = logical_plan.record_path(self.home_common, K)
        path.unlink()
        stages = (record.stages[0], replace(record.stages[1], owner="web"))
        return logical_plan.create(
            self.home_common, replace(record, stages=stages, slices=logical_plan.derive_slices(K, stages), events=())
        )

    def test_owners_survive_a_snapshot_without_repository_lines(self):
        self.assertNotIn("Repository:", self.snapshot.read_text(encoding="utf-8"))
        (stage,) = self.scoped(K2, snapshot=True).stages()
        self.assertEqual(stage.owner, "web")
        (stage,) = self.scoped(K).stages()
        self.assertEqual(stage.owner, "repo")


class SliceRunTests(_LogicalCase):
    def setUp(self):
        super().setUp()
        self.execution(self.repo, K, "feature/x", input_path=self.plan_path, index=1)
        self.execution(self.web, K2, WEB_BRANCH, input_path=self.snapshot, index=2)

    def run_first(self, verdicts=(READY_WITH_DEFERRAL,)):
        sparrer = _SparringAdapter(list(verdicts))
        committer = _Committer(self.repo, "feature/x")
        return start_plan(
            self.scoped(K), self.sparring_dir, self.repo, lambda s: (committer, sparrer),
            expected_branch="feature/x", managed=True,
        )

    def run_second(self):
        sparrer = _SparringAdapter([READY] * 3)
        committer = _Committer(self.web, WEB_BRANCH)
        return start_plan(
            self.scoped(K2, snapshot=True), self.web / ".sparring", self.web, lambda s: (committer, sparrer),
            expected_branch=WEB_BRANCH, managed=True,
        )

    def resume_second(self, source=None, **kwargs):
        return resume_plan(
            source or self.scoped(K2, snapshot=True), self.web / ".sparring", self.web, _never,
            expected_branch=WEB_BRANCH, run_key=K2, **kwargs,
        )

    def ledger(self):
        return load_ledger(logical_plan.ledger_path(self.home_common, K))

    def test_a_part_counts_only_its_stages_and_carries_the_plans_obligation(self):
        result = self.run_first()
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        state = PlanRunState.load(run_state_path(self.sparring_dir, K))
        self.assertEqual(state.scope, {"logical_key": K, "stage_ids": [f"{K}-stage-1-foundation"]})
        self.assertEqual(state.plan_digest, self.record.input_digest)
        self.assertEqual(state.deferred_human_checks, ())
        (entry,) = self.ledger()
        self.assertEqual(entry.origin_slice, K)
        self.assertEqual(state.carried_deferred, (entry.instance_id,))
        self.assertFalse(Stage.resolve(self.sparring_dir, f"{K}-stage-2-incremental-rendering").exists())
        status = logical_plan.derived_status(self.record)
        self.assertEqual([s.complete for s in status.slices], [True, False])
        self.assertEqual(status.slices[1].lifecycle, "created")

    def test_the_last_part_claims_it_and_stops_until_answered(self):
        self.run_first()
        (entry,) = self.ledger()
        result = self.run_second()
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        self.assertEqual(result.awaiting.instance_ids, (entry.instance_id,))

        done = self.resume_second(deferred_results=(DeferredAnswer.parse(f"{CHECK}=pass=clean"),))
        self.assertIs(done.status, PlanRunStatus.COMPLETE)
        (entry,) = self.ledger()
        self.assertEqual(entry.resolved_by, K2)
        self.assertEqual(entry.writeback_skipped, ())
        notes = Stage.resolve(self.sparring_dir, entry.obligation.stage_id).read_notes()
        self.assertIn("clean", notes)
        self.assertTrue(logical_plan.derived_status(self.record).complete)

    def test_an_archived_origin_is_not_recreated_and_the_skip_is_recorded(self):
        self.run_first()
        (entry,) = self.ledger()
        self.run_second()
        origin = Stage.resolve(self.sparring_dir, entry.obligation.stage_id).directory
        archive = self.repo.parent / "archived-stage"
        shutil.move(str(origin), str(archive))  # what finish-run's archive leaves behind

        done = self.resume_second(deferred_results=(DeferredAnswer.parse(f"{CHECK}=pass=clean"),))
        self.assertIs(done.status, PlanRunStatus.COMPLETE)
        self.assertFalse(origin.exists(), "a removed origin is never recreated")
        (entry,) = self.ledger()
        self.assertEqual(entry.resolved_by, K2)
        self.assertEqual([(s["check_id"], s["run"]) for s in entry.writeback_skipped], [(CHECK, K2)])
        here = Stage.resolve(self.web / ".sparring", f"{K}-stage-2-incremental-rendering").read_notes()
        self.assertIn("clean", here)
        self.assertIn("was archived", here)

    def test_resume_scopes_a_plain_input_from_the_record_and_refuses_another_part(self):
        self.run_first()
        self.run_second()
        plain = load_markdown_source(self.snapshot, "docs/plan.md")
        again = self.resume_second(source=plain)
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        with self.assertRaisesRegex(PlanError, "different part"):
            self.resume_second(source=self.scoped(K))

    def tamper_first(self, mutate):
        path = run_state_path(self.sparring_dir, K)
        state = PlanRunState.load(path)
        mutate(state)
        state.save(path)

    def first_status(self):
        return logical_plan.derived_status(self.record).slices[0]

    def test_a_complete_state_proves_nothing_unless_it_is_this_part_of_this_plan(self):
        self.run_first()
        self.assertTrue(self.first_status().complete, self.first_status().proof)
        original = run_state_path(self.sparring_dir, K).read_bytes()
        for mutate, why in (
            (lambda s: setattr(s, "plan_digest", "0" * 64), "different plan input"),
            (lambda s: setattr(s, "scope", None), "not scoped"),
            (lambda s: setattr(s, "scope", {"logical_key": "other", "stage_ids": s.scope["stage_ids"]}), "not scoped"),
            (lambda s: setattr(s, "run", "someone-else"), "not plan-logical"),
        ):
            run_state_path(self.sparring_dir, K).write_bytes(original)
            self.tamper_first(mutate)
            status = self.first_status()
            self.assertFalse(status.complete, why)
            self.assertIn(why, status.proof)

    def test_a_complete_state_with_an_unaccepted_stage_in_scope_is_not_complete(self):
        self.run_first()
        stage = Stage.resolve(self.sparring_dir, f"{K}-stage-1-foundation")
        state = stage.read_state()
        state.status = StageStatus.WORKING
        stage.write_state(state)
        status = self.first_status()
        self.assertFalse(status.complete)
        self.assertIn("not accepted", status.proof)

    def test_an_unproven_earlier_part_is_not_counted_so_the_check_is_not_claimed(self):
        self.run_first()
        self.tamper_first(lambda s: setattr(s, "plan_digest", "0" * 64))
        result = self.run_second()
        self.assertIsNone(result.awaiting, "nothing claimed while slice 1 is unproven")
        (entry,) = self.ledger()
        self.assertIsNone(entry.resolved_by)
        self.assertEqual(slice_context(self.scoped(K2)).incomplete_slices(), (K,))

    def test_an_integrated_part_stays_complete_after_its_state_is_archived(self):
        self.run_first()
        run_state_path(self.sparring_dir, K).unlink()
        self.assertFalse(self.first_status().complete)
        managed_run.append_event(self.repo, K, "merged", {})
        status = self.first_status()
        self.assertTrue(status.complete, status.proof)

    def test_an_execution_that_does_not_name_this_plan_is_not_its_part(self):
        path = managed_run.record_path(self.web, K2)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for bad in (None, {**payload["slice"], "index": 3}, {**payload["slice"], "home_common_dir": "/elsewhere"}):
            changed = {k: v for k, v in payload.items() if k != "slice"}
            if bad is not None:
                changed["slice"] = bad
            changed["events"] = payload["events"] + [{"at": "t", "event": "merged", "detail": {}}]
            path.write_text(json.dumps(changed), encoding="utf-8")
            second = logical_plan.derived_status(self.record).slices[1]
            self.assertTrue(second.integrated)
            self.assertFalse(second.complete, bad)

    def test_the_slice_context_is_the_logical_plans(self):
        context = slice_context(self.scoped(K2))
        self.assertIsInstance(context, LogicalSliceContext)
        self.assertEqual(context.run_ids, (K, K2))
        self.assertEqual(context.ledger_path, logical_plan.ledger_path(self.home_common, K))
        self.assertEqual(context.incomplete_slices(), (K,))


def _never(stage):
    raise AssertionError("no provider turn is expected")


class ScopedFinishTests(_ManagedRepoTestCase):
    """Finish eligibility of a managed run that is one part of a logical plan."""

    def setUp(self):
        super().setUp()
        self._start_managed()
        (self.managed,) = self._records()
        self.worktree = Path(self.managed.worktree_path)
        key = self.managed.run_key
        common = managed_run.git_common_dir(self.repo)
        input_path = Path(self.managed.input_path)
        snapshot, sha = logical_plan.snapshot_input(common, key, input_path)
        source = load_markdown_source(input_path, self.managed.plan_label, namespace=key)
        bindings = (
            RepositoryBinding("repo", str(self.repo.resolve()), str(common), "repo"),
            RepositoryBinding("web", str(self.repo.parent / "web"), str(self.repo.parent / "web.git"), "web"),
        )
        record = logical_plan.new_record(
            logical_key=key, source=source, input_path=input_path, home="repo",
            repositories=bindings, snapshot=snapshot, snapshot_sha256=sha,
        )
        stages = (record.stages[0], replace(record.stages[1], owner="web"))
        logical_plan.create(common, replace(record, stages=stages, slices=logical_plan.derive_slices(key, stages)))
        self.first, self.second = (s.stage_id for s in source.stages())
        self.state_path = self.managed.sparring_dir / "plans" / f"{key}.json"
        state = PlanRunState.load(self.state_path)
        state.scope = {"logical_key": key, "stage_ids": [self.first]}
        state.status = PlanRunStatus.COMPLETE
        state.awaiting = None
        state.save(self.state_path)

    def accept(self, stage_id):
        stage = Stage.resolve(self.managed.sparring_dir, stage_id)
        if not stage.exists():
            stage.create(run=self.managed.run_key)
        state = stage.read_state()
        state.status = StageStatus.ACCEPTED
        state.candidate_sha = _git(self.worktree, "rev-parse", "HEAD")
        state.run = self.managed.run_key
        stage.write_state(state)

    def run_check(self):
        finish = managed_finish.finish_status(self.repo, self.managed.run_key)["finish"]
        (check,) = [c for c in finish["checks"] if c["code"] == "run_not_complete"]
        return check

    def test_eligible_once_every_stage_in_scope_is_accepted(self):
        self.assertFalse(self.run_check()["ok"])
        self.accept(self.first)
        check = self.run_check()
        self.assertTrue(check["ok"], check)
        self.assertFalse(Stage.resolve(self.managed.sparring_dir, self.second).exists())

    def test_refused_while_a_stage_in_scope_is_not_accepted(self):
        stage = Stage.resolve(self.managed.sparring_dir, self.first)
        if not stage.exists():
            stage.create(run=self.managed.run_key)
        check = self.run_check()
        self.assertFalse(check["ok"])
        self.assertIn(self.first, check["detail"])

    def test_a_gone_input_is_read_from_the_logical_snapshot_when_it_is_the_executed_plan(self):
        self.accept(self.first)
        Path(self.managed.input_path).unlink()
        self.assertTrue(self.run_check()["ok"])
        state = PlanRunState.load(self.state_path)
        state.plan_digest = "0" * 64
        state.save(self.state_path)
        check = self.run_check()
        self.assertFalse(check["ok"])
        self.assertIn("logical snapshot is not the plan", check["detail"])


class ScopedFinishSliceMismatchTests(ScopedFinishTests):
    """One slice holding both stages: a state scoped to less, or to the same
    stages in another order, is not that slice."""

    def setUp(self):
        _ManagedRepoTestCase.setUp(self)
        self._start_managed()
        (self.managed,) = self._records()
        self.worktree = Path(self.managed.worktree_path)
        key = self.managed.run_key
        common = managed_run.git_common_dir(self.repo)
        input_path = Path(self.managed.input_path)
        snapshot, sha = logical_plan.snapshot_input(common, key, input_path)
        source = load_markdown_source(input_path, self.managed.plan_label, namespace=key)
        record = logical_plan.new_record(
            logical_key=key, source=source, input_path=input_path, home="repo",
            repositories=(RepositoryBinding("repo", str(self.repo.resolve()), str(common), "repo"),),
            snapshot=snapshot, snapshot_sha256=sha,
        )
        logical_plan.create(common, record)
        self.first, self.second = (s.stage_id for s in source.stages())
        self.state_path = self.managed.sparring_dir / "plans" / f"{key}.json"

    def scope(self, stage_ids):
        state = PlanRunState.load(self.state_path)
        state.scope = {"logical_key": self.managed.run_key, "stage_ids": stage_ids}
        state.status = PlanRunStatus.COMPLETE
        state.awaiting = None
        state.save(self.state_path)

    def test_eligible_once_every_stage_in_scope_is_accepted(self):
        self.scope([self.first, self.second])
        self.accept(self.first)
        self.assertFalse(self.run_check()["ok"])
        self.accept(self.second)
        self.assertTrue(self.run_check()["ok"], self.run_check())

    def test_refused_while_a_stage_in_scope_is_not_accepted(self):
        self.scope([self.first, self.second])
        self.accept(self.second)
        check = self.run_check()
        self.assertFalse(check["ok"])
        self.assertIn(self.first, check["detail"])

    def test_a_gone_input_is_read_from_the_logical_snapshot_when_it_is_the_executed_plan(self):
        self.scope([self.first, self.second])
        self.accept(self.first)
        self.accept(self.second)
        Path(self.managed.input_path).unlink()
        self.assertTrue(self.run_check()["ok"])

    def test_a_scope_omitting_a_stage_of_the_slice_is_refused(self):
        self.accept(self.first)
        self.scope([self.first])
        check = self.run_check()
        self.assertFalse(check["ok"])
        self.assertIn("scope", check["detail"])

    def test_a_reordered_scope_is_refused(self):
        self.accept(self.first)
        self.accept(self.second)
        self.scope([self.second, self.first])
        check = self.run_check()
        self.assertFalse(check["ok"])
        self.assertIn("scope", check["detail"])


class SingleRepositoryBytesTests(_PlanRepoTestCase):
    def test_run_state_and_record_files_are_unchanged_for_a_whole_plan_run(self):
        state = PlanRunState(
            plan="docs/plan.md", plan_digest="d" * 64, expected_branch="feature/x", current_stage_index=0,
            current_stage="s", status=PlanRunStatus.RUNNING, run="r", managed=True,
        )
        state.save(self.state_path)
        before = self.state_path.read_bytes()
        self.assertEqual(
            set(json.loads(before)),
            {
                "awaiting", "current_stage", "current_stage_index", "deferred_human_checks", "expected_branch",
                "managed", "plan", "plan_digest", "push_authorization", "run", "source", "status",
            },
        )
        PlanRunState.load(self.state_path).save(self.state_path)
        self.assertEqual(self.state_path.read_bytes(), before)

        record = ManagedRunRecord(
            run_key="r", plan_label="docs/plan.md", input_kind="markdown", input_path=str(self.plan_path.resolve()),
            worktree_path=str(self.repo.resolve()), branch="b", target_branch="main", base_sha="a" * 40,
            remote=None, created_at="2026-10-09T00:00:00Z",
        )
        payload = record.to_dict()
        self.assertNotIn("slice", payload)
        self.assertEqual(
            set(payload),
            {
                "schema_version", "run_key", "plan_label", "input", "worktree_path", "branch", "target_branch",
                "base_sha", "remote", "created_at", "created_by", "project_dir", "events",
            },
        )
        self.assertEqual(ManagedRunRecord.from_dict(payload), record)

    def test_the_slice_key_round_trips_and_is_validated(self):
        record = ManagedRunRecord(
            run_key="r-s2", plan_label="p", input_kind="markdown", input_path="/x/p.md", worktree_path="/x",
            branch="b", target_branch="main", base_sha="a" * 40, remote=None, created_at="t",
            logical_slice={"logical_key": "r", "home_common_dir": "/home/.git", "index": 2},
        )
        self.assertEqual(ManagedRunRecord.from_dict(record.to_dict()), record)
        for bad in ({"logical_key": "r"}, {"logical_key": "r", "home_common_dir": "rel", "index": 2}):
            payload = {**record.to_dict(), "slice": bad}
            with self.assertRaises(ManagedRunError):
                ManagedRunRecord.from_dict(payload)
        self.assertIn("rescoped", managed_run.EVENTS)

    def test_a_malformed_scope_is_refused(self):
        payload = PlanRunState(
            plan="p", plan_digest="d", expected_branch="b", current_stage_index=0, current_stage="s",
            status=PlanRunStatus.RUNNING,
        ).to_dict()
        for bad in ({"logical_key": "k"}, {"logical_key": "k", "stage_ids": []}, None):
            with self.assertRaises(PlanError):
                PlanRunState.from_dict({**payload, "scope": bad})
