"""Compile-mode intake: dispositions, guarded transforms and the new guards.

The fixture plans under ``tests/fixtures/plans/`` have the shapes compile
mode exists for -- labels ``3A``/``3B`` in the wrong document order, a review
barrier inside a stage, plan-wide invariants, a conditional canary and an
optional sibling. The agent turn is a recorded interpretation built here.
"""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.intake import (
    COMPILE_INTERPRETATION_SCHEMA,
    INTERPRETATION_SCHEMA,
    CompileContext,
    IntakeError,
    SourcePlan,
    all_findings,
    approve_plan,
    check_interpretation,
    compile_context_for,
    parse_interpretation,
    names_repository,
    prepare_plan,
    states_gate,
)
from agent_sparring.intake_approval import load_intake_manifest
from agent_sparring.providers import SparringAgentResult
from test_intake import PLAN, _Repo, good_interpretation, lines

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
CLOUD = (FIXTURES / "cloud_sync_like.md").read_text(encoding="utf-8")
CONTRADICTORY = (FIXTURES / "cloud_sync_contradictory.md").read_text(encoding="utf-8")
COMPILER = (FIXTURES / "run_plan_compiler.md").read_text(encoding="utf-8")
CONTEXT = CompileContext(named=frozenset({"app", "web"}), inspected=frozenset({"app", "web"}))
ALL = ["1", "2", "3A", "3B", "4", "4R", "5"]


def until(text: str, first: str, stop: str | None = None) -> dict:
    """From the line containing ``first`` to the last non-blank line before
    the next line containing ``stop`` (or the end of the document)."""

    numbered = text.splitlines()
    start = next(i for i, line in enumerate(numbered, 1) if first in line)
    end = len(numbered)
    if stop is not None:
        end = next(i for i, line in enumerate(numbered, 1) if stop in line and i > start) - 1
    while not numbered[end - 1].strip():
        end -= 1
    return {"start": start, "end": end}


def node(text, label, title, first, stop, **extra):
    base = {
        "label": label,
        "title": title,
        "source_ranges": [until(text, first, stop)],
        "context_ids": ["status", "invariants"],
        "repositories": [],
        "depends_on": [],
        "mode": "implementation",
        "intake_scope": None,
        "rationale": "",
    }
    base.update(extra)
    return base


def finding(code, disposition, stages, ranges=(), transform=None, decision=None, message="m"):
    return {
        "code": code,
        "disposition": disposition,
        "transform": transform,
        "decision": decision,
        "stages": list(stages),
        "ranges": list(ranges),
        "message": message,
    }


def auto(transform, stages, ranges=()):
    return finding("other", "auto_resolved", stages, ranges, transform=transform)


def cloud_interpretation(text: str = CLOUD) -> dict:
    """The recorded compile answer for ``cloud_sync_like.md``."""

    invariants = until(text, "## Global invariants", "## Repositories")
    return {
        "verdict": "executable_with_recommendations",
        "summary": "One run slice; 3A before 3B; Stage 4's review barrier is its own node.",
        "context": [
            {"id": "status", "title": "Status", "ranges": [until(text, "# Cloud sync", "## Global")], "reason": "authorization"},
            {"id": "invariants", "title": "Global invariants", "ranges": [invariants], "reason": "invariants"},
            {"id": "repositories", "title": "Repositories", "ranges": [until(text, "## Repositories", "---")], "reason": "sibling"},
        ],
        "runs": [
            {
                "id": "app",
                "primary_repository": "app",
                "expected_branch": None,
                "rationale": "every stage lives in app",
                "stages": [
                    node(text, "1", "Schema", "## Stage 1", "## Stage 2"),
                    node(text, "2", "Sync engine", "## Stage 2", "## Stage 3B", depends_on=["1"]),
                    node(text, "3A", "Client upload path", "## Stage 3A", "## Stage 4", depends_on=["2"]),
                    node(
                        text, "3B", "Client download path", "## Stage 3B", "## Stage 3A",
                        depends_on=["3A"], repositories=["web"],
                        context_ids=["status", "invariants", "repositories"],
                    ),
                    node(text, "4", "Conflict resolution", "## Stage 4", "### Review barrier", depends_on=["3B"]),
                    node(
                        text, "4R", "Review barrier", "### Review barrier", "## Stage 5",
                        depends_on=["4"], mode="independent_review",
                    ),
                    node(text, "5", "Cleanup", "## Stage 5", "## Canaries", depends_on=["4R"]),
                ],
            }
        ],
        "gates": [
            {
                "id": "canary",
                "title": "Conditional canary",
                "kind": "manual",
                "ranges": [until(text, "## Canaries", "## History")],
                "after_stage": None,
                "blocks_stages": ["3A"],
                "reason": "only if Stage 2's metrics regress",
            }
        ],
        "excluded": [{"ranges": [until(text, "## History")], "reason": "history"}],
        "findings": [
            auto("relabel_stage", ["3A", "3B"]),
            auto("attach_context", ALL, [invariants]),
            auto("split_review_barrier", ["4", "4R"], [lines("Before Stage 5 starts", "identity never depends", text)]),
            auto("gate_at_boundary", ["3A"], [until(text, "## Canaries", "## History")]),
            auto("reorder_within_candidate", ["3A", "3B"], [lines("Depends on Stage 3A", text=text)]),
            auto("conditional_sibling", ["3B"], [lines("If the web client parses", "otherwise `web`", text)]),
            finding("stale_baseline_fact", "plan_note", ["1"], message="re-check the current schema"),
        ],
        "amended_plan": None,
    }


