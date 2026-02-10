"""Intent parser — LLM-based with deterministic fallback.

Two parse modes:
- "llm": Tries Sparkstation first, falls back to deterministic on failure.
- "deterministic": Requires explicit command prefixes (search/find,
  summarize, write) or skill-name prefixes. Rejects ambiguous input
  with help text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, NamedTuple

from kavi.agent.models import (
    HelpIntent,
    ParsedIntent,
    SearchAndSummarizeIntent,
    SkillInvocationIntent,
    TransformIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.consumer.shim import SkillInfo
from kavi.llm.spark import SparkUnavailableError, generate

_SYSTEM_PROMPT_HEADER = """\
You are an intent parser for a personal assistant with access to skills.
Given a user message, extract ONE intent as strict JSON matching one of \
these schemas:

1. Search and summarize notes (2-step chain: search → summarize):
   {"kind": "search_and_summarize", "query": "<terms>",
    "top_k": <int>, "style": "bullet"|"paragraph",
    "warnings": ["..."]}

2. Write a new note:
   {"kind": "write_note", "title": "<note title>",
    "body": "<note content>", "warnings": ["..."]}

3. Invoke a specific skill:
   {"kind": "skill_invocation", "skill_name": "<name>",
    "input": {<matching the skill's input schema>},
    "warnings": ["..."]}

4. Refine/correct a prior result (change a parameter):
   {"kind": "transform", "overrides": {"field": "value"},
    "target_ref": "last", "warnings": ["..."]}

5. Help / list skills / capabilities:
   {"kind": "help", "warnings": ["..."]}

6. Anything else:
   {"kind": "unsupported",
    "message": "<explain what is supported>",
    "warnings": ["..."]}

Rules:
- Output ONLY valid JSON, no markdown fences, no extra text.
- top_k defaults to 5, style defaults to "bullet" if not specified.
- If the user wants to find/search notes, use search_and_summarize.
- If the user wants to write/create a note, use write_note.
- If the user mentions a specific .md file path to summarize, use \
skill_invocation with skill_name "summarize_note".
- For any other skill, use skill_invocation with the correct skill_name \
and input fields matching the skill's input schema.
- Each message maps to EXACTLY ONE intent. Pick the best-fitting one.
- If the message requests multiple actions, pick the primary intent and \
add a warning for each ignored action.
- Never infer write_note as part of search_and_summarize or vice versa.
- For write_note, both title and body are required. If body is missing,
  still return write_note with an empty body string.
- Omit the "warnings" field entirely if there are no warnings.
- CORRECTIONS: If the user says "no, I meant", "but paragraph", "make it \
shorter", "try X instead" — use "transform" with overrides containing the \
changed fields. target_ref defaults to "last". Example: "but paragraph" → \
{"kind": "transform", "overrides": {"style": "paragraph"}}.
- REFERENCES: If the user says "that", "it", "the result", "again", or \
refers to a prior result, use "ref:last" as the value for the relevant \
input field. For example: "summarize that" becomes \
{"kind": "skill_invocation", "skill_name": "summarize_note", \
"input": {"path": "ref:last"}}. Use "ref:last_<skill>" to reference the \
most recent result of a specific skill (e.g. "ref:last_search").
"""

ParseMode = Literal["llm", "deterministic"]


class ParseResult(NamedTuple):
    """Return type of ``parse_intent`` — intent plus optional warnings."""

    intent: ParsedIntent
    warnings: list[str]


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


def _llm_parse(
    message: str, skills: list[SkillInfo],
) -> ParseResult:
    """LLM-based parser with deterministic fallback on failure."""
    try:
        prompt = _build_prompt(message, skills)
        raw = generate(prompt)
        data = _parse_json_response(raw)
        raw_warnings = data.pop("warnings", None)
        warnings: list[str] = list(raw_warnings) if isinstance(raw_warnings, list) else []
        return ParseResult(_dict_to_intent(data), warnings)
    except SparkUnavailableError:
        return ParseResult(_deterministic_parse(message, skills), [])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ParseResult(_deterministic_parse(message, skills), [])


def _deterministic_parse(
    message: str, skills: list[SkillInfo],
) -> ParsedIntent:
    """Deterministic heuristic parser — requires explicit command prefixes.

    Recognized prefixes:
    - summarize <path> [paragraph]  → SkillInvocationIntent(summarize_note)
    - summarize that/it/the result  → SkillInvocationIntent with ref:last
    - write <title>\\n<body>         → WriteNoteIntent
    - daily <content>               → SkillInvocationIntent(create_daily_note)
    - add to daily: <content>       → SkillInvocationIntent(create_daily_note)
    - search/find <query>           → SearchAndSummarizeIntent
    - <skill_name> <json>           → SkillInvocationIntent (generic)

    Anything else returns UnsupportedIntent with help text.
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

    skill_names = _available_skill_commands(skills)
    return UnsupportedIntent(
        message=f"Unknown command. Available commands: "
        f"search <query>, find <query>, summarize <path>, "
        f"write <title>{skill_names}",
    )


# ── Reference detection (D015) ────────────────────────────────────────

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


# ── LLM helpers ──────────────────────────────────────────────────────


def _build_prompt(message: str, skills: list[SkillInfo]) -> str:
    skill_section = _build_skill_section(skills)
    return (
        f"{_SYSTEM_PROMPT_HEADER}\n"
        f"{skill_section}\n\n"
        f"User message: {message}\n\n"
        f"JSON:"
    )


def _build_skill_section(skills: list[SkillInfo]) -> str:
    """Build a dynamic skill reference for the LLM prompt."""
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


def _parse_json_response(raw: str) -> dict[str, object]:
    """Extract JSON from LLM response, tolerating markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [x for x in lines if not x.strip().startswith("```")]
        text = "\n".join(lines).strip()
    result: dict[str, object] = json.loads(text)
    return result


def _dict_to_intent(data: dict) -> ParsedIntent:
    """Convert a parsed dict to the appropriate intent model."""
    kind = data.get("kind")
    if kind == "search_and_summarize":
        return SearchAndSummarizeIntent(**data)
    if kind == "write_note":
        return WriteNoteIntent(**data)
    if kind == "skill_invocation":
        return SkillInvocationIntent(**data)
    if kind == "transform":
        return TransformIntent(**data)
    if kind == "help":
        return HelpIntent()
    # Backward compat: LLM may still emit summarize_note → convert
    if kind == "summarize_note":
        return SkillInvocationIntent(
            skill_name="summarize_note",
            input={k: v for k, v in data.items() if k != "kind"},
        )
    return UnsupportedIntent(
        message=data.get("message", "Could not determine intent."),
    )
