"""Manifest v2: plan-declared gates between stages and before completion.

v2 adds ``gates_before`` per stage and top-level ``completion_gates``. The
runner stops before creating a gated stage (or before reporting COMPLETE),
mints one engine-owned obligation per gate, and continues only once each is
answered ``pass`` with ``--deferred-result``. Reaching a gate never satisfies
it, and v1 manifests are untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.deferred_gate import (
    DeferredAnswer,
    DeferredObligation,
    DeferredVerificationRequired,
    before_stage_checkpoint,
)
from agent_sparring.intake import (
    SourcePlan,
    all_findings,
    approve_plan,
    compile_context_for,
    parse_interpretation,
    run_prerequisites,
    slice_ancestors,
    slice_manifest_gates,
)
from agent_sparring.intake_approval import load_intake_manifest
from agent_sparring.manifest import ManifestError, manifest_digest, parse_manifest
from agent_sparring.plan import PlanError, PlanRunStatus
from agent_sparring.stage import StageStatus
from test_intake import FakeAdapter, make_repo
from test_intake import lines as plan_lines
from test_intake import stage as intake_stage
from test_manifest import (
    _ManifestRepoTestCase,
    _stage_entry,
    manifest_payload,
)
from test_plan import READY, _SparringAdapter, _StageAdapter

S3C, S3D = "stage-3c-cloud-schema", "stage-3d-snapshot-v2"

CANARY = {"id": "canary", "title": "Staging canary", "kind": "production", "reason": "writes live data"}
CLOSEOUT = {"id": "closeout", "title": "Owner sign-off", "kind": "manual", "reason": "owner accepts the release"}


def gated_payload(*, before=(CANARY,), completion=()) -> dict:
    payload = manifest_payload()
    payload["version"] = 2
    if before:
        payload["stages"][1]["gates_before"] = [dict(g) for g in before]
    if completion:
        payload["completion_gates"] = [dict(g) for g in completion]
    return payload


class ManifestV2ParsingTests(unittest.TestCase):
    def test_v1_is_unchanged_and_refuses_gate_fields(self):
        v1 = parse_manifest(json.dumps(manifest_payload()))
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.completion_gates, ())
        payload = manifest_payload()
        payload["stages"][1]["gates_before"] = [CANARY]
        with self.assertRaisesRegex(ManifestError, "unknown field"):
            parse_manifest(json.dumps(payload))
        payload = manifest_payload()
        payload["completion_gates"] = [CLOSEOUT]
        with self.assertRaisesRegex(ManifestError, "unknown field"):
            parse_manifest(json.dumps(payload))

    def test_v2_parses_gates(self):
        manifest = parse_manifest(json.dumps(gated_payload(completion=(CLOSEOUT,))))
        self.assertEqual(manifest.version, 2)
        self.assertEqual([g.id for g in manifest.stages[1].gates_before], ["canary"])
        self.assertEqual(manifest.stages[0].gates_before, ())
        self.assertEqual([g.id for g in manifest.completion_gates], ["closeout"])

    def test_v2_accepts_a_gate_before_the_first_stage(self):
        payload = gated_payload(before=())
        payload["stages"][0]["gates_before"] = [CANARY]
        manifest = parse_manifest(json.dumps(payload))
        self.assertEqual([g.id for g in manifest.stages[0].gates_before], ["canary"])

    def test_v2_must_use_a_gate(self):
        payload = manifest_payload()
        payload["version"] = 2
        with self.assertRaisesRegex(ManifestError, "at least one gate"):
            parse_manifest(json.dumps(payload))

    def test_v2_refuses_malformed_gates(self):
        for mutate, message in (
            (lambda p: p.__setitem__("completion_gates", [CANARY]), "repeated"),
            (lambda p: p["stages"][1]["gates_before"][0].pop("reason"), "reason"),
            (lambda p: p["stages"][1]["gates_before"][0].__setitem__("extra", 1), "unknown field"),
            (lambda p: p["stages"][1].__setitem__("gates_before", {}), "array"),
        ):
            payload = gated_payload()
            mutate(payload)
            with self.subTest(message), self.assertRaisesRegex(ManifestError, message):
                parse_manifest(json.dumps(payload))

    def test_v2_digest_covers_every_gate_field_and_position(self):
        base = manifest_digest(parse_manifest(json.dumps(gated_payload(completion=(CLOSEOUT,)))))
        variants = []
        for key in ("id", "title", "kind", "reason"):
            payload = gated_payload(completion=(CLOSEOUT,))
            payload["stages"][1]["gates_before"][0][key] += "x"
            variants.append(payload)
        variants.append(gated_payload(before=(), completion=(CANARY, CLOSEOUT)))  # moved
        variants.append(gated_payload(completion=()))  # removed
        variants.append(gated_payload(before=(CANARY, CLOSEOUT)))  # closeout moved before a stage
        digests = {manifest_digest(parse_manifest(json.dumps(p))) for p in variants}
        self.assertEqual(len(digests), len(variants))
        self.assertNotIn(base, digests)

    def test_v1_digest_is_unchanged(self):
        # A pinned value: v1 runs recorded before v2 existed keep resuming.
        manifest = parse_manifest(json.dumps(manifest_payload()))
        from agent_sparring.plan_model import digest_planned_stages

        parts = ["1", manifest.plan_label, manifest.source_digest]
        for stage in manifest.stages:
            parts += [stage.stage_id, stage.label, stage.title, stage.brief]
        self.assertEqual(manifest_digest(manifest), digest_planned_stages(*parts))


class GateRunTests(_ManifestRepoTestCase):
    def setUp(self):
        super().setUp()
        self.stage_adapter = _StageAdapter(self.repo, commit=True)

    def _gated(self, **kwargs):
        self.write_manifest(gated_payload(**kwargs))

    def _turns(self):
        return len(self.stage_adapter.start_calls) + len(self.stage_adapter.resume_calls)

    def test_accept_pause_answer_next_stage(self):
        self._gated()
        reports: list[str] = []
        result = self._start(self.stage_adapter, _SparringAdapter([READY]), report=reports.append)

        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual([sid for sid, _ in result.accepted], [S3C])
        self.assertIsInstance(result.awaiting, DeferredVerificationRequired)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.BEFORE_STAGE)
        # Paused before the gated stage was created.
        self.assertFalse(self._stage(S3D).directory.exists())
        state = self._plan_state()
        (owed,) = state.deferred_human_checks
        self.assertTrue(owed.manifest_gate)
        self.assertEqual(owed.checkpoint, before_stage_checkpoint(S3D))
        self.assertEqual(owed.stage_id, S3C)
        self.assertEqual([c.id for c in owed.gate.checks], ["canary"])
        self.assertEqual(result.awaiting.instance_ids, (owed.instance_id,))
        self.assertIn("before the next stage may start", "\n".join(reports))

        # A plain resume re-derives the same pause, minting nothing new.
        again = self._resume(self.stage_adapter, _SparringAdapter([]))
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        self.assertEqual(
            [o.instance_id for o in self._plan_state().deferred_human_checks], [owed.instance_id]
        )

        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([READY]),
            deferred_results=(DeferredAnswer.parse(f"{owed.instance_id}:canary=pass=canary green"),),
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertIs(self._stage(S3D).read_state().status, StageStatus.ACCEPTED)
        self.assertIn("canary green", self._stage(S3C).read_notes())

    def test_a_live_write_gate_is_never_satisfied_by_reaching_it(self):
        # A production gate pauses like every other kind; nothing but an
        # explicit pass answer authorizes the next stage.
        self._gated()
        self._start(self.stage_adapter, _SparringAdapter([READY]))
        for _ in range(2):
            result = self._resume(self.stage_adapter, _SparringAdapter([]))
            self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertFalse(self._stage(S3D).directory.exists())
        self.assertEqual(self._turns(), 1)

    def test_fail_and_blocked_keep_the_run_stopped(self):
        self._gated()
        self._start(self.stage_adapter, _SparringAdapter([READY]))
        for outcome in ("blocked", "fail=canary red"):
            result = self._resume(
                self.stage_adapter,
                _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse(f"canary={outcome}"),),
            )
            self.assertIs(result.status, PlanRunStatus.PAUSED, outcome)
            self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.BEFORE_STAGE)
            self.assertFalse(self._stage(S3D).directory.exists())
        # Fixed, then answered pass: the run continues.
        result = self._resume(
            self.stage_adapter,
            _SparringAdapter([READY]),
            deferred_results=(DeferredAnswer.parse("canary=pass"),),
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

    def test_evidence_is_refused_at_a_before_stage_pause(self):
        self._gated()
        self._start(self.stage_adapter, _SparringAdapter([READY]))
        before = self._plan_state().to_dict()
        with self.assertRaisesRegex(PlanError, "--deferred-result"):
            self._resume(self.stage_adapter, _SparringAdapter([]), evidence="the canary looked fine")
        self.assertEqual(self._plan_state().to_dict(), before)
        self.assertNotIn("the canary looked fine", self._stage(S3C).read_notes())

    def test_a_first_stage_gate_pauses_before_anything_is_created(self):
        payload = gated_payload(before=())
        payload["stages"][0]["gates_before"] = [CANARY]
        payload["completion_gates"] = [CLOSEOUT]  # keep it a valid v2 with one extra gate
        self.write_manifest(payload)
        result = self._start(self.stage_adapter, _SparringAdapter([]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.BEFORE_STAGE)
        self.assertEqual(result.accepted, ())
        self.assertFalse(self._stage(S3C).directory.exists())
        self.assertEqual(self._turns(), 0)
        (owed,) = self._plan_state().deferred_human_checks
        self.assertEqual(owed.checkpoint, before_stage_checkpoint(S3C))
        self.assertEqual(owed.stage_id, S3C)

        with self.assertRaisesRegex(PlanError, "--deferred-result"):
            self._resume(self.stage_adapter, _SparringAdapter([]), evidence="looks fine")
        for outcome in ("blocked", "fail"):
            again = self._resume(
                self.stage_adapter, _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse(f"canary={outcome}"),),
            )
            self.assertIs(again.status, PlanRunStatus.PAUSED)
            self.assertFalse(self._stage(S3C).directory.exists())

        result = self._resume(
            self.stage_adapter, _SparringAdapter([READY, READY]),
            deferred_results=(DeferredAnswer.parse("canary=pass=green"),),
        )
        # The answer is durable in the run ledger without any stage notes.
        state = self._plan_state()
        self.assertTrue(state.obligation(owed.instance_id).resolved)
        self.assertEqual(state.obligation(owed.instance_id).results[0].note, "green")
        # Ran both stages, then stopped at the closeout gate.
        self.assertEqual([sid for sid, _ in result.accepted], [S3C, S3D])
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)

    def test_closeout_gate_blocks_complete_until_pass(self):
        self._gated(before=(), completion=(CLOSEOUT,))
        result = self._start(self.stage_adapter, _SparringAdapter([READY, READY]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        self.assertEqual([sid for sid, _ in result.accepted], [S3C, S3D])
        (owed,) = self._plan_state().deferred_human_checks
        self.assertEqual(owed.stage_id, S3D)

        result = self._resume(
            self.stage_adapter, _SparringAdapter([]),
            deferred_results=(DeferredAnswer.parse("closeout=fail=not yet"),),
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        result = self._resume(
            self.stage_adapter, _SparringAdapter([]),
            deferred_results=(DeferredAnswer.parse("closeout=pass"),),
        )
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self._turns(), 2)

    def test_a_gate_edit_changes_the_digest_and_refuses_resume(self):
        self._gated()
        self._start(self.stage_adapter, _SparringAdapter([READY]))
        payload = gated_payload()
        payload["stages"][1]["gates_before"][0]["reason"] = "reworded"
        self.write_manifest(payload)
        with self.assertRaisesRegex(PlanError, "executable content"):
            self._resume(
                self.stage_adapter, _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse("canary=pass"),),
            )

    def test_v1_manifest_runs_exactly_as_before(self):
        result = self._start(self.stage_adapter, _SparringAdapter([READY, READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self._plan_state().deferred_human_checks, ())

    def test_an_obligation_recorded_before_v2_loads_unchanged(self):
        payload = DeferredObligation.from_dict(
            {
                "stage_id": S3C,
                "gate": {
                    "category": "OTHER",
                    "title": "t",
                    "instance_id": "abc",
                    "checks": [{"id": "c", "instruction": "i", "pass_criteria": "p", "source": None}],
                },
                "rationale": "r",
                "checkpoint": "before_plan_completion",
                "promoted": False,
                "results": [],
            }
        )
        self.assertFalse(payload.manifest_gate)
        self.assertNotIn("manifest_gate", payload.to_dict())


GATED_PLAN = """\
# Gated

