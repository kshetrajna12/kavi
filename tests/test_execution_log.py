"""Tests for execution log persistence (JSONL) and tail-executions filtering."""

from __future__ import annotations

from pathlib import Path

from kavi.consumer.log import ExecutionLogWriter, read_execution_log
from kavi.consumer.shim import ExecutionRecord


def _make_record(
    *,
    skill_name: str = "test_skill",
    success: bool = True,
    error: str | None = None,
    execution_id: str | None = None,
    parent_execution_id: str | None = None,
) -> ExecutionRecord:
    kwargs: dict = dict(
        skill_name=skill_name,
        source_hash="deadbeef",
        side_effect_class="READ_ONLY",
        input_json={"key": "value"},
        output_json={"result": "ok"} if success else None,
        success=success,
        error=error,
        started_at="2025-01-01T00:00:00+00:00",
        finished_at="2025-01-01T00:00:01+00:00",
    )
    if execution_id is not None:
        kwargs["execution_id"] = execution_id
    if parent_execution_id is not None:
        kwargs["parent_execution_id"] = parent_execution_id
    return ExecutionRecord(**kwargs)


# ── ExecutionRecord new fields ────────────────────────────────────────


def test_execution_record_has_execution_id() -> None:
    rec = _make_record()
    assert rec.execution_id
    assert len(rec.execution_id) == 32  # uuid4 hex


def test_execution_record_parent_id_defaults_none() -> None:
    rec = _make_record()
    assert rec.parent_execution_id is None


def test_execution_record_parent_id_set() -> None:
    rec = _make_record(parent_execution_id="parent123")
    assert rec.parent_execution_id == "parent123"


def test_execution_record_round_trips_with_new_fields() -> None:
    rec = _make_record(execution_id="abc123", parent_execution_id="parent456")
    data = rec.model_dump()
    restored = ExecutionRecord(**data)
    assert restored == rec
    assert restored.execution_id == "abc123"
    assert restored.parent_execution_id == "parent456"


# ── ExecutionLogWriter ────────────────────────────────────────────────


def test_log_writer_creates_dirs(tmp_path: Path) -> None:
    log_path = tmp_path / "deep" / "nested" / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record())
    assert log_path.exists()


def test_log_writer_appends_multiple(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(execution_id="aaa"))
    writer.append(_make_record(execution_id="bbb"))
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2


def test_log_writer_success_and_failure(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(success=True))
    writer.append(_make_record(success=False, error="RuntimeError: boom"))
    records = read_execution_log(log_path, n=100)
    assert len(records) == 2
    assert records[0].success is True
    assert records[1].success is False
    assert records[1].error == "RuntimeError: boom"


# ── read_execution_log ────────────────────────────────────────────────


def test_read_empty_log(tmp_path: Path) -> None:
    log_path = tmp_path / "nonexistent.jsonl"
    records = read_execution_log(log_path)
    assert records == []


def test_read_log_round_trip(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    original = _make_record(execution_id="round_trip_id")
    writer.append(original)
    records = read_execution_log(log_path)
    assert len(records) == 1
    assert records[0] == original


def test_read_log_tolerates_malformed(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(execution_id="good1"))
    # Inject malformed line
    with open(log_path, "a") as f:
        f.write("NOT VALID JSON\n")
        f.write('{"partial": true}\n')
    writer.append(_make_record(execution_id="good2"))
    records = read_execution_log(log_path, n=100)
    assert len(records) == 2
    assert records[0].execution_id == "good1"
    assert records[1].execution_id == "good2"


def test_read_log_n_limit(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    for i in range(10):
        writer.append(_make_record(execution_id=f"id_{i:03d}"))
    records = read_execution_log(log_path, n=3)
    assert len(records) == 3
    # Should be the last 3
    assert records[0].execution_id == "id_007"
    assert records[2].execution_id == "id_009"


def test_read_log_only_failures(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(execution_id="ok1", success=True))
    writer.append(_make_record(execution_id="fail1", success=False, error="err"))
    writer.append(_make_record(execution_id="ok2", success=True))
    writer.append(_make_record(execution_id="fail2", success=False, error="err"))
    records = read_execution_log(log_path, n=100, only_failures=True)
    assert len(records) == 2
    assert all(not r.success for r in records)


def test_read_log_filter_by_skill(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(skill_name="alpha"))
    writer.append(_make_record(skill_name="beta"))
    writer.append(_make_record(skill_name="alpha"))
    records = read_execution_log(log_path, n=100, skill_name="alpha")
    assert len(records) == 2
    assert all(r.skill_name == "alpha" for r in records)


def test_read_log_combined_filters(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    writer.append(_make_record(skill_name="alpha", success=True))
    writer.append(_make_record(skill_name="alpha", success=False, error="err"))
    writer.append(_make_record(skill_name="beta", success=False, error="err"))
    writer.append(_make_record(skill_name="alpha", success=False, error="err"))
    records = read_execution_log(log_path, n=100, only_failures=True, skill_name="alpha")
    assert len(records) == 2
    assert all(r.skill_name == "alpha" and not r.success for r in records)


# ── Schema round-trip through JSONL ───────────────────────────────────


def test_schema_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    log_path = tmp_path / "executions.jsonl"
    writer = ExecutionLogWriter(log_path)
    original = ExecutionRecord(
        execution_id="specific_id",
        parent_execution_id="parent_id",
        skill_name="write_note",
        source_hash="abc123def456",
        side_effect_class="FILE_WRITE",
        input_json={"title": "hello", "body": "world", "tags": ["a", "b"]},
        output_json={"path": "/vault/hello.md"},
        success=True,
        error=None,
        started_at="2025-06-15T10:30:00+00:00",
        finished_at="2025-06-15T10:30:01+00:00",
    )
    writer.append(original)
    records = read_execution_log(log_path)
    assert len(records) == 1
    assert records[0] == original
