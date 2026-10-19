"""No engine remote operation follows live configuration (issue #5, Plan R
Stage 1).

Every repository here is a temporary one with disposable local bare
remotes. Planted programs are shell scripts that touch a marker file, so
"never ran" is the marker's absence, and each planted-configuration test
ends with a positive control: plain ``git`` -- outside the engine, in the
same checkout -- does run the planted program (or does reach the second
bare repository), so the marker mechanism is proven to work.

The planted items, each in the repository's own configuration and,
separately, in ``config.worktree``:

- ``core.sshCommand``;
- ``credential.helper=!<marker>``;
- ``remote.origin.uploadpack=<marker>``, with ``origin`` a local-path remote,
  so the program really runs if anything consults it;
- ``url.<second>.insteadOf=<origin's URL>``, a second bare repository;
- ``remote.origin.pushurl=<second>``.

and the surfaces, with no guard active:

- ``sparring runs --json`` and ``finish-run --dry-run`` of a pinned managed
  run (the run's pin and snapshot: a typed refusal in the check's detail);
- ``sparring handoff`` of a stage no run owns (a typed refusal) and of a
  stage a pinned run owns (the pin's typed refusal);
- freeze of a stage declaring a sibling repository, unpinned (a typed
  refusal) and pinned at run start (the sibling's own pin refuses), and
  acceptance of a stage frozen before the planting (it never reads the
  sibling's remote).

A read leaves no trace in a bare repository, so "the second bare
repository receives no contact" is shown by its refs being unchanged, by
the refusal happening before any transport, and by the positive control
that plain ``git`` reading ``origin`` does see the second repository's
marker ref.
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import engine_git, managed_finish
from agent_sparring.acceptance import AcceptanceError, accept_candidate, freeze_candidate
from agent_sparring.cli import main
from agent_sparring.engine_git import GitConfigRefusal
from agent_sparring.git_context import diff_patch, dirty_paths
from agent_sparring.push_gate import authorization_for_run, ensure_candidate_pushed
from agent_sparring.remote_pin import pin_remote, pin_sibling
from agent_sparring.stage import CandidateRepository, Stage, StageStatus

from test_managed_finish import _FinishTestCase
from test_multi_repo_candidates import _commit, _make_repo
from test_plan import S1, _PlanRepoTestCase, _head_sha, _run_git

ITEMS = ("ssh_command", "credential_helper", "uploadpack", "insteadof", "pushurl")
SCOPES = ("local", "worktree")
SECOND_REF = "refs/heads/second-was-read"
CREDENTIAL_REQUEST = "protocol=https\nhost=example.invalid\n\n"

#: Variables plain ``git`` must not inherit from the developer's shell for a
#: positive control to be about the planted configuration only.
_PLAIN_STRIPPED = (
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)


def _plain(cwd: Path, *args: str, input: str | None = None, env: dict | None = None):
    """The test's own ``git``, outside the engine."""

    base = {k: v for k, v in os.environ.items() if k not in _PLAIN_STRIPPED}
    base["GIT_TERMINAL_PROMPT"] = "0"
    base.update(env or {})
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, input=input,
        env=base, check=False, timeout=120,
    )


def _plain_ok(cwd: Path, *args: str) -> str:
    result = _plain(cwd, *args)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _refs(repo: Path) -> str:
    return _plain_ok(repo, "for-each-ref", "--format=%(refname) %(objectname)")


