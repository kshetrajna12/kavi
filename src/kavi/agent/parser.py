"""Intent parser — LLM-based with deterministic fallback.

Two parse modes:
- "llm": Tries Sparkstation first, falls back to deterministic on failure.
- "deterministic": Requires explicit command prefixes (search/find,
  summarize, write). Rejects ambiguous input with help text.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from kavi.agent.models import (
    ParsedIntent,
    SearchAndSummarizeIntent,
    SummarizeNoteIntent,
    UnsupportedIntent,
    WriteNoteIntent,
)
from kavi.consumer.shim import SkillInfo
from kavi.llm.spark import SparkUnavailableError, generate

_SYSTEM_PROMPT = """\
You are an intent parser for a note-taking assistant.
Given a user message, extract the intent as strict JSON matching one of \
these schemas:

1. Search and summarize notes:
   {"kind": "search_and_summarize", "query": "<terms>",
    "top_k": <int>, "style": "bullet"|"paragraph"}

2. Summarize a specific note:
   {"kind": "summarize_note", "path": "<file path>",
    "style": "bullet"|"paragraph"}

3. Write a new note:
   {"kind": "write_note", "title": "<note title>",
    "body": "<note content>"}

4. Anything else:
   {"kind": "unsupported",
    "message": "<explain what is supported>"}

Rules:
- Output ONLY valid JSON, no markdown fences, no extra text.
- top_k defaults to 5, style defaults to "bullet" if not specified.
- If the user mentions a .md file path, use summarize_note.
- If the user wants to find/search notes, use search_and_summarize.
- If the user wants to write/create a note, use write_note.
"""

_UNSUPPORTED_HELP = (
    "Unknown command. Available commands: "
    "search <query>, find <query>, "
    "summarize <path>, write <title>"
)

ParseMode = Literal["llm", "deterministic"]


def parse_intent(
    message: str,
    skills: list[SkillInfo],
    *,
    mode: ParseMode = "llm",
) -> ParsedIntent:
    """Parse user message into a structured intent.

    Args:
        message: Raw user input.
        skills: Available skill metadata (used for LLM prompt context).
        mode: "llm" tries Sparkstation first with deterministic fallback.
              "deterministic" requires explicit command prefixes only.
    """
    if mode == "deterministic":
        return _deterministic_parse(message)
    return _llm_parse(message, skills)


def _llm_parse(message: str, skills: list[SkillInfo]) -> ParsedIntent:
    """LLM-based parser with deterministic fallback on failure."""
    try:
        prompt = _build_prompt(message, skills)
        raw = generate(prompt)
        data = _parse_json_response(raw)
        return _dict_to_intent(data)
    except SparkUnavailableError:
        return _deterministic_parse(message)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _deterministic_parse(message)


def _deterministic_parse(message: str) -> ParsedIntent:
    """Deterministic heuristic parser — requires explicit command prefixes.

    Recognized prefixes: summarize, write, search, find.
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

    return UnsupportedIntent(message=_UNSUPPORTED_HELP)


# ── LLM helpers ──────────────────────────────────────────────────────


def _build_prompt(message: str, skills: list[SkillInfo]) -> str:
    skill_names = ", ".join(s.name for s in skills)
    return (
        f"{_SYSTEM_PROMPT}\n"
        f"Available skills: {skill_names}\n\n"
        f"User message: {message}\n\n"
        f"JSON:"
    )


def _parse_json_response(raw: str) -> dict:
    """Extract JSON from LLM response, tolerating markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [x for x in lines if not x.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _dict_to_intent(data: dict) -> ParsedIntent:
    """Convert a parsed dict to the appropriate intent model."""
    kind = data.get("kind")
    if kind == "search_and_summarize":
        return SearchAndSummarizeIntent(**data)
    if kind == "summarize_note":
        return SummarizeNoteIntent(**data)
    if kind == "write_note":
        return WriteNoteIntent(**data)
    return UnsupportedIntent(
        message=data.get("message", "Could not determine intent."),
    )
