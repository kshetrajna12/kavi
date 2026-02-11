"""Deterministic planner — maps parsed intents to executable actions."""

from __future__ import annotations

from kavi.agent.models import (
    ChainAction,
    HelpIntent,
    ParsedIntent,
    PlannedAction,
    SearchAndSummarizeIntent,
    SkillAction,
    SkillInvocationIntent,
    TalkIntent,
    UnsupportedIntent,
    WriteNoteIntent,
    note_path_for_title,
)
from kavi.consumer.chain import (
    ChainOptions,
    ChainSpec,
    ChainStep,
    FieldMapping,
)

MAX_CHAIN_STEPS = 2


def intent_to_plan(intent: ParsedIntent) -> PlannedAction | None:
    """Convert a parsed intent into a planned action. Purely deterministic.

    Returns None for unsupported intents (caller handles the error).
    """
    if isinstance(intent, SearchAndSummarizeIntent):
        return _plan_search_and_summarize(intent)
    if isinstance(intent, WriteNoteIntent):
        return _plan_write(intent)
    if isinstance(intent, SkillInvocationIntent):
        return SkillAction(skill_name=intent.skill_name, input=intent.input)
    # TransformIntent is resolved to SkillInvocationIntent by resolve_refs()
    # before reaching the planner. HelpIntent is handled by core.py directly.
    # Return None defensively so the caller gets a clear signal.
    if isinstance(intent, (UnsupportedIntent, HelpIntent, TalkIntent)):
        return None
    return None


def _plan_search_and_summarize(intent: SearchAndSummarizeIntent) -> ChainAction:
    spec = ChainSpec(
        steps=[
            ChainStep(
                skill_name="search_notes",
                input={"query": intent.query, "top_k": intent.top_k},
            ),
            ChainStep(
                skill_name="summarize_note",
                input_template={"style": intent.style},
                from_prev=[
                    FieldMapping(to_field="path", from_path="results.0.path"),
                ],
            ),
        ],
        options=ChainOptions(stop_on_failure=True),
    )
    assert len(spec.steps) <= MAX_CHAIN_STEPS
    return ChainAction(chain=spec)


def _plan_write(intent: WriteNoteIntent) -> SkillAction:
    path = note_path_for_title(intent.title)
    return SkillAction(
        skill_name="write_note",
        input={"path": path, "title": intent.title, "body": intent.body},
    )
