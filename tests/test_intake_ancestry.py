"""Plan intake: approval-time drift accepts the transitive dependency closure.

A slice's repository may have moved since intake only to the exact commit a
slice it depends on -- directly or through another slice -- was accepted at.
For ``A1 -> B1 -> A2`` (A slices in ``app``, B slices in ``web``), approving
A2 must recognise A1's accepted candidate although A2 depends on A1 only
through B1. Nothing broader counts: not a later commit, not a slice that is
not proven complete and accepted, not another intake's accepted commit.

These drive the real prepare -> approve -> run-plan path with scripted
providers, as :mod:`test_intake_sealing` does.
"""

import json
import unittest
import uuid

import conftest_path  # noqa: F401

from agent_sparring.intake import IntakeError, approve_plan, prepare_plan
from agent_sparring.plan import PlanRunStatus, run_state_path
from test_intake import FakeAdapter, _git, lines
from test_intake_sealing import APP_BRANCH, _Committer, _head, _Sealed
from test_plan import READY, _SparringAdapter

WEB_BRANCH = "feature/web"

# Each stage is its own run slice; each depends on the one before it.
CHAIN = [("A1", "app"), ("B1", "web"), ("A2", "app"), ("B2", "web"), ("B3", "web"), ("A3", "app")]

PLAN = "# Chain\n\nStatus: planned.\n\n## Principles\n\n- Identity never changes.\n\n" + "".join(
    f"## Stage {label} — Work {label}\n\nRepos: {repo}. Do {label}.\n\nAcceptance: done {label}.\n\n"
    for label, repo in CHAIN
)


def chain_interpretation() -> dict:
    runs = []
    previous = None
    for label, repo in CHAIN:
        runs.append(
            {
                "id": label.lower(),
                "primary_repository": repo,
                "expected_branch": None,
                "rationale": f"{repo} work {label}",
                "stages": [
                    {
                        "label": label,
                        "title": f"Work {label}",
                        "source_ranges": [lines(f"## Stage {label} ", f"done {label}.", text=PLAN)],
                        "context_ids": ["status", "principles"],
                        "repositories": [],
                        "depends_on": [previous] if previous else [],
                        "mode": "implementation",
                        "intake_scope": None,
                        "rationale": "",
                    }
                ],
            }
        )
        previous = label
    return {
        "verdict": "executable_with_recommendations",
        "summary": "A chain alternating between two repositories.",
        "context": [
            {"id": "status", "title": "Status", "ranges": [lines("# Chain", "Status:", text=PLAN)], "reason": "authorization"},
            {"id": "principles", "title": "Principles", "ranges": [lines("## Principles", "Identity never", text=PLAN)], "reason": "invariants"},
        ],
        "runs": runs,
        "gates": [],
        "excluded": [],
        "findings": [],
        "amended_plan": None,
    }


def parallel_interpretation() -> dict:
    """A1 -> B1 and A1 -> A2: B1 (web) runs beside A2 (app), as Stage 5 beside Stage 3P."""

    payload = chain_interpretation()
    for run in payload["runs"]:
        if run["id"] == "a2":
            run["stages"][0]["depends_on"] = ["A1"]
    return payload


