"""Tests for the consumer shim."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from kavi.consumer.shim import ExecutionRecord, SkillInfo, consume_skill, get_trusted_skills
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput
from kavi.skills.loader import TrustError

# ── Stubs ──────────────────────────────────────────────────────────────


class StubInput(SkillInput):
    value: str


class StubOutput(SkillOutput):
    result: str


class StubSkill(BaseSkill):
    name = "stub_skill"
    description = "A test stub skill"
    input_model = StubInput
    output_model = StubOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: BaseModel) -> BaseModel:
        assert isinstance(input_data, StubInput)
        return StubOutput(result=f"processed: {input_data.value}")


STUB_ENTRY = {
    "name": "stub_skill",
    "description": "A test stub skill",
    "side_effect_class": "READ_ONLY",
    "version": "1.0.0",
    "hash": "abc123",
    "module_path": "fake.module.StubSkill",
    "input_model": "fake.module.StubInput",
    "output_model": "fake.module.StubOutput",
}

FAKE_REGISTRY = Path("/fake/registry.yaml")


# ── get_trusted_skills ─────────────────────────────────────────────────


def test_get_trusted_skills_returns_skill_info() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", return_value=StubSkill()),
    ):
        result = get_trusted_skills(FAKE_REGISTRY)

    assert len(result) == 1
    info = result[0]
    assert isinstance(info, SkillInfo)
    assert info.name == "stub_skill"
    assert info.side_effect_class == "READ_ONLY"
    assert info.version == "1.0.0"
    assert info.source_hash == "abc123"
    assert "properties" in info.input_schema
    assert "value" in info.input_schema["properties"]
    assert "properties" in info.output_schema
    assert "result" in info.output_schema["properties"]


def test_get_trusted_skills_empty_registry() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[]),
        patch("kavi.consumer.shim.load_skill"),
    ):
        result = get_trusted_skills(FAKE_REGISTRY)

    assert result == []


# ── consume_skill: success ─────────────────────────────────────────────


def test_consume_skill_success() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", return_value=StubSkill()),
    ):
        record = consume_skill(FAKE_REGISTRY, "stub_skill", {"value": "hello"})

    assert record.success is True
    assert record.error is None
    assert record.output_json == {"result": "processed: hello"}
    assert record.skill_name == "stub_skill"
    assert record.source_hash == "abc123"
    assert record.side_effect_class == "READ_ONLY"


def test_consume_skill_success_has_timestamps() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", return_value=StubSkill()),
    ):
        record = consume_skill(FAKE_REGISTRY, "stub_skill", {"value": "test"})

    started = datetime.fromisoformat(record.started_at)
    finished = datetime.fromisoformat(record.finished_at)
    assert finished >= started


# ── consume_skill: schema validation failure ───────────────────────────


def test_consume_skill_schema_validation_failure() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", return_value=StubSkill()),
    ):
        record = consume_skill(FAKE_REGISTRY, "stub_skill", {"wrong_field": "hello"})

    assert record.success is False
    assert record.output_json is None
    assert record.error is not None
    assert "ValidationError" in record.error


# ── consume_skill: trust error ─────────────────────────────────────────


def test_consume_skill_trust_error() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", side_effect=TrustError("hash mismatch")),
    ):
        record = consume_skill(FAKE_REGISTRY, "stub_skill", {"value": "hello"})

    assert record.success is False
    assert record.output_json is None
    assert "hash mismatch" in (record.error or "")
    assert record.source_hash == "abc123"


# ── consume_skill: skill not found ─────────────────────────────────────


def test_consume_skill_not_found() -> None:
    with (
        patch("kavi.consumer.shim.list_skills", return_value=[]),
        patch("kavi.consumer.shim.load_skill", side_effect=KeyError("no_such_skill")),
    ):
        record = consume_skill(FAKE_REGISTRY, "no_such_skill", {"value": "hello"})

    assert record.success is False
    assert record.output_json is None
    assert record.error is not None
    assert record.source_hash == ""
    assert record.side_effect_class == ""


# ── consume_skill: execution error ─────────────────────────────────────


def test_consume_skill_execution_error() -> None:
    class FailingSkill(StubSkill):
        def execute(self, input_data: BaseModel) -> BaseModel:
            raise RuntimeError("boom")

    with (
        patch("kavi.consumer.shim.list_skills", return_value=[STUB_ENTRY]),
        patch("kavi.consumer.shim.load_skill", return_value=FailingSkill()),
    ):
        record = consume_skill(FAKE_REGISTRY, "stub_skill", {"value": "hello"})

    assert record.success is False
    assert record.output_json is None
    assert "RuntimeError" in (record.error or "")
    assert "boom" in (record.error or "")


# ── ExecutionRecord serialization ──────────────────────────────────────


def test_execution_record_round_trips_json() -> None:
    record = ExecutionRecord(
        skill_name="test",
        source_hash="deadbeef",
        side_effect_class="READ_ONLY",
        input_json={"key": "value"},
        output_json={"result": "ok"},
        success=True,
        error=None,
        started_at="2025-01-01T00:00:00+00:00",
        finished_at="2025-01-01T00:00:01+00:00",
    )
    data = record.model_dump()
    restored = ExecutionRecord(**data)
    assert restored == record
