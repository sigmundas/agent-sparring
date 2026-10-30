"""Plan L Stage 2: local acceptance and history preservation.

Everything here runs against real, disposable Git repositories:

- I-L1 (no remote contact) is shown by a ``git`` wrapper placed first on
  ``PATH`` that logs every subcommand the engine runs -- never by mocking
  ``engine_git`` -- and, where a remote exists, by a bare remote whose refs
  are compared byte for byte and whose ``post-receive`` hook leaves a marker.
- I-L2 (acceptance is local): the candidate is the exact, clean ``HEAD`` of
  the expected branch, and accept needs the reviewer's READY at that SHA.
- I-L2b (accepted history only grows, per repository): legitimate advances
  are accepted, rewrites refuse with ``run_history_rewritten``.
- I-L6 / L-D3: the retired push flags refuse with typed codes, changing
  nothing.

Every negative test has a positive control in the same class: plain ``git``
doing what the engine refuses to, or the same scenario with the protection
disabled through ``mock.patch`` in the test only.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import acceptance
from agent_sparring import trusted_snapshots
from agent_sparring.acceptance import (
    CANDIDATE_NOT_ON_BRANCH,
    RUN_HISTORY_REWRITTEN,
    VERDICT_NOT_AT_CANDIDATE,
    AcceptanceError,
    accept_candidate,
    freeze_candidate,
    pin_sibling_candidates,
    record_review,
)
from agent_sparring.trusted_snapshots import TRUSTED_CONFIG_MISSING
from agent_sparring.capabilities import PUSH_NOT_PART_OF_RUNS, PUSH_PERMISSION_DEPRECATED
from agent_sparring.cli import main
from agent_sparring.next_turn import record_next_turn
from agent_sparring.plan import (
    PlanError,
    PlanRunError,
    PlanRunState,
    PlanRunStatus,
    load_plan_source,
    plan_key,
    resume_plan,
    run_state_path,
    start_plan,
)
from agent_sparring.providers import SparringAgentResult, StageAgentResult
from agent_sparring.push_gate import PUSH_AUTHORIZATION_REQUIRED, PushRequired
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring
from agent_sparring.stage import NEXT_TURN_FINALIZATION, Stage, StageStatus, TurnCandidate

from git_recorder import REAL_GIT, GitRecorder, git, make_bare, refs

PLAN = """\
# Local plan

## Stage 1 — First change

Append a line to work.txt.

## Stage 2 — Second change