class _Planting:
    """Marker scripts, a second bare repository, and planting by scope."""

    marker: Path
    scratch: Path

    def script(self, name: str, body: str | None = None) -> Path:
        path = self.scratch / name
        path.write_text(body or f"#!/bin/sh\ntouch '{self.marker}'\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def second(self, checkout: Path) -> Path:
        """A second bare repository holding a ref plain ``git`` can see."""

        path = self.scratch / "second.git"
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
        _plain_ok(checkout, "push", "-q", str(path), f"HEAD:{SECOND_REF}")
        return path

    def plant(self, checkout: Path, item: str, scope: str, second: Path) -> str:
        origin = _plain_ok(checkout, "config", "--get", "remote.origin.url")
        key, value = {
            "ssh_command": ("core.sshCommand", str(self.script("ssh"))),
            "credential_helper": ("credential.helper", f"!{self.script('helper')}"),
            "uploadpack": ("remote.origin.uploadpack", str(self.script("upload-pack"))),
            "insteadof": (f"url.{second}.insteadOf", origin),
            "pushurl": ("remote.origin.pushurl", str(second)),
        }[item]
        if scope == "worktree":
            _plain_ok(checkout, "config", "extensions.worktreeConfig", "true")
            _plain_ok(checkout, "config", "--worktree", key, value)
            listed = _plain_ok(checkout, "config", "--list", "--show-scope")
            self.assertTrue(
                any(line.startswith("worktree\t") and value in line for line in listed.splitlines()),
                f"{key} was not planted in worktree scope:\n{listed}",
            )
        else:
            _plain_ok(checkout, "config", key, value)
        return key

    def assertNoMarker(self):
        self.assertFalse(self.marker.exists(), "a planted program ran")

    def positive_control(self, checkout: Path, item: str, second: Path) -> None:
        """Plain ``git`` in ``checkout`` does what the planted item says."""

        self.assertFalse(self.marker.exists())
        if item == "ssh_command":
            _plain(checkout, "ls-remote", "ssh://example.invalid/x.git")
            self.assertTrue(self.marker.exists(), "plain git did not run core.sshCommand")
        elif item == "credential_helper":
            _plain(checkout, "credential", "fill", input=CREDENTIAL_REQUEST)
            self.assertTrue(self.marker.exists(), "plain git did not run the credential helper")
        elif item == "uploadpack":
            _plain(checkout, "ls-remote", "origin")
            self.assertTrue(self.marker.exists(), "plain git did not run remote.origin.uploadpack")
        elif item == "insteadof":
            listed = _plain(checkout, "ls-remote", "origin").stdout
            self.assertIn(SECOND_REF, listed, "plain git did not follow the insteadOf rewrite")
        elif item == "pushurl":
            _plain(checkout, "push", "-q", "origin", "HEAD:refs/heads/pushurl-control")
            self.assertIn("refs/heads/pushurl-control", _refs(second))


def _matrix(cls, run):
    """One test method per planted item and scope."""

    for item in ITEMS:
        for scope in SCOPES:
            def test(self, item=item, scope=scope):
                run(self, item, scope)

            test.__name__ = f"test_{item}_in_{scope}_config"
            setattr(cls, test.__name__, test)
    return cls


# -- sparring runs --json and finish-run --dry-run ------------------------------


class _StatusMatrix(_Planting, _FinishTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = self.repo.parent
        self.marker = self.scratch / "MARKER"

    def _candidate_check(self, finish: dict) -> dict:
        return next(c for c in finish["checks"] if c["code"] == "candidate_not_pushed")

    def _run_status_surfaces(self, item: str, scope: str) -> None:
        record, _ = self._complete()
        worktree = Path(record.worktree_path)
        second = self.second(worktree)
        before = _refs(second)
        key = self.plant(worktree, item, scope, second)

        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")
        self.assertEqual(code, 0, err)
        (run,) = json.loads(out)["runs"]
        check = self._candidate_check(run["finish"])
        self.assertFalse(check["ok"])
        self.assertIn("remote state not checked", check["detail"])
        self.assertIn("repository_config_changed", check["detail"])
        self.assertIn(key.lower().rsplit(".", 1)[-1], check["detail"].lower())
        self.assertFalse(run["git"]["candidate_pushed"])

        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--dry-run", "--json"
        )
        self.assertEqual(code, 3, err)
        dry = json.loads(out)
        self.assertEqual(dry, run["finish"])
        self.assertIn("repository_config_changed", self._candidate_check(dry)["detail"])

        self.assertNoMarker()
        self.assertEqual(_refs(second), before)
        self.positive_control(worktree, item, second)


StatusPlantedConfigurationTests = _matrix(
    type("StatusPlantedConfigurationTests", (_StatusMatrix,), {}),
    _StatusMatrix._run_status_surfaces,
)


class StatusWithoutPinTests(_Planting, _FinishTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = self.repo.parent
        self.marker = self.scratch / "MARKER"

    def test_a_run_with_no_pin_reports_the_remote_unchecked_and_never_contacts_it(self):
        record, _ = self._complete()
        worktree = Path(record.worktree_path)
        state_path = record.sparring_dir / "plans" / f"{record.run_key}.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload.pop("remote_pin")
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        second = self.second(worktree)
        self.plant(worktree, "uploadpack", "local", second)

        status = managed_finish.finish_status(self.repo, record.run_key)
        code, out, err = self._main("runs", "--repo-root", str(self.repo), "--json")

        self.assertEqual(code, 0, err)
        (run,) = json.loads(out)["runs"]
        for finish in (status["finish"], run["finish"]):
            check = next(c for c in finish["checks"] if c["code"] == "candidate_not_pushed")
            self.assertEqual(
                (check["ok"], check["detail"]), (False, managed_finish.REMOTE_NOT_CHECKED_NO_PIN)
            )
        self.assertEqual(managed_finish.REMOTE_NOT_CHECKED_NO_PIN, "remote state not checked: run has no pin")
        self.assertNoMarker()
        self.positive_control(worktree, "uploadpack", second)


# -- sparring handoff -----------------------------------------------------------


class _HandoffMatrix(_Planting, _PlanRepoTestCase):
    RUN = "plan-0123abcd"

    def setUp(self):
        super().setUp()
        self.scratch = Path(self._tmp.name)
        self.marker = self.scratch / "MARKER"

    def _handoff(self, stage_id: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.sparring_dir), "handoff", stage_id,
                "--claims", "done", "--repo-root", str(self.repo),
            ])
        return code, err.getvalue()

    def _run_handoff(self, item: str, scope: str) -> None:
        # A stage no run owns, and one a pinned run owns (pinned before the
        # planting, as a run start would).
        Stage.resolve(self.sparring_dir, "unowned").create()
        Stage.resolve(self.sparring_dir, "owned").create(run=self.RUN)
        pin = pin_remote(self.repo, "feature/x")
        plans = self.sparring_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{self.RUN}.json").write_text(
            json.dumps({"remote_pin": pin.to_dict()}), encoding="utf-8"
        )
        self.assertEqual(self._handoff("unowned")[0], 0)  # unplanted: the read works
        second = self.second(self.repo)
        before = _refs(second)
        key = self.plant(self.repo, item, scope, second)

        code, err = self._handoff("unowned")
        self.assertEqual(code, 1, err)
        self.assertIn("repository_transport_config", err)
        self.assertIn(key.lower().rsplit(".", 1)[-1], err.lower())

        code, err = self._handoff("owned")
        self.assertEqual(code, 1, err)
        self.assertRegex(err, "repository_config_changed|remote_retargeted")

        self.assertNoMarker()
        self.assertEqual(_refs(second), before)
        self.positive_control(self.repo, item, second)


