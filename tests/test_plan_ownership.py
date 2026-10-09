"""Stage ownership (``Repository:``) and Markdown plan gates (``Gate before:``).

A stage declares the repository it belongs to; an undeclared stage belongs
to the home repository. A Markdown ``Gate before:`` block is the same v2
gate a manifest's ``gates_before`` declares. Nothing runs across
repositories yet: a foreign owner is refused, and a plan without
declarations is byte-for-byte what it was.
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.cli import main
from agent_sparring.deferred_gate import DeferredAnswer, DeferredVerificationRequired, before_stage_checkpoint
from agent_sparring.intake import undeclared_repository_mentions
from agent_sparring.manifest import ManifestError, manifest_digest, parse_manifest
from agent_sparring.plan import (
    GATE_DECLARATION_INVALID,
    OWNER_DECLARATION_INVALID,
    PlanRefusal,
    PlanRunState,
    PlanRunStatus,
    _verify_source_unchanged,
    load_markdown_source,
    markdown_source_from_text,
    new_run_key,
    parse_plan,
    plan_digest,
    plan_key,
    refuse_foreign_owners,
    resume_plan,
    run_state_path,
    start_plan,
)
from agent_sparring.plan_model import derive_executions
from test_managed_run import _ManagedRepoTestCase
from test_manifest import _ManifestRepoTestCase, manifest_payload
from test_plan import PLAN, READY, _fixed, _PlanRepoTestCase, _SparringAdapter, _StageAdapter

# Pinned from the engine before ownership existed (commit c4fb297).
GOLDEN_IDS = ["run-0001-stage-1-foundation", "run-0001-stage-2-incremental-rendering"]
GOLDEN_SOURCE_IDS = ["plan-61bf2008-stage-1-foundation", "plan-61bf2008-stage-2-incremental-rendering"]
GOLDEN_PLAN_DIGEST = "549438a5e8892fbcf337985ebc1511e2a532b66bf85e8b4ec10624f73ea4af4a"
GOLDEN_MANIFEST_DIGEST = "d5609d89d6179e314f08bbc5689017c3164aa8a699a2ee6ab54a030ca82f443a"

REASON = """\
Commit in sporely-landing's `.sparring/project.toml`:

```toml
[visual_review]
command = ["npm", "run", "screenshots"]
```"""


def quoted(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def plan(*stages: str) -> str:
    sections = [f"## Stage {n} — S{n}\n\n{body}\n" for n, body in enumerate(stages, start=1)]
    return "# Plan\n\n" + "\n".join(sections)


GATED = f"""\
# Gated

## Stage 1 — First

Build the first part.

## Stage 2 — Second

Gate before: canary — Staging canary
{quoted(REASON)}