Append another line to work.txt.
"""

READY = json.dumps(
    {"action": "READY", "summary": "looks good", "needs_you_reason": None,
     "findings": "none", "deferred": None, "human_gate": None}
)
RUN_KEY = plan_key("docs/plan.md")
S1 = f"{RUN_KEY}-stage-1-first-change"
S2 = f"{RUN_KEY}-stage-2-second-change"

FAKE_CLAUDE = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
d = os.environ["FAKE_DIR"]; git = os.environ["FAKE_GIT"]
p = os.path.join(d, "claude-calls"); n = int(open(p).read()) if os.path.exists(p) else 0
open(p, "w").write(str(n + 1))
def run(*a, cwd):
    subprocess.run([git, *a], cwd=cwd, check=True, capture_output=True)
def commit(where):
    with open(os.path.join(where, "work.txt"), "a") as f:
        f.write(f"change {n}\n")
    run("add", "work.txt", cwd=where); run("commit", "-q", "-m", f"fake change {n}", cwd=where)
commit(os.getcwd())
if os.environ.get("FAKE_SIBLING"):
    commit(os.environ["FAKE_SIBLING"])
sid = args[args.index("--resume") + 1] if "--resume" in args else f"impl-{n + 1}"
for line in ({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"},
             {"type": "result", "subtype": "success", "is_error": False, "result": "implemented",
              "session_id": sid, "num_turns": 1}):
    print(json.dumps(line), flush=True)
'''

FAKE_CODEX = r'''
import json, os, sys
args = sys.argv[1:]
tid = args[args.index("resume") + 1] if "resume" in args else "thread-1"
verdict = {"action": "READY", "summary": "fake", "needs_you_reason": None,
           "findings": "none", "deferred": None}
for line in ({"type": "thread.started", "thread_id": tid}, {"type": "turn.started"},
             {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": json.dumps(verdict)}},
             {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}):
    print(json.dumps(line), flush=True)
open(args[args.index("-o") + 1], "w").write(json.dumps(verdict))
'''


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def commit_file(repo: Path, name: str, text: str = "x\n") -> str:
    path = repo / name
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"change {name}")
    return head(repo)


def make_repo(path: Path, *, plan: str | None = PLAN, branch: str = "feature/x") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
    (path / "work.txt").write_text("start\n", encoding="utf-8")
    if plan is not None:
        (path / "docs").mkdir()
        (path / "docs" / "plan.md").write_text(plan, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    git(path, "switch", "-q", "-c", branch)
    return path


def tree_snapshot(repo: Path) -> str:
    """Everything a refusal must leave byte-identical: ``.sparring``, refs,
    HEAD and status."""

    digest = hashlib.sha256()
    sparring = repo / ".sparring"
    if sparring.exists():
        for path in sorted(sparring.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(repo)).encode())
                digest.update(path.read_bytes())
    for args in (("for-each-ref",), ("rev-parse", "HEAD"), ("status", "--porcelain", "--untracked-files=all")):
        digest.update(git(repo, *args).encode())
    return digest.hexdigest()


class _World(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = make_repo(self.root / "repo")
        self.sparring_dir = self.repo / ".sparring"
        self.plan_path = self.repo / "docs" / "plan.md"
        self.state_path = run_state_path(self.sparring_dir, RUN_KEY)
        self.recorder = GitRecorder(self.root)

    def stage(self, stage_id: str) -> Stage:
        return Stage.resolve(self.sparring_dir, stage_id)

    def status(self, stage_id: str) -> StageStatus | None:
        stage = self.stage(stage_id)
        return stage.read_state().status if stage.exists() else None


# -- end to end through the CLI, with fake provider executables --------------


class _CliWorld(_World):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.root / "fake"
        self.fake.mkdir()
        self.claude = self._script("fake-claude", FAKE_CLAUDE)
        self.codex = self._script("fake-codex", FAKE_CODEX)
        patcher = mock.patch.dict(
            os.environ, {"FAKE_DIR": str(self.fake), "FAKE_GIT": REAL_GIT}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _script(self, name: str, body: str) -> Path:
        script = self.root / f"{name}.py"
        script.write_text(body.lstrip("\n"), encoding="utf-8")
        wrapper = self.root / name
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--sparring-dir", str(self.sparring_dir), *args])
        return code, out.getvalue(), err.getvalue()

    def run_plan(self, *extra: str) -> tuple[int, str, str]:
        return self.cli(
            "run-plan", str(self.plan_path), "--run-key", RUN_KEY, "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--claude-executable", str(self.claude),
            "--codex-executable", str(self.codex), *extra,
        )


class LocalRunEndToEndTests(_CliWorld):
    """A managed run through the real CLI reaches acceptance locally."""

    def test_a_run_in_a_repository_with_no_remote_at_all_is_accepted(self):
        self.assertEqual(git(self.repo, "remote"), "")
        with self.recorder.active():
            code, out, err = self.run_plan()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.status(S1), self.status(S2)), (StageStatus.ACCEPTED, StageStatus.ACCEPTED))
        self.assertTrue(self.recorder.lines(), "the recorder saw the engine's git commands")
        self.assertEqual(self.recorder.remote_commands(), [])
        state = PlanRunState.load(self.state_path)
        self.assertIsNone(state.remote_pin)
        self.assertIsNone(state.push_authorization)
        self.assertEqual(
            [(entry.stage_id, entry.sha, entry.repository) for entry in state.accepted_candidates],
            [(S1, self.stage(S1).read_state().candidate_sha, None),
             (S2, self.stage(S2).read_state().candidate_sha, None)],
        )
        self.assertEqual(state.status, PlanRunStatus.COMPLETE)

    def test_a_run_with_a_real_bare_origin_leaves_it_byte_identical(self):
        marker = self.root / "MARKER-origin"
        bare = make_bare(self.root / "origin.git", marker)
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "push", "-q", "-u", "origin", "main", "feature/x")
        marker.unlink()
        before = refs(bare)
        with self.recorder.active():
            code, _out, err = self.run_plan()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.status(S1), self.status(S2)), (StageStatus.ACCEPTED, StageStatus.ACCEPTED))
        self.assertEqual(self.recorder.remote_commands(), [])
        self.assertEqual(refs(bare), before)
        self.assertFalse(marker.exists())
        # The accepted candidate is local only: the remote never saw it.
        self.assertNotIn(head(self.repo), refs(bare))

    def test_positive_control_the_recorder_and_the_marker_see_a_plain_push(self):
        marker = self.root / "MARKER-origin"
        bare = make_bare(self.root / "origin.git", marker)
        git(self.repo, "remote", "add", "origin", str(bare))
        before = refs(bare)
        with self.recorder.active():
            subprocess.run(["git", "push", "-q", "origin", "feature/x"], cwd=self.repo, check=True,
                           capture_output=True)
        self.assertTrue(self.recorder.remote_commands())
        self.assertTrue(marker.exists())
        self.assertNotEqual(refs(bare), before)

    def test_a_run_with_a_sibling_repository_is_accepted_and_records_both_histories(self):
        sibling = make_repo(self.root / "sibling", plan=None)
        manifest = {
            "version": 1, "plan_label": "docs/plan.md", "source_digest": "sha256:local",
            "stages": [
                {"stage_id": f"local-s{i}", "label": f"S{i}", "title": f"Change {i}",
                 "brief": f"Append a line to work.txt in both repositories (stage {i}).",
                 "mode": "implementation",
                 "repositories": [{"name": "sib", "path": str(sibling), "branch": "feature/x",
                                   "candidate_sha": None}]}
                for i in (1, 2)
            ],
        }
        manifest_path = self.repo / "local.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        git(self.repo, "add", manifest_path.name)
        git(self.repo, "commit", "-q", "-m", "manifest")
        with self.recorder.active(), mock.patch.dict(os.environ, {"FAKE_SIBLING": str(sibling)}):
            code, _out, err = self.cli(
                "run-plan", "--manifest", str(manifest_path), "--run-key", "sib-run",
                "--repo-root", str(self.repo), "--expected-branch", "feature/x",
                "--claude-executable", str(self.claude), "--codex-executable", str(self.codex),
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.recorder.remote_commands(), [])
        s2 = self.stage("local-s2").read_state()
        self.assertIs(s2.status, StageStatus.ACCEPTED)
        self.assertEqual(s2.repositories[0].candidate_sha, head(sibling))
        state = PlanRunState.load(run_state_path(self.sparring_dir, "sib-run"))
        self.assertEqual((state.remote_pin, state.sibling_pins), (None, ()))
        self.assertEqual(
            [(e.stage_id, e.repository) for e in state.accepted_candidates],
            [("local-s1", None), ("local-s1", "sib"), ("local-s2", None), ("local-s2", "sib")],
        )


class HandDrivenStageTests(_CliWorld):
    """A hand-driven stage -- run-loop, then freeze-candidate and
    accept-candidate -- with fake providers, accepted locally."""

    def _hand_driven(self) -> tuple[int, str]:
        stage = self.stage("hand-1").create(brief="Append a line to work.txt.\n")
        self.assertTrue(stage.exists())
        common = ["--repo-root", str(self.repo), "--expected-branch", "feature/x"]
        code, _out, err = self.cli(
            "run-loop", "hand-1", *common, "--claude-executable", str(self.claude),
            "--codex-executable", str(self.codex),
        )
        if code != 0:
            return code, err
        code, _out, err = self.cli("freeze-candidate", "hand-1", *common)
        if code != 0:
            return code, err
        code, _out, err = self.cli("accept-candidate", "hand-1", *common)
        return code, err

    def test_a_hand_driven_stage_with_no_remote_is_accepted(self):
        with self.recorder.active():
            code, err = self._hand_driven()
        self.assertEqual(code, 0, err)
        self.assertIs(self.status("hand-1"), StageStatus.ACCEPTED)
        self.assertEqual(self.stage("hand-1").read_state().candidate_sha, head(self.repo))
        self.assertEqual(self.recorder.remote_commands(), [])

    def test_a_hand_driven_stage_leaves_a_real_bare_origin_byte_identical(self):
        marker = self.root / "MARKER-hand"
        bare = make_bare(self.root / "origin.git", marker)
        git(self.repo, "remote", "add", "origin", str(bare))
        git(self.repo, "push", "-q", "-u", "origin", "feature/x")
        marker.unlink()
        before = refs(bare)
        with self.recorder.active():
            code, err = self._hand_driven()
        self.assertEqual(code, 0, err)
        self.assertIs(self.status("hand-1"), StageStatus.ACCEPTED)
        self.assertEqual(self.recorder.remote_commands(), [])
        self.assertEqual(refs(bare), before)
        self.assertFalse(marker.exists())


# -- per-repository history (I-L2b), through the plan runner ----------------


class _Scripted:
    """A stage adapter that runs one scripted action per turn."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def _turn(self, session_id: str) -> StageAgentResult:
        action = self.actions[self.calls] if self.calls < len(self.actions) else None
        self.calls += 1
        if action is not None:
            action()
        return StageAgentResult(session_id=session_id, text="claims", is_error=False)

    def start(self, prompt: str) -> StageAgentResult:
        return self._turn(f"impl-{self.calls + 1}")

    def resume(self, session_id: str, prompt: str) -> StageAgentResult:
        return self._turn(session_id)


