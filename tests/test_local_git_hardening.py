"""Plan L Stage 5: the remaining local Git hardening (audit R-4, R-6 to R-8).

What these tests hold, one sentence each:

- **R-4, ownership.** In a managed worktree the owning run is the one the
  engine's managed record names; a ``.sparring/`` claim that contradicts it
  -- the stage's ``run`` cleared or rewritten, the run state's
  ``current_stage`` moved, the run state deleted -- refuses freeze and
  accept with ``stage_owner_mismatch``. The unmanipulated stage is accepted.
- **R-6, signing programs.** A local or worktree ``log.showSignature``,
  ``commit.gpgSign``, ``tag.gpgSign``, ``merge.verifySignatures`` or
  ``gpg.*program`` (and ``gpg.ssh.defaultKeyCommand``) makes plain Git run a
  marker program from ``log --format=%H``, ``reflog show``, ``commit``,
  ``commit-tree``, ``merge`` and ``tag``; the engine's same command does
  not. The user's own global signer still signs, the finish merge included.
- **R-7, submodules.** A populated submodule's own clean filter runs under
  plain ``git status`` and ``git diff`` in the superproject, and not under
  the engine's; an uncommitted gitlink change is still reported dirty.
- **R-8, listing failure.** A repository whose configuration cannot be
  listed refuses with ``repository_config_unreadable``, and the command is
  never run.

Real disposable repositories; plain ``git`` for every positive control.
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

from agent_sparring import acceptance, engine_git
from agent_sparring.cli import main
from agent_sparring.engine_git import GitConfigRefusal
from agent_sparring.git_context import dirty_paths
from agent_sparring.stage import Stage, StageStatus
from git_recorder import REAL_GIT, GitRecorder
from test_managed_end_to_end import _ManagedStageAdapter
from test_managed_finish import _FinishTestCase
from test_managed_run import _ManagedRepoTestCase, _git
from test_plan import READY, _SparringAdapter, _run_git


def _plain(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """The test's own ``git``, never the engine's."""

    result = subprocess.run([REAL_GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def _global_config(*lines: str) -> None:
    """Append to the user's own (per-test, see conftest) global configuration."""

    with open(Path(os.environ["HOME"]) / ".gitconfig", "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def signer(directory: Path, name: str) -> tuple[Path, Path]:
    """``(program, marker)``: a stand-in signing program that appends its
    arguments to ``marker`` and produces what Git expects of a signer --
    OpenPGP/X.509 (``SIG_CREATED`` on the status fd) or SSH (``-Y sign``
    writes ``<file>.sig``). A verification gets no valid answer."""

    program, marker = directory / name, directory / f"MARKER-{name}"
    program.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{marker}"\n'
        'for last in "$@"; do :; done\n'
        'case " $* " in\n'
        '  *" -Y sign "*) printf -- "-----BEGIN SSH SIGNATURE-----\\nZmFrZQ==\\n-----END SSH SIGNATURE-----\\n" '
        '> "$last.sig"; exit 0 ;;\n'
        '  *" -Y "*|*" --verify "*) cat >/dev/null; exit 1 ;;\n'
        "esac\n"
        "cat >/dev/null\n"
        'printf -- "-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n"\n'
        'printf "\\n[GNUPG:] SIG_CREATED D 1 8 00 0 X\\n" >&2\n',
        encoding="utf-8",
    )
    program.chmod(0o755)
    return program, marker


def _touch(path: Path) -> None:
    """Change ``path``'s stat information but not its size or content, so
    the next index refresh must hash it -- through any clean filter."""

    path.write_bytes(path.read_bytes())
    later = path.stat().st_mtime + 100 + _touch.calls
    _touch.calls += 1
    os.utime(path, (later, later))


_touch.calls = 0


