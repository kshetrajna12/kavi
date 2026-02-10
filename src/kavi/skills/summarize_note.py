"""Skill: summarize_note — Summarize a markdown note via Sparkstation LLM."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal

from kavi.config import SPARK_MODEL
from kavi.llm.spark import SparkError, SparkUnavailableError, generate
from kavi.skills.base import BaseSkill, SkillInput, SkillOutput

VAULT_OUT = Path("vault_out")

_FALLBACK_PREFIX = "[Fallback summary] "
_FALLBACK_CHARS = 500

# Classified error codes — short, greppable, stable across log entries.
_ERROR_CODES: dict[type, str] = {
    SparkUnavailableError: "SPARKSTATION_UNAVAILABLE",
    SparkError: "SPARKSTATION_ERROR",
    json.JSONDecodeError: "SPARKSTATION_BAD_JSON",
    KeyError: "SPARKSTATION_BAD_SCHEMA",
    TypeError: "SPARKSTATION_BAD_SCHEMA",
}


class SummarizeNoteInput(SkillInput):
    """Input for summarize_note skill."""

    path: str
    style: Literal["bullet", "paragraph"] = "bullet"
    max_chars: int = 12000
    timeout_s: float = 12.0


class SummarizeNoteOutput(SkillOutput):
    """Output for summarize_note skill."""

    path: str
    summary: str
    key_points: list[str]
    truncated: bool
    used_model: str
    error: str | None = None


class SummarizeNoteSkill(BaseSkill):
    """Summarize an existing markdown note from the vault using Sparkstation."""

    name = "summarize_note"
    description = "Summarize an existing markdown note from the vault using Sparkstation"
    input_model = SummarizeNoteInput
    output_model = SummarizeNoteOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: SummarizeNoteInput) -> SummarizeNoteOutput:  # type: ignore[override]
        path_str = input_data.path
        rel = PurePosixPath(path_str)

        # Reject absolute paths and path traversal
        if rel.is_absolute() or ".." in rel.parts:
            msg = f"Invalid path: {path_str}"
            raise ValueError(msg)

        target = VAULT_OUT / rel

        # Reject symlinks
        if target.is_symlink():
            msg = f"Symlinks not allowed: {path_str}"
            raise ValueError(msg)

        # Reject non-existent files
        if not target.is_file():
            msg = f"File not found: {path_str}"
            raise ValueError(msg)

        # Read file content
        content = target.read_text(encoding="utf-8")
        truncated = len(content) > input_data.max_chars
        if truncated:
            content = content[: input_data.max_chars]

        # Build LLM prompt
        prompt = (
            f"Summarize the following markdown note in {input_data.style} style.\n"
            "Return ONLY a JSON object with keys:\n"
            '- "summary": a string summary\n'
            '- "key_points": a list of strings with key points\n\n'
            f"Note content:\n{content}"
        )

        # Attempt LLM call
        try:
            raw = generate(prompt, model=SPARK_MODEL, timeout=input_data.timeout_s)
            parsed = json.loads(raw)
            summary = str(parsed["summary"])
            key_points = [str(kp) for kp in parsed["key_points"]]
            return SummarizeNoteOutput(
                path=path_str,
                summary=summary,
                key_points=key_points,
                truncated=truncated,
                used_model=SPARK_MODEL,
            )
        except (
            SparkUnavailableError, SparkError, json.JSONDecodeError, KeyError, TypeError,
        ) as exc:
            fallback_text = content[:_FALLBACK_CHARS]
            code = _ERROR_CODES.get(type(exc))
            if code is None:
                # Walk MRO for subclass matches (e.g. SparkUnavailableError → SparkError)
                for cls, c in _ERROR_CODES.items():
                    if isinstance(exc, cls):
                        code = c
                        break
                else:
                    code = "SPARKSTATION_UNKNOWN"
            return SummarizeNoteOutput(
                path=path_str,
                summary=f"{_FALLBACK_PREFIX}{fallback_text}",
                key_points=[],
                truncated=truncated,
                used_model="fallback",
                error=code,
            )