class _AlwaysReady:
    def __init__(self):
        self.calls = 0

    def start(self, prompt: str) -> SparringAgentResult:
        self.calls += 1
        return SparringAgentResult(session_id=f"spar-{self.calls}", text=READY, is_error=False)

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.calls += 1
        return SparringAgentResult(session_id=session_id, text=READY, is_error=False)


class _HistoryWorld(_World):
    def setUp(self) -> None:
        super().setUp()
        self.sibling = make_repo(self.root / "sibling", plan=None)
        self.base = head(self.repo)
        self.sibling_base = head(self.sibling)

    def run_primary(self, first, second):
        return self._run(load_plan_source(self.plan_path, self.repo), [first, second], RUN_KEY)

    def run_with_sibling(self, first, second):
        manifest = {
            "version": 1, "plan_label": "docs/plan.md", "source_digest": "sha256:local",
            "stages": [
                {"stage_id": f"sib-s{i}", "label": f"S{i}", "title": f"Change {i}",
                 "brief": f"Change both repositories (stage {i}).", "mode": "implementation",
                 "repositories": [{"name": "sib", "path": str(self.sibling), "branch": "feature/x",
                                   "candidate_sha": None}]}
                for i in (1, 2)
            ],
        }
        path = self.repo / "sib.manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        git(self.repo, "add", path.name)
        git(self.repo, "commit", "-q", "-m", "manifest")
        return self._run(load_plan_source(path, self.repo, manifest=True), [first, second], "sib-run")

    def _run(self, source, actions, key):
        with self.recorder.active():
            try:
                result = start_plan(
                    source, self.sparring_dir, self.repo,
                    lambda stage: (_Scripted(actions[:1] if stage.stage_id.endswith(("1-first-change", "s1"))
                                             else actions[1:]), _AlwaysReady()),
                    expected_branch="feature/x", run_key=key,
                )
            except PlanRunError as exc:
                return exc
        self.assertEqual(self.recorder.remote_commands(), [])
        return result

    def accepted(self, *stage_ids: str) -> list[str]:
        return [s for s in stage_ids if self.status(s) is StageStatus.ACCEPTED]


def _reset_to(repo: Path, rev: str):
    return lambda: git(repo, "reset", "-q", "--hard", rev)


def _then(*steps):
    def run():
        for step in steps:
            step()
    return run


def _commit(repo: Path, name: str = "work.txt"):
    return lambda: commit_file(repo, name)


def _reword_with_interactive_rebase(repo: Path, onto: str):
    """``git rebase -i`` that rewords every commit after ``onto`` -- a
    rewrite of the accepted commit that keeps its content."""

    def run():
        env = dict(os.environ, GIT_SEQUENCE_EDITOR="sed -i.bak -e 's/^pick/reword/'",
                   GIT_EDITOR="sed -i.bak -e '1s/^/reworded: /'")
        subprocess.run([REAL_GIT, "rebase", "-q", "-i", onto], cwd=repo, env=env, check=True,
                       capture_output=True)
    return run