Build the second part.
"""


def refusal(text: str) -> PlanRefusal:
    try:
        parse_plan(text)
    except PlanRefusal as exc:
        return exc
    raise AssertionError(f"not refused:\n{text}")


class UnchangedTests(unittest.TestCase):
    def test_an_undeclared_plan_keeps_its_ids_digest_and_run_key(self):
        stages = parse_plan(PLAN, plan_key="run-0001")
        self.assertEqual([s.stage_id for s in stages], GOLDEN_IDS)
        self.assertEqual(plan_digest(stages), GOLDEN_PLAN_DIGEST)
        self.assertEqual([(s.owner, s.gates_before) for s in stages], [(None, ())] * 2)
        source = markdown_source_from_text(Path("docs/plan.md"), "docs/plan.md", PLAN)
        self.assertEqual([s.stage_id for s in source.stages()], GOLDEN_SOURCE_IDS)
        self.assertEqual(source.digest(), GOLDEN_PLAN_DIGEST)
        self.assertEqual([(s.owner, s.gates_before) for s in source.stages()], [(None, ())] * 2)
        self.assertRegex(new_run_key("docs/plan.md"), r"^plan-61bf2008-[0-9a-f]{8}$")
        self.assertEqual(plan_key("docs/plan.md"), "plan-61bf2008")

    def test_a_manifest_without_an_owner_keeps_its_digest(self):
        manifest = parse_manifest(json.dumps(manifest_payload()))
        self.assertEqual(manifest_digest(manifest), GOLDEN_MANIFEST_DIGEST)
        self.assertEqual([s.owner for s in manifest.stages], [None, None])
        payload = manifest_payload()
        payload["stages"][0]["owner_repository"] = None
        self.assertEqual(manifest_digest(parse_manifest(json.dumps(payload))), GOLDEN_MANIFEST_DIGEST)


class OwnerDeclarationTests(unittest.TestCase):
    def test_declaration_forms(self):
        for line, name in (
            ("Repository: sporely-landing", "sporely-landing"),
            ("**Repository:** sporely-landing  ", "sporely-landing"),
            ("**Repository**: `sporely.landing_2`", "sporely.landing_2"),
            ("**Repository:** `agent-sparring`. **Depends on:** none.", "agent-sparring"),
        ):
            with self.subTest(line):
                (first, second) = parse_plan(plan("Body.", f"{line}\n\nBody."))
                self.assertEqual((first.owner, second.owner), (None, name))
                # The line is the section's, so the digest covers it.
                self.assertIn(line, second.section)

    def test_the_declaration_is_covered_by_the_digest(self):
        declared = plan_digest(parse_plan(plan("Body.", "Repository: web\n\nBody.")))
        other = plan_digest(parse_plan(plan("Body.", "Repository: app\n\nBody.")))
        undeclared = plan_digest(parse_plan(plan("Body.", "Body.")))
        self.assertEqual(len({declared, other, undeclared}), 3)

    def test_malformed_declarations_are_refused(self):
        for line in (
            "Repository:",
            "Repository: two words",
            "Repository: a/b",
            "Repository: `a` and `b`",
            "Repository: ...",
            "repository: lower-case",
            "  Repository: indented",
        ):
            with self.subTest(line):
                exc = refusal(plan(f"{line}\n\nBody."))
                self.assertEqual(exc.code, OWNER_DECLARATION_INVALID)
                self.assertIn("[owner_declaration_invalid]", str(exc))

    def test_misplaced_declarations_are_refused(self):
        for body in (
            "Body.\n\nRepository: web",
            "Gate before: g — G\n> why\n\nRepository: web",
            "Body.\n\n### Details\n\n**Repository:** web",
        ):
            with self.subTest(body):
                self.assertEqual(refusal(plan(body)).code, OWNER_DECLARATION_INVALID)

    def test_a_declaration_shaped_line_in_a_code_fence_is_prose(self):
        (stage,) = parse_plan(plan("Body.\n\n```\nRepository: web\nGate before: x — y\n```"))
        self.assertEqual((stage.owner, stage.gates_before), (None, ()))


class OutsideStageDeclarationTests(unittest.TestCase):
    """Nothing declaration-shaped outside a stage header is ever silently
    ignored: it is the preamble's contextual ``Repository:`` line, or it is
    refused. The preamble line decides nothing about execution."""

    def _source(self, text: str):
        return markdown_source_from_text(Path("p.md"), "p.md", text)

    def test_a_preamble_declaration_is_context_only(self):
        for name in ("app", "foreign"):
            with self.subTest(name):
                source = self._source(f"# Plan\n\n**Repository:** `{name}`  \n\n" + plan("Body."))
                self.assertEqual(source.declared_home, name)
                # No stage is owned by it, and nothing is refused for it.
                self.assertEqual([s.owner for s in source.stages()], [None])
                refuse_foreign_owners(source, "app")

    def test_malformed_or_conflicting_preamble_declarations_are_refused(self):
        for preamble in ("Repository: a/b", "Repository: two words", "Repository: a\n\nRepository: b"):
            with self.subTest(preamble):
                self.assertEqual(refusal(f"{preamble}\n\n" + plan("Body.")).code, OWNER_DECLARATION_INVALID)

    def test_declarations_after_a_non_stage_heading_are_refused(self):
        for text, code in (
            (plan("Body.") + "\n## Notes\n\nRepository: foreign\n", OWNER_DECLARATION_INVALID),
            ("# Run A\n\n" + plan("Body.") + "\n# Run B\n\n**Repository:** web\n\n## Stage 2 — B\n\nx\n",
             OWNER_DECLARATION_INVALID),
            ("Gate before: g — G\n> why\n\n" + plan("Body."), GATE_DECLARATION_INVALID),
            (plan("Body.") + "\n## Closeout\n\nGate before: g — G\n> why\n", GATE_DECLARATION_INVALID),
        ):
            with self.subTest(text):
                self.assertEqual(refusal(text).code, code)

    def test_fenced_examples_and_wrapped_prose_outside_stages_are_prose(self):
        text = (
            "# Plan\n\nThe engine records it in the\nrepository: never elsewhere.\n\n"
            "```markdown\nRepository: web\nGate before: g — G\n> why\n```\n\n"
            + plan("Body in the same\nrepository: still prose.")
            + "\n## Notes\n\n~~~\nRepository: x\n~~~\n"
        )
        source = self._source(text)
        self.assertIsNone(source.declared_home)
        self.assertEqual([(s.owner, s.gates_before) for s in source.stages()], [(None, ())])

    def test_changing_the_preamble_between_invocations_changes_no_execution(self):
        # Run state recorded against a plan with a preamble line; a separate
        # invocation then reads the plan with the line changed or removed.
        # Because the line is context, there is nothing to protect: the
        # digest, stage ids, owners and refusals are all as they were.
        path = Path(self._tmp()) / "p.md"
        body = plan("Body.", "Repository: web\n\nThere.")
        path.write_text("Repository: repo\n\n" + body, encoding="utf-8")
        recorded = load_markdown_source(path, "p.md")
        state = PlanRunState(
            plan="p.md", plan_digest=recorded.digest(), expected_branch="feature/x",
            current_stage_index=0, current_stage=recorded.stages()[0].stage_id, status=PlanRunStatus.PAUSED,
        )
        for preamble in ("Repository: elsewhere\n\n", ""):
            with self.subTest(preamble or "removed"):
                path.write_text(preamble + body, encoding="utf-8")
                later = load_markdown_source(path, "p.md")
                self.assertEqual(later.digest(), state.plan_digest)
                self.assertEqual(_verify_source_unchanged(later, state), recorded.stages())
                self.assertEqual(
                    [(s.stage_id, s.owner) for s in later.stages()],
                    [(s.stage_id, s.owner) for s in recorded.stages()],
                )

    def _tmp(self) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def test_this_plans_own_preamble_declaration_parses(self):
        # The plan this stage implements declares its repository in its
        # preamble; it must stay runnable from that repository.
        path = Path(__file__).resolve().parent.parent / "docs" / "plans" / "repository-slices.md"
        source = markdown_source_from_text(path, "docs/plans/repository-slices.md", path.read_text(encoding="utf-8"))
        self.assertEqual(source.declared_home, "agent-sparring")
        refuse_foreign_owners(source, "agent-sparring")


class MarkdownGateTests(unittest.TestCase):
    def test_a_multi_line_reason_survives_verbatim(self):
        _, second = parse_plan(GATED)
        (gate,) = second.gates_before
        self.assertEqual((gate.id, gate.title, gate.kind), ("canary", "Staging canary", "human"))
        self.assertEqual(gate.reason, REASON)

    def test_leading_and_trailing_quoted_blank_lines_are_kept(self):
        (stage,) = parse_plan(plan("Gate before: g — G\n>\n> set this:\n>    indented\n>\n\nBody."))
        self.assertEqual(stage.gates_before[0].reason, "\nset this:\n   indented\n")

    def test_gates_follow_the_repository_line_and_may_repeat(self):
        body = (
            "**Repository:** `sporely-landing`\n\n"
            "Gate before: config — Visual review configured\n> one\n\n"
            "**Gate before:** owner-ok - Owner go-ahead\n>  indented\n>\n> two\n\nBody."
        )
        (stage,) = parse_plan(plan(body))
        self.assertEqual(stage.owner, "sporely-landing")
        self.assertEqual(
            [(g.id, g.title, g.reason) for g in stage.gates_before],
            [("config", "Visual review configured", "one"), ("owner-ok", "Owner go-ahead", " indented\n\ntwo")],
        )

    def test_a_gate_reaches_the_planned_stage(self):
        source = markdown_source_from_text(Path("docs/gated.md"), "docs/gated.md", GATED)
        first, second = source.stages()
        self.assertEqual(first.gates_before, ())
        self.assertEqual([g.id for g in second.gates_before], ["canary"])
        # The section, gate included, is still the brief verbatim.
        self.assertIn("Gate before: canary — Staging canary", second.brief)

    def test_malformed_and_misplaced_gates_are_refused(self):
        for body in (
            "Gate before: canary — Staging canary\n\nno quoted reason",
            "Gate before: canary — Staging canary\n>\n>   \n\nBody.",
            "Gate before: two words — Title\n> why",
            "Gate before: canary\n> why",
            "Body.\n\nGate before: canary — Late\n> why",
            "Gate before: canary — A\n> why\n\nBody.\n\n### More\n\n**Gate before:** other — B\n> why",
        ):
            with self.subTest(body):
                self.assertEqual(refusal(plan(body)).code, GATE_DECLARATION_INVALID)

    def test_gate_ids_are_unique_across_the_plan(self):
        gate = "Gate before: canary — C\n> why\n\nBody."
        self.assertEqual(refusal(plan("Body.", gate, gate)).code, GATE_DECLARATION_INVALID)

    def test_a_gate_is_covered_by_the_digest(self):
        base = plan_digest(parse_plan(GATED))
        self.assertNotEqual(base, plan_digest(parse_plan(GATED.replace("npm", "pnpm"))))
        self.assertNotEqual(base, plan_digest(parse_plan(plan("Build the first part.", "Build the second part."))))


class ManifestOwnerTests(unittest.TestCase):
    def test_owner_repository_round_trips_and_is_digested(self):
        payload = manifest_payload()
        payload["stages"][1]["owner_repository"] = "sporely-landing"
        manifest = parse_manifest(json.dumps(payload))
        self.assertEqual([s.owner for s in manifest.stages], [None, "sporely-landing"])
        self.assertNotEqual(manifest_digest(manifest), GOLDEN_MANIFEST_DIGEST)
        payload["stages"][1]["owner_repository"] = "sporely-web"
        self.assertNotEqual(manifest_digest(parse_manifest(json.dumps(payload))), manifest_digest(manifest))

    def test_owner_repository_in_a_v2_manifest(self):
        payload = manifest_payload()
        payload["version"] = 2
        payload["stages"][1].update(
            owner_repository="web",
            gates_before=[{"id": "g", "title": "G", "kind": "human", "reason": "why"}],
        )
        self.assertEqual(parse_manifest(json.dumps(payload)).stages[1].owner, "web")

    def test_an_invalid_owner_is_refused(self):
        for value in ("", "two words", "a/b", 3, "..."):
            payload = manifest_payload()
            payload["stages"][0]["owner_repository"] = value
            with self.subTest(value), self.assertRaisesRegex(ManifestError, "owner_repository"):
                parse_manifest(json.dumps(payload))


class ExecutionsTests(unittest.TestCase):
    def test_consecutive_same_owner_stages_form_one_execution(self):
        text = plan("A.", "B.", "Repository: web\n\nC.", "Repository: web\n\nD.", "Repository: app\n\nE.")
        source = markdown_source_from_text(Path("p.md"), "p.md", text)
        self.assertEqual(
            [(e.repository, e.positions) for e in derive_executions(source.stages(), "app")],
            [("app", (1, 2)), ("web", (3, 4)), ("app", (5,))],
        )
        refuse_foreign_owners(markdown_source_from_text(Path("p.md"), "p.md", plan("Repository: app\n\nA.")), "app")
        with self.assertRaises(PlanRefusal) as ctx:
            refuse_foreign_owners(source, "app")
        self.assertEqual(ctx.exception.code, "cross_repository_requires_managed")


UI_SCREENSHOTS_SHAPED = """\
# Agent Sparring — Automated UI visual review

