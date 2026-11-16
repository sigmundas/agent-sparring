"""Plan L Stage 3: the unconditional remote refusal and offline operations.

What these tests hold, one sentence each:

- ``engine_git.run`` refuses every remote command form with
  ``remote_operation_not_allowed`` before anything runs -- including forms
  reached through an alias, ``-c``, ``-C`` or ``--git-dir``, and under a run
  guard (there is no exception context, I-L1). For each form, plain ``git``
  running the very same arguments in the same repository does contact a
  local bare remote, whose marker program runs (the positive control);
- behind the classifier, every command ``engine_git.run`` does start carries
  ``protocol.allow=never`` and ``GIT_NO_LAZY_FETCH``: with the classifier
  disabled in the test only, no transport starts and a promisor remote is
  never fetched from; with those protections disabled too, it is;
- nothing in ``src/`` calls ``engine_git.run_remote`` (the primitive kept for
  Plan P), by a static scan whose own positive control finds a planted call;
- a full managed run, ``runs --json`` polling, the handoff and ``finish-run
  --dry-run`` in a repository with planted remote configuration run no
  remote subcommand -- shown by the ``git`` recorder first on ``PATH``,
  never by mocking ``engine_git`` -- and no planted program; plain ``git``
  in that repository does run them.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import engine_git
from agent_sparring.engine_git import GitConfigRefusal
from agent_sparring.stage import StageStatus

from git_recorder import (
    REAL_GIT,
    GitRecorder,
    PlantedRemotes,
    git,
    is_remote_command,
    make_bare,
    marker_program,
    refs,
)
from test_local_first import RUN_KEY, S1, S2, _CliWorld, head
from test_managed_run import _ManagedRepoTestCase

REMOTE_OPERATION_NOT_ALLOWED = "remote_operation_not_allowed"
SRC = Path(__file__).resolve().parent.parent / "src" / "agent_sparring"


# -- the refusal, one remote form at a time -------------------------------------


class _RemoteWorld(unittest.TestCase):
    """A repository on ``feature/x`` with ``origin``, a real local bare
    remote. Every way Git could reach ``origin`` runs a marker program first:
    ``remote.origin.uploadpack``/``receivepack`` for transfers, a
    ``post-receive`` hook for writes. ``self.programs`` are the same
    stand-ins for forms that name a program on the command line."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.marker = self.root / "MARKER-contacted"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        self.bare = make_bare(self.root / "origin.git", self.marker)
        git(self.repo, "remote", "add", "origin", str(self.bare))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "switch", "-q", "-c", "feature/x")
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "candidate")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.upload = marker_program(bin_dir / "upload-pack", self.marker, "upload-pack")
        self.receive = marker_program(bin_dir / "receive-pack", self.marker, "receive-pack")
        self.archive = marker_program(bin_dir / "upload-archive", self.marker, "upload-archive")
        git(self.repo, "config", "remote.origin.uploadpack", str(self.upload))
        git(self.repo, "config", "remote.origin.receivepack", str(self.receive))
        git(self.bare, "config", "uploadpack.allowFilter", "true")
        self.marker.unlink(missing_ok=True)
        self.recorder = GitRecorder(self.root)

    def snapshot(self) -> tuple[str, str]:
        return refs(self.bare), git(self.repo, "for-each-ref")

    def plain(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        return subprocess.run([REAL_GIT, *args], cwd=self.repo, capture_output=True, text=True,
                              env=env, check=False, timeout=120)

    def assertRefused(self, *args: str, key: str | None = None, env: dict | None = None) -> GitConfigRefusal:
        """``engine_git.run`` refuses ``args`` (with the caller's ``env``
        overrides) with the typed code, nothing runs (no marker, no ref
        anywhere changes) and the recorder sees no remote command."""

        before = self.snapshot()
        self.recorder.clear()
        with self.recorder.active(), self.assertRaises(GitConfigRefusal) as ctx:
            engine_git.run(self.repo, *args, env=env)
        self.assertEqual(ctx.exception.code, REMOTE_OPERATION_NOT_ALLOWED, str(ctx.exception))
        if key is not None:
            self.assertEqual(ctx.exception.key, key)
        self.assertFalse(self.marker.exists(), "the remote was contacted")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.recorder.remote_commands(), [])
        return ctx.exception

    def assertPlainContacts(self, *args: str, evidence=None, env: dict | None = None) -> None:
        """Positive control: plain ``git`` with the same arguments (and
        ``env``) reaches the remote (its marker program runs, or
        ``evidence()`` holds)."""

        self.marker.unlink(missing_ok=True)
        result = self.plain(*args, env=env)
        reached = evidence() if evidence is not None else self.marker.exists()
        self.assertTrue(reached, f"plain git {' '.join(args)} reached nothing: {result.stderr}")
        self.marker.unlink(missing_ok=True)

    def check(self, *args: str, key: str | None = None, evidence=None) -> None:
        self.assertRefused(*args, key=key)
        self.assertPlainContacts(*args, evidence=evidence)

    def _plain_recorded(self, *args: str) -> subprocess.CompletedProcess:
        """Plain ``git`` through ``PATH``, so the recorder logs it."""

        self.recorder.clear()
        with self.recorder.active():
            return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False, timeout=120)

    def assertPlainContactsAndIsRecorded(self, *args: str) -> None:
        """Positive control that also proves the recorder: plain ``git``
        through ``PATH`` with the same arguments reaches the remote, and
        the recorder's own classification flags the logged command."""

        self.marker.unlink(missing_ok=True)
        result = self._plain_recorded(*args)
        self.assertTrue(self.marker.exists(), f"plain git {' '.join(args)} reached nothing: {result.stderr}")
        self.assertTrue(self.recorder.remote_commands(), self.recorder.lines())
        self.marker.unlink(missing_ok=True)