class PrimaryHistoryTests(_HistoryWorld):
    def test_a_new_commit_on_top_of_the_accepted_candidate_is_accepted(self):
        result = self.run_primary(_commit(self.repo), _commit(self.repo))
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.accepted(S1, S2), [S1, S2])

    def test_several_commits_are_accepted(self):
        result = self.run_primary(
            _commit(self.repo), _then(_commit(self.repo, "a.txt"), _commit(self.repo, "b.txt"),
                                      _commit(self.repo, "c.txt"))
        )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.accepted(S1, S2), [S1, S2])

    def test_a_merge_commit_that_contains_the_accepted_candidate_is_accepted(self):
        def merge():
            git(self.repo, "switch", "-q", "-c", "side")
            commit_file(self.repo, "side.txt")
            git(self.repo, "switch", "-q", "feature/x")
            commit_file(self.repo, "main-line.txt")
            git(self.repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")

        result = self.run_primary(_commit(self.repo), merge)
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.accepted(S1, S2), [S1, S2])
        self.assertEqual(len(git(self.repo, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)

    def test_a_reset_before_the_accepted_candidate_refuses_and_accepts_nothing(self):
        result = self.run_primary(
            _commit(self.repo), _then(_reset_to(self.repo, "HEAD~1"), _commit(self.repo, "other.txt"))
        )
        self.assertIsInstance(result, PlanRunError)
        self.assertIn(RUN_HISTORY_REWRITTEN, str(result))
        self.assertIn("the primary repository", str(result))
        self.assertIn(self.stage(S1).read_state().candidate_sha, str(result))
        self.assertEqual(self.accepted(S1, S2), [S1])
        # Nothing was frozen either.
        self.assertIs(self.status(S2), StageStatus.WORKING)
        self.assertIsNone(self.stage(S2).read_state().candidate_sha)

    def test_positive_control_the_same_reset_with_the_check_disabled_is_accepted(self):
        with mock.patch.object(acceptance, "_require_history"):
            result = self.run_primary(
                _commit(self.repo), _then(_reset_to(self.repo, "HEAD~1"), _commit(self.repo, "other.txt"))
            )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.accepted(S1, S2), [S1, S2])

    def test_an_interactive_rebase_that_rewrites_the_accepted_commit_refuses(self):
        result = self.run_primary(
            _commit(self.repo),
            _then(_reword_with_interactive_rebase(self.repo, self.base), _commit(self.repo, "other.txt")),
        )
        self.assertIsInstance(result, PlanRunError)
        self.assertIn(RUN_HISTORY_REWRITTEN, str(result))
        self.assertIn("the primary repository", str(result))
        self.assertEqual(self.accepted(S1, S2), [S1])

    def test_positive_control_the_rebase_really_rewrote_the_commit(self):
        accepted = commit_file(self.repo, "work.txt")
        _reword_with_interactive_rebase(self.repo, self.base)()
        self.assertNotEqual(head(self.repo), accepted)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^{tree}"), git(self.repo, "rev-parse", f"{accepted}^{{tree}}"))
        self.assertNotEqual(
            subprocess.run([REAL_GIT, "merge-base", "--is-ancestor", accepted, "HEAD"], cwd=self.repo).returncode, 0
        )


class SiblingHistoryTests(_HistoryWorld):
    def test_both_repositories_advancing_is_accepted(self):
        result = self.run_with_sibling(
            _then(_commit(self.repo), _commit(self.sibling)), _then(_commit(self.repo), _commit(self.sibling))
        )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        self.assertEqual(self.accepted("sib-s1", "sib-s2"), ["sib-s1", "sib-s2"])

    def test_the_sibling_advancing_while_the_primary_does_not_is_accepted(self):
        result = self.run_with_sibling(
            _then(_commit(self.repo), _commit(self.sibling)), _commit(self.sibling)
        )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        s1, s2 = (self.stage(s).read_state() for s in ("sib-s1", "sib-s2"))
        self.assertEqual(s1.candidate_sha, s2.candidate_sha)
        self.assertNotEqual(s1.repositories[0].candidate_sha, s2.repositories[0].candidate_sha)

    def test_the_primary_advancing_while_the_sibling_does_not_is_accepted(self):
        result = self.run_with_sibling(
            _then(_commit(self.repo), _commit(self.sibling)), _commit(self.repo)
        )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        s1, s2 = (self.stage(s).read_state() for s in ("sib-s1", "sib-s2"))
        self.assertEqual(s1.repositories[0].candidate_sha, s2.repositories[0].candidate_sha)

    def test_the_sibling_base_is_recorded_when_the_stage_is_entered(self):
        self.run_with_sibling(_then(_commit(self.repo), _commit(self.sibling)), _commit(self.repo))
        s1, s2 = (self.stage(s).read_state() for s in ("sib-s1", "sib-s2"))
        self.assertEqual(s1.repositories[0].base_sha, self.sibling_base)
        self.assertEqual(s2.repositories[0].base_sha, s1.repositories[0].candidate_sha)

    def test_a_sibling_reset_before_its_accepted_candidate_refuses(self):
        result = self.run_with_sibling(
            _then(_commit(self.repo), _commit(self.sibling)),
            _then(_commit(self.repo), _reset_to(self.sibling, "HEAD~1"), _commit(self.sibling, "other.txt")),
        )
        self.assertIsInstance(result, PlanRunError)
        self.assertIn(RUN_HISTORY_REWRITTEN, str(result))
        self.assertIn("sibling repository 'sib'", str(result))
        self.assertIn(str(self.sibling), str(result))
        self.assertEqual(self.accepted("sib-s1", "sib-s2"), ["sib-s1"])
        self.assertIsNone(self.stage("sib-s2").read_state().candidate_sha)

    def test_positive_control_the_same_sibling_reset_with_the_check_disabled_is_accepted(self):
        with mock.patch.object(acceptance, "_require_history"):
            result = self.run_with_sibling(
                _then(_commit(self.repo), _commit(self.sibling)),
                _then(_commit(self.repo), _reset_to(self.sibling, "HEAD~1"), _commit(self.sibling, "other.txt")),
            )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)

    def test_a_sibling_interactive_rebase_that_rewrites_its_accepted_commit_refuses(self):
        result = self.run_with_sibling(
            _then(_commit(self.repo), _commit(self.sibling)),
            _then(_commit(self.repo), _reword_with_interactive_rebase(self.sibling, self.sibling_base),
                  _commit(self.sibling, "other.txt")),
        )
        self.assertIsInstance(result, PlanRunError)
        self.assertIn(RUN_HISTORY_REWRITTEN, str(result))
        self.assertIn("sibling repository 'sib'", str(result))
        self.assertEqual(self.accepted("sib-s1", "sib-s2"), ["sib-s1"])


