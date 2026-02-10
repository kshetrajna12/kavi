"""AgentCore — stateless orchestrator for Kavi Chat v0.

Converts a user message into a deterministic action (single skill or
fixed 2-step chain), executes via consumer, and returns an auditable
AgentResponse. Never raises — all errors captured in the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from kavi.agent.constants import CHAT_DEFAULT_ALLOWED_EFFECTS, CONFIRM_SIDE_EFFECTS
from kavi.agent.models import (
    AgentResponse,
    AmbiguityResponse,
    ChainAction,
    HelpIntent,
    ParsedIntent,
    SessionContext,
    SkillAction,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.agent.parser import parse_intent
from kavi.agent.planner import intent_to_plan
from kavi.agent.resolver import extract_anchors, resolve_refs
from kavi.consumer.chain import consume_chain
from kavi.consumer.log import ExecutionLogWriter
from kavi.consumer.shim import ExecutionRecord, SkillInfo, consume_skill, get_trusted_skills


def handle_message(
    message: str,
    *,
    registry_path: Path,
    log_path: Path | None = None,
    confirmed: bool = False,
    parse_mode: Literal["llm", "deterministic"] = "llm",
    allowed_effects: frozenset[str] | None = None,
    session: SessionContext | None = None,
) -> AgentResponse:
    """Process a single user message and return an auditable response.

    Args:
        message: Raw user input.
        registry_path: Path to the skill registry YAML.
        log_path: Optional JSONL log path for execution records.
        confirmed: If True, skip confirmation for side-effect skills.
                   In single-turn mode this is False; the REPL sets it
                   after receiving explicit user consent.
        parse_mode: "llm" uses Sparkstation with deterministic fallback.
                    "deterministic" requires explicit command prefixes.
                    REPL uses "deterministic" to avoid misclassification.
        allowed_effects: Side-effect classes permitted in chat.
                         Defaults to CHAT_DEFAULT_ALLOWED_EFFECTS
                         (READ_ONLY, FILE_WRITE). Pass a broader set
                         to enable NETWORK or SECRET_READ skills.
        session: Optional session context for reference resolution (D015).
                 None = stateless mode (backward compatible).
    """
    if allowed_effects is None:
        allowed_effects = CHAT_DEFAULT_ALLOWED_EFFECTS

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

    # 2b. Resolve references (D015)
    if session is not None and not isinstance(intent, (UnsupportedIntent, HelpIntent)):
        resolved = resolve_refs(intent, session, skills=skills)
        if isinstance(resolved, AmbiguityResponse):
            return AgentResponse(
                intent=intent,
                warnings=warnings,
                error=resolved.message,
                session=session,
            )
        intent = resolved

    # 3. Check for unsupported
    if isinstance(intent, UnsupportedIntent):
        return AgentResponse(
            intent=intent, warnings=warnings, error=intent.message,
            session=session if session is not None else None,
        )

    # 3b. Help — return skills index, no planning needed
    if isinstance(intent, HelpIntent):
        from kavi.agent.skills_index import build_index, format_index

        index = build_index(skills, allowed_effects)
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            help_text=format_index(index),
            session=session if session is not None else None,
        )

    # 4. Plan
    plan = intent_to_plan(intent)
    if plan is None:
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error="Could not create a plan for this intent.",
            session=session if session is not None else None,
        )

    # 5. Chat policy — block skills whose side-effect class isn't allowed
    blocked = _blocked_effects(plan, skills, allowed_effects)
    if blocked:
        classes = ", ".join(sorted(blocked))
        return AgentResponse(
            intent=intent,
            plan=plan,
            warnings=warnings,
            error=f"Skill blocked by chat policy "
            f"(side-effect class not allowed: {classes}). "
            f"Use run-skill or consume-skill for direct invocation.",
            session=session if session is not None else None,
        )

    # 6. Write with empty body — prompt user instead of executing
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
            session=session if session is not None else None,
        )

    # 7. Check confirmation for side-effect skills
    if not confirmed and _needs_confirmation(plan, skills):
        return AgentResponse(
            intent=intent,
            plan=plan,
            needs_confirmation=True,
            warnings=warnings,
            session=session if session is not None else None,
        )

    # 8. Execute
    try:
        records = _execute(plan, registry_path)
    except Exception as exc:  # noqa: BLE001
        return AgentResponse(
            intent=intent,
            plan=plan,
            warnings=warnings,
            error=f"Execution error: {exc}",
            session=session if session is not None else None,
        )

    # 9. Log
    if log_path is not None:
        writer = ExecutionLogWriter(log_path)
        for rec in records:
            writer.append(rec)

    # 10. Build updated session (D015)
    updated_session = None
    if session is not None:
        updated_session = extract_anchors(records, existing=session)

    # 11. Return
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
        session=updated_session,
    )


def execute_plan(
    plan: SkillAction | ChainAction,
    intent: ParsedIntent,
    *,
    registry_path: Path,
    log_path: Path | None = None,
    session: SessionContext | None = None,
    warnings: list[str] | None = None,
) -> AgentResponse:
    """Execute a previously resolved plan without re-parsing.

    Used by the REPL to execute a stashed plan after user confirmation.
    The plan must have been produced by a prior handle_message() call
    with all refs already resolved and anchors bound.
    """
    try:
        records = _execute(plan, registry_path)
    except Exception as exc:  # noqa: BLE001
        return AgentResponse(
            intent=intent,
            plan=plan,
            warnings=warnings or [],
            error=f"Execution error: {exc}",
            session=session,
        )

    if log_path is not None:
        writer = ExecutionLogWriter(log_path)
        for rec in records:
            writer.append(rec)

    updated_session = None
    if session is not None:
        updated_session = extract_anchors(records, existing=session)

    error = None
    if any(not r.success for r in records):
        failed = [r for r in records if not r.success]
        error = f"{len(failed)} step(s) failed: {failed[0].error}"

    return AgentResponse(
        intent=intent,
        plan=plan,
        records=records,
        warnings=warnings or [],
        error=error,
        session=updated_session,
    )


def _blocked_effects(
    plan: SkillAction | ChainAction,
    skills: list[SkillInfo],
    allowed: frozenset[str],
) -> set[str]:
    """Return set of side-effect classes in the plan that aren't allowed."""
    skill_effects = {s.name: s.side_effect_class for s in skills}

    plan_effects: set[str] = set()
    if isinstance(plan, SkillAction):
        eff = skill_effects.get(plan.skill_name, "")
        if eff:
            plan_effects.add(eff)
    elif isinstance(plan, ChainAction):
        for step in plan.chain.steps:
            eff = skill_effects.get(step.skill_name, "")
            if eff:
                plan_effects.add(eff)

    return plan_effects - allowed


def _needs_confirmation(
    plan: SkillAction | ChainAction,
    skills: list[SkillInfo],
) -> bool:
    """Check if any skill in the plan requires confirmation."""
    skill_effects = {s.name: s.side_effect_class for s in skills}

    if isinstance(plan, SkillAction):
        return skill_effects.get(plan.skill_name, "") in CONFIRM_SIDE_EFFECTS
    if isinstance(plan, ChainAction):
        return any(
            skill_effects.get(step.skill_name, "") in CONFIRM_SIDE_EFFECTS
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