HandoffPlantedConfigurationTests = _matrix(
    type("HandoffPlantedConfigurationTests", (_HandoffMatrix,), {}),
    _HandoffMatrix._run_handoff,
)


# -- freeze/accept with a declared sibling --------------------------------------


class _SiblingMatrix(_Planting, unittest.TestCase):
    PRIMARY_BRANCH = "feature/desktop"
    SIBLING_BRANCH = "feature/cloud"
    RUN = "plan-4567cdef"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scratch = Path(self._tmp.name)
        self.marker = self.scratch / "MARKER"
        self.primary = _make_repo(self.scratch, "primary", self.PRIMARY_BRANCH)
        self.sibling = _make_repo(self.scratch, "sibling", self.SIBLING_BRANCH)
        self.sparring_dir = self.primary / ".sparring"
        _commit(self.primary, "desktop.py", self.PRIMARY_BRANCH)
        self.sibling_sha = _commit(self.sibling, "migration.sql", self.SIBLING_BRANCH)

    def _stage(self, stage_id: str, *, run: str | None = None) -> Stage:
        stage = Stage.resolve(self.sparring_dir, stage_id).create(run=run)
        state = stage.read_state()
        state.repositories = (
            CandidateRepository(name="sibling", path="../sibling", branch=self.SIBLING_BRANCH),
        )
        stage.write_state(state)
        return stage

    def _freeze(self, stage: Stage):
        return freeze_candidate(stage, self.sparring_dir, self.primary, expected_branch=self.PRIMARY_BRANCH)

    def _run_sibling(self, item: str, scope: str) -> None:
        # Frozen before the planting: its acceptance must not need the
        # sibling's remote at all.
        frozen = self._stage("frozen-before")
        self.assertEqual(self._freeze(frozen).repositories[0].candidate_sha, self.sibling_sha)
        # A stage of a run that pinned the sibling at run start.
        entry = pin_sibling(self.sibling, self.SIBLING_BRANCH)
        plans = self.sparring_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{self.RUN}.json").write_text(
            json.dumps({"sibling_pins": [entry.to_dict()]}), encoding="utf-8"
        )
        owned = self._stage("owned", run=self.RUN)
        unowned = self._stage("unowned")
        second = self.second(self.sibling)
        before = _refs(second)
        key = self.plant(self.sibling, item, scope, second)

        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze(unowned)
        self.assertIn("repository_transport_config", str(ctx.exception))
        self.assertIn(key.lower().rsplit(".", 1)[-1], str(ctx.exception).lower())
        self.assertIs(unowned.read_state().status, StageStatus.WORKING)

        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze(owned)
        self.assertRegex(str(ctx.exception), "repository_config_changed|remote_retargeted")
        self.assertIs(owned.read_state().status, StageStatus.WORKING)

        accepted = accept_candidate(frozen, self.primary, expected_branch=self.PRIMARY_BRANCH)
        self.assertEqual(accepted.repositories[0].candidate_sha, self.sibling_sha)
        self.assertIs(frozen.read_state().status, StageStatus.ACCEPTED)

        self.assertNoMarker()
        self.assertEqual(_refs(second), before)
        self.positive_control(self.sibling, item, second)


