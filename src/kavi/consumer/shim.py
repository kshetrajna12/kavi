"""Consumer shim — load, validate, execute trusted skills with auditable records.

This module provides the runtime interface for downstream systems that consume
the trusted skill registry. It does NOT plan, select, or compose skills — it
executes exactly one named skill with validated input and returns a structured
ExecutionRecord capturing provenance.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kavi.skills.loader import list_skills, load_skill


class SkillInfo(BaseModel):
    """Metadata about a trusted skill, including I/O schemas."""

    name: str
    description: str
    side_effect_class: str
    version: str
    source_hash: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class ExecutionRecord(BaseModel):
    """Auditable record of a single skill execution."""

    skill_name: str
    source_hash: str
    side_effect_class: str
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None
    success: bool
    error: str | None
    started_at: str
    finished_at: str


def get_trusted_skills(registry_path: Path) -> list[SkillInfo]:
    """Load all trusted skills from registry and return structured metadata.

    Each skill is loaded (with trust verification) so that I/O schemas
    can be extracted from the Pydantic input/output models.
    """
    entries = list_skills(registry_path)
    result = []
    for entry in entries:
        skill = load_skill(registry_path, entry["name"])
        result.append(
            SkillInfo(
                name=entry["name"],
                description=entry.get("description", ""),
                side_effect_class=entry.get("side_effect_class", ""),
                version=entry.get("version", ""),
                source_hash=entry.get("hash", ""),
                input_schema=skill.input_model.model_json_schema(),
                output_schema=skill.output_model.model_json_schema(),
            )
        )
    return result


def consume_skill(
    registry_path: Path,
    skill_name: str,
    raw_input: dict[str, Any],
) -> ExecutionRecord:
    """Execute a trusted skill and return an auditable ExecutionRecord.

    Steps:
    1. Look up metadata (hash, side-effect class) from registry.
    2. Load the skill with trust verification.
    3. Validate input and execute via the skill's validate_and_run.
    4. Capture output, timing, and status in an ExecutionRecord.

    Never raises — all failures are captured in the returned record.
    """
    started_at = _now_iso()

    # Look up metadata from registry before loading
    entries = list_skills(registry_path)
    source_hash = ""
    side_effect_class = ""
    for entry in entries:
        if entry["name"] == skill_name:
            source_hash = entry.get("hash", "")
            side_effect_class = entry.get("side_effect_class", "")
            break

    try:
        skill = load_skill(registry_path, skill_name)
    except Exception as e:
        return ExecutionRecord(
            skill_name=skill_name,
            source_hash=source_hash,
            side_effect_class=side_effect_class,
            input_json=raw_input,
            output_json=None,
            success=False,
            error=f"{type(e).__name__}: {e}",
            started_at=started_at,
            finished_at=_now_iso(),
        )

    # Use skill instance's side_effect_class if registry didn't have it
    if not side_effect_class:
        side_effect_class = skill.side_effect_class

    try:
        output = skill.validate_and_run(raw_input)
    except Exception as e:
        return ExecutionRecord(
            skill_name=skill_name,
            source_hash=source_hash,
            side_effect_class=side_effect_class,
            input_json=raw_input,
            output_json=None,
            success=False,
            error=f"{type(e).__name__}: {e}",
            started_at=started_at,
            finished_at=_now_iso(),
        )

    return ExecutionRecord(
        skill_name=skill_name,
        source_hash=source_hash,
        side_effect_class=side_effect_class,
        input_json=raw_input,
        output_json=output,
        success=True,
        error=None,
        started_at=started_at,
        finished_at=_now_iso(),
    )


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.datetime.now(datetime.UTC).isoformat()