class TransitiveAncestryTests(_Sealed):
    def setUp(self):
        super().setUp()
        self.plan.write_text(PLAN, encoding="utf-8")
        _git(self.repo, "add", "docs/plan.md")
        _git(self.repo, "commit", "-q", "-m", "chain plan")
        _git(self.repo, "push", "-q", "origin", APP_BRANCH)

    def prepare(self, payload=None, **_):
        return prepare_plan(
            self.plan,
            self.repo,
            self.sparring_dir,
            FakeAdapter(payload or chain_interpretation()),
            mode="faithful",
            primary_repository="app",
            context_repositories=self.context,
            project_context="Project context text.",
            mint_run_key=lambda label: f"chain-run-{uuid.uuid4().hex[:8]}",
        )

    def repo_of(self, run):
        return (self.repo, APP_BRANCH) if dict(CHAIN)[run.upper()] == "app" else (self.web, WEB_BRANCH)

    def approve_slice(self, intake, run):
        repo, _ = self.repo_of(run)
        return approve_plan(
            intake.directory,
            run_id=run,
            repo_root=repo,
            sparring_dir=repo / ".sparring",
            primary_repository=dict(CHAIN)[run.upper()],
        )

    def run_slice(self, approval, run):
        repo, branch = self.repo_of(run)
        stage, sparrer = _Committer(repo, branch), _SparringAdapter([READY] * 4)
        result = self.start(approval, lambda s: (stage, sparrer), repo=repo, branch=branch)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        return _head(repo)

    def complete(self, intake, *runs):
        """Approve and run each slice to acceptance; its final candidate."""

        return {run: self.run_slice(self.approve_slice(intake, run), run) for run in runs}

    def record(self, approval):
        return json.loads(approval.approval_path.read_text(encoding="utf-8"))

    def commit_on(self, repo, name):
        (repo / name).write_text("x\n", encoding="utf-8")
        _git(repo, "add", name)
        _git(repo, "commit", "-q", "-m", name)

    def test_an_indirect_ancestors_accepted_candidate_is_intake_progress(self):
        # A1 -> B1 -> A2: app moved only by A1, which A2 reaches through B1.
        intake = self.prepare()
        heads = self.complete(intake, "a1", "b1")
        self.assertEqual(_head(self.repo), heads["a1"])

        approval = self.approve_slice(intake, "a2")
        self.assertTrue(approval.created)
        prerequisites = self.record(approval)["prerequisites"]
        self.assertEqual([e["run_id"] for e in prerequisites["earlier_slices"]], ["b1"])
        (indirect,) = prerequisites["indirect_slices"]
        self.assertEqual((indirect["run_id"], indirect["final_candidate_sha"]), ("a1", heads["a1"]))
        self.assertEqual(self.run_slice(approval, "a2"), _head(self.repo))

    def test_a_multi_level_chain_recognises_every_ancestor(self):
        intake = self.prepare()
        heads = self.complete(intake, "a1", "b1", "a2", "b2", "b3")
        # A3 -> B3 -> B2 -> A2: app is at A2's candidate, three slices up.
        self.assertEqual(_head(self.repo), heads["a2"])
        approval = self.approve_slice(intake, "a3")
        indirect = self.record(approval)["prerequisites"]["indirect_slices"]
        self.assertEqual(sorted(e["run_id"] for e in indirect), ["a1", "a2", "b1", "b2"])
        self.assertEqual(self.record(approval)["repositories"]["at_approval"]["app"]["head"], heads["a2"])

    def test_direct_dependency_movement_is_still_recognised_and_recorded(self):
        intake = self.prepare()
        heads = self.complete(intake, "a1")
        approval = self.approve_slice(intake, "b1")  # B1 -> A1 directly
        prerequisites = self.record(approval)["prerequisites"]
        (direct,) = prerequisites["earlier_slices"]
        self.assertEqual((direct["run_id"], direct["final_candidate_sha"]), ("a1", heads["a1"]))
        self.assertEqual(prerequisites["indirect_slices"], [])

    def test_a_commit_after_an_indirect_ancestors_candidate_refuses(self):
        intake = self.prepare()
        self.complete(intake, "a1", "b1")
        self.commit_on(self.repo, "descendant.txt")
        with self.assertRaisesRegex(IntakeError, "app: .*not a commit an earlier slice was accepted at"):
            self.approve_slice(intake, "a2")
        self.assertFalse((intake.directory / "runs" / "a2" / "approval.json").exists())

    def test_an_indirect_ancestor_not_proven_complete_does_not_count(self):
        intake = self.prepare()
        a1 = self.approve_slice(intake, "a1")
        self.run_slice(a1, "a1")
        self.complete(intake, "b1")
        # A1's run record no longer proves completion.
        state = run_state_path(self.sparring_dir, a1.run_key)
        original = json.loads(state.read_text(encoding="utf-8"))
        state.write_text(json.dumps({**original, "status": "running"}), encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "app: .* moved from"):
            self.approve_slice(intake, "a2")
        state.unlink()
        with self.assertRaisesRegex(IntakeError, "app: .* moved from"):
            self.approve_slice(intake, "a2")

    def test_an_unrelated_intakes_accepted_commit_refuses(self):
        intake = self.prepare()
        self.complete(intake, "a1", "b1")
        # Another intake of the same plan, prepared now, accepts its own A1.
        other = self.prepare()
        other_heads = self.complete(other, "a1")
        self.assertEqual(_head(self.repo), other_heads["a1"])
        with self.assertRaisesRegex(IntakeError, "app: .*not a commit an earlier slice was accepted at"):
            self.approve_slice(intake, "a2")

    def test_a_protected_branch_still_refuses_at_an_ancestors_candidate(self):
        intake = self.prepare()
        heads = self.complete(intake, "a1", "b1")
        _git(self.repo, "checkout", "-q", "-B", "main", heads["a1"])
        with self.assertRaisesRegex(IntakeError, "needs a feature branch"):
            self.approve_slice(intake, "a2")


if __name__ == "__main__":
    unittest.main()

    def test_a_completed_parallel_slices_candidate_is_not_drift(self):
        intake = self.prepare(parallel_interpretation())
        heads = self.complete(intake, "a1", "b1")
        self.assertEqual(_head(self.web), heads["b1"])
        approval = self.approve_slice(intake, "a2")
        self.assertTrue(approval.created)
        prerequisites = self.record(approval)["prerequisites"]
        self.assertEqual([e["run_id"] for e in prerequisites["earlier_slices"]], ["a1"])
        self.assertEqual(prerequisites["indirect_slices"], [], "B1 is not a prerequisite of A2")
        (parallel,) = prerequisites["parallel_slices"]
        self.assertEqual((parallel["run_id"], parallel["final_candidate_sha"]), ("b1", heads["b1"]))

    def test_a_commit_after_a_parallel_slices_candidate_still_refuses(self):
        intake = self.prepare(parallel_interpretation())
        self.complete(intake, "a1", "b1")
        self.commit_on(self.web, "unreviewed.txt")
        with self.assertRaisesRegex(IntakeError, "web: .*not a commit an earlier slice was accepted at"):
            self.approve_slice(intake, "a2")
