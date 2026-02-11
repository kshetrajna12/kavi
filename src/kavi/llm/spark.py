"""Sparkstation client — healthcheck, bounded generation, embeddings, clean errors.

D019: generate() takes messages: list[dict] (role-separated).
      generate_tool_call() returns structured tool calls for intent parsing.
"""

from __future__ import annotations

from typing import Any, NamedTuple

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


class ToolCallResult(NamedTuple):
    """Structured result from a tool-call completion."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""


def is_available(base_url: str = SPARK_BASE_URL, timeout: float = 5) -> bool:
    """Return True if Sparkstation responds to a model list request."""
    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        client.models.list()
        return True
    except Exception:
        return False


def _content_len(msg: dict[str, Any]) -> int:
    """Return char length of a message's content (handles None)."""
    c = msg.get("content")
    if c is None:
        return 0
    return len(str(c))


def _truncate_messages(
    messages: list[dict[str, Any]],
    max_chars: int,
) -> list[dict[str, Any]]:
    """Drop oldest history messages to fit within max_chars (D020).

    Rules:
    1. Never drop the system message (first) or current user message (last).
    2. Drop oldest history messages first.  Keep tool-call groups atomic:
       assistant+tool_calls, tool, assistant = 3 messages together.
    3. Last resort: truncate the current user message.
    """
    total = sum(_content_len(m) for m in messages)
    if total <= max_chars:
        return messages

    result = list(messages)

    # Identify protected indices: system (first if role=system) and last message
    first_history = 1 if result and result[0].get("role") == "system" else 0

    # Drop from oldest history until within budget or only protected remain
    while sum(_content_len(m) for m in result) > max_chars:
        if first_history >= len(result) - 1:
            break  # only system + last message remain

        msg = result[first_history]
        # Tool-call group: assistant with tool_calls + tool + assistant = 3 msgs
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            count = min(3, len(result) - first_history - 1)  # don't eat last
            del result[first_history:first_history + count]
        else:
            del result[first_history]

    # Last resort: truncate the last user message
    total = sum(_content_len(m) for m in result)
    if total > max_chars:
        overshoot = total - max_chars
        for i in range(len(result) - 1, -1, -1):
            if result[i].get("role") == "user":
                content = str(result[i].get("content", ""))
                if len(content) > overshoot:
                    result[i] = {**result[i], "content": content[:-overshoot]}
                else:
                    result[i] = {**result[i], "content": ""}
                break

    return result


def generate(
    messages: list[dict[str, Any]],
    *,
    model: str = SPARK_MODEL,
    base_url: str = SPARK_BASE_URL,
    temperature: float = 0,
    timeout: float = SPARK_TIMEOUT,
    max_prompt_chars: int = SPARK_MAX_PROMPT_CHARS,
) -> str:
    """Chat completion via Sparkstation. Returns content string.

    Args:
        messages: List of role-separated messages (system/user/assistant).
        model: Sparkstation model name.
        temperature: Sampling temperature.
        timeout: Request timeout in seconds.
        max_prompt_chars: Truncate last user message if total exceeds this.

    Raises SparkUnavailableError if the gateway is unreachable,
    SparkError on unexpected response issues.
    """
    messages = _truncate_messages(messages, max_prompt_chars)

    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
    except Exception as exc:
        raise SparkUnavailableError(f"Sparkstation unreachable: {exc}") from exc

    choice = response.choices[0] if response.choices else None
    if choice is None or choice.message.content is None:
        raise SparkError("Sparkstation returned empty response")

    return choice.message.content


def generate_tool_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    model: str = SPARK_MODEL,
    base_url: str = SPARK_BASE_URL,
    temperature: float = 0,
    timeout: float = SPARK_TIMEOUT,
    max_prompt_chars: int = SPARK_MAX_PROMPT_CHARS,
    tool_choice: str | dict[str, Any] = "auto",
) -> ToolCallResult:
    """Chat completion expecting a tool call. Returns tool name + args.

    Raises SparkUnavailableError if the gateway is unreachable,
    SparkError if no tool call in response or response is malformed.
    """
    import json

    messages = _truncate_messages(messages, max_prompt_chars)

    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
    except Exception as exc:
        raise SparkUnavailableError(f"Sparkstation unreachable: {exc}") from exc

    choice = response.choices[0] if response.choices else None
    if choice is None:
        raise SparkError("Sparkstation returned empty response")

    # Extract tool call
    tool_calls = choice.message.tool_calls
    if not tool_calls:
        raise SparkError("Sparkstation returned no tool call")

    tc = tool_calls[0]
    try:
        args: dict[str, Any] = json.loads(tc.function.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SparkError(f"Invalid tool call arguments: {exc}") from exc

    return ToolCallResult(name=tc.function.name, arguments=args, call_id=tc.id or "")


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
