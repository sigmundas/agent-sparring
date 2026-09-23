"""The usage report: what a stage's provider turns used, and what it refuses
to invent.

Two properties matter more than the formatting. First, the report is a
quotation: a number no provider reported is absent rather than zero, and the
model the engine asked for stays distinguishable from the model the provider
said it ran. Second, it is telemetry-grade input: a missing, truncated or
partly corrupt log must produce a partial report and never an exception,
because ``activity.jsonl`` is explicitly allowed to be any of those.
"""

import json
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.stage import ACTIVITY_FILENAME, Stage
from agent_sparring.usage import (
    collect_stage_usage,
    format_duration,
    render_stage_usage,
)

STAGE_ID = "demo-stage"


def _line(actor, event, **fields):
    record = {"v": 1, "actor": actor, "event": event}
    record.update(fields)
    return json.dumps(record)


class _StageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sparring_dir = Path(self._tmp.name) / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, STAGE_ID)
        self.stage.directory.mkdir(parents=True)

    def write_log(self, *lines):
        (self.stage.directory / ACTIVITY_FILENAME).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )


class CollectTest(_StageTestCase):
    def test_absent_log_is_reported_not_raised(self):
        usage = collect_stage_usage(self.stage)
        self.assertFalse(usage.log_present)
        self.assertEqual(usage.turns, [])
        self.assertIn("no activity log", render_stage_usage(usage))

    def test_resolution_event_carries_values_and_their_sources(self):
        self.write_log(
            _line(
                "sparrer",
                "agents.resolved",
                role="sparring",
                provider="codex-cli",
                provider_source="project",
                requested_model="gpt-6-astra",
                model_source="env",
                requested_effort="medium",
                effort_source="project",
            )
        )
        role = collect_stage_usage(self.stage).roles["sparring"]
        self.assertTrue(role.resolved_seen)
        self.assertEqual(role.requested_model, "gpt-6-astra")
        self.assertEqual(role.model_source, "env")
        self.assertEqual(role.requested_effort, "medium")
        self.assertEqual(role.provider, "codex-cli")

    def test_requested_and_reported_model_stay_distinct(self):
        self.write_log(
            _line(
                "sparrer",
                "agents.resolved",
                role="sparring",
                provider="codex-cli",
                requested_model="gpt-6-astra",
                model_source="env",
            ),
            _line("sparrer", "provider.usage", provider="codex-cli", model="gpt-5.6-sol"),
        )
        usage = collect_stage_usage(self.stage)
        role = usage.roles["sparring"]
        self.assertEqual(role.requested_model, "gpt-6-astra")
        self.assertEqual(role.reported_model, "gpt-5.6-sol")
        # A disagreement is surfaced rather than reconciled.
        self.assertIn("provider reported model gpt-5.6-sol", render_stage_usage(usage))

    def test_unreported_tokens_are_none_not_zero(self):
        self.write_log(_line("sparrer", "provider.usage", input_tokens=10))
        role = collect_stage_usage(self.stage).roles["sparring"]
        self.assertEqual(role.input_tokens, 10)
        self.assertIsNone(role.output_tokens)
        self.assertIsNone(role.total_tokens)
        self.assertIn("out -", render_stage_usage(collect_stage_usage(self.stage)))

    def test_a_later_usage_event_does_not_reset_an_omitted_field(self):
        self.write_log(
            _line("sparrer", "provider.usage", input_tokens=10, output_tokens=4),
            _line("sparrer", "provider.usage", input_tokens=20),
        )
        role = collect_stage_usage(self.stage).roles["sparring"]
        self.assertEqual(role.input_tokens, 20)
        self.assertEqual(role.output_tokens, 4)

    def test_measured_duration_is_used_verbatim(self):
        self.write_log(
            _line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z", resumed=False),
            _line("stage", "turn.finished", ts="2026-09-22T10:05:00.000Z", duration_ms=4242),
        )
        turn = collect_stage_usage(self.stage).turns[0]
        self.assertEqual(turn.duration_ms, 4242)
        self.assertFalse(turn.duration_derived)

    def test_duration_is_derived_and_marked_when_the_log_predates_measurement(self):
        self.write_log(
            _line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z", resumed=False),
            _line("stage", "turn.finished", ts="2026-09-22T10:05:00.000Z"),
        )
        usage = collect_stage_usage(self.stage)
        turn = usage.turns[0]
        self.assertEqual(turn.duration_ms, 300_000)
        self.assertTrue(turn.duration_derived)
        rendered = render_stage_usage(usage)
        self.assertIn("~5m 00s", rendered)
        self.assertIn("derived from event timestamps", rendered)

    def test_a_backwards_clock_yields_no_duration(self):
        self.write_log(
            _line("stage", "turn.started", ts="2026-09-22T10:05:00.000Z"),
            _line("stage", "turn.finished", ts="2026-09-22T10:00:00.000Z"),
        )
        turn = collect_stage_usage(self.stage).turns[0]
        self.assertIsNone(turn.duration_ms)

    def test_a_failed_turn_keeps_its_outcome(self):
        self.write_log(
            _line("sparrer", "sparring.started", ts="2026-09-22T10:00:00.000Z"),
            _line(
                "sparrer",
                "sparring.failed",
                ts="2026-09-22T10:00:30.000Z",
                duration_ms=30_000,
                summary="provider error",
            ),
        )
        turn = collect_stage_usage(self.stage).turns[0]
        self.assertEqual(turn.outcome, "failed")
        self.assertEqual(turn.summary, "provider error")

    def test_a_verdict_reports_its_action(self):
        self.write_log(
            _line("sparrer", "sparring.started", ts="2026-09-22T10:00:00.000Z"),
            _line("sparrer", "verdict", ts="2026-09-22T10:01:00.000Z", action="SEND_BACK"),
        )
        usage = collect_stage_usage(self.stage)
        self.assertEqual(usage.turns[0].action, "SEND_BACK")
        self.assertIn("verdict: SEND_BACK", render_stage_usage(usage))

    def test_an_unfinished_turn_is_shown_as_unfinished(self):
        self.write_log(_line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z"))
        usage = collect_stage_usage(self.stage)
        self.assertIsNone(usage.turns[0].outcome)
        self.assertIn("in progress / unfinished", render_stage_usage(usage))

    def test_an_end_without_a_start_still_counts_as_a_turn(self):
        self.write_log(_line("stage", "turn.finished", ts="2026-09-22T10:00:00.000Z"))
        usage = collect_stage_usage(self.stage)
        self.assertEqual(len(usage.turns), 1)
        self.assertEqual(usage.turns[0].outcome, "finished")

    def test_corrupt_lines_are_counted_and_skipped(self):
        self.write_log(
            "{not json",
            "[]",
            _line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z"),
            _line("stage", "turn.finished", ts="2026-09-22T10:00:01.000Z", duration_ms=1000),
        )
        usage = collect_stage_usage(self.stage)
        self.assertEqual(usage.unreadable_lines, 2)
        self.assertEqual(len(usage.turns), 1)
        self.assertIn("unreadable log line", render_stage_usage(usage))

    def test_a_truncated_final_line_does_not_raise(self):
        (self.stage.directory / ACTIVITY_FILENAME).write_text(
            _line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z")
            + "\n"
            + '{"v":1,"actor":"stage","ev',
            encoding="utf-8",
        )
        usage = collect_stage_usage(self.stage)
        self.assertEqual(usage.unreadable_lines, 1)
        self.assertEqual(len(usage.turns), 1)

    def test_provider_is_recovered_from_ordinary_events(self):
        # A log written before agents.resolved existed still says which
        # provider ran, because every adapter binds its id onto each event.
        self.write_log(_line("sparrer", "provider.usage", provider="codex-cli"))
        role = collect_stage_usage(self.stage).roles["sparring"]
        self.assertEqual(role.provider, "codex-cli")
        # ...but the source genuinely is not recoverable, and is not guessed.
        self.assertIsNone(role.provider_source)
        self.assertFalse(role.resolved_seen)

    def test_an_unresolved_role_shows_no_model_rather_than_provider_default(self):
        self.write_log(_line("sparrer", "provider.usage", provider="codex-cli"))
        rendered = render_stage_usage(collect_stage_usage(self.stage))
        self.assertNotIn("provider default", rendered)

    def test_json_payload_is_serialisable(self):
        self.write_log(
            _line("stage", "turn.started", ts="2026-09-22T10:00:00.000Z"),
            _line("stage", "turn.finished", ts="2026-09-22T10:00:01.000Z", duration_ms=1000),
        )
        payload = collect_stage_usage(self.stage).as_dict()
        json.dumps(payload)  # must not raise
        self.assertEqual(payload["stage_id"], STAGE_ID)
        self.assertEqual(payload["turns"][0]["duration_ms"], 1000)


class FormatDurationTest(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(format_duration(None), "-")
        self.assertEqual(format_duration(4242), "4.2s")
        self.assertEqual(format_duration(187_000), "3m 07s")
        self.assertEqual(format_duration(3_840_000), "1h 04m")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