def contradictory_interpretation() -> dict:
    """An answer for ``cloud_sync_contradictory.md`` that claims every
    normalization is automatic, although none of them holds."""

    text = CONTRADICTORY
    payload = cloud_interpretation(text)
    stages = {s["label"]: s for s in payload["runs"][0]["stages"]}
    # 3A loses its acceptance line (excluded instead): no longer its section.
    stages["3A"]["source_ranges"] = [until(text, "## Stage 3A", "Acceptance: uploads")]
    # Stage 5 no longer attaches the invariants the finding cites.
    stages["5"]["context_ids"] = ["status"]
    # The review barrier is dropped rather than split into a node.
    del payload["runs"][0]["stages"][5]
    stages["5"]["depends_on"] = ["4"]
    # The canary is excluded rather than kept as a gate.
    payload["gates"] = []
    # An unnamed sibling and a named one intake never inspected.
    stages["3B"]["repositories"] = ["web", "desktop", "mobile"]
    payload["excluded"] += [
        {"ranges": [lines("Acceptance: uploads", text=text)], "reason": "covered elsewhere"},
        {"ranges": [until(text, "### Review barrier", "## Stage 5")], "reason": "process"},
        {"ranges": [until(text, "## Canaries", "## History")], "reason": "conditional"},
    ]
    payload["findings"] += [
        auto("merge_stages", ["1", "2"]),
        auto(None, ["1"]),
        finding(
            "repeated_or_contradictory_requirement",
            "needs_decision",
            ["2", "5"],
            [lines("Deletions stay soft", text=text), lines("Stage 2 hard-deletes", text=text)],
            decision={
                "id": "deletion-timing",
                "question": "When are rows hard-deleted?",
                "why": "The invariants state both.",
                "options": [
                    {"id": "stage-5", "label": "Only in Stage 5", "consequence": "Stage 2 keeps soft deletes"},
                    {"id": "stage-2", "label": "Already in Stage 2", "consequence": "the soft-delete invariant is dropped"},
                ],
            },
        ),
        finding("ambiguous_source", "refuse", ["3B"], message="mobile is out of this plan's repositories"),
    ]
    payload["verdict"] = "cannot_interpret_faithfully"
    return payload


def compile_check(payload, text=CLOUD):
    """All findings, with the compile context prepare would record for an
    ``app`` project that also inspected ``web``."""

    source = SourcePlan(label="docs/cloud.md", text=text)
    interpretation = parse_interpretation(json.dumps(payload), mode="compile")
    context = compile_context_for(
        interpretation, source, project_context=None, primary_repository="app", inspected={"app", "web"}
    )
    return all_findings(interpretation, source, mode="compile", compile_context=context)


