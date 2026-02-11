"""Deterministic planner — maps parsed intents to executable actions."""

from __future__ import annotations

from kavi.agent.models import (
    ClarifyIntent,
    HelpIntent,
    ParsedIntent,
    PlannedAction,
    SkillAction,
    SkillInvocationIntent,
    TalkIntent,
    UnsupportedIntent,
    WriteNoteIntent,
    note_path_for_title,
)


def intent_to_plan(intent: ParsedIntent) -> PlannedAction | None:
    """Convert a parsed intent into a planned action. Purely deterministic.

    Returns None for unsupported intents (caller handles the error).
    """
    if isinstance(intent, WriteNoteIntent):
        return _plan_write(intent)
    if isinstance(intent, SkillInvocationIntent):
        return SkillAction(skill_name=intent.skill_name, input=intent.input)
    # TransformIntent is resolved to SkillInvocationIntent by resolve_refs()
    # before reaching the planner. HelpIntent is handled by core.py directly.
    # Return None defensively so the caller gets a clear signal.
    if isinstance(intent, (UnsupportedIntent, HelpIntent, TalkIntent, ClarifyIntent)):
        return None
    return None


def _plan_write(intent: WriteNoteIntent) -> SkillAction:
    path = note_path_for_title(intent.title)
    return SkillAction(
        skill_name="write_note",
        input={"path": path, "title": intent.title, "body": intent.body},
    )
