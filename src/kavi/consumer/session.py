"""Session view — inspect an execution chain as a human-readable tree.

Reads execution records from the JSONL log and builds a session graph
using parent_execution_id linkage.  Renders a compact tree showing
skill names, success/failure, durations, and error messages.

Does NOT import from forge, ledger, or policies.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from kavi.consumer.log import DEFAULT_LOG_PATH
from kavi.consumer.shim import ExecutionRecord


class SessionError(Exception):
    """Raised when session view cannot be built."""


def _load_all_records(log_path: Path) -> list[ExecutionRecord]:
    """Load every valid record from the JSONL log."""
    if not log_path.exists():
        return []

    records: list[ExecutionRecord] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(ExecutionRecord(**data))
            except Exception:  # noqa: BLE001
                continue
    return records


def _format_duration(started_at: str, finished_at: str) -> str:
    """Compute and format duration between two ISO timestamps."""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
        delta = end - start
        total_ms = int(delta.total_seconds() * 1000)
        if total_ms < 1000:
            return f"{total_ms}ms"
        total_s = delta.total_seconds()
        if total_s < 60:
            return f"{total_s:.1f}s"
        minutes = int(total_s // 60)
        seconds = total_s % 60
        return f"{minutes}m{seconds:.0f}s"
    except (ValueError, TypeError):
        return "?"


def build_session(
    execution_id: str,
    *,
    log_path: Path | None = None,
) -> list[ExecutionRecord]:
    """Build the full session chain containing *execution_id*.

    Walks backward to the root (parent_execution_id=None), then
    collects all descendants forward.  Returns records ordered by
    started_at.

    Raises SessionError if execution_id is not found.
    """
    effective_log = log_path or DEFAULT_LOG_PATH
    all_records = _load_all_records(effective_log)

    if not all_records:
        raise SessionError(f"No execution records found in {effective_log}")

    # Index by execution_id
    by_id: dict[str, ExecutionRecord] = {}
    for rec in all_records:
        by_id[rec.execution_id] = rec

    if execution_id not in by_id:
        raise SessionError(f"Execution ID not found: {execution_id}")

    # Walk backward to root
    root_id = execution_id
    while by_id[root_id].parent_execution_id is not None:
        parent_id = by_id[root_id].parent_execution_id
        if parent_id not in by_id:
            break  # parent not in log, current is effective root
        root_id = parent_id

    # Build children index
    children: dict[str, list[str]] = {}
    for rec in all_records:
        if rec.parent_execution_id is not None:
            children.setdefault(rec.parent_execution_id, []).append(
                rec.execution_id,
            )

    # Walk forward from root (BFS/DFS to collect all descendants)
    session_ids: set[str] = set()
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        if current in session_ids:
            continue
        session_ids.add(current)
        for child_id in children.get(current, []):
            queue.append(child_id)

    # Filter and sort by started_at
    session_records = [
        rec for rec in all_records if rec.execution_id in session_ids
    ]
    session_records.sort(key=lambda r: r.started_at)

    return session_records


def get_latest_execution(
    *,
    log_path: Path | None = None,
) -> str:
    """Return the execution_id of the most recent record.

    Raises SessionError if the log is empty.
    """
    effective_log = log_path or DEFAULT_LOG_PATH
    all_records = _load_all_records(effective_log)
    if not all_records:
        raise SessionError(f"No execution records found in {effective_log}")
    return all_records[-1].execution_id


def render_session_tree(records: list[ExecutionRecord]) -> str:
    """Render a session as a compact, indented tree.

    Returns a human-readable string.
    """
    if not records:
        return "Session: (empty)"

    # Build parent→children mapping
    children: dict[str | None, list[ExecutionRecord]] = {}
    for rec in records:
        children.setdefault(rec.parent_execution_id, []).append(rec)

    # Sort children by started_at
    for kid_list in children.values():
        kid_list.sort(key=lambda r: r.started_at)

    lines: list[str] = ["Session:"]

    def _render_node(rec: ExecutionRecord, depth: int) -> None:
        indent = "  " * (depth + 1)
        marker = "\u2705" if rec.success else "\u274c"
        short_id = rec.execution_id[:12]
        duration = _format_duration(rec.started_at, rec.finished_at)
        line = f"{indent}{rec.skill_name} {marker}  (id={short_id}\u2026)  [{duration}]"
        if not rec.success and rec.error:
            # Truncate long error messages
            err_msg = rec.error
            if len(err_msg) > 80:
                err_msg = err_msg[:77] + "..."
            line += f"  {err_msg}"
        lines.append(line)

        for child in children.get(rec.execution_id, []):
            _render_node(child, depth + 1)

    # Find roots (records whose parent is not in the session)
    record_ids = {r.execution_id for r in records}
    roots = [
        r for r in records
        if r.parent_execution_id is None
        or r.parent_execution_id not in record_ids
    ]
    roots.sort(key=lambda r: r.started_at)

    for root in roots:
        _render_node(root, 0)

    return "\n".join(lines)
