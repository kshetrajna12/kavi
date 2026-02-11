"""AgentCore — stateless orchestrator for Kavi Chat v0.

Converts a user message into a deterministic action (single skill or
fixed 2-step chain), executes via consumer, and returns an auditable
AgentResponse. Never raises — all errors captured in the response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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

    # 2. Parse intent
    intent, warnings = parse_intent(message, skills, mode=parse_mode)

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
            )
        intent = resolved

    # 2c. TransformIntent without session → error
    if isinstance(intent, TransformIntent) and session is None:
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error="Cannot apply correction — no session context. "
            "Use the REPL for multi-turn workflows.",
        )

    # 3. Check for unsupported / clarify
    if isinstance(intent, UnsupportedIntent):
        return AgentResponse(
            intent=intent, warnings=warnings, error=intent.message,
            session=session if session is not None else None,
        )

    if isinstance(intent, ClarifyIntent):
        return AgentResponse(
            intent=intent,
            warnings=warnings,
            error=intent.question,
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

    # 3c. Talk — generate conversational response, log as ExecutionRecord
    if isinstance(intent, TalkIntent):
        return _handle_talk(
            intent,
            session=session,
            log_path=log_path,
            warnings=warnings,
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
        )

    # 8. Execute → log → session → return
    return _finalize(
        plan, intent,
        registry_path=registry_path,
        log_path=log_path,
        session=session,
        warnings=warnings,
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


# ── TalkIntent handling ──────────────────────────────────────────────

_TALK_SYSTEM = (
    "You are Kavi, a personal knowledge assistant. "
    "You are in CONVERSATION-ONLY mode right now — you CANNOT execute "
    "actions in this turn. You have NOT written, saved, added, searched, "
    "created, or done anything on the user's behalf. "
    "NEVER claim to have performed an action. "
    "If the user asks you to do something (write, save, search, etc.), "
    "acknowledge their request warmly and offer to do it — for example: "
    "'Sure, I can save that to a note. Want me to go ahead?' "
    "Respond naturally and concisely to conversation."
)

_TALK_FALLBACK = (
    "I'm here to help! I can search your notes, summarize them, "
    "save things to your daily log, and more. What would you like to do?"
)

# Phrases that indicate the LLM hallucinated an action in talk mode.
_ACTION_CLAIM_PATTERNS = (
    "i saved", "i wrote", "i added", "i created",
    "has been added", "has been written", "has been saved",
    "has been created", "note has been", "added to your daily",
    "saved to your", "written to your", "created your",
    "added your note", "saved your note", "wrote your note",
)

_SANITIZED_REDIRECT = (
    "I can do that, but I haven't done anything yet. "
    "Want me to go ahead?"
)


def _sanitize_talk_response(text: str) -> str:
    """Replace hallucinated action claims with a safe redirect.

    Defense-in-depth: even with prompt hardening, LLMs may still claim
    to have performed actions. This post-check catches those cases.
    """
    lower = text.lower()
    for pattern in _ACTION_CLAIM_PATTERNS:
        if pattern in lower:
            return _SANITIZED_REDIRECT
    return text


def _handle_talk(
    intent: TalkIntent,
    *,
    session: SessionContext | None,
    log_path: Path | None,
    warnings: list[str] | None,
) -> AgentResponse:
    """Generate a conversational response and log as ExecutionRecord."""
    import datetime

    started_at = datetime.datetime.now(datetime.UTC).isoformat()
    response_text = _generate_talk_response(intent.message, session)
    finished_at = datetime.datetime.now(datetime.UTC).isoformat()

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
    )


def _generate_talk_response(
    message: str,
    session: SessionContext | None,
) -> str:
    """Generate a conversational response via Sparkstation.

    Falls back to a canned response if Sparkstation is unavailable.
    Uses role-separated messages (D019).
    """
    from kavi.llm.spark import SparkUnavailableError, generate

    system_parts = [_TALK_SYSTEM]
    if session and session.anchors:
        context_lines = ["Recent context:"]
        for anchor in session.anchors[-3:]:
            data_summary = ", ".join(
                f"{k}={v}" for k, v in anchor.data.items()
            )
            context_lines.append(
                f"- {anchor.skill_name}: {data_summary}",
            )
        system_parts.append("\n".join(context_lines))

    messages: list[dict[str, str]] = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": message},
    ]

    try:
        raw = generate(messages, temperature=0.7)
        return _sanitize_talk_response(raw)
    except (SparkUnavailableError, Exception):  # noqa: BLE001
        return _TALK_FALLBACK
