"""Tests for the deterministic skill chain executor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from kavi.consumer.chain import (
    ChainOptions,
    ChainSpec,
    ChainStep,
    FieldMapping,
    consume_chain,
    extract_path,
)
from kavi.consumer.log import ExecutionLogWriter
from kavi.consumer.shim import SkillInfo
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

# ── Stubs ─────────────────────────────────────────────────────────────


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


class SearchSkill(BaseSkill):
    name = "search_notes"
    description = "Search notes"
    input_model = SearchInput
    output_model = SearchOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, SearchInput)
        return SearchOutput(
            query=input_data.query,
            results=[
                SearchResult(path="notes/ml.md", score=0.95, title="ML Notes"),
                SearchResult(path="notes/python.md", score=0.80, title="Python"),
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


SEARCH_ENTRY = {
    "name": "search_notes",
    "description": "Search notes",
    "side_effect_class": "READ_ONLY",
    "version": "1.0.0",
    "hash": "aaa111",
    "module_path": "fake.SearchSkill",
}
SUMMARIZE_ENTRY = {
    "name": "summarize_note",
    "description": "Summarize a note",
    "side_effect_class": "READ_ONLY",
    "version": "1.0.0",
    "hash": "bbb222",
    "module_path": "fake.SummarizeSkill",
}
ENTRIES = [SEARCH_ENTRY, SUMMARIZE_ENTRY]
FAKE_REGISTRY = Path("/fake/registry.yaml")

SEARCH_INFO = SkillInfo(
    name="search_notes",
    description="Search notes",
    side_effect_class="READ_ONLY",
    version="1.0.0",
    source_hash="aaa111",
    input_schema=SearchInput.model_json_schema(),
    output_schema=SearchOutput.model_json_schema(),
)
SUMMARIZE_INFO = SkillInfo(
    name="summarize_note",
    description="Summarize a note",
    side_effect_class="READ_ONLY",
    version="1.0.0",
    source_hash="bbb222",
    input_schema=SummarizeInput.model_json_schema(),
    output_schema=SummarizeOutput.model_json_schema(),
)


def _load_skill_side_effect(registry_path: Path, name: str) -> BaseSkill:
    """Return the right stub skill based on name."""
    if name == "search_notes":
        return SearchSkill()
    if name == "summarize_note":
        return SummarizeSkill()
    raise KeyError(f"Skill '{name}' not found")


def _patch_chain():
    """Context manager patching loader + get_trusted_skills for chain tests."""
    return (
        patch("kavi.consumer.shim.list_skills", return_value=ENTRIES),
        patch("kavi.consumer.shim.load_skill", side_effect=_load_skill_side_effect),
        patch(
            "kavi.consumer.chain.get_trusted_skills",
            return_value=[SEARCH_INFO, SUMMARIZE_INFO],
        ),
    )


# ── extract_path ──────────────────────────────────────────────────────


def test_extract_path_simple_field() -> None:
    assert extract_path({"foo": "bar"}, "foo") == "bar"


def test_extract_path_nested() -> None:
    assert extract_path({"a": {"b": {"c": 42}}}, "a.b.c") == 42


def test_extract_path_list_index() -> None:
    data = {"results": [{"path": "a.md"}, {"path": "b.md"}]}
    assert extract_path(data, "results.0.path") == "a.md"
    assert extract_path(data, "results.1.path") == "b.md"


def test_extract_path_missing_key() -> None:
    import pytest

    with pytest.raises(KeyError, match="missing key 'x'"):
        extract_path({"a": 1}, "x")


def test_extract_path_index_out_of_range() -> None:
    import pytest

    with pytest.raises(KeyError, match="index 5 out of range"):
        extract_path({"results": [{"path": "a.md"}]}, "results.5.path")


def test_extract_path_non_int_index_on_list() -> None:
    import pytest

    with pytest.raises(KeyError, match="expected integer index"):
        extract_path({"results": [1, 2]}, "results.foo")


# ── Happy path: search → summarize ───────────────────────────────────


def test_happy_path_search_then_summarize() -> None:
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={"query": "machine learning", "top_k": 3},
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 2
    assert records[0].success is True
    assert records[0].skill_name == "search_notes"
    assert records[1].success is True
    assert records[1].skill_name == "summarize_note"
    # Verify mapped input was "notes/ml.md" (results.0.path from search output)
    assert records[1].input_json["path"] == "notes/ml.md"
    assert records[1].output_json is not None
    assert records[1].output_json["summary"] == "A summary of the note."


# ── Verify 1: Mapping extraction failure — no skill invocation ───────


def test_mapping_extraction_failure_no_invocation() -> None:
    """Mapping path that doesn't exist produces FAILURE without invoking skill."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={"query": "test"},
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    # Only 2 results exist (index 0 and 1), index 5 is out of range
                    FieldMapping(to_field="path", from_path="results.5.path"),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 2
    assert records[0].success is True
    assert records[1].success is False
    assert records[1].output_json is None
    # Error should mention the bad path and source step
    assert "results.5.path" in (records[1].error or "")
    assert "step 0" in (records[1].error or "")


