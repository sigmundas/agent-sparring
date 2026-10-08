"""Resume hints a paused run prints: a managed run is addressed by its key
alone; an unmanaged run keeps its full plan/repo/branch form."""

import argparse
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace

import conftest_path  # noqa: F401

from agent_sparring import cli
from agent_sparring.deferred_gate import DeferredVerificationRequired
from agent_sparring.plan import PlanRunResult, PlanRunStatus
from agent_sparring.routing import RoutingAction

SHORT = "sparring resume-plan --run-key K1"
LONG = "sparring resume-plan docs/plans/x.md --run-key K1 --repo-root /wt --expected-branch sparring/x"


class _Stage:
    directory = Path("/wt/.sparring/stages/s1")

    def read_sparring(self) -> str:
        return "review"


def _args(managed: bool) -> argparse.Namespace:
    return argparse.Namespace(
        plan_path="docs/plans/x.md",
        manifest=None,
        repo_root="/wt",
        expected_branch="sparring/x",
        run_key="K1",
        managed_record=SimpleNamespace(run_key="K1") if managed else None,
    )


def _report(result: PlanRunResult, managed: bool) -> str:
    out = io.StringIO()
    stage = _Stage()
    original = cli.Stage.resolve
    cli.Stage.resolve = staticmethod(lambda *_: stage)
    try:
        with contextlib.redirect_stdout(out):
            cli._report_plan_result(result, _args(managed), Path("/wt/.sparring"))
    finally:
        cli.Stage.resolve = original
    return out.getvalue()


def _result(**fields) -> PlanRunResult:
    return PlanRunResult(status=PlanRunStatus.PAUSED, plan="x", stage_id="s1", run="K1", **fields)


_STOPPED = {}
_VERDICT = {
    "routing": SimpleNamespace(
        action=RoutingAction.NEEDS_YOU, needs_you_reason=None, summary="s", human_gate=None
    )
}
_PUSH = {
    "awaiting": SimpleNamespace(
        candidate_sha="abc", branch="sparring/x", remote="origin", remote_branch="sparring/x", detail=""
    )
}
_DEFERRED = {"awaiting": DeferredVerificationRequired(kind="k", reason="r", instance_ids=())}


class ResumeHintTests(unittest.TestCase):
    def _check(self, fields, suffix=""):
        managed = _report(_result(**fields), managed=True)
        self.assertIn(SHORT + suffix, managed)
        self.assertNotIn("--repo-root", managed)
        self.assertNotIn("--expected-branch", managed)
        self.assertNotIn("docs/plans/x.md", managed)
        self.assertIn(LONG + suffix, _report(_result(**fields), managed=False))

    def test_stop_after_stage(self):
        self._check(_STOPPED)

    def test_verdict_pause(self):
        self._check(_VERDICT, " --evidence")

    def test_push_authorization(self):
        self._check(_PUSH, " --allow-push-candidate abc")

    def test_deferred_verification(self):
        self._check({**_DEFERRED, "deferred": ()}, " \\\n    --deferred-result")

    def test_retry_command_for_a_managed_run_is_key_only(self):
        args = _args(True)
        args.stop_after_stage = "s2"
        command = cli._retry_command(args, Path("/wt/.sparring"), Path("/wt"), cli._resume_retry_parts(args, "K1"))
        self.assertEqual(command, SHORT)
        args = _args(False)
        command = cli._retry_command(args, Path("/wt/.sparring"), Path("/wt"), cli._resume_retry_parts(args, "K1"))
        self.assertIn("--repo-root /wt --expected-branch sparring/x", command)
        self.assertIn(str(Path("docs/plans/x.md").resolve()), command)


class _Mode:
    value = "m"
    describe = "d"


_RESET = SimpleNamespace(
    stage_id="s1", run="K1", from_mode=_Mode(), to_mode=_Mode(), stage_directory="/wt/s",
    archive="/wt/a", discarded_sessions=(), reviewed_stage="r", reviewed_stage_id="r1",
    reviewed_sha="abc", files=(), previous_digest="d", digest="d",
)
_REOPEN = SimpleNamespace(
    stage_id="s1", run="K1", instance_id="i", title="t", failed_checks=(), candidate_sha="abc",
    stage_directory="/wt/s",
)


class RecoveryResumeHintTests(unittest.TestCase):
    """reset-stage and reopen-stage are not managed-aware themselves; the
    hint they print still names a recorded managed run by its key."""

    def _run(self, command, managed: bool) -> str:
        args = _args(False)
        args.sparring_dir = "/wt/.sparring"
        args.stage_id = "s1"
        args.mode = None
        args.gate_instance = "i"
        record = SimpleNamespace(run_key="K1", branch="sparring/x") if managed else None
        patches = {
            (cli, "_resolve_repo_root"): lambda *_: Path("/wt"),
            (cli, "_plan_source"): lambda *_: None,
            (cli, "reset_stage"): lambda *a, **k: _RESET,
            (cli, "reopen_for_failed_check"): lambda *a, **k: _REOPEN,
            (cli.managed_run, "read_record"): lambda *_: record,
        }
        saved = {key: getattr(*key) for key in patches}
        out = io.StringIO()
        try:
            for (owner, name), value in patches.items():
                setattr(owner, name, value)
            with contextlib.redirect_stdout(out):
                self.assertEqual(command(args), 0)
        finally:
            for (owner, name), value in saved.items():
                setattr(owner, name, value)
        return out.getvalue()

    def _check(self, command):
        managed = self._run(command, managed=True)
        self.assertIn(f"  {SHORT}\n", managed)
        self.assertNotIn("--expected-branch", managed)
        self.assertIn(f"  {LONG}\n", self._run(command, managed=False))

    def test_reset_stage(self):
        self._check(cli._cmd_reset_stage)

    def test_reopen_stage(self):
        self._check(cli._cmd_reopen_stage)


class DriftRefusalTests(unittest.TestCase):
    """A finalization-drift refusal offers reset-stage only to an unmanaged run."""

    def _printed(self, managed: bool) -> str:
        from agent_sparring.next_turn import FinalizationDrift

        args = _args(managed)
        cause = FinalizationDrift("drift", stage_id="s1")
        reset = cli._drift_reset_command(args, Path("/wt/.sparring"), Path("/wt"), cause)
        rerun = cli._retry_command(args, Path("/wt/.sparring"), Path("/wt"), cli._resume_retry_parts(args, "K1"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._print_next_commands(cause, rerun, reset=reset)
        return err.getvalue()

    def test_managed_refusal_prints_only_the_short_resume(self):
        printed = self._printed(managed=True)
        self.assertIn(f"  {SHORT}\n", printed)
        self.assertNotIn("reset-stage", printed)
        self.assertNotIn("discard", printed)
        self.assertNotIn("/wt", printed)

    def test_unmanaged_refusal_keeps_the_reset_alternative(self):
        printed = self._printed(managed=False)
        self.assertIn("Or deliberately discard this attempt:", printed)
        self.assertIn(
            "reset-stage s1 " + str(Path("docs/plans/x.md").resolve())
            + " --repo-root /wt --expected-branch sparring/x",
            printed,
        )
