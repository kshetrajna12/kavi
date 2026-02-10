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