def test_mapping_missing_key_failure() -> None:
    """Mapping references a key that doesn't exist in output."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={"query": "test"},
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="nonexistent_field"),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 2
    assert records[1].success is False
    assert "nonexistent_field" in (records[1].error or "")


# ── Verify 2: Schema validation gate ─────────────────────────────────


def test_schema_validation_wrong_type() -> None:
    """Mapped value has wrong type → fails before execution."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="summarize_note",
                input={"path": 123},  # should be str
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 1
    assert records[0].success is False
    assert records[0].output_json is None
    assert "schema validation" in (records[0].error or "").lower() or \
           "ValidationError" in (records[0].error or "")


def test_schema_validation_missing_required() -> None:
    """Missing required field in input → fails before execution."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={},  # 'query' is required
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 1
    assert records[0].success is False
    assert "query" in (records[0].error or "").lower()


# ── Verify 3: Lineage correctness ────────────────────────────────────


def test_parent_execution_id_defaults_to_previous() -> None:
    """Step i's parent_execution_id = step i-1's execution_id by default."""
    spec = ChainSpec(
        steps=[
            ChainStep(skill_name="search_notes", input={"query": "test"}),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert records[0].parent_execution_id is None
    assert records[1].parent_execution_id == records[0].execution_id


def test_parent_index_override() -> None:
    """parent_index overrides the default previous-step linkage."""
    spec = ChainSpec(
        steps=[
            ChainStep(skill_name="search_notes", input={"query": "first"}),
            ChainStep(skill_name="search_notes", input={"query": "second"}),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(
                        to_field="path",
                        from_path="results.0.path",
                        from_step_index=0,  # reference step 0, not step 1
                    ),
                ],
                parent_index=0,  # parent is step 0, not step 1
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 3
    assert records[2].parent_execution_id == records[0].execution_id


def test_first_step_has_no_parent() -> None:
    """The first step in a chain should have no parent_execution_id."""
    spec = ChainSpec(
        steps=[
            ChainStep(skill_name="search_notes", input={"query": "test"}),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert records[0].parent_execution_id is None


# ── Verify 4: Stop-on-failure semantics ──────────────────────────────


def test_stop_on_failure_true_halts_chain() -> None:
    """With stop_on_failure=true, chain stops after first failed step."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={},  # missing required 'query' → will fail
            ),
            ChainStep(
                skill_name="summarize_note",
                input={"path": "test.md"},
            ),
        ],
        options=ChainOptions(stop_on_failure=True),
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    # Only one record — chain halted after first failure
    assert len(records) == 1
    assert records[0].success is False


def test_stop_on_failure_false_continues() -> None:
    """With stop_on_failure=false, subsequent steps run even after failure."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={},  # missing required 'query' → will fail
            ),
            ChainStep(
                skill_name="search_notes",
                input={"query": "real query"},  # independent, should succeed
            ),
        ],
        options=ChainOptions(stop_on_failure=False),
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 2
    assert records[0].success is False
    assert records[1].success is True


def test_stop_on_failure_false_mapping_from_failed_step() -> None:
    """With stop_on_failure=false, mapping from a failed step fails cleanly."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={},  # will fail — missing 'query'
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
        options=ChainOptions(stop_on_failure=False),
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 2
    assert records[0].success is False
    assert records[1].success is False
    assert "failed" in (records[1].error or "").lower()


# ── JSONL logging ─────────────────────────────────────────────────────


def test_chain_records_log_to_jsonl(tmp_path: Path) -> None:
    """Both records are written in order to the JSONL log."""
    spec = ChainSpec(
        steps=[
            ChainStep(skill_name="search_notes", input={"query": "test"}),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    log_file = tmp_path / "chain.jsonl"
    writer = ExecutionLogWriter(log_file)
    for rec in records:
        writer.append(rec)

    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    r1 = json.loads(lines[1])
    assert r0["skill_name"] == "search_notes"
    assert r1["skill_name"] == "summarize_note"
    assert r1["parent_execution_id"] == r0["execution_id"]


# ── ChainSpec serialization ──────────────────────────────────────────


def test_chain_spec_round_trips_json() -> None:
    """ChainSpec can be serialized to JSON and deserialized back."""
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={"query": "test"},
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "bullet"},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
        options=ChainOptions(stop_on_failure=True),
    )

    data = spec.model_dump()
    restored = ChainSpec(**data)
    assert restored == spec

    # Also test from JSON string (how CLI will use it)
    json_str = spec.model_dump_json()
    restored2 = ChainSpec.model_validate_json(json_str)
    assert restored2 == spec


# ── Edge cases ────────────────────────────────────────────────────────


def test_empty_chain_returns_empty() -> None:
    spec = ChainSpec(steps=[])

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert records == []


def test_from_step_index_cross_reference() -> None:
    """from_step_index allows referencing a non-adjacent step."""
    spec = ChainSpec(
        steps=[
            ChainStep(skill_name="search_notes", input={"query": "first"}),
            ChainStep(skill_name="search_notes", input={"query": "second"}),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": "paragraph"},
                from_prev=[
                    FieldMapping(
                        to_field="path",
                        from_path="results.1.path",
                        from_step_index=0,
                    ),
                ],
            ),
        ],
    )

    p1, p2, p3 = _patch_chain()
    with p1, p2, p3:
        records = consume_chain(FAKE_REGISTRY, spec)

    assert len(records) == 3
    assert all(r.success for r in records)
    # Step 2 should have mapped results.1.path from step 0 → "notes/python.md"
    assert records[2].input_json["path"] == "notes/python.md"
