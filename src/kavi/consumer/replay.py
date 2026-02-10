"""Execution replay — re-run a past execution safely and audibly.

Loads an ExecutionRecord from the JSONL log, validates that the skill is
still TRUSTED and the source hash matches, then re-executes with the exact
same input.  Produces a new ExecutionRecord linked to the original via
parent_execution_id.

Does NOT import from forge, ledger, or policies.
"""

from __future__ import annotations

import json
from pathlib import Path

from kavi.consumer.log import DEFAULT_LOG_PATH
from kavi.consumer.shim import ExecutionRecord, consume_skill
from kavi.skills.loader import TrustError, list_skills, load_skill


class ReplayError(Exception):
    """Raised when replay cannot proceed."""


def _find_record(execution_id: str, log_path: Path) -> ExecutionRecord:
    """Scan the JSONL log for a record matching *execution_id*.

    Raises ReplayError if not found.
    """
    if not log_path.exists():
        raise ReplayError(f"Execution log not found: {log_path}")

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("execution_id") == execution_id:
                return ExecutionRecord(**data)

    raise ReplayError(f"Execution ID not found: {execution_id}")


def _validate_skill(
    registry_path: Path,
    record: ExecutionRecord,
) -> None:
    """Validate that the skill is still TRUSTED with a matching hash.

    Raises ReplayError on mismatch or missing skill.
    """
    # Check skill exists in registry
    entries = list_skills(registry_path)
    entry = None
    for e in entries:
        if e["name"] == record.skill_name:
            entry = e
            break

    if entry is None:
        raise ReplayError(
            f"Skill '{record.skill_name}' not found in registry. "
            "Cannot replay."
        )

    # Check hash matches what was recorded at original execution time
    registry_hash = entry.get("hash", "")
    if registry_hash and record.source_hash and registry_hash != record.source_hash:
        raise ReplayError(
            f"Source hash mismatch for '{record.skill_name}': "
            f"registry has {registry_hash[:12]}…, "
            f"original execution recorded {record.source_hash[:12]}…. "
            "Skill has changed since original execution."
        )

    # Verify trust (re-hash source file against registry)
    try:
        load_skill(registry_path, record.skill_name)
    except TrustError as e:
        raise ReplayError(f"Trust verification failed: {e}") from e
    except KeyError as e:
        raise ReplayError(
            f"Skill '{record.skill_name}' not loadable: {e}"
        ) from e


def replay_execution(
    execution_id: str,
    *,
    registry_path: Path,
    log_path: Path | None = None,
) -> tuple[ExecutionRecord, ExecutionRecord]:
    """Replay a past execution and return (original, new) records.

    The new record has a fresh execution_id and parent_execution_id set
    to the original.  The original record is never mutated.

    Raises ReplayError if the execution cannot be replayed.
    """
    effective_log = log_path or DEFAULT_LOG_PATH

    # 1. Find the original record
    original = _find_record(execution_id, effective_log)

    # 2. Validate skill is still trusted with matching hash
    _validate_skill(registry_path, original)

    # 3. Re-execute with the exact same input
    new_record = consume_skill(
        registry_path,
        original.skill_name,
        original.input_json,
    )

    # 4. Link to original
    new_record.parent_execution_id = original.execution_id

    return original, new_record
