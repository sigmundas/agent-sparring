"""The logical view of a plan across repositories, end to end.

Stage 1 runs in the home repository, Stages 2 and 3 in ``web``, with a
Markdown ``Gate before:`` Stage 3; Stage 1's reviewer defers a check to plan
completion. Start → finish → continue → stop at the gate → release it with
``pass`` → answer the plan's obligation → finish, with ``runs --json`` read
at every step from both repositories.

Every view is compared with ``tests/fixtures/logical_plan_view.json``, the
published shape the editor extension tests against. Regenerate it with
``SPARRING_UPDATE_FIXTURES=1`` after a deliberate change to the view.
"""

import json
import os
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring import logical_plan, managed_finish
from agent_sparring.plan_obligations import load_ledger
from test_cross_repository import K, K2, _CrossCase
from test_deferred_human_verification import READY_WITH_DEFERRAL
from test_plan import NEEDS_YOU, READY, _run_git
from test_plan_ownership import plan

FIXTURE = Path(__file__).parent / "fixtures" / "logical_plan_view.json"
GATE = "canary"
CHECK = "resize-readability"
GATED_CROSS = plan(
    "Build here.",
    "Repository: web\n\nBuild there.",
    "Repository: web\n\nGate before: canary — Staging canary\n> Set `canary = true` in staging first.\n\nBuild more.",
)


def _normalized(view: dict) -> dict:
    """``view`` without what differs between runs: gate instance ids."""

    gates = [{**gate, "instance_id": "<instance>" if gate["instance_id"] else None} for gate in view["gates"]]
    return {**view, "gates": gates}


