"""Plan intake: interpretation checks, brief assembly, prepare and approve.

The fixture plan is deliberately generic -- labels ``0``/``1A``/``1B``, a
principles section above the stages, a production release between two
stages that live in different repositories -- because those are the shapes
intake exists for, not any one project's.
"""

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.intake import (
    INTERPRETATION_SCHEMA,
    IntakeError,
    SourcePlan,
    all_findings,
    approve_plan,
    check_interpretation,
    parse_interpretation,
    prepare_plan,
    run_prerequisites,
)
from agent_sparring.manifest import load_manifest_source
from agent_sparring.providers import SparringAgentResult

PLAN = """\
# Widget overhaul

Status: planned. No stage may touch production without its own go-ahead.

## Principles

- Widget identity never changes.
- Names are display metadata, never identity.

---

## Stage 0 — Audit

Repos: app. Measure the widget population.

Acceptance: counts reproduce from a clean checkout.

## Stage 1A — App change

Repos: app. Emit reviewed widget records.

Acceptance: tests pass; no identity changes.

## Stage 1B — Web repair

Repos: web. Repair historical rows once the release is active.

Acceptance: idempotent; dry run matches the run.

## Release

Activate the release in production after Stage 1A, with explicit go-ahead.

## References

- docs/widgets.md
"""


def lines(first: str, last: str | None = None, text: str = PLAN) -> dict:
    """The 1-based inclusive range from the line containing ``first`` to the
    line containing ``last`` (or just ``first``)."""

    numbered = text.splitlines()
    start = next(i for i, line in enumerate(numbered, 1) if first in line)
    end = next(i for i, line in enumerate(numbered, 1) if (last or first) in line and i >= start)
    return {"start": start, "end": end}


def stage(label, title, first, last, **extra):
    base = {
        "label": label,
        "title": title,
        "source_ranges": [lines(first, last)],
        "context_ids": ["principles"],
        "repositories": [],
        "depends_on": [],
        "mode": "implementation",
        "intake_scope": None,
        "rationale": "",
    }
    base.update(extra)
    return base


def good_interpretation() -> dict:
    return {
        "verdict": "executable_with_recommendations",
        "summary": "Two run slices separated by the production release.",
        "context": [
            {"id": "status", "title": "Status", "ranges": [lines("# Widget overhaul", "Status:")], "reason": "authorization"},
            {"id": "principles", "title": "Principles", "ranges": [lines("## Principles", "Names are display")], "reason": "invariants"},
        ],
        "runs": [
            {
                "id": "app",
                "primary_repository": "app",
                "expected_branch": "feature/widgets",
                "rationale": "app-only stages",
                "stages": [
                    stage("0", "Audit", "## Stage 0", "counts reproduce", context_ids=["status", "principles"]),
                    stage("1A", "App change", "## Stage 1A", "tests pass", depends_on=["0"]),
                ],
            },
            {
                "id": "web",
                "primary_repository": "web",
                "expected_branch": None,
                "rationale": "web-only, after the release",
                "stages": [
                    stage(
                        "1B",
                        "Web repair",
                        "## Stage 1B",
                        "idempotent",
                        depends_on=["1A"],
                        intake_scope="Implementation and dry run only; the production run is gate `repair-run`.",
                    ),
                ],
            },
        ],
        "gates": [
            {
                "id": "release",
                "title": "Production release activation",
                "kind": "production",
                "ranges": [lines("## Release", "Activate the release")],
                "after_stage": "1A",
                "blocks_stages": ["1B"],
                "reason": "1B needs the active release",
            }
        ],
        "excluded": [{"ranges": [lines("## References", "docs/widgets.md")], "reason": "references"}],
        "findings": [
            {
                "code": "stale_baseline_fact",
                "severity": "info",
                "stages": ["0"],
                "ranges": [],
                "message": "counts should be recomputed",
            }
        ],
        "amended_plan": None,
    }


def codes(findings) -> list[str]:
    return [f.code for f in findings if f.blocking]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class FakeAdapter:
    def __init__(self, payload, *, side_effect=None):
        self.payload = payload
        self.side_effect = side_effect
        self.calls = []

    def start_structured(self, prompt, output_schema):
        self.calls.append((prompt, output_schema))
        if self.side_effect:
            self.side_effect()
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SparringAgentResult(session_id="t-1", text=text, is_error=False)