**Primary repository:** `agent-sparring`
**Pilot repository:** `sporely-landing`

# Run A — Agent Sparring engine

## Stage 1 — Visual evidence contract

Add a repository-owned capture contract to the engine.

# Run B — sporely-landing pilot

## Stage 2 — Deterministic Playwright screenshot capture

**Outcome:** `sporely-landing` can reliably capture the spore panel.
"""


class WarningTests(unittest.TestCase):
    def _mentions(self, text: str, home: str = "agent-sparring") -> tuple[str, ...]:
        sections = [stage.section for stage in parse_plan(text)]
        return undeclared_repository_mentions(text, sections, home)

    def test_fires_on_ui_screenshots_shaped_text(self):
        self.assertEqual(self._mentions(UI_SCREENSHOTS_SHAPED), ("sporely-landing",))

    def test_a_heading_alone_is_enough(self):
        text = UI_SCREENSHOTS_SHAPED.replace("`sporely-landing` can", "It can")
        self.assertEqual(self._mentions(text), ("sporely-landing",))

    def test_a_qualified_heading_name_backticked_in_stage_prose_fires(self):
        text = "# P\n\n## Stage 1 — sporely-landing pilot\n\nImplement in `sporely-landing`.\n"
        self.assertEqual(self._mentions(text), ("sporely-landing",))

    def test_a_backticked_name_in_stage_prose_alone_fires(self):
        text = "# P\n\n## Stage 1 — Pilot\n\nImplement in `sporely-landing`.\n"
        self.assertEqual(self._mentions(text), ("sporely-landing",))

    def test_a_backticked_name_in_a_heading_alone_fires(self):
        text = "# P\n\n## Stage 1 — `sporely-landing` pilot\n\nImplement it.\n"
        self.assertEqual(self._mentions(text), ("sporely-landing",))

    def test_a_bare_heading_name_fires_when_the_document_corroborates_it(self):
        heading = "# P\n\n## Stage 1 — sporely-landing pilot\n\nImplement it.\n"
        # Same family as the home repository.
        self.assertEqual(self._mentions(heading, home="sporely-web"), ("sporely-landing",))
        # Backticked elsewhere in the document.
        self.assertEqual(self._mentions("See `sporely-landing`.\n\n" + heading), ("sporely-landing",))
        # Alone it is indistinguishable from "end-to-end" and does not warn.
        self.assertEqual(self._mentions(heading), ())

    def test_commands_identifiers_files_and_compound_words_do_not_fire(self):
        text = plan(
            "Run `sparring check-plan --json`, then `run-plan --managed`; see `input.kind`, "
            "`plan_digest`, `docs/x-y.md`, `ui-screenshots.md` and `2026-09-19`.",
        ).replace("## Stage 1 — S1", "## Stage 1 — end-to-end read-only dry-run")
        self.assertEqual(self._mentions(text), ())

    def test_home_and_plain_words_do_not_fire(self):
        text = plan("Edit the repository and the repository-owned script in `agent-sparring`.")
        self.assertEqual(self._mentions(text), ())
        # Named only in the preamble, not in a heading or stage: no warning.
        self.assertEqual(self._mentions(UI_SCREENSHOTS_SHAPED, home="sporely-landing"), ())


class _Cli:
    def _main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *argv])
        return code, out.getvalue(), err.getvalue()


class CheckPlanTests(_Cli, _PlanRepoTestCase):
    def _check(self, text: str, *argv: str):
        path = Path(self._tmp.name) / "check.md"
        path.write_text(text, encoding="utf-8")
        return self._main("check-plan", str(path), "--repo-root", str(self.repo), *argv)

    def test_json_reports_repository_gates_and_executions(self):
        text = GATED.replace("Gate before:", "Repository: web\n\nGate before:")
        code, out, err = self._check(text, "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["home_repository"], "repo")
        self.assertEqual([s["repository"] for s in report["stages"]], ["repo", "web"])
        self.assertEqual(report["stages"][0]["gates_before"], [])
        self.assertEqual(
            report["stages"][1]["gates_before"],
            [{"id": "canary", "title": "Staging canary", "kind": "human", "reason": REASON}],
        )
        self.assertEqual(
            report["executions"], [{"repository": "repo", "stages": [1]}, {"repository": "web", "stages": [2]}]
        )
        self.assertEqual(report["warnings"], [])
        self.assertFalse((self.sparring_dir / "plans").exists())

    def test_an_undeclared_cross_repository_plan_warns_but_is_valid(self):
        code, out, err = self._check(UI_SCREENSHOTS_SHAPED, "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertTrue(report["valid"])
        (warning,) = report["warnings"]
        self.assertIn("'sporely-landing'", warning)
        self.assertEqual(report["executions"], [{"repository": "repo", "stages": [1, 2]}])
        code, _, err = self._check(UI_SCREENSHOTS_SHAPED)
        self.assertEqual(code, 0)
        self.assertIn("warning: the plan names repository 'sporely-landing'", err)

    def test_a_foreign_preamble_declaration_warns(self):
        code, out, err = self._check("Repository: elsewhere\n\n" + plan("Body."), "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["executions"], [{"repository": "repo", "stages": [1]}])
        (warning,) = report["warnings"]
        self.assertIn("'Repository: elsewhere' before its first stage", warning)
        self.assertIn("context only", warning)
        code, out, err = self._check("Repository: repo\n\n" + UI_SCREENSHOTS_SHAPED, "--json")
        self.assertEqual(code, 0, err)
        # Declared, so not the undeclared shape the warning is for.
        self.assertEqual(json.loads(out)["warnings"], [])

    def test_a_malformed_declaration_is_reported_with_its_code(self):
        code, out, _ = self._check(plan("Repository: two words\n\nBody."), "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual((report["valid"], report["code"]), (False, OWNER_DECLARATION_INVALID))


FOREIGN = plan("Build here.", "Repository: elsewhere\n\nBuild there.")


class CurrentCheckoutRefusalTests(_Cli, _PlanRepoTestCase):
    def test_run_plan_refuses_a_foreign_owner_before_anything_is_written(self):
        self.plan_path.write_text(FOREIGN, encoding="utf-8")
        _commit(self.repo, "foreign plan")
        code, _, err = self._main(
            "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--expected-branch", "feature/x"
        )
        self.assertEqual(code, 1)
        self.assertIn("[cross_repository_requires_managed]", err)
        self.assertFalse((self.sparring_dir / "plans").exists())
        self.assertFalse((self.sparring_dir / "stages").exists())

    def test_start_plan_refuses_a_foreign_owner_and_never_falls_back_to_intake(self):
        for text, code_ in ((FOREIGN, "cross_repository_requires_managed"),
                            (plan("Repository: two words\n\nBody."), OWNER_DECLARATION_INVALID)):
            self.plan_path.write_text(text, encoding="utf-8")
            _commit(self.repo, code_)
            with self.subTest(code_):
                code, out, _ = self._main(
                    "start-plan", str(self.plan_path), "--repo-root", str(self.repo),
                    "--expected-branch", "feature/x", "--json",
                )
                status = json.loads(out)
                self.assertEqual((code, status["status"]), (1, "refused"))
                self.assertIn(f"[{code_}]", status["error"])
                self.assertFalse((self.sparring_dir / "intake").exists())


class ManagedRefusalTests(_Cli, _ManagedRepoTestCase):
    def setUp(self):
        super().setUp()
        self.plan_path.write_text(FOREIGN, encoding="utf-8")
        _commit(self.repo, "foreign plan")

    def test_run_plan_managed_refuses_a_foreign_owner_without_its_repository(self):
        code, _, err = self._start_managed()
        self.assertEqual(code, 1)
        self.assertIn("[repository_unknown]", err)
        self.assertEqual(self._records(), ())

    def test_start_plan_managed_refuses_a_foreign_owner_without_its_repository(self):
        code, out, _ = self._main("start-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", "--json")
        self.assertEqual(code, 1)
        self.assertIn("[repository_unknown]", json.loads(out)["error"])
        self.assertEqual(self._records(), ())

    def test_a_home_owned_declaration_runs_managed(self):
        self.plan_path.write_text(plan("Repository: repo\n\nBuild here."), encoding="utf-8")
        _commit(self.repo, "home plan")
        code, _, err = self._start_managed()
        self.assertNotIn("cross_repository", err)
        self.assertEqual(len(self._records()), 1, err)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", message], check=True, capture_output=True)


class GateEquivalenceTests(_ManifestRepoTestCase):
    """A Markdown ``Gate before:`` owes the same obligation, at the same
    checkpoint, with the same stop, as the equivalent v2 manifest gate."""

    RUN_KEY = "gated-run-0001"

    def _markdown(self):
        path = Path(self._tmp.name) / "gated.md"
        path.write_text(GATED, encoding="utf-8")
        return markdown_source_from_text(path, "docs/gated.md", GATED).in_namespace(self.RUN_KEY)

    def _run(self, source, **kwargs):
        self.stage_adapter = _StageAdapter(self.repo, commit=True)
        return start_plan(
            source, self.sparring_dir, self.repo, _fixed(self.stage_adapter, _SparringAdapter([READY])),
            expected_branch="feature/x", run_key=self.RUN_KEY, **kwargs,
        )

    @staticmethod
    def _owed(case):
        (owed,) = PlanRunState.load(run_state_path(case.sparring_dir, GateEquivalenceTests.RUN_KEY)).deferred_human_checks
        return owed

    def _shape(self, result, owed):
        check = owed.gate.checks[0]
        return (
            result.status,
            result.awaiting.reason,
            [sid for sid, _ in result.accepted],
            owed.stage_id,
            owed.checkpoint,
            owed.manifest_gate,
            owed.rationale,
            owed.gate.category,
            owed.gate.title,
            (check.id, check.instruction, check.pass_criteria),
        )

    def test_same_obligation_checkpoint_and_stop(self):
        markdown = self._markdown()
        first, second = markdown.stages()
        result = self._run(markdown)
        md_owed = self._owed(self)
        md_shape = self._shape(result, md_owed)
        self.assertEqual(md_owed.checkpoint, before_stage_checkpoint(second.stage_id))
        self.assertEqual(result.awaiting.reason, DeferredVerificationRequired.BEFORE_STAGE)
        self.assertFalse((self.sparring_dir / "stages" / second.stage_id).exists())
        self.assertIn(REASON, md_owed.gate.checks[0].instruction)
        self.assertEqual(md_owed.rationale, REASON)

        # Released only by an explicit pass.
        again = resume_plan(
            markdown, self.sparring_dir, self.repo,
            _fixed(self.stage_adapter, _SparringAdapter([])), expected_branch="feature/x",
            deferred_results=(DeferredAnswer.parse("canary=blocked"),),
        )
        self.assertIs(again.status, PlanRunStatus.PAUSED)
        done = resume_plan(
            markdown, self.sparring_dir, self.repo,
            _fixed(self.stage_adapter, _SparringAdapter([READY])), expected_branch="feature/x",
            deferred_results=(DeferredAnswer.parse(f"{md_owed.instance_id}:canary=pass"),),
        )
        self.assertIs(done.status, PlanRunStatus.COMPLETE)

        # The equivalent v2 manifest, in a fresh repository, stops the same way.
        other = _ManifestRepoTestCase()
        other.setUp()
        self.addCleanup(other.doCleanups)
        payload = manifest_payload(
            [{"stage_id": s.stage_id, "label": s.label, "title": s.title, "brief": s.brief} for s in (first, second)]
        )
        payload["version"] = 2
        payload["stages"][1]["gates_before"] = [
            {"id": "canary", "title": "Staging canary", "kind": "human", "reason": REASON}
        ]
        other.write_manifest(payload)
        result = start_plan(
            other.source(), other.sparring_dir, other.repo,
            _fixed(_StageAdapter(other.repo, commit=True), _SparringAdapter([READY])),
            expected_branch="feature/x", run_key=self.RUN_KEY,
        )
        self.assertEqual(self._shape(result, self._owed(other)), md_shape)


if __name__ == "__main__":
    unittest.main()