SiblingPlantedConfigurationTests = _matrix(
    type("SiblingPlantedConfigurationTests", (_SiblingMatrix,), {}),
    _SiblingMatrix._run_sibling,
)


class SiblingPinTests(_SiblingMatrix):
    def test_a_pinned_sibling_is_read_through_its_own_pin(self):
        entry = pin_sibling(self.sibling, self.SIBLING_BRANCH)
        self.assertEqual(entry.path, str(self.sibling.resolve()))
        self.assertEqual(entry.pin.branch, self.SIBLING_BRANCH)
        # The sibling's snapshot is its own configuration, not the primary's.
        _plain_ok(self.sibling, "config", "credential.helper", "store --file=/nonexistent")
        entry = pin_sibling(self.sibling, self.SIBLING_BRANCH)
        self.assertIn(("credential.helper", "store --file=/nonexistent"), entry.pin.trusted_config)
        plans = self.sparring_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"{self.RUN}.json").write_text(
            json.dumps({"sibling_pins": [entry.to_dict()]}), encoding="utf-8"
        )
        # Trusted at run start, so the pinned read uses it rather than
        # refusing it (the unpinned read would refuse).
        owned = self._stage("owned", run=self.RUN)
        frozen = self._freeze(owned)
        self.assertEqual(frozen.repositories[0].candidate_sha, self.sibling_sha)
        unowned = self._stage("unowned")
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze(unowned)
        self.assertIn("repository_transport_config", str(ctx.exception))

    def test_a_plan_run_pins_its_declared_siblings_at_start(self):
        from agent_sparring.plan import PlanRunState, PlanRunStatus, _pin_siblings
        from agent_sparring.plan_model import PlannedStage

        planned = PlannedStage(
            position=1, label="1", title="one", stage_id="s1", brief="b",
            repositories=(
                CandidateRepository(name="sibling", path="../sibling", branch=self.SIBLING_BRANCH),
                CandidateRepository(name="missing", path="../missing", branch="x"),
            ),
        )
        notes: list[str] = []
        pins = _pin_siblings(self.primary, (planned, planned), report=notes.append)
        self.assertEqual([entry.path for entry in pins], [str(self.sibling.resolve())])
        self.assertTrue(any("'missing'" in note and "not pinned" in note for note in notes))
        state = PlanRunState(plan="p", plan_digest="d", expected_branch=self.PRIMARY_BRANCH,
                             current_stage_index=0, current_stage="s1",
                             status=PlanRunStatus.RUNNING, sibling_pins=pins)
        path = self.scratch / "state.json"
        state.save(path)
        self.assertEqual(PlanRunState.load(path).sibling_pins, pins)