class LogicalViewEndToEndTests(_CrossCase):
    verdicts = [READY_WITH_DEFERRAL, READY, READY]

    def setUp(self):
        super().setUp()
        self.plan_path.write_text(GATED_CROSS, encoding="utf-8")
        _run_git(self.repo, "commit", "-qam", "gated cross plan")
        self.views: list[dict] = []

    def runs(self, where: Path) -> dict:
        code, out, err = self.run_cli("runs", "--json", "--repo-root", str(where))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def step(self, name: str, *, state: list[str], next_: dict | None, gates: list[str]) -> dict:
        """The view at this step: the same from both repositories once the
        target takes part, and the same as :func:`logical_plan.view`."""

        home = self.runs(self.repo)
        self.assertEqual(home["schema_version"], managed_finish.RUNS_SCHEMA_VERSION)
        (view,) = home["plans"]
        self.assertEqual(view, logical_plan.view(self.home_common(), self.logical()))
        web = self.runs(self.web)
        self.assertEqual(web["plans"], [view])  # the target names the home from the start
        self.assertEqual([s["state"] for s in view["stages"]], state, name)
        self.assertEqual(view["next"], next_, name)
        self.assertEqual([g["state"] for g in view["gates"]], gates, name)
        self.views.append({"step": name, "view": _normalized(view)})
        return view

    def test_start_finish_continue_gate_pass_and_finish(self):
        main = {"target_branch": "main"}
        # start: Stage 1 in the home repository, stopping where web begins.
        code, out, err = self.start()
        self.assertEqual(code, 0, err)
        self.assertIn("Stage 2 belongs to web. Finish this part, then continue the plan.", out)
        (carried,) = load_ledger(logical_plan.ledger_path(self.home_common(), K))
        self.assertEqual(carried.origin_slice, K)
        view = self.step(
            "started", state=["accepted", "not_started", "not_started"],
            next_={"repository": "repo", **main, "action": "finish", "answer": None}, gates=["pending"],
        )
        self.assertEqual([s["repository"] for s in view["stages"]], ["repo", "web", "web"])
        self.assertEqual([s["run_key"] for s in view["stages"]], [K, None, None])
        (gate,) = view["gates"]
        self.assertEqual((gate["id"], gate["before_stage"], gate["repository"]), (GATE, view["stages"][2]["stage_id"], "web"))
        self.assertEqual(gate["reason"], "Set `canary = true` in staging first.")
        (run,) = self.runs(self.repo)["runs"]
        self.assertEqual(run["logical_plan"], K)

        # finish: the home part is integrated; the plan continues into web.
        self.finish_first()
        self.step(
            "home_part_finished", state=["accepted", "not_started", "not_started"],
            next_={"repository": "web", **main, "action": "continue", "answer": None}, gates=["pending"],
        )

        # continue: authorized with the token; Stage 2 runs, the gate stops it.
        token = self.status_json()["confirm_token"]
        code, out, err = self.run_cli("resume-plan", "--run-key", K, "--confirm", token, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        view = self.step(
            "stopped_at_gate", state=["accepted", "accepted", "not_started"],
            next_={"repository": "web", **main, "action": "gate", "answer": "deferred_result"}, gates=["open"],
        )
        self.assertEqual([s["run_key"] for s in view["stages"]], [K, K2, K2])
        (web_run,) = self.runs(self.web)["runs"]
        self.assertEqual(web_run["logical_plan"], K)
        instance = view["gates"][0]["instance_id"]
        code, out, err = self.run_cli("runs", "--repo-root", str(self.web))
        self.assertEqual(code, 0, err)
        self.assertIn("Stage 3 — S3 (web): not_started", out)
        self.assertIn("gate canary — Staging canary: open", out)
        self.assertIn("next: gate in web (main)", out)

        # released with pass: Stage 3 runs; the plan's obligation stops it at the end.
        code, out, err = self.run_cli(
            "resume-plan", "--run-key", K, "--deferred-result", f"{instance}:{GATE}=pass",
            "--repo-root", str(self.repo),
        )
        self.assertEqual(code, 0, out + err)
        self.step(
            "plan_obligation_owed", state=["accepted", "accepted", "accepted"],
            next_={"repository": "web", **main, "action": "gate", "answer": "deferred_result"}, gates=["passed"],
        )
        self.assertFalse(logical_plan.derived_status(self.logical()).complete)

        # The ledger obligation, raised in the home repository, is answered at the end.
        code, out, err = self.run_cli(
            "resume-plan", "--run-key", K, "--deferred-result", f"{CHECK}=pass=legible at 700px",
            "--repo-root", str(self.repo),
        )
        self.assertEqual(code, 0, out + err)
        (answered,) = load_ledger(logical_plan.ledger_path(self.home_common(), K))
        self.assertEqual((answered.instance_id, answered.resolved_by), (carried.instance_id, K2))
        self.step(
            "last_part_complete", state=["accepted", "accepted", "accepted"],
            next_={"repository": "web", **main, "action": "finish", "answer": None}, gates=["passed"],
        )

        # finish the last part: the whole plan is integrated.
        code, out, err = self.from_web("finish-run", "--run-key", K2, "--json")
        self.assertEqual(code, 0, out + err)
        view = self.step(
            "finished", state=["accepted", "accepted", "accepted"], next_=None, gates=["passed"],
        )
        self.assertEqual(view["status"], "complete")
        self.assertEqual([s["run_key"] for s in view["stages"]], [K, K2, K2])

        self.assert_fixture()

    def assert_fixture(self):
        published = {
            "description": (
                "logical_plan.view at each step of tests/test_logical_view.py: one plan, Stage 1 in "
                "'repo', Stages 2-3 in 'web' with a gate before Stage 3. Gate instance ids are "
                "replaced by '<instance>'. The `plans` entries of `runs --json` have this shape."
            ),
            "runs_schema_version": managed_finish.RUNS_SCHEMA_VERSION,
            "next_actions": list(logical_plan.NEXT_ACTIONS),
            "steps": self.views,
        }
        if os.environ.get("SPARRING_UPDATE_FIXTURES"):
            FIXTURE.write_text(json.dumps(published, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(json.loads(FIXTURE.read_text(encoding="utf-8")), published)


class PlanKeysTests(_CrossCase):
    def test_a_single_repository_run_has_no_logical_plan(self):
        self.plan_path.write_text(plan("Build here.", "Build more."), encoding="utf-8")
        _run_git(self.repo, "commit", "-qam", "home plan")
        code, _, err = self.run_cli("run-plan", str(self.plan_path), "--repo-root", str(self.repo), "--managed")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("runs", "--json", "--repo-root", str(self.repo))
        payload = json.loads(out)
        self.assertEqual(payload["plans"], [])
        self.assertEqual([run["logical_plan"] for run in payload["runs"]], [None])

    def test_an_unreadable_plan_is_reported_not_hidden(self):
        code, _, err = self.start()
        self.assertEqual(code, 0, err)
        path = logical_plan.record_path(self.home_common(), K)
        path.write_text("{}", encoding="utf-8")
        code, out, err = self.run_cli("runs", "--json", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        (entry,) = json.loads(out)["plans"]
        self.assertEqual((entry["logical_key"], entry["error"]["code"]), (K, "record_schema_unknown"))


class ReviewerPauseTests(_CrossCase):
    """A reviewer's NEEDS_YOU in a later part is a gate answered with
    evidence -- not a plain resume, and not a deferred result."""

    verdicts = [READY, NEEDS_YOU, READY]

    def next_(self) -> dict:
        (view,) = json.loads(self.run_cli("runs", "--json", "--repo-root", str(self.web))[1])["plans"]
        return view["next"]

    def test_needs_you_is_a_gate_released_by_evidence(self):
        self.assertEqual(self.start()[0], 0)
        self.finish_first()
        token = self.status_json()["confirm_token"]
        code, out, err = self.run_cli("resume-plan", "--run-key", K, "--confirm", token, "--repo-root", str(self.repo))
        self.assertEqual(code, 0, out + err)
        expected = {"repository": "web", "target_branch": "main", "action": "gate", "answer": "evidence"}
        self.assertEqual(self.next_(), expected)

        # A plain resume keeps the pause, and the view says so still.
        self.run_cli("resume-plan", "--run-key", K, "--repo-root", str(self.repo))
        self.assertEqual(self.next_(), expected)

        code, out, err = self.run_cli(
            "resume-plan", "--run-key", K, "--evidence", "checked by hand", "--repo-root", str(self.repo)
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(
            self.next_(), {"repository": "web", "target_branch": "main", "action": "finish", "answer": None}
        )


class SnapshotTests(_CrossCase):
    """The view shows only the snapshot the record names; anything else is
    a structured error for that plan, never a crashed listing."""

    def setUp(self):
        super().setUp()
        self.plan_path.write_text(GATED_CROSS, encoding="utf-8")
        _run_git(self.repo, "commit", "-qam", "gated cross plan")
        self.assertEqual(self.start()[0], 0)
        self.snapshot = logical_plan.snapshot_path(self.home_common(), self.logical())

    def plan_error(self) -> dict:
        code, out, err = self.run_cli("runs", "--json", "--repo-root", str(self.repo))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload["runs"]), 1)  # the rest of the listing stands
        (entry,) = payload["plans"]
        self.assertEqual(set(entry), {"logical_key", "error"})
        return entry["error"]

    def test_changed_gate_text_is_refused(self):
        self.snapshot.write_text(GATED_CROSS.replace("canary = true", "canary = false"), encoding="utf-8")
        self.assertEqual(self.plan_error()["code"], "snapshot_mismatch")

    def test_a_snapshot_missing_a_stage_is_refused_even_with_a_matching_hash(self):
        import hashlib

        text = plan("Build here.", "Repository: web\n\nBuild there.")
        self.snapshot.write_text(text, encoding="utf-8")
        # Even a record naming its bytes does not make it this plan's input.
        path = logical_plan.record_path(self.home_common(), K)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["input"]["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.plan_error()["code"], "logical_digest_mismatch")

    def test_a_missing_snapshot_is_refused(self):
        self.snapshot.unlink()
        self.assertEqual(self.plan_error()["code"], "snapshot_missing")
