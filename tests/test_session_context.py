"""Tests for SessionContext: anchors, resolution, and resolver (D015)."""

from __future__ import annotations

from typing import Any

from kavi.agent.core import handle_message
from kavi.agent.models import (
    AmbiguityResponse,
    Anchor,
    ParsedIntent,
    SessionContext,
    SkillInvocationIntent,
    TransformIntent,
    WriteNoteIntent,
    _extract_anchor_data,
)
from kavi.agent.parser import parse_intent
from kavi.consumer.shim import ExecutionRecord
from tests.test_agent_chat_v0 import (
    FAKE_REGISTRY,
    SKILL_INFOS,
    _ctx,
)

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


def _anchor(
    skill: str,
    eid: str,
    data: dict[str, Any] | None = None,
    label: str = "",
) -> Anchor:
    """Shorthand anchor constructor for tests."""
    return Anchor(
        label=label or f"{skill} result",
        execution_id=eid,
        skill_name=skill,
        data=data or {},
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

    def test_write_note_extracts_scalars(self) -> None:
        output = {"written_path": "vault/test.md", "title": "Test"}
        data = _extract_anchor_data("write_note", output)
        assert data["written_path"] == "vault/test.md"
        assert data["title"] == "Test"

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
        rec = _make_record(
            "search_notes", {"query": "ml", "results": []},
        )
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
            _make_record(
                "search_notes",
                {"query": "q", "results": []},
                execution_id="aaa",
            ),
            _make_record(
                "summarize_note",
                {"path": "a.md", "summary": "s"},
                execution_id="bbb",
            ),
        ]
        ctx.add_from_records(recs)
        assert len(ctx.anchors) == 2

    def test_sliding_window_caps_at_10(self) -> None:
        ctx = SessionContext()
        for i in range(15):
            rec = _make_record(
                "search_notes",
                {"query": f"q{i}", "results": []},
                execution_id=f"id{i:03d}",
            )
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
            _anchor("search_notes", "aaa111", {"query": "ml"}),
            _anchor("summarize_note", "bbb222", {"path": "a.md"}),
            _anchor("search_notes", "ccc333", {"query": "python"}),
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

    def test_last_skill_exact_match_preferred(self) -> None:
        """Exact match beats startswith/contains."""
        ctx = SessionContext()
        ctx.anchors = [
            _anchor("search", "aaa"),
            _anchor("search_notes", "bbb"),
        ]
        a = ctx.resolve("last_search")
        assert a is not None
        # Exact match on "search" is preferred over startswith "search_notes"
        assert a.execution_id == "aaa"

    def test_last_skill_startswith_before_contains(self) -> None:
        """startswith match beats contains."""
        ctx = SessionContext()
        ctx.anchors = [
            _anchor("note_search", "aaa"),  # contains "search"
            _anchor("search_notes", "bbb"),  # startswith "search"
        ]
        a = ctx.resolve("last_search")
        assert a is not None
        assert a.execution_id == "bbb"

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
            _anchor("s1", "abc111"),
            _anchor("s2", "abc222"),
            _anchor("s3", "def333"),
        ]
        candidates = ctx.ambiguous("exec:abc")
        assert len(candidates) == 2

    def test_exec_prefix_no_match(self) -> None:
        ctx = SessionContext()
        ctx.anchors = [_anchor("s1", "abc111")]
        assert ctx.ambiguous("exec:zzz") == []

    def test_non_exec_ref_returns_empty(self) -> None:
        ctx = SessionContext()
        ctx.anchors = [_anchor("s1", "abc111")]
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
            _anchor(
                "search_notes", "aaa",
                {"top_result_path": "x.md"},
            ),
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
                data={
                    "query": "ml",
                    "top_result_path": "notes/ml.md",
                },
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
            _anchor(
                "summarize_note", "aaa",
                {"path": "old.md"},
            ),
            _anchor(
                "search_notes", "bbb",
                {"top_result_path": "found.md"},
            ),
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
            _anchor(
                "search_notes", "abc123def",
                {"top_result_path": "x.md"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:exec:abc123"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["path"] == "x.md"


# ── Resolve "search for that" and "search again" ─────────────────────


class TestResolveSearchRef:
    """resolve_refs handles search ref patterns via SkillInvocationIntent."""

    def test_search_for_that_after_search(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor("search_notes", "aaa", {"query": "kubernetes"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["query"] == "kubernetes"

    def test_search_for_that_after_summarize(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "bbb",
                {"path": "notes/ml.md", "summary": "ML is great"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        # Generic resolver uses _anchor_value which prefers path over summary
        assert result.input["query"] == "notes/ml.md"

    def test_search_again_uses_last_search_query(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor("search_notes", "aaa", {"query": "kubernetes"}),
            _anchor("summarize_note", "bbb", {"path": "a.md", "summary": "s"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "ref:last_search"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["query"] == "kubernetes"

    def test_search_ref_no_session_passes_through(self) -> None:
        from kavi.agent.resolver import resolve_refs

        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "ref:last"},
        )
        result = resolve_refs(intent, None)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["query"] == "ref:last"

    def test_search_ref_no_anchors_returns_ambiguity(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, AmbiguityResponse)
        assert "no prior results" in result.message.lower()

    def test_search_no_ref_passes_through(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [_anchor("search_notes", "aaa", {"query": "ml"})]
        intent = SkillInvocationIntent(
            skill_name="search_notes", input={"query": "kubernetes"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["query"] == "kubernetes"


# ── Resolve TransformIntent ───────────────────────────────────────────


class TestResolveTransform:
    """resolve_refs handles TransformIntent by re-invoking with overrides."""

    def test_transform_style_override(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "aaa",
                {"path": "notes/ml.md", "summary": "ML is great"},
            ),
        ]
        intent = TransformIntent(overrides={"style": "paragraph"})
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "summarize_note"
        assert result.input["path"] == "notes/ml.md"
        assert result.input["style"] == "paragraph"

    def test_transform_path_override(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "aaa",
                {"path": "notes/old.md", "summary": "Old stuff"},
            ),
        ]
        intent = TransformIntent(overrides={"path": "notes/new.md"})
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "summarize_note"
        assert result.input["path"] == "notes/new.md"

    def test_transform_after_search(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor("search_notes", "aaa", {"query": "ml"}),
            _anchor(
                "summarize_note", "bbb",
                {"path": "notes/ml.md", "summary": "s"},
            ),
        ]
        intent = TransformIntent(overrides={"style": "paragraph"})
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        # Target is last anchor (summarize_note)
        assert result.skill_name == "summarize_note"
        assert result.input["style"] == "paragraph"

    def test_transform_no_session_passes_through(self) -> None:
        from kavi.agent.resolver import resolve_refs

        intent = TransformIntent(overrides={"style": "paragraph"})
        result = resolve_refs(intent, None)
        # Without session, transform passes through unresolved
        assert isinstance(result, TransformIntent)

    def test_transform_no_anchors_returns_ambiguity(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        intent = TransformIntent(overrides={"style": "paragraph"})
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, AmbiguityResponse)
        assert "no prior results" in result.message.lower()

    def test_transform_filters_to_valid_input_fields(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "aaa",
                {"path": "a.md", "summary": "s"},
            ),
        ]
        intent = TransformIntent(overrides={"style": "bullet"})
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        # "summary" is an output field, not input — should be filtered
        assert "summary" not in result.input
        assert result.input["path"] == "a.md"


# ── Resolve "again" and "write that" ─────────────────────────────────


class TestResolveAgain:
    """resolve_refs handles 'again' by re-invoking last skill."""

    def test_again_re_invokes_last_skill(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "search_notes", "aaa",
                {"query": "ml"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="ref:last_skill",
            input={"ref:again": "true"},
        )
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "search_notes"
        assert result.input["query"] == "ml"

    def test_again_with_style_override(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "bbb",
                {"path": "a.md", "summary": "s"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="ref:last_skill",
            input={"ref:again": "true", "style": "paragraph"},
        )
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "summarize_note"
        assert result.input["style"] == "paragraph"
        assert result.input["path"] == "a.md"
        assert "summary" not in result.input  # output field, not input

    def test_again_no_anchors_returns_ambiguity(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()  # empty
        intent = SkillInvocationIntent(
            skill_name="ref:last_skill",
            input={"ref:again": "true"},
        )
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, AmbiguityResponse)
        assert "again" in result.message.lower()

    def test_again_unknown_skill_copies_all_data(self) -> None:
        """When skill isn't in registry, fall back to copying all data."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor("unknown_skill", "aaa", {"foo": "bar"}),
        ]
        intent = SkillInvocationIntent(
            skill_name="ref:last_skill",
            input={"ref:again": "true"},
        )
        result = resolve_refs(intent, ctx, skills=SKILL_INFOS)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["foo"] == "bar"


class TestResolveWriteThat:
    """resolve_refs handles 'write that' by writing last result."""

    def test_write_that_from_search(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "search_notes", "aaa",
                {"query": "ml"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="write_note",
            input={
                "path": "ref:last_written_path",
                "title": "ref:last_title",
                "body": "ref:last_body",
            },
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "write_note"
        assert result.input["title"] == "Notes: ml"
        assert "ml" in result.input["body"]

    def test_write_that_from_summarize(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "bbb",
                {"path": "notes/ml.md", "summary": "Great notes"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="write_note",
            input={
                "path": "ref:last_written_path",
                "title": "ref:last_title",
                "body": "ref:last_body",
            },
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.skill_name == "write_note"
        assert result.input["body"] == "Great notes"
        assert result.input["title"] == "Summary: ml"

    def test_write_that_no_anchors_returns_ambiguity(self) -> None:
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()  # empty
        intent = SkillInvocationIntent(
            skill_name="write_note",
            input={
                "path": "ref:last_written_path",
                "title": "ref:last_title",
                "body": "ref:last_body",
            },
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, AmbiguityResponse)
        assert "write that" in result.message.lower()


# ── Extract anchors helper ────────────────────────────────────────────


class TestExtractAnchors:
    def test_extract_from_mixed_records(self) -> None:
        from kavi.agent.resolver import extract_anchors

        records = [
            _make_record(
                "search_notes",
                {"query": "ml", "results": [
                    {"path": "a.md", "score": 0.9},
                ]},
                execution_id="r1",
            ),
            _make_record(
                "summarize_note", None,
                success=False, execution_id="r2",
            ),
            _make_record(
                "summarize_note",
                {"path": "a.md", "summary": "s"},
                execution_id="r3",
            ),
        ]
        ctx = extract_anchors(records, existing=None)
        assert len(ctx.anchors) == 2
        assert ctx.anchors[0].execution_id == "r1"
        assert ctx.anchors[1].execution_id == "r3"

    def test_extract_preserves_existing(self) -> None:
        from kavi.agent.resolver import extract_anchors

        existing = SessionContext()
        existing.anchors = [
            _anchor("s", "old1"),
        ]
        records = [
            _make_record(
                "search_notes",
                {"query": "q", "results": []},
                execution_id="new1",
            ),
        ]
        ctx = extract_anchors(records, existing=existing)
        assert len(ctx.anchors) == 2
        assert ctx.anchors[0].execution_id == "old1"
        assert ctx.anchors[1].execution_id == "new1"


# ── Parser ref pattern tests ──────────────────────────────────────────


class TestParserRefPatterns:
    """Deterministic parser emits ref markers for 'that'/'it'/'again'."""

    def _parse(self, msg: str) -> ParsedIntent:
        return parse_intent(msg, SKILL_INFOS, mode="deterministic").intent

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
        # Parser emits ref:last_* fields for resolver to handle
        assert intent.input["body"] == "ref:last_body"

    def test_write_that_to_a_note(self) -> None:
        intent = self._parse("write that to a note")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "write_note"
        assert intent.input["title"] == "ref:last_title"

    def test_again(self) -> None:
        intent = self._parse("again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "ref:last_skill"
        assert intent.input["ref:again"] == "true"

    def test_do_it_again(self) -> None:
        intent = self._parse("do it again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "ref:last_skill"
        assert intent.input["ref:again"] == "true"

    def test_again_paragraph(self) -> None:
        intent = self._parse("again paragraph")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "ref:last_skill"
        assert intent.input["style"] == "paragraph"

    def test_search_for_that(self) -> None:
        intent = self._parse("search for that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last"

    def test_search_that(self) -> None:
        intent = self._parse("search that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last"

    def test_find_that(self) -> None:
        intent = self._parse("find that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last"

    def test_search_for_it(self) -> None:
        intent = self._parse("search for it")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last"

    def test_search_notes_about_that(self) -> None:
        intent = self._parse("search notes about that")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last"

    def test_search_again(self) -> None:
        intent = self._parse("search again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last_search"

    def test_find_again(self) -> None:
        intent = self._parse("find again")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "ref:last_search"

    def test_search_real_query_not_ref(self) -> None:
        """'search kubernetes' should NOT match ref pattern."""
        intent = self._parse("search kubernetes")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "search_notes"
        assert intent.input["query"] == "kubernetes"
        assert "ref:" not in intent.input["query"]

    # ── TransformIntent patterns ──────────────────────────────────────

    def test_but_paragraph(self) -> None:
        intent = self._parse("but paragraph")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "paragraph"}

    def test_but_bullet(self) -> None:
        intent = self._parse("but bullet")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "bullet"}

    def test_make_it_paragraph(self) -> None:
        intent = self._parse("make it paragraph")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "paragraph"}

    def test_no_paragraph(self) -> None:
        intent = self._parse("no, paragraph")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "paragraph"}

    def test_actually_bullet(self) -> None:
        intent = self._parse("actually, bullet")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "bullet"}

    def test_try_path_instead(self) -> None:
        intent = self._parse("try notes/ml.md instead")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"path": "notes/ml.md"}

    def test_no_path(self) -> None:
        intent = self._parse("no, notes/other.md")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"path": "notes/other.md"}

    def test_i_meant_paragraph(self) -> None:
        intent = self._parse("I meant paragraph")
        assert isinstance(intent, TransformIntent)
        assert intent.overrides == {"style": "paragraph"}

    def test_transform_default_target_is_last(self) -> None:
        intent = self._parse("but paragraph")
        assert isinstance(intent, TransformIntent)
        assert intent.target_ref == "last"

    def test_summarize_real_path_not_ref(self) -> None:
        """'summarize notes/ml.md' should NOT match ref pattern."""
        intent = self._parse("summarize notes/ml.md")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["path"] == "notes/ml.md"
        assert "ref:" not in intent.input["path"]

    def test_write_real_title_not_ref(self) -> None:
        """'write My Title' should NOT match ref pattern."""
        intent = self._parse("write My Title")
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "My Title"


# ── Integration: handle_message with session ──────────────────────────


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

    def test_talk_intent_updates_session(self) -> None:
        """TalkIntent produces a __talk__ record and updates session."""
        session = SessionContext()
        with _ctx():
            resp = handle_message(
                "do something weird",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
                session=session,
            )
        # TalkIntent succeeds and updates session with __talk__ anchor
        assert resp.error is None
        assert resp.session is not None
        assert any(a.skill_name == "__talk__" for a in resp.session.anchors)

    def test_ref_resolution_in_handle_message(self) -> None:
        """ref:last in input resolves to prior search result."""
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
        search_anchors = [
            a for a in session.anchors
            if a.skill_name == "search_notes"
        ]
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
        assert len(resp.records) == 1
        assert resp.session is None


# ── Generic content ref resolution ────────────────────────────────────


class TestContentRefResolution:
    """resolve_refs uses content extraction for content-like fields."""

    def test_content_ref_from_talk_anchor(self) -> None:
        """ref:last in a content field resolves to __talk__ response text."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "__talk__", "aaa",
                {"response": "India has a rich history of mathematics."},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="create_daily_note",
            input={"content": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["content"] == "India has a rich history of mathematics."

    def test_content_ref_from_summarize_anchor(self) -> None:
        """ref:last in a content field resolves to summary text."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "bbb",
                {"path": "notes/ml.md", "summary": "ML is fascinating."},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="create_daily_note",
            input={"content": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["content"] == "ML is fascinating."

    def test_content_ref_from_search_anchor(self) -> None:
        """ref:last in content field from search → uses query."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "search_notes", "ccc",
                {"query": "machine learning", "top_result_path": "notes/ml.md"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="create_daily_note",
            input={"content": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        # Content extraction prefers response > summary > content > query
        assert result.input["content"] == "machine learning"

    def test_path_ref_still_uses_path_extraction(self) -> None:
        """ref:last in a path field still uses path-oriented extraction."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "search_notes", "aaa",
                {"query": "ml", "top_result_path": "notes/ml.md"},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        # Path extraction prefers top_result_path
        assert result.input["path"] == "notes/ml.md"

    def test_content_ref_no_anchors_returns_ambiguity(self) -> None:
        """ref:last in content field with empty session → AmbiguityResponse."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        intent = SkillInvocationIntent(
            skill_name="create_daily_note",
            input={"content": "ref:last"},
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, AmbiguityResponse)
        assert "no prior results" in result.message.lower()

    def test_body_field_uses_content_extraction(self) -> None:
        """'body' is also a content-like field — uses content extraction."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "__talk__", "aaa",
                {"response": "A conversation about Python."},
            ),
        ]
        intent = SkillInvocationIntent(
            skill_name="write_note",
            input={
                "path": "Inbox/AI/test.md",
                "title": "Test",
                "body": "ref:last",
            },
        )
        result = resolve_refs(intent, ctx)
        assert isinstance(result, SkillInvocationIntent)
        assert result.input["body"] == "A conversation about Python."
        # path and title kept as-is
        assert result.input["path"] == "Inbox/AI/test.md"
        assert result.input["title"] == "Test"


class TestWriteNoteIntentRefResolution:
    """resolve_refs handles ref: markers in WriteNoteIntent fields."""

    def test_write_note_body_ref_resolves_talk_response(self) -> None:
        """'write that to a note' resolves body from TalkIntent anchor."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "__talk__", "aaa",
                {"response": "Here is a lovely poem about dogs."},
            ),
        ]
        intent = WriteNoteIntent(title="Dog Poem", body="ref:last")
        result = resolve_refs(intent, ctx)
        assert isinstance(result, WriteNoteIntent)
        assert result.title == "Dog Poem"
        assert result.body == "Here is a lovely poem about dogs."

    def test_write_note_both_refs_resolve(self) -> None:
        """Both title and body as ref:last resolve from anchor."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        ctx.anchors = [
            _anchor(
                "summarize_note", "bbb",
                {"path": "notes/ml.md", "summary": "ML is fascinating."},
            ),
        ]
        intent = WriteNoteIntent(title="ref:last", body="ref:last")
        result = resolve_refs(intent, ctx)
        assert isinstance(result, WriteNoteIntent)
        # title uses _anchor_value → path field
        assert result.title == "notes/ml.md"
        # body uses _content_anchor_value → summary field
        assert result.body == "ML is fascinating."

    def test_write_note_no_ref_passes_through(self) -> None:
        """WriteNoteIntent without refs passes through unchanged."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        intent = WriteNoteIntent(title="My Note", body="Some text.")
        result = resolve_refs(intent, ctx)
        assert isinstance(result, WriteNoteIntent)
        assert result.title == "My Note"
        assert result.body == "Some text."

    def test_write_note_ref_no_session_passes_through(self) -> None:
        """WriteNoteIntent with ref but no session passes through."""
        from kavi.agent.resolver import resolve_refs

        intent = WriteNoteIntent(title="My Note", body="ref:last")
        result = resolve_refs(intent, None)
        assert isinstance(result, WriteNoteIntent)
        assert result.body == "ref:last"

    def test_write_note_ref_empty_session_returns_ambiguity(self) -> None:
        """WriteNoteIntent ref:last with empty session → AmbiguityResponse."""
        from kavi.agent.resolver import resolve_refs

        ctx = SessionContext()
        intent = WriteNoteIntent(title="My Note", body="ref:last")
        result = resolve_refs(intent, ctx)
        assert isinstance(result, AmbiguityResponse)


class TestWriteNoteAutoBindFromSession:
    """handle_message auto-binds empty write_note body from session (D018)."""

    def test_empty_body_binds_talk_anchor(self) -> None:
        """LLM returns empty body but session has Talk anchor → auto-bind."""
        from unittest.mock import patch

        from kavi.agent.parser import ParseResult

        poem = "A poem about dogs in the park."
        session = SessionContext()
        session.anchors = [
            _anchor("__talk__", "aaa", {"response": poem}),
        ]

        # Mock parser to return WriteNoteIntent with empty body
        # (simulates LLM not emitting ref:last)
        intent = WriteNoteIntent(title="dog_poem", body="")
        mock_parse = ParseResult(intent, [])

        with _ctx():
            with patch("kavi.agent.core.parse_intent", return_value=mock_parse):
                resp = handle_message(
                    "write this into dog_poem.md",
                    registry_path=FAKE_REGISTRY,
                    session=session,
                    confirmed=True,
                )
        # Should have auto-bound the body from the Talk anchor
        assert resp.records
        assert resp.records[0].success
        assert resp.records[0].input_json["body"] == poem