def _ran(marker: Path) -> bool:
    ran = marker.exists()
    marker.unlink(missing_ok=True)
    return ran


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        _plain(self.root, "init", "-q", "-b", "main", str(self.repo), check=True)
        self.commit("base.txt")

    def commit(self, name: str, *flags: str, cwd: Path | None = None) -> str:
        where = cwd or self.repo
        (where / name).write_text(f"{name}\n", encoding="utf-8")
        _plain(where, "add", name, check=True)
        _plain(where, *flags, "commit", "-q", "-m", name, check=True)
        return _plain(where, "rev-parse", "HEAD", check=True).stdout.strip()

    def local(self, key: str, value: str, scope: str = "local") -> None:
        if scope == "worktree":
            _plain(self.repo, "config", "extensions.worktreeConfig", "true", check=True)
        _plain(self.repo, "config", f"--{scope}", key, value, check=True)


# -- R-6: signing and verification programs ------------------------------------


class SigningProgramTests(_Repo):
    def setUp(self):
        super().setUp()
        self.planted, self.marker = signer(self.root, "planted")
        other, other_marker = signer(self.root, "fixture")
        # A signed commit, signed by a program that is not the planted one.
        self.signed = self.commit("signed.txt", "-c", f"gpg.program={other}", "-c", "commit.gpgSign=true")
        self.assertTrue(_ran(other_marker))

    def _plant(self, scope: str, *pairs: tuple[str, str]) -> None:
        self.local("gpg.program", str(self.planted), scope)
        for key, value in pairs:
            self.local(key, value, scope)

    def test_log_and_reflog_run_no_verification_program(self):
        for scope in ("local", "worktree"):
            for args in (
                ("log", "--format=%H"),
                ("reflog", "show", "--format=%H%x00%gs", "refs/heads/main", "--"),
            ):
                with self.subTest(scope=scope, command=args[0]):
                    self._plant(scope, ("log.showSignature", "true"))
                    plain = _plain(self.repo, *args)
                    self.assertTrue(_ran(self.marker), "positive control: plain git verifies")
                    engine = engine_git.run(self.repo, *args)
                    self.assertEqual(engine.returncode, 0, engine.stderr)
                    self.assertFalse(_ran(self.marker), f"{scope} signing program ran under the engine")
                    self.assertIn(self.signed, engine.stdout)
                    self.assertEqual(plain.stdout, engine.stdout)
                    _plain(self.repo, "config", f"--{scope}", "--unset", "log.showSignature")

    def test_commit_signs_with_no_planted_program(self):
        for scope in ("local", "worktree"):
            with self.subTest(scope=scope, command="commit"):
                self._plant(scope, ("commit.gpgSign", "true"))
                (self.repo / f"plain-{scope}.txt").write_text("x\n", encoding="utf-8")
                _plain(self.repo, "add", f"plain-{scope}.txt", check=True)
                _plain(self.repo, "commit", "-q", "-m", "plain", check=True)
                self.assertTrue(_ran(self.marker), "positive control: plain git signs")
                (self.repo / f"engine-{scope}.txt").write_text("x\n", encoding="utf-8")
                _plain(self.repo, "add", f"engine-{scope}.txt", check=True)
                result = engine_git.run(self.repo, "commit", "-q", "--no-verify", "-m", "engine")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(_ran(self.marker))
                self.assertNotIn("gpgsig", _plain(self.repo, "cat-file", "commit", "HEAD").stdout)
            with self.subTest(scope=scope, command="commit-tree"):
                # Marker finding: commit-tree does not read commit.gpgSign,
                # so it runs no signing program under plain Git either --
                # only an explicit -S does.
                _plain(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "x", check=True)
                self.assertFalse(_ran(self.marker))
                _plain(self.repo, "commit-tree", "-S", "HEAD^{tree}", "-p", "HEAD", "-m", "x", check=True)
                self.assertTrue(_ran(self.marker), "the planted program is a working signer")
                made = engine_git.run(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "x")
                self.assertEqual(made.returncode, 0, made.stderr)
                self.assertFalse(_ran(self.marker))
                _plain(self.repo, "config", f"--{scope}", "--unset", "commit.gpgSign")

    def test_a_merge_commit_signs_with_no_planted_program(self):
        base = _plain(self.repo, "rev-parse", "HEAD", check=True).stdout.strip()
        _plain(self.repo, "branch", "side", base, check=True)
        self.commit("main-only.txt")
        tip = _plain(self.repo, "rev-parse", "HEAD", check=True).stdout.strip()
        _plain(self.repo, "switch", "-q", "side", check=True)
        self.commit("side.txt")
        _plain(self.repo, "switch", "-q", "main", check=True)
        self._plant("local", ("commit.gpgSign", "true"))

        _plain(self.repo, "merge", "--no-ff", "--no-edit", "side", check=True)
        self.assertTrue(_ran(self.marker), "positive control: plain git signs the merge")
        _plain(self.repo, "reset", "-q", "--hard", tip, check=True)

        merged = engine_git.run(self.repo, "merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", "side")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertFalse(_ran(self.marker))

    def test_merge_verifies_no_signature_with_a_planted_program(self):
        _plain(self.repo, "branch", "side", "HEAD~1", check=True)
        _plain(self.repo, "switch", "-q", "side", check=True)
        _plain(self.repo, "merge", "-q", "--ff-only", self.signed, check=True)
        _plain(self.repo, "switch", "-q", "-c", "behind", "HEAD~1", check=True)
        self._plant("local", ("merge.verifySignatures", "true"))

        refused = _plain(self.repo, "merge", "--ff-only", "side")
        self.assertTrue(_ran(self.marker), "positive control: plain git verifies")
        self.assertNotEqual(refused.returncode, 0)

        merged = engine_git.run(self.repo, "merge", "--ff-only", "--no-overwrite-ignore", "side")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.assertFalse(_ran(self.marker))

    def test_a_tag_signs_with_no_planted_program(self):
        self._plant("local", ("tag.gpgSign", "true"))
        _plain(self.repo, "tag", "-m", "plain", "plain", check=True)
        self.assertTrue(_ran(self.marker), "positive control: plain git signs the tag")
        made = engine_git.run(self.repo, "tag", "-m", "engine", "engine")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertFalse(_ran(self.marker))

    def test_the_users_global_signer_still_signs(self):
        # Each format's program key planted locally; the user's own global
        # signer of that format is what the engine runs.
        cases = (
            ("gpg.program", ()),
            ("gpg.openpgp.program", ()),
            ("gpg.x509.program", ("[gpg]", "\tformat = x509")),
            ("gpg.ssh.program", ("[gpg]", "\tformat = ssh", "[user]", "\tsigningKey = /nonexistent/key")),
        )
        for key, extra in cases:
            with self.subTest(key=key):
                home = Path(os.environ["HOME"]) / ".gitconfig"
                saved = home.read_text(encoding="utf-8")
                own, own_marker = signer(self.root, "global")
                section, _, variable = key.rpartition(".")
                head, _, sub = section.partition(".")
                header = f'[{head} "{sub}"]' if sub else f"[{head}]"
                _global_config("[commit]", "\tgpgSign = true", *extra, header, f"\t{variable} = {own}")
                self.local(key, str(self.planted))

                (self.repo / "p.txt").write_text(key, encoding="utf-8")
                _plain(self.repo, "add", "p.txt", check=True)
                _plain(self.repo, "commit", "-q", "-m", "plain", check=True)
                self.assertTrue(_ran(self.marker), "positive control: the planted program signs under plain git")
                self.assertFalse(_ran(own_marker))

                (self.repo / "e.txt").write_text(key, encoding="utf-8")
                _plain(self.repo, "add", "e.txt", check=True)
                result = engine_git.run(self.repo, "commit", "-q", "--no-verify", "-m", "engine")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(_ran(self.marker))
                self.assertTrue(_ran(own_marker), "the user's global signer did not sign")
                self.assertIn("gpgsig", _plain(self.repo, "cat-file", "commit", "HEAD").stdout)

                _plain(self.repo, "config", "--unset", key, check=True)
                home.write_text(saved, encoding="utf-8")

    def test_a_planted_default_key_command_never_runs(self):
        _global_config("[commit]", "\tgpgSign = true", "[gpg]", "\tformat = ssh")
        self.local("gpg.ssh.defaultKeyCommand", str(self.planted))
        (self.repo / "p.txt").write_text("p\n", encoding="utf-8")
        _plain(self.repo, "add", "p.txt", check=True)
        _plain(self.repo, "commit", "-q", "-m", "plain")
        self.assertTrue(_ran(self.marker), "positive control: plain git runs the key command")
        engine_git.run(self.repo, "commit", "-q", "--no-verify", "-m", "engine")
        self.assertFalse(_ran(self.marker))

    def test_without_the_overrides_the_planted_program_runs(self):
        # Positive control through the engine itself: with the signing
        # overrides disabled in this test only, the engine's log runs it.
        self._plant("local", ("log.showSignature", "true"))
        with mock.patch.object(engine_git, "_signing_overrides", return_value=[]):
            engine_git.run(self.repo, "log", "--format=%H")
        self.assertTrue(_ran(self.marker))


