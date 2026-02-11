"""Tests for the presenter module — template-based AgentResponse formatting."""

from __future__ import annotations

from kavi.agent.models import (
    AgentResponse,
    ChainAction,
    ClarifyIntent,
    HelpIntent,
    PendingConfirmation,
    SessionContext,
    SkillAction,
    SkillInvocationIntent,
    TalkIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.agent.presenter import present
from kavi.consumer.chain import ChainSpec, ChainStep
from kavi.consumer.shim import ExecutionRecord


def _record(
    skill_name: str = "test",
    output: dict | None = None,
    success: bool = True,
    error: str | None = None,
    side_effect_class: str = "READ_ONLY",
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id="abc12345",
        skill_name=skill_name,
        source_hash="aaa",
        side_effect_class=side_effect_class,
        input_json={"query": "test"},
        output_json=output,
        success=success,
        error=error,
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:00:01",
    )


# ── Conversational mode tests ───────────────────────────────────────


class TestConversationalTalk:
    """TalkIntent shows response text directly."""

    def test_talk_response(self) -> None:
        rec = _record("__talk__", {"response": "Hello there!"})
        resp = AgentResponse(
            intent=TalkIntent(message="hi"),
            records=[rec],
        )
        out = present(resp)
        assert "Hello there!" in out
        assert "__talk__" not in out

    def test_talk_empty_response(self) -> None:
        rec = _record("__talk__", {"response": ""})
        resp = AgentResponse(
            intent=TalkIntent(message="hi"),
            records=[rec],
        )
        out = present(resp)
        assert out == ""


class TestConversationalConfirmation:
    """Confirmation messages read like natural language."""

    def test_write_note_with_body(self) -> None:
        resp = AgentResponse(
            intent=WriteNoteIntent(title="My Note", body="some text"),
            plan=SkillAction(
                skill_name="write_note",
                input={"path": "a.md", "title": "My Note", "body": "some text"},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="write_note",
                    input={"path": "a.md", "title": "My Note", "body": "some text"},
                ),
                intent=WriteNoteIntent(title="My Note", body="some text"),
            ),
        )
        out = present(resp)
        assert "My Note" in out
        assert "okay?" in out.lower()

    def test_write_note_empty_body(self) -> None:
        resp = AgentResponse(
            intent=WriteNoteIntent(title="Ideas", body=""),
            plan=SkillAction(
                skill_name="write_note",
                input={"path": "a.md", "title": "Ideas", "body": ""},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="write_note",
                    input={"path": "a.md", "title": "Ideas", "body": ""},
                ),
                intent=WriteNoteIntent(title="Ideas", body=""),
            ),
        )
        out = present(resp)
        assert "Ideas" in out
        assert "what should it say" in out.lower()

    def test_daily_note_confirmation(self) -> None:
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="create_daily_note",
                input={"content": "test"},
            ),
            plan=SkillAction(
                skill_name="create_daily_note",
                input={"content": "test"},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="create_daily_note",
                    input={"content": "test"},
                ),
                intent=SkillInvocationIntent(
                    skill_name="create_daily_note",
                    input={"content": "test"},
                ),
            ),
        )
        out = present(resp)
        assert "daily note" in out.lower()
        assert "okay?" in out.lower()

    def test_http_get_confirmation(self) -> None:
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="http_get_json",
                input={"url": "https://api.example.com"},
            ),
            plan=SkillAction(
                skill_name="http_get_json",
                input={"url": "https://api.example.com"},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="http_get_json",
                    input={"url": "https://api.example.com"},
                ),
                intent=SkillInvocationIntent(
                    skill_name="http_get_json",
                    input={"url": "https://api.example.com"},
                ),
            ),
        )
        out = present(resp)
        assert "api.example.com" in out
        assert "okay?" in out.lower()

    def test_no_plan_json_in_default_mode(self) -> None:
        """Default mode should NOT show raw plan JSON."""
        resp = AgentResponse(
            intent=WriteNoteIntent(title="Test", body="hi"),
            plan=SkillAction(
                skill_name="write_note",
                input={"path": "a.md", "title": "Test", "body": "hi"},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="write_note",
                    input={"path": "a.md", "title": "Test", "body": "hi"},
                ),
                intent=WriteNoteIntent(title="Test", body="hi"),
            ),
        )
        out = present(resp)
        assert '"kind"' not in out
        assert '"skill_name"' not in out


