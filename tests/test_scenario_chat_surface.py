"""Scenario tests for Chat Surface v1 — Phase 4 DoD.

Multi-turn mixed conversations exercising the full pipeline:
talk turns, search→summarize→refine→write, confirmation flow,
failure+recovery, presenter formatting.

Key assertions:
- No re-parse on confirm (PendingConfirmation round-trip)
- Deterministic anchor binding across turns
- Proper TalkIntent logging (effect=NONE, __talk__ skill)
- Presenter formatting in both default and verbose modes
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from kavi.agent.core import confirm_pending, handle_message
from kavi.agent.models import (
    SearchAndSummarizeIntent,
    SessionContext,
    SkillInvocationIntent,
    TalkIntent,
    WriteNoteIntent,
)
from kavi.agent.presenter import present
from tests.test_agent_chat_v0 import (
    FAKE_REGISTRY,
    _ctx,
)


class TestScenarioSearchSummarizeRefineWrite:
    """10-turn conversation: greet → search → summarize → refine → write.

    Exercises talk, skill execution, ref resolution, transform,
    confirmation, and session accumulation.
    """

    def test_full_conversation(self) -> None:
        session = SessionContext()

        # Turn 1: Greeting (TalkIntent)
        with _ctx(talk_return="Hello! How can I help you today?"):
            r1 = handle_message(
                "hello",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r1.intent, TalkIntent)
        assert r1.error is None
        assert len(r1.records) == 1
        assert r1.records[0].skill_name == "__talk__"
        assert r1.records[0].side_effect_class == "NONE"
        session = r1.session or session

        # Turn 2: Search (SearchAndSummarizeIntent → chain)
        with _ctx():
            r2 = handle_message(
                "search machine learning",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r2.intent, SearchAndSummarizeIntent)
        assert r2.error is None
        assert len(r2.records) == 2
        assert r2.records[0].skill_name == "search_notes"
        assert r2.records[1].skill_name == "summarize_note"
        session = r2.session or session

        # Verify session has anchors from both talk and search
        assert len(session.anchors) >= 2
        skill_names = [a.skill_name for a in session.anchors]
        assert "__talk__" in skill_names
        assert "search_notes" in skill_names

        # Turn 3: Summarize a specific path (SkillInvocationIntent)
        with _ctx():
            r3 = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r3.intent, SkillInvocationIntent)
        assert r3.records[0].skill_name == "summarize_note"
        assert r3.records[0].success
        session = r3.session or session

        # Turn 4: Refine with style override (TransformIntent → resolved)
        with _ctx():
            r4 = handle_message(
                "but paragraph",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        # TransformIntent gets resolved to SkillInvocationIntent
        assert isinstance(r4.intent, SkillInvocationIntent)
        assert r4.records[0].skill_name == "summarize_note"
        # Style should be "paragraph" from the override
        assert r4.records[0].input_json.get("style") == "paragraph"
        session = r4.session or session

        # Turn 5: Conversational follow-up (TalkIntent)
        with _ctx(talk_return="That's a great summary!"):
            r5 = handle_message(
                "looks good",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r5.intent, TalkIntent)
        assert r5.records[0].output_json["response"] == "That's a great summary!"
        session = r5.session or session

        # Turn 6: Write note (needs confirmation)
        with _ctx():
            r6 = handle_message(
                "write ML Summary\nKey points from machine learning notes.",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r6.intent, WriteNoteIntent)
        assert r6.needs_confirmation is True
        assert r6.pending is not None
        assert r6.records == []  # Not executed yet

        # Verify pending has the right plan
        assert r6.pending.plan.skill_name == "write_note"
        assert r6.pending.intent == r6.intent

        # Turn 7: Confirm the write (via confirm_pending — no re-parse)
        with _ctx(), patch(
            "kavi.agent.parser.parse_intent",
            side_effect=AssertionError("parse_intent must not be called"),
        ):
            r7 = confirm_pending(
                r6.pending,
                registry_path=FAKE_REGISTRY,
            )
        assert r7.error is None
        assert len(r7.records) == 1
        assert r7.records[0].skill_name == "write_note"
        assert r7.records[0].success
        assert r7.records[0].input_json["title"] == "ML Summary"

        # Verify session captures the write
        if r7.session is not None:
            session = r7.session
        assert any(
            a.skill_name == "write_note" for a in session.anchors
        )

        # Turn 8: Another talk turn
        with _ctx(talk_return="You're welcome!"):
            r8 = handle_message(
                "thanks",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r8.intent, TalkIntent)
        session = r8.session or session

        # Final session state: should have anchors from all turns
        assert len(session.anchors) >= 5


class TestScenarioConfirmationStashRoundtrip:
    """Verify confirmation stash preserves exact plan — no re-parse."""

    def test_stash_preserves_bound_anchors(self) -> None:
        """Pending captures session snapshot, confirmed execution uses it."""
        session = SessionContext()

        # Step 1: search to populate session
        with _ctx():
            r1 = handle_message(
                "search python",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = r1.session or session
        assert len(session.anchors) >= 1

        # Step 2: write (needs confirmation) — session is captured
        with _ctx():
            r2 = handle_message(
                "write Python Notes\nNotes about Python.",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert r2.pending is not None
        assert r2.pending.session is session

        # Step 3: confirm — uses the stashed session, not a new one
        with _ctx():
            r3 = confirm_pending(r2.pending, registry_path=FAKE_REGISTRY)
        assert r3.error is None
        assert r3.records[0].success
        # Session from confirm should extend the stashed session
        if r3.session is not None:
            assert len(r3.session.anchors) >= len(session.anchors)

    def test_expired_stash_rejected(self) -> None:
        """Expired PendingConfirmation returns error, not execution."""
        from datetime import datetime, timedelta

        from kavi.agent.models import CONFIRMATION_TTL_SECONDS, PendingConfirmation, SkillAction

        old = datetime.now(tz=__import__("datetime").UTC) - timedelta(
            seconds=CONFIRMATION_TTL_SECONDS + 10,
        )
        pending = PendingConfirmation(
            plan=SkillAction(
                skill_name="write_note",
                input={"path": "a.md", "title": "T", "body": "b"},
            ),
            intent=WriteNoteIntent(title="T", body="b"),
            created_at=old,
        )
        with _ctx():
            resp = confirm_pending(pending, registry_path=FAKE_REGISTRY)
        assert resp.error is not None
        assert "expired" in resp.error.lower()
        assert resp.records == []


class TestScenarioFailureRecovery:
    """Failure in one turn doesn't break subsequent turns."""

    def test_error_then_success(self) -> None:
        session = SessionContext()

        # Turn 1: Search succeeds
        with _ctx():
            r1 = handle_message(
                "search quantum",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert r1.error is None
        session = r1.session or session

        # Turn 2: Summarize a non-existent path (skill raises)
        with _ctx(), patch(
            "kavi.consumer.shim.load_skill",
            side_effect=KeyError("missing_skill"),
        ):
            r2 = handle_message(
                "summarize nonexistent.md",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        # Error captured in records, not raised
        assert r2.records[0].success is False
        # Session still available (from r1)
        if r2.session is not None:
            session = r2.session

        # Turn 3: Recovery — search still works
        with _ctx():
            r3 = handle_message(
                "search physics",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert r3.error is None
        assert len(r3.records) == 2  # search + summarize chain
        session = r3.session or session

        # Turn 4: Talk still works after error
        with _ctx(talk_return="No worries!"):
            r4 = handle_message(
                "that's fine",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert isinstance(r4.intent, TalkIntent)
        assert r4.error is None


class TestScenarioPresenterFormatting:
    """Presenter formats multi-turn results correctly in both modes."""

    def test_conversational_mode_hides_mechanics(self) -> None:
        """Default mode shows natural language, not JSON or intent kinds."""
        with _ctx(talk_return="Hi! I can help with notes."):
            resp = handle_message(
                "hello",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        out = present(resp)
        assert "Hi! I can help with notes." in out
        # Should NOT contain internal details
        assert '"kind"' not in out
        assert "__talk__" not in out
        assert "Intent:" not in out

    def test_verbose_mode_exposes_all(self) -> None:
        """Verbose mode shows intent, plan, records, timing."""
        with _ctx():
            resp = handle_message(
                "search quantum computing",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        out = present(resp, verbose=True)
        assert "Intent:" in out
        assert "search_and_summarize" in out
        assert "Plan:" in out
        assert "Records" in out
        assert "timing:" in out

    def test_confirmation_conversational(self) -> None:
        """Confirmation messages read naturally in default mode."""
        with _ctx():
            resp = handle_message(
                "write My Ideas\nSome brainstorming",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        out = present(resp)
        assert "My Ideas" in out
        assert "okay?" in out.lower()
        # No raw JSON
        assert '"kind"' not in out

    def test_confirmation_verbose(self) -> None:
        """Verbose mode shows full plan details for confirmation."""
        with _ctx():
            resp = handle_message(
                "write My Ideas\nSome brainstorming",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        out = present(resp, verbose=True)
        assert "Pending confirmation:" in out
        assert "write_note" in out
        assert "Created:" in out


class TestScenarioTalkLogging:
    """TalkIntent execution records are properly logged."""

    def test_talk_records_logged(self, tmp_path: Path) -> None:
        """All turns including talk are logged to JSONL."""
        log_file = tmp_path / "scenario.jsonl"
        session = SessionContext()

        # Turn 1: Talk
        with _ctx(talk_return="Hello!"):
            r1 = handle_message(
                "hi",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
                log_path=log_file,
            )
        session = r1.session or session

        # Turn 2: Skill
        with _ctx():
            r2 = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
                log_path=log_file,
            )
        session = r2.session or session

        # Turn 3: Talk
        with _ctx(talk_return="Got it!"):
            handle_message(
                "thanks",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
                log_path=log_file,
            )

        # Verify log has all records
        lines = log_file.read_text().strip().split("\n")
        records = [json.loads(line) for line in lines]

        # Turn 1: __talk__, Turn 2: search_notes + summarize_note, Turn 3: __talk__
        assert len(records) == 4
        assert records[0]["skill_name"] == "__talk__"
        assert records[0]["side_effect_class"] == "NONE"
        assert records[1]["skill_name"] == "search_notes"
        assert records[2]["skill_name"] == "summarize_note"
        assert records[3]["skill_name"] == "__talk__"

    def test_anchor_binding_across_turns(self) -> None:
        """Session anchors from earlier turns are available in later turns."""
        session = SessionContext()

        # Turn 1: Talk (produces __talk__ anchor)
        with _ctx(talk_return="Sure!"):
            r1 = handle_message(
                "hey",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = r1.session or session
        assert len(session.anchors) == 1

        # Turn 2: Search (produces search_notes + summarize_note anchors)
        with _ctx():
            r2 = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = r2.session or session
        # Talk + search + summarize = 3 anchors
        assert len(session.anchors) == 3

        # Turn 3: "summarize that" — should resolve to last summarize
        with _ctx():
            r3 = handle_message(
                "summarize that",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        # Should resolve ref:last to the search result path
        assert isinstance(r3.intent, SkillInvocationIntent)
        assert r3.records[0].skill_name == "summarize_note"
        assert r3.records[0].success
