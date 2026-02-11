"""Unit tests for kavi.llm.spark — fully mocked, no network."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kavi.llm.spark import (
    SparkError,
    SparkUnavailableError,
    ToolCallResult,
    _truncate_messages,
    embed,
    generate,
    generate_tool_call,
    is_available,
)

# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


@patch("kavi.llm.spark.OpenAI")
def test_is_available_returns_true_when_healthy(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    assert is_available() is True
    mock_client.models.list.assert_called_once()


@patch("kavi.llm.spark.OpenAI")
def test_is_available_returns_false_on_connection_error(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.models.list.side_effect = ConnectionError("refused")
    mock_openai_cls.return_value = mock_client
    assert is_available() is False


@patch("kavi.llm.spark.OpenAI")
def test_is_available_returns_false_on_timeout(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.models.list.side_effect = TimeoutError("timed out")
    mock_openai_cls.return_value = mock_client
    assert is_available() is False


# ---------------------------------------------------------------------------
# generate (D019: messages API)
# ---------------------------------------------------------------------------


def _mock_response(content: str) -> MagicMock:
    """Build a mock chat completion response."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@patch("kavi.llm.spark.OpenAI")
def test_generate_returns_content(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_response("hello world")
    mock_openai_cls.return_value = mock_client

    result = generate([{"role": "user", "content": "Say hello"}])
    assert result == "hello world"


@patch("kavi.llm.spark.OpenAI")
def test_generate_passes_messages_directly(mock_openai_cls: MagicMock) -> None:
    """Messages are passed through to the API as-is."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_response("ok")
    mock_openai_cls.return_value = mock_client

    msgs = [
        {"role": "system", "content": "You are a helper."},
        {"role": "user", "content": "Hello"},
    ]
    generate(msgs)

    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs["messages"] == msgs


@patch("kavi.llm.spark.OpenAI")
def test_generate_truncates_last_user_message(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_response("ok")
    mock_openai_cls.return_value = mock_client

    msgs = [
        {"role": "system", "content": "short"},  # 5 chars
        {"role": "user", "content": "x" * 200},  # 200 chars
    ]
    generate(msgs, max_prompt_chars=100)

    call_args = mock_client.chat.completions.create.call_args
    sent = call_args.kwargs["messages"]
    # System message preserved, user message truncated
    assert sent[0]["content"] == "short"
    total = sum(len(m["content"]) for m in sent)
    assert total <= 100


@patch("kavi.llm.spark.OpenAI")
def test_generate_never_truncates_system(mock_openai_cls: MagicMock) -> None:
    """System messages are never truncated, only user messages."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_response("ok")
    mock_openai_cls.return_value = mock_client

    system_content = "s" * 80
    msgs = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "u" * 80},
    ]
    generate(msgs, max_prompt_chars=100)

    call_args = mock_client.chat.completions.create.call_args
    sent = call_args.kwargs["messages"]
    assert sent[0]["content"] == system_content  # never truncated


@patch("kavi.llm.spark.OpenAI")
def test_generate_raises_spark_unavailable_on_connection_error(
    mock_openai_cls: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = ConnectionError("refused")
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkUnavailableError, match="unreachable"):
        generate([{"role": "user", "content": "test"}])


@patch("kavi.llm.spark.OpenAI")
def test_generate_raises_spark_error_on_empty_response(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    resp = MagicMock()
    resp.choices = []
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkError, match="empty response"):
        generate([{"role": "user", "content": "test"}])