class TestConversationalSuccess:
    """Success responses are formatted naturally."""

    def test_search_results_table(self) -> None:
        rec = _record("search_notes", {
            "query": "ml",
            "results": [
                {"path": "notes/ml.md", "score": 0.95, "title": "ML Notes"},
            ],
        })
        resp = AgentResponse(
            intent=SkillInvocationIntent(skill_name="search_notes", input={"query": "ml"}),
            records=[rec],
        )
        out = present(resp)
        assert "notes/ml.md" in out
        assert "0.9500" in out

    def test_summarize_shows_summary(self) -> None:
        rec = _record("summarize_note", {
            "path": "notes/ml.md",
            "summary": "This note covers machine learning basics.",
        })
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="summarize_note",
                input={"path": "notes/ml.md"},
            ),
            records=[rec],
        )
        out = present(resp)
        assert "machine learning basics" in out

    def test_write_note_done(self) -> None:
        rec = _record("write_note", {
            "written_path": "vault_out/a.md",
            "sha256": "abc123",
        })
        resp = AgentResponse(
            intent=WriteNoteIntent(title="My Note", body="hi"),
            records=[rec],
        )
        out = present(resp)
        assert "vault_out/a.md" in out
        assert "Done" in out

    def test_daily_note_done(self) -> None:
        rec = _record("create_daily_note", {
            "path": "vault_out/daily/2026-01-01.md",
        })
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="create_daily_note",
                input={"content": "test"},
            ),
            records=[rec],
        )
        out = present(resp)
        assert "daily note" in out.lower()

    def test_read_by_tag(self) -> None:
        rec = _record("read_notes_by_tag", {
            "notes": [{"path": "n/ml.md", "title": "ML"}],
            "count": 1,
        })
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="read_notes_by_tag",
                input={"tag": "ml"},
            ),
            records=[rec],
        )
        out = present(resp)
        assert "1 note" in out

    def test_failed_record(self) -> None:
        rec = _record("summarize_note", success=False, error="File not found")
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="summarize_note",
                input={"path": "missing.md"},
            ),
            records=[rec],
        )
        out = present(resp)
        assert "failed" in out.lower()
        assert "File not found" in out


class TestConversationalError:
    """Error responses are user-friendly."""

    def test_error_message(self) -> None:
        resp = AgentResponse(
            intent=UnsupportedIntent(message="Cannot do that"),
            error="Cannot do that",
        )
        out = present(resp)
        assert "Cannot do that" in out
        assert "sorry" in out.lower() or "wrong" in out.lower()


class TestConversationalClarify:
    """ClarifyIntent shows question naturally, not as an error."""

    def test_clarify_no_error_prefix(self) -> None:
        resp = AgentResponse(
            intent=ClarifyIntent(question="Which note would you like to summarize?"),
            error="Which note would you like to summarize?",
        )
        out = present(resp)
        assert "Which note would you like to summarize?" in out
        assert "sorry" not in out.lower()
        assert "wrong" not in out.lower()

    def test_clarify_verbose_shows_intent(self) -> None:
        resp = AgentResponse(
            intent=ClarifyIntent(question="Did you mean X or Y?"),
            error="Did you mean X or Y?",
        )
        out = present(resp, verbose=True)
        assert "clarify" in out.lower()
        assert "Did you mean X or Y?" in out


class TestConversationalHelp:
    """Help text passes through."""

    def test_help_text(self) -> None:
        resp = AgentResponse(
            intent=HelpIntent(),
            help_text="Available skills:\n- search_notes\n- write_note",
        )
        out = present(resp)
        assert "search_notes" in out
        assert "write_note" in out


