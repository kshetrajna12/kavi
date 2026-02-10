"""Unit tests for kavi.llm.spark — fully mocked, no network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kavi.llm.spark import SparkError, SparkUnavailableError, generate, is_available

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
# generate
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

    result = generate("Say hello")
    assert result == "hello world"


@patch("kavi.llm.spark.OpenAI")
def test_generate_truncates_long_prompt(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_response("ok")
    mock_openai_cls.return_value = mock_client

    long_prompt = "x" * 10000
    generate(long_prompt, max_prompt_chars=100)

    call_args = mock_client.chat.completions.create.call_args
    sent_prompt = call_args.kwargs["messages"][0]["content"]
    assert len(sent_prompt) == 100


@patch("kavi.llm.spark.OpenAI")
def test_generate_raises_spark_unavailable_on_connection_error(
    mock_openai_cls: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = ConnectionError("refused")
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkUnavailableError, match="unreachable"):
        generate("test prompt")


@patch("kavi.llm.spark.OpenAI")
def test_generate_raises_spark_error_on_empty_response(mock_openai_cls: MagicMock) -> None:
    mock_client = MagicMock()
    resp = MagicMock()
    resp.choices = []
    mock_client.chat.completions.create.return_value = resp
    mock_openai_cls.return_value = mock_client

    with pytest.raises(SparkError, match="empty response"):
        generate("test prompt")


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
        generate("test prompt")
