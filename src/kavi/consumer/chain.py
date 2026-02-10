"""Deterministic skill chain executor with schema-validated input mapping.

Runs a fixed sequence of skill steps. Each step produces an ExecutionRecord.
Input for downstream steps can be mapped from prior step outputs using
dot-path extraction. No LLM planning or auto-mapping — purely deterministic.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kavi.consumer.shim import ExecutionRecord, consume_skill, get_trusted_skills

# ── Data models ───────────────────────────────────────────────────────


class FieldMapping(BaseModel):
    """Map a value from a prior step's output into the current step's input.

    Attributes:
        to_field: Target field name in the current step's input.
        from_path: Dotted path into the source step's output_json
                   (e.g. "results.0.path", "summary").
        from_step_index: Index of the source step.  If None, defaults to
                         the immediately previous step (i - 1).
    """

    to_field: str
    from_path: str
    from_step_index: int | None = None


class ChainStep(BaseModel):
    """One step in a deterministic execution chain.

    Provide EITHER ``input`` (full JSON) OR ``input_template`` + ``from_prev``.
    """

    skill_name: str
    input: dict[str, Any] | None = None
    input_template: dict[str, Any] | None = None
    from_prev: list[FieldMapping] | None = None
    parent_index: int | None = None


class ChainOptions(BaseModel):
    """Execution options for the chain."""

    stop_on_failure: bool = True


class ChainSpec(BaseModel):
    """A deterministic chain of skill invocations with explicit input mapping."""

    steps: list[ChainStep]
    options: ChainOptions = Field(default_factory=ChainOptions)


# ── Dot-path extraction ──────────────────────────────────────────────


def extract_path(data: dict[str, Any], dotted_path: str) -> Any:
    """Extract a value from *data* using a dotted path.

    Supports dict keys and integer list indexes:
        "field"             → data["field"]
        "field.subfield"    → data["field"]["subfield"]
        "results.0.path"    → data["results"][0]["path"]

    Raises ``KeyError`` with a descriptive message on any failure.
    """
    parts = dotted_path.split(".")
    current: Any = data
    for i, part in enumerate(parts):
        traversed = ".".join(parts[: i + 1])
        if isinstance(current, dict):
            if part not in current:
                msg = f"missing key '{part}' at '{traversed}'"
                raise KeyError(msg)
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                msg = f"expected integer index at '{traversed}', got '{part}'"
                raise KeyError(msg)
            if idx < 0 or idx >= len(current):
                msg = f"index {idx} out of range (length {len(current)}) at '{traversed}'"
                raise KeyError(msg)
            current = current[idx]
        else:
            msg = f"cannot traverse into {type(current).__name__} at '{traversed}'"
            raise KeyError(msg)
    return current


# ── Input resolution ─────────────────────────────────────────────────


def _resolve_input(
    step: ChainStep,
    step_index: int,
    records: list[ExecutionRecord],
    skill_schemas: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the concrete input dict for *step*.

    Returns ``(input_dict, None)`` on success or ``(None, error_msg)`` on failure.
    """
    # Case A: full input provided directly
    if step.input is not None:
        resolved = step.input
    # Case B: template + mappings
    elif step.input_template is not None:
        resolved = copy.deepcopy(step.input_template)
        for mapping in step.from_prev or []:
            src_idx = (
                mapping.from_step_index
                if mapping.from_step_index is not None
                else step_index - 1
            )
            if src_idx < 0 or src_idx >= len(records):
                return None, (
                    f"mapping references step {src_idx} but only "
                    f"{len(records)} steps have executed"
                )
            src_record = records[src_idx]
            if not src_record.success or src_record.output_json is None:
                return None, (
                    f"mapping references step {src_idx} "
                    f"({src_record.skill_name}) which failed"
                )
            try:
                value = extract_path(src_record.output_json, mapping.from_path)
            except KeyError as exc:
                return None, (
                    f"mapping '{mapping.from_path}' from step {src_idx} "
                    f"({src_record.skill_name}): {exc}"
                )
            resolved[mapping.to_field] = value
    else:
        # No input at all — pass empty dict (skill validation will catch missing fields)
        resolved = {}

    # Schema validation against the skill's declared input model
    schema = skill_schemas.get(step.skill_name)
    if schema is not None:
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for field_name in required:
            if field_name not in resolved:
                return None, (
                    f"schema validation failed for '{step.skill_name}': "
                    f"missing required field '{field_name}'"
                )
        for field_name, value in resolved.items():
            if field_name in properties:
                prop = properties[field_name]
                prop_type = prop.get("type")
                if prop_type == "string" and not isinstance(value, str):
                    return None, (
                        f"schema validation failed for '{step.skill_name}': "
                        f"field '{field_name}' expected string, got {type(value).__name__}"
                    )
                if prop_type == "integer" and not isinstance(value, int):
                    return None, (
                        f"schema validation failed for '{step.skill_name}': "
                        f"field '{field_name}' expected integer, got {type(value).__name__}"
                    )

    return resolved, None


# ── Chain executor ───────────────────────────────────────────────────


def _make_failure_record(
    skill_name: str,
    input_json: dict[str, Any] | None,
    error: str,
    parent_execution_id: str | None,
) -> ExecutionRecord:
    """Build a FAILURE ExecutionRecord without invoking the skill."""
    import datetime
    import uuid

    now = datetime.datetime.now(datetime.UTC).isoformat()
    return ExecutionRecord(
        execution_id=uuid.uuid4().hex,
        parent_execution_id=parent_execution_id,
        skill_name=skill_name,
        source_hash="",
        side_effect_class="",
        input_json=input_json or {},
        output_json=None,
        success=False,
        error=error,
        started_at=now,
        finished_at=now,
    )


def consume_chain(
    registry_path: Path,
    spec: ChainSpec,
) -> list[ExecutionRecord]:
    """Execute a deterministic chain of skills with mapped inputs.

    Steps run sequentially. Each step produces an ExecutionRecord.
    Input mapping between steps uses explicit dot-path extraction.

    Returns the list of ExecutionRecords (one per step attempted).
    """
    # Pre-load skill schemas for validation
    skill_schemas: dict[str, dict[str, Any]] = {}
    try:
        skill_infos = get_trusted_skills(registry_path)
        for info in skill_infos:
            skill_schemas[info.name] = info.input_schema
    except Exception:  # noqa: BLE001
        pass  # proceed without schema validation

    records: list[ExecutionRecord] = []

    for i, step in enumerate(spec.steps):
        # Determine parent_execution_id
        parent_execution_id: str | None = None
        if step.parent_index is not None:
            if 0 <= step.parent_index < len(records):
                parent_execution_id = records[step.parent_index].execution_id
        elif i > 0:
            parent_execution_id = records[i - 1].execution_id

        # Resolve input
        resolved_input, error = _resolve_input(step, i, records, skill_schemas)
        if error is not None:
            record = _make_failure_record(
                skill_name=step.skill_name,
                input_json=resolved_input,
                error=error,
                parent_execution_id=parent_execution_id,
            )
            records.append(record)
            if spec.options.stop_on_failure:
                break
            continue

        assert resolved_input is not None

        # Execute via consume_skill (handles trust verification, validation, execution)
        record = consume_skill(registry_path, step.skill_name, resolved_input)
        # Set parent_execution_id (consume_skill doesn't know about chaining)
        record.parent_execution_id = parent_execution_id
        records.append(record)

        if not record.success and spec.options.stop_on_failure:
            break

    return records
