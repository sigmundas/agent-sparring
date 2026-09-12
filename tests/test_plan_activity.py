"""The plan runner and the observational activity stream.

Three things are proven here, all with scripted providers (no real
Claude/Codex calls):

1. Per-stage binding: a planned stage's provider telemetry (session,
   tool, file, command, result events) lands in THAT stage's
   ``activity.jsonl`` -- end to end through the real CLI, the real
   streaming runner and both real adapters against fake executables.
2. Chronology: the plan runner's own ``plan.*`` events mirror what it did,
   in order, into the current stage's log.
3. Non-authority: making a planned stage's log unusable, pre-seeding it
   with a fabricated outcome, or deleting it mid-run changes nothing the
   plan runner decides; the run-state file and each ``state.json`` stay the
   only authority.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.acceptance import accept_candidate, freeze_candidate
from agent_sparring.cli import main
from agent_sparring.plan import (
    PlanRunError,
    PlanRunStatus,
    plan_state_path,
    resume_plan,
    start_plan,
)
from agent_sparring.providers import ProviderError
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import Stage, StageStatus
from test_plan import (
    ESCALATE,
    NEEDS_YOU,
    PLAN,
    READY,
    S1,
    S2,
    SEND_BACK,
    _fixed,
    _head_sha,
    _PlanRepoTestCase,
    _run_git,
    _SparringAdapter,
    _StageAdapter,
)

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _events(stage: Stage) -> list[dict]:
    path = stage.activity_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _names(stage: Stage, *actors: str) -> list[str]:
    return [
        f"{e['actor']}:{e['event']}"
        for e in _events(stage)
        if not actors or e["actor"] in actors
    ]


def _plan_events(stage: Stage) -> list[dict]:
    return [e for e in _events(stage) if e["actor"] == "plan"]


class _PlanActivityCase(_PlanRepoTestCase):
    def _s1(self) -> Stage:
        return self._stage(S1)

    def _s2(self) -> Stage:
        return self._stage(S2)

    def _authoritative_snapshot(self) -> dict[str, str]:
        """Everything the plan runner decides from, with commit ids
        normalized so two repositories can be compared byte for byte."""

        snapshot = {"plan": self.state_path.read_text(encoding="utf-8")}
        for stage_id in (S1, S2):
            stage = self._stage(stage_id)
            if stage.exists():
                snapshot[stage_id] = (stage.directory / "state.json").read_text(encoding="utf-8")
            else:
                snapshot[stage_id] = "<absent>"
        return {key: _SHA_RE.sub("<sha>", value) for key, value in snapshot.items()}


# -- per-stage provider binding, end to end ---------------------------------


class CliPlanProviderBindingTests(_PlanActivityCase):
    """`run-plan` through the real CLI against fake ``claude``/``codex``
    executables that print canned structured output and mint a session id
    per fresh start, so each planned stage gets genuinely different ids."""

    def setUp(self):
        super().setUp()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        self.claude = self._script(
            "fake-claude",
            """
import json, os, sys, time
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
if "--resume" in args:
    sid = args[args.index("--resume") + 1]
else:
    counter = os.path.join(here, "claude-starts")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    sid = f"impl-{n + 1}"