class FinishMergeSigningTests(_FinishTestCase):
    """The finish merge, with a repository-scoped signing program that was
    already there when the run started (so the run-start snapshot trusts
    it, and the snapshot comparison does not refuse), and the user's own
    global signer configured afterwards."""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name)
        self.planted, self.marker = signer(root, "planted")
        self.own, self.own_marker = signer(root, "global")
        _run_git(self.repo, "config", "gpg.program", str(self.planted))

    def _finish(self, record, *flags: str):
        code, out, err = self._main(
            "finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo), "--json", *flags
        )
        return code, (json.loads(out) if out.strip() else None), err

    def _ready(self, *, checked_out: bool):
        record, head = self._complete()
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        _run_git(self.repo, "add", "other.txt")
        _run_git(self.repo, "commit", "-q", "-m", "other")
        if not checked_out:
            _run_git(self.repo, "checkout", "-q", "feature/x")
        _global_config("[commit]", "\tgpgSign = true", "[gpg]", f"\tprogram = {self.own}")
        # Positive control: plain git in this repository signs with the
        # planted program, not the user's.
        _plain(self.repo, "commit-tree", "-S", "HEAD^{tree}", "-p", "HEAD", "-m", "x", check=True)
        self.assertTrue(_ran(self.marker))
        self.assertFalse(_ran(self.own_marker))
        return record, head

    def _merge(self, head) -> str:
        merge = _git(self.repo, "rev-list", "--merges", "-n", "1", "refs/heads/main")
        self.assertIn(head, _git(self.repo, "rev-list", "--parents", "-n", "1", merge).split()[1:])
        return _git(self.repo, "cat-file", "commit", merge)

    def test_a_merge_in_the_checkout_is_signed_by_the_users_own_signer(self):
        record, head = self._ready(checked_out=True)
        code, report, err = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 0, err)
        self.assertIn("gpgsig", self._merge(head))
        self.assertFalse(_ran(self.marker), "the planted signing program ran")
        self.assertTrue(_ran(self.own_marker), "the user's global signer did not sign")

    def test_a_merge_with_no_checkout_runs_no_planted_program(self):
        # The ref-only merge is a commit-tree, which reads no commit.gpgSign
        # (see SigningProgramTests): unsigned, as under plain Git, and the
        # planted program does not run.
        record, head = self._ready(checked_out=False)
        code, report, err = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 0, err)
        self.assertNotIn("gpgsig", self._merge(head))
        self.assertFalse(_ran(self.marker), "the planted signing program ran")

    def test_without_the_overrides_the_finish_merge_runs_the_planted_program(self):
        record, _ = self._ready(checked_out=True)
        with mock.patch.object(engine_git, "_signing_overrides", return_value=[]):
            code, report, err = self._finish(record, "--allow-merge-commit")
        self.assertEqual(code, 0, err)
        self.assertTrue(_ran(self.marker))


