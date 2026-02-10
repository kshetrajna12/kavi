"""Tests for the summarize_note skill."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kavi.skills import summarize_note
from kavi.skills.summarize_note import (
    SummarizeNoteInput,
    SummarizeNoteOutput,
    SummarizeNoteSkill,
)


@pytest.fixture(autouse=True)
def _isolate_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarize_note, "VAULT_OUT", tmp_path / "vault_out")


def _vault(tmp_path: Path) -> Path:
    return tmp_path / "vault_out"


def _write_note(tmp_path: Path, rel: str, content: str) -> Path:
    vault = _vault(tmp_path)
    dest = vault / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


def _llm_json(summary: str, key_points: list[str]) -> str:
    return json.dumps({"summary": summary, "key_points": key_points})


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestSummarizeNoteModels:
    def test_input_defaults(self) -> None:
        inp = SummarizeNoteInput(path="note.md")
        assert inp.style == "bullet"
        assert inp.max_chars == 12000
        assert inp.timeout_s == 12.0

    def test_input_custom(self) -> None:
        inp = SummarizeNoteInput(
            path="a.md", style="paragraph", max_chars=5000, timeout_s=5.0
        )
        assert inp.style == "paragraph"
        assert inp.max_chars == 5000
        assert inp.timeout_s == 5.0

    def test_input_invalid_style(self) -> None:
        with pytest.raises(Exception):
            SummarizeNoteInput(path="a.md", style="invalid")  # type: ignore[arg-type]

    def test_output_model(self) -> None:
        out = SummarizeNoteOutput(
            path="n.md",
            summary="hello",
            key_points=["a"],
            truncated=False,
            used_model="gpt-oss-20b",
        )
        assert out.error is None

    def test_output_with_error(self) -> None:
        out = SummarizeNoteOutput(
            path="n.md",
            summary="x",
            key_points=[],
            truncated=False,
            used_model="fallback",
            error="something broke",
        )
        assert out.error == "something broke"


# ---------------------------------------------------------------------------
# Skill attribute tests
# ---------------------------------------------------------------------------


class TestSummarizeNoteAttributes:
    def test_attributes(self) -> None:
        skill = SummarizeNoteSkill()
        assert skill.name == "summarize_note"
        assert skill.side_effect_class == "READ_ONLY"
        assert skill.input_model is SummarizeNoteInput
        assert skill.output_model is SummarizeNoteOutput


# ---------------------------------------------------------------------------
# Path validation tests
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        skill = SummarizeNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(SummarizeNoteInput(path="/etc/passwd"))

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        skill = SummarizeNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(SummarizeNoteInput(path="../secrets.md"))

    def test_nested_traversal_rejected(self, tmp_path: Path) -> None:
        skill = SummarizeNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(SummarizeNoteInput(path="a/../../etc/passwd"))

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        real = tmp_path / "secret.md"
        real.write_text("secret")
        link = vault / "link.md"
        link.symlink_to(real)

        skill = SummarizeNoteSkill()
        with pytest.raises(ValueError, match="Symlinks not allowed"):
            skill.execute(SummarizeNoteInput(path="link.md"))

    def test_nonexistent_file_rejected(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)

        skill = SummarizeNoteSkill()
        with pytest.raises(ValueError, match="File not found"):
            skill.execute(SummarizeNoteInput(path="missing.md"))


# ---------------------------------------------------------------------------
# LLM success tests
# ---------------------------------------------------------------------------


class TestLLMSuccess:
    @patch("kavi.skills.summarize_note.generate")
    def test_successful_summary(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "# Title\n\nSome content here.")
        mock_gen.return_value = _llm_json("A short summary", ["point 1", "point 2"])

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        assert result.summary == "A short summary"
        assert result.key_points == ["point 1", "point 2"]
        assert result.truncated is False
        assert result.used_model != "fallback"
        assert result.error is None
        assert result.path == "note.md"

    @patch("kavi.skills.summarize_note.generate")
    def test_passes_model_and_timeout(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "content")
        mock_gen.return_value = _llm_json("s", [])

        skill = SummarizeNoteSkill()
        skill.execute(SummarizeNoteInput(path="note.md", timeout_s=7.5))

        _, kwargs = mock_gen.call_args
        assert kwargs["timeout"] == 7.5

    @patch("kavi.skills.summarize_note.generate")
    def test_style_in_prompt(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "content")
        mock_gen.return_value = _llm_json("s", [])

        skill = SummarizeNoteSkill()
        skill.execute(SummarizeNoteInput(path="note.md", style="paragraph"))

        prompt_arg = mock_gen.call_args[0][0]
        assert "paragraph" in prompt_arg

    @patch("kavi.skills.summarize_note.generate")
    def test_bullet_style_in_prompt(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "content")
        mock_gen.return_value = _llm_json("s", [])

        skill = SummarizeNoteSkill()
        skill.execute(SummarizeNoteInput(path="note.md", style="bullet"))

        prompt_arg = mock_gen.call_args[0][0]
        assert "bullet" in prompt_arg


# ---------------------------------------------------------------------------
# Truncation tests
# ---------------------------------------------------------------------------


class TestTruncation:
    @patch("kavi.skills.summarize_note.generate")
    def test_long_content_truncated(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        long_content = "x" * 15000
        _write_note(tmp_path, "big.md", long_content)
        mock_gen.return_value = _llm_json("truncated summary", [])

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="big.md", max_chars=12000))

        assert result.truncated is True
        # Verify the prompt only got max_chars worth of content
        prompt_arg = mock_gen.call_args[0][0]
        # The prompt includes preamble + content, but content portion is capped
        assert len(prompt_arg) < 15000

    @patch("kavi.skills.summarize_note.generate")
    def test_short_content_not_truncated(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "small.md", "short note")
        mock_gen.return_value = _llm_json("summary", [])

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="small.md"))

        assert result.truncated is False


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------


class TestFallback:
    @patch("kavi.skills.summarize_note.generate")
    def test_spark_unavailable_fallback(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        from kavi.llm.spark import SparkUnavailableError

        _write_note(tmp_path, "note.md", "Some note content for fallback.")
        mock_gen.side_effect = SparkUnavailableError("gateway down")

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        assert result.used_model == "fallback"
        assert result.summary.startswith("[Fallback summary] ")
        assert "Some note content for fallback." in result.summary
        assert result.key_points == []
        assert result.error is not None
        assert "SparkUnavailableError" in result.error

    @patch("kavi.skills.summarize_note.generate")
    def test_spark_error_fallback(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        from kavi.llm.spark import SparkError

        _write_note(tmp_path, "note.md", "content")
        mock_gen.side_effect = SparkError("bad response")

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        assert result.used_model == "fallback"
        assert result.error is not None
        assert "SparkError" in result.error

    @patch("kavi.skills.summarize_note.generate")
    def test_json_parse_error_fallback(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "content")
        mock_gen.return_value = "not valid json at all"

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        assert result.used_model == "fallback"
        assert result.error is not None
        assert "JSONDecodeError" in result.error

    @patch("kavi.skills.summarize_note.generate")
    def test_missing_key_fallback(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "note.md", "content")
        mock_gen.return_value = json.dumps({"wrong_key": "value"})

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        assert result.used_model == "fallback"
        assert "KeyError" in result.error  # type: ignore[operator]

    @patch("kavi.skills.summarize_note.generate")
    def test_fallback_truncates_long_content(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        from kavi.llm.spark import SparkError

        long_content = "A" * 1000
        _write_note(tmp_path, "note.md", long_content)
        mock_gen.side_effect = SparkError("fail")

        skill = SummarizeNoteSkill()
        result = skill.execute(SummarizeNoteInput(path="note.md"))

        # Fallback summary should be prefix + first 500 chars
        expected = "[Fallback summary] " + "A" * 500
        assert result.summary == expected


# ---------------------------------------------------------------------------
# validate_and_run integration
# ---------------------------------------------------------------------------


class TestValidateAndRun:
    @patch("kavi.skills.summarize_note.generate")
    def test_validate_and_run(self, mock_gen: MagicMock, tmp_path: Path) -> None:
        _write_note(tmp_path, "test.md", "# Hello\n\nWorld")
        mock_gen.return_value = _llm_json("hello world", ["greeting"])

        skill = SummarizeNoteSkill()
        result = skill.validate_and_run({"path": "test.md"})

        assert result["summary"] == "hello world"
        assert result["key_points"] == ["greeting"]
        assert result["truncated"] is False
        assert result["error"] is None
