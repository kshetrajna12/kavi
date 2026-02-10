"""Sparkstation client — healthcheck, bounded generation, embeddings, clean errors."""

from __future__ import annotations

from openai import OpenAI

from kavi.config import (
    SPARK_BASE_URL,
    SPARK_EMBED_MODEL,
    SPARK_MAX_PROMPT_CHARS,
    SPARK_MODEL,
    SPARK_TIMEOUT,
)


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


def embed(
    texts: list[str],
    *,
    model: str = SPARK_EMBED_MODEL,
    base_url: str = SPARK_BASE_URL,
    timeout: float = SPARK_TIMEOUT,
) -> list[list[float]]:
    """Return embedding vectors for a batch of texts via Sparkstation.

    Raises SparkUnavailableError if the gateway is unreachable,
    SparkError on unexpected response issues.
    """
    if not texts:
        return []

    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        response = client.embeddings.create(model=model, input=texts)
    except Exception as exc:
        raise SparkUnavailableError(f"Sparkstation unreachable: {exc}") from exc

    if not response.data:
        raise SparkError("Sparkstation returned empty embeddings response")

    # Sort by index to preserve input order
    sorted_data = sorted(response.data, key=lambda d: d.index)
    return [d.embedding for d in sorted_data]