# -- the local rules (I-L2), hand-driven ------------------------------------


class LocalRuleTests(_World):
    def setUp(self) -> None:
        super().setUp()
        self.s = self.stage("rules-1").create()
        self.candidate = commit_file(self.repo, "work.txt")

    def ready_at(self, sha: str) -> None:
        record_next_turn(
            self.s, NEXT_TURN_FINALIZATION,
            candidate=TurnCandidate(head_sha=sha, kind="commit", content_digest="sha256:test"),
        )

    def freeze(self):
        return freeze_candidate(self.s, self.sparring_dir, self.repo, expected_branch="feature/x")

    def accept(self):
        return accept_candidate(self.s, self.repo, expected_branch="feature/x")

    def assertCode(self, ctx, code):
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(code, str(ctx.exception))

    def test_positive_control_a_clean_head_on_its_branch_with_ready_at_it_is_accepted(self):
        self.ready_at(self.candidate)
        with self.recorder.active():
            self.freeze()
            result = self.accept()
        self.assertEqual(result.candidate_sha, self.candidate)
        self.assertEqual(self.recorder.remote_commands(), [])

    def test_a_candidate_on_another_branch_refuses(self):
        git(self.repo, "switch", "-q", "-c", "elsewhere")
        with self.assertRaises(AcceptanceError) as ctx:
            self.freeze()
        self.assertCode(ctx, CANDIDATE_NOT_ON_BRANCH)
        self.assertIsNone(self.s.read_state().candidate_sha)

    def test_a_detached_head_refuses(self):
        git(self.repo, "switch", "-q", "--detach", "HEAD")
        with self.assertRaises(AcceptanceError) as ctx:
            self.freeze()
        self.assertCode(ctx, CANDIDATE_NOT_ON_BRANCH)

    def test_a_dirty_tree_refuses(self):
        (self.repo / "work.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(AcceptanceError) as ctx:
            self.freeze()
        self.assertCode(ctx, CANDIDATE_NOT_ON_BRANCH)
        self.assertIsNone(self.s.read_state().candidate_sha)

    def test_a_dirty_tree_at_accept_refuses(self):
        self.ready_at(self.candidate)
        self.freeze()
        (self.repo / "work.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, CANDIDATE_NOT_ON_BRANCH)
        self.assertIs(self.s.read_state().status, StageStatus.FROZEN)

    def test_a_frozen_candidate_that_is_no_longer_head_refuses(self):
        self.ready_at(self.candidate)
        self.freeze()
        commit_file(self.repo, "later.txt")
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, CANDIDATE_NOT_ON_BRANCH)

    def test_a_verdict_recorded_at_another_sha_refuses_accept(self):
        self.ready_at(self.base_of_candidate())
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)
        self.assertIs(self.s.read_state().status, StageStatus.FROZEN)

    def _escalated_over(self, sha: str) -> None:
        record_next_turn(
            self.s, "sparring",
            candidate=TurnCandidate(head_sha=sha, kind="commit", content_digest="sha256:test"),
        )
        record_sparring(self.s, RoutingResult(action=RoutingAction.ESCALATE, summary="spar elsewhere"))

    def test_positive_control_a_person_accepts_after_escalate_at_the_reviewed_sha(self):
        self._escalated_over(self.candidate)
        self.freeze()
        self.assertEqual(self.accept().candidate_sha, self.candidate)

    def test_accepting_after_escalate_over_another_sha_refuses(self):
        self._escalated_over(self.base_of_candidate())
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)

    def test_no_recorded_review_refuses_accept(self):
        # No review of the candidate is recorded at all: accept refuses, and
        # nothing is accepted. Positive controls: the two tests below.
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)
        self.assertIn("no review of it is recorded", str(ctx.exception))
        self.assertIs(self.s.read_state().status, StageStatus.FROZEN)

    def test_positive_control_a_ready_recorded_with_the_verdict_is_accepted(self):
        record_review(self.s, self.repo, RoutingAction.READY)
        self.freeze()
        self.assertEqual(self.accept().candidate_sha, self.candidate)

    def test_a_ready_recorded_over_an_earlier_commit_refuses(self):
        record_review(self.s, self.repo, RoutingAction.READY)
        commit_file(self.repo, "later.txt")
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)

    def test_a_ready_recorded_over_uncommitted_work_refuses(self):
        # The reviewer saw more than the commit: the same HEAD with
        # uncommitted work. That is not a review of the commit alone.
        (self.repo / "work.txt").write_text("reviewed but never committed\n", encoding="utf-8")
        record_review(self.s, self.repo, RoutingAction.READY)
        git(self.repo, "checkout", "--", "work.txt")
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)
        self.assertIn("uncommitted work", str(ctx.exception))

    def _record_by_hand(self, action: str, repo_root: Path) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main([
                "--sparring-dir", str(self.sparring_dir), "record-sparring", "rules-1",
                "--action", action, "--summary", "by hand", "--repo-root", str(repo_root),
            ])

    def test_a_later_verdict_whose_review_cannot_be_captured_leaves_no_ready_usable(self):
        # A READY at the candidate, recorded both ways the engine records one
        # (review record and loop marker); then a later ESCALATE recorded with
        # a root it cannot read. The ESCALATE is written, its review cannot be
        # captured -- and neither the old record nor the old marker may still
        # authorize the candidate.
        self.ready_at(self.candidate)
        record_review(self.s, self.repo, RoutingAction.READY)
        self.assertEqual(self._record_by_hand("ESCALATE", self.root / "no-such-repository"), 0)
        review = self.s.read_state().review
        self.assertEqual((review.action, review.head_sha), ("ESCALATE", None))
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)
        self.assertIn("could not be recorded", str(ctx.exception))
        self.assertIs(self.s.read_state().status, StageStatus.FROZEN)

    def test_positive_control_without_the_invalidation_the_old_ready_would_accept(self):
        # The same steps with the replacement's review recording disabled in
        # this test only: the earlier READY survives and accepts -- so the
        # refusal above is the invalidation.
        self.ready_at(self.candidate)
        record_review(self.s, self.repo, RoutingAction.READY)
        with mock.patch("agent_sparring.cli.record_review"):
            self.assertEqual(self._record_by_hand("ESCALATE", self.root / "no-such-repository"), 0)
        self.freeze()
        self.assertEqual(self.accept().candidate_sha, self.candidate)

    def test_a_later_verdict_recorded_properly_replaces_the_ready(self):
        # A later SEND_BACK, captured normally at the same commit, replaces
        # the READY: the candidate is no longer one the reviewer approved.
        record_review(self.s, self.repo, RoutingAction.READY)
        self.assertEqual(self._record_by_hand("SEND_BACK", self.repo), 0)
        self.assertEqual(self.s.read_state().review.action, "SEND_BACK")
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)

    def test_a_recorded_send_back_refuses_accept(self):
        record_review(self.s, self.repo, RoutingAction.SEND_BACK)
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)
        self.assertIn("not a READY", str(ctx.exception))

    def test_a_last_step_that_is_not_ready_refuses_accept(self):
        record_next_turn(self.s, "stage")
        self.freeze()
        with self.assertRaises(AcceptanceError) as ctx:
            self.accept()
        self.assertCode(ctx, VERDICT_NOT_AT_CANDIDATE)

    def test_a_stage_base_that_is_not_an_ancestor_refuses_with_run_history_rewritten(self):
        state = self.s.read_state()
        state.base_sha = self.candidate
        self.s.write_state(state)
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        commit_file(self.repo, "other.txt")
        with self.assertRaises(AcceptanceError) as ctx:
            self.freeze()
        self.assertCode(ctx, RUN_HISTORY_REWRITTEN)
        self.assertIn(self.candidate, str(ctx.exception))

    def base_of_candidate(self) -> str:
        return git(self.repo, "rev-parse", f"{self.candidate}~1")


