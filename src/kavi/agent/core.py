"""AgentCore — stateless orchestrator for Kavi Chat v0.

Converts a user message into a deterministic action (single skill or
fixed 2-step chain), executes via consumer, and returns an auditable
AgentResponse. Never raises — all errors captured in the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from kavi.agent.models import (
    AgentResponse,
    ChainAction,
    SkillAction,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.agent.parser import parse_intent
from kavi.agent.planner import intent_to_plan
from kavi.consumer.chain import consume_chain
from kavi.consumer.log import ExecutionLogWriter
from kavi.consumer.shim import ExecutionRecord, SkillInfo, consume_skill, get_trusted_skills

# Side-effect classes that require user confirmation before execution
_CONFIRM_SIDE_EFFECTS = {"FILE_WRITE", "NETWORK", "SECRET_READ"}


def handle_message(
    message: str,
    *,
    registry_path: Path,
    log_path: Path | None = None,
    confirmed: bool = False,
    parse_mode: Literal["llm", "deterministic"] = "llm",
) -> AgentResponse:
    """Process a single user message and return an auditable response.

    Args:
        message: Raw user input.
        registry_path: Path to the skill registry YAML.
        log_path: Optional JSONL log path for execution records.
        confirmed: If True, skip confirmation for FILE_WRITE skills.
                   In single-turn mode this is False; the REPL sets it
                   after receiving explicit user consent.
        parse_mode: "llm" uses Sparkstation with deterministic fallback.
                    "deterministic" requires explicit command prefixes.
                    REPL uses "deterministic" to avoid misclassification.
    """
    # 1. Load available skills
    try:
        skills = get_trusted_skills(registry_path)
    except Exception as exc:  # noqa: BLE001
        return AgentResponse(
            intent=UnsupportedIntent(
                message="Failed to load skill registry.",
            ),
            error=f"Registry error: {exc}",
        )

    # 2. Parse intent
    intent, warnings = parse_intent(message, skills, mode=parse_mode)

    # 3. Check for unsupported
    if isinstance(intent, UnsupportedIntent):
        return AgentResponse(
            intent=intent, warnings=warnings, error=intent.message,
        )

    # 4. Plan
    plan = intent_to_plan(intent)
    if plan is None:
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error="Could not create a plan for this intent.",
        )

    # 5. Write with empty body — prompt user instead of executing
    if (
        isinstance(intent, WriteNoteIntent)
        and not intent.body
        and not confirmed
    ):
        return AgentResponse(
            intent=intent,
            plan=plan,
            needs_confirmation=True,
            warnings=warnings,
            error="No body provided. Use the REPL for multi-line input.",
        )

    # 6. Check confirmation for FILE_WRITE
    if not confirmed and _needs_confirmation(plan, skills):
        return AgentResponse(
            intent=intent,
            plan=plan,
            needs_confirmation=True,
            warnings=warnings,
        )

    # 7. Execute
    try:
        records = _execute(plan, registry_path)
    except Exception as exc:  # noqa: BLE001
        return AgentResponse(
            intent=intent,
            plan=plan,
            warnings=warnings,
            error=f"Execution error: {exc}",
        )

    # 8. Log
    if log_path is not None:
        writer = ExecutionLogWriter(log_path)
        for rec in records:
            writer.append(rec)

    # 9. Return
    error = None
    if any(not r.success for r in records):
        failed = [r for r in records if not r.success]
        error = f"{len(failed)} step(s) failed: {failed[0].error}"

    return AgentResponse(
        intent=intent,
        plan=plan,
        records=records,
        warnings=warnings,
        error=error,
    )


def _needs_confirmation(
    plan: SkillAction | ChainAction,
    skills: list[SkillInfo],
) -> bool:
    """Check if any skill in the plan requires confirmation."""
    skill_effects = {s.name: s.side_effect_class for s in skills}

    if isinstance(plan, SkillAction):
        return skill_effects.get(plan.skill_name, "") in _CONFIRM_SIDE_EFFECTS
    if isinstance(plan, ChainAction):
        return any(
            skill_effects.get(step.skill_name, "") in _CONFIRM_SIDE_EFFECTS
            for step in plan.chain.steps
        )
    return False


def _execute(plan: SkillAction | ChainAction, registry_path: Path) -> list[ExecutionRecord]:
    """Execute the planned action via the consumer layer."""
    if isinstance(plan, SkillAction):
        record = consume_skill(registry_path, plan.skill_name, plan.input)
        return [record]
    if isinstance(plan, ChainAction):
        return consume_chain(registry_path, plan.chain)
    msg = f"Unknown plan type: {type(plan)}"
    raise ValueError(msg)