lines = [
    {"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                              "input": {"file_path": "f.txt", "old_string": "OLD-LEAK",
                                        "new_string": "NEW-LEAK"}}]}},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                              "input": {"command": "pytest -q CMD-LEAK"}}]}},
    {"type": "user", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                              "content": "OUTPUT-LEAK 3 passed"}]}},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Read",
                              "input": {"file_path": "f.txt"}}]}},
    {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
     "message": {"content": [{"type": "tool_use", "id": "t4", "name": "Task",
                              "input": {"description": "SUBAGENT-LEAK", "prompt": "PROMPT-LEAK"}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "implemented",
     "session_id": sid, "num_turns": 3},
]
for line in lines:
    print(json.dumps(line), flush=True)
    time.sleep(0.01)
""",
        )
        self.codex = self._script(
            "fake-codex",
            """
import json, os, sys, time
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
out = args[args.index("-o") + 1]
counter = os.path.join(here, "codex-calls")
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
if "resume" in args:
    tid = args[args.index("resume") + 1]
else:
    tid = f"thread-{n + 1}"
action = ["SEND_BACK", "READY", "READY"][n]
verdict = {"action": action, "summary": f"verdict {n}", "needs_you_reason": None,
           "findings": "FINDINGS-LEAK", "deferred": None}
lines = [
    {"type": "thread.started", "thread_id": tid},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "REASONING-LEAK"}},
    {"type": "item.started", "item": {"id": "c", "type": "command_execution",
                                      "command": "git status CMD-LEAK", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "c", "type": "command_execution",
                                        "command": "git status CMD-LEAK",
                                        "aggregated_output": "OUTPUT-LEAK", "exit_code": 0,
                                        "status": "completed"}},
    {"type": "item.completed", "item": {"id": "m", "type": "agent_message",
                                        "text": json.dumps(verdict)}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
]
for line in lines:
    print(json.dumps(line), flush=True)
    time.sleep(0.01)
open(out, "w").write(json.dumps(verdict))
""",
        )

    def _script(self, name: str, body: str) -> Path:
        script = self.bin / f"{name}.py"
        script.write_text(body.lstrip("\n"), encoding="utf-8")
        wrapper = self.bin / name
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def test_each_planned_stage_streams_its_own_provider_events(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--sparring-dir", str(self.sparring_dir),
                    "run-plan", str(self.plan_path),
                    "--repo-root", str(self.repo),
                    "--expected-branch", "feature/x",
                    "--claude-executable", str(self.claude),
                    "--codex-executable", str(self.codex),
                ]
            )
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("plan complete: docs/plan.md", stdout.getvalue())

        # Authoritative outcome first: sessions per stage from state.json.
        s1, s2 = self._s1().read_state(), self._s2().read_state()
        self.assertIs(s1.status, StageStatus.ACCEPTED)
        self.assertIs(s2.status, StageStatus.ACCEPTED)
        self.assertEqual((s1.implementation_session_id, s1.sparring_session_id), ("impl-1", "thread-1"))
        self.assertEqual((s2.implementation_session_id, s2.sparring_session_id), ("impl-2", "thread-3"))

        provider_events = (
            "session.observed", "tool.call", "file.changed", "command.started",
            "command.finished", "subagent.started", "provider.result",
        )
        for stage, impl, thread, cycles in ((self._s1(), "impl-1", "thread-1", 2),
                                            (self._s2(), "impl-2", "thread-3", 1)):
            events = _events(stage)
            provider = [e for e in events if e["event"] in provider_events]
            self.assertTrue(provider, stage.stage_id)
            # Every provider line in this stage's log names this stage's
            # own sessions and nobody else's.
            self.assertEqual(
                {e.get("session_id") for e in provider if "session_id" in e}, {impl, thread}
            )
            self.assertEqual(
                [e["provider"] for e in events if e["event"] == "session.observed"],
                ["claude-cli", "codex-cli"] * cycles,
            )
            self.assertEqual(
                [e["path"] for e in events if e["event"] == "file.changed"], ["f.txt"] * cycles
            )
            self.assertEqual(
                [e["tool"] for e in events if e["event"] == "tool.call"], ["Read"] * cycles
            )
            self.assertEqual(
                [e["tool_use_id"] for e in events if e["event"] == "subagent.started"],
                ["t4"] * cycles,
            )
            self.assertEqual(
                len([e for e in events if e["event"] == "command.finished"
                     and e["actor"] == "sparrer"]),
                cycles,
            )

        # Nothing from stage 1's sessions leaked into stage 2's log, and
        # vice versa.
        s2_text = self._s2().activity_path().read_text(encoding="utf-8")
        self.assertNotIn("impl-1", s2_text)
        self.assertNotIn("thread-1", s2_text)
        s1_text = self._s1().activity_path().read_text(encoding="utf-8")
        self.assertNotIn("impl-2", s1_text)
        self.assertNotIn("thread-3", s1_text)

        # The control skeleton around those provider events, per stage.
        self.assertEqual(
            _names(self._s1(), "plan", "loop", "gate"),
            [
                "plan:plan.stage.entered",
                "loop:loop.started",
                "loop:loop.send_back",
                "loop:loop.stopped",
                "gate:candidate.frozen",
                "gate:candidate.accepted",
                "plan:plan.stage.accepted",
            ],
        )
        self.assertEqual(
            _names(self._s2(), "plan", "loop", "gate"),
            [
                "plan:plan.stage.entered",
                "loop:loop.started",
                "loop:loop.stopped",
                "gate:candidate.frozen",
                "gate:candidate.accepted",
                "plan:plan.stage.accepted",
                "plan:plan.completed",
            ],
        )
        self.assertEqual(
            [e["sha"] for e in _plan_events(self._s2()) if e["event"] == "plan.stage.accepted"],
            [s2.candidate_sha],
        )

        for text in (s1_text, s2_text):
            for leak in ("OLD-LEAK", "NEW-LEAK", "CMD-LEAK", "OUTPUT-LEAK", "REASONING-LEAK",
                         "FINDINGS-LEAK", "SUBAGENT-LEAK", "PROMPT-LEAK", "3 passed",
                         "Lay the groundwork", "Render incrementally"):
                self.assertNotIn(leak, text)


# -- the adapter factory -----------------------------------------------------


class AdapterFactoryTests(_PlanActivityCase):
    def test_factory_is_called_once_per_stage_entered_for_execution(self):
        seen: list[str] = []
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([SEND_BACK, READY, READY])

        def make_adapters(stage: Stage):
            seen.append(stage.stage_id)
            self.assertTrue(stage.exists())
            return stage_adapter, sparring_adapter

        result = self._start_with(make_adapters)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        # Once per stage, not per SEND_BACK cycle.
        self.assertEqual(seen, [S1, S2])

    def test_fresh_adapter_objects_on_resume_still_resume_the_recorded_sessions(self):
        first_stage, first_sparring = _StageAdapter(self.repo), _SparringAdapter([NEEDS_YOU])
        self._start(first_stage, first_sparring)
        self.assertEqual(self._s1().read_state().implementation_session_id, "impl-1")

        # A new process: entirely new adapter objects, no memory of the first.
        second_stage, second_sparring = _StageAdapter(self.repo), _SparringAdapter([READY, READY])
        result = self._resume(second_stage, second_sparring, evidence="checked")

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        # Continuity came from state.json's ids handed to resume(), not from
        # the adapter objects of the earlier run.
        self.assertEqual([sid for sid, _ in second_stage.resume_calls], ["impl-1"])
        self.assertEqual([sid for sid, _ in second_sparring.resume_calls], ["spar-1"])
        self.assertEqual(second_stage.start_calls[0].count("Stage 2 of 2"), 1)
        s2 = self._s2().read_state()
        self.assertEqual((s2.implementation_session_id, s2.sparring_session_id), ("impl-1", "spar-1"))

    def test_factory_is_not_called_for_an_already_accepted_stage(self):
        stage_adapter = _StageAdapter(self.repo)
        self._start(stage_adapter, _SparringAdapter([ESCALATE]))
        freeze_candidate(self._s1(), self.sparring_dir, self.repo, expected_branch="feature/x")
        accept_candidate(self._s1(), self.repo, expected_branch="feature/x")

        seen: list[str] = []

        def make_adapters(stage: Stage):
            seen.append(stage.stage_id)
            return stage_adapter, _SparringAdapter([READY])

        result = self._resume_with(make_adapters)

        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(seen, [S2])
        entered = [e for e in _plan_events(self._s1()) if e["event"] == "plan.stage.entered"]
        self.assertEqual(
            [e["summary"] for e in entered],
            ["Stage 1/2", "Stage 1/2; already accepted, advancing"],
        )

    def test_factory_failure_pauses_and_is_mirrored_without_its_text(self):
        def make_adapters(stage: Stage):
            raise ProviderError("SECRET-PATH /Users/someone/bin/claude missing")

        with self.assertRaises(PlanRunError) as ctx:
            self._start_with(make_adapters)

        self.assertIn("could not build the provider adapters", str(ctx.exception))
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)
        self.assertIs(self._s1().read_state().status, StageStatus.WORKING)
        self.assertEqual(
            _plan_events(self._s1())[-1],
            {**_plan_events(self._s1())[-1], "event": "plan.failed",
             "summary": "adapter construction failed"},
        )
        self.assertNotIn("SECRET-PATH", self._s1().activity_path().read_text(encoding="utf-8"))

    def _start_with(self, make_adapters, **kwargs):
        return start_plan(
            self.plan_path, self.sparring_dir, self.repo, make_adapters,
            expected_branch="feature/x", **kwargs,
        )

    def _resume_with(self, make_adapters, **kwargs):
        return resume_plan(
            self.plan_path, self.sparring_dir, self.repo, make_adapters,
            expected_branch="feature/x", **kwargs,
        )


# -- plan event chronology ---------------------------------------------------


class PlanEventChronologyTests(_PlanActivityCase):
    def test_two_ready_stages(self):
        result = self._start(_StageAdapter(self.repo, commit=True), _SparringAdapter([READY, READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

        s1_events, s2_events = _plan_events(self._s1()), _plan_events(self._s2())
        self.assertEqual(
            [(e["event"], e.get("summary")) for e in s1_events],
            [("plan.stage.entered", "Stage 1/2"), ("plan.stage.accepted", None)],
        )
        self.assertEqual(
            [(e["event"], e.get("summary")) for e in s2_events],
            [
                ("plan.stage.entered", "Stage 2/2"),
                ("plan.stage.accepted", None),
                ("plan.completed", "2 stage(s) accepted"),
            ],
        )
        self.assertEqual(s1_events[1]["sha"], self._s1().read_state().candidate_sha)
        self.assertEqual(s2_events[1]["sha"], self._s2().read_state().candidate_sha)
        self.assertNotEqual(s1_events[1]["sha"], s2_events[1]["sha"])
        for event in s1_events + s2_events:
            self.assertEqual((event["v"], event["actor"]), (1, "plan"))
            self.assertRegex(event["ts"], r"^\d{4}-\d{2}-\d{2}T.*Z$")

    def test_send_back_stays_inside_the_same_stage(self):
        result = self._start(_StageAdapter(self.repo), _SparringAdapter([SEND_BACK, READY, READY]))
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

        self.assertEqual(
            _names(self._s1(), "plan", "loop"),
            [
                "plan:plan.stage.entered",
                "loop:loop.started",
                "loop:loop.send_back",
                "loop:loop.stopped",
                "plan:plan.stage.accepted",
            ],
        )
        self.assertEqual(
            _names(self._s2(), "plan", "loop"),
            [
                "plan:plan.stage.entered",
                "loop:loop.started",
                "loop:loop.stopped",
                "plan:plan.stage.accepted",
                "plan:plan.completed",
            ],
        )

    def test_needs_you_pause_then_resume_with_evidence(self):
        stage_adapter = _StageAdapter(self.repo)
        sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])
        paused = self._start(stage_adapter, sparring_adapter)
        self.assertIs(paused.status, PlanRunStatus.PAUSED)

        self.assertEqual(
            [(e["event"], e.get("action")) for e in _plan_events(self._s1())],
            [("plan.stage.entered", None), ("plan.paused", "NEEDS_YOU")],
        )
        self.assertFalse(self._s2().exists())

        evidence = "Tested on a Pixel 7: resume after 24h works. SECRET-EVIDENCE"
        result = self._resume(stage_adapter, sparring_adapter, evidence=evidence)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)

        self.assertEqual(
            [e["event"] for e in _plan_events(self._s1())],
            [
                "plan.stage.entered",
                "plan.paused",
                "plan.evidence_recorded",
                "plan.stage.entered",
                "plan.stage.accepted",
            ],
        )
        recorded = [e for e in _plan_events(self._s1()) if e["event"] == "plan.evidence_recorded"]
        self.assertEqual(set(recorded[0]), {"v", "ts", "actor", "event"})
        # The evidence lives in notes.md (authoritative, documented); the
        # log only says that it was recorded.
        self.assertIn("SECRET-EVIDENCE", self._s1().read_notes())
        self.assertNotIn("SECRET-EVIDENCE", self._s1().activity_path().read_text(encoding="utf-8"))
        self.assertNotIn("Pixel", self._s1().activity_path().read_text(encoding="utf-8"))
        self.assertEqual(
            [e["event"] for e in _plan_events(self._s2())],
            ["plan.stage.entered", "plan.stage.accepted", "plan.completed"],
        )

    def test_escalate_pause(self):
        result = self._start(_StageAdapter(self.repo), _SparringAdapter([ESCALATE]))
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(
            [(e["event"], e.get("action")) for e in _plan_events(self._s1())],
            [("plan.stage.entered", None), ("plan.paused", "ESCALATE")],
        )

    def test_provider_failure_is_failed_not_paused_and_carries_no_provider_text(self):
        with self.assertRaises(PlanRunError):
            self._start(_StageAdapter(self.repo, fail_at=1), _SparringAdapter([READY]))

        events = _plan_events(self._s1())
        self.assertEqual(
            [(e["event"], e.get("summary")) for e in events],
            [("plan.stage.entered", "Stage 1/2"), ("plan.failed", "stage loop failed")],
        )
        self.assertNotIn("plan.paused", _names(self._s1()))
        self.assertNotIn("boom", self._s1().activity_path().read_text(encoding="utf-8"))
        self.assertIs(self._plan_state().status, PlanRunStatus.PAUSED)

    def test_acceptance_refusal_is_failed_with_a_fixed_phrase(self):
        # A committed but unpushed candidate: the sparrer says READY, the
        # gate refuses.
        with self.assertRaises(PlanRunError) as ctx:
            self._start(_StageAdapter(self.repo, commit=True, push=False), _SparringAdapter([READY]))

        self.assertIn("acceptance gate refused", str(ctx.exception))
        self.assertEqual(
            [(e["event"], e.get("summary")) for e in _plan_events(self._s1())],
            [("plan.stage.entered", "Stage 1/2"), ("plan.failed", "acceptance gate refused")],
        )
        # The refusal's own detail (remote names, paths) stays out of the log.
        self.assertNotIn(str(self.remote), self._s1().activity_path().read_text(encoding="utf-8"))
        self.assertIs(self._s1().read_state().status, StageStatus.WORKING)

    def test_changed_plan_before_acceptance_is_failed(self):
        class _EditsPlan(_StageAdapter):
            def _turn(self, session_id):
                plan = self.repo / "docs" / "plan.md"
                plan.write_text(plan.read_text(encoding="utf-8").replace("Lay the groundwork", "Dig"),
                                encoding="utf-8")
                _run_git(self.repo, "commit", "-q", "-am", "edit plan")
                _run_git(self.repo, "push", "-q", "origin", "feature/x")
                return super()._turn(session_id)

        with self.assertRaises(PlanRunError):
            self._start(_EditsPlan(self.repo), _SparringAdapter([READY]))

        self.assertEqual(
            [(e["event"], e.get("summary")) for e in _plan_events(self._s1())],
            [("plan.stage.entered", "Stage 1/2"), ("plan.failed", "reviewed plan changed")],
        )


# -- non-authority -----------------------------------------------------------


class _Env:
    """A second, independent plan repository for reference runs."""

    def __init__(self, root: Path):
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True)
        self.repo = root / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        _run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
        self.plan_path = self.repo / "docs" / "plan.md"
        self.plan_path.parent.mkdir()
        self.plan_path.write_text(PLAN, encoding="utf-8")
        _run_git(self.repo, "add", ".")
        _run_git(self.repo, "commit", "-q", "-m", "base")
        _run_git(self.repo, "push", "-q", "-u", "origin", "main")
        _run_git(self.repo, "checkout", "-q", "-b", "feature/x")
        _run_git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        self.sparring_dir = self.repo / ".sparring"
        self.state_path = plan_state_path(self.sparring_dir, "docs/plan.md")

    def snapshot(self) -> dict[str, str]:
        snapshot = {"plan": self.state_path.read_text(encoding="utf-8")}
        for stage_id in (S1, S2):
            stage = Stage.resolve(self.sparring_dir, stage_id)
            snapshot[stage_id] = (
                (stage.directory / "state.json").read_text(encoding="utf-8")
                if stage.exists()
                else "<absent>"
            )
        return {key: _SHA_RE.sub("<sha>", value) for key, value in snapshot.items()}


class NonAuthorityTests(_PlanActivityCase):
    def _reference_run(self, scenario) -> tuple[object, dict[str, str]]:
        """Run ``scenario(env)`` against a pristine second repository with
        working telemetry and return (result, normalized snapshot)."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = _Env(Path(tmp.name))
        result = scenario(env)
        return result, env.snapshot()

    def test_directory_at_a_planned_stages_activity_path_changes_nothing(self):
        def scenario(env, *, sabotage: bool):
            stage_adapter = _StageAdapter(env.repo)
            sparring_adapter = _SparringAdapter([NEEDS_YOU, READY, READY])

            def make_adapters(stage: Stage):
                if sabotage and stage.stage_id == S2:
                    # Stage 2's log becomes a directory before any provider
                    # turn (plan.stage.entered has already created the file).
                    stage.activity_path().unlink()
                    stage.activity_path().mkdir()
                return stage_adapter, sparring_adapter

            start_plan(env.plan_path, env.sparring_dir, env.repo, make_adapters,
                       expected_branch="feature/x")
            if sabotage:
                # Stage 1's log becomes a directory between pause and resume.
                s1 = Stage.resolve(env.sparring_dir, S1)
                s1.activity_path().unlink()
                s1.activity_path().mkdir()
            result = resume_plan(env.plan_path, env.sparring_dir, env.repo, make_adapters,
                                 expected_branch="feature/x", evidence="checked on device")
            return result, stage_adapter, sparring_adapter

        reference, reference_snapshot = self._reference_run(
            lambda env: scenario(env, sabotage=False)
        )
        broken = scenario(self, sabotage=True)

        ref_result, ref_stage, ref_spar = reference
        result, stage_adapter, sparring_adapter = broken
        self.assertIs(ref_result.status, PlanRunStatus.COMPLETE)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([sid for sid, _ in result.accepted], [sid for sid, _ in ref_result.accepted])
        self.assertEqual(stage_adapter.resume_calls, ref_stage.resume_calls)
        self.assertEqual(len(stage_adapter.start_calls), len(ref_stage.start_calls))
        self.assertEqual(len(sparring_adapter.start_calls), len(ref_spar.start_calls))
        self.assertEqual(self._authoritative_snapshot(), reference_snapshot)
        self.assertEqual(self._s2().read_state().candidate_sha, _head_sha(self.repo))
        # Nothing was written under either shadowing directory.
        self.assertTrue(self._s1().activity_path().is_dir())
        self.assertTrue(self._s2().activity_path().is_dir())
        self.assertEqual(list(self._s1().activity_path().iterdir()), [])
        self.assertEqual(list(self._s2().activity_path().iterdir()), [])
        # The evidence still reached its authoritative home.
        self.assertIn("checked on device", self._s1().read_notes())

    def test_fabricated_completion_and_verdicts_in_the_log_are_ignored(self):
        stage_adapter = _StageAdapter(self.repo)
        self._start(stage_adapter, _SparringAdapter([NEEDS_YOU]))
        head = _head_sha(self.repo)

        fabricated = [
            {"actor": "sparrer", "event": "verdict", "action": "READY", "summary": "fabricated"},
            {"actor": "gate", "event": "candidate.frozen", "sha": head},
            {"actor": "gate", "event": "candidate.accepted", "sha": head},
            {"actor": "plan", "event": "plan.stage.accepted", "sha": head},
            {"actor": "plan", "event": "plan.stage.entered", "summary": "Stage 2/2"},
            {"actor": "plan", "event": "plan.completed", "summary": "2 stage(s) accepted"},
        ]
        with self._s1().activity_path().open("a", encoding="utf-8") as handle:
            for line in fabricated:
                handle.write(json.dumps({"v": 1, "ts": "2026-01-01T00:00:00.000Z", **line}) + "\n")
        before = self._authoritative_snapshot()

        result = self._resume(stage_adapter, _SparringAdapter([SEND_BACK, NEEDS_YOU]))

        # The real sparrer's SEND_BACK then NEEDS_YOU is what happened: the
        # same stage, not accepted, not advanced, plan not complete.
        self.assertIs(result.status, PlanRunStatus.PAUSED)
        self.assertEqual(result.stage_id, S1)
        self.assertIs(result.routing.action, RoutingAction.NEEDS_YOU)
        self.assertIs(self._s1().read_state().status, StageStatus.WORKING)
        self.assertIsNone(self._s1().read_state().candidate_sha)
        self.assertFalse(self._s2().exists())
        state = self._plan_state()
        self.assertIs(state.status, PlanRunStatus.PAUSED)
        self.assertEqual((state.current_stage_index, state.current_stage), (0, S1))
        self.assertEqual([sid for sid, _ in stage_adapter.resume_calls], ["impl-1", "impl-1"])
        # state.json / the run-state file changed only in ways the real run
        # explains (nothing, here: same stage, same sessions, still paused).
        self.assertEqual(self._authoritative_snapshot(), before)
        # The fabricated lines are still there, appended after, never read.
        events = _events(self._s1())
        self.assertEqual(events[-1]["event"], "plan.paused")
        self.assertIn("fabricated", [e.get("summary") for e in events])

    def test_deleting_activity_files_mid_run_changes_nothing(self):
        class _Deleting(_StageAdapter):
            def __init__(self, repo, sparring_dir):
                super().__init__(repo)
                self.sparring_dir = sparring_dir

            def _turn(self, session_id):
                for stage_id in (S1, S2):
                    path = Stage.resolve(self.sparring_dir, stage_id).activity_path()
                    if path.is_file():
                        path.unlink()
                return super()._turn(session_id)

        def scenario(env, *, delete: bool):
            stage_adapter = (
                _Deleting(env.repo, env.sparring_dir) if delete else _StageAdapter(env.repo)
            )
            sparring_adapter = _SparringAdapter([SEND_BACK, READY, SEND_BACK, READY])
            result = start_plan(
                env.plan_path, env.sparring_dir, env.repo, _fixed(stage_adapter, sparring_adapter),
                expected_branch="feature/x",
            )
            return result, stage_adapter

        (reference, ref_stage), reference_snapshot = self._reference_run(
            lambda env: scenario(env, delete=False)
        )
        result, stage_adapter = scenario(self, delete=True)

        self.assertIs(reference.status, PlanRunStatus.COMPLETE)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual([sid for sid, _ in result.accepted], [sid for sid, _ in reference.accepted])
        self.assertEqual(stage_adapter.resume_calls, ref_stage.resume_calls)
        self.assertEqual(len(stage_adapter.start_calls), len(ref_stage.start_calls))
        self.assertEqual(self._authoritative_snapshot(), reference_snapshot)
        # Telemetry simply resumed in a fresh file after each deletion:
        # stage 1's log (last deleted during stage 2's turns) is gone for
        # good, stage 2's holds only what came after its last turn.
        self.assertFalse(self._s1().activity_path().exists())
        self.assertNotIn("plan:plan.stage.entered", _names(self._s2()))
        self.assertEqual(_names(self._s2(), "plan")[-1], "plan:plan.completed")


if __name__ == "__main__":
    unittest.main()
