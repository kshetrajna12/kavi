"""Append-only JSONL execution log for consumer shim provenance."""

from __future__ import annotations

import json
import os
from pathlib import Path

from kavi.consumer.shim import ExecutionRecord

DEFAULT_LOG_PATH = Path.home() / ".kavi" / "executions.jsonl"


class ExecutionLogWriter:
    """Appends ExecutionRecords to a JSONL file.

    - Creates parent directories if missing.
    - Uses open+append+fsync for atomic-ish writes.
    - Tolerates malformed existing lines (append-only, never reads back).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_LOG_PATH

    def append(self, record: ExecutionRecord) -> None:
        """Serialize and append one record as a single JSONL line."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json() + "\n"
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def read_execution_log(
    path: Path | None = None,
    *,
    n: int = 20,
    only_failures: bool = False,
    skill_name: str | None = None,
) -> list[ExecutionRecord]:
    """Read and filter execution records from a JSONL log.

    Tolerates malformed lines (skips them silently).
    Returns up to *n* most recent matching records (newest last).
    """
    log_path = path or DEFAULT_LOG_PATH
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
                rec = ExecutionRecord(**data)
            except Exception:  # noqa: BLE001
                continue

            if only_failures and rec.success:
                continue
            if skill_name and rec.skill_name != skill_name:
                continue
            records.append(rec)

    # Return the last n records (newest last, since file is append-only)
    return records[-n:]
