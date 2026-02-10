"""Live Sparkstation tests — require running gateway.

Run with: uv run pytest -m spark
"""

from __future__ import annotations

import pytest

from kavi.llm.spark import generate, is_available

pytestmark = pytest.mark.spark


def test_spark_healthcheck() -> None:
    assert is_available() is True


def test_spark_generate_simple() -> None:
    result = generate("Say hello in one word.", temperature=0)
    assert isinstance(result, str)
    assert len(result) > 0
