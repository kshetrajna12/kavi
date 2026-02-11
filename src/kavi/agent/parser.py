"""Intent parser — per-skill tool schemas with conversation history (D018–D020).

Two parse modes:
- "llm" (default): Sparkstation classifies every turn via tool calling
  into a structured intent.  Each registered skill is its own tool with
  typed parameters.  Full conversation history is sent for context.
  Falls back to deterministic on Sparkstation failure or malformed output.
- "deterministic": Frozen fallback floor for Sparkstation-unavailable
  degraded mode.  Covers the 6 current skills' most common invocation
  forms.  New skills MUST NOT add new regex patterns here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from kavi.agent.models import (
    ClarifyIntent,
    HelpIntent,
    ParsedIntent,
    SkillInvocationIntent,
    TalkIntent,
    TransformIntent,
    WriteNoteIntent,
)
from kavi.consumer.shim import SkillInfo
from kavi.llm.spark import SparkError, SparkUnavailableError, ToolCallResult, generate_tool_call

ParseMode = Literal["llm", "deterministic"]


class ParseResult(NamedTuple):
    """Return type of ``parse_intent`` — intent plus optional warnings."""

    intent: ParsedIntent
    warnings: list[str]
    tool_call: ToolCallResult | None = None


# ── Static tool schemas (D019) ────────────────────────────────────────

TOOL_TALK = {
    "type": "function",
    "function": {
        "name": "talk",
        "description": "General conversation — no skill needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The conversational response or acknowledgment.",
                },
            },
            "required": ["message"],
        },
    },
}

TOOL_CLARIFY = {
    "type": "function",
    "function": {
        "name": "clarify",
        "description": "Ask the user for clarification when the request is ambiguous.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The clarifying question to ask.",
                },
            },
            "required": ["question"],
        },
    },
}

TOOL_META = {
    "type": "function",
    "function": {
        "name": "meta",
        "description": "Meta-commands: help, quit, explain, verbose.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["help", "quit", "explain", "verbose"],
                    "description": "The meta-command.",
                },
            },
            "required": ["command"],
        },
    },
}

_STATIC_TOOLS = [TOOL_TALK, TOOL_CLARIFY, TOOL_META]


# ── Per-skill tool schema builder (D020) ──────────────────────────────


def _build_skill_tools(skills: list[SkillInfo]) -> list[dict[str, Any]]:
    """Build one tool schema per registered skill from registry metadata."""
    tools: list[dict[str, Any]] = []
    for s in skills:
        tools.append({
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.input_schema,
            },
        })
    return tools


def _build_tools(skills: list[SkillInfo]) -> list[dict[str, Any]]:
    """Build the full tool list: per-skill + static tools."""
    return _build_skill_tools(skills) + _STATIC_TOOLS


# ── System message ──────────────────────────────────────────────────

_SYSTEM_MESSAGE = """\
You are an intent classifier for a personal knowledge assistant.
Given the conversation so far, call exactly ONE tool to handle the user's latest message.

