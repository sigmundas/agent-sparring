"""``sparring start-plan``: routing, decisions, the confirm token and sealing.

The intake agent turn is a recorded interpretation (``CodexCliAdapter.
start_structured`` patched); the run itself is not exercised here -- the
confirm path is checked up to the in-process run-plan entry, which is
patched to record what it would run.
"""

import contextlib
import hashlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import plan_start
from agent_sparring.cli import main
from agent_sparring.intake import IntakeError, answered_decisions, build_approval, prepare_plan
from agent_sparring.intake_approval import IntakeApprovalError, load_intake_manifest
from agent_sparring.loop import DEFAULT_MAX_SEND_BACK_CYCLES
from agent_sparring.plan import load_markdown_source
from agent_sparring.providers.claude_cli import DEFAULT_PERMISSION_MODE
from agent_sparring.plan_start import confirm_token
from agent_sparring.providers import SparringAgentResult
from test_intake import PLAN, FakeAdapter, _git, _Repo, make_repo
from test_intake_compile import CLOUD, cloud_interpretation, compile_widget_interpretation, finding

DIRECT = """\
# Direct plan

## Stage 1 — Foundation

Build it.

## Stage 2 — Polish

Polish it.
"""

DECISION = {
    "id": "audit-depth",
    "question": "Audit every widget or a sample?",
    "why": "The plan says both.",
    "options": [
        {"id": "all", "label": "Every widget", "consequence": "slower audit"},
        {"id": "sample", "label": "A 5% sample", "consequence": "counts are estimates"},
    ],
}


def ambiguous_interpretation() -> dict:
    payload = compile_widget_interpretation()
    payload["findings"].append(finding("other", "needs_decision", ["0"], decision=DECISION, message="ambiguous audit"))
    return payload


def answer(payload) -> SparringAgentResult:
    return SparringAgentResult(session_id="t", text=json.dumps(payload), is_error=False)


def tree(directory: Path) -> dict:
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