class RemoteFormRefusalTests(_RemoteWorld):
    def test_push(self):
        self.check("push", "origin", "HEAD:refs/heads/pushed", key="push")

    def test_fetch(self):
        self.check("fetch", "origin", key="fetch")

    def test_pull(self):
        self.check("pull", "--no-rebase", "origin", "main", key="pull")

    def test_ls_remote(self):
        self.check("ls-remote", "origin", key="ls-remote")

    def test_remote_update(self):
        self.check("remote", "update", key="remote update")

    def test_remote_show(self):
        self.check("remote", "show", "origin", key="remote show")

    def test_remote_prune(self):
        self.check("remote", "prune", "origin", key="remote prune")

    def test_remote_set_head_auto(self):
        self.check("remote", "set-head", "origin", "--auto", key="remote set-head")

    def test_remote_add_with_fetch(self):
        self.check("-c", f"remote.other.uploadpack={self.upload}", "remote", "add", "-f", "other",
                   str(self.bare), key="remote add")

    def test_clone_from_a_url(self):
        self.check("clone", "-q", "-u", str(self.upload), f"file://{self.bare}", str(self.root / "cloned"),
                   key="clone")

    def test_fetch_pack(self):
        self.check("fetch-pack", f"--upload-pack={self.upload}", str(self.bare), "refs/heads/main",
                   key="fetch-pack")

    def test_send_pack(self):
        self.check("send-pack", f"--receive-pack={self.receive}", str(self.bare), "HEAD:refs/heads/sent",
                   key="send-pack")

    def test_request_pull(self):
        self.check("request-pull", "main", "origin", key="request-pull")

    def test_archive_from_a_remote(self):
        self.check("archive", f"--remote={self.bare}", f"--exec={self.archive}",
                   "-o", str(self.root / "archive.tar"), "main", key="archive --remote")

    def test_maintenance_prefetch(self):
        self.check("maintenance", "run", "--task=prefetch", key="maintenance run")

    def test_submodule_add_from_a_url(self):
        sub = self.root / "sub"
        sub.mkdir()
        git(sub, "init", "-q", "-b", "main")
        git(sub, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q",
            "--allow-empty", "-m", "sub")
        self.check("-c", "protocol.file.allow=always", "submodule", "add", f"file://{sub}", "vendored",
                   key="submodule add",
                   evidence=lambda: (self.repo / "vendored" / ".git").exists())

    def test_submodule_update(self):
        sub = self.root / "sub"
        sub.mkdir()
        git(sub, "init", "-q", "-b", "main")
        git(sub, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q",
            "--allow-empty", "-m", "sub")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendored")
        git(self.repo, "commit", "-q", "-m", "submodule")
        git(self.repo, "submodule", "deinit", "-q", "-f", "vendored")
        modules = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        modules = (modules if modules.is_absolute() else self.repo / modules) / "modules" / "vendored"
        subprocess.run(["rm", "-rf", str(modules)], check=True)
        self.check("-c", "protocol.file.allow=always", "submodule", "update", "--init", key="submodule update",
                   evidence=modules.exists)

    def test_a_remote_helper_url(self):
        # ``ext::`` runs a command as the transport; plain git with it
        # allowed runs the marker.
        url = f"ext::sh -c touch% {self.marker}"
        self.check("-c", "protocol.ext.allow=always", "ls-remote", url, key="ls-remote")

    # Options before the nested command are valid Git; with the transport
    # explicitly allowed, the refusal is the classifier's alone.

    def test_remote_show_with_an_option_first(self):
        args = ("-c", "protocol.file.allow=always", "remote", "-v", "show", "origin")
        self.assertRefused(*args, key="remote show")
        self.assertPlainContactsAndIsRecorded(*args)

    def test_remote_update_with_an_option_first(self):
        args = ("-c", "protocol.file.allow=always", "remote", "--verbose", "update")
        self.assertRefused(*args, key="remote update")
        self.assertPlainContactsAndIsRecorded(*args)

    def _submodule(self) -> Path:
        """A committed submodule ``vendored`` of a local repository; returns
        its module directory (where a clone of it lands)."""

        sub = self.root / "sub"
        sub.mkdir()
        git(sub, "init", "-q", "-b", "main")
        git(sub, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q",
            "--allow-empty", "-m", "sub")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendored")
        git(self.repo, "commit", "-q", "-m", "submodule")
        common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        return (common if common.is_absolute() else self.repo / common) / "modules" / "vendored"

    def test_submodule_update_with_an_option_first(self):
        modules = self._submodule()
        git(self.repo, "submodule", "deinit", "-q", "-f", "vendored")
        subprocess.run(["rm", "-rf", str(modules)], check=True)
        args = ("-c", "protocol.file.allow=always", "submodule", "--quiet", "update", "--init")
        self.assertRefused(*args, key="submodule update")
        self.assertFalse(modules.exists())
        # Positive control: plain git with the same arguments clones the
        # submodule from its repository, and the recorder flags it.
        result = self._plain_recorded(*args)
        self.assertTrue(modules.exists(), f"plain git cloned nothing: {result.stderr}")
        self.assertTrue(self.recorder.remote_commands(), self.recorder.lines())

    def test_submodule_foreach(self):
        # ``foreach`` runs a shell command in each submodule: what it runs
        # cannot be known, so it is refused.
        self._submodule()
        args = ("submodule", "--quiet", "foreach", f"touch {self.marker}")
        self.assertRefused(*args, key="submodule foreach")
        # Positive control: plain git runs the command, and the recorder
        # flags it.
        self._plain_recorded(*args)
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.recorder.remote_commands(), self.recorder.lines())

    def test_any_command_given_a_repository_url(self):
        # The classifier's own URL rule, on a subcommand that is not itself
        # a transfer: whatever reads the argument as a repository gets none.
        for args in (
            ("bundle", "verify", f"file://{self.bare}"),
            ("log", "ssh://example.invalid/x.git"),
            ("show", "git@example.invalid:x.git"),
            ("rev-parse", "ext::sh -c true"),
        ):
            with self.subTest(args=args):
                refusal = self.assertRefused(*args)
                self.assertIn("with a repository URL", refusal.key)
        # Positive control for the rule: the same argument shapes are what
        # plain git reads as repositories (``ls-remote`` contacts the URL).
        self.assertPlainContacts("ls-remote", "--upload-pack", str(self.upload), f"file://{self.bare}")