# -- trusted configuration snapshots, per repository (I-L4) -----------------


def _no_guard(*_args, **_kwargs):
    return contextlib.nullcontext()


class TrustedSnapshotTests(_HistoryWorld):
    """Every participating repository's Git is held to the snapshot the run
    trusted. A snapshot that should be there and is not refuses
    (``trusted_config_missing``) rather than letting the operation run
    unguarded. Watched local configuration changed after the run trusted it
    is refused by name (``repository_config_changed``)."""

    def _paused_before_freeze(self, *, sibling: bool) -> Stage:
        """The reviewer said READY over a commit (in both repositories when
        ``sibling``) and the run stopped before the acceptance gate."""

        with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                if sibling:
                    self.run_with_sibling(_then(_commit(self.repo), _commit(self.sibling)), None)
                else:
                    self.run_primary(_commit(self.repo), None)
        self.key = "sib-run" if sibling else RUN_KEY
        self.run_state = run_state_path(self.sparring_dir, self.key)
        return self.stage("sib-s1" if sibling else S1)

    def _rewrite(self, change) -> None:
        payload = json.loads(self.run_state.read_text(encoding="utf-8"))
        change(payload)
        self.run_state.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _freeze(self, stage: Stage):
        return freeze_candidate(stage, self.sparring_dir, self.repo, expected_branch="feature/x")

    def _assert_refused(self, stage: Stage, code: str) -> str:
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze(stage)
        self.assertIn(code, str(ctx.exception))
        self.assertIs(stage.read_state().status, StageStatus.WORKING)
        return str(ctx.exception)

    # -- the primary ----------------------------------------------------

    def test_a_changed_primary_configuration_refuses_freeze_by_name(self):
        stage = self._paused_before_freeze(sibling=False)
        git(self.repo, "config", "core.sshCommand", "/bin/false")
        message = self._assert_refused(stage, "repository_config_changed")
        self.assertIn("core.sshcommand", message.lower())

    def test_positive_control_without_the_guard_the_primary_change_is_followed(self):
        stage = self._paused_before_freeze(sibling=False)
        git(self.repo, "config", "core.sshCommand", "/bin/false")
        with mock.patch.object(trusted_snapshots, "guard", _no_guard):
            self._freeze(stage)
        self.assertIs(stage.read_state().status, StageStatus.FROZEN)

    def test_removing_the_primary_snapshot_refuses_instead_of_running_unguarded(self):
        stage = self._paused_before_freeze(sibling=False)
        git(self.repo, "config", "core.sshCommand", "/bin/false")
        self._rewrite(lambda p: p.pop("config_snapshot"))
        message = self._assert_refused(stage, TRUSTED_CONFIG_MISSING)
        self.assertIn("not taken again", message)

    def test_a_malformed_primary_snapshot_refuses(self):
        stage = self._paused_before_freeze(sibling=False)
        self._rewrite(lambda p: p.__setitem__("config_snapshot", "damaged"))
        self._assert_refused(stage, TRUSTED_CONFIG_MISSING)

    def test_resume_refuses_a_removed_primary_snapshot_and_runs_nothing(self):
        self._paused_before_freeze(sibling=False)
        self._rewrite(lambda p: p.pop("config_snapshot"))
        before = self.run_state.read_bytes()
        adapter = _Scripted([])
        with self.assertRaises(PlanError) as ctx:
            resume_plan(self.plan_path, self.sparring_dir, self.repo, lambda stage: (adapter, _AlwaysReady()),
                        expected_branch="feature/x", run_key=self.key)
        self.assertIn(TRUSTED_CONFIG_MISSING, str(ctx.exception))
        self.assertEqual(self.run_state.read_bytes(), before)
        self.assertEqual(adapter.calls, 0)

    # -- a sibling ------------------------------------------------------

    def test_a_run_snapshots_each_sibling_at_start(self):
        self._paused_before_freeze(sibling=True)
        state = PlanRunState.load(self.run_state)
        self.assertEqual([entry.path for entry in state.sibling_snapshots], [str(self.sibling.resolve())])

    def test_a_changed_sibling_configuration_refuses_freeze_by_name(self):
        stage = self._paused_before_freeze(sibling=True)
        git(self.sibling, "config", "core.sshCommand", "/bin/false")
        message = self._assert_refused(stage, "repository_config_changed")
        self.assertIn("core.sshcommand", message.lower())

    def test_positive_control_without_the_guard_the_sibling_change_is_followed(self):
        stage = self._paused_before_freeze(sibling=True)
        git(self.sibling, "config", "core.sshCommand", "/bin/false")
        with mock.patch.object(trusted_snapshots, "guard", _no_guard):
            self._freeze(stage)
        self.assertIs(stage.read_state().status, StageStatus.FROZEN)

    def test_removing_the_sibling_snapshot_refuses_instead_of_running_unguarded(self):
        stage = self._paused_before_freeze(sibling=True)
        git(self.sibling, "config", "core.sshCommand", "/bin/false")
        self._rewrite(lambda p: p.pop("sibling_snapshots"))
        message = self._assert_refused(stage, TRUSTED_CONFIG_MISSING)
        self.assertIn("sibling repository 'sib'", message)

    def test_review_pinning_of_siblings_is_held_to_the_sibling_snapshot(self):
        # The path an independent review takes into the sibling set
        # (enter_review -> pin_sibling_candidates), standalone.
        stage = self._paused_before_freeze(sibling=True)
        repositories = stage.read_state().repositories
        self.assertEqual(
            pin_sibling_candidates(self.repo, repositories, stage=stage)[0].candidate_sha, head(self.sibling)
        )
        git(self.sibling, "config", "core.sshCommand", "/bin/false")
        with self.assertRaises(AcceptanceError) as ctx:
            pin_sibling_candidates(self.repo, repositories, stage=stage)
        self.assertIn("repository_config_changed", str(ctx.exception))

    def test_resume_refuses_a_removed_sibling_snapshot_and_runs_nothing(self):
        self._paused_before_freeze(sibling=True)
        self._rewrite(lambda p: p.pop("sibling_snapshots"))
        before = self.run_state.read_bytes()
        adapter = _Scripted([])
        with self.assertRaises(PlanError) as ctx:
            resume_plan(load_plan_source(self.repo / "sib.manifest.json", self.repo, manifest=True),
                        self.sparring_dir, self.repo, lambda stage: (adapter, _AlwaysReady()),
                        expected_branch="feature/x", run_key=self.key)
        self.assertIn(TRUSTED_CONFIG_MISSING, str(ctx.exception))
        self.assertEqual(self.run_state.read_bytes(), before)
        self.assertEqual(adapter.calls, 0)

    def test_a_sibling_created_during_the_run_is_snapshotted_when_first_declared(self):
        # Stage 1 creates the sibling repository; stage 2 is the first to
        # declare it, and its snapshot is taken then, before any turn there.
        late = self.root / "late"
        manifest = {
            "version": 1, "plan_label": "docs/plan.md", "source_digest": "sha256:local",
            "stages": [
                {"stage_id": "late-s1", "label": "S1", "title": "Create", "brief": "Create it.",
                 "mode": "implementation"},
                {"stage_id": "late-s2", "label": "S2", "title": "Use", "brief": "Use it.",
                 "mode": "implementation",
                 "repositories": [{"name": "late", "path": str(late), "branch": "feature/x",
                                   "candidate_sha": None}]},
            ],
        }
        path = self.repo / "late.manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        git(self.repo, "add", path.name)
        git(self.repo, "commit", "-q", "-m", "manifest")
        reports: list[str] = []
        actions = {
            "late-s1": [_then(_commit(self.repo), lambda: make_repo(late, plan=None))],
            "late-s2": [_then(_commit(self.repo), _commit(late))],
        }
        result = start_plan(
            load_plan_source(path, self.repo, manifest=True), self.sparring_dir, self.repo,
            lambda stage: (_Scripted(actions[stage.stage_id]), _AlwaysReady()),
            expected_branch="feature/x", run_key="late-run", report=reports.append,
        )
        self.assertEqual(result.status, PlanRunStatus.COMPLETE)
        state = PlanRunState.load(run_state_path(self.sparring_dir, "late-run"))
        self.assertEqual([entry.path for entry in state.sibling_snapshots], [str(late.resolve())])
        self.assertTrue(any("first time this run works in it" in line for line in reports), reports)