@patch("kavi.llm.spark.OpenAI")
def test_generate_raises_spark_error_on_none_content(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    choice = MagicMock()
    choice.message.content = None
    resp = MagicMock()
    resp.choices = [choice]
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkError, match="empty response"):
        generate([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------------------
# _truncate_messages
# ---------------------------------------------------------------------------


class TestTruncateMessages:
    """Test message truncation logic."""

    def test_no_truncation_when_under_limit(self) -> None:
        msgs = [
            {"role": "system", "content": "hi"},
            {"role": "user", "content": "hello"},
        ]
        result = _truncate_messages(msgs, 100)
        assert result == msgs

    def test_truncates_last_user_message(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},  # 3
            {"role": "user", "content": "x" * 100},  # 100
        ]
        result = _truncate_messages(msgs, 50)
        assert result[0]["content"] == "sys"
        total = sum(len(m["content"]) for m in result)
        assert total <= 50

    def test_system_never_truncated(self) -> None:
        msgs = [
            {"role": "system", "content": "s" * 80},
            {"role": "user", "content": "u" * 80},
        ]
        result = _truncate_messages(msgs, 100)
        assert result[0]["content"] == "s" * 80

    def test_no_user_message_returns_unchanged(self) -> None:
        msgs = [{"role": "system", "content": "x" * 200}]
        result = _truncate_messages(msgs, 100)
        assert result == msgs


# ---------------------------------------------------------------------------
# generate_tool_call (D019)
# ---------------------------------------------------------------------------


def _mock_tool_call_response(name: str, arguments: dict) -> MagicMock:
    """Build a mock chat completion response with a tool call."""
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)

    choice = MagicMock()
    choice.message.tool_calls = [tc]
    choice.message.content = None

    resp = MagicMock()
    resp.choices = [choice]
    return resp


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_returns_result(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_tool_call_response(
        "talk", {"message": "hello"},
    )
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "talk", "parameters": {}}}]
    result = generate_tool_call(
        [{"role": "user", "content": "hi"}], tools,
    )
    assert isinstance(result, ToolCallResult)
    assert result.name == "talk"
    assert result.arguments == {"message": "hello"}


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_passes_tools(mock_openai_cls: MagicMock) -> None:
    """Tools and tool_choice are forwarded to the API."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_tool_call_response(
        "invoke_skill", {"skill_name": "search_notes", "input": {}},
    )
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "invoke_skill"}}]
    generate_tool_call(
        [{"role": "user", "content": "search"}], tools,
    )

    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs["tools"] == tools
    assert call_args.kwargs["tool_choice"] == "auto"


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_raises_on_no_tool_call(mock_openai_cls: MagicMock) -> None:
    """Raises SparkError when response has no tool calls."""
    mock_client = MagicMock()
    choice = MagicMock()
    choice.message.tool_calls = None
    choice.message.content = "I can't do that"
    resp = MagicMock()
    resp.choices = [choice]
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "talk"}}]
    with pytest.raises(SparkError, match="no tool call"):
        generate_tool_call(
            [{"role": "user", "content": "hi"}], tools,
        )


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_raises_on_bad_args(mock_openai_cls: MagicMock) -> None:
    """Raises SparkError when tool call arguments are not valid JSON."""
    mock_client = MagicMock()
    tc = MagicMock()
    tc.function.name = "talk"
    tc.function.arguments = "not json"
    choice = MagicMock()
    choice.message.tool_calls = [tc]
    resp = MagicMock()
    resp.choices = [choice]
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "talk"}}]
    with pytest.raises(SparkError, match="Invalid tool call"):
        generate_tool_call(
            [{"role": "user", "content": "hi"}], tools,
        )


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_raises_unavailable(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = ConnectionError("refused")
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "talk"}}]
    with pytest.raises(SparkUnavailableError, match="unreachable"):
        generate_tool_call(
            [{"role": "user", "content": "hi"}], tools,
        )


@patch("kavi.llm.spark.OpenAI")
def test_generate_tool_call_empty_response(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    resp = MagicMock()
    resp.choices = []
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    tools = [{"type": "function", "function": {"name": "talk"}}]
    with pytest.raises(SparkError, match="empty response"):
        generate_tool_call(
            [{"role": "user", "content": "hi"}], tools,
        )


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------


def _mock_embedding(index: int, vector: list[float]) -> MagicMock:
    """Build a mock embedding data item."""
    item = MagicMock()
    item.index = index
    item.embedding = vector
    return item


@patch("kavi.llm.spark.OpenAI")
def test_embed_returns_vectors(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    resp = MagicMock()
    resp.data = [
        _mock_embedding(0, [0.1, 0.2]),
        _mock_embedding(1, [0.3, 0.4]),
    ]
    mock_client.embeddings.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    result = embed(["hello", "world"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


@patch("kavi.llm.spark.OpenAI")
def test_embed_preserves_order(mock_openai_cls: MagicMock) -> None:
    """Ensure results are sorted by index even if API returns them out of order."""
    mock_client = MagicMock()
    resp = MagicMock()
    resp.data = [
        _mock_embedding(1, [0.3, 0.4]),
        _mock_embedding(0, [0.1, 0.2]),
    ]
    mock_client.embeddings.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    result = embed(["hello", "world"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_empty_list() -> None:
    result = embed([])
    assert result == []


@patch("kavi.llm.spark.OpenAI")
def test_embed_raises_unavailable_on_error(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.embeddings.create.side_effect = ConnectionError("refused")
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkUnavailableError, match="unreachable"):
        embed(["test"])


@patch("kavi.llm.spark.OpenAI")
def test_embed_raises_spark_error_on_empty_response(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    resp = MagicMock()
    resp.data = []
    mock_client.embeddings.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkError, match="empty embeddings"):
        embed(["test"])