class CloudSyncFixtureTests(unittest.TestCase):
    def test_the_fixture_compiles_to_auto_resolved_and_plan_notes_only(self):
        findings = compile_check(cloud_interpretation())
        # The canary sits inside the slice: gate_at_boundary is validated
        # here but not rendered until manifest v2, so this one stays blocking.
        rest = [f for f in findings if f.code != "gate_inside_run"]
        self.assertEqual([f.code for f in findings if f.code == "gate_inside_run"], ["gate_inside_run"])
        self.assertEqual({f.disposition for f in rest}, {"auto_resolved", "plan_note"})
        self.assertEqual([f.downgraded for f in rest], [None] * len(rest))
        self.assertEqual(
            sorted(f.transform for f in rest if f.transform),
            sorted(["relabel_stage", "attach_context", "split_review_barrier", "gate_at_boundary",
                    "reorder_within_candidate", "conditional_sibling"]),
        )
        severity = {f.disposition: f.severity for f in rest}
        self.assertEqual(severity, {"auto_resolved": "info", "plan_note": "recommendation"})

    def test_the_contradictory_fixture_never_auto_resolves(self):
        findings = compile_check(contradictory_interpretation(), text=CONTRADICTORY)
        self.assertNotIn("auto_resolved", {f.disposition for f in findings})
        downgraded = [f for f in findings if f.downgraded]
        self.assertEqual(len(downgraded), 8)
        self.assertTrue(all(f.disposition == "needs_decision" and f.blocking for f in downgraded))
        engine = {f.code: f.disposition for f in findings if f.origin == "engine"}
        self.assertEqual(engine["review_requirement_unmapped"], "refuse")
        self.assertEqual(engine["gate_dropped"], "refuse")
        self.assertEqual(engine["unnamed_repository"], "needs_decision")
        self.assertEqual(engine["sibling_not_supplied"], "refuse")
        not_supplied = next(f for f in findings if f.code == "sibling_not_supplied")
        self.assertIn("--context-repository mobile=<path>", not_supplied.message)
        decided = next(f for f in findings if f.code == "repeated_or_contradictory_requirement")
        self.assertEqual(decided.decision.id, "deletion-timing")

    def test_each_transform_guard_rejects_its_own_violation(self):
        def stage(payload, label):
            return next(s for s in payload["runs"][0]["stages"] if s["label"] == label)

        def claim_of(payload, transform):
            return next(f for f in payload["findings"] if f["transform"] == transform)

        def relabel(p):  # the heading says 3A, the node now claims 3C
            stage(p, "3A")["label"] = "3C"
            stage(p, "3B")["depends_on"] = ["3C"]
            p["gates"][0]["blocks_stages"] = ["3C"]
            for f in p["findings"]:
                f["stages"] = ["3C" if s == "3A" else s for s in f["stages"]]

        broken = {
            "relabel_stage": relabel,
            "attach_context": lambda p: stage(p, "5").update(context_ids=["status"]),
            "split_review_barrier": lambda p: stage(p, "4R").update(mode="implementation"),
            "gate_at_boundary": lambda p: p["gates"][0].update(blocks_stages=["5"]),
            # cites text that belongs to neither reordered node
            "reorder_within_candidate": lambda p: claim_of(p, "reorder_within_candidate").update(
                ranges=[lines("Resolve concurrent edits", text=CLOUD)]
            ),
            "conditional_sibling": lambda p: stage(p, "3B").update(repositories=[]),
        }
        for transform, mutate in broken.items():
            with self.subTest(transform):
                payload = cloud_interpretation()
                mutate(payload)
                findings = compile_check(payload)
                claim = next(f for f in findings if f.transform == transform)
                self.assertEqual(claim.disposition, "needs_decision", claim)
                self.assertTrue(claim.downgraded)
                self.assertTrue(claim.blocking)

    def test_a_gate_kind_change_is_not_auto_resolved(self):
        payload = cloud_interpretation()
        payload["gates"][0]["kind"] = "deferred"
        claim = next(f for f in compile_check(payload) if f.transform == "gate_at_boundary")
        self.assertEqual(claim.disposition, "needs_decision")
        self.assertIn("kind 'deferred'", claim.downgraded)

    def test_a_routine_review_does_not_justify_an_independent_review_node(self):
        text = CLOUD.replace("an independent review", "a routine review")
        claim = next(f for f in compile_check(cloud_interpretation(text), text=text) if f.transform == "split_review_barrier")
        self.assertEqual(claim.disposition, "needs_decision")
        self.assertIn("independent review", claim.downgraded)

    def test_conflicting_kind_evidence_is_not_auto_resolved(self):
        text = CLOUD.replace("run a canary on 5% of devices before", "run a manual canary before the later")
        payload = cloud_interpretation(text)
        payload["gates"][0]["kind"] = "deferred"
        claim = next(f for f in compile_check(payload, text=text) if f.transform == "gate_at_boundary")
        self.assertEqual(claim.disposition, "needs_decision")
        self.assertIn("deferred, manual", claim.downgraded)

    def test_an_excluded_owner_go_ahead_refuses(self):
        text = CLOUD.replace(
            "## Canaries\n\nIf Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.",
            "## Owner approval\n\nStage 3A requires the owner's go-ahead before it\nstarts.",
        )
        self.assertIn("Owner approval", text)
        payload = cloud_interpretation()  # same line numbers: the section keeps its shape
        owner = until(text, "## Owner approval", "## History")
        payload["gates"] = []
        payload["excluded"].append({"ranges": [owner], "reason": "process"})
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        dropped = [f for f in compile_check(payload, text=text) if f.code == "gate_dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertTrue(dropped[0].blocking)

    def test_an_excluded_go_ahead_phrased_as_a_requirement_refuses(self):
        text = CLOUD.replace(
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.",
            "Stage 3A requires the owner's go-ahead.\nDo not start without it.",
        )
        payload = cloud_interpretation()  # same line numbers
        payload["gates"] = []
        payload["excluded"].append({"ranges": [until(text, "## Canaries", "## History")], "reason": "process"})
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        dropped = [f for f in compile_check(payload, text=text) if f.code == "gate_dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertTrue(dropped[0].blocking)

    def _go_ahead_moved_after_the_run(self, *, keep_claim):
        text = CLOUD.replace(
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.",
            "Stage 3A requires the owner's go-ahead.\nDo not start without it.",
        )
        payload = cloud_interpretation()  # same line numbers
        payload["gates"][0].update(after_stage="5", blocks_stages=[])
        if keep_claim:
            next(f for f in payload["findings"] if f["transform"] == "gate_at_boundary")["stages"] = ["5"]
        else:
            payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        return compile_check(payload, text=text)

    def test_a_go_ahead_turned_into_a_post_run_gate_refuses_and_is_not_auto_resolved(self):
        findings = self._go_ahead_moved_after_the_run(keep_claim=True)
        unenforced = [f for f in findings if f.code == "gate_unenforced"]
        self.assertEqual(len(unenforced), 1)
        self.assertTrue(unenforced[0].blocking)
        self.assertIn("blocks stage 3A", unenforced[0].message)
        claim = next(f for f in findings if f.transform == "gate_at_boundary")
        self.assertEqual(claim.disposition, "needs_decision")
        self.assertIn("blocks stage 3A", claim.downgraded)

    def test_a_go_ahead_gate_that_blocks_nothing_refuses_without_any_claim(self):
        findings = self._go_ahead_moved_after_the_run(keep_claim=False)
        self.assertEqual([f.code for f in findings if f.code == "gate_unenforced"], ["gate_unenforced"])

    def test_a_post_run_gate_with_no_stated_blocked_stage_may_block_nothing(self):
        text = CLOUD.replace(
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.",
            "After Stage 5, deploy the release with the owner's go-ahead.\nThat closes the plan.",
        )
        payload = cloud_interpretation()
        payload["gates"][0].update(after_stage="5", blocks_stages=[], kind="production")
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        self.assertNotIn("gate_unenforced", [f.code for f in compile_check(payload, text=text)])

    def _post_run_gate(self, sentence, **gate):
        text = CLOUD.replace(
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.", sentence
        )
        payload = cloud_interpretation()  # same line numbers
        payload["gates"][0].update(gate)
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        return compile_check(payload, text=text), payload, text

    def test_until_a_stage_completes_is_a_post_run_gate_that_follows_it(self):
        findings, _, _ = self._post_run_gate(
            "Do not deploy the release until Stage 5 is complete.\nThat closes the plan.",
            after_stage="5", blocks_stages=[], kind="production",
        )
        self.assertEqual([f.code for f in findings if f.blocking], [])

    def test_until_a_stage_still_requires_the_gate_to_follow_it(self):
        findings, _, _ = self._post_run_gate(
            "Do not deploy the release until Stage 5 is complete.\nThat closes the plan.",
            after_stage=None, blocks_stages=[], kind="production",
        )
        unenforced = [f for f in findings if f.code == "gate_unenforced"]
        self.assertEqual(len(unenforced), 1)
        self.assertIn("follows stage 5", unenforced[0].message)

    def test_a_start_prohibition_blocks_its_stage(self):
        findings, _, _ = self._post_run_gate(
            "Do not start Stage 3A without the owner's go-ahead.\nThat is final.",
            after_stage="5", blocks_stages=[],
        )
        unenforced = [f for f in findings if f.code == "gate_unenforced"]
        self.assertEqual(len(unenforced), 1)
        self.assertIn("blocks stage 3A", unenforced[0].message)

    def test_ordinary_work_mentioning_a_deployment_is_not_a_gate(self):
        text = CLOUD.replace(
            "Add the sync columns and the server row id.", "Add unit tests for the deployment configuration parser."
        )
        findings = compile_check(cloud_interpretation(text), text=text)
        self.assertNotIn("gate_dropped", [f.code for f in findings])

    def test_gate_text_carried_as_context_instead_of_a_gate_refuses(self):
        payload = cloud_interpretation()
        canary = payload["gates"].pop()["ranges"]
        payload["context"].append({"id": "canary", "title": "Canary", "ranges": canary, "reason": "moved"})
        next(s for s in payload["runs"][0]["stages"] if s["label"] == "3A")["context_ids"].append("canary")
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        dropped = [f for f in compile_check(payload) if f.code == "gate_dropped"]
        self.assertTrue(dropped)
        self.assertTrue(all(f.disposition == "refuse" and f.blocking for f in dropped))


class ShapeTests(unittest.TestCase):
    source = SourcePlan(label="docs/plan.md", text=CLOUD)

    def test_compile_schema_is_strict_and_faithful_schema_is_unchanged(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") in ("object", ["object", "null"]):
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(COMPILE_INTERPRETATION_SCHEMA)
        compiled = COMPILE_INTERPRETATION_SCHEMA["properties"]["findings"]["items"]["properties"]
        self.assertNotIn("severity", compiled)
        self.assertEqual(compiled["transform"]["enum"][-1], None)
        faithful = INTERPRETATION_SCHEMA["properties"]["findings"]["items"]["properties"]
        self.assertEqual(sorted(faithful), ["code", "message", "ranges", "severity", "stages"])

    def test_dispositions_belong_to_compile_mode_only(self):
        payload = cloud_interpretation()
        with self.assertRaisesRegex(IntakeError, "must not carry a 'disposition'"):
            parse_interpretation(json.dumps(payload), mode="faithful")
        legacy = good_interpretation()
        with self.assertRaisesRegex(IntakeError, "must carry a 'disposition'"):
            parse_interpretation(json.dumps(legacy), mode="compile")
        # Mode-less readers (slice-branch) accept either shape.
        self.assertEqual(len(parse_interpretation(json.dumps(payload)).findings), 7)

    def test_a_decision_is_only_for_needs_decision_and_offers_a_choice(self):
        decision = {"id": "d", "question": "q?", "why": "", "options": [{"id": "a", "label": "A", "consequence": "c"}]}
        payload = cloud_interpretation()
        payload["findings"] = [finding("other", "needs_decision", ["1"], decision=decision)]
        with self.assertRaisesRegex(IntakeError, "at least two options"):
            parse_interpretation(json.dumps(payload), mode="compile")
        decision["options"].append({"id": "b", "label": "B", "consequence": "c"})
        payload["findings"] = [finding("other", "plan_note", ["1"], decision=decision)]
        with self.assertRaisesRegex(IntakeError, "must not carry a decision"):
            parse_interpretation(json.dumps(payload), mode="compile")

    def test_needs_decision_without_a_decision_and_an_amendment_refuse(self):
        payload = cloud_interpretation()
        payload["findings"] = [finding("other", "needs_decision", ["1"])]
        payload["amended_plan"] = CLOUD + "\nmore\n"
        engine = check_interpretation(
            parse_interpretation(json.dumps(payload), mode="compile"), self.source, mode="compile", compile_context=CONTEXT
        )
        codes = {f.code: f.disposition for f in engine}
        self.assertEqual(codes["decision_missing"], "refuse")
        self.assertEqual(codes["amendment_in_compile_mode"], "refuse")

    def test_faithful_engine_findings_carry_no_disposition(self):
        payload = good_interpretation()
        payload["excluded"] = []
        findings = check_interpretation(
            parse_interpretation(json.dumps(payload), mode="faithful"), SourcePlan(label="docs/plan.md", text=PLAN), mode="faithful"
        )
        self.assertEqual([(f.code, f.severity, f.disposition) for f in findings], [("uncovered_source", "blocking", None)])


def compile_widget_interpretation() -> dict:
    """``test_intake``'s widget answer, as compile mode answers it."""

    payload = good_interpretation()
    payload["findings"] = [finding("stale_baseline_fact", "plan_note", ["0"], message="counts should be recomputed")]
    return payload


class CompileEndToEndTests(_Repo):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_prepare_compile_then_approve_through_the_existing_path(self):
        (self.sparring_dir / "project.toml").write_text('project = "app"\n', encoding="utf-8")
        answer = SparringAgentResult(session_id="t-1", text=json.dumps(compile_widget_interpretation()), is_error=False)
        with mock.patch("agent_sparring.cli.CodexCliAdapter.start_structured", return_value=answer) as call:
            code, out, err = self._main(
                "prepare-plan", str(self.plan), "--mode", "compile", "--repo-root", str(self.repo),
                "--context-repository", f"web={self.web}",
            )
        self.assertEqual(code, 0, err)
        self.assertIs(call.call_args.args[1], COMPILE_INTERPRETATION_SCHEMA)
        self.assertIn("## Mode: compile", call.call_args.args[0])
        self.assertIn("dispositions: plan_note=1", out)
        intake_dir = next((self.sparring_dir / "intake").iterdir())
        record = json.loads((intake_dir / "intake.json").read_text(encoding="utf-8"))
        self.assertEqual(record["mode"], "compile")
        self.assertEqual(record["compile"], {"named_repositories": ["app", "web"], "inspected_repositories": ["app", "web"]})
        self.assertEqual(record["findings"]["dispositions"]["plan_note"], 1)
        report = (intake_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("- **plan_note** (recommendation) `stale_baseline_fact`", report)

        code, out, err = self._main("approve-plan", str(intake_dir), "--run", "app", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        manifest = next((intake_dir / "runs" / "app").glob("manifest.json"))
        self.assertEqual([s.label for s in load_intake_manifest(manifest).stages()], ["Stage 0", "Stage 1A"])

    def test_a_downgraded_auto_resolution_blocks_approval(self):
        payload = compile_widget_interpretation()
        payload["findings"].append(auto("relabel_stage", ["1A"], []))
        payload["runs"][0]["stages"][1]["source_ranges"] = [lines("## Stage 1A", "Emit reviewed")]
        payload["excluded"].append({"ranges": [lines("Acceptance: tests pass")], "reason": "x"})
        result = self.prepare(payload, mode="compile")
        self.assertEqual([f.code for f in result.blocking], ["other"])
        self.assertIn("auto-resolution rejected", (result.directory / "report.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(IntakeError, "1 blocking finding"):
            approve_plan(result.directory, run_id="app", primary_repository="app", repo_root=self.repo, sparring_dir=self.sparring_dir)

    def test_a_named_sibling_that_was_not_supplied_refuses_with_the_flag_to_add(self):
        result = self.prepare(compile_widget_interpretation(), mode="compile", context={})
        refused = [f for f in result.blocking if f.code == "sibling_not_supplied"]
        self.assertEqual(len(refused), 1)
        self.assertIn("--context-repository web=<path>", refused[0].message)


def compiler_interpretation() -> dict:
    """An honest compile answer for this repository's own run-plan-compiler
    plan (fixture copy): five stages, the design as context, no gates."""

    text = COMPILER
    labels = [("1", "## Stage 1 "), ("2", "## Stage 2 "), ("3", "## Stage 3 "), ("4", "## Stage 4 "), ("5", "## Stage 5 ")]
    stops = [first for _, first in labels[1:]] + ["## Explicitly out of scope"]
    stages = [
        node(text, label, f"Stage {label}", first, stop, context_ids=["design"], depends_on=[labels[i - 1][0]] if i else [])
        for i, ((label, first), stop) in enumerate(zip(labels, stops))
    ]
    return {
        "verdict": "executable_with_recommendations",
        "summary": "Five stages in one slice.",
        "context": [{"id": "design", "title": "Design", "ranges": [until(text, "# Run Plan", "## Stage 1 ")], "reason": "design"}],
        "runs": [{"id": "app", "primary_repository": "app", "expected_branch": None, "rationale": "", "stages": stages}],
        "gates": [],
        "excluded": [{"ranges": [until(text, "## Explicitly out of scope")], "reason": "scope"}],
        "findings": [],
        "amended_plan": None,
    }


class GateDetectionTests(unittest.TestCase):
    def test_this_repositorys_own_plan_compiles_without_a_dropped_gate(self):
        findings = compile_check(compiler_interpretation(), text=COMPILER)
        self.assertNotIn("gate_dropped", [f.code for f in findings])

    def test_false_positives(self):
        for sentence in (
            "Stage 5). Status: proposed, not approved.",
            "Status: proposed, not approved, covering Stage 1 to Stage 5.",
            "Review focus: Stage 1 must keep the `approve-plan` path unchanged.",
            "`gate_at_boundary` is validated here but rendered in Stage 2.",
            "Stage 2 adds manifest v2 and between-stage gates.",
            "Stage 1 must add tests for the deployment parser.",
            "The approval path in Stage 3 seals the manifest.",
        ):
            with self.subTest(sentence=sentence):
                self.assertFalse(states_gate(sentence))

    def test_true_positives(self):
        for sentence in (
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before Stage 3A starts.",
            "Activate the release after Stage 1A, with explicit go-ahead.",
            "Stage 3A requires the owner's go-ahead.",
            "Stage 4 needs sign-off from the owner.",
            "Deploy to production before Stage 2 begins.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(states_gate(sentence))

    def test_only_the_gate_sentence_must_be_covered(self):
        text = CLOUD.replace(
            "If Stage 2's sync metrics regress, run a canary on 5% of devices before\nStage 3A starts.",
            "Notes on Stage 2 metrics. Run a canary before\nStage 3A starts.",
        )
        payload = cloud_interpretation(text)
        canary = payload["gates"][0]["ranges"][0]
        line = next(i for i, l in enumerate(text.splitlines(), 1) if l.startswith("Notes on Stage 2"))
        payload["gates"][0]["ranges"] = [{"start": line, "end": canary["end"]}]
        self.assertNotIn("gate_dropped", [f.code for f in compile_check(payload, text=text)])
        payload["gates"] = []
        payload["excluded"].append({"ranges": [canary], "reason": "x"})
        payload["findings"] = [f for f in payload["findings"] if f["transform"] != "gate_at_boundary"]
        dropped = [f for f in compile_check(payload, text=text) if f.code == "gate_dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertTrue(dropped[0].blocking)

    def test_gate_text_in_code_or_a_heading_is_not_a_gate(self):
        from agent_sparring.intake import _sentences

        text = "## Deploy before Stage 3\n\n```\ndeploy before Stage 3\n```\n\nRun `deploy before Stage 3`.\n"
        found = _sentences(SourcePlan(label="p", text=text))
        self.assertEqual([lines for lines, _ in found], [[7]])
        self.assertFalse(any(states_gate(sentence) for _, sentence in found))


class RepositoryNamingTests(unittest.TestCase):
    def test_a_plain_word_in_prose_does_not_name_a_repository(self):
        self.assertFalse(names_repository("Stage 3B checks the web client and an app restart.", "web"))
        self.assertFalse(names_repository("the Web client", "web"))

    def test_repository_contexts_name_it(self):
        for text in ("update `web`", "the web repository", "repository web", "Repos: app, web.", "sibling sporely-web"):
            with self.subTest(text=text):
                name = "sporely-web" if "sporely" in text else "web"
                self.assertTrue(names_repository(text, name))

    def test_an_unnamed_repository_needs_a_decision(self):
        text = CLOUD.replace("`web`", "web")  # same line numbers; "web" only in prose
        findings = compile_check(cloud_interpretation(), text=text)
        self.assertIn(("unnamed_repository", "needs_decision"), [(f.code, f.disposition) for f in findings])

    def test_from_record_refuses_a_context_that_does_not_match_the_repositories(self):
        record = {"compile": CONTEXT.as_dict(), "repositories": {"app": {}, "web": {}}}
        self.assertEqual(CompileContext.from_record(record), CONTEXT)
        record["repositories"] = {"app": {}}
        with self.assertRaisesRegex(IntakeError, "does not match"):
            CompileContext.from_record(record)


class CompileAmendmentTests(_Repo):
    def test_compile_mode_writes_no_amendment_even_if_one_is_returned(self):
        payload = compile_widget_interpretation()
        payload["amended_plan"] = PLAN + "\nmore\n"
        result = self.prepare(payload, mode="compile")
        self.assertIn("amendment_in_compile_mode", [f.code for f in result.blocking])
        self.assertFalse((result.directory / "amendment.diff").exists())
        report = (result.directory / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("Proposed amendment", report)
        self.assertNotIn("--without-amendment", report)


if __name__ == "__main__":
    unittest.main()