class CheckTests(unittest.TestCase):
    source = SourcePlan(label="docs/plan.md", text=PLAN)

    def check(self, payload, mode="faithful"):
        return check_interpretation(parse_interpretation(json.dumps(payload)), self.source, mode=mode)

    def test_schema_is_strict_everywhere(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object" or node.get("type") == ["object", "null"]:
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(INTERPRETATION_SCHEMA)

    def test_a_complete_interpretation_has_no_engine_findings(self):
        self.assertEqual(self.check(good_interpretation()), ())

    def test_unaccounted_source_text_blocks(self):
        payload = good_interpretation()
        payload["excluded"] = []
        findings = self.check(payload)
        self.assertEqual(codes(findings), ["uncovered_source"])
        self.assertIn("## References", findings[0].message)

    def test_dropping_a_global_constraint_is_caught(self):
        payload = good_interpretation()
        payload["context"] = [c for c in payload["context"] if c["id"] != "principles"]
        for run in payload["runs"]:
            for s in run["stages"]:
                s["context_ids"] = [c for c in s["context_ids"] if c != "principles"]
        findings = self.check(payload)
        self.assertEqual(codes(findings), ["uncovered_source"])
        self.assertIn("Principles", findings[0].message)

    def test_a_gate_between_two_stages_of_one_run_blocks(self):
        payload = good_interpretation()
        web = payload["runs"].pop()
        payload["runs"][0]["stages"].extend(web["stages"])
        self.assertIn("gate_inside_run", codes(self.check(payload)))

    def test_a_dependency_on_a_later_stage_blocks(self):
        payload = good_interpretation()
        payload["runs"][0]["stages"][0]["depends_on"] = ["1A"]
        self.assertEqual(codes(self.check(payload)), ["dependency_not_satisfied"])

    def test_out_of_range_lines_and_unknown_references_block(self):
        payload = good_interpretation()
        payload["runs"][0]["stages"][0]["source_ranges"].append({"start": 900, "end": 901})
        payload["runs"][0]["stages"][1]["context_ids"].append("nope")
        self.assertEqual(sorted(codes(self.check(payload))), ["invalid_range", "unknown_reference"])

    def test_primary_repository_cannot_also_be_a_sibling(self):
        payload = good_interpretation()
        payload["runs"][0]["stages"][1]["repositories"] = ["app"]
        self.assertEqual(codes(self.check(payload)), ["primary_declared_as_sibling"])

    def test_faithful_mode_refuses_an_amendment_and_refine_allows_one(self):
        payload = good_interpretation()
        payload["amended_plan"] = PLAN + "\nMore.\n"
        self.assertEqual(codes(self.check(payload, "faithful")), ["amendment_in_faithful_mode"])
        self.assertEqual(codes(self.check(payload, "refine")), [])

    def test_a_refusal_must_say_why(self):
        payload = good_interpretation()
        payload["verdict"] = "cannot_interpret_faithfully"
        self.assertEqual(codes(self.check(payload)), ["unexplained_refusal"])

    def test_prerequisites_are_gates_and_earlier_slices(self):
        interpretation = parse_interpretation(json.dumps(good_interpretation()))
        self.assertEqual(run_prerequisites(interpretation, "app"), ())
        self.assertEqual(run_prerequisites(interpretation, "web"), ("app", "release"))

    def test_malformed_answers_are_refused(self):
        with self.assertRaises(IntakeError):
            parse_interpretation("not json")
        broken = good_interpretation()
        broken["verdict"] = "sure"
        with self.assertRaises(IntakeError):
            parse_interpretation(json.dumps(broken))

    def test_agent_findings_sort_with_engine_findings(self):
        payload = good_interpretation()
        payload["excluded"] = []
        interpretation = parse_interpretation(json.dumps(payload))
        ordered = all_findings(interpretation, self.source, mode="faithful")
        self.assertEqual([f.severity for f in ordered], ["blocking", "info"])


class _Repo(unittest.TestCase):
    ignore = ".sparring/stages/\n.sparring/plans/\n.sparring/intake/\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "app"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / ".gitignore").write_text(self.ignore, encoding="utf-8")
        self.plan = self.repo / "docs" / "plan.md"
        self.plan.parent.mkdir()
        self.plan.write_text(PLAN, encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.sparring_dir = self.repo / ".sparring"
        self.sparring_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def prepare(self, payload=None, *, mode="faithful", adapter=None):
        adapter = adapter or FakeAdapter(payload if payload is not None else good_interpretation())
        keys = iter(["app-run-0001", "web-run-0002", "x-run-0003"])
        return prepare_plan(
            self.plan,
            self.repo,
            self.sparring_dir,
            adapter,
            mode=mode,
            primary_repository="app",
            project_context="Project context text.",
            mint_run_key=lambda label: next(keys),
        )


class PrepareTests(_Repo):
    def test_writes_a_reviewable_intake_and_nothing_executable(self):
        adapter = FakeAdapter(good_interpretation())
        result = self.prepare(adapter=adapter)
        files = sorted(p.relative_to(result.directory).as_posix() for p in result.directory.rglob("*") if p.is_file())
        self.assertEqual(
            files,
            [
                "briefs/app/01-0.md",
                "briefs/app/02-1a.md",
                "briefs/web/01-1b.md",
                "intake.json",
                "interpretation.json",
                "prompt.md",
                "report.md",
                "source.md",
            ],
        )
        self.assertEqual(self.plan.read_text(encoding="utf-8"), PLAN)
        self.assertEqual(result.blocking, ())
        prompt, schema = adapter.calls[0]
        self.assertIs(schema, INTERPRETATION_SCHEMA)
        self.assertIn("Project context text.", prompt)
        self.assertIn("## Stage 1A — App change", prompt)

        report = (result.directory / "report.md").read_text(encoding="utf-8")
        self.assertIn("Run slice `web` — primary `web`", report)
        self.assertIn("Must confirm before approval: `app`, `release`", report)
        self.assertIn("`release` (production)", report)

    def test_briefs_are_verbatim_source_plus_a_labelled_scope(self):
        result = self.prepare()
        brief = (result.directory / "briefs/web/01-1b.md").read_text(encoding="utf-8")
        self.assertIn("# Stage brief: web-run-0002-stage-1b-web-repair", brief)
        self.assertIn("- Widget identity never changes.", brief)
        self.assertIn("Repos: web. Repair historical rows once the release is active.", brief)
        self.assertIn("# Intake scoping", brief)
        self.assertIn("the source plan text governs", brief)
        self.assertLess(brief.index("Widget identity"), brief.index("## Stage 1B"))
        self.assertLess(brief.index("## Stage 1B"), brief.index("# Intake scoping"))
        stage0 = (result.directory / "briefs/app/01-0.md").read_text(encoding="utf-8")
        self.assertIn("No stage may touch production", stage0)

    def test_blocking_findings_are_reported_not_refused(self):
        payload = good_interpretation()
        payload["excluded"] = []
        result = self.prepare(payload)
        self.assertEqual(codes(result.blocking), ["uncovered_source"])
        report = (result.directory / "report.md").read_text(encoding="utf-8")
        self.assertIn("**blocked**", report)

    def test_refine_amendment_becomes_a_diff(self):
        payload = good_interpretation()
        payload["amended_plan"] = PLAN.replace("Acceptance: tests pass;", "Acceptance: tests and probes pass;")
        result = self.prepare(payload, mode="refine")
        diff = (result.directory / "amendment.diff").read_text(encoding="utf-8")
        self.assertIn("+Acceptance: tests and probes pass;", diff)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), PLAN)

    def test_a_write_during_the_turn_is_refused(self):
        adapter = FakeAdapter(
            good_interpretation(),
            side_effect=lambda: (self.repo / "stray.txt").write_text("x", encoding="utf-8"),
        )
        with self.assertRaisesRegex(IntakeError, "changed during the read-only intake turn"):
            self.prepare(adapter=adapter)
        self.assertFalse((self.sparring_dir / "intake").exists())

    def test_visible_intake_directory_is_refused_before_the_turn(self):
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        _git(self.repo, "commit", "-q", "-am", "narrow ignore")
        adapter = FakeAdapter(good_interpretation())
        with self.assertRaisesRegex(IntakeError, r"\.sparring/intake/"):
            self.prepare(adapter=adapter)
        self.assertEqual(adapter.calls, [])


