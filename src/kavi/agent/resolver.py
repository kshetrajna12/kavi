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
    SessionContext,
    SkillInvocationIntent,
)
from kavi.consumer.shim import ExecutionRecord

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


_SKILL_INPUT_FIELDS: dict[str, set[str]] = {
    "search_notes": {"query", "top_k"},
    "summarize_note": {"path", "style"},
    "write_note": {"path", "title", "body", "tags"},
    "read_notes_by_tag": {"tag"},
    "http_get_json": {"url", "headers"},
}


def _resolve_again(
    intent: SkillInvocationIntent,
    session: SessionContext,
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
    allowed = _SKILL_INPUT_FIELDS.get(last.skill_name)
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

    path = f"Inbox/AI/{title}.md"

    return SkillInvocationIntent(
        skill_name="write_note",
        input={"path": path, "title": title, "body": body},
    )


def resolve_refs(
    intent: ParsedIntent,
    session: SessionContext | None,
) -> ParsedIntent | AmbiguityResponse:
    """Resolve ref markers in intent inputs using session anchors.

    Only processes SkillInvocationIntent — other intent types pass through.
    Returns the intent with refs replaced, or AmbiguityResponse if
    resolution fails.
    """
    if session is None:
        return intent

    if not isinstance(intent, SkillInvocationIntent):
        return intent

    # Special case: "again" — re-invoke last skill
    if intent.skill_name == "ref:last_skill":
        return _resolve_again(intent, session)

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