class TestConversationalWarnings:
    """Warnings appear as subtle notes."""

    def test_warnings_shown(self) -> None:
        rec = _record("search_notes", {"query": "ml", "results": []})
        resp = AgentResponse(
            intent=SkillInvocationIntent(skill_name="search_notes", input={"query": "ml"}),
            records=[rec],
            warnings=["Ignored: write_note. Ask separately."],
        )
        out = present(resp)
        assert "write_note" in out
        assert "Note:" in out


# ── Verbose mode tests ──────────────────────────────────────────────


class TestVerboseMode:
    """Verbose mode exposes full internal details."""

    def test_shows_intent_kind(self) -> None:
        rec = _record("__talk__", {"response": "Hello!"})
        resp = AgentResponse(
            intent=TalkIntent(message="hi"),
            records=[rec],
        )
        out = present(resp, verbose=True)
        assert "Intent:" in out
        assert '"talk"' in out

    def test_shows_plan(self) -> None:
        rec = _record("summarize_note", {
            "path": "a.md", "summary": "test",
        })
        resp = AgentResponse(
            intent=SkillInvocationIntent(
                skill_name="summarize_note",
                input={"path": "a.md"},
            ),
            plan=SkillAction(
                skill_name="summarize_note",
                input={"path": "a.md", "style": "bullet"},
            ),
            records=[rec],
        )
        out = present(resp, verbose=True)
        assert "Plan:" in out
        assert "summarize_note" in out

    def test_shows_records(self) -> None:
        rec = _record("search_notes", {"query": "ml", "results": []})
        resp = AgentResponse(
            intent=SkillInvocationIntent(skill_name="search_notes", input={"query": "ml"}),
            records=[rec],
        )
        out = present(resp, verbose=True)
        assert "Records (1):" in out
        assert "abc12345"[:8] in out
        assert "side_effect:" in out
        assert "timing:" in out

    def test_shows_session(self) -> None:
        from kavi.agent.models import Anchor

        session = SessionContext(anchors=[
            Anchor(
                label="test",
                execution_id="xyz789",
                skill_name="search_notes",
                data={"query": "ml"},
            ),
        ])
        rec = _record("__talk__", {"response": "hi"})
        resp = AgentResponse(
            intent=TalkIntent(message="hi"),
            records=[rec],
            session=session,
        )
        out = present(resp, verbose=True)
        assert "Session (1 anchors):" in out
        assert "search_notes" in out

    def test_shows_error(self) -> None:
        resp = AgentResponse(
            intent=UnsupportedIntent(message="nope"),
            error="Something failed",
        )
        out = present(resp, verbose=True)
        assert "Error:" in out
        assert "Something failed" in out

    def test_shows_pending(self) -> None:
        resp = AgentResponse(
            intent=WriteNoteIntent(title="T", body="b"),
            plan=SkillAction(
                skill_name="write_note",
                input={"path": "a.md", "title": "T", "body": "b"},
            ),
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=SkillAction(
                    skill_name="write_note",
                    input={"path": "a.md", "title": "T", "body": "b"},
                ),
                intent=WriteNoteIntent(title="T", body="b"),
            ),
        )
        out = present(resp, verbose=True)
        assert "Pending confirmation:" in out
        assert "Created:" in out
        assert "Expired:" in out

    def test_verbose_shows_raw_json(self) -> None:
        """Verbose mode includes JSON representations for inspectability."""
        rec = _record("search_notes", {"query": "ml", "results": []})
        resp = AgentResponse(
            intent=SkillInvocationIntent(skill_name="search_notes", input={"query": "ml"}),
            plan=ChainAction(
                chain=ChainSpec(steps=[
                    ChainStep(skill_name="search_notes", input={"query": "ml"}),
                ]),
            ),
            records=[rec],
        )
        out = present(resp, verbose=True)
        # Should contain JSON-formatted data
        assert '"skill_invocation"' in out
        assert "input:" in out
        assert "output:" in out