class ReachedThroughGlobalOptionsTests(_RemoteWorld):
    def test_through_a_config_option(self):
        self.check("-c", "x.y=1", "push", "origin", "HEAD:refs/heads/viac", key="push")

    def test_through_dash_c_directory(self):
        self.check("-C", str(self.repo), "fetch", "origin", key="fetch")

    def test_through_git_dir(self):
        self.check("--git-dir", str(self.repo / ".git"), "ls-remote", "origin", key="ls-remote")

    def test_through_git_dir_with_equals_and_work_tree(self):
        self.check(f"--git-dir={self.repo / '.git'}", "--work-tree", str(self.repo), "fetch", "origin",
                   key="fetch")

    def test_under_a_run_guard_too(self):
        # No exception context: a run's trusted snapshot and pinned
        # destination change nothing.
        snapshot = engine_git.capture_snapshot(self.repo)
        with engine_git.trusted_config([self.repo], snapshot, destinations=(str(self.bare),)):
            self.assertRefused("push", str(self.bare), "HEAD:refs/heads/guarded", key="push")
            self.assertRefused("ls-remote", str(self.bare), key="ls-remote")
        self.assertPlainContacts("push", str(self.bare), "HEAD:refs/heads/guarded")


class ReachedThroughAliasesTests(_RemoteWorld):
    def test_an_alias_given_with_dash_c(self):
        self.check("-c", "alias.sync=fetch", "sync", "origin", key="fetch")

    def test_an_alias_in_the_repository_configuration(self):
        git(self.repo, "config", "alias.up", "ls-remote origin")
        self.check("up", key="ls-remote")

    def test_a_chained_alias(self):
        git(self.repo, "config", "alias.outer", "inner")
        git(self.repo, "config", "alias.inner", "push origin HEAD:refs/heads/chained")
        self.check("outer", key="push")

    def test_a_shell_alias(self):
        git(self.repo, "config", "alias.shup", "!git ls-remote origin")
        self.check("shup", key="alias shup (a shell command)")

    def test_a_quoted_alias_given_with_dash_c(self):
        # Git splits an alias with shell-like quoting: "fetch" is fetch.
        self.check("-c", 'alias.sync="fetch"', "sync", "origin", key="fetch")

    def test_a_quoted_alias_in_the_repository_configuration(self):
        git(self.repo, "config", "alias.qup", "'ls-remote' \"origin\"")
        self.check("qup", key="ls-remote")

    def test_an_alias_given_through_config_env(self):
        with mock.patch.dict(os.environ, {"STAGE3_ALIAS": "fetch"}):
            self.check("--config-env=alias.envsync=STAGE3_ALIAS", "envsync", "origin", key="fetch")
            self.check("--config-env", "alias.envsync=STAGE3_ALIAS", "envsync", "origin", key="fetch")

    def test_an_alias_given_through_config_env_with_the_callers_environment(self):
        # The caller's env overrides are what the command runs with, so they
        # are what the alias is resolved with -- even when the engine's own
        # process environment says something harmless.
        harmless = {"STAGE3_ALIAS": "version"}
        for value, key in (("fetch", "fetch"), ("!git ls-remote origin", "alias envsync (a shell command)")):
            with self.subTest(alias=value), mock.patch.dict(os.environ, harmless):
                self.assertRefused("--config-env=alias.envsync=STAGE3_ALIAS", "envsync", "origin",
                                   key=key, env={"STAGE3_ALIAS": value})
                # Positive control: plain git with that environment reaches
                # the remote (the shell alias runs its own git ls-remote).
                self.assertPlainContacts("--config-env=alias.envsync=STAGE3_ALIAS", "envsync", "origin",
                                         env={"STAGE3_ALIAS": value})
        # And with only the harmless value, the same call runs locally.
        with mock.patch.dict(os.environ, harmless):
            result = engine_git.run(self.repo, "--config-env=alias.envsync=STAGE3_ALIAS", "envsync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git version", result.stdout)
        self.assertFalse(self.marker.exists())

    def _other_repository(self) -> Path:
        """A second repository, reaching the same marked origin, whose own
        configuration -- not the engine's cwd -- defines the alias."""

        other = self.root / "other"
        git(self.root, "clone", "-q", str(self.bare), str(other))
        git(other, "config", "remote.origin.uploadpack", str(self.upload))
        git(other, "config", "alias.otherup", "ls-remote origin")
        self.marker.unlink(missing_ok=True)
        return other

    def test_an_alias_from_another_repository_selected_with_dash_c_directory(self):
        other = self._other_repository()
        self.check("-C", str(other), "otherup", key="ls-remote")

    def test_an_alias_from_another_repository_selected_with_git_dir(self):
        other = self._other_repository()
        self.check("--git-dir", str(other / ".git"), "otherup", key="ls-remote")

    def test_an_alias_never_shadows_a_builtin(self):
        # Git ignores an alias named like a builtin, so the engine does too:
        # its own ``status`` is not refused for an alias it would never run.
        git(self.repo, "config", "alias.status", "push origin HEAD:refs/heads/shadow")
        result = engine_git.run(self.repo, "status", "--porcelain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        # Positive control: plain git also runs the builtin, not the alias.
        self.plain("status", "--porcelain")
        self.assertFalse(self.marker.exists())
        self.assertNotIn("shadow", refs(self.bare))


class LocalCommandsStillRunTests(_RemoteWorld):
    """The refusal is about remote forms only (positive control for the
    classifier as a whole): the engine's local commands are unaffected."""

    def test_local_commands_that_mention_remotes_run(self):
        for args in (
            ("remote", "get-url", "origin"),
            ("remote", "show", "-n", "origin"),
            ("remote",),
            ("config", "--get", "remote.origin.url"),
            ("rev-parse", "HEAD"),
            ("log", "-1", "--format=%H"),
            # Paths after ``--`` and a message are never read as repositories.
            ("log", "-1", "--format=%H", "--", "std::odd.txt", "user@host:file"),
            ("update-ref", "-m", "https://example.invalid/ note", "refs/heads/noted", "HEAD"),
            ("for-each-ref", "refs/remotes"),
            ("status", "--porcelain"),
        ):
            with self.subTest(args=args):
                result = engine_git.run(self.repo, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())


# -- behind the classifier --------------------------------------------------------


class TransportOffForLocalCommandsTests(_RemoteWorld):
    """``protocol.allow=never``, ``GIT_ALLOW_PROTOCOL=none`` and
    ``GIT_NO_LAZY_FETCH`` on every command ``engine_git.run`` starts: shown
    with the classifier disabled in the test only (``mock.patch``), and
    their own positive control with them disabled as well."""

    def _unclassified(self):
        return mock.patch.object(engine_git, "remote_form", return_value=None)

    def test_no_transport_starts_even_if_the_classifier_missed_a_form(self):
        # Including when a protocol-specific setting says ``always`` -- on
        # the command line or in the repository -- which outranks
        # ``protocol.allow``: GIT_ALLOW_PROTOCOL outranks them all.
        git(self.repo, "config", "protocol.file.allow", "always")
        for args in (("ls-remote", "origin"), ("-c", "protocol.file.allow=always", "ls-remote", "origin")):
            with self.subTest(args=args):
                with self._unclassified():
                    result = engine_git.run(self.repo, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not allowed", result.stderr)
                self.assertFalse(self.marker.exists())
                # Positive control: without the protections as well, the
                # same engine call reaches the remote.
                with self._unclassified(), mock.patch.object(engine_git, "LOCAL_OVERRIDES", ()), \
                        mock.patch.object(engine_git, "LOCAL_ENVIRONMENT", {}):
                    engine_git.run(self.repo, *args)
                self.assertTrue(self.marker.exists())
                self.marker.unlink()

    def test_protocol_allow_alone_would_not_hold_against_a_protocol_specific_setting(self):
        # Why GIT_ALLOW_PROTOCOL is needed: with only protocol.allow=never,
        # a protocol.file.allow=always reaches the remote.
        with self._unclassified(), mock.patch.object(engine_git, "LOCAL_ENVIRONMENT", {}):
            engine_git.run(self.repo, "-c", "protocol.file.allow=always", "ls-remote", "origin")
        self.assertTrue(self.marker.exists())

    def test_a_promisor_remote_is_never_fetched_from_by_a_local_command(self):
        partial = self.root / "partial"
        git(self.root, "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{self.bare}", str(partial))
        git(partial, "config", "remote.origin.uploadpack", str(self.upload))
        blob = git(partial, "rev-parse", "origin/main:a.txt")
        self.marker.unlink(missing_ok=True)

        result = engine_git.run(partial, "cat-file", "-p", blob)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists(), "a local command lazily fetched from the promisor")
        # Positive controls: with both protections disabled the same engine
        # call fetches the blob from the promisor, and so does plain git.
        with mock.patch.object(engine_git, "LOCAL_OVERRIDES", ()), \
                mock.patch.object(engine_git, "LOCAL_ENVIRONMENT", {}):
            fetched = engine_git.run(partial, "cat-file", "-p", blob)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertTrue(self.marker.exists())


# -- the recorder's own classification -----------------------------------------


class RecorderClassificationTests(unittest.TestCase):
    """The recorder is the test's independent oracle for I-L1: options
    before a nested subcommand must not hide a remote command from it."""

    def test_remote_forms_are_flagged_wherever_their_options_sit(self):
        for line in (
            "remote show origin", "remote -v show origin", "remote --verbose update",
            "remote -v prune origin", "remote -v set-head origin --auto", "remote -v add -f x /r",
            "submodule update --init", "submodule --quiet update --init", "submodule --quiet add /r x",
            "submodule --quiet foreach true", "-c protocol.file.allow=always remote -v show origin",
            "-C /repo --git-dir /repo/.git fetch origin", "maintenance run --task=prefetch",
        ):
            with self.subTest(line=line):
                self.assertTrue(is_remote_command(line))

    def test_local_forms_are_not_flagged(self):
        # Positive control for the oracle: it does not flag everything.
        for line in (
            "remote -v", "remote", "remote get-url origin", "submodule --quiet status",
            "rev-parse HEAD", "-c core.hooksPath=/dev/null status --porcelain",
        ):
            with self.subTest(line=line):
                self.assertFalse(is_remote_command(line))


# -- run_remote has no production caller ----------------------------------------


def run_remote_references(source: str) -> list[int]:
    """Line numbers in ``source`` that reference ``run_remote`` other than
    its own definition: a call, an attribute, a name, or an import."""

    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == "run_remote":
            found.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "run_remote":
            found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name == "run_remote" for a in node.names):
            found.append(node.lineno)
    return found


