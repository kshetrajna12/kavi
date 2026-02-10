"""Skills index — registry-driven skill discoverability with policy labeling.

Extracts and normalizes skill metadata from the trusted registry, then
labels each skill with its chat policy status (allowed / confirm / blocked).
Single source of truth: the registry via get_trusted_skills().

Presentation layer: format_index() renders the index as a human-readable
table; example_invocation() generates a minimal usage example from the
JSON schema's required fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from kavi.agent.constants import CHAT_DEFAULT_ALLOWED_EFFECTS, CONFIRM_SIDE_EFFECTS
from kavi.consumer.shim import SkillInfo

PolicyLabel = Literal["allowed", "confirm", "blocked"]

_POLICY_ICONS: dict[PolicyLabel, str] = {
    "allowed": "[auto]",
    "confirm": "[confirm]",
    "blocked": "[blocked]",
}


class SkillEntry(BaseModel):
    """A single skill's metadata with its chat policy label."""

    name: str
    description: str
    side_effect_class: str
    policy: PolicyLabel
    required_secrets: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}


def policy_label(
    side_effect_class: str,
    allowed_effects: frozenset[str] = CHAT_DEFAULT_ALLOWED_EFFECTS,
) -> PolicyLabel:
    """Derive the chat policy label for a side-effect class.

    - "allowed": in the allowed set and not requiring confirmation
    - "confirm": in the allowed set but requires confirmation before execution
    - "blocked": not in the allowed set (must be explicitly opted in)
    """
    if side_effect_class not in allowed_effects:
        return "blocked"
    if side_effect_class in CONFIRM_SIDE_EFFECTS:
        return "confirm"
    return "allowed"


def build_index(
    skills: list[SkillInfo],
    allowed_effects: frozenset[str] = CHAT_DEFAULT_ALLOWED_EFFECTS,
) -> list[SkillEntry]:
    """Build a sorted skills index with policy labels.

    Returns entries sorted alphabetically by name for stable output.
    """
    entries = [
        SkillEntry(
            name=s.name,
            description=s.description,
            side_effect_class=s.side_effect_class,
            policy=policy_label(s.side_effect_class, allowed_effects),
            required_secrets=s.required_secrets,
            input_schema=s.input_schema,
            output_schema=s.output_schema,
        )
        for s in skills
    ]
    return sorted(entries, key=lambda e: e.name)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_TYPE_PLACEHOLDERS: dict[str, str] = {
    "string": '"..."',
    "integer": "1",
    "number": "1.0",
    "boolean": "true",
    "array": "[]",
    "object": "{}",
}


def _placeholder(prop: dict[str, Any]) -> str:
    """Return a minimal placeholder value for a JSON schema property."""
    # Enum — use first value
    if "enum" in prop:
        val = prop["enum"][0]
        return f'"{val}"' if isinstance(val, str) else str(val)

    # anyOf / oneOf — pick first non-null type
    for key in ("anyOf", "oneOf"):
        if key in prop:
            for variant in prop[key]:
                if variant.get("type") != "null":
                    return _placeholder(variant)

    typ = prop.get("type", "string")
    return _TYPE_PLACEHOLDERS.get(typ, '"..."')


def example_invocation(entry: SkillEntry) -> str:
    """Generate a minimal example invocation from the input schema.

    Uses only the ``required`` fields from the JSON schema, producing a
    compact one-liner like: ``skill_name(field="...", n=1)``
    """
    schema = entry.input_schema
    required = schema.get("required", [])
    props = schema.get("properties", {})

    args: list[str] = []
    for field_name in required:
        prop = props.get(field_name, {})
        args.append(f"{field_name}={_placeholder(prop)}")

    return f"{entry.name}({', '.join(args)})"


def format_entry(entry: SkillEntry) -> str:
    """Format a single SkillEntry as a multi-line text block."""
    icon = _POLICY_ICONS[entry.policy]
    lines = [
        f"  {entry.name}  {icon}  {entry.side_effect_class}",
        f"    {entry.description}",
        f"    Example: {example_invocation(entry)}",
    ]
    if entry.required_secrets:
        lines.append(f"    Secrets: {', '.join(entry.required_secrets)}")
    return "\n".join(lines)


def format_index(entries: list[SkillEntry]) -> str:
    """Render a list of SkillEntry objects as a human-readable skill listing.

    Groups skills by policy label (allowed first, then confirm, then blocked)
    and shows per-skill: name, effect class, description, and example usage.
    """
    if not entries:
        return "No skills available."

    groups: dict[PolicyLabel, list[SkillEntry]] = {
        "allowed": [],
        "confirm": [],
        "blocked": [],
    }
    for e in entries:
        groups[e.policy].append(e)

    sections: list[str] = []

    if groups["allowed"]:
        lines = ["Available skills (auto-execute):"]
        for e in groups["allowed"]:
            lines.append(format_entry(e))
        sections.append("\n".join(lines))

    if groups["confirm"]:
        lines = ["Requires confirmation:"]
        for e in groups["confirm"]:
            lines.append(format_entry(e))
        sections.append("\n".join(lines))

    if groups["blocked"]:
        lines = ["Blocked (opt-in required):"]
        for e in groups["blocked"]:
            lines.append(format_entry(e))
        sections.append("\n".join(lines))

    return "\n\n".join(sections)
