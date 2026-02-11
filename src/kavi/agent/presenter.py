"""Presenter — template-based formatting for AgentResponse.

Converts internal AgentResponse into user-facing text. Two modes:
- Default: conversational, hides mechanics, feels like a chatbot.
- Verbose: exposes intent, plan, records, session — load-bearing
  for invariant #8 (bounded, deterministic, inspectable behavior).

No LLM calls. Templates only. LLM is used for semantic value in
TalkIntent and TransformIntent, not for formatting boilerplate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from kavi.agent.constants import TALK_SKILL_NAME

if TYPE_CHECKING:
    from kavi.agent.models import AgentResponse
    from kavi.consumer.shim import ExecutionRecord


def present(resp: AgentResponse, *, verbose: bool = False) -> str:
    """Format an AgentResponse as user-facing text.

    Args:
        resp: The response from handle_message or confirm_pending.
        verbose: If True, show full internal details (intent, plan,
                 records, session). Load-bearing for inspectability.

    Returns:
        Formatted string ready for display (may contain Rich markup).
    """
    if verbose:
        return _present_verbose(resp)
    return _present_conversational(resp)


# ── Conversational mode (default) ───────────────────────────────────


def _present_conversational(resp: AgentResponse) -> str:
    parts: list[str] = []

    # Warnings (brief, inline)
    for w in resp.warnings:
        parts.append(f"[dim]Note: {w}[/dim]")

    # Help text
    if resp.help_text:
        parts.append(resp.help_text)
        return "\n".join(parts)

    # Confirmation needed
    if resp.pending is not None:
        parts.append(_format_confirmation(resp))
        return "\n".join(parts)

    # Error
    if resp.error:
        parts.append(f"Sorry, something went wrong: {resp.error}")
        return "\n".join(parts)

    # Success — format records
    if resp.records:
        parts.append(_format_records(resp.records))

    return "\n".join(parts) if parts else ""


def _format_confirmation(resp: AgentResponse) -> str:
    """Conversational confirmation prompt based on intent/plan."""
    intent = resp.intent
    kind = getattr(intent, "kind", "")

    if kind == "write_note":
        title = getattr(intent, "title", "")
        body = getattr(intent, "body", "")
        if not body:
            return f"I'll write a note titled '{title}' — what should it say?"
        return f"I'll write '{title}' to your notes — okay?"

    if kind == "skill_invocation":
        skill = getattr(intent, "skill_name", "")
        inp = getattr(intent, "input", {})

        if skill == "create_daily_note":
            return "I'll add this to today's daily note — okay?"
        if skill == "http_get_json":
            url = inp.get("url", "a URL")
            return f"I'll fetch data from {url} — okay?"

        return f"I'll run {skill} — okay?"

    if kind == "search_and_summarize":
        query = getattr(intent, "query", "")
        return f"I'll search and summarize notes about '{query}' — okay?"

    # Fallback
    plan = resp.pending.plan if resp.pending else resp.plan
    if plan is not None:
        skill_name = getattr(plan, "skill_name", "this action")
        return f"I'll run {skill_name} — okay?"

    return "This action requires confirmation — okay?"


def _format_records(records: list[ExecutionRecord]) -> str:
    """Format execution records conversationally."""
    parts: list[str] = []
    for rec in records:
        parts.append(_format_single_record(rec))
    return "\n".join(parts)


def _format_single_record(rec: ExecutionRecord) -> str:
    """Format a single execution record."""
    if rec.skill_name == TALK_SKILL_NAME:
        return str((rec.output_json or {}).get("response", ""))

    if not rec.success:
        return f"[red]{rec.skill_name} failed:[/red] {rec.error}"

    out = rec.output_json or {}

    # Skill-specific templates
    if rec.skill_name == "search_notes":
        return _format_search(out)

    if rec.skill_name == "summarize_note":
        summary = out.get("summary", "")
        path = out.get("path", "")
        if summary:
            header = f"[dim]Summary of {path}:[/dim]" if path else ""
            return f"{header}\n{summary}" if header else summary
        return f"Summarized {path}."

    if rec.skill_name == "write_note":
        path = out.get("written_path", "")
        return f"Done — wrote {path}."

    if rec.skill_name == "create_daily_note":
        return "Added to today's daily note."

    if rec.skill_name == "read_notes_by_tag":
        count = out.get("count", 0)
        notes = out.get("notes", [])
        if notes:
            paths = [n.get("path", n.get("title", "?")) for n in notes[:5]]
            listing = ", ".join(paths)
            return f"Found {count} note(s): {listing}"
        return f"Found {count} note(s)."

    if rec.skill_name == "http_get_json":
        url = out.get("url", "")
        status = out.get("status_code", "")
        data = out.get("data")
        header = f"Fetched {url} ({status})"
        if data:
            data_str = json.dumps(data, indent=2)
            if len(data_str) > 200:
                data_str = data_str[:200] + "..."
            return f"{header}:\n{data_str}"
        return header

    # Generic: compact key-value
    lines: list[str] = [f"  {rec.skill_name}: OK"]
    for key, val in out.items():
        if isinstance(val, str) and len(val) > 120:
            val = val[:120] + "..."
        lines.append(f"    {key}: {val}")
    return "\n".join(lines)


def _format_search(out: dict) -> str:
    """Format search_notes output as a compact table."""
    results = out.get("results", [])
    if not results:
        return "No results found."

    lines: list[str] = []
    lines.append(f"  {'#':<4} {'Score':<8} {'Path':<30} Title")
    lines.append(f"  {'─' * 4} {'─' * 8} {'─' * 30} {'─' * 20}")

    for i, r in enumerate(results, 1):
        score = f"{r.get('score', 0):.4f}"
        path = r.get("path", "")
        title = r.get("title") or ""
        lines.append(f"  {i:<4} {score:<8} {path:<30} {title}")

    return "\n".join(lines)


# ── Verbose mode ────────────────────────────────────────────────────


def _present_verbose(resp: AgentResponse) -> str:
    """Full internal details — intent, plan, records, session."""
    sections: list[str] = []

    # Intent
    intent_data = resp.intent
    kind = getattr(intent_data, "kind", "unknown")
    sections.append(f"[bold]Intent:[/bold] {kind}")
    sections.append(f"  {json.dumps(intent_data.model_dump(), indent=2)}")

    # Warnings
    if resp.warnings:
        sections.append("[bold]Warnings:[/bold]")
        for w in resp.warnings:
            sections.append(f"  - {w}")

    # Plan
    if resp.plan is not None:
        sections.append("[bold]Plan:[/bold]")
        sections.append(f"  {json.dumps(resp.plan.model_dump(), indent=2)}")

    # Pending confirmation
    if resp.pending is not None:
        sections.append("[bold]Pending confirmation:[/bold]")
        sections.append(
            f"  Plan: {json.dumps(resp.pending.plan.model_dump(), indent=2)}",
        )
        sections.append(f"  Created: {resp.pending.created_at.isoformat()}")
        sections.append(f"  Expired: {resp.pending.is_expired()}")

    # Error
    if resp.error:
        sections.append(f"[bold]Error:[/bold] {resp.error}")

    # Help
    if resp.help_text:
        sections.append(f"[bold]Help:[/bold]\n{resp.help_text}")

    # Records
    if resp.records:
        sections.append(f"[bold]Records ({len(resp.records)}):[/bold]")
        for rec in resp.records:
            sections.append(f"  [{rec.execution_id[:8]}] {rec.skill_name}")
            sections.append(f"    side_effect: {rec.side_effect_class}")
            sections.append(f"    success: {rec.success}")
            if rec.error:
                sections.append(f"    error: {rec.error}")
            if rec.input_json:
                inp_str = json.dumps(rec.input_json)
                if len(inp_str) > 200:
                    inp_str = inp_str[:200] + "..."
                sections.append(f"    input: {inp_str}")
            if rec.output_json:
                out_str = json.dumps(rec.output_json)
                if len(out_str) > 500:
                    out_str = out_str[:500] + "..."
                sections.append(f"    output: {out_str}")
            sections.append(f"    timing: {rec.started_at} → {rec.finished_at}")

    # Session
    if resp.session is not None:
        anchors = resp.session.anchors
        sections.append(f"[bold]Session ({len(anchors)} anchors):[/bold]")
        for a in anchors:
            sections.append(
                f"  [{a.execution_id[:8]}] {a.skill_name}: "
                f"{json.dumps(a.data)}",
            )

    return "\n".join(sections)
