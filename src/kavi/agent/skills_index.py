"""Skills index — registry-driven skill discoverability with policy labeling.

Extracts and normalizes skill metadata from the trusted registry, then
labels each skill with its chat policy status (allowed / confirm / blocked).
Single source of truth: the registry via get_trusted_skills().
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from kavi.agent.core import _CONFIRM_SIDE_EFFECTS, CHAT_DEFAULT_ALLOWED_EFFECTS
from kavi.consumer.shim import SkillInfo

PolicyLabel = Literal["allowed", "confirm", "blocked"]


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
    if side_effect_class in _CONFIRM_SIDE_EFFECTS:
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
