"""Tests for Kavi Chat v0 — AgentCore, parser, planner."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from kavi.agent.core import handle_message
from kavi.agent.models import (
    AgentResponse,
    ChainAction,
    SearchAndSummarizeIntent,
    SkillAction,
    SummarizeNoteIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.agent.parser import parse_intent
from kavi.agent.planner import intent_to_plan
from kavi.consumer.shim import SkillInfo
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

# ── Skill stubs ──────────────────────────────────────────────────────


class SearchInput(SkillInput):
    query: str
    top_k: int = 5


class SearchResult(BaseModel):
    path: str
    score: float
    title: str | None = None
    snippet: str | None = None


class SearchOutput(SkillOutput):
    query: str
    results: list[SearchResult]
    truncated_paths: list[str] = []
    used_model: str = "test"
    error: str | None = None


class SummarizeInput(SkillInput):
    path: str
    style: str = "bullet"


class SummarizeOutput(SkillOutput):
    path: str
    summary: str
    key_points: list[str]
    truncated: bool = False
    used_model: str = "test"
    error: str | None = None


class WriteInput(SkillInput):
    title: str
    body: str


class WriteOutput(SkillOutput):
    path: str
    title: str


class SearchSkill(BaseSkill):
    name = "search_notes"
    description = "Search notes by embedding similarity"
    input_model = SearchInput
    output_model = SearchOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, SearchInput)
        return SearchOutput(
            query=input_data.query,
            results=[
                SearchResult(
                    path="notes/ml.md", score=0.95, title="ML Notes",
                ),
                SearchResult(
                    path="notes/python.md", score=0.80, title="Python",
                ),
            ],
        )


class SummarizeSkill(BaseSkill):
    name = "summarize_note"
    description = "Summarize a note"
    input_model = SummarizeInput
    output_model = SummarizeOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, SummarizeInput)
        return SummarizeOutput(
            path=input_data.path,
            summary="A summary of the note.",
            key_points=["point 1", "point 2"],
        )


class WriteSkill(BaseSkill):
    name = "write_note"
    description = "Write a note to vault"
    input_model = WriteInput
    output_model = WriteOutput
    side_effect_class = "FILE_WRITE"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, WriteInput)
        return WriteOutput(
            path=f"vault/Inbox/AI/{input_data.title}.md",
            title=input_data.title,
        )


# ── Registry stubs ───────────────────────────────────────────────────

ENTRIES = [
    {
        "name": "search_notes",
        "description": "Search notes",
        "side_effect_class": "READ_ONLY",
        "version": "1.0.0",
        "hash": "aaa",
        "module_path": "fake.SearchSkill",
    },
    {
        "name": "summarize_note",
        "description": "Summarize",
        "side_effect_class": "READ_ONLY",
        "version": "1.0.0",
        "hash": "bbb",
        "module_path": "fake.SummarizeSkill",
    },
    {
        "name": "write_note",
        "description": "Write note",
        "side_effect_class": "FILE_WRITE",
        "version": "1.0.0",
        "hash": "ccc",
        "module_path": "fake.WriteSkill",
    },
]


def _make_info(name, desc, sec, shash, in_cls, out_cls):
    return SkillInfo(
        name=name,
        description=desc,
        side_effect_class=sec,
        version="1.0.0",
        source_hash=shash,
        input_schema=in_cls.model_json_schema(),
        output_schema=out_cls.model_json_schema(),
    )


SKILL_INFOS = [
    _make_info(
        "search_notes", "Search", "READ_ONLY",
        "aaa", SearchInput, SearchOutput,
    ),
    _make_info(
        "summarize_note", "Summarize", "READ_ONLY",
        "bbb", SummarizeInput, SummarizeOutput,
    ),
    _make_info(
        "write_note", "Write note", "FILE_WRITE",
        "ccc", WriteInput, WriteOutput,
    ),
]

FAKE_REGISTRY = Path("/fake/registry.yaml")


def _load_skill_stub(registry_path: Path, name: str) -> BaseSkill:
    skills = {
        "search_notes": SearchSkill,
        "summarize_note": SummarizeSkill,
        "write_note": WriteSkill,
    }
    if name in skills:
        return skills[name]()
    raise KeyError(f"Skill '{name}' not found")


_GEN = "kavi.agent.parser.generate"


def _ctx(llm_return=None, llm_error=None):
    """Return ExitStack context patching consumer + optional LLM."""
    stack = ExitStack()
    stack.enter_context(
        patch("kavi.consumer.shim.list_skills", return_value=ENTRIES),
    )
    stack.enter_context(
        patch(
            "kavi.consumer.shim.load_skill",
            side_effect=_load_skill_stub,
        ),
    )
    stack.enter_context(
        patch(
            "kavi.consumer.chain.get_trusted_skills",
            return_value=SKILL_INFOS,
        ),
    )
    stack.enter_context(
        patch(
            "kavi.agent.core.get_trusted_skills",
            return_value=SKILL_INFOS,
        ),
    )
    if llm_error is not None:
        stack.enter_context(
            patch(_GEN, side_effect=llm_error),
        )
    elif llm_return is not None:
        stack.enter_context(
            patch(_GEN, return_value=llm_return),
        )
    return stack


# ── Parser tests ─────────────────────────────────────────────────────


class TestParserLLMSuccess:
    """parse_intent with mocked Sparkstation returning valid JSON."""

    def test_search_and_summarize(self) -> None:
        resp = {
            "kind": "search_and_summarize",
            "query": "machine learning",
            "top_k": 3,
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent = parse_intent(
                "find notes about machine learning", SKILL_INFOS,
            )
        assert isinstance(intent, SearchAndSummarizeIntent)
        assert intent.query == "machine learning"
        assert intent.top_k == 3

    def test_summarize_note(self) -> None:
        resp = {"kind": "summarize_note", "path": "notes/ml.md"}
        with patch(_GEN, return_value=json.dumps(resp)):
            intent = parse_intent("summarize notes/ml.md", SKILL_INFOS)
        assert isinstance(intent, SummarizeNoteIntent)
        assert intent.path == "notes/ml.md"

    def test_write_note(self) -> None:
        resp = {
            "kind": "write_note",
            "title": "Test",
            "body": "Hello world",
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent = parse_intent(
                "write a note called Test", SKILL_INFOS,
            )
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "Test"
        assert intent.body == "Hello world"

    def test_unsupported(self) -> None:
        resp = {"kind": "unsupported", "message": "Not supported"}
        with patch(_GEN, return_value=json.dumps(resp)):
            intent = parse_intent("delete everything", SKILL_INFOS)
        assert isinstance(intent, UnsupportedIntent)

    def test_llm_returns_markdown_fenced_json(self) -> None:
        raw = (
            '```json\n'
            '{"kind": "summarize_note", "path": "a.md"}\n'
            '```'
        )
        with patch(_GEN, return_value=raw):
            intent = parse_intent("summarize a.md", SKILL_INFOS)
        assert isinstance(intent, SummarizeNoteIntent)
        assert intent.path == "a.md"


class TestParserDeterministic:
    """parse_intent with mode='deterministic' — explicit prefixes only."""

    def _parse(self, msg: str) -> object:
        return parse_intent(msg, SKILL_INFOS, mode="deterministic")

    def test_summarize_path(self) -> None:
        intent = self._parse("summarize notes/ml.md")
        assert isinstance(intent, SummarizeNoteIntent)
        assert intent.path == "notes/ml.md"

    def test_summarize_with_paragraph(self) -> None:
        intent = self._parse("summarize notes/ml.md paragraph")
        assert isinstance(intent, SummarizeNoteIntent)
        assert intent.style == "paragraph"

    def test_write_note(self) -> None:
        intent = self._parse("write My Title\nBody here")
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "My Title"
        assert intent.body == "Body here"

    def test_write_note_colon_syntax(self) -> None:
        intent = self._parse("write note: My Note\nBody text")
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "My Note"
        assert intent.body == "Body text"

    def test_search_query(self) -> None:
        intent = self._parse("search machine learning")
        assert isinstance(intent, SearchAndSummarizeIntent)
        assert intent.query == "machine learning"

    def test_find_query(self) -> None:
        intent = self._parse("find notes about python")
        assert isinstance(intent, SearchAndSummarizeIntent)
        assert intent.query == "python"

    def test_unsupported_message(self) -> None:
        intent = self._parse("do something random")
        assert isinstance(intent, UnsupportedIntent)
        assert "Available commands" in intent.message

    def test_ambiguous_input_not_executed(self) -> None:
        """Ambiguous text without a command prefix → UnsupportedIntent."""
        intent = self._parse("Kshetrajna Note")
        assert isinstance(intent, UnsupportedIntent)

    def test_bare_text_not_executed(self) -> None:
        """Bare sentence without command prefix → UnsupportedIntent."""
        intent = self._parse("notes about machine learning")
        assert isinstance(intent, UnsupportedIntent)

    def test_partial_prefix_not_matched(self) -> None:
        """'searching' is not 'search' — should not match."""
        intent = self._parse("searching for python notes")
        assert isinstance(intent, UnsupportedIntent)

    def test_spark_unavailable_triggers_fallback(self) -> None:
        from kavi.llm.spark import SparkUnavailableError

        err = SparkUnavailableError("down")
        with patch(_GEN, side_effect=err):
            intent = parse_intent(
                "summarize notes/ml.md", SKILL_INFOS,
            )
        assert isinstance(intent, SummarizeNoteIntent)

    def test_llm_bad_json_triggers_fallback(self) -> None:
        with patch(_GEN, return_value="not json at all"):
            intent = parse_intent(
                "summarize notes/ml.md", SKILL_INFOS,
            )
        assert isinstance(intent, SummarizeNoteIntent)


# ── Planner tests ────────────────────────────────────────────────────


class TestPlanner:
    def test_search_and_summarize_produces_chain(self) -> None:
        intent = SearchAndSummarizeIntent(query="ml", top_k=3)
        plan = intent_to_plan(intent)
        assert isinstance(plan, ChainAction)
        assert len(plan.chain.steps) == 2
        assert plan.chain.steps[0].skill_name == "search_notes"
        assert plan.chain.steps[1].skill_name == "summarize_note"

    def test_summarize_produces_skill_action(self) -> None:
        intent = SummarizeNoteIntent(path="notes/ml.md")
        plan = intent_to_plan(intent)
        assert isinstance(plan, SkillAction)
        assert plan.skill_name == "summarize_note"
        assert plan.input["path"] == "notes/ml.md"

    def test_write_produces_skill_action(self) -> None:
        intent = WriteNoteIntent(title="Test", body="Hello")
        plan = intent_to_plan(intent)
        assert isinstance(plan, SkillAction)
        assert plan.skill_name == "write_note"
        assert plan.input["title"] == "Test"

    def test_unsupported_returns_none(self) -> None:
        intent = UnsupportedIntent(message="nope")
        assert intent_to_plan(intent) is None

    def test_chain_max_two_steps(self) -> None:
        intent = SearchAndSummarizeIntent(query="anything", top_k=10)
        plan = intent_to_plan(intent)
        assert isinstance(plan, ChainAction)
        assert len(plan.chain.steps) <= 2


# ── AgentCore integration tests ──────────────────────────────────────


class TestHandleMessage:
    """Full pipeline: parse -> plan -> execute via mocked consumer."""

    def test_search_and_summarize_happy_path(self) -> None:
        llm = json.dumps({
            "kind": "search_and_summarize", "query": "ml",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "find ml notes", registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp, AgentResponse)
        assert isinstance(resp.intent, SearchAndSummarizeIntent)
        assert isinstance(resp.plan, ChainAction)
        assert len(resp.records) == 2
        assert all(r.success for r in resp.records)
        assert resp.error is None
        assert not resp.needs_confirmation

    def test_summarize_happy_path(self) -> None:
        llm = json.dumps({
            "kind": "summarize_note", "path": "notes/ml.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, SummarizeNoteIntent)
        assert isinstance(resp.plan, SkillAction)
        assert len(resp.records) == 1
        assert resp.records[0].success
        out = resp.records[0].output_json
        assert out["summary"] == "A summary of the note."

    def test_write_needs_confirmation_single_turn(self) -> None:
        """FILE_WRITE returns needs_confirmation when not confirmed."""
        llm = json.dumps({
            "kind": "write_note", "title": "Test", "body": "hi",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "write Test\nhi", registry_path=FAKE_REGISTRY,
            )
        assert resp.needs_confirmation is True
        assert resp.records == []
        assert resp.plan is not None

    def test_write_confirmed_executes(self) -> None:
        """With confirmed=True, FILE_WRITE executes normally."""
        llm = json.dumps({
            "kind": "write_note", "title": "Test", "body": "hi",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "write Test\nhi",
                registry_path=FAKE_REGISTRY,
                confirmed=True,
            )
        assert resp.needs_confirmation is False
        assert len(resp.records) == 1
        assert resp.records[0].success

    def test_unsupported_intent_returns_error(self) -> None:
        llm = json.dumps({
            "kind": "unsupported", "message": "Not supported",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "delete everything", registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, UnsupportedIntent)
        assert resp.error is not None
        assert resp.records == []
        assert resp.plan is None

    def test_response_always_has_intent(self) -> None:
        """AgentResponse always has a parsed intent, even on error."""
        llm = json.dumps({
            "kind": "unsupported", "message": "nope",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "gibberish", registry_path=FAKE_REGISTRY,
            )
        assert resp.intent is not None

    def test_response_serializes_to_json(self) -> None:
        """AgentResponse can round-trip through JSON."""
        llm = json.dumps({
            "kind": "summarize_note", "path": "a.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize a.md", registry_path=FAKE_REGISTRY,
            )
        data = json.loads(resp.model_dump_json())
        assert data["intent"]["kind"] == "summarize_note"
        assert data["plan"]["kind"] == "skill"
        assert len(data["records"]) == 1


class TestHandleMessageFallback:
    """Sparkstation unavailable — deterministic fallback path."""

    def test_fallback_summarize(self) -> None:
        from kavi.llm.spark import SparkUnavailableError

        err = SparkUnavailableError("down")
        with _ctx(llm_error=err):
            resp = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, SummarizeNoteIntent)
        assert len(resp.records) == 1
        assert resp.records[0].success

    def test_fallback_search(self) -> None:
        from kavi.llm.spark import SparkUnavailableError

        err = SparkUnavailableError("down")
        with _ctx(llm_error=err):
            resp = handle_message(
                "search machine learning",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, SearchAndSummarizeIntent)
        assert len(resp.records) == 2

    def test_fallback_unsupported(self) -> None:
        from kavi.llm.spark import SparkUnavailableError

        err = SparkUnavailableError("down")
        with _ctx(llm_error=err):
            resp = handle_message(
                "do something weird",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, UnsupportedIntent)
        assert resp.error is not None


class TestDeterministicParseMode:
    """handle_message with parse_mode='deterministic' (REPL mode)."""

    def test_ambiguous_input_returns_unsupported(self) -> None:
        """Ambiguous text in deterministic mode → error, no execution."""
        with _ctx():
            resp = handle_message(
                "Kshetrajna Note",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, UnsupportedIntent)
        assert resp.error is not None
        assert resp.records == []

    def test_deterministic_search_works(self) -> None:
        with _ctx():
            resp = handle_message(
                "search machine learning",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, SearchAndSummarizeIntent)
        assert len(resp.records) == 2

    def test_deterministic_summarize_works(self) -> None:
        with _ctx():
            resp = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, SummarizeNoteIntent)
        assert len(resp.records) == 1

    def test_write_empty_body_needs_confirmation(self) -> None:
        """Write with no body → needs_confirmation + helpful error."""
        with _ctx():
            resp = handle_message(
                "write My Title",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, WriteNoteIntent)
        assert resp.needs_confirmation is True
        assert resp.error is not None
        assert "body" in resp.error.lower()

    def test_write_with_body_needs_file_write_confirm(self) -> None:
        """Write with body still needs FILE_WRITE confirmation."""
        with _ctx():
            resp = handle_message(
                "write My Title\nSome body text",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, WriteNoteIntent)
        assert resp.needs_confirmation is True
        assert resp.records == []


class TestChainLengthEnforcement:
    """Ensure max 2 steps is enforced for chain plans."""

    def test_search_and_summarize_chain_is_two_steps(self) -> None:
        intent = SearchAndSummarizeIntent(query="test")
        plan = intent_to_plan(intent)
        assert isinstance(plan, ChainAction)
        assert len(plan.chain.steps) == 2


class TestExecutionLogging:
    """Verify records are logged when log_path is provided."""

    def test_records_logged_to_jsonl(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.jsonl"
        llm = json.dumps({
            "kind": "summarize_note", "path": "a.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize a.md",
                registry_path=FAKE_REGISTRY,
                log_path=log_file,
            )
        assert resp.records
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["skill_name"] == "summarize_note"

    def test_no_log_when_path_is_none(self) -> None:
        llm = json.dumps({
            "kind": "summarize_note", "path": "a.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize a.md",
                registry_path=FAKE_REGISTRY,
                log_path=None,
            )
        assert resp.records  # executed, just not logged