## Stage 1 — First

Repos: app. Build the first part.

## Stage 2 — Second

Repos: app. Build the second part.

## Canary

Run a manual canary after Stage 1, before Stage 2, with the owner's go-ahead.
"""


def _gated_interpretation(*, gates=True) -> dict:
    def stage(label, title, first, last, **extra):
        entry = intake_stage(label, title, "## Stage 0", None, **extra)
        entry["source_ranges"] = [plan_lines(first, last, text=GATED_PLAN)]
        entry["context_ids"] = []
        return entry

    return {
        "verdict": "executable_as_written",
        "summary": "One slice with a canary between its stages.",
        "context": [],
        "runs": [
            {
                "id": "app",
                "primary_repository": "app",
                "expected_branch": None,
                "rationale": "",
                "stages": [
                    stage("1", "First", "## Stage 1", "Build the first"),
                    stage("2", "Second", "## Stage 2", "Build the second", depends_on=["1"]),
                ],
            }
        ],
        "gates": [
            {
                "id": "canary",
                "title": "Owner canary",
                "kind": "manual",
                "ranges": [plan_lines("## Canary", "Run a manual canary", text=GATED_PLAN)],
                "after_stage": "1",
                "blocks_stages": ["2"],
                "reason": "the owner checks the canary",
            }
        ]
        if gates
        else [],
        "excluded": [{"ranges": [plan_lines("# Gated", text=GATED_PLAN)], "reason": "title"}],
        "findings": [],
        "amended_plan": None,
    }


class IntakeGateRenderingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name) / "app", "feature/gated", {"docs/plan.md": GATED_PLAN})
        self.sparring_dir = self.repo / ".sparring"

    def _check(self, payload, mode):
        source = SourcePlan(label="docs/plan.md", text=GATED_PLAN)
        interpretation = parse_interpretation(json.dumps(payload), mode=mode)
        context = (
            compile_context_for(interpretation, source, project_context=None, primary_repository="app", inspected={"app"})
            if mode == "compile"
            else None
        )
        return interpretation, all_findings(interpretation, source, mode=mode, compile_context=context)

    def test_faithful_mode_still_refuses_a_gate_inside_a_slice(self):
        interpretation, findings = self._check(_gated_interpretation(), "faithful")
        self.assertIn("gate_inside_run", {f.code for f in findings})
        self.assertEqual(slice_manifest_gates(interpretation, "app"), ({}, ()))
        self.assertEqual(run_prerequisites(interpretation, "app"), ("canary",))

    def test_compile_mode_renders_it_as_gates_before(self):
        interpretation, findings = self._check(_gated_interpretation(), "compile")
        self.assertEqual([f for f in findings if f.blocking], [])
        before, completion = slice_manifest_gates(interpretation, "app")
        self.assertEqual({label: [g.id for g in gates] for label, gates in before.items()}, {"2": ["canary"]})
        self.assertEqual(completion, ())
        self.assertEqual(run_prerequisites(interpretation, "app"), ())

    def test_a_gate_blocking_the_first_and_a_later_stage_lands_before_the_first(self):
        payload = _gated_interpretation()
        payload["gates"][0]["after_stage"] = None
        payload["gates"][0]["blocks_stages"] = ["1", "2"]
        interpretation = parse_interpretation(json.dumps(payload), mode="compile")
        before, completion = slice_manifest_gates(interpretation, "app")
        self.assertEqual({label: [g.id for g in gates] for label, gates in before.items()}, {"1": ["canary"]})
        self.assertEqual(completion, ())
        # Carried by the manifest, so not an approval prerequisite -- and the
        # run stops before Stage 1, so nothing executes unauthorized.
        self.assertEqual(run_prerequisites(interpretation, "app"), ())

    def test_a_gate_blocking_only_the_first_stage_is_a_manifest_gate(self):
        payload = _gated_interpretation()
        payload["gates"][0]["after_stage"] = None
        payload["gates"][0]["blocks_stages"] = ["1"]
        interpretation = parse_interpretation(json.dumps(payload), mode="compile")
        before, _ = slice_manifest_gates(interpretation, "app")
        self.assertEqual(list(before), ["1"])
        self.assertEqual(run_prerequisites(interpretation, "app"), ())

    def test_a_gate_carried_into_a_later_slice_still_requires_the_slice_it_follows(self):
        # "After Stage 1, before Stage 2" with the stages in separate slices
        # and no structured dependency between them: the second slice stops
        # for the gate, but it must not be approvable before the first ran.
        payload = _gated_interpretation()
        first, second = payload["runs"][0]["stages"]
        second["depends_on"] = []
        payload["runs"] = [
            {**payload["runs"][0], "id": "one", "stages": [first]},
            {**payload["runs"][0], "id": "two", "stages": [second]},
        ]
        interpretation = parse_interpretation(json.dumps(payload), mode="compile")
        before, _ = slice_manifest_gates(interpretation, "two")
        self.assertEqual({label: [g.id for g in gates] for label, gates in before.items()}, {"2": ["canary"]})
        self.assertEqual(run_prerequisites(interpretation, "two"), ("one",))
        self.assertEqual(run_prerequisites(interpretation, "one"), ())

    def test_completion_gate_remains_a_manifest_obligation_not_a_slice_prerequisite(self):
        payload = _gated_interpretation()
        payload["gates"][0].update(after_stage="2", blocks_stages=[])
        interpretation = parse_interpretation(json.dumps(payload), mode="compile")
        before, completion = slice_manifest_gates(interpretation, "app")
        self.assertEqual(before, {})
        self.assertEqual([gate.id for gate in completion], ["canary"])
        self.assertEqual(run_prerequisites(interpretation, "app"), ())
        self.assertEqual(slice_ancestors(interpretation, "app"), ())

    def test_gate_and_explicit_slice_prerequisites_have_transitive_closure(self):
        payload = _gated_interpretation()
        first, second = payload["runs"][0]["stages"]
        second["depends_on"] = []
        third = {**second, "label": "3", "depends_on": ["2"]}
        payload["runs"] = [
            {**payload["runs"][0], "id": "one", "stages": [first]},
            {**payload["runs"][0], "id": "two", "stages": [second]},
            {**payload["runs"][0], "id": "three", "stages": [third]},
        ]
        for mode in ("compile", "faithful"):
            with self.subTest(mode=mode):
                interpretation = parse_interpretation(json.dumps(payload), mode=mode)
                self.assertEqual(run_prerequisites(interpretation, "one"), ())
                self.assertEqual(run_prerequisites(interpretation, "two"),
                                 ("one",) if mode == "compile" else ("canary", "one"))
                self.assertEqual(run_prerequisites(interpretation, "three"), ("two",))
                self.assertEqual(slice_ancestors(interpretation, "three"), ("one", "two"))
                self.assertEqual(slice_ancestors(interpretation, "one"), ())

    def _prepare_and_approve(self, payload):
        from agent_sparring.intake import prepare_plan

        result = prepare_plan(
            self.repo / "docs" / "plan.md", self.repo, self.sparring_dir, FakeAdapter(payload),
            mode="compile", primary_repository="app", context_repositories={},
            project_context=None, mint_run_key=lambda label: "app-run-0001",
        )
        self.assertEqual(result.blocking, (), [f.message for f in result.blocking])
        approval = approve_plan(
            result.directory, run_id="app", primary_repository="app",
            repo_root=self.repo, sparring_dir=self.sparring_dir,
        )
        return result, load_intake_manifest(approval.manifest_path)

    def test_approve_emits_v2_without_confirming_the_gate(self):
        result, source = self._prepare_and_approve(_gated_interpretation())
        self.assertEqual(source.manifest.version, 2)
        self.assertEqual([g.id for g in source.stages()[1].gates_before], ["canary"])
        self.assertIn("The run stops before stage 2 for: `canary`", (result.directory / "report.md").read_text(encoding="utf-8"))


# The ingestion-provenance plan's own wording, reduced: G1 is evidence, G1C
# is the Stage 2 go-ahead that must follow it, and the Stage 1 start gate
# only restates approving the run.
START = {
    "id": "managed-run-approval",
    "title": "Start managed execution with the explicit verification and push policy",
    "kind": "authorization",
    "reason": "approving the run must include the commit-before-manual-gate policy",
}
G1 = {"id": "G1", "title": "API and access checks", "kind": "manual",
      "reason": "the operator verifies the accepted migration on a disposable target"}
G1C = {"id": "G1C", "title": "Permission to continue with image ingestion", "kind": "authorization",
       "reason": "requires a distinct Stage 2 go-ahead after G1 passes"}
PROD = {"id": "prod", "title": "Authorize the production migration", "kind": "production",
        "reason": "a fresh, specific production authorization"}


class AuthorizationGateRunTests(_ManifestRepoTestCase):
    """One informed authorization per consequential decision: the approved
    start of a run slice is the go-ahead for its stages, a go-ahead that
    depends on verification is asked once after it passes, and decisions are
    never presented as tests."""

    def setUp(self):
        super().setUp()
        self.stage_adapter = _StageAdapter(self.repo, commit=True)

    def _first_stage_gated(self, gates, completion=(CLOSEOUT,)):
        payload = gated_payload(before=(), completion=completion)
        payload["stages"][0]["gates_before"] = [dict(g) for g in gates]
        self.write_manifest(payload)

    def _turns(self):
        return len(self.stage_adapter.start_calls) + len(self.stage_adapter.resume_calls)

    def _owed(self, gate_id):
        return next(
            entry for entry in self._plan_state().deferred_human_checks
            if any(check.id == gate_id for check in entry.gate.checks)
        )

    def test_the_approved_start_satisfies_a_go_ahead_for_its_own_first_stage(self):
        self._first_stage_gated([START])
        reports: list[str] = []
        result = self._start(self.stage_adapter, _SparringAdapter([READY, READY]), report=reports.append)
        # No pause before Stage 1: the run ran to its closeout gate.
        self.assertEqual([sid for sid, _ in result.accepted], [S3C, S3D])
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        start = self._owed("managed-run-approval")
        self.assertTrue(start.resolved)
        self.assertEqual(start.gate.category, "AUTHORIZATION")
        self.assertIn("approved start", start.results[0].note)
        self.assertIn("satisfied by the approved start", "\n".join(reports))
        # It authorized starting, not pushing: push stays the engine's own gate.
        self.assertIsNone(self._plan_state().push_authorization)

    def test_g1c_is_not_asked_while_g1_has_not_passed(self):
        # The slice that starts at Stage 2: its start predates G1's evidence.
        self._first_stage_gated([G1, G1C])
        result = self._start(self.stage_adapter, _SparringAdapter([]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        (g1,) = self._plan_state().deferred_human_checks
        self.assertEqual([c.id for c in g1.gate.checks], ["G1"])
        self.assertEqual(g1.gate.category, "OTHER")
        self.assertEqual(result.awaiting.instance_ids, (g1.instance_id,))
        for outcome in ("blocked", "fail=anonymous read allowed"):
            again = self._resume(
                self.stage_adapter, _SparringAdapter([]),
                deferred_results=(DeferredAnswer.parse(f"G1={outcome}"),),
            )
            self.assertIs(again.status, PlanRunStatus.PAUSED, outcome)
            # Never offered, never satisfied by the earlier start.
            self.assertEqual(len(self._plan_state().deferred_human_checks), 1)
        self.assertFalse(self._stage(S3C).directory.exists())
        self.assertEqual(self._turns(), 0)

    def test_g1c_is_one_scoped_decision_after_g1_passes(self):
        self._first_stage_gated([G1, G1C])
        self._start(self.stage_adapter, _SparringAdapter([]))
        result = self._resume(
            self.stage_adapter, _SparringAdapter([]),
            deferred_results=(DeferredAnswer.parse("G1=pass=matrix green"),),
        )
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        g1c = self._owed("G1C")
        self.assertEqual(result.awaiting.instance_ids, (g1c.instance_id,))
        self.assertEqual(g1c.gate.category, "AUTHORIZATION")
        self.assertEqual(g1c.results, ())
        (check,) = g1c.gate.checks
        self.assertIn("Decide whether to authorize continuing into Stage 3C", check.instruction)
        self.assertIn("not any later stage, production change or publication", check.pass_criteria)
        self.assertFalse(self._stage(S3C).directory.exists())
        # Not now keeps the run stopped, and nothing else is asked.
        again = self._resume(
            self.stage_adapter, _SparringAdapter([]),
            deferred_results=(DeferredAnswer.parse("G1C=blocked"),),
        )
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        self.assertEqual(again.awaiting.instance_ids, (g1c.instance_id,))
        # Continue: one answer, and the run proceeds.
        result = self._resume(
            self.stage_adapter, _SparringAdapter([READY, READY]),
            deferred_results=(DeferredAnswer.parse("G1C=pass"),),
        )
        self.assertEqual([sid for sid, _ in result.accepted], [S3C, S3D])
        self.assertEqual(len(self._plan_state().deferred_human_checks), 3)

    def test_a_go_ahead_for_a_later_stage_is_asked_not_assumed(self):
        self.write_manifest(gated_payload(before=(G1C,)))
        result = self._start(self.stage_adapter, _SparringAdapter([READY]))
        self.assertEqual([sid for sid, _ in result.accepted], [S3C])
        g1c = self._owed("G1C")
        self.assertFalse(g1c.resolved)
        self.assertEqual(g1c.gate.category, "AUTHORIZATION")
        self.assertFalse(self._stage(S3D).directory.exists())

    def test_verification_and_production_gates_are_never_satisfied_by_the_start(self):
        for gate, category in ((G1, "OTHER"), (PROD, "OTHER")):
            with self.subTest(gate=gate["id"]):
                self.setUp()
                self._first_stage_gated([gate])
                result = self._start(self.stage_adapter, _SparringAdapter([]))
                self.assertIs(result.status, PlanRunStatus.PAUSED)
                owed = self._owed(gate["id"])
                self.assertFalse(owed.resolved)
                self.assertEqual(owed.gate.category, category)
                self.assertIn("is satisfied", owed.gate.checks[0].instruction)
                self.assertFalse(self._stage(S3C).directory.exists())

    def test_a_completion_go_ahead_is_asked(self):
        self.write_manifest(gated_payload(before=(), completion=(G1C,)))
        result = self._start(self.stage_adapter, _SparringAdapter([READY, READY]))
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.PLAN_COMPLETION)
        self.assertFalse(self._owed("G1C").resolved)
        self.assertIn("completing this run", self._owed("G1C").gate.checks[0].instruction)


class AuthorizationGateIntakeTests(unittest.TestCase):
    def test_authorization_is_a_gate_kind_the_manifest_carries(self):
        payload = _gated_interpretation()
        payload["gates"][0]["kind"] = "authorization"
        interpretation = parse_interpretation(json.dumps(payload), mode="compile")
        before, _ = slice_manifest_gates(interpretation, "app")
        self.assertEqual([g.kind for g in before["2"]], ["authorization"])

    def test_moving_a_gate_keeps_its_family(self):
        from agent_sparring.intake import _GATE_KIND_EVIDENCE, _GATE_KIND_FAMILY

        def family(text):
            return {_GATE_KIND_FAMILY.get(k, k) for k, ev in _GATE_KIND_EVIDENCE.items() if ev.search(text)}

        self.assertEqual(family("The user explicitly gives the Stage 2 go-ahead."), {"manual"})
        self.assertEqual(family("Manual approval of the owner canary."), {"manual"})
        self.assertEqual(family("Deploy the release to production."), {"production"})

    def test_the_interpreter_is_told_which_approvals_the_engine_already_enforces(self):
        from agent_sparring.intake_prompt import _INTERPRETATION

        text = " ".join(_INTERPRETATION.split())
        self.assertIn("approving and starting the prepared run slice", text)
        self.assertIn("candidate push authorization", text)
        self.assertIn("raise a finding instead of inventing an approval gate", text)
        self.assertIn("continuing into a later stage, production changes, publication", text)


if __name__ == "__main__":
    unittest.main()