# -- retired flags (I-L6, L-D3) ---------------------------------------------


class RetiredPushFlagTests(_CliWorld):
    def setUp(self) -> None:
        super().setUp()
        # Any provider start is a failure: a tripwire.
        self.claude.write_text(f'#!/bin/sh\ntouch "{self.root}/TRIPWIRE"\nexit 3\n', encoding="utf-8")
        self.codex.write_text(f'#!/bin/sh\ntouch "{self.root}/TRIPWIRE"\nexit 3\n', encoding="utf-8")

    def test_run_plan_allow_push_for_run_refuses_and_creates_nothing(self):
        before = tree_snapshot(self.repo)
        code, _out, err = self.run_plan("--allow-push-for-run")
        self.assertNotEqual(code, 0)
        self.assertIn(PUSH_NOT_PART_OF_RUNS, err)
        self.assertIn("Push to review", err)
        self.assertEqual(tree_snapshot(self.repo), before)
        self.assertFalse(self.sparring_dir.exists())
        self.assertFalse((self.root / "TRIPWIRE").exists())

    def test_start_plan_allow_push_for_run_refuses_and_creates_nothing(self):
        before = tree_snapshot(self.repo)
        code, out, _err = self.cli(
            "start-plan", str(self.plan_path), "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--allow-push-for-run", "--json",
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "refused")
        self.assertIn(PUSH_NOT_PART_OF_RUNS, payload["error"])
        self.assertEqual(tree_snapshot(self.repo), before)
        self.assertFalse((self.root / "TRIPWIRE").exists())

    def test_the_api_refuses_allow_push_for_run_before_recording_anything(self):
        with self.assertRaises(PlanError) as ctx:
            start_plan(self.plan_path, self.sparring_dir, self.repo, lambda stage: (None, None),
                       expected_branch="feature/x", run_key=RUN_KEY, allow_push_for_run=True)
        self.assertIn(PUSH_NOT_PART_OF_RUNS, str(ctx.exception))
        self.assertFalse(self.state_path.exists())

    def _legacy_push_pause(self) -> str:
        """A run an engine from before local-first runs paused for push
        permission: the reviewer said READY over a commit, and the run state
        records the push pause (with the legacy fields it carried)."""

        candidate = commit_file(self.repo, "work.txt")
        stage_adapter, sparring = _Scripted([]), _AlwaysReady()
        with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                start_plan(self.plan_path, self.sparring_dir, self.repo,
                           lambda stage: (stage_adapter, sparring),
                           expected_branch="feature/x", run_key=RUN_KEY)
        state = PlanRunState.load(self.state_path)
        state.status = PlanRunStatus.PAUSED
        state.awaiting = PushRequired(
            kind=PUSH_AUTHORIZATION_REQUIRED, stage_id=S1, candidate_sha=candidate,
            branch="feature/x", remote="origin", remote_branch="feature/x",
        )
        state.save(self.state_path)
        return candidate

    def test_resume_allow_push_candidate_refuses_and_leaves_the_run_byte_identical(self):
        candidate = self._legacy_push_pause()
        before = tree_snapshot(self.repo)
        code, _out, err = self.cli(
            "resume-plan", str(self.plan_path), "--run-key", RUN_KEY, "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--allow-push-candidate", candidate,
            "--claude-executable", str(self.claude), "--codex-executable", str(self.codex),
        )
        self.assertNotEqual(code, 0)
        self.assertIn(PUSH_PERMISSION_DEPRECATED, err)
        self.assertIn("local-first", err)
        self.assertIn("plain resume", err)
        self.assertIn(f"sparring resume-plan --run-key {RUN_KEY}", err)
        self.assertEqual(tree_snapshot(self.repo), before)
        self.assertIs(self.status(S1), StageStatus.WORKING)
        self.assertFalse((self.root / "TRIPWIRE").exists())

    def test_resume_allow_push_for_run_refuses_and_leaves_the_run_byte_identical(self):
        self._legacy_push_pause()
        before = tree_snapshot(self.repo)
        code, _out, err = self.cli(
            "resume-plan", str(self.plan_path), "--run-key", RUN_KEY, "--repo-root", str(self.repo),
            "--expected-branch", "feature/x", "--allow-push-for-run",
        )
        self.assertNotEqual(code, 0)
        self.assertIn(PUSH_NOT_PART_OF_RUNS, err)
        self.assertEqual(tree_snapshot(self.repo), before)

    def test_positive_control_a_plain_resume_gets_past_the_flags_and_converts_the_legacy_pause(self):
        """Without the retired flag the same resume is not refused as
        deprecated: it reaches the run. Plan L Stage 4 (L-D1): the pause
        meets every legacy migration condition, so the plain resume converts
        it -- the stage is accepted locally at exactly the reviewed commit,
        with no provider turn and nothing pushed (was: kept paused; the
        conversion is covered in full by test_legacy_migration)."""

        candidate = self._legacy_push_pause()
        with self.recorder.active():
            code, out, err = self.cli(
                "resume-plan", str(self.plan_path), "--run-key", RUN_KEY, "--repo-root", str(self.repo),
                "--expected-branch", "feature/x", "--stop-after-stage", S1,
                "--claude-executable", str(self.claude), "--codex-executable", str(self.codex),
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn(PUSH_PERMISSION_DEPRECATED, err)
        self.assertIn("legacy_push_pause_converted", out + err)
        # No command offering the retired permission is printed.
        self.assertNotIn(f"--allow-push-candidate {candidate}", out)
        self.assertIs(self.status(S1), StageStatus.ACCEPTED)
        self.assertEqual(head(self.repo), candidate)
        self.assertEqual(self.recorder.remote_commands(), [])
        self.assertFalse((self.root / "TRIPWIRE").exists())

    def test_the_api_refuses_allow_push_candidate_before_reading_anything(self):
        with self.assertRaises(PlanError) as ctx:
            resume_plan(self.plan_path, self.sparring_dir, self.repo, lambda stage: (None, None),
                        expected_branch="feature/x", allow_push_candidate="0" * 40)
        self.assertIn(PUSH_PERMISSION_DEPRECATED, str(ctx.exception))


# -- capabilities -----------------------------------------------------------


class CapabilityTests(_CliWorld):
    def test_capabilities_json_lists_local_first(self):
        code, out, _err = self.cli("capabilities", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("local_first", payload["capabilities"])

    def test_runs_json_adds_the_capability_and_keeps_schema_version_1(self):
        code, out, err = self.cli("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["capabilities"], ["local_first"])
        self.assertIn("runs", payload)

    def test_finish_run_json_adds_the_capability_and_keeps_schema_version_1(self):
        code, out, _err = self.cli(
            "finish-run", "--run-key", "no-such-run", "--repo-root", str(self.repo), "--dry-run", "--json"
        )
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["capabilities"], ["local_first"])
        self.assertIn(code, (0, 3))

    def test_handoff_json_adds_the_capability(self):
        self.stage("cap-1").create()
        code, out, err = self.cli(
            "handoff", "cap-1", "--claims", "nothing", "--repo-root", str(self.repo),
            "--no-check-pushed", "--json",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["capabilities"], ["local_first"])
        self.assertEqual(payload["stage_id"], "cap-1")


if __name__ == "__main__":
    unittest.main()