class NoProductionCallerTests(unittest.TestCase):
    def test_nothing_in_src_references_run_remote(self):
        files = sorted(SRC.rglob("*.py"))
        self.assertGreater(len(files), 20)
        callers = {
            str(path.relative_to(SRC)): lines
            for path in files
            if (lines := run_remote_references(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(callers, {})
        # It is still there, for Plan P.
        self.assertIn("def run_remote(", (SRC / "engine_git.py").read_text(encoding="utf-8"))

    def test_positive_control_the_scan_finds_every_way_of_calling_it(self):
        for source in (
            "from agent_sparring import engine_git\nengine_git.run_remote(r, ())\n",
            "from agent_sparring.engine_git import run_remote\n",
            "from agent_sparring.engine_git import run_remote as push\npush(r, ())\n",
            "import agent_sparring.engine_git as g\nf = g.run_remote\n",
            "def run(cwd, *args):\n    return run_remote(cwd, (), *args)\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(run_remote_references(source))
        self.assertEqual(run_remote_references("def run_remote(repo_root, trusted):\n    pass\n"), [])


# -- offline status, end to end, with planted configuration ----------------------


class PlantedConfigurationEndToEndTests(_CliWorld):
    """A full run through the real CLI (fake providers), then the status
    surfaces the companion extension polls, in a repository whose
    configuration plants a decoy origin, a URL rewrite, an SSH command and a
    credential helper (all present at run start, so the run's own snapshot
    trusts them -- a planted change would be refused by name instead)."""

    def setUp(self) -> None:
        super().setUp()
        self.planted = PlantedRemotes(self.repo, self.root, branch="feature/x")

    def test_a_run_status_polling_and_the_handoff_contact_no_remote(self):
        with self.recorder.active():
            code, _out, err = self.run_plan()
            self.assertEqual(code, 0, err)
            polls = [self.cli("runs", "--repo-root", str(self.repo), "--json") for _ in range(2)]
            handoff = self.cli("handoff", S2, "--claims", "done", "--repo-root", str(self.repo), "--json")
            finish = self.cli("finish-run", "--run-key", RUN_KEY, "--repo-root", str(self.repo),
                              "--dry-run", "--json")

        self.assertEqual((self.status(S1), self.status(S2)), (StageStatus.ACCEPTED, StageStatus.ACCEPTED))
        for code, out, err in polls:
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["schema_version"], 1)
        code, out, err = handoff
        self.assertEqual(code, 0, err)
        content = json.loads(out)["content"]
        self.assertIn(f"- Local commit: `{head(self.repo)}` on `feature/x`", content)
        self.assertIn("- Branch: `feature/x`", content)
        self.assertTrue(json.loads(finish[1]), finish[2])
        self.assertTrue(self.recorder.lines())
        self.assertEqual(self.recorder.remote_commands(), [])
        self.assertEqual(self.planted.ran(), [])
        self.assertNotIn(head(self.repo), refs(self.planted.decoy))

    def test_positive_control_plain_git_runs_every_planted_program(self):
        with self.recorder.active():
            ran = self.planted.positive_controls()
        self.assertEqual(sorted(ran), sorted(m.name for m in self.planted.markers))
        self.assertTrue(self.recorder.remote_commands())


class PlantedConfigurationManagedRunTests(_ManagedRepoTestCase):
    """The same, for a managed run (its own worktree and record): the run,
    ``runs --json`` polling and ``finish-run --dry-run``."""

    def test_a_managed_run_its_polling_and_finish_dry_run_contact_no_remote(self):
        root = self.repo.parent
        planted = PlantedRemotes(self.repo, root, branch="main")
        recorder = GitRecorder(root)
        with recorder.active():
            code, _out, err = self._start_managed()
            self.assertEqual(code, 0, err)
            (record,) = self._records()
            polls = [self._main("runs", "--repo-root", str(self.repo), "--json") for _ in range(2)]
            finish = self._main("finish-run", "--run-key", record.run_key, "--repo-root", str(self.repo),
                                "--dry-run", "--json")

        for code, out, err in polls:
            self.assertEqual(code, 0, err)
            (run,) = json.loads(out)["runs"]
            self.assertEqual(run["run_key"], record.run_key)
        report = json.loads(finish[1])
        self.assertNotIn("candidate_not_pushed", {c["code"] for c in report["checks"] if not c["ok"]})
        self.assertTrue(recorder.lines())
        self.assertEqual(recorder.remote_commands(), [])
        self.assertEqual(planted.ran(), [])
        # Positive control: plain git in the same repository runs them.
        self.assertEqual(sorted(planted.positive_controls()), sorted(m.name for m in planted.markers))


if __name__ == "__main__":
    unittest.main()
