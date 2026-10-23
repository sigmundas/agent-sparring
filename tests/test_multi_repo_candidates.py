"""Cross-repository candidate sets: freeze pins them, acceptance verifies them.

The real case this exists for: a stage whose reviewed work is a desktop
change in the primary repository *and* a coupled migration in a second one.
The sparrer's READY verdict depends on both, so pinning only the primary
commit would let the sibling move between review and acceptance and leave
the stage claiming a candidate set that no longer exists.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.acceptance import (
    AcceptanceError,
    StaleCandidateError,
    accept_candidate,
    freeze_candidate,
    record_review,
)
from agent_sparring.routing import RoutingAction
from agent_sparring.stage import CandidateRepository, Stage, StageState, StageStatus


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_repo(root: Path, name: str, branch: str) -> Path:
    remote = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / ".gitignore").write_text(".sparring/stages/\n.sparring/plans/\n", encoding="utf-8")
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "checkout", "-q", "-b", branch)
    _git(repo, "push", "-q", "-u", "origin", branch)
    return repo


def _commit(repo: Path, filename: str, branch: str, *, push: bool = True) -> str:
    (repo / filename).write_text("content\n", encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", filename)
    if push:
        _git(repo, "push", "-q", "origin", branch)
    return _head(repo)


class CrossRepositoryCandidateTests(unittest.TestCase):
    PRIMARY_BRANCH = "feature/desktop"
    SIBLING_BRANCH = "feature/cloud"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.primary = _make_repo(self.root, "sporely-py", self.PRIMARY_BRANCH)
        self.sibling = _make_repo(self.root, "sporely-web", self.SIBLING_BRANCH)
        self.sparring_dir = self.primary / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-3d").create()
        self.primary_sha = _commit(self.primary, "desktop.py", self.PRIMARY_BRANCH)
        self.sibling_sha = _commit(self.sibling, "migration.sql", self.SIBLING_BRANCH)

    def _declare(self, *, candidate_sha: str | None = None, path: str = "../sporely-web") -> None:
        state = self.stage.read_state()
        state.repositories = (
            CandidateRepository(
                name="sporely-web",
                path=path,
                branch=self.SIBLING_BRANCH,
                candidate_sha=candidate_sha,
            ),
        )
        self.stage.write_state(state)

    def _freeze(self):
        # The reviewer's READY over the candidate set, recorded with what it
        # was over (Plan L, I-L2: accept needs it at the exact commits).
        record_review(self.stage, self.primary, RoutingAction.READY)
        return freeze_candidate(
            self.stage, self.sparring_dir, self.primary, expected_branch=self.PRIMARY_BRANCH
        )

    def _accept(self):
        return accept_candidate(self.stage, self.primary, expected_branch=self.PRIMARY_BRANCH)

    # -- the happy path ----------------------------------------------------

    def test_freeze_pins_the_sibling_and_acceptance_verifies_it(self):
        self._declare()
        frozen = self._freeze()

        self.assertEqual(frozen.candidate_sha, self.primary_sha)
        self.assertEqual(len(frozen.repositories), 1)
        self.assertEqual(frozen.repositories[0].candidate_sha, self.sibling_sha)
        # The pin is persisted, so a later accept-candidate in another
        # process verifies the same set.
        recorded = self.stage.read_state().repositories
        self.assertEqual(recorded[0].candidate_sha, self.sibling_sha)

        accepted = self._accept()
        self.assertEqual(accepted.candidate_sha, self.primary_sha)
        self.assertEqual(accepted.repositories[0].candidate_sha, self.sibling_sha)
        self.assertIs(self.stage.read_state().status, StageStatus.ACCEPTED)

    def test_a_stage_with_no_siblings_is_completely_unaffected(self):
        frozen = self._freeze()
        self.assertEqual(frozen.repositories, ())
        accepted = self._accept()
        self.assertEqual(accepted.repositories, ())
        # state.json keeps its original shape: no empty list is introduced.
        raw = (self.stage.directory / "state.json").read_text(encoding="utf-8")
        self.assertNotIn("repositories", raw)

    # -- the hole this closes ---------------------------------------------

    def test_acceptance_refuses_when_the_sibling_moved_after_the_freeze(self):
        self._declare()
        self._freeze()
        moved = _commit(self.sibling, "afterthought.sql", self.SIBLING_BRANCH)
        self.assertNotEqual(moved, self.sibling_sha)

        with self.assertRaises(StaleCandidateError) as ctx:
            self._accept()
        self.assertIn("sporely-web", str(ctx.exception))
        # Nothing was accepted, and the pin was left exactly as frozen.
        self.assertIs(self.stage.read_state().status, StageStatus.FROZEN)
        self.assertEqual(self.stage.read_state().repositories[0].candidate_sha, self.sibling_sha)

    def test_acceptance_refuses_when_the_siblings_worktree_is_dirty(self):
        self._declare()
        self._freeze()
        (self.sibling / "migration.sql").write_text("edited\n", encoding="utf-8")

        with self.assertRaises(AcceptanceError) as ctx:
            self._accept()
        self.assertIn("migration.sql", str(ctx.exception))
        self.assertIs(self.stage.read_state().status, StageStatus.FROZEN)

    # -- refusals at freeze time -------------------------------------------

    def test_an_unpushed_sibling_candidate_is_frozen_locally(self):
        # Plan L (I-L2): was test_freeze_refuses_an_unpushed_sibling_candidate.
        # A sibling's remote is never read; its local rule refuses instead
        # (the dirty and wrong-branch tests below, with candidate_not_on_branch).
        local_only = _commit(self.sibling, "local-only.sql", self.SIBLING_BRANCH, push=False)
        self._declare()
        self._freeze()
        state = self.stage.read_state()
        self.assertIs(state.status, StageStatus.FROZEN)
        self.assertEqual(state.repositories[0].candidate_sha, local_only)

    def test_freeze_refuses_a_dirty_sibling(self):
        (self.sibling / "scratch.sql").write_text("wip\n", encoding="utf-8")
        self._declare()
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("scratch.sql", str(ctx.exception))
        self.assertIs(self.stage.read_state().status, StageStatus.WORKING)

    def test_freeze_refuses_a_sibling_on_the_wrong_branch(self):
        _git(self.sibling, "checkout", "-q", "main")
        self._declare()
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("is on 'main'", str(ctx.exception))

    def test_freeze_refuses_a_declared_sha_that_is_not_where_the_branch_is(self):
        self._declare(candidate_sha="b" * 40)
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("was declared at", str(ctx.exception))

    def test_freeze_accepts_a_declared_sha_that_matches(self):
        self._declare(candidate_sha=self.sibling_sha)
        frozen = self._freeze()
        self.assertEqual(frozen.repositories[0].candidate_sha, self.sibling_sha)

    def test_freeze_refuses_a_sibling_path_that_is_not_a_repository(self):
        self._declare(path="../not-a-repo")
        with self.assertRaises(AcceptanceError) as ctx:
            self._freeze()
        self.assertIn("does not resolve to a git repository", str(ctx.exception))

    def test_a_review_over_another_sibling_commit_refuses_accept(self):
        # Plan L (I-L2): the verdict covers the whole candidate set. The
        # READY was recorded over the sibling's earlier commit; the sibling
        # then moved before the freeze, so the frozen set was never reviewed.
        # (Positive control: test_freeze_pins_the_sibling_and_acceptance_
        # verifies_it, where the review is over the frozen set.)
        self._declare()
        record_review(self.stage, self.primary, RoutingAction.READY)
        moved = _commit(self.sibling, "later.sql", self.SIBLING_BRANCH)
        freeze_candidate(self.stage, self.sparring_dir, self.primary, expected_branch=self.PRIMARY_BRANCH)
        self.assertEqual(self.stage.read_state().repositories[0].candidate_sha, moved)
        with self.assertRaises(AcceptanceError) as ctx:
            self._accept()
        self.assertIn("verdict_not_at_candidate", str(ctx.exception))
        self.assertIs(self.stage.read_state().status, StageStatus.FROZEN)

    def test_accept_refuses_a_declared_but_never_pinned_sibling(self):
        # Freeze pins siblings, so this can only happen if state.json was
        # edited by hand between the two steps. It must not be waved through.
        self._freeze()
        self._declare()
        with self.assertRaises(AcceptanceError) as ctx:
            self._accept()
        self.assertIn("no pinned candidate", str(ctx.exception))


class CandidateRepositoryStateTests(unittest.TestCase):
    def test_round_trips_through_state_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Stage.resolve(Path(tmp) / ".sparring", "s").create()
            state = StageState(
                repositories=(
                    CandidateRepository(
                        name="web", path="../web", branch="feature/x", candidate_sha="c" * 40
                    ),
                )
            )
            stage.write_state(state)
            self.assertEqual(stage.read_state().repositories, state.repositories)

    def test_malformed_repositories_are_refused_not_coerced(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Stage.resolve(Path(tmp) / ".sparring", "s").create()
            (stage.directory / "state.json").write_text(
                '{"status": "working", "repositories": [{"name": "web"}]}', encoding="utf-8"
            )
            with self.assertRaises(Exception):
                stage.read_state()


if __name__ == "__main__":
    unittest.main()
