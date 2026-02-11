"""AgentCore — stateless orchestrator for Kavi Chat v0.

Converts a user message into a deterministic action (single skill or
fixed 2-step chain), executes via consumer, and returns an auditable
AgentResponse. Never raises — all errors captured in the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from kavi.agent.constants import (
    CHAT_DEFAULT_ALLOWED_EFFECTS,
    CONFIRM_SIDE_EFFECTS,
    TALK_SKILL_NAME,
)
from kavi.agent.models import (
    AgentResponse,
    AmbiguityResponse,
    ChainAction,
    ClarifyIntent,
    HelpIntent,
    ParsedIntent,
    PendingConfirmation,
    SessionContext,
    SkillAction,
    TalkIntent,
    TransformIntent,
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

    # 2. Parse intent (with conversation history for LLM context)
    history = session.messages if session else None
    parse_result = parse_intent(message, skills, mode=parse_mode, history=history)
    intent = parse_result.intent
    warnings = list(parse_result.warnings)
    tool_call = parse_result.tool_call

    # 2b. Resolve references (D015)
    _skip_resolve = (UnsupportedIntent, HelpIntent, TalkIntent, ClarifyIntent)
    if session is not None and not isinstance(intent, _skip_resolve):
        resolved = resolve_refs(intent, session, skills=skills)
        if isinstance(resolved, AmbiguityResponse):
            return AgentResponse(
                intent=intent,
                warnings=warnings,
                error=resolved.message,
                session=session,
                tool_call=tool_call,
            )
        intent = resolved

    # 2c. TransformIntent without session → error
    if isinstance(intent, TransformIntent) and session is None:
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error="Cannot apply correction — no session context. "
            "Use the REPL for multi-turn workflows.",
            tool_call=tool_call,
        )

    # 3. Check for unsupported / clarify
    if isinstance(intent, UnsupportedIntent):
        return AgentResponse(
            intent=intent, warnings=warnings, error=intent.message,
            session=session if session is not None else None,
            tool_call=tool_call,
        )

    if isinstance(intent, ClarifyIntent):
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error=intent.question,
            session=session if session is not None else None,
            tool_call=tool_call,
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
            tool_call=tool_call,
        )

    # 3c. Talk — generate conversational response, log as ExecutionRecord
    if isinstance(intent, TalkIntent):
        return _handle_talk(
            intent,
            session=session,
            log_path=log_path,
            warnings=warnings,
            tool_call=tool_call,
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

    # 6. Write with empty body — try to auto-bind from session, else prompt
    if isinstance(intent, WriteNoteIntent) and not intent.body:
        # Defensive: if the LLM didn't emit ref:last but there's a recent
        # Talk/summarize anchor, bind it automatically.  Small LLMs often
        # fail to produce ref markers reliably.
        if session and session.anchors:
            from kavi.agent.resolver import _content_anchor_value

            last_anchor = session.anchors[-1]
            content = _content_anchor_value(last_anchor)
            if content:
                intent = WriteNoteIntent(title=intent.title, body=content)
                plan = intent_to_plan(intent)

        if not intent.body and not confirmed:
            return AgentResponse(
                intent=intent,
                plan=plan,
                needs_confirmation=True,
                pending=PendingConfirmation(
                    plan=plan, intent=intent,
                    session=session, warnings=warnings or [],
                ),
                warnings=warnings,
                error="No body provided. Use the REPL for multi-line input.",
                session=session if session is not None else None,
                tool_call=tool_call,
            )

    # 7. Check confirmation for side-effect skills
    if not confirmed and _needs_confirmation(plan, skills):
        return AgentResponse(
            intent=intent,
            plan=plan,
            needs_confirmation=True,
            pending=PendingConfirmation(
                plan=plan, intent=intent,
                session=session, warnings=warnings or [],
            ),
            warnings=warnings,
            session=session if session is not None else None,
            tool_call=tool_call,
        )

    # 8. Execute → log → session → return
    return _finalize(
        plan, intent,
        registry_path=registry_path,
        log_path=log_path,
        session=session,
        warnings=warnings,
        tool_call=tool_call,
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
    return _finalize(
        plan, intent,
        registry_path=registry_path,
        log_path=log_path,
        session=session,
        warnings=warnings,
    )


def confirm_pending(
    pending: PendingConfirmation,
    *,
    registry_path: Path,
    log_path: Path | None = None,
) -> AgentResponse:
    """Execute a stashed PendingConfirmation after user consent.

    Validates TTL. Uses the exact plan/intent/session snapshot from the
    original handle_message() call — no re-parse, no re-resolve.
    """
    if pending.is_expired():
        return AgentResponse(
            intent=pending.intent,
            plan=pending.plan,
            error="Confirmation expired. Please try again.",
            session=pending.session,
        )
    return _finalize(
        pending.plan, pending.intent,
        registry_path=registry_path,
        log_path=log_path,
        session=pending.session,
        warnings=pending.warnings,
    )


def _finalize(
    plan: SkillAction | ChainAction,
    intent: ParsedIntent,
    *,
    registry_path: Path,
    log_path: Path | None = None,
    session: SessionContext | None = None,
    warnings: list[str] | None = None,
    tool_call: Any = None,
) -> AgentResponse:
    """Execute plan, log records, update session, return response.

    Shared tail of handle_message() and execute_plan().
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
            tool_call=tool_call,
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
        tool_call=tool_call,
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


# ── TalkIntent handling ──────────────────────────────────────────────

_TALK_FALLBACK = (
    "I'm here to help! I can search your notes, summarize them, "
    "save things to your daily log, and more. What would you like to do?"
)


def _handle_talk(
    intent: TalkIntent,
    *,
    session: SessionContext | None,
    log_path: Path | None,
    warnings: list[str] | None,
    tool_call: Any = None,
) -> AgentResponse:
    """Record a conversational response as an ExecutionRecord.

    When intent.generated is True (LLM path), intent.message already
    contains the response — the parser LLM generated it with full
    conversation history (D020). No second LLM call needed.

    When intent.generated is False (deterministic fallback / Spark down),
    intent.message is raw user input, so we use a canned fallback.
    """
    import datetime

    response_text = intent.message if intent.generated else _TALK_FALLBACK
    if not response_text:
        response_text = _TALK_FALLBACK

    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    finished_at = started_at  # no LLM call, effectively instant

    record = ExecutionRecord(
        skill_name=TALK_SKILL_NAME,
        source_hash="",
        side_effect_class="NONE",
        input_json={"message": intent.message},
        output_json={"response": response_text},
        success=True,
        error=None,
        started_at=started_at,
        finished_at=finished_at,
    )

    if log_path is not None:
        writer = ExecutionLogWriter(log_path)
        writer.append(record)

    updated_session = None
    if session is not None:
        updated_session = extract_anchors([record], existing=session)

    return AgentResponse(
        intent=intent,
        records=[record],
        warnings=warnings or [],
        session=updated_session,
        tool_call=tool_call,
    )
