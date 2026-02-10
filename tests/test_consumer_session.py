"""Tests for session view (consumer/session.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kavi.consumer.session import (
    SessionError,
    _format_duration,
    build_session,
    get_latest_execution,
    render_session_tree,
)
from kavi.consumer.shim import ExecutionRecord

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_record(
    execution_id: str,
    skill_name: str = "stub_skill",
    parent_execution_id: str | None = None,
    success: bool = True,
    error: str | None = None,
    started_at: str = "2025-07-01T00:00:00+00:00",
    finished_at: str = "2025-07-01T00:00:01+00:00",
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=execution_id,
        parent_execution_id=parent_execution_id,
        skill_name=skill_name,
        source_hash="deadbeef",
        side_effect_class="READ_ONLY",
        input_json={"key": "val"},
        output_json={"result": "ok"} if success else None,
        success=success,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
    )


def _write_log(path: Path, records: list[ExecutionRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(rec.model_dump_json() + "\n")


# ── _format_duration ──────────────────────────────────────────────────


def test_format_duration_milliseconds() -> None:
    result = _format_duration(
        "2025-07-01T00:00:00+00:00",
        "2025-07-01T00:00:00.500000+00:00",
    )
    assert result == "500ms"


def test_format_duration_seconds() -> None:
    result = _format_duration(
        "2025-07-01T00:00:00+00:00",
        "2025-07-01T00:00:02.500000+00:00",
    )
    assert result == "2.5s"


def test_format_duration_minutes() -> None:
    result = _format_duration(
        "2025-07-01T00:00:00+00:00",
        "2025-07-01T00:02:30+00:00",
    )
    assert result == "2m30s"


def test_format_duration_invalid() -> None:
    assert _format_duration("bad", "data") == "?"


# ── build_session: single step ────────────────────────────────────────


def test_build_session_single_step(tmp_path: Path) -> None:
    rec = _make_record("aaa")
    log = tmp_path / "exec.jsonl"
    _write_log(log, [rec])

    result = build_session("aaa", log_path=log)
    assert len(result) == 1
    assert result[0].execution_id == "aaa"


# ── build_session: two-step chain ─────────────────────────────────────


def test_build_session_two_step_chain(tmp_path: Path) -> None:
    parent = _make_record(
        "aaa", skill_name="search_notes",
        started_at="2025-07-01T00:00:00+00:00",
    )
    child = _make_record(
        "bbb", skill_name="summarize_note",
        parent_execution_id="aaa",
        started_at="2025-07-01T00:00:01+00:00",
    )
    log = tmp_path / "exec.jsonl"
    _write_log(log, [parent, child])

    # Query from child → should find both
    result = build_session("bbb", log_path=log)
    assert len(result) == 2
    assert result[0].execution_id == "aaa"
    assert result[1].execution_id == "bbb"

    # Query from parent → should also find both
    result2 = build_session("aaa", log_path=log)
    assert len(result2) == 2


# ── build_session: replay child shows under original ──────────────────


def test_build_session_replay_child(tmp_path: Path) -> None:
    """A replayed execution (parent_execution_id set) appears as a child."""
    original = _make_record(
        "orig", skill_name="summarize_note",
        started_at="2025-07-01T00:00:00+00:00",
    )
    replay = _make_record(
        "replay", skill_name="summarize_note",
        parent_execution_id="orig",
        started_at="2025-07-01T00:01:00+00:00",
    )
    log = tmp_path / "exec.jsonl"
    _write_log(log, [original, replay])

    result = build_session("replay", log_path=log)
    assert len(result) == 2
    assert result[0].execution_id == "orig"
    assert result[1].execution_id == "replay"


# ── build_session: branching ──────────────────────────────────────────


def test_build_session_branching(tmp_path: Path) -> None:
    """Multiple children of the same parent form a branching tree."""
    root = _make_record(
        "root", skill_name="search_notes",
        started_at="2025-07-01T00:00:00+00:00",
    )
    child_a = _make_record(
        "child_a", skill_name="summarize_note",
        parent_execution_id="root",
        started_at="2025-07-01T00:00:01+00:00",
    )
    child_b = _make_record(
        "child_b", skill_name="write_note",
        parent_execution_id="root",
        started_at="2025-07-01T00:00:02+00:00",
    )
    log = tmp_path / "exec.jsonl"
    _write_log(log, [root, child_a, child_b])

    result = build_session("child_b", log_path=log)
    assert len(result) == 3
    ids = [r.execution_id for r in result]
    assert "root" in ids
    assert "child_a" in ids
    assert "child_b" in ids


# ── build_session: missing execution_id ───────────────────────────────


def test_build_session_missing_id(tmp_path: Path) -> None:
    rec = _make_record("aaa")
    log = tmp_path / "exec.jsonl"
    _write_log(log, [rec])

    with pytest.raises(SessionError, match="Execution ID not found"):
        build_session("nonexistent", log_path=log)


def test_build_session_empty_log(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")

    with pytest.raises(SessionError, match="No execution records"):
        build_session("any", log_path=log)


def test_build_session_missing_log(tmp_path: Path) -> None:
    log = tmp_path / "nonexistent.jsonl"

    with pytest.raises(SessionError, match="No execution records"):
        build_session("any", log_path=log)


# ── get_latest_execution ──────────────────────────────────────────────


def test_get_latest_execution(tmp_path: Path) -> None:
    rec1 = _make_record("aaa", started_at="2025-07-01T00:00:00+00:00")
    rec2 = _make_record("bbb", started_at="2025-07-01T00:00:01+00:00")
    log = tmp_path / "exec.jsonl"
    _write_log(log, [rec1, rec2])

    assert get_latest_execution(log_path=log) == "bbb"


def test_get_latest_execution_empty(tmp_path: Path) -> None:
    log = tmp_path / "exec.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")

    with pytest.raises(SessionError, match="No execution records"):
        get_latest_execution(log_path=log)


# ── render_session_tree ───────────────────────────────────────────────


def test_render_session_tree_empty() -> None:
    assert render_session_tree([]) == "Session: (empty)"


def test_render_session_tree_single() -> None:
    rec = _make_record("aaa", skill_name="search_notes")
    output = render_session_tree([rec])

    assert "Session:" in output
    assert "search_notes" in output
    assert "\u2705" in output  # checkmark
    assert "aaa" in output  # short id


def test_render_session_tree_chain() -> None:
    parent = _make_record(
        "aaa", skill_name="search_notes",
        started_at="2025-07-01T00:00:00+00:00",
    )
    child = _make_record(
        "bbb", skill_name="summarize_note",
        parent_execution_id="aaa",
        started_at="2025-07-01T00:00:01+00:00",
    )
    output = render_session_tree([parent, child])

    assert "search_notes" in output
    assert "summarize_note" in output
    # Child should be more indented than parent
    lines = output.split("\n")
    search_line = [ln for ln in lines if "search_notes" in ln][0]
    summarize_line = [ln for ln in lines if "summarize_note" in ln][0]
    assert len(summarize_line) - len(summarize_line.lstrip()) > (
        len(search_line) - len(search_line.lstrip())
    )


def test_render_session_tree_failure() -> None:
    rec = _make_record(
        "aaa", skill_name="write_note",
        success=False, error="needs confirmation",
    )
    output = render_session_tree([rec])

    assert "\u274c" in output  # cross mark
    assert "needs confirmation" in output


def test_render_session_tree_long_error_truncated() -> None:
    long_error = "x" * 200
    rec = _make_record("aaa", success=False, error=long_error)
    output = render_session_tree([rec])

    # Error should be truncated with ...
    assert "..." in output
    # Should not contain the full 200-char error
    error_line = [ln for ln in output.split("\n") if "\u274c" in ln][0]
    assert len(error_line) < 250


def test_render_session_tree_duration_shown() -> None:
    rec = _make_record(
        "aaa",
        started_at="2025-07-01T00:00:00+00:00",
        finished_at="2025-07-01T00:00:02.500000+00:00",
    )
    output = render_session_tree([rec])
    assert "2.5s" in output


# ── Isolation: unrelated records excluded ─────────────────────────────


def test_build_session_excludes_unrelated(tmp_path: Path) -> None:
    """Records not in the same chain are excluded."""
    chain_a = _make_record("aaa", started_at="2025-07-01T00:00:00+00:00")
    chain_b = _make_record("bbb", started_at="2025-07-01T00:00:01+00:00")
    log = tmp_path / "exec.jsonl"
    _write_log(log, [chain_a, chain_b])

    result = build_session("aaa", log_path=log)
    assert len(result) == 1
    assert result[0].execution_id == "aaa"