class _Start(_Repo):
    def start(self, *argv, answers=(), plan=None, confirm=None, json_out=True):
        args = ["start-plan", str(plan or self.plan), "--repo-root", str(self.repo), "--expected-branch",
                self.branch(), *argv]
        for value in answers:
            args += ["--answer", value]
        if confirm:
            args += ["--confirm", confirm]
        if json_out:
            args.append("--json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *args])
        payload = json.loads(out.getvalue()) if json_out and out.getvalue().strip().startswith("{") else None
        return code, payload, out.getvalue() + err.getvalue()

    def branch(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def intakes(self) -> list[Path]:
        root = self.sparring_dir / "intake"
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name != "registry") if root.is_dir() else []

    def agent(self, *payloads):
        return mock.patch(
            "agent_sparring.cli.CodexCliAdapter.start_structured", side_effect=[answer(p) for p in payloads]
        )

    def ran(self):
        return mock.patch("agent_sparring.cli._run_plan_command", return_value=0)


class DirectRouteTests(_Start):
    def setUp(self):
        super().setUp()
        (self.repo / "docs" / "direct.md").write_text(DIRECT, encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "direct")
        self.direct = self.repo / "docs" / "direct.md"

    def test_a_direct_plan_is_ready_with_no_intake(self):
        with self.agent() as agent:
            code, status, out = self.start(plan=self.direct)
        self.assertEqual(code, 0, out)
        self.assertEqual((status["status"], status["route"], status["intake"]), ("ready", "direct", None))
        self.assertEqual([s["label"] for s in status["slice"]["stages"]], ["Stage 1", "Stage 2"])
        self.assertRegex(status["confirm_token"], "^[0-9a-f]{64}$")
        agent.assert_not_called()
        self.assertEqual(self.intakes(), [])

    def test_confirm_runs_exactly_the_run_plan_path(self):
        _, status, _ = self.start(plan=self.direct)
        with self.ran() as run:
            code, _, out = self.start(plan=self.direct, confirm=status["confirm_token"], json_out=False)
        self.assertEqual(code, 0, out)
        args = run.call_args.args[0]
        self.assertEqual((args.plan_path, args.manifest, args.run_key), (str(self.direct), None, None))
        self.assertEqual(set(run.call_args.kwargs), {"resume", "source"})
        self.assertFalse(run.call_args.kwargs["resume"])
        self.assertEqual(run.call_args.kwargs["source"].digest(), load_markdown_source(self.direct, "docs/direct.md").digest())
        self.assertEqual(self.intakes(), [])

    def test_a_plan_edited_after_the_token_check_is_not_run(self):
        _, status, _ = self.start(plan=self.direct)
        checked = load_markdown_source(self.direct, "docs/direct.md").digest()
        real = plan_start.evaluate

        def check_then_edit(*args, **kwargs):
            result = real(*args, **kwargs)
            # The file changes after the token check, before the run.
            self.direct.write_text(DIRECT.replace("Build it.", "Delete everything."), encoding="utf-8")
            return result

        with mock.patch.object(plan_start, "evaluate", side_effect=check_then_edit), self.ran() as run:
            code, _, out = self.start(plan=self.direct, confirm=status["confirm_token"], json_out=False)
        self.assertEqual(code, 0, out)
        source = run.call_args.kwargs["source"]
        self.assertEqual(source.digest(), checked)
        self.assertNotIn("Delete everything", "".join(stage.brief for stage in source.stages()))

    def test_another_sparring_dir_refuses_the_token_and_writes_nothing(self):
        _, status, _ = self.start(plan=self.direct)
        self.sparring_dir = self.root / "other-sparring"
        _, other, _ = self.start(plan=self.direct)
        self.assertEqual(other["execution"]["sparring_dir"], str(self.sparring_dir.resolve()))
        self.assertNotEqual(other["confirm_token"], status["confirm_token"])
        with self.ran() as run:
            code, refused, _ = self.start(plan=self.direct, confirm=status["confirm_token"])
        self.assertEqual((code, refused["status"]), (1, "refused"))
        run.assert_not_called()
        self.assertFalse(self.sparring_dir.exists())

    def test_every_execution_input_is_shown_and_bound(self):
        _, status, _ = self.start(plan=self.direct)
        self.assertEqual(
            status["execution"],
            {"sparring_dir": str(self.sparring_dir.resolve()), "permission_mode": DEFAULT_PERMISSION_MODE, "claude_executable": "claude", "codex_executable": "codex",
             "max_send_back_cycles": DEFAULT_MAX_SEND_BACK_CYCLES},
        )
        for flag, value in EXECUTION_CHANGES:
            with self.subTest(flag):
                _, changed, _ = self.start(flag, value, plan=self.direct)
                self.assertNotEqual(changed["confirm_token"], status["confirm_token"])
                with self.ran() as run:
                    code, refused, _ = self.start(flag, value, plan=self.direct, confirm=status["confirm_token"])
                self.assertEqual((code, refused["status"]), (1, "refused"))
                self.assertIn("execution options", refused["error"])
                run.assert_not_called()
        code, _, out = self.start("--permission-mode", "bypassPermissions", plan=self.direct, json_out=False)
        self.assertEqual(code, 0, out)
        self.assertIn("permission mode bypassPermissions", out)
        self.assertIn(f"sparring dir {self.sparring_dir.resolve()}", out)

    def test_a_changed_plan_or_push_choice_refuses_the_token(self):
        _, status, _ = self.start(plan=self.direct)
        with self.ran() as run:
            code, refused, _ = self.start("--allow-push-for-run", plan=self.direct, confirm=status["confirm_token"])
        self.assertEqual((code, refused["status"]), (1, "refused"))
        self.assertIn("confirm token does not match", refused["error"])
        run.assert_not_called()


    def test_a_supplied_sibling_is_bound_into_the_direct_token(self):
        _, alone, _ = self.start(plan=self.direct)
        _, with_web, _ = self.start("--context-repository", f"web={self.web}", plan=self.direct)
        self.assertNotEqual(alone["confirm_token"], with_web["confirm_token"])
        (self.web / "more.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", ".")
        _git(self.web, "commit", "-q", "-m", "move")
        _, moved, _ = self.start("--context-repository", f"web={self.web}", plan=self.direct)
        self.assertNotEqual(moved["confirm_token"], with_web["confirm_token"])
        with self.ran() as run:
            code, refused, _ = self.start(
                "--context-repository", f"web={self.web}", plan=self.direct, confirm=with_web["confirm_token"]
            )
        self.assertEqual((code, refused["status"]), (1, "refused"))
        run.assert_not_called()

    def test_a_sibling_branch_is_refused_on_the_direct_route(self):
        code, status, _ = self.start("--repository-branch", "web=feature/web", plan=self.direct)
        self.assertEqual((code, status["status"]), (1, "refused"))
        self.assertIn("--repository-branch", status["error"])


class CommandAndSchemaTests(_Start):
    def test_an_argument_refusal_still_reports_every_schema_key(self):
        code, status, _ = self.start(answers=["malformed"])
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "refused")
        self.assertIn("--answer expects NAME=VALUE", status["error"])
        self.assertEqual(
            set(status),
            {"schema_version", "status", "route", "plan", "expected_branch", "intake", "slice",
             "later_slices", "decisions", "findings", "confirm_token", "error", "execution"},
        )
        self.assertEqual(status["execution"]["permission_mode"], DEFAULT_PERMISSION_MODE)
        self.assertEqual(status["plan"]["label"], "docs/plan.md")
        self.assertEqual(status["expected_branch"], "feature/widgets")

    def test_printed_commands_keep_the_execution_inputs(self):
        (self.repo / "docs" / "direct.md").write_text(DIRECT, encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "direct")
        flags = ["--claude-executable", "/opt/claude", "--codex-executable", "/opt/codex",
                 "--permission-mode", "acceptEdits", "--max-send-back-cycles", "7"]
        code, _, out = self.start(*flags, plan=self.repo / "docs" / "direct.md", json_out=False)
        self.assertEqual(code, 0, out)
        printed = out.splitlines()[out.splitlines().index("start the run with:") + 1]
        for flag, value in zip(flags[::2], flags[1::2]):
            self.assertIn(f"{flag} {value}", printed)
        self.assertIn("--confirm ", printed)