Rules:
- CREATIVE REQUESTS use "talk". If the user asks you to compose, draft, \
generate, or create content (e.g. "write a poem about X"), use "talk".
- SAVE/STORE REQUESTS call the skill directly. "write that to a note" \
calls write_note. "add that to my daily notes" calls create_daily_note.
- REFERENCES: If the user says "that", "it", "the result", "this", \
"all of this", "the above", "what you said", look at the conversation \
history to find what they're referring to and use the actual content. \
When saving content the user is referring to, set body to the actual \
content from the conversation — do NOT echo the user's instruction as body.
- For search/find queries, call search_notes directly.
- For help/skills/capabilities questions, use meta(command="help").
- For harmful or impossible requests, use talk with a polite refusal.
- If the request is ambiguous and you need more info, use clarify.\
"""


# ── Main parse entry point ──────────────────────────────────────────


def parse_intent(
    message: str,
    skills: list[SkillInfo],
    *,
    mode: ParseMode = "llm",
    history: list[dict[str, Any]] | None = None,
) -> ParseResult:
    """Parse user message into a structured intent.

    Returns:
        ParseResult(intent, warnings, tool_call).

    Args:
        message: Raw user input.
        skills: Available skill metadata (used for tool schemas).
        mode: "llm" tries Sparkstation first with deterministic fallback.
              "deterministic" requires explicit command prefixes only.
        history: Prior conversation messages for context (D020).
    """
    if mode == "deterministic":
        return ParseResult(_deterministic_parse(message, skills), [])
    return _llm_parse(message, skills, history=history)


# ── LLM tool-call parser (D019 → D020) ──────────────────────────────


def _llm_parse(
    message: str,
    skills: list[SkillInfo],
    *,
    history: list[dict[str, Any]] | None = None,
) -> ParseResult:
    """Tool-call-based parser with per-skill tools and history (D020)."""
    try:
        tools = _build_tools(skills)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_MESSAGE},
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        result = generate_tool_call(messages, tools)
        return _tool_call_to_intent(result.name, result.arguments, skills, result)
    except SparkUnavailableError:
        return ParseResult(_deterministic_parse(message, skills), [])
    except (SparkError, KeyError, TypeError, ValueError, ValidationError):
        return ParseResult(_deterministic_parse(message, skills), [])


def _tool_call_to_intent(
    tool_name: str,
    args: dict[str, Any],
    skills: list[SkillInfo],
    tool_call: ToolCallResult | None = None,
) -> ParseResult:
    """Convert a tool call result to an internal intent (D020)."""
    if tool_name == "talk":
        return ParseResult(
            TalkIntent(message=args.get("message", "")),
            [],
            tool_call,
        )

    if tool_name == "clarify":
        return ParseResult(
            ClarifyIntent(question=args.get("question", "")),
            [],
            tool_call,
        )

    if tool_name == "meta":
        cmd = args.get("command", "help")
        if cmd == "help":
            return ParseResult(HelpIntent(), [], tool_call)
        return ParseResult(TalkIntent(message=cmd), [], tool_call)

    # Per-skill tool: model calls skill_name directly (D020)
    skill_names = {s.name for s in skills}
    if tool_name in skill_names:
        if tool_name == "write_note":
            return ParseResult(
                WriteNoteIntent(
                    title=args.get("title", ""),
                    body=args.get("body", ""),
                ),
                [],
                tool_call,
            )
        return ParseResult(
            SkillInvocationIntent(skill_name=tool_name, input=args),
            [],
            tool_call,
        )

    # Unknown tool → fallback to talk
    return ParseResult(
        TalkIntent(message=args.get("message", "")),
        [],
        tool_call,
    )


# ── Deterministic fallback (frozen D018) ────────────────────────────


def _deterministic_parse(
    message: str, skills: list[SkillInfo],
) -> ParsedIntent:
    """Deterministic fallback parser — frozen stability floor (D018).

    Used when Sparkstation is unavailable.  These patterns are NOT a
    growth target — new skills MUST NOT add new regexes here.  The LLM
    prompt auto-discovers skills from the registry.

    Recognized prefixes:
    - summarize <path> [paragraph]  → SkillInvocationIntent(summarize_note)
    - summarize that/it/the result  → SkillInvocationIntent with ref:last
    - write <title>\\n<body>         → WriteNoteIntent
    - daily <content>               → SkillInvocationIntent(create_daily_note)
    - add to daily: <content>       → SkillInvocationIntent(create_daily_note)
    - search/find <query>           → SkillInvocationIntent(search_notes)
    - <skill_name> <json>           → SkillInvocationIntent (generic)

    Anything else returns TalkIntent.
    """
    msg = message.strip()
    lower = msg.lower()

    # help / skills / what can you do
    if _is_help_request(lower):
        return HelpIntent()

    # Reference detection: "summarize that/it/the result" → ref:last
    ref_intent = _detect_ref_pattern(msg, lower)
    if ref_intent is not None:
        return ref_intent

    # "summarize <path>" [paragraph] → sugar for summarize_note skill
    if lower.startswith("summarize "):
        rest = msg[len("summarize "):].strip()
        path = rest.split()[0] if rest.split() else rest
        style = "paragraph" if "paragraph" in lower else "bullet"
        if path:
            return SkillInvocationIntent(
                skill_name="summarize_note",
                input={"path": path, "style": style},
            )

    # "write <title>\n<body>" or "write note: <title>\n<body>"
    write_match = re.match(
        r"^write(?:\s+note)?[:\s]+(.+?)(?:\n(.+))?$",
        msg,
        re.DOTALL | re.IGNORECASE,
    )
    if write_match:
        title = write_match.group(1).strip()
        body = (write_match.group(2) or "").strip()
        if title:
            return WriteNoteIntent(title=title, body=body)

    # "daily <content>" or "add to daily: <content>"
    daily_match = re.match(
        r"^(?:daily|add\s+to\s+daily)[:\s]+(.+)$",
        msg,
        re.DOTALL | re.IGNORECASE,
    )
    if daily_match:
        content = daily_match.group(1).strip()
        if content:
            return SkillInvocationIntent(
                skill_name="create_daily_note",
                input={"content": content},
            )

    # "search <query>" or "find <query>"
    search_match = re.match(
        r"^(?:search|find)"
        r"(?:\s+(?:notes?\s+)?(?:about|for|on)?)?\s+(.+)$",
        msg,
        re.IGNORECASE,
    )
    if search_match:
        query = search_match.group(1).strip()
        if query:
            return SkillInvocationIntent(
                skill_name="search_notes",
                input={"query": query},
            )

    # Generic: "<skill_name> <json_or_args>" for any registered skill
    first_word = lower.split()[0] if lower.split() else ""
    skill_match = _find_skill_by_name(first_word, skills)
    if skill_match is not None:
        rest = msg[len(first_word):].strip()
        inp = _parse_generic_input(rest)
        return SkillInvocationIntent(
            skill_name=skill_match.name, input=inp,
        )

    return TalkIntent(message=msg)


# ── Reference detection (D015) ──────────────────────────────────────
# FROZEN (D018): These patterns are a stability floor for when
# Sparkstation is unavailable.  Do NOT add new per-skill regexes.

# Pronouns that refer to the most recent result
_REF_PRONOUNS = {"that", "it", "the result", "this"}

# Patterns: "summarize that [paragraph]", "write that to a note"
_SUMMARIZE_REF = re.compile(
    r"^summarize\s+(?:that|it|the\s+result|this)"
    r"(?:\s+(paragraph|bullet))?$",
    re.IGNORECASE,
)

_WRITE_REF = re.compile(
    r"^write\s+(?:that|it|the\s+result|this)"
    r"(?:\s+(?:to\s+)?(?:a\s+)?note)?$",
    re.IGNORECASE,
)

# "search/find for that" / "search for it" / "find that"
_SEARCH_REF = re.compile(
    r"^(?:search|find)\s+(?:(?:notes?\s+)?(?:about|for|on)\s+)?(?:that|it|the\s+result|this)$",
    re.IGNORECASE,
)

# "search again" / "find again"
_SEARCH_AGAIN = re.compile(
    r"^(?:search|find)\s+again$",
    re.IGNORECASE,
)

# Corrections: "but paragraph", "make it bullet", "no, paragraph"
_TRANSFORM_STYLE = re.compile(
    r"^(?:no,?\s*|actually,?\s*)?(?:I\s+meant\s+|make\s+it\s+|but\s+)?"
    r"(paragraph|bullet)\s*(?:style)?$",
    re.IGNORECASE,
)

# Corrections: "try notes/ml.md instead", "no, notes/ml.md"
_TRANSFORM_PATH = re.compile(
    r"^(?:no,?\s*|actually,?\s*)?(?:I\s+meant\s+|try\s+)?"
    r"(\S+\.md)\s*(?:instead)?$",
    re.IGNORECASE,
)

# "again" or "do it again" with optional style override
_AGAIN_REF = re.compile(
    r"^(?:do\s+it\s+)?again(?:\s+(paragraph|bullet))?$",
    re.IGNORECASE,
)


def _detect_ref_pattern(msg: str, lower: str) -> ParsedIntent | None:
    """Detect reference patterns and return intent with ref markers.

    Returns None if no ref pattern matched.
    """
    m = _SUMMARIZE_REF.match(lower)
    if m:
        style = m.group(1) or "bullet"
        return SkillInvocationIntent(
            skill_name="summarize_note",
            input={"path": "ref:last", "style": style},
        )

    m = _WRITE_REF.match(lower)
    if m:
        # "write that" → write_note with summary from last result as body
        return SkillInvocationIntent(
            skill_name="write_note",
            input={
                "path": "ref:last_written_path",
                "title": "ref:last_title",
                "body": "ref:last_body",
            },
        )

    m = _TRANSFORM_STYLE.match(lower)
    if m:
        return TransformIntent(overrides={"style": m.group(1).lower()})

    m = _TRANSFORM_PATH.match(lower)
    if m:
        return TransformIntent(overrides={"path": m.group(1)})

    m = _SEARCH_REF.match(lower)
    if m:
        return SkillInvocationIntent(
            skill_name="search_notes",
            input={"query": "ref:last"},
        )

    m = _SEARCH_AGAIN.match(lower)
    if m:
        return SkillInvocationIntent(
            skill_name="search_notes",
            input={"query": "ref:last_search"},
        )

    m = _AGAIN_REF.match(lower)
    if m:
        # "again" → re-invoke the last skill with its input
        style = m.group(1)
        inp: dict[str, Any] = {"ref:again": "true"}
        if style:
            inp["style"] = style
        return SkillInvocationIntent(
            skill_name="ref:last_skill",
            input=inp,
        )

    return None


_HELP_PATTERNS = re.compile(
    r"^(?:help|skills|commands|what can you do|what do you do|"
    r"what skills|list skills|show skills|capabilities)\s*\??$",
    re.IGNORECASE,
)


def _is_help_request(lower: str) -> bool:
    """Return True if the message is a help/skills query."""
    return _HELP_PATTERNS.match(lower) is not None


def _find_skill_by_name(
    name: str, skills: list[SkillInfo],
) -> SkillInfo | None:
    """Match a token against registered skill names."""
    for s in skills:
        if s.name == name:
            return s
    return None


def _parse_generic_input(rest: str) -> dict[str, Any]:
    """Try to parse remaining text as JSON, otherwise return as raw query."""
    if not rest:
        return {}
    try:
        result: dict[str, Any] = json.loads(rest)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return {"query": rest}
