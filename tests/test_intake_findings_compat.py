"""Sealed intakes survive engine findings added after they were prepared.

Approval re-renders ``report.md`` and compares it byte for byte with what the
person reviewed. An engine check added later (``automatable_human_gate``)
would change that render for every intake sealed before it, so a plan already
part-way through -- earlier slices approved and completed under that intake --
could never approve its next slice. ``findings_version`` records which checks
an intake was prepared with, and approval recomputes exactly those.

The fixture reproduces the ingestion-provenance intake that hit this: a
compiled, two-repository plan whose manual gate between slices carries the
plan's real G1 wording, prepared before the check existed, with its first
slice approved and completed before the dependent slice is approved.
"""

import json
from unittest import mock

import conftest_path  # noqa: F401

from agent_sparring import intake as intake_module
from agent_sparring.intake import (
    FINDINGS_VERSION,
    IntakeError,
    SourcePlan,
    check_interpretation,
    parse_interpretation,
)
from agent_sparring.plan import PlanRunState, PlanRunStatus, run_state_path
from test_intake import PLAN, _G1_TEXT, _git, good_interpretation, lines
from test_intake_sealing import APP_BRANCH, _head, _Sealed

_RELEASE = "Activate the release in production after Stage 1A, with explicit go-ahead."
G1_PLAN = PLAN.replace(_RELEASE, _G1_TEXT)


def g1_interpretation() -> dict:
    payload = good_interpretation()
    gate = payload["gates"][0]
    gate.update(kind="manual", title="API and access checks", ranges=[lines("## Release", "Kind: manual", text=G1_PLAN)])
    return payload


class SealedFindingsCompatTests(_Sealed):
    def setUp(self):
        super().setUp()
        self.plan.write_text(G1_PLAN, encoding="utf-8")
        _git(self.repo, "add", "docs/plan.md")
        _git(self.repo, "commit", "-q", "-m", "plan: G1 manual gate")
        _git(self.repo, "push", "-q", "origin", APP_BRANCH)

    def _record(self, intake):
        return json.loads((intake.directory / "intake.json").read_text(encoding="utf-8"))

    def _write_record(self, intake, record):
        (intake.directory / "intake.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def prepare_legacy(self):
        """Prepare as the engine did before ``automatable_human_gate``
        existed: findings version 1, and no ``findings_version`` key."""

        with mock.patch.object(intake_module, "FINDINGS_VERSION", 1):
            intake = self.prepare(g1_interpretation())
        record = self._record(intake)
        self.assertEqual(record.pop("findings_version"), 1)
        self._write_record(intake, record)
        return intake

    def approve_web(self, intake):
        return self.approve(intake.directory, run="web", confirmed_prerequisites=["release"])

    def test_the_fixture_reproduces_the_drift(self):
        # The current check flags this gate, and the legacy report omits it.
        intake = self.prepare_legacy()
        report = (intake.directory / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("automatable_human_gate", report)
        interpretation = parse_interpretation(json.dumps(g1_interpretation()))
        current = check_interpretation(
            interpretation, SourcePlan(label="docs/plan.md", text=G1_PLAN), mode="faithful"
        )
        self.assertIn("automatable_human_gate", [f.code for f in current])

    def test_a_legacy_intake_approves_its_dependent_slice_after_the_earlier_one_completed(self):
        intake = self.prepare_legacy()
        record_before = self._record(intake)
        report_before = (intake.directory / "report.md").read_bytes()

        app = self.approve(intake.directory)
        result, _ = self.run_app(app)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        accepted = _head(self.repo)

        web = self.approve_web(intake)
        self.assertTrue(web.created)

        # Nothing the person reviewed or the earlier slice proved was rewritten.
        self.assertEqual(self._record(intake), record_before)
        self.assertEqual((intake.directory / "report.md").read_bytes(), report_before)
        approval = json.loads(web.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(approval["report_digest"], record_before["report_digest"])
        self.assertEqual(web.run_key, record_before["run_keys"]["web"])
        self.assertEqual(app.run_key, record_before["run_keys"]["app"])
        (evidence,) = approval["prerequisites"]["earlier_slices"]
        self.assertEqual(evidence["run_id"], "app")
        self.assertEqual(evidence["final_candidate_sha"], accepted)
        self.assertEqual(evidence["approval_sha256"], self.source(app).approval.sha256)
        self.assertEqual(approval["repositories"]["at_approval"]["app"]["head"], accepted)
        state = PlanRunState.load(run_state_path(self.sparring_dir, app.run_key))
        self.assertIs(state.status, PlanRunStatus.COMPLETE)

    def test_a_new_intake_records_the_version_and_reports_the_finding(self):
        intake = self.prepare(g1_interpretation())
        self.assertEqual(self._record(intake)["findings_version"], FINDINGS_VERSION)
        report = (intake.directory / "report.md").read_text(encoding="utf-8")
        self.assertIn("`automatable_human_gate` [engine]", report)
        self.assertTrue(self.approve(intake.directory).created)

    def test_a_new_intake_downgraded_to_hide_the_finding_is_refused(self):
        # Removing the key does not let a new report pass as legacy: the
        # legacy render lacks the finding the reviewed report contains.
        intake = self.prepare(g1_interpretation())
        record = self._record(intake)
        del record["findings_version"]
        self._write_record(intake, record)
        with self.assertRaisesRegex(IntakeError, "is not the report this intake renders"):
            self.approve(intake.directory)

    def test_a_legacy_intake_claiming_the_new_version_is_refused(self):
        intake = self.prepare_legacy()
        record = self._record(intake)
        record["findings_version"] = FINDINGS_VERSION
        self._write_record(intake, record)
        with self.assertRaisesRegex(IntakeError, "is not the report this intake renders"):
            self.approve(intake.directory)

    def test_an_unsupported_findings_version_is_refused(self):
        intake = self.prepare_legacy()
        for value in (0, FINDINGS_VERSION + 1, "2", True):
            with self.subTest(value=value):
                record = self._record(intake)
                record["findings_version"] = value
                self._write_record(intake, record)
                with self.assertRaisesRegex(IntakeError, "unsupported findings version"):
                    self.approve(intake.directory)

    def test_an_edited_legacy_report_is_refused(self):
        intake = self.prepare_legacy()
        path = intake.directory / "report.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "is not the report this intake renders"):
            self.approve(intake.directory)

    def test_corrupted_earlier_slice_evidence_is_still_refused(self):
        intake = self.prepare_legacy()
        app = self.approve(intake.directory)
        result, _ = self.run_app(app)
        self.assertIs(result.status, PlanRunStatus.COMPLETE)
        approval_path = app.approval_path
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["run_key"] = "forged-run-key"
        approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(IntakeError, "earlier run slice 'app'"):
            self.approve_web(intake)