class PreflightTests(_Start):
    def test_a_dirty_tree_refuses_before_any_provider_turn(self):
        (self.repo / "stray.txt").write_text("x\n", encoding="utf-8")
        with self.agent() as agent:
            code, status, _ = self.start()
        self.assertEqual((code, status["status"]), (1, "refused"))
        self.assertIn("uncommitted changes (stray.txt)", status["error"])
        agent.assert_not_called()

    def test_a_protected_branch_refuses_and_says_how_to_branch(self):
        _git(self.repo, "checkout", "-q", "main")
        with self.agent() as agent:
            code, status, _ = self.start()
        self.assertEqual((code, status["status"]), (1, "refused"))
        self.assertIn("protected branch 'main'", status["error"])
        self.assertIn("slice-branch", status["error"])
        agent.assert_not_called()

    def test_another_expected_branch_refuses(self):
        args = ["start-plan", str(self.plan), "--repo-root", str(self.repo), "--expected-branch", "feature/other", "--json"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--sparring-dir", str(self.sparring_dir), *args])
        self.assertEqual(code, 1)
        self.assertIn("expected branch 'feature/other'", json.loads(out.getvalue())["error"])

    def test_confirm_never_prepares(self):
        with self.agent() as agent:
            code, status, _ = self.start("--context-repository", f"web={self.web}", confirm="0" * 64)
        self.assertEqual((code, status["status"]), (1, "refused"))
        self.assertIn("--confirm never prepares", status["error"])
        agent.assert_not_called()
        self.assertEqual(self.intakes(), [])


class IntakeRouteTests(_Start):
    ctx = property(lambda self: ["--context-repository", f"web={self.web}"])

    def test_a_widget_plan_is_ready_without_a_manual_prepare_and_reused_after(self):
        with self.agent(compile_widget_interpretation()) as agent:
            code, status, out = self.start(*self.ctx)
        self.assertEqual(code, 0, out)
        self.assertEqual((status["status"], status["route"]), ("ready", "intake"))
        self.assertFalse(status["intake"]["reused"])
        self.assertEqual(status["slice"]["run_id"], "app")
        self.assertEqual([s["plan_stage_label"] for s in status["slice"]["stages"]], ["0", "1A"])
        self.assertEqual([s["run_id"] for s in status["later_slices"]], ["web"])
        self.assertEqual(agent.call_count, 1)
        (intake,) = self.intakes()
        self.assertFalse((intake / "runs").exists())  # dry: nothing approved

        with self.agent() as again:
            _, second, _ = self.start(*self.ctx)
        again.assert_not_called()
        self.assertTrue(second["intake"]["reused"])
        self.assertEqual(second["confirm_token"], status["confirm_token"])

    def test_confirm_seals_through_approve_plan_and_runs_the_sealed_manifest(self):
        with self.agent(compile_widget_interpretation()):
            _, status, _ = self.start(*self.ctx)
        with self.ran() as run:
            code, _, out = self.start(*self.ctx, confirm=status["confirm_token"], json_out=False)
        self.assertEqual(code, 0, out)
        (intake,) = self.intakes()
        approval = json.loads((intake / "runs/app/approval.json").read_text(encoding="utf-8"))
        self.assertEqual(approval["approved_via"], "start-plan")
        args = run.call_args.args[0]
        self.assertEqual((args.plan_path, args.manifest), (None, str(intake / "runs/app/manifest.json")))
        self.assertEqual(args.run_key, status["slice"]["run_key"])
        self.assertEqual(load_intake_manifest(Path(args.manifest)).approval.run_key, args.run_key)
        # A retried confirm with the same token reuses the identical approval.
        before = (intake / "runs/app/approval.json").read_bytes()
        with self.ran():
            code, _, out = self.start(*self.ctx, confirm=status["confirm_token"], json_out=False)
        self.assertEqual(code, 0, out)
        self.assertIn("already approved (identical)", out)
        self.assertEqual((intake / "runs/app/approval.json").read_bytes(), before)

    def test_approve_plan_then_start_plan_confirm_is_not_a_conflict(self):
        with self.agent(compile_widget_interpretation()):
            _, status, _ = self.start(*self.ctx)
        (intake,) = self.intakes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(
                main(["--sparring-dir", str(self.sparring_dir), "approve-plan", str(intake), "--run", "app",
                      "--repo-root", str(self.repo)]),
                0,
                out.getvalue(),
            )
        before = (intake / "runs/app/approval.json").read_bytes()
        self.assertEqual(load_intake_manifest(intake / "runs/app/manifest.json").manifest.version, 1)
        with self.ran() as run:
            code, _, text = self.start(*self.ctx, confirm=status["confirm_token"], json_out=False)
        self.assertEqual(code, 0, text)
        self.assertIn("already approved (identical)", text)
        self.assertEqual((intake / "runs/app/approval.json").read_bytes(), before)
        self.assertNotIn("approved_via", json.loads(before))
        run.assert_called_once()

    def test_needs_decision_then_answer_then_ready_and_the_source_is_untouched(self):
        with self.agent(ambiguous_interpretation()):
            code, status, out = self.start(*self.ctx)
        self.assertEqual(code, 2, out)
        self.assertEqual(status["status"], "needs_decision")
        self.assertIsNone(status["confirm_token"])
        self.assertEqual([d["id"] for d in status["decisions"]], ["audit-depth"])
        self.assertEqual([o["id"] for o in status["decisions"][0]["options"]], ["all", "sample"])
        (parent,) = self.intakes()
        parent_files = tree(parent)

        with self.agent(compile_widget_interpretation()) as agent:
            code, ready, out = self.start(*self.ctx, answers=["audit-depth=sample"])
        self.assertEqual(code, 0, out)
        self.assertEqual(ready["status"], "ready")
        self.assertIn("Preparation decisions (a person's answers)", agent.call_args.args[0])
        self.assertIn("A 5% sample", agent.call_args.args[0])
        self.assertEqual(self.plan.read_text(encoding="utf-8"), PLAN)
        self.assertEqual(tree(parent), parent_files)
        child = next(p for p in self.intakes() if p != parent)
        decisions = json.loads((child / "decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(decisions["parent"]["intake_id"], parent.name)
        self.assertEqual(decisions["answers"][0]["option"],
                         {"id": "sample", "label": "A 5% sample", "consequence": "counts are estimates"})
        self.assertEqual(decisions["answers"][0]["question"], DECISION["question"])
        record = json.loads((child / "intake.json").read_text(encoding="utf-8"))
        self.assertEqual(record["decisions"]["answers"], {"audit-depth": "sample"})
        brief = (child / "briefs/app/01-0.md").read_text(encoding="utf-8")
        self.assertIn("# Preparation decision (not plan text)", brief)
        self.assertIn("Answer `sample`: A 5% sample", brief)
        self.assertNotIn("Preparation decision", (child / "briefs/app/02-1a.md").read_text(encoding="utf-8"))
        self.assertIn("## Preparation decisions", (child / "report.md").read_text(encoding="utf-8"))

        # The answered intake is reused; another answer is another intake and token.
        with self.agent() as again:
            _, same, _ = self.start(*self.ctx, answers=["audit-depth=sample"])
        again.assert_not_called()
        self.assertEqual(same["confirm_token"], ready["confirm_token"])
        with self.agent(compile_widget_interpretation()):
            _, other, _ = self.start(*self.ctx, answers=["audit-depth=all"])
        self.assertEqual(other["status"], "ready")
        self.assertNotEqual(other["confirm_token"], ready["confirm_token"])

        # Sealed and confirmed, decisions.json is re-checked before every run.
        with self.ran():
            self.assertEqual(self.start(*self.ctx, answers=["audit-depth=sample"], confirm=ready["confirm_token"],
                                        json_out=False)[0], 0)
        (child / "decisions.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(IntakeApprovalError, "decisions.json changed"):
            load_intake_manifest(child / "runs/app/manifest.json")

    def test_an_unknown_decision_or_option_refuses_before_any_turn(self):
        with self.agent(ambiguous_interpretation()):
            self.start(*self.ctx)
        for value, expected in (("nope=all", "no prepared intake of this plan asks"), ("audit-depth=most", "offers")):
            with self.subTest(value), self.agent() as agent:
                code, status, _ = self.start(*self.ctx, answers=[value])
                self.assertEqual(code, 1)
                self.assertIn(expected, status["error"])
                agent.assert_not_called()

    def test_answering_refuses_a_changed_source(self):
        result = self.prepare(ambiguous_interpretation(), mode="compile")
        with self.assertRaisesRegex(IntakeError, "source plan changed"):
            answered_decisions(result.directory, {"audit-depth": "all"}, PLAN + "\nmore\n")

    def test_every_execution_input_is_bound_on_the_intake_route(self):
        with self.agent(compile_widget_interpretation()):
            _, status, _ = self.start(*self.ctx)
        self.assertEqual(status["execution"]["codex_executable"], "codex")
        for flag, value in EXECUTION_CHANGES:
            with self.subTest(flag), self.agent() as agent:
                _, changed, _ = self.start(*self.ctx, flag, value)
                self.assertEqual(changed["execution"][flag[2:].replace("-", "_")], int(value) if value.isdigit() else value)
                self.assertNotEqual(changed["confirm_token"], status["confirm_token"])
                with self.ran() as run:
                    code, refused, _ = self.start(*self.ctx, flag, value, confirm=status["confirm_token"])
                self.assertEqual((code, refused["status"]), (1, "refused"))
                run.assert_not_called()
                agent.assert_not_called()
        (intake,) = self.intakes()
        self.assertFalse((intake / "runs").exists())

    def test_a_branch_for_a_repository_not_given_refuses_before_any_preparation(self):
        before = tree(self.repo)
        with self.agent() as agent:
            code, refused, _ = self.start(*self.ctx, "--repository-branch", "ghost=feature/x")
        self.assertEqual((code, refused["status"]), (1, "refused"))
        self.assertIn("['ghost']", refused["error"])
        self.assertIn("['web']", refused["error"])
        agent.assert_not_called()
        self.assertEqual(self.intakes(), [])
        self.assertEqual(tree(self.repo), before)

    def test_an_undeclared_repository_branch_is_refused_and_nothing_is_approved(self):
        with self.agent(compile_widget_interpretation()):
            _, status, _ = self.start(*self.ctx)
        (intake,) = self.intakes()
        before = tree(self.sparring_dir)
        for confirm in (None, status["confirm_token"]):
            with self.subTest(confirm=confirm), self.ran() as run:
                # web is given but slice "app" declares no siblings.
                code, refused, _ = self.start(*self.ctx, "--repository-branch", "web=feature/x", confirm=confirm)
                self.assertEqual((code, refused["status"]), (1, "refused"))
                self.assertIn("['web']", refused["error"])
                self.assertIn("does not declare", refused["error"])
                self.assertIsNone(refused["confirm_token"])
                run.assert_not_called()
        self.assertEqual(tree(self.sparring_dir), before)
        self.assertFalse((intake / "runs").exists())

    def test_the_token_changes_when_the_report_or_a_gate_changes(self):
        repo = self.root / "cloud"
        self.repo = make_repo(repo, "feature/cloud", {"docs/cloud.md": CLOUD})
        self.plan, self.sparring_dir = repo / "docs" / "cloud.md", repo / ".sparring"
        ctx = ["--repository-name", "app", "--context-repository", f"web={self.web}"]
        with self.agent(cloud_interpretation()):
            _, first, _ = self.start(*ctx)
        tokens = {first["confirm_token"]}
        regated = cloud_interpretation()
        regated["gates"][0]["reason"] = "only if Stage 2's metrics regress badly"
        resummarized = cloud_interpretation()
        resummarized["summary"] = "Another summary, so another report."
        for interpretation in (regated, resummarized):
            # A newer intake of the same plan and repositories: the one start-plan reuses.
            self.prepare(interpretation, mode="compile", context={"web": self.web})
            with self.agent() as agent:
                _, status, _ = self.start(*ctx)
            agent.assert_not_called()
            self.assertTrue(status["intake"]["reused"])
            self.assertEqual(status["status"], "ready")
            self.assertNotIn(status["confirm_token"], tokens)
            tokens.add(status["confirm_token"])
            with self.ran() as run:
                code, _, _ = self.start(*ctx, confirm=first["confirm_token"])
            self.assertEqual(code, 1)
            run.assert_not_called()

    def test_the_token_changes_when_a_sibling_head_moves(self):
        repo = self.root / "cloud"
        self.repo = make_repo(repo, "feature/cloud", {"docs/cloud.md": CLOUD})
        # The cloud fixture names the project "app".
        self.plan, self.sparring_dir = repo / "docs" / "cloud.md", repo / ".sparring"
        ctx = ["--repository-name", "app", "--context-repository", f"web={self.web}"]
        with self.agent(cloud_interpretation()):
            code, status, out = self.start(*ctx)
        self.assertEqual(code, 0, out)
        stages = status["slice"]["stages"]
        self.assertEqual(status["slice"]["manifest_version"], 2)
        self.assertEqual([g["id"] for s in stages for g in s["gates_before"]], ["canary"])
        (intake,) = self.intakes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(
                main(["--sparring-dir", str(self.sparring_dir), "approve-plan", str(intake), "--run", "app",
                      "--repo-root", str(self.repo), "--repository-name", "app", "--repository", f"web={self.web}",
                      "--repository-branch", "web=feature/web"]),
                0,
                out.getvalue(),
            )
        _, approved, _ = self.start(*ctx)
        self.assertEqual(approved["confirm_token"], status["confirm_token"])
        (self.web / "more.txt").write_text("x\n", encoding="utf-8")
        _git(self.web, "add", ".")
        _git(self.web, "commit", "-q", "-m", "move")
        _, moved, _ = self.start(*ctx)
        self.assertEqual(moved["status"], "ready")
        self.assertNotEqual(moved["confirm_token"], status["confirm_token"])
        with self.ran() as run:
            code, refused, _ = self.start(*ctx, confirm=status["confirm_token"])
        self.assertEqual((code, refused["status"]), (1, "refused"))
        run.assert_not_called()


EXECUTION_CHANGES = (
    ("--permission-mode", "bypassPermissions"),
    ("--claude-executable", "/opt/other/claude"),
    ("--codex-executable", "/opt/other/codex"),
    ("--max-send-back-cycles", "9"),
)


class TokenTests(unittest.TestCase):
    base = {
        "route": "intake", "intake_id": "i", "intake_record_sha256": "r", "report_digest": "p",
        "interpretation_digest": "n", "decisions_digest": None, "run_id": "app", "run_key": "k",
        "manifest_digest": "m", "source_digest": "s", "expected_branch": "b",
        "repositories": [{"name": "app", "path": "/a", "git_common_dir": "/a/.git", "branch": "b", "head": "h"}],
        "models": {"stage": {"provider": "claude-cli", "model": None, "effort": None}},
        "allow_push_for_run": False,
        "execution": {"permission_mode": "default", "claude_executable": "claude", "codex_executable": "codex",
                      "max_send_back_cycles": 3},
    }

    def test_every_bound_input_changes_the_token(self):
        token = confirm_token(self.base)
        self.assertEqual(token, confirm_token(dict(reversed(list(self.base.items())))))
        for key, value in (
            ("report_digest", "p2"), ("decisions_digest", "d"), ("manifest_digest", "m-with-gate"),
            ("intake_record_sha256", "r2"), ("allow_push_for_run", True),
            ("repositories", [{**self.base["repositories"][0], "head": "h2"}]),
            ("models", {"stage": {"provider": "claude-cli", "model": "x", "effort": None}}),
            *(("execution", {**self.base["execution"], key: value}) for key, value in (
                ("permission_mode", "bypassPermissions"), ("claude_executable", "/x/claude"),
                ("codex_executable", "/x/codex"), ("max_send_back_cycles", 4),
            )),
        ):
            with self.subTest(key):
                self.assertNotEqual(confirm_token({**self.base, key: value}), token)


class BuildSealTests(_Repo):
    def test_build_writes_nothing_and_matches_what_approve_plan_seals(self):
        result = self.prepare(compile_widget_interpretation(), mode="compile")
        before = tree(self.sparring_dir)
        pending = build_approval(
            result.directory, run_id="app", primary_repository="app", repo_root=self.repo, sparring_dir=self.sparring_dir
        )
        self.assertEqual(tree(self.sparring_dir), before)
        self.assertIsNotNone(pending.payload)
        from agent_sparring.intake import approve_plan

        approval = approve_plan(
            result.directory, run_id="app", primary_repository="app", repo_root=self.repo, sparring_dir=self.sparring_dir
        )
        self.assertEqual(approval.manifest_digest, pending.manifest_digest)
        self.assertEqual(approval.manifest_path.read_text(encoding="utf-8"), pending.manifest_text)
        written = json.loads(approval.approval_path.read_text(encoding="utf-8"))
        self.assertEqual({k: v for k, v in written.items() if k not in ("version", "decision", "approved_at")},
                         dict(pending.payload))
        # Built again with the approval present: it agrees, and seals nothing new.
        self.assertIsNone(build_approval(
            result.directory, run_id="app", primary_repository="app", repo_root=self.repo, sparring_dir=self.sparring_dir
        ).payload)

    def test_prepare_answers_need_compile_mode(self):
        result = self.prepare(ambiguous_interpretation(), mode="compile")
        with self.assertRaisesRegex(IntakeError, "only a compile prepare"):
            prepare_plan(self.plan, self.repo, self.sparring_dir, FakeAdapter({}), mode="faithful",
                         primary_repository="app", answer_parent=result.directory, answers={"audit-depth": "all"})


if __name__ == "__main__":
    unittest.main()
