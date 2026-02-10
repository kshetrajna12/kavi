"""Tests for execution replay (consumer/replay.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kavi.consumer.replay import ReplayError, _find_record, replay_execution
from kavi.consumer.shim import ExecutionRecord
from kavi.skills.loader import TrustError

# ── Fixtures ──────────────────────────────────────────────────────────

FAKE_REGISTRY = Path("/fake/registry.yaml")

RECORD_OK = ExecutionRecord(
    execution_id="aaa111bbb222ccc333ddd444eee55500",
    parent_execution_id=None,
    skill_name="summarize_note",
    source_hash="deadbeefdeadbeefdeadbeefdeadbeef",
    side_effect_class="READ_ONLY",
    input_json={"path": "notes/ml.md", "style": "bullet"},
    output_json={"summary": "ML stuff"},
    success=True,
    error=None,
    started_at="2025-07-01T00:00:00+00:00",
    finished_at="2025-07-01T00:00:01+00:00",
)

REGISTRY_ENTRY = {
    "name": "summarize_note",
    "hash": "deadbeefdeadbeefdeadbeefdeadbeef",
    "side_effect_class": "READ_ONLY",
    "version": "1.0.0",
    "module_path": "kavi.skills.summarize_note.SummarizeNoteSkill",
}


def _write_log(path: Path, records: list[ExecutionRecord]) -> None:
    """Write records to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(rec.model_dump_json() + "\n")


# ── _find_record ──────────────────────────────────────────────────────


def test_find_record_success(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    found = _find_record(RECORD_OK.execution_id, log)
    assert found.execution_id == RECORD_OK.execution_id
    assert found.skill_name == "summarize_note"


def test_find_record_not_found(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    with pytest.raises(ReplayError, match="Execution ID not found"):
        _find_record("nonexistent_id", log)


def test_find_record_missing_log(tmp_path: Path) -> None:
    log = tmp_path / "nonexistent.jsonl"

    with pytest.raises(ReplayError, match="Execution log not found"):
        _find_record("any_id", log)


def test_find_record_tolerates_malformed_lines(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write(RECORD_OK.model_dump_json() + "\n")
        f.write("{bad json too\n")

    found = _find_record(RECORD_OK.execution_id, log)
    assert found.execution_id == RECORD_OK.execution_id


# ── replay_execution: success ─────────────────────────────────────────


def test_replay_success(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    new_record = ExecutionRecord(
        skill_name="summarize_note",
        source_hash="deadbeefdeadbeefdeadbeefdeadbeef",
        side_effect_class="READ_ONLY",
        input_json={"path": "notes/ml.md", "style": "bullet"},
        output_json={"summary": "ML stuff replayed"},
        success=True,
        error=None,
        started_at="2025-07-02T00:00:00+00:00",
        finished_at="2025-07-02T00:00:01+00:00",
    )

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[REGISTRY_ENTRY]),
        patch("kavi.consumer.replay.load_skill"),
        patch("kavi.consumer.replay.consume_skill", return_value=new_record),
    ):
        original, replayed = replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )

    assert original.execution_id == RECORD_OK.execution_id
    assert replayed.parent_execution_id == RECORD_OK.execution_id
    assert replayed.execution_id != RECORD_OK.execution_id
    assert replayed.skill_name == "summarize_note"
    assert replayed.input_json == RECORD_OK.input_json


def test_replay_passes_exact_input(tmp_path: Path) -> None:
    """Replay must pass the original input_json, not invent inputs."""
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    captured_input = {}

    def fake_consume(registry, name, raw_input):  # noqa: ANN001, ANN202
        captured_input.update(raw_input)
        return ExecutionRecord(
            skill_name=name,
            source_hash="deadbeefdeadbeefdeadbeefdeadbeef",
            side_effect_class="READ_ONLY",
            input_json=raw_input,
            output_json={"summary": "ok"},
            success=True,
            error=None,
            started_at="2025-07-02T00:00:00+00:00",
            finished_at="2025-07-02T00:00:01+00:00",
        )

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[REGISTRY_ENTRY]),
        patch("kavi.consumer.replay.load_skill"),
        patch("kavi.consumer.replay.consume_skill", side_effect=fake_consume),
    ):
        replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )

    assert captured_input == {"path": "notes/ml.md", "style": "bullet"}


# ── replay_execution: hash mismatch ──────────────────────────────────


def test_replay_hash_mismatch(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    mismatched_entry = {**REGISTRY_ENTRY, "hash": "different_hash_value"}

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[mismatched_entry]),
        pytest.raises(ReplayError, match="Source hash mismatch"),
    ):
        replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )


# ── replay_execution: trust error ────────────────────────────────────


def test_replay_trust_error(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[REGISTRY_ENTRY]),
        patch("kavi.consumer.replay.load_skill", side_effect=TrustError("tampered")),
        pytest.raises(ReplayError, match="Trust verification failed"),
    ):
        replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )


# ── replay_execution: missing skill ──────────────────────────────────


def test_replay_skill_not_in_registry(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[]),
        pytest.raises(ReplayError, match="not found in registry"),
    ):
        replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )


# ── replay_execution: missing execution_id ────────────────────────────


def test_replay_missing_execution_id(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    with pytest.raises(ReplayError, match="Execution ID not found"):
        replay_execution(
            "nonexistent_id",
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )


# ── replay does not mutate original ──────────────────────────────────


def test_replay_does_not_mutate_original(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    _write_log(log, [RECORD_OK])

    original_snapshot = RECORD_OK.model_dump()

    new_record = ExecutionRecord(
        skill_name="summarize_note",
        source_hash="deadbeefdeadbeefdeadbeefdeadbeef",
        side_effect_class="READ_ONLY",
        input_json={"path": "notes/ml.md", "style": "bullet"},
        output_json={"summary": "replayed"},
        success=True,
        error=None,
        started_at="2025-07-02T00:00:00+00:00",
        finished_at="2025-07-02T00:00:01+00:00",
    )

    with (
        patch("kavi.consumer.replay.list_skills", return_value=[REGISTRY_ENTRY]),
        patch("kavi.consumer.replay.load_skill"),
        patch("kavi.consumer.replay.consume_skill", return_value=new_record),
    ):
        original, _ = replay_execution(
            RECORD_OK.execution_id,
            registry_path=FAKE_REGISTRY,
            log_path=log,
        )

    # Original record from log should be unchanged
    assert original.model_dump() == original_snapshot

    # On-disk log should be unchanged (replay_execution doesn't write)
    with open(log, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["execution_id"] == RECORD_OK.execution_id
