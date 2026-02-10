"""Reference resolver — binds ref markers to concrete anchor values (D015).

Runs between parse and plan. Scans intent inputs for ``ref:`` prefixes
and replaces them with concrete values from the session's anchors.
"""

from __future__ import annotations

from typing import Any

from kavi.agent.models import (
    AmbiguityResponse,
    Anchor,
    ParsedIntent,
    SearchAndSummarizeIntent,
    SessionContext,
    SkillInvocationIntent,
    TransformIntent,
    note_path_for_title,
)
from kavi.consumer.shim import ExecutionRecord, SkillInfo

# Fields to try when extracting a concrete value from an anchor.
# Order matters — first match wins.
_VALUE_FIELDS = ("top_result_path", "path", "written_path", "url", "query", "summary")


def _anchor_value(anchor: Anchor) -> str | None:
    """Extract the most useful concrete value from an anchor's data."""
    for field in _VALUE_FIELDS:
        val = anchor.data.get(field)
        if val is not None:
            return str(val)
    # Fallback: first string value in data
    for v in anchor.data.values():
        if isinstance(v, str):
            return v
    return None


# For search refs, prefer query/summary over path fields.
_SEARCH_VALUE_FIELDS = ("query", "summary")


def _search_anchor_value(anchor: Anchor) -> str | None:
    """Extract a search-friendly value from an anchor's data."""
    for field in _SEARCH_VALUE_FIELDS:
        val = anchor.data.get(field)
        if val is not None:
            return str(val)
    return _anchor_value(anchor)


def _input_fields_for(
    skill_name: str, skills: list[SkillInfo],
) -> set[str] | None:
    """Derive valid input field names from a skill's input_schema."""
    for s in skills:
        if s.name == skill_name:
            return set(s.input_schema.get("properties", {}).keys())
    return None


def _resolve_again(
    intent: SkillInvocationIntent,
    session: SessionContext,
    skills: list[SkillInfo],
) -> SkillInvocationIntent | AmbiguityResponse:
    """Resolve 'again' — re-invoke the last skill with optional overrides."""
    if not session.anchors:
        return AmbiguityResponse(
            ref="last_skill",
            candidates=[],
            message="Could not resolve 'again' — no prior results "
            "to reference. Try running a command first.",
        )

    last = session.anchors[-1]
    # Only copy anchor data fields that are valid inputs for the skill
    allowed = _input_fields_for(last.skill_name, skills)
    new_input: dict[str, Any] = {}
    for key, val in last.data.items():
        if allowed is None or key in allowed:
            new_input[key] = val

    # Apply overrides from the intent (e.g. style=paragraph)
    for key, val in intent.input.items():
        if key == "ref:again":
            continue  # marker, not a real field
        new_input[key] = val

    return SkillInvocationIntent(
        skill_name=last.skill_name,
        input=new_input,
    )


def _resolve_write_that(
    intent: SkillInvocationIntent,
    session: SessionContext,
) -> SkillInvocationIntent | AmbiguityResponse:
    """Resolve 'write that' — write last result to a note."""
    if not session.anchors:
        return AmbiguityResponse(
            ref="last",
            candidates=[],
            message="Could not resolve 'write that' — no prior results "
            "to reference. Try running a command first.",
        )

    last = session.anchors[-1]

    # Extract a meaningful body from the last result
    body = (
        last.data.get("summary")
        or _anchor_value(last)
        or f"{last.skill_name} result"
    )

    # Derive a title from the last skill's context
    title = f"{last.skill_name} result"
    if "query" in last.data:
        title = f"Notes: {last.data['query']}"
    elif "path" in last.data:
        # Use filename without extension as title
        fname = last.data["path"].rsplit("/", 1)[-1]
        if fname.endswith(".md"):
            fname = fname[:-3]
        title = f"Summary: {fname}"

    return SkillInvocationIntent(
        skill_name="write_note",
        input={
            "path": note_path_for_title(title),
            "title": title,
            "body": body,
        },
    )


