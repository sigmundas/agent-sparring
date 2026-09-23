"""The reviewer side conversation: what it refuses, and what it must not touch.

A dialogue turn is the one provider turn a person triggers directly, into a
thread that holds a recorded verdict. Almost all of its risk is in what it
could accidentally change, so most of these tests assert that something
did *not* happen: no state written, no verdict parsed, no session id moved,
no repository write tolerated. The rest pin the refusals, because a refusal
that silently becomes a no-op is how a person ends up believing they asked
the reviewer something they never asked.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import conftest_path  # noqa: F401

from agent_sparring.concurrency import worktree_lock
from agent_sparring.dialogue import DialogueError, run_dialogue_turn
from agent_sparring.dialogue_prompt import TURN_DIALOGUE, assemble_dialogue_prompt
from agent_sparring.human_gate import HumanCheck, HumanGate
from agent_sparring.providers import ProviderError, SparringAgentResult
from agent_sparring.routing import RoutingAction, RoutingResult
from agent_sparring.sparring_exchange import record_sparring_result
from agent_sparring.stage import Stage

SESSION = "thread-1"


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class _Adapter:
    """Answers with prose, and records what it was asked."""

    def __init__(self, *, text="Here is the evidence.", session_id=SESSION, raises=None):
        self.resume_calls: list[tuple[str, str]] = []
        self.start_calls: list[str] = []
        self._text = text
        self._session_id = session_id
        self._raises = raises

    def start(self, prompt: str) -> SparringAgentResult:  # pragma: no cover
        self.start_calls.append(prompt)
        raise AssertionError("a dialogue must never start a new reviewer session")

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:  # pragma: no cover
        raise AssertionError(
            "a dialogue must use converse: resume imposes the verdict output "
            "schema, which forces a JSON answer whatever the prompt says"
        )

    def converse(self, session_id: str, prompt: str) -> SparringAgentResult:
        self.resume_calls.append((session_id, prompt))
        if self._raises is not None:
            raise self._raises
        return SparringAgentResult(
            session_id=self._session_id, text=self._text, is_error=False
        )


class _WritingAdapter:
    """A reviewer that writes to the repository during a read-only turn."""

    def __init__(self, repo: Path):
        self.repo = repo

    def start(self, prompt: str) -> SparringAgentResult:  # pragma: no cover
        raise AssertionError("unexpected start")

    def resume(self, session_id: str, prompt: str) -> SparringAgentResult:  # pragma: no cover
        raise AssertionError("unexpected resume")

    def converse(self, session_id: str, prompt: str) -> SparringAgentResult:
        (self.repo / "sneaky.txt").write_text("written by a reviewer\n", encoding="utf-8")
        return SparringAgentResult(session_id=session_id, text="done", is_error=False)


GATE = HumanGate(
    category="OTHER",
    title="Confirm the cross-source bridge",
    checks=(
        HumanCheck(
            id="review-nortaxa-53482-bridge",
            instruction="Confirm NorTaxa 53482 maps to COL 39ZCL.",
            pass_criteria="Pass when the reciprocal synonym evidence holds.",
            source="docs/plans/active/x.md#Stage 3",
        ),
        HumanCheck(
            id="other-check",
            instruction="Do the other thing.",
            pass_criteria="Pass when it is done.",
        ),
    ),
)


class _DialogueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "config", "user.email", "test@example.com")
        _run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "f.txt").write_text("hi\n", encoding="utf-8")
        _run_git(self.repo, "add", "f.txt")
        _run_git(self.repo, "commit", "-q", "-m", "base")

        self.sparring_dir = self.repo / ".sparring"
        self.stage = Stage.resolve(self.sparring_dir, "stage-1").create()
        (self.stage.directory / "brief.md").write_text("# brief\n", encoding="utf-8")
        self._set_session(SESSION)

    def _set_session(self, session_id):
        state = self.stage.read_state()
        state.sparring_session_id = session_id
        self.stage.write_state(state)

    def _record_gate(self):
        record_sparring_result(
            self.stage,
            RoutingResult(
                action=RoutingAction.NEEDS_YOU,
                summary="needs your judgement",
                needs_you_reason="a taxonomic decision",
                human_gate=GATE,
            ),
            findings="the findings",
        )

    def _ask(self, adapter, **kwargs):
        kwargs.setdefault("message", "Show me the evidence.")
        return run_dialogue_turn(self.stage, self.repo, adapter, **kwargs)


class RefusalTest(_DialogueTestCase):
    def test_no_reviewer_session_is_refused_not_started(self):
        self._set_session(None)
        adapter = _Adapter()
        with self.assertRaises(DialogueError) as caught:
            self._ask(adapter)
        self.assertIn("no recorded sparring session", str(caught.exception))
        self.assertEqual(adapter.start_calls, [])
        self.assertEqual(adapter.resume_calls, [])

    def test_empty_question_is_refused_before_the_provider(self):
        adapter = _Adapter()
        with self.assertRaises(DialogueError):
            self._ask(adapter, message="   ")
        self.assertEqual(adapter.resume_calls, [])

    def test_a_turn_in_flight_refuses_with_an_explanation(self):
        adapter = _Adapter()
        with worktree_lock(self.repo):
            with self.assertRaises(DialogueError) as caught:
                self._ask(adapter)
        message = str(caught.exception)
        self.assertIn("locked", message)
        self.assertIn("being edited", message)
        self.assertEqual(adapter.resume_calls, [])

    def test_unknown_check_id_lists_the_ids_that_exist(self):
        self._record_gate()
        adapter = _Adapter()
        with self.assertRaises(DialogueError) as caught:
            self._ask(adapter, check_id="not-a-check")
        message = str(caught.exception)
        self.assertIn("unknown check id", message)
        self.assertIn("review-nortaxa-53482-bridge", message)
        self.assertEqual(adapter.resume_calls, [])

    def test_a_different_session_id_discards_the_reply(self):
        adapter = _Adapter(session_id="someone-else")
        with self.assertRaises(DialogueError) as caught:
            self._ask(adapter)
        self.assertIn("someone-else", str(caught.exception))
        # And the recorded session id is untouched.
        self.assertEqual(self.stage.read_state().sparring_session_id, SESSION)
        self.assertEqual(self.stage.read_dialogue(), ())

    def test_a_repository_write_is_caught_and_reported(self):
        with self.assertRaises(DialogueError) as caught:
            self._ask(_WritingAdapter(self.repo))
        self.assertIn("modified the repository", str(caught.exception))
        self.assertEqual(self.stage.read_dialogue(), ())

    def test_a_provider_failure_records_nothing(self):
        with self.assertRaises(DialogueError):
            self._ask(_Adapter(raises=ProviderError("boom")))
        self.assertEqual(self.stage.read_dialogue(), ())


class NonMutationTest(_DialogueTestCase):
    """The heart of it: a conversation changes nothing about the run."""

    def _snapshot(self):
        files = {}
        for name in ("state.json", "sparring.md", "handoff.md", "notes.md", "brief.md"):
            path = self.stage.directory / name
            files[name] = path.read_bytes() if path.is_file() else None
        return files

    def test_no_stage_artifact_changes(self):
        self._record_gate()
        before = self._snapshot()
        self._ask(_Adapter())
        self.assertEqual(self._snapshot(), before)

    def test_a_json_reply_is_passed_through_as_prose(self):
        # The thread's last instruction was "emit a verdict", so a reviewer
        # may answer in verdict shape out of habit. It is an answer, not a
        # routing decision, and must not be treated as one.
        verdict_shaped = json.dumps({"action": "READY", "summary": "fine"})
        self._record_gate()
        sparring_before = self.stage.read_sparring()
        turn = self._ask(_Adapter(text=verdict_shaped))
        self.assertEqual(turn.answer, verdict_shaped)
        self.assertEqual(self.stage.read_sparring(), sparring_before)

    def test_the_worktree_stays_clean(self):
        self._ask(_Adapter())
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--short"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # The dialogue transcript and captured prompt live under .sparring,
        # which the acceptance allowlist exempts; nothing else appears.
        self.assertNotIn("sneaky", status)


class TranscriptTest(_DialogueTestCase):
    def test_an_exchange_is_appended_with_its_provenance(self):
        self._record_gate()
        self._ask(
            _Adapter(text="Because of the seven shared synonyms."),
            message="Why 39ZCL?",
            check_id="review-nortaxa-53482-bridge",
            gate_instance="f0c451",
        )
        records = self.stage.read_dialogue()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["question"], "Why 39ZCL?")
        self.assertEqual(record["answer"], "Because of the seven shared synonyms.")
        self.assertEqual(record["check_id"], "review-nortaxa-53482-bridge")
        self.assertEqual(record["gate_instance"], "f0c451")
        self.assertEqual(record["session_id"], SESSION)
        self.assertIsInstance(record["duration_ms"], int)

    def test_exchanges_accumulate_in_order(self):
        self._ask(_Adapter(text="first"), message="one")
        self._ask(_Adapter(text="second"), message="two")
        records = self.stage.read_dialogue()
        self.assertEqual([r["question"] for r in records], ["one", "two"])

    def test_a_damaged_line_does_not_hide_the_rest(self):
        self._ask(_Adapter(text="kept"), message="one")
        with self.stage.dialogue_path().open("a", encoding="utf-8") as handle:
            handle.write("{truncated\n")
        self.assertEqual(len(self.stage.read_dialogue()), 1)


class PromptTest(_DialogueTestCase):
    def test_the_prompt_forbids_a_verdict_and_says_it_decides_nothing(self):
        prompt = assemble_dialogue_prompt(self.stage, message="why?").text
        self.assertIn("Do not emit a routing verdict", prompt)
        self.assertIn("cannot change the verdict", prompt)
        self.assertIn("read-only", prompt)

    def test_a_named_check_is_quoted_verbatim(self):
        self._record_gate()
        adapter = _Adapter()
        self._ask(adapter, check_id="review-nortaxa-53482-bridge")
        sent = adapter.resume_calls[0][1]
        self.assertIn("Confirm NorTaxa 53482 maps to COL 39ZCL.", sent)
        self.assertIn("Pass when the reciprocal synonym evidence holds.", sent)
        self.assertIn("review-nortaxa-53482-bridge", sent)

    def test_the_prompt_stays_short_because_the_thread_has_the_context(self):
        # Re-sending brief/handoff/candidate would pay twice for what the
        # resumed thread already holds.
        self._record_gate()
        adapter = _Adapter()
        self._ask(adapter, message="why?")
        sent = adapter.resume_calls[0][1]
        self.assertNotIn("## Stage brief", sent)
        self.assertNotIn("## Handoff", sent)

    def test_the_captured_prompt_is_filed_as_its_own_turn_kind(self):
        self._ask(_Adapter())
        names = [p.name for p in (self.stage.directory / "prompts").iterdir()]
        self.assertTrue(
            any(name.endswith(f"-sparrer-{TURN_DIALOGUE}.md") for name in names), names
        )


class ActivityTest(_DialogueTestCase):
    def _events(self):
        path = self.stage.activity_path()
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_a_turn_emits_started_and_finished_with_a_duration(self):
        self._ask(_Adapter(), activity=self.stage.activity_log().bind("sparrer"))
        names = [f"{e['actor']}:{e['event']}" for e in self._events()]
        self.assertEqual(names, ["sparrer:dialogue.started", "sparrer:dialogue.finished"])
        finished = self._events()[-1]
        self.assertIsInstance(finished["duration_ms"], int)
        self.assertEqual(finished["session_id"], SESSION)

    def test_a_failure_is_emitted_with_a_fixed_phrase(self):
        with self.assertRaises(DialogueError):
            self._ask(
                _Adapter(raises=ProviderError("boom")),
                activity=self.stage.activity_log().bind("sparrer"),
            )
        last = self._events()[-1]
        self.assertEqual(last["event"], "dialogue.failed")
        self.assertEqual(last["summary"], "provider error")
        # Never the provider's own text, which can carry its stdout.
        self.assertNotIn("boom", json.dumps(last))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