# -- R-7: submodules -------------------------------------------------------------


class SubmoduleTests(_Repo):
    def setUp(self):
        super().setUp()
        self.sub_origin = self.root / "sub-origin"
        _plain(self.root, "init", "-q", "-b", "main", str(self.sub_origin), check=True)
        self.commit("inner.txt", cwd=self.sub_origin)
        _plain(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
               str(self.sub_origin), "sub", check=True)
        _plain(self.repo, "commit", "-q", "-m", "submodule", check=True)
        self.sub = self.repo / "sub"
        self.marker = self.root / "MARKER-submodule-clean"
        clean = self.root / "clean"
        clean.write_text(f'#!/bin/sh\ntouch "{self.marker}"\ncat\n', encoding="utf-8")
        clean.chmod(0o755)
        # The submodule's own configuration and attributes, which no check
        # of the superproject reads.
        _plain(self.sub, "config", "filter.planted.clean", str(clean), check=True)
        (self.sub / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
        self.inner = self.sub / "inner.txt"

    def test_status_and_diff_never_run_the_submodules_filter(self):
        for args in (("status", "--porcelain"), ("diff", "--name-only")):
            with self.subTest(command=args[0]):
                _touch(self.inner)
                _plain(self.repo, *args, check=True)
                self.assertTrue(_ran(self.marker), "positive control: plain git runs the submodule's filter")
                _touch(self.inner)
                result = engine_git.run(self.repo, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(_ran(self.marker))
        # Through an engine function, and with the repository asking for
        # recursion.
        _plain(self.repo, "config", "submodule.recurse", "true", check=True)
        _touch(self.inner)
        dirty_paths(self.repo)
        self.assertFalse(_ran(self.marker))

    def test_without_the_flag_the_engine_would_run_it(self):
        # Positive control through the engine itself.
        _touch(self.inner)
        with mock.patch.object(engine_git, "_SUBMODULE_COMMANDS", frozenset()):
            engine_git.run(self.repo, "status", "--porcelain")
        self.assertTrue(_ran(self.marker))

    def test_an_uncommitted_gitlink_change_is_still_dirty(self):
        (self.sub / ".gitattributes").unlink()
        self.assertEqual(engine_git.run(self.repo, "status", "--porcelain").stdout, "")
        self.commit("moved.txt", cwd=self.sub)

        status = engine_git.run(self.repo, "status", "--porcelain")
        self.assertEqual(status.stdout.strip(), "M sub")
        self.assertIn("sub", dirty_paths(self.repo))
        self.assertEqual(engine_git.run(self.repo, "diff", "--name-only").stdout.strip(), "sub")
        self.assertFalse(_ran(self.marker))


# -- R-8: a configuration listing that fails ------------------------------------


class UnreadableConfigTests(_Repo):
    def test_a_broken_configuration_refuses_with_no_command_run(self):
        with open(self.repo / ".git" / "config", "a", encoding="utf-8") as handle:
            handle.write("[broken\n")
        recorder = GitRecorder(self.root)
        with recorder.active(), self.assertRaises(GitConfigRefusal) as ctx:
            engine_git.run(self.repo, "status", "--porcelain")
        self.assertEqual(ctx.exception.code, "repository_config_unreadable")
        logged = recorder.lines()
        self.assertTrue(logged and all(" config --list " in f" {line} " for line in logged), logged)
        self.assertFalse(any("status" in line for line in logged), logged)

    def _planted_filter(self) -> Path:
        marker = self.root / "MARKER-filter"
        clean = self.root / "clean"
        clean.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n', encoding="utf-8")
        clean.chmod(0o755)
        _plain(self.repo, "config", "filter.planted.clean", str(clean), check=True)
        (self.repo / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
        _touch(self.repo / "base.txt")
        return marker

    def test_a_listing_that_fails_refuses_before_a_planted_filter_could_run(self):
        # A listing can fail while the command itself would run (an older
        # Git without --show-scope, say): simulated here.
        marker = self._planted_filter()
        recorder = GitRecorder(self.root)
        with recorder.active(), mock.patch.object(engine_git, "config_entries", return_value=None):
            with self.assertRaises(GitConfigRefusal) as ctx:
                engine_git.run(self.repo, "status", "--porcelain")
        self.assertEqual(ctx.exception.code, "repository_config_unreadable")
        self.assertEqual(recorder.lines(), [])
        self.assertFalse(marker.exists())

    def test_without_the_refusal_the_command_runs_the_planted_filter(self):
        # Positive control: with the refusal disabled in this test only, the
        # unchecked command runs against the live configuration.
        marker = self._planted_filter()
        with mock.patch.object(engine_git, "config_entries", return_value=None), \
                mock.patch.object(engine_git, "_refuse_unreadable"):
            engine_git.run(self.repo, "status", "--porcelain")
        self.assertTrue(marker.exists())

    def test_outside_a_repository_nothing_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.assertEqual(engine_git.run(outside, "version").returncode, 0)
        made = engine_git.run(self.root, "init", "-q", str(self.root / "fresh"))
        self.assertEqual(made.returncode, 0, made.stderr)
        missing = engine_git.run(self.repo / "no-such-directory", "rev-parse", "HEAD")
        self.assertNotEqual(missing.returncode, 0)


# -- R-4: ownership in a managed worktree ---------------------------------------


class ManagedOwnershipTests(_ManagedRepoTestCase):
    def setUp(self):
        super().setUp()
        # READY over a commit in the managed worktree, interrupted before
        # the acceptance gate (the CLI reports the interruption).
        adapters = (_ManagedStageAdapter(self.repo), _SparringAdapter([READY]))
        with mock.patch("agent_sparring.plan.freeze_candidate", side_effect=KeyboardInterrupt):
            code, _, err = self._main(
                "run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed", adapters=adapters
            )
        self.assertNotEqual(code, 0, err)
        (self.record,) = self._records()
        self.worktree = Path(self.record.worktree_path)
        self.state_path = self.record.sparring_dir / "plans" / f"{self.record.run_key}.json"
        self.stage_id = json.loads(self.state_path.read_text(encoding="utf-8"))["current_stage"]
        self.assertIs(self._stage().read_state().status, StageStatus.WORKING)

    def _stage(self) -> Stage:
        return Stage.resolve(self.record.sparring_dir, self.stage_id)

    def _gate(self, command: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--sparring-dir", str(self.record.sparring_dir), command, self.stage_id,
                "--repo-root", str(self.worktree), "--expected-branch", self.record.branch,
            ])
        return code, err.getvalue()

    def _set_owner(self, run: str | None) -> None:
        stage = self._stage()
        state = stage.read_state()
        state.run = run
        stage.write_state(state)

    def _move_run_state(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        payload["current_stage"] = "elsewhere"
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_the_unmanipulated_stage_is_accepted(self):
        # Positive control for every refusal below.
        self.assertEqual(self._stage().read_state().run, self.record.run_key)
        code, err = self._gate("freeze-candidate")
        self.assertEqual(code, 0, err)
        code, err = self._gate("accept-candidate")
        self.assertEqual(code, 0, err)
        self.assertIs(self._stage().read_state().status, StageStatus.ACCEPTED)

    def _refused(self, manipulate) -> None:
        manipulate()
        before = self._stage().read_state().status
        for command in ("freeze-candidate", "accept-candidate"):
            with self.subTest(command=command):
                code, err = self._gate(command)
                self.assertEqual(code, 1, err)
                self.assertIn(acceptance.STAGE_OWNER_MISMATCH, err)
                self.assertIn(self.record.run_key, err)
        self.assertIs(self._stage().read_state().status, before)

    def test_the_owner_field_cleared_refuses(self):
        self._refused(lambda: self._set_owner(None))

    def test_the_owner_field_rewritten_refuses(self):
        self._refused(lambda: self._set_owner("plan-someone-else-0123456789ab"))

    def test_the_run_state_moved_to_another_stage_refuses(self):
        self._refused(self._move_run_state)

    def test_the_run_state_deleted_refuses(self):
        self._refused(self.state_path.unlink)

    def test_cleared_and_moved_together_refuses(self):
        self._refused(lambda: (self._set_owner(None), self._move_run_state()))

    def test_without_the_managed_record_the_cleared_and_moved_stage_is_hand_driven(self):
        # Positive control: with the managed-record lookup disabled in this
        # test only, the same manipulation makes the owned stage look
        # hand-driven -- no run's history, pause or snapshot applies -- and
        # it is frozen and accepted.
        self._set_owner(None)
        self._move_run_state()
        with mock.patch.object(acceptance, "_managed_owner", return_value=None):
            self.assertIsNone(acceptance._owning_run(self._stage()))
            self.assertEqual(self._gate("freeze-candidate")[0], 0)
            code, err = self._gate("accept-candidate")
        self.assertEqual(code, 0, err)
        self.assertIs(self._stage().read_state().status, StageStatus.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