class ApproveTests(_Repo):
    def approve(self, directory, run="app", **kwargs):
        kwargs.setdefault("primary_repository", "app")
        return approve_plan(directory, run_id=run, **kwargs)

    def test_approves_a_slice_into_a_manifest_the_engine_reads(self):
        result = self.prepare()
        approval = self.approve(result.directory)
        self.assertTrue(approval.created)
        self.assertEqual(approval.expected_branch, "feature/widgets")
        source = load_manifest_source(approval.manifest_path)
        stages = source.stages()
        self.assertEqual([s.label for s in stages], ["Stage 0", "Stage 1A"])
        self.assertEqual(stages[0].stage_id, "app-run-0001-stage-0-audit")
        self.assertEqual(
            stages[1].brief,
            (result.directory / "briefs/app/02-1a.md").read_text(encoding="utf-8"),
        )
        record = json.loads((approval.manifest_path.parent / "approval.json").read_text(encoding="utf-8"))
        self.assertEqual(record["manifest_digest"], source.digest())

        again = self.approve(result.directory)
        self.assertFalse(again.created)

    def test_a_slice_is_approved_only_from_its_primary_repository(self):
        result = self.prepare()
        with self.assertRaisesRegex(IntakeError, "does not move a stage's work"):
            self.approve(result.directory, run="web", expected_branch="feature/web")

    def test_gates_and_earlier_slices_must_be_confirmed(self):
        result = self.prepare()
        with self.assertRaisesRegex(IntakeError, r"\['app', 'release'\]"):
            self.approve(result.directory, run="web", primary_repository="web", expected_branch="feature/web")
        with self.assertRaisesRegex(IntakeError, "names nothing"):
            self.approve(
                result.directory, run="web", primary_repository="web", expected_branch="feature/web",
                confirmed_prerequisites=["app", "release", "typo"],
            )
        approval = self.approve(
            result.directory, run="web", primary_repository="web", expected_branch="feature/web",
            confirmed_prerequisites=["app", "release"],
        )
        record = json.loads((approval.manifest_path.parent / "approval.json").read_text(encoding="utf-8"))
        self.assertEqual(record["confirmed_prerequisites"], ["app", "release"])

    def test_blocking_findings_refuse(self):
        payload = good_interpretation()
        payload["excluded"] = []
        result = self.prepare(payload)
        with self.assertRaisesRegex(IntakeError, "uncovered_source"):
            self.approve(result.directory)
        self.assertFalse((result.directory / "runs").exists())

    def test_a_changed_source_plan_refuses(self):
        result = self.prepare()
        self.plan.write_text(PLAN + "\nLate edit.\n", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "changed since intake"):
            self.approve(result.directory)

    def test_an_edited_interpretation_refuses(self):
        result = self.prepare()
        path = result.directory / "interpretation.json"
        path.write_text(path.read_text(encoding="utf-8").replace("Audit", "Audit!"), encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "modified after intake"):
            self.approve(result.directory)

    def test_an_amendment_must_be_explicitly_set_aside(self):
        payload = good_interpretation()
        payload["amended_plan"] = PLAN + "\nMore.\n"
        result = self.prepare(payload, mode="refine")
        with self.assertRaisesRegex(IntakeError, "--without-amendment"):
            self.approve(result.directory)
        self.assertTrue(self.approve(result.directory, without_amendment=True).created)

    def test_siblings_need_an_explicit_path_and_branch(self):
        payload = good_interpretation()
        payload["runs"][0]["stages"][1]["repositories"] = ["docs"]
        result = self.prepare(payload)
        with self.assertRaisesRegex(IntakeError, "--repository NAME=PATH"):
            self.approve(result.directory)
        approval = self.approve(
            result.directory,
            repositories={"docs": "../docs"},
            repository_branches={"docs": "feature/docs"},
        )
        declared = load_manifest_source(approval.manifest_path).stages()[1].repositories
        self.assertEqual([(r.name, r.path, r.branch) for r in declared], [("docs", "../docs", "feature/docs")])

    def test_expected_branch_is_required_when_the_plan_states_none(self):
        payload = good_interpretation()
        payload["runs"][0]["expected_branch"] = None
        result = self.prepare(payload)
        with self.assertRaisesRegex(IntakeError, "--expected-branch"):
            self.approve(result.directory)


