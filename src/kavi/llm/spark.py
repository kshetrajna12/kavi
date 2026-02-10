"""Sparkstation client — healthcheck, bounded generation, clean errors."""

from __future__ import annotations

from openai import OpenAI

from kavi.config import SPARK_BASE_URL, SPARK_MAX_PROMPT_CHARS, SPARK_MODEL, SPARK_TIMEOUT


class SparkError(Exception):
    """Base error for Sparkstation operations."""


class SparkUnavailableError(SparkError):
    """Sparkstation gateway is unreachable or not responding."""


def is_available(base_url: str = SPARK_BASE_URL, timeout: float = 5) -> bool:
    """Return True if Sparkstation responds to a model list request."""
    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        client.models.list()
        return True
    except Exception:
        return False


def generate(
    prompt: str,
    *,
    model: str = SPARK_MODEL,
    base_url: str = SPARK_BASE_URL,
    temperature: float = 0,
    timeout: float = SPARK_TIMEOUT,
    max_prompt_chars: int = SPARK_MAX_PROMPT_CHARS,
) -> str:
    """Chat completion via Sparkstation. Truncates prompt, enforces timeout.

    Raises SparkUnavailable if the gateway is unreachable,
    SparkError on unexpected response issues.
    """
    if len(prompt) > max_prompt_chars:
        prompt = prompt[:max_prompt_chars]

    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
    except Exception as exc:
        raise SparkUnavailableError(f"Sparkstation unreachable: {exc}") from exc

    choice = response.choices[0] if response.choices else None
    if choice is None or choice.message.content is None:
        raise SparkError("Sparkstation returned empty response")

    return choice.message.content
