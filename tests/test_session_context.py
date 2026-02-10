"""Tests for SessionContext: anchors, resolution, and resolver (D015)."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from kavi.agent.models import (
    AmbiguityResponse,
    Anchor,
    SessionContext,
    SkillInvocationIntent,
    _extract_anchor_data,
)
from kavi.consumer.shim import ExecutionRecord, SkillInfo
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


# ── Helpers ───────────────────────────────────────────────────────────


def _make_record(
    skill_name: str,
    output: dict[str, Any] | None = None,
    success: bool = True,
    execution_id: str = "abc123",
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=execution_id,
        skill_name=skill_name,
        source_hash="hash",
        side_effect_class="READ_ONLY",
        input_json={},
        output_json=output,
        success=success,
        error=None if success else "fail",
        started_at="2026-02-10T00:00:00",
        finished_at="2026-02-10T00:00:01",
    )


# ── Anchor extraction tests ──────────────────────────────────────────


class TestExtractAnchorData:
    def test_search_notes_extracts_query_and_top_result(self) -> None:
        output = {
            "query": "machine learning",
            "results": [
                {"path": "notes/ml.md", "score": 0.95, "title": "ML"},
                {"path": "notes/python.md", "score": 0.80},
            ],
        }
        data = _extract_anchor_data("search_notes", output)
        assert data["query"] == "machine learning"
        assert data["top_result_path"] == "notes/ml.md"

    def test_summarize_note_extracts_path_and_summary(self) -> None:
        output = {
            "path": "notes/ml.md",
            "summary": "A summary.",
            "key_points": ["a", "b"],
        }
        data = _extract_anchor_data("summarize_note", output)
        assert data["path"] == "notes/ml.md"
        assert data["summary"] == "A summary."
        assert "key_points" not in data

    def test_write_note_extracts_written_path(self) -> None:
        output = {"written_path": "vault/test.md", "title": "Test"}
        data = _extract_anchor_data("write_note", output)
        assert data["written_path"] == "vault/test.md"
        assert "title" not in data

    def test_unknown_skill_takes_scalars(self) -> None:
        output = {"a": 1, "b": "hello", "c": [1, 2]}
        data = _extract_anchor_data("unknown_skill", output)
        assert data == {"a": 1, "b": "hello"}

    def test_unknown_skill_caps_at_five(self) -> None:
        output = {f"key{i}": i for i in range(10)}
        data = _extract_anchor_data("unknown_skill", output)
        assert len(data) == 5

    def test_search_no_results_no_top_result_path(self) -> None:
        output = {"query": "nothing", "results": []}
        data = _extract_anchor_data("search_notes", output)
        assert data == {"query": "nothing"}
        assert "top_result_path" not in data


# ── SessionContext.add_from_records ───────────────────────────────────


class TestAddFromRecords:
    def test_successful_record_creates_anchor(self) -> None:
        ctx = SessionContext()
        rec = _make_record("search_notes", {"query": "ml", "results": []})
        ctx.add_from_records([rec])
        assert len(ctx.anchors) == 1
        assert ctx.anchors[0].skill_name == "search_notes"
        assert ctx.anchors[0].execution_id == "abc123"

    def test_failed_record_ignored(self) -> None:
        ctx = SessionContext()
        rec = _make_record("search_notes", None, success=False)
        ctx.add_from_records([rec])
        assert len(ctx.anchors) == 0

    def test_none_output_ignored(self) -> None:
        ctx = SessionContext()
        rec = _make_record("search_notes", None, success=True)
        ctx.add_from_records([rec])
        assert len(ctx.anchors) == 0

    def test_multiple_records_create_multiple_anchors(self) -> None:
        ctx = SessionContext()
        recs = [
            _make_record("search_notes", {"query": "q", "results": []}, execution_id="aaa"),
            _make_record("summarize_note", {"path": "a.md", "summary": "s"}, execution_id="bbb"),
        ]
        ctx.add_from_records(recs)
        assert len(ctx.anchors) == 2

    def test_sliding_window_caps_at_10(self) -> None:
        ctx = SessionContext()
        for i in range(15):
            rec = _make_record("search_notes", {"query": f"q{i}", "results": []}, execution_id=f"id{i:03d}")
            ctx.add_from_records([rec])
        assert len(ctx.anchors) == 10
        # Oldest evicted, newest kept
        assert ctx.anchors[0].execution_id == "id005"
        assert ctx.anchors[-1].execution_id == "id014"


# ── SessionContext.resolve ────────────────────────────────────────────


class TestResolve:
    def _ctx_with_anchors(self) -> SessionContext:
        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="search_notes result", execution_id="aaa111", skill_name="search_notes", data={"query": "ml"}),
            Anchor(label="summarize_note result", execution_id="bbb222", skill_name="summarize_note", data={"path": "a.md"}),
            Anchor(label="search_notes result", execution_id="ccc333", skill_name="search_notes", data={"query": "python"}),
        ]
        return ctx

    def test_last_returns_most_recent(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("last")
        assert a is not None
        assert a.execution_id == "ccc333"

    def test_that_returns_most_recent(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("that")
        assert a is not None
        assert a.execution_id == "ccc333"

    def test_it_returns_most_recent(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("it")
        assert a is not None
        assert a.execution_id == "ccc333"

    def test_the_result_returns_most_recent(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("the result")
        assert a is not None
        assert a.execution_id == "ccc333"

    def test_last_search_returns_most_recent_search(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("last_search")
        assert a is not None
        assert a.execution_id == "ccc333"
        assert a.skill_name == "search_notes"

    def test_last_summarize_returns_summarize(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("last_summarize")
        assert a is not None
        assert a.execution_id == "bbb222"
        assert a.skill_name == "summarize_note"

    def test_last_summarize_note_exact_match(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("last_summarize_note")
        assert a is not None
        assert a.skill_name == "summarize_note"

    def test_exec_prefix_match(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("exec:bbb")
        assert a is not None
        assert a.execution_id == "bbb222"

    def test_exec_no_match_returns_none(self) -> None:
        ctx = self._ctx_with_anchors()
        assert ctx.resolve("exec:zzz") is None

    def test_empty_session_returns_none(self) -> None:
        ctx = SessionContext()
        assert ctx.resolve("last") is None
        assert ctx.resolve("last_search") is None
        assert ctx.resolve("exec:abc") is None

    def test_unknown_ref_returns_none(self) -> None:
        ctx = self._ctx_with_anchors()
        assert ctx.resolve("something_random") is None

    def test_last_nonexistent_skill_returns_none(self) -> None:
        ctx = self._ctx_with_anchors()
        assert ctx.resolve("last_write") is None

    def test_case_insensitive(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("LAST")
        assert a is not None
        assert a.execution_id == "ccc333"

    def test_last_with_whitespace(self) -> None:
        ctx = self._ctx_with_anchors()
        a = ctx.resolve("  last  ")
        assert a is not None


# ── SessionContext.ambiguous ──────────────────────────────────────────


class TestAmbiguous:
    def test_exec_prefix_multiple_matches(self) -> None:
        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="abc111", skill_name="s1", data={}),
            Anchor(label="b", execution_id="abc222", skill_name="s2", data={}),
            Anchor(label="c", execution_id="def333", skill_name="s3", data={}),
        ]
        candidates = ctx.ambiguous("exec:abc")
        assert len(candidates) == 2

    def test_exec_prefix_no_match(self) -> None:
        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="abc111", skill_name="s1", data={}),
        ]
        assert ctx.ambiguous("exec:zzz") == []

    def test_non_exec_ref_returns_empty(self) -> None:
        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="abc111", skill_name="s1", data={}),
        ]
        assert ctx.ambiguous("last") == []

    def test_empty_session(self) -> None:
        ctx = SessionContext()
        assert ctx.ambiguous("exec:abc") == []


# ── Resolver tests ────────────────────────────────────────────────────


class TestResolver:
    """Tests for resolve_refs in agent/resolver.py."""

    def test_no_session_passes_through(self) -> None:
        from kavi.agent.resolver import resolve_refs

        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "notes/ml.md"},
        )
        result = resolve_refs(intent, None)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["path"] == "notes/ml.md"

    def test_no_ref_marker_passes_through(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="aaa", skill_name="search_notes", data={"top_result_path": "x.md"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "notes/ml.md"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["path"] == "notes/ml.md"

    def test_ref_last_resolves_to_anchor_data(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            Anchor(
                label="search_notes result",
                execution_id="aaa",
                skill_name="search_notes",
                data={"query": "ml", "top_result_path": "notes/ml.md"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        # Should resolve path from search anchor's top_result_path
        assert result.input["path"] == "notes/ml.md"

    def test_ref_last_search_resolves(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="aaa", skill_name="summarize_note", data={"path": "old.md"}),
            Anchor(label="b", execution_id="bbb", skill_name="search_notes", data={"top_result_path": "found.md"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:last_search"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["path"] == "found.md"

    def test_ref_unresolved_returns_ambiguity(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()  # empty — no anchors
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, AmbiguityResponse)
        assert "no prior results" in result.message.lower()

    def test_ref_exec_prefix_resolves(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            Anchor(label="a", execution_id="abc123def", skill_name="search_notes", data={"top_result_path": "x.md"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:exec:abc123"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["path"] == "x.md"


# ── Extract anchors helper ────────────────────────────────────────────


class TestExtractAnchors:
    def test_extract_from_mixed_records(self) -> None:
        from kavi.agent.resolver import extract_anchors

        records = [
            _make_record("search_notes", {"query": "ml", "results": [{"path": "a.md", "score": 0.9}]}, execution_id="r1"),
            _make_record("summarize_note", None, success=False, execution_id="r2"),
            _make_record("summarize_note", {"path": "a.md", "summary": "s"}, execution_id="r3"),
        ]
        ctx = extract_anchors(records, existing=None)
        assert len(ctx.anchors) == 2
        assert ctx.anchors[0].execution_id == "r1"
        assert ctx.anchors[1].execution_id == "r3"

    def test_extract_preserves_existing(self) -> None:
        from kavi.agent.resolver import extract_anchors

        existing = SessionContext()
        existing.anchors = [
            Anchor(label="old", execution_id="old1", skill_name="s", data={}),
        ]
        records = [
            _make_record("search_notes", {"query": "q", "results": []}, execution_id="new1"),
        ]
        ctx = extract_anchors(records, existing=existing)
        assert len(ctx.anchors) == 2
        assert ctx.anchors[0].execution_id == "old1"
        assert ctx.anchors[1].execution_id == "new1"


# ── Integration: handle_message with session ──────────────────────────

# Reuse stubs from test_agent_chat_v0

# ── Parser ref pattern tests ──────────────────────────────────────────

from kavi.agent.parser import parse_intent


class TestParserRefPatterns:
    """Deterministic parser emits ref markers for 'that'/'it'/'again'."""

    def _parse(self, msg: str) -> ParsedIntent:
        from tests.test_agent_chat_v0 import SKILL_INFOS as _si
        intent, _ = parse_intent(msg, _si, mode="deterministic")
        return intent

    def test_summarize_that(self) -> None:
        intent = self._parse("summarize that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"
        assert intent.input["path"] == "ref:last"

    def test_summarize_it(self) -> None:
        intent = self._parse("summarize it")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"

    def test_summarize_the_result(self) -> None:
        intent = self._parse("summarize the result")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"

    def test_summarize_that_paragraph(self) -> None:
        intent = self._parse("summarize that paragraph")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"
        assert intent.input["style"] == "paragraph"

    def test_summarize_this(self) -> None:
        intent = self._parse("summarize this")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"

    def test_write_that(self) -> None:
        intent = self._parse("write that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "write_note"
        assert intent.input["path"] == "ref:last"

    def test_write_that_to_a_note(self) -> None:
        intent = self._parse("write that to a note")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "write_note"

    def test_again(self) -> None:
        intent = self._parse("again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"

    def test_do_it_again(self) -> None:
        intent = self._parse("do it again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "ref:last"

    def test_again_paragraph(self) -> None:
        intent = self._parse("again paragraph")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["style"] == "paragraph"
        assert intent.input["path"] == "ref:last"

    def test_summarize_real_path_not_ref(self) -> None:
        """'summarize notes/ml.md' should NOT match ref pattern."""
        intent = self._parse("summarize notes/ml.md")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "notes/ml.md"
        assert "ref:" not in intent.input["path"]

    def test_write_real_title_not_ref(self) -> None:
        """'write My Title' should NOT match ref pattern."""
        from kavi.agent.models import WriteNoteIntent
        intent = self._parse("write My Title")
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "My Title"


from tests.test_agent_chat_v0 import (
    FAKE_REGISTRY,
    SKILL_INFOS,
    _ALL_EFFECTS,
    _ctx,
)
from kavi.agent.core import handle_message


class TestHandleMessageWithSession:
    """handle_message passes session through and returns updated session."""

    def test_stateless_returns_none_session(self) -> None:
        """Without session param, response.session is None."""
        with _ctx():
            resp = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert resp.session is None

    def test_session_returned_after_execution(self) -> None:
        """With session param, response has updated session."""
        session = SessionContext()
        with _ctx():
            resp = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        assert resp.session is not None
        assert len(resp.session.anchors) > 0
        # search → summarize chain produces 2 anchors
        skill_names = [a.skill_name for a in resp.session.anchors]
        assert "search_notes" in skill_names

    def test_session_accumulates_across_calls(self) -> None:
        """Passing returned session back preserves history."""
        session = SessionContext()
        with _ctx():
            resp1 = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = resp1.session
        assert session is not None

        with _ctx():
            resp2 = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = resp2.session
        assert session is not None
        # Should have anchors from both calls
        skill_names = [a.skill_name for a in session.anchors]
        assert "search_notes" in skill_names
        assert "summarize_note" in skill_names

    def test_session_none_on_error(self) -> None:
        """On error paths, session may still be returned."""
        session = SessionContext()
        with _ctx():
            resp = handle_message(
                "do something weird",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        # Even on error, if session was passed, we get it back
        # (may be unchanged)
        assert resp.error is not None

    def test_ref_resolution_in_handle_message(self) -> None:
        """ref:last in input resolves to prior search result."""
        # First turn: search (produces search + summarize anchors)
        session = SessionContext()
        with _ctx():
            resp1 = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        session = resp1.session
        assert session is not None
        # Find the search_notes anchor
        search_anchors = [a for a in session.anchors if a.skill_name == "search_notes"]
        assert len(search_anchors) == 1
        assert "top_result_path" in search_anchors[0].data

    def test_backward_compat_no_session(self) -> None:
        """Existing callers without session param still work."""
        with _ctx():
            resp = handle_message(
                "search ml",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert resp.error is None
        assert len(resp.records) == 2
        assert resp.session is None