class CliTests(_Repo):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_prepare_then_approve(self):
        (self.sparring_dir / "project.toml").write_text('project = "app"\n', encoding="utf-8")
        result = SparringAgentResult(session_id="t-1", text=json.dumps(good_interpretation()), is_error=False)
        with mock.patch("agent_sparring.cli.CodexCliAdapter.start_structured", return_value=result) as call:
            code, out, err = self._main("prepare-plan", str(self.plan), "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_count, 1)
        self.assertIn("run slice web (web): 1B", out)
        intake_dir = next((self.sparring_dir / "intake").iterdir())

        code, out, err = self._main("approve-plan", str(intake_dir), "--run", "app", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertIn("sparring run-plan --manifest", out)
        self.assertIn("--expected-branch feature/widgets", out)

        code, out, err = self._main("approve-plan", str(intake_dir), "--run", "web", "--repo-root", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("approve and run this slice from the 'web' project", err)

    def test_prepare_refuses_a_non_codex_intake_provider(self):
        code, _, err = self._main(
            "prepare-plan", str(self.plan), "--repo-root", str(self.repo), "--sparring-provider", "claude-cli"
        )
        self.assertEqual(code, 1)
        self.assertIn("could not prepare plan", err)


if __name__ == "__main__":
    unittest.main()