# -- engine_git itself ----------------------------------------------------------


class _EngineCase(_Planting, _PlanRepoTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(self._tmp.name)
        self.marker = self.scratch / "MARKER"

    def commit(self, name: str, body: str = "work\n") -> str:
        (self.repo / name).write_text(body, encoding="utf-8")
        _run_git(self.repo, "add", name)
        _run_git(self.repo, "commit", "-q", "-m", name)
        return _head_sha(self.repo)

    def authorized_push(self, pin):
        authorization = authorization_for_run(self.repo, branch="feature/x", pin=pin)
        with engine_git.trusted_config([self.repo], pin.trusted_config, destinations=(pin.destination,)):
            return ensure_candidate_pushed(
                self.repo, stage_id=S1, expected_branch="feature/x",
                authorization=authorization, pin=pin,
            )


class UnguardedRemoteTests(_EngineCase):
    def test_a_remote_command_with_no_guard_is_a_typed_refusal_naming_it(self):
        second = self.second(self.repo)
        self.plant(self.repo, "uploadpack", "local", second)
        for args in (
            ("ls-remote", "origin"),
            ("fetch", "origin"),
            ("-c", "push.followTags=false", "push", "origin", "HEAD:refs/heads/elsewhere"),
            ("pull", "origin"),
        ):
            with self.subTest(args=args):
                with self.assertRaises(GitConfigRefusal) as ctx:
                    engine_git.run(self.repo, *args)
                self.assertEqual(ctx.exception.code, "unguarded_remote_operation")
                self.assertEqual(ctx.exception.key, engine_git.subcommand(args)[1])
        for sub in (("update",), ("prune", "origin"), ("show", "origin"), ("add", "-f", "x", str(second))):
            with self.subTest(remote=sub), self.assertRaises(GitConfigRefusal) as ctx:
                engine_git.run(self.repo, "remote", *sub)
            self.assertEqual(ctx.exception.code, "unguarded_remote_operation")
        self.assertEqual(engine_git.run(self.repo, "remote", "get-url", "origin").returncode, 0)
        self.assertNoMarker()
        self.assertNotIn("elsewhere", _refs(self.remote))
        self.positive_control(self.repo, "uploadpack", second)

    def test_a_guarded_remote_command_runs_against_the_snapshot(self):
        pin = pin_remote(self.repo, "feature/x")
        with engine_git.trusted_config([self.repo], pin.trusted_config, destinations=(pin.destination,)):
            result = engine_git.run(self.repo, "ls-remote", pin.destination, "refs/heads/feature/x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(_head_sha(self.repo), result.stdout)

    def test_an_unpinned_local_path_is_never_pushed_to(self):
        pin = pin_remote(self.repo, "feature/x")
        sha = self.commit("one.txt")
        other = self.scratch / "other.git"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True, capture_output=True)
        hook = other / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(self.script("receive").read_text(encoding="utf-8"), encoding="utf-8")
        hook.chmod(0o755)

        for pinned in (None, str(self.remote)):
            with self.subTest(pinned=pinned), self.assertRaises(GitConfigRefusal) as ctx:
                engine_git.run_remote(
                    self.repo, pin.trusted_config, "push", str(other), f"{sha}:refs/heads/x",
                    pinned=pinned,
                )
            self.assertEqual(ctx.exception.code, "unpinned_local_remote")
        with engine_git.trusted_config([self.repo], pin.trusted_config, destinations=(pin.destination,)):
            with self.assertRaises(GitConfigRefusal) as ctx:
                engine_git.run(self.repo, "push", f"file://{other}", f"{sha}:refs/heads/x")
            self.assertEqual(ctx.exception.code, "unpinned_local_remote")
            pushed = engine_git.run(self.repo, "push", pin.destination, f"{sha}:refs/heads/feature/x")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(_refs(other), "")
        self.assertNoMarker()
        # Positive control: a push to that repository runs its own hook.
        _plain(self.repo, "push", "-q", str(other), f"{sha}:refs/heads/x")
        self.assertTrue(self.marker.exists())

    def _hooked_bare(self, name: str) -> Path:
        """A disposable bare repository whose pre-receive hook touches the
        marker, holding one branch a ``--mirror`` push would delete."""

        path = self.scratch / name
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
        _plain_ok(self.repo, "push", "-q", str(path), "HEAD:refs/heads/keep")
        hook = path / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(self.script("receive").read_text(encoding="utf-8"), encoding="utf-8")
        hook.chmod(0o755)
        return path

    def test_push_forms_that_hide_the_destination_are_refused(self):
        # The repository Git writes must be the one argument checked: a form
        # whose first plain argument is not the destination (or that names
        # none) is refused, never parsed.
        pin = pin_remote(self.repo, "feature/x")
        sha = self.commit("one.txt")
        other = self._hooked_bare("other.git")
        before = _refs(other)
        pinned_before = _refs(self.remote)
        upload = self.script("upload-pack")
        forms = [
            ("push", f"--repo={other}", "--mirror"),
            ("push", "--repo", str(other), f"{sha}:refs/heads/x"),
            ("push", "--mirror", str(other)),
            ("push", "--all", str(other)),
            ("push", "-o", pin.destination, str(other), f"{sha}:refs/heads/x"),
            ("push", "--push-option", pin.destination, str(other), f"{sha}:refs/heads/x"),
            ("push", f"--receive-pack={upload}", pin.destination, f"{sha}:refs/heads/x"),
            ("push", "--exec", str(upload), pin.destination, f"{sha}:refs/heads/x"),
            ("push", "--", str(other), f"{sha}:refs/heads/x"),
            ("push",),
            ("-c", f"url.{other}.insteadOf={pin.destination}", "push", pin.destination,
             f"{sha}:refs/heads/x"),
            ("-c", f"remote.x.receivepack={upload}", "push", pin.destination, f"{sha}:refs/heads/x"),
            ("--git-dir", str(other), "push", pin.destination, f"{sha}:refs/heads/x"),
            ("ls-remote", f"--upload-pack={upload}", pin.destination),
            ("ls-remote", "-u", str(upload), pin.destination),
            ("fetch", f"--upload-pack={upload}", pin.destination),
        ]
        for args in forms:
            with self.subTest(args=args):
                with self.assertRaises(GitConfigRefusal) as ctx:
                    engine_git.run_remote(
                        self.repo, pin.trusted_config, *args, pinned=pin.destination
                    )
                self.assertEqual(ctx.exception.code, "unsupported_remote_form")
                with engine_git.trusted_config(
                    [self.repo], pin.trusted_config, destinations=(pin.destination,)
                ), self.assertRaises(GitConfigRefusal) as ctx:
                    engine_git.run(self.repo, *args)
                self.assertEqual(ctx.exception.code, "unsupported_remote_form")
        self.assertNoMarker()
        self.assertEqual(_refs(other), before)
        self.assertEqual(_refs(self.remote), pinned_before)

        # The forms the engine does use still work.
        with engine_git.trusted_config([self.repo], pin.trusted_config, destinations=(pin.destination,)):
            listed = engine_git.run(self.repo, "ls-remote", "--exit-code", pin.destination, "refs/heads/feature/x")
            pushed = engine_git.run(
                self.repo, "-c", "push.followTags=false", "push", "--porcelain", pin.destination,
                f"{sha}:refs/heads/feature/x",
            )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertNoMarker()

        # Positive controls: plain git with these forms does reach the
        # unpinned repository (its hook runs, a mirror push deletes its
        # branch) and does run a named upload-pack.
        _plain(self.repo, "push", f"--repo={other}", "--mirror")
        self.assertTrue(self.marker.exists(), "plain git --repo= --mirror did not reach the repository")
        self.assertNotIn("refs/heads/keep", _refs(other))
        self.marker.unlink()
        _plain(self.repo, "push", "--repo", str(other), f"{sha}:refs/heads/x")
        self.assertTrue(self.marker.exists(), "plain git --repo <path> did not reach the repository")
        self.marker.unlink()
        _plain(self.repo, "ls-remote", f"--upload-pack={upload}", pin.destination)
        self.assertTrue(self.marker.exists(), "plain git did not run --upload-pack")


class IncludeTests(_EngineCase):
    def test_a_global_conditional_include_keeps_engine_git_working(self):
        helper = self.script(
            "include-helper", "#!/bin/sh\necho username=included\necho password=from-include\n"
        )
        harmless = self.scratch / "harmless.cfg"
        harmless.write_text(f"[core]\n\tabbrev = 12\n[credential]\n\thelper = !{helper}\n", encoding="utf-8")
        global_config = Path(os.environ["HOME"]) / ".gitconfig"
        with open(global_config, "a", encoding="utf-8") as handle:
            handle.write(f'[includeIf "gitdir:{self.repo.resolve()}/"]\n\tpath = {harmless}\n')
        self.assertIn("core.abbrev=12", _plain_ok(self.repo, "config", "--list"))  # plain git applies it

        entries = engine_git.config_entries(self.repo)
        self.assertIn(engine_git.ConfigEntry(scope="global", key="core.abbrev", value="12"), entries)
        pin = pin_remote(self.repo, "feature/x")
        base = _head_sha(self.repo)
        sha = self.commit("one.txt")
        (self.repo / "one.txt").write_text("dirty\n", encoding="utf-8")
        self.assertIn("one.txt", dirty_paths(self.repo))
        self.assertIn("one.txt", diff_patch(self.repo, base, sha))
        _run_git(self.repo, "checkout", "--", "one.txt")
        outcome = self.authorized_push(pin)
        self.assertTrue(outcome.pushed)
        self.assertEqual(_plain_ok(self.remote, "rev-parse", "refs/heads/feature/x"), sha)
        # A remote operation reads the include's expanded entries, resolved
        # for the real repository (not for its throwaway one).
        filled = engine_git.run_remote(
            self.repo, pin.trusted_config, "credential", "fill", input=CREDENTIAL_REQUEST
        )
        self.assertEqual(filled.returncode, 0, filled.stderr)
        self.assertIn("password=from-include", filled.stdout)

        # A repository-scope include is still refused.
        for key in ("include.path", "includeIf.gitdir:/.path"):
            with self.subTest(key=key):
                _run_git(self.repo, "config", key, str(harmless))
                try:
                    with self.assertRaises(GitConfigRefusal) as ctx:
                        dirty_paths(self.repo)
                    self.assertEqual(ctx.exception.code, "included_config")
                finally:
                    _run_git(self.repo, "config", "--unset", key)


class EnvironmentTests(_EngineCase):
    LISTED = (
        "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND",
        "GIT_EXEC_PATH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
        "GIT_TEMPLATE_DIR", "GIT_EDITOR", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    )

    def test_the_listed_variables_are_stripped_and_documented(self):
        with mock.patch.dict(os.environ, {name: "planted" for name in self.LISTED}):
            env = engine_git._environment({"GIT_CONFIG_GLOBAL": "engine-set"})
        for name in self.LISTED:
            if name != "GIT_CONFIG_GLOBAL":
                self.assertNotIn(name, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "engine-set")  # the engine's own setting stays
        doc = engine_git._environment.__doc__
        for name in self.LISTED:
            self.assertIn(name.rsplit("_", 1)[0] if name.endswith("_0") else name, doc)

    def test_an_authorized_push_leaves_no_marker_with_planted_environment(self):
        pin = pin_remote(self.repo, "feature/x")
        sha = self.commit("one.txt")
        planted = {
            "GIT_SSH_COMMAND": str(self.script("env-ssh")),
            "GIT_CONFIG_PARAMETERS": f"'credential.helper=!{self.script('env-helper')}'",
            "GIT_ASKPASS": str(self.script("env-askpass")),
            "GIT_TERMINAL_PROMPT": "0",
        }
        with mock.patch.dict(os.environ, planted):
            outcome = self.authorized_push(pin)
            engine_git.run_remote(self.repo, pin.trusted_config, "credential", "fill", input=CREDENTIAL_REQUEST)
        self.assertTrue(outcome.pushed)
        self.assertEqual(_plain_ok(self.remote, "rev-parse", "refs/heads/feature/x"), sha)
        self.assertNoMarker()

        # Positive controls: plain git with that environment runs them.
        _plain(self.repo, "credential", "fill", input=CREDENTIAL_REQUEST,
               env={"GIT_CONFIG_PARAMETERS": planted["GIT_CONFIG_PARAMETERS"]})
        self.assertTrue(self.marker.exists(), "GIT_CONFIG_PARAMETERS was not honoured by plain git")
        self.marker.unlink()
        _plain(self.repo, "ls-remote", "ssh://example.invalid/x.git",
               env={"GIT_SSH_COMMAND": planted["GIT_SSH_COMMAND"]})
        self.assertTrue(self.marker.exists(), "GIT_SSH_COMMAND was not honoured by plain git")


class CheckoutIsolationTests(_EngineCase):
    def test_a_checkout_performing_worktree_add_reads_the_validated_configuration(self):
        (self.repo / ".gitattributes").write_text("*.txt filter=late\n", encoding="utf-8")
        _run_git(self.repo, "add", ".gitattributes")
        self.commit("a.txt", "content\n")
        real = engine_git.config_entries
        planted = []

        def racing(cwd):
            # The check reads the configuration; a filter appears before
            # the command runs.
            entries = real(cwd)
            if not planted:
                planted.append(True)
                _run_git(self.repo, "config", "filter.late.smudge", str(self.script("smudge")))
                _run_git(self.repo, "config", "filter.late.required", "true")
            return entries

        target = self.scratch / "added"
        with mock.patch.object(engine_git, "config_entries", side_effect=racing):
            result = engine_git.run(self.repo, "worktree", "add", "-q", "-b", "added", str(target), "HEAD")

        self.assertEqual(planted, [True])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNoMarker()
        # The smudge stays planted for the positive control; "required"
        # would only make plain git's own status below fail for want of a
        # clean command.
        _run_git(self.repo, "config", "--unset", "filter.late.required")
        self.assertEqual((target / "a.txt").read_text(encoding="utf-8"), "content\n")
        self.assertIn(str(target.resolve()), _plain_ok(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(_plain_ok(target, "rev-parse", "HEAD"), _head_sha(self.repo))
        self.assertEqual(_plain_ok(target, "status", "--porcelain"), "")
        # Positive control: a live checkout runs the filter.
        _plain(self.repo, "worktree", "add", "-q", "-b", "control", str(self.scratch / "control"), "HEAD")
        self.assertTrue(self.marker.exists())

    def test_worktree_bookkeeping_without_a_checkout_stays_live(self):
        index, name = engine_git.subcommand(["worktree", "add", "--no-checkout", "x"])
        self.assertTrue(engine_git._live(name, ["worktree", "add", "--no-checkout", "x"], index))
        self.assertTrue(engine_git._live("worktree", ["worktree", "list"], 0))
        self.assertFalse(engine_git._live("worktree", ["worktree", "add", "x"], 0))
        self.assertFalse(engine_git._live("switch", ["switch", "x"], 0))
        self.assertFalse(engine_git._live("checkout", ["checkout", "x"], 0))


if __name__ == "__main__":
    unittest.main()
