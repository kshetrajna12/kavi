"""Intent parser — tool-call-first with deterministic fallback (D018, D019).

Two parse modes:
- "llm" (default): Sparkstation classifies every turn via tool calling
  into a structured intent.  Falls back to deterministic on Sparkstation
  failure or malformed LLM output.
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
    SearchAndSummarizeIntent,
    SkillInvocationIntent,
    TalkIntent,
    TransformIntent,
    WriteNoteIntent,
)
from kavi.consumer.shim import SkillInfo
from kavi.llm.spark import SparkError, SparkUnavailableError, generate_tool_call

ParseMode = Literal["llm", "deterministic"]


class ParseResult(NamedTuple):
    """Return type of ``parse_intent`` — intent plus optional warnings."""

    intent: ParsedIntent
    warnings: list[str]


# ── Tool schemas (D019) ──────────────────────────────────────────────

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

TOOL_INVOKE_SKILL = {
    "type": "function",
    "function": {
        "name": "invoke_skill",
        "description": "Trigger a governed skill by name with input.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to invoke.",
                },
                "input": {
                    "type": "object",
                    "description": "Input fields matching the skill's input schema.",
                },
            },
            "required": ["skill_name", "input"],
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

TOOLS = [TOOL_TALK, TOOL_INVOKE_SKILL, TOOL_CLARIFY, TOOL_META]


# ── System message builder ───────────────────────────────────────────

_SYSTEM_HEADER = """\
You are an intent classifier for a personal knowledge assistant.
Given a user message, call exactly ONE tool to classify the intent.

Rules:
- CREATIVE REQUESTS are "talk", NOT invoke_skill. If the user asks you to \
compose, draft, generate, or create content (e.g. "write a poem about X", \
"write me a story", "draft an email about Y"), this is a generation request \
— use "talk". The user wants YOU to produce the content, not to save something.
- SAVE/STORE REQUESTS use invoke_skill. "write that to a note" saves prior \
content to a file. "add that to my daily notes" saves to daily notes.
- The distinction: "write a poem about dogs" → talk. \
"write that to a note" → invoke_skill(write_note).
- REFERENCES: If the user says "that", "it", "the result", use "ref:last" \
as the value. For skill-specific refs, use "ref:last_<skill>".
- For CORRECTIONS ("but paragraph", "make it shorter", "try X instead"), \
use invoke_skill with the corrected parameters and "ref:last" for unchanged fields.
- For search/find queries, use invoke_skill with skill_name "search_notes" \
or "search_and_summarize" (2-step chain: search → summarize).
- If the user mentions "daily notes" or "daily" as a target, route to \
invoke_skill with skill_name "create_daily_note".
- For help/skills/capabilities questions, use meta(command="help").
- For harmful or impossible requests, use talk with a polite refusal.
- If the request is ambiguous and you need more info, use clarify.\
"""


def _build_system_message(skills: list[SkillInfo]) -> str:
    """Build the system message with dynamic skill section."""
    skill_section = _build_skill_section(skills)
    return f"{_SYSTEM_HEADER}\n\n{skill_section}"


def _build_skill_section(skills: list[SkillInfo]) -> str:
    """Build a dynamic skill reference for the system message."""
    lines = ["Available skills:"]
    for s in skills:
        required = _get_required_fields(s.input_schema)
        fields_str = ", ".join(f"{k}: {v}" for k, v in required.items())
        lines.append(f"- {s.name} ({s.side_effect_class}): {s.description}")
        if fields_str:
            lines.append(f"  Input fields: {fields_str}")
    return "\n".join(lines)


def _get_required_fields(schema: dict[str, Any]) -> dict[str, str]:
    """Extract required field names and types from a JSON schema."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    result: dict[str, str] = {}
    for name, info in props.items():
        if name in required:
            result[name] = info.get("type", "any")
    return result


# ── Main parse entry point ───────────────────────────────────────────


def parse_intent(
    message: str,
    skills: list[SkillInfo],
    *,
    mode: ParseMode = "llm",
) -> ParseResult:
    """Parse user message into a structured intent.

    Returns:
        ParseResult(intent, warnings).  ``warnings`` is non-empty when
        parts of the user request were ignored (e.g. trailing intents).

    Args:
        message: Raw user input.
        skills: Available skill metadata (used for LLM prompt context).
        mode: "llm" tries Sparkstation first with deterministic fallback.
              "deterministic" requires explicit command prefixes only.
    """
    if mode == "deterministic":
        return ParseResult(_deterministic_parse(message, skills), [])
    return _llm_parse(message, skills)


# ── LLM tool-call parser (D019) ─────────────────────────────────────


def _llm_parse(
    message: str, skills: list[SkillInfo],
) -> ParseResult:
    """Tool-call-based parser with deterministic fallback on failure."""
    try:
        system = _build_system_message(skills)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]
        result = generate_tool_call(messages, TOOLS)
        return _tool_call_to_intent(result.name, result.arguments)
    except SparkUnavailableError:
        return ParseResult(_deterministic_parse(message, skills), [])
    except (SparkError, KeyError, TypeError, ValueError, ValidationError):
        return ParseResult(_deterministic_parse(message, skills), [])


def _tool_call_to_intent(
    tool_name: str, args: dict[str, Any],
) -> ParseResult:
    """Convert a tool call result to an internal intent."""
    if tool_name == "talk":
        return ParseResult(
            TalkIntent(message=args.get("message", "")),
            [],
        )

    if tool_name == "invoke_skill":
        skill_name = args.get("skill_name", "")
        inp = args.get("input", {})
        if not isinstance(inp, dict):
            inp = {}
        # Route well-known compound intents
        if skill_name == "search_and_summarize":
            return ParseResult(
                SearchAndSummarizeIntent(
                    query=inp.get("query", ""),
                    top_k=inp.get("top_k", 5),
                    style=inp.get("style", "bullet"),
                ),
                [],
            )
        if skill_name == "write_note":
            return ParseResult(
                WriteNoteIntent(
                    title=inp.get("title", ""),
                    body=inp.get("body", ""),
                ),
                [],
            )
        return ParseResult(
            SkillInvocationIntent(skill_name=skill_name, input=inp),
            [],
        )

    if tool_name == "clarify":
        return ParseResult(
            ClarifyIntent(question=args.get("question", "")),
            [],
        )

    if tool_name == "meta":
        cmd = args.get("command", "help")
        if cmd == "help":
            return ParseResult(HelpIntent(), [])
        # Other meta commands treated as talk for now
        return ParseResult(TalkIntent(message=cmd), [])

    # Unknown tool → fallback to talk
    return ParseResult(
        TalkIntent(message=args.get("message", "")),
        [],
    )


# ── Deterministic fallback (frozen D018) ─────────────────────────────


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
    - search/find <query>           → SearchAndSummarizeIntent
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
            return SearchAndSummarizeIntent(query=query)

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


# ── Reference detection (D015) ────────────────────────────────────────
# FROZEN (D018): These patterns are a stability floor for when
# Sparkstation is unavailable.  Do NOT add new per-skill regexes.
# The LLM parser handles all routing via _build_skill_section().

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
        return SearchAndSummarizeIntent(query="ref:last")

    m = _SEARCH_AGAIN.match(lower)
    if m:
        return SearchAndSummarizeIntent(query="ref:last_search")

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


def _available_skill_commands(skills: list[SkillInfo]) -> str:
    """Build help text listing skills available via generic invocation."""
    names = [s.name for s in skills]
    if not names:
        return ""
    return ", " + ", ".join(f"{n} <input>" for n in sorted(names))