def _resolve_transform(
    intent: TransformIntent,
    session: SessionContext,
    skills: list[SkillInfo],
) -> SkillInvocationIntent | AmbiguityResponse:
    """Resolve a transform — re-invoke target skill with overrides applied."""
    anchor = session.resolve(intent.target_ref)
    if anchor is None:
        return AmbiguityResponse(
            ref=intent.target_ref,
            candidates=[],
            message=f"Could not resolve '{intent.target_ref}' — no prior results "
            "to reference. Try running a command first.",
        )

    # Copy anchor data fields that are valid inputs for the skill
    allowed = _input_fields_for(anchor.skill_name, skills)
    new_input: dict[str, Any] = {}
    for key, val in anchor.data.items():
        if allowed is None or key in allowed:
            new_input[key] = val

    # Apply overrides
    new_input.update(intent.overrides)

    return SkillInvocationIntent(
        skill_name=anchor.skill_name,
        input=new_input,
    )


def resolve_refs(
    intent: ParsedIntent,
    session: SessionContext | None,
    skills: list[SkillInfo] | None = None,
) -> ParsedIntent | AmbiguityResponse:
    """Resolve ref markers in intent inputs using session anchors.

    Only processes SkillInvocationIntent — other intent types pass through.
    Returns the intent with refs replaced, or AmbiguityResponse if
    resolution fails.

    Args:
        skills: Skill metadata for input-field filtering. When provided,
                "again" only copies anchor fields that match the skill's
                input schema. When None, all anchor data is copied.
    """
    if session is None:
        return intent

    # SearchAndSummarizeIntent: resolve ref: in query field
    if isinstance(intent, SearchAndSummarizeIntent) and intent.query.startswith("ref:"):
        ref = intent.query[4:]
        anchor = session.resolve(ref)
        if anchor is None:
            return AmbiguityResponse(
                ref=ref,
                candidates=[],
                message=f"Could not resolve '{ref}' — no prior results "
                "to reference. Try running a command first.",
            )
        value = _search_anchor_value(anchor)
        if value is not None:
            return SearchAndSummarizeIntent(
                query=value, top_k=intent.top_k, style=intent.style,
            )
        return intent

    # TransformIntent: re-invoke target skill with overrides
    if isinstance(intent, TransformIntent):
        return _resolve_transform(intent, session, skills or [])

    if not isinstance(intent, SkillInvocationIntent):
        return intent

    # Special case: "again" — re-invoke last skill
    if intent.skill_name == "ref:last_skill":
        return _resolve_again(intent, session, skills or [])

    # Special case: "write that" — write last result to a note
    if (
        intent.skill_name == "write_note"
        and any(
            isinstance(v, str) and v.startswith("ref:last_")
            for v in intent.input.values()
        )
    ):
        return _resolve_write_that(intent, session)

    # General case: resolve ref: markers in input values
    resolved_input: dict[str, Any] = {}
    for key, val in intent.input.items():
        if isinstance(val, str) and val.startswith("ref:"):
            ref = val[4:]  # strip "ref:" prefix
            anchor = session.resolve(ref)
            if anchor is None:
                # Check for ambiguity
                candidates = session.ambiguous(ref)
                if candidates:
                    labels = ", ".join(
                        f"{a.skill_name} ({a.execution_id[:8]})"
                        for a in candidates
                    )
                    return AmbiguityResponse(
                        ref=ref,
                        candidates=candidates,
                        message=f"Ambiguous reference '{ref}'. "
                        f"Did you mean: {labels}?",
                    )
                return AmbiguityResponse(
                    ref=ref,
                    candidates=[],
                    message=f"Could not resolve '{ref}' — no prior results "
                    f"to reference. Try running a command first.",
                )

            concrete = _anchor_value(anchor)
            if concrete is not None:
                resolved_input[key] = concrete
            else:
                resolved_input[key] = val  # keep original if no value
        else:
            resolved_input[key] = val

    return SkillInvocationIntent(
        skill_name=intent.skill_name,
        input=resolved_input,
    )


def extract_anchors(
    records: list[ExecutionRecord],
    existing: SessionContext | None,
) -> SessionContext:
    """Build updated SessionContext from execution records.

    Preserves existing anchors and appends new ones from records.
    """
    ctx = SessionContext()
    if existing is not None:
        ctx.anchors = list(existing.anchors)
    ctx.add_from_records(records)
    return ctx
