"""Tests for Kavi Chat v0 — AgentCore, parser, planner."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from kavi.agent.constants import CHAT_DEFAULT_ALLOWED_EFFECTS
from kavi.agent.core import handle_message
from kavi.agent.models import (
    AgentResponse,
    ChainAction,
    HelpIntent,
    ParsedIntent,
    SearchAndSummarizeIntent,
    SkillAction,
    SkillInvocationIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.agent.parser import _is_help_request, parse_intent
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


class ReadByTagInput(SkillInput):
    tag: str


class ReadByTagOutput(SkillOutput):
    notes: list[dict[str, str]]
    count: int


class HttpGetInput(SkillInput):
    url: str
    allowed_hosts: list[str]


class HttpGetOutput(SkillOutput):
    url: str
    status_code: int = 200
    data: dict[str, Any] | None = None
    error: str | None = None


class WriteInput(SkillInput):
    path: str
    title: str
    body: str


class WriteOutput(SkillOutput):
    written_path: str
    title: str


class ReadByTagSkill(BaseSkill):
    name = "read_notes_by_tag"
    description = "Read notes by tag"
    input_model = ReadByTagInput
    output_model = ReadByTagOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, ReadByTagInput)
        return ReadByTagOutput(
            notes=[{"path": "notes/cooking.md", "title": "Cooking"}],
            count=1,
        )


class HttpGetSkill(BaseSkill):
    name = "http_get_json"
    description = "Fetch JSON from URL"
    input_model = HttpGetInput
    output_model = HttpGetOutput
    side_effect_class = "NETWORK"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, HttpGetInput)
        return HttpGetOutput(
            url=input_data.url,
            data={"result": "ok"},
        )


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
            written_path=f"vault_out/{input_data.path}",
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
    {
        "name": "read_notes_by_tag",
        "description": "Read notes by tag",
        "side_effect_class": "READ_ONLY",
        "version": "1.0.0",
        "hash": "ddd",
        "module_path": "fake.ReadByTagSkill",
    },
    {
        "name": "http_get_json",
        "description": "Fetch JSON from URL",
        "side_effect_class": "NETWORK",
        "version": "1.0.0",
        "hash": "eee",
        "module_path": "fake.HttpGetSkill",
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
    _make_info(
        "read_notes_by_tag", "Read notes by tag", "READ_ONLY",
        "ddd", ReadByTagInput, ReadByTagOutput,
    ),
    _make_info(
        "http_get_json", "Fetch JSON from URL", "NETWORK",
        "eee", HttpGetInput, HttpGetOutput,
    ),
]

FAKE_REGISTRY = Path("/fake/registry.yaml")

# Allowed effects including NETWORK — for tests that need it
_ALL_EFFECTS = frozenset({"READ_ONLY", "FILE_WRITE", "NETWORK", "SECRET_READ"})


def _load_skill_stub(registry_path: Path, name: str) -> BaseSkill:
    skills = {
        "search_notes": SearchSkill,
        "summarize_note": SummarizeSkill,
        "write_note": WriteSkill,
        "read_notes_by_tag": ReadByTagSkill,
        "http_get_json": HttpGetSkill,
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
            intent, warnings = parse_intent(
                "find notes about machine learning", SKILL_INFOS,
            )
        assert isinstance(intent, SearchAndSummarizeIntent)
        assert intent.query == "machine learning"
        assert intent.top_k == 3
        assert warnings == []

    def test_summarize_note_backward_compat(self) -> None:
        """LLM returning summarize_note kind → SkillInvocationIntent."""
        resp = {"kind": "summarize_note", "path": "notes/ml.md"}
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, _ = parse_intent(
                "summarize notes/ml.md", SKILL_INFOS,
            )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"
        assert intent.input["path"] == "notes/ml.md"

    def test_write_note(self) -> None:
        resp = {
            "kind": "write_note",
            "title": "Test",
            "body": "Hello world",
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, _ = parse_intent(
                "write a note called Test", SKILL_INFOS,
            )
        assert isinstance(intent, WriteNoteIntent)
        assert intent.title == "Test"
        assert intent.body == "Hello world"

    def test_unsupported(self) -> None:
        resp = {"kind": "unsupported", "message": "Not supported"}
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, _ = parse_intent(
                "delete everything", SKILL_INFOS,
            )
        assert isinstance(intent, UnsupportedIntent)

    def test_llm_returns_markdown_fenced_json(self) -> None:
        """LLM returning summarize_note in fences → SkillInvocationIntent."""
        raw = (
            '```json\n'
            '{"kind": "summarize_note", "path": "a.md"}\n'
            '```'
        )
        with patch(_GEN, return_value=raw):
            intent, _ = parse_intent(
                "summarize a.md", SKILL_INFOS,
            )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"
        assert intent.input["path"] == "a.md"

    def test_llm_warnings_propagated(self) -> None:
        """LLM returns warnings for trailing intents."""
        resp = {
            "kind": "search_and_summarize",
            "query": "architecture",
            "warnings": [
                "Ignored: write_note is not part of "
                "search_and_summarize. Ask separately.",
            ],
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, warnings = parse_intent(
                "search architecture then write a note",
                SKILL_INFOS,
            )
        assert isinstance(intent, SearchAndSummarizeIntent)
        assert len(warnings) == 1
        assert "write_note" in warnings[0]

    def test_no_warnings_field_means_empty_list(self) -> None:
        """LLM omits warnings → empty list, not error."""
        resp = {"kind": "summarize_note", "path": "a.md"}
        with patch(_GEN, return_value=json.dumps(resp)):
            _, warnings = parse_intent(
                "summarize a.md", SKILL_INFOS,
            )
        assert warnings == []


class TestParserDeterministic:
    """parse_intent with mode='deterministic' — explicit prefixes only."""

    def _parse(self, msg: str) -> ParsedIntent:
        intent, _ = parse_intent(msg, SKILL_INFOS, mode="deterministic")
        return intent

    def test_summarize_path(self) -> None:
        intent = self._parse("summarize notes/ml.md")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"
        assert intent.input["path"] == "notes/ml.md"

    def test_summarize_with_paragraph(self) -> None:
        intent = self._parse("summarize notes/ml.md paragraph")
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input["style"] == "paragraph"

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
            intent, warnings = parse_intent(
                "summarize notes/ml.md", SKILL_INFOS,
            )
        # Falls back to deterministic → SkillInvocationIntent
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"
        assert warnings == []

    def test_llm_bad_json_triggers_fallback(self) -> None:
        with patch(_GEN, return_value="not json at all"):
            intent, _ = parse_intent(
                "summarize notes/ml.md", SKILL_INFOS,
            )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "summarize_note"


class TestParserSkillInvocation:
    """Tests for the generic SkillInvocationIntent path."""

    def test_llm_returns_skill_invocation(self) -> None:
        resp = {
            "kind": "skill_invocation",
            "skill_name": "read_notes_by_tag",
            "input": {"tag": "cooking"},
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, _ = parse_intent(
                "show me notes tagged cooking", SKILL_INFOS,
            )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "read_notes_by_tag"
        assert intent.input == {"tag": "cooking"}

    def test_llm_returns_http_get_json(self) -> None:
        resp = {
            "kind": "skill_invocation",
            "skill_name": "http_get_json",
            "input": {
                "url": "https://api.example.com/data",
                "allowed_hosts": ["api.example.com"],
            },
        }
        with patch(_GEN, return_value=json.dumps(resp)):
            intent, _ = parse_intent(
                "get data from api.example.com", SKILL_INFOS,
            )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "http_get_json"

    def test_deterministic_generic_skill_name_json(self) -> None:
        """Typing a skill name + JSON works."""
        intent, _ = parse_intent(
            'read_notes_by_tag {"tag": "ml"}', SKILL_INFOS,
            mode="deterministic",
        )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "read_notes_by_tag"
        assert intent.input == {"tag": "ml"}

    def test_deterministic_generic_skill_non_json(self) -> None:
        """Non-JSON rest goes into {"query": rest}."""
        intent, _ = parse_intent(
            "read_notes_by_tag cooking", SKILL_INFOS,
            mode="deterministic",
        )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.input == {"query": "cooking"}

    def test_deterministic_http_get_json_explicit(self) -> None:
        """http_get_json requires explicit skill_name + JSON form."""
        intent, _ = parse_intent(
            'http_get_json {"url": "https://api.example.com", '
            '"allowed_hosts": ["api.example.com"]}',
            SKILL_INFOS,
            mode="deterministic",
        )
        assert isinstance(intent, SkillInvocationIntent)
        assert intent.skill_name == "http_get_json"
        assert intent.input["url"] == "https://api.example.com"

    def test_unsupported_lists_all_skills(self) -> None:
        """Help text includes all skill names."""
        intent, _ = parse_intent(
            "do something weird", SKILL_INFOS, mode="deterministic",
        )
        assert isinstance(intent, UnsupportedIntent)
        assert "http_get_json" in intent.message
        assert "read_notes_by_tag" in intent.message


# ── Planner tests ────────────────────────────────────────────────────


class TestPlanner:
    def test_search_and_summarize_produces_chain(self) -> None:
        intent = SearchAndSummarizeIntent(query="ml", top_k=3)
        plan = intent_to_plan(intent)
        assert isinstance(plan, ChainAction)
        assert len(plan.chain.steps) == 2
        assert plan.chain.steps[0].skill_name == "search_notes"
        assert plan.chain.steps[1].skill_name == "summarize_note"

    def test_summarize_via_skill_invocation(self) -> None:
        """summarize_note goes through SkillInvocationIntent now."""
        intent = SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "notes/ml.md", "style": "bullet"},
        )
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
        assert plan.input["path"] == "Inbox/AI/Test.md"

    def test_skill_invocation_produces_skill_action(self) -> None:
        intent = SkillInvocationIntent(
            skill_name="read_notes_by_tag", input={"tag": "ml"},
        )
        plan = intent_to_plan(intent)
        assert isinstance(plan, SkillAction)
        assert plan.skill_name == "read_notes_by_tag"
        assert plan.input == {"tag": "ml"}

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
        """summarize_note via backward-compat → SkillInvocationIntent."""
        llm = json.dumps({
            "kind": "summarize_note", "path": "notes/ml.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize notes/ml.md",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, SkillInvocationIntent)
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
        assert data["intent"]["kind"] == "skill_invocation"
        assert data["plan"]["kind"] == "skill"
        assert len(data["records"]) == 1
        assert data["warnings"] == []

    def test_warnings_propagated_to_response(self) -> None:
        """LLM parser warnings appear on AgentResponse.warnings."""
        llm = json.dumps({
            "kind": "search_and_summarize",
            "query": "arch",
            "warnings": ["Ignored: write_note. Ask separately."],
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "search arch then write a note",
                registry_path=FAKE_REGISTRY,
            )
        assert resp.warnings == ["Ignored: write_note. Ask separately."]
        assert isinstance(resp.intent, SearchAndSummarizeIntent)
        assert resp.error is None

    def test_no_warnings_by_default(self) -> None:
        llm = json.dumps({
            "kind": "summarize_note", "path": "a.md",
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "summarize a.md", registry_path=FAKE_REGISTRY,
            )
        assert resp.warnings == []

    def test_read_notes_by_tag_auto_executes(self) -> None:
        """READ_ONLY skill invocation executes without confirmation."""
        llm = json.dumps({
            "kind": "skill_invocation",
            "skill_name": "read_notes_by_tag",
            "input": {"tag": "cooking"},
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "notes tagged cooking", registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, SkillInvocationIntent)
        assert isinstance(resp.plan, SkillAction)
        assert resp.needs_confirmation is False
        assert len(resp.records) == 1
        assert resp.records[0].success
        assert resp.records[0].output_json["count"] == 1


# ── Chat policy tests ────────────────────────────────────────────────


class TestChatPolicy:
    """Chat policy gates skills by side_effect_class."""

    def test_network_blocked_by_default(self) -> None:
        """NETWORK skills are blocked under default chat policy."""
        llm = json.dumps({
            "kind": "skill_invocation",
            "skill_name": "http_get_json",
            "input": {
                "url": "https://api.example.com/data",
                "allowed_hosts": ["api.example.com"],
            },
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "get data from api",
                registry_path=FAKE_REGISTRY,
            )
        assert resp.error is not None
        assert "chat policy" in resp.error
        assert "NETWORK" in resp.error
        assert resp.records == []

    def test_network_allowed_with_explicit_effects(self) -> None:
        """NETWORK skill works when allowed_effects includes NETWORK."""
        llm = json.dumps({
            "kind": "skill_invocation",
            "skill_name": "http_get_json",
            "input": {
                "url": "https://api.example.com/data",
                "allowed_hosts": ["api.example.com"],
            },
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "get data from api",
                registry_path=FAKE_REGISTRY,
                confirmed=True,
                allowed_effects=_ALL_EFFECTS,
            )
        assert resp.error is None
        assert len(resp.records) == 1
        assert resp.records[0].success

    def test_network_still_needs_confirmation(self) -> None:
        """Even with allowed_effects, NETWORK needs confirmation."""
        llm = json.dumps({
            "kind": "skill_invocation",
            "skill_name": "http_get_json",
            "input": {
                "url": "https://api.example.com/data",
                "allowed_hosts": ["api.example.com"],
            },
        })
        with _ctx(llm_return=llm):
            resp = handle_message(
                "get data from api",
                registry_path=FAKE_REGISTRY,
                confirmed=False,
                allowed_effects=_ALL_EFFECTS,
            )
        assert resp.needs_confirmation is True
        assert resp.records == []

    def test_read_only_allowed_by_default(self) -> None:
        """READ_ONLY is in default allowed effects."""
        assert "READ_ONLY" in CHAT_DEFAULT_ALLOWED_EFFECTS

    def test_file_write_allowed_by_default(self) -> None:
        """FILE_WRITE is in default allowed effects."""
        assert "FILE_WRITE" in CHAT_DEFAULT_ALLOWED_EFFECTS

    def test_network_not_in_default_allowed(self) -> None:
        """NETWORK is NOT in default allowed effects."""
        assert "NETWORK" not in CHAT_DEFAULT_ALLOWED_EFFECTS

    def test_secret_read_not_in_default_allowed(self) -> None:
        """SECRET_READ is NOT in default allowed effects."""
        assert "SECRET_READ" not in CHAT_DEFAULT_ALLOWED_EFFECTS


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
        assert isinstance(resp.intent, SkillInvocationIntent)
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
        assert isinstance(resp.intent, SkillInvocationIntent)
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


# ── HelpIntent parser tests ─────────────────────────────────────────


class TestHelpPatterns:
    """_is_help_request matches help/skills/capabilities patterns."""

    def test_help(self) -> None:
        assert _is_help_request("help")

    def test_skills(self) -> None:
        assert _is_help_request("skills")

    def test_commands(self) -> None:
        assert _is_help_request("commands")

    def test_what_can_you_do(self) -> None:
        assert _is_help_request("what can you do")

    def test_what_can_you_do_question_mark(self) -> None:
        assert _is_help_request("what can you do?")

    def test_capabilities(self) -> None:
        assert _is_help_request("capabilities")

    def test_list_skills(self) -> None:
        assert _is_help_request("list skills")

    def test_show_skills(self) -> None:
        assert _is_help_request("show skills")

    def test_negative_help_me_write(self) -> None:
        assert not _is_help_request("help me write")

    def test_negative_search_skills(self) -> None:
        assert not _is_help_request("search skills")

    def test_negative_write_help_doc(self) -> None:
        assert not _is_help_request("write help doc")

    def test_negative_bare_text(self) -> None:
        assert not _is_help_request("notes about machine learning")


class TestHelpIntentDeterministic:
    """Deterministic parser returns HelpIntent for help patterns."""

    def _parse(self, msg: str) -> ParsedIntent:
        intent, _ = parse_intent(msg, SKILL_INFOS, mode="deterministic")
        return intent

    def test_help(self) -> None:
        assert isinstance(self._parse("help"), HelpIntent)

    def test_skills(self) -> None:
        assert isinstance(self._parse("skills"), HelpIntent)

    def test_what_can_you_do(self) -> None:
        assert isinstance(self._parse("what can you do?"), HelpIntent)

    def test_commands(self) -> None:
        assert isinstance(self._parse("commands"), HelpIntent)


class TestHelpIntentLLM:
    """LLM parser returns HelpIntent when LLM emits kind=help."""

    def test_llm_help_kind(self) -> None:
        llm = json.dumps({"kind": "help"})
        with patch(_GEN, return_value=llm):
            intent, _ = parse_intent("what can you do", SKILL_INFOS)
        assert isinstance(intent, HelpIntent)


class TestHelpIntentPlanner:
    """Planner returns None for HelpIntent (handled by core)."""

    def test_returns_none(self) -> None:
        assert intent_to_plan(HelpIntent()) is None


# ── HelpIntent integration tests ────────────────────────────────────


class TestHandleMessageHelp:
    """handle_message returns help_text for HelpIntent."""

    def test_help_returns_help_text(self) -> None:
        with _ctx():
            resp = handle_message(
                "help",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert isinstance(resp.intent, HelpIntent)
        assert resp.help_text is not None
        assert "Available skills" in resp.help_text
        assert resp.records == []
        assert resp.plan is None
        assert resp.error is None

    def test_help_text_contains_skill_names(self) -> None:
        with _ctx():
            resp = handle_message(
                "skills",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert resp.help_text is not None
        assert "search_notes" in resp.help_text
        assert "write_note" in resp.help_text
        assert "http_get_json" in resp.help_text

    def test_help_text_shows_policy_groups(self) -> None:
        with _ctx():
            resp = handle_message(
                "help",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert resp.help_text is not None
        assert "auto-execute" in resp.help_text
        assert "confirmation" in resp.help_text
        assert "Blocked" in resp.help_text

    def test_help_no_confirmation_needed(self) -> None:
        with _ctx():
            resp = handle_message(
                "help",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        assert resp.needs_confirmation is False

    def test_help_via_llm_mode(self) -> None:
        llm = json.dumps({"kind": "help"})
        with _ctx(llm_return=llm):
            resp = handle_message(
                "what can you do",
                registry_path=FAKE_REGISTRY,
            )
        assert isinstance(resp.intent, HelpIntent)
        assert resp.help_text is not None

    def test_help_serializes_to_json(self) -> None:
        with _ctx():
            resp = handle_message(
                "help",
                registry_path=FAKE_REGISTRY,
                parse_mode="deterministic",
            )
        data = json.loads(resp.model_dump_json())
        assert data["intent"]["kind"] == "help"
        assert data["help_text"] is not None
        assert data["records"] == []
