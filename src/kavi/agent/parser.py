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
    ParsedIntent,
    SearchAndSummarizeIntent,
    SkillInvocationIntent,
    SummarizeNoteIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.consumer.shim import SkillInfo
from kavi.llm.spark import SparkUnavailableError, generate

# Intents with custom wiring — everything else goes through SkillInvocationIntent
_CUSTOM_INTENT_SKILLS = {"search_notes", "summarize_note", "write_note"}

_SYSTEM_PROMPT_HEADER = """\
You are an intent parser for a personal assistant with access to skills.
Given a user message, extract ONE intent as strict JSON matching one of \
these schemas:

1. Search and summarize notes:
   {"kind": "search_and_summarize", "query": "<terms>",
    "top_k": <int>, "style": "bullet"|"paragraph",
    "warnings": ["..."]}

2. Summarize a specific note:
   {"kind": "summarize_note", "path": "<file path>",
    "style": "bullet"|"paragraph", "warnings": ["..."]}

3. Write a new note:
   {"kind": "write_note", "title": "<note title>",
    "body": "<note content>", "warnings": ["..."]}

4. Invoke a specific skill (for any skill not covered above):
   {"kind": "skill_invocation", "skill_name": "<name>",
    "input": {<matching the skill's input schema>},
    "warnings": ["..."]}

5. Anything else:
   {"kind": "unsupported",
    "message": "<explain what is supported>",
    "warnings": ["..."]}

Rules:
- Output ONLY valid JSON, no markdown fences, no extra text.
- top_k defaults to 5, style defaults to "bullet" if not specified.
- If the user mentions a .md file path, use summarize_note.
- If the user wants to find/search notes, use search_and_summarize.
- If the user wants to write/create a note, use write_note.
- For any other skill, use skill_invocation with the correct skill_name \
and input fields.
- Each message maps to EXACTLY ONE intent. Pick the best-fitting one.
- If the message requests multiple actions, pick the primary intent and \
add a warning for each ignored action.
- Never infer write_note as part of search_and_summarize or vice versa.
- For write_note, both title and body are required. If body is missing,
  still return write_note with an empty body string.
- Omit the "warnings" field entirely if there are no warnings.
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

    Recognized prefixes: summarize, write, search, find, tags, fetch,
    and any registered skill name.
    Anything else returns UnsupportedIntent with help text.
    """
    msg = message.strip()
    lower = msg.lower()

    # "summarize <path>" [paragraph]
    if lower.startswith("summarize "):
        rest = msg[len("summarize "):].strip()
        path = rest.split()[0] if rest.split() else rest
        style: Literal["bullet", "paragraph"] = "bullet"
        if "paragraph" in lower:
            style = "paragraph"
        if path:
            return SummarizeNoteIntent(path=path, style=style)

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

    # "tags <tag>" → read_notes_by_tag
    tags_match = re.match(r"^tags?\s+(.+)$", msg, re.IGNORECASE)
    if tags_match:
        tag = tags_match.group(1).strip()
        if tag and _has_skill("read_notes_by_tag", skills):
            return SkillInvocationIntent(
                skill_name="read_notes_by_tag", input={"tag": tag},
            )

    # "fetch <url>" → http_get_json
    fetch_match = re.match(r"^fetch\s+(https?://\S+)$", msg, re.IGNORECASE)
    if fetch_match:
        url = fetch_match.group(1).strip()
        if _has_skill("http_get_json", skills):
            from urllib.parse import urlparse

            host = urlparse(url).hostname or ""
            return SkillInvocationIntent(
                skill_name="http_get_json",
                input={"url": url, "allowed_hosts": [host]},
            )

    # Generic: "<skill_name> <json>" for any registered skill
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


def _has_skill(name: str, skills: list[SkillInfo]) -> bool:
    return any(s.name == name for s in skills)


def _find_skill_by_name(
    name: str, skills: list[SkillInfo],
) -> SkillInfo | None:
    """Match a token against registered skill names (excluding custom-wired ones)."""
    for s in skills:
        if s.name == name and s.name not in _CUSTOM_INTENT_SKILLS:
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
    """Build help text listing non-custom skills."""
    extra = [s.name for s in skills if s.name not in _CUSTOM_INTENT_SKILLS]
    if not extra:
        return ""
    return ", " + ", ".join(f"{n} <input>" for n in sorted(extra))


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
    if kind == "summarize_note":
        return SummarizeNoteIntent(**data)
    if kind == "write_note":
        return WriteNoteIntent(**data)
    if kind == "skill_invocation":
        return SkillInvocationIntent(**data)
    return UnsupportedIntent(
        message=data.get("message", "Could not determine intent."),
    )
