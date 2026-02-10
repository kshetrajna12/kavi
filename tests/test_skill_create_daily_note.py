"""Tests for create_daily_note skill."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kavi.skills import create_daily_note
from kavi.skills.create_daily_note import (
    CreateDailyNoteInput,
    CreateDailyNoteOutput,
    CreateDailyNoteSkill,
)

FIXED_DT = datetime(2025, 6, 15, 14, 30, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect VAULT_OUT to tmp_path for every test."""
    monkeypatch.setattr(create_daily_note, "VAULT_OUT", tmp_path / "vault_out")


class TestCreateDailyNoteModels:
    """Pydantic model validation tests."""

    def test_valid_input(self):
        inp = CreateDailyNoteInput(content="Finished the report")
        assert inp.content == "Finished the report"

    def test_missing_field_raises(self):
        with pytest.raises(Exception):
            CreateDailyNoteInput()  # type: ignore[call-arg]

    def test_output_model(self):
        out = CreateDailyNoteOutput(
            path="vault_out/daily/2025-06-15.md",
            date="2025-06-15",
            sha256="abc123",
        )
        assert out.path == "vault_out/daily/2025-06-15.md"
        assert out.date == "2025-06-15"
        assert out.sha256 == "abc123"


class TestCreateDailyNoteSkill:
    """Skill execution tests."""

    def test_attributes(self):
        skill = CreateDailyNoteSkill()
        assert skill.name == "create_daily_note"
        expected = "Create or append a timestamped entry to today's daily note in the vault"
        assert skill.description == expected
        assert skill.input_model is CreateDailyNoteInput
        assert skill.output_model is CreateDailyNoteOutput
        assert skill.side_effect_class == "FILE_WRITE"

    @patch("kavi.skills.create_daily_note.datetime")
    def test_execute_creates_new_file(self, mock_dt, tmp_path: Path):
        mock_dt.now.return_value = FIXED_DT
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        skill = CreateDailyNoteSkill()
        result = skill.execute(CreateDailyNoteInput(content="Started the project"))

        daily_file = tmp_path / "vault_out" / "daily" / "2025-06-15.md"
        assert daily_file.exists()

        text = daily_file.read_text()
        assert text.startswith("# 2025-06-15\n\n")
        assert "- 14:30 — Started the project\n" in text

        assert result.date == "2025-06-15"
        assert result.path == str(daily_file)
        assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()

    @patch("kavi.skills.create_daily_note.datetime")
    def test_execute_appends_to_existing(self, mock_dt, tmp_path: Path):
        mock_dt.now.return_value = FIXED_DT
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        daily_dir = tmp_path / "vault_out" / "daily"
        daily_dir.mkdir(parents=True)
        daily_file = daily_dir / "2025-06-15.md"
        daily_file.write_text("# 2025-06-15\n\n- 09:00 — Morning standup\n")

        skill = CreateDailyNoteSkill()
        result = skill.execute(CreateDailyNoteInput(content="Afternoon review"))

        text = daily_file.read_text()
        assert "- 09:00 — Morning standup\n" in text
        assert "- 14:30 — Afternoon review\n" in text
        assert text.startswith("# 2025-06-15\n\n")

        assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()

    @patch("kavi.skills.create_daily_note.datetime")
    def test_execute_multiple_appends(self, mock_dt, tmp_path: Path):
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        skill = CreateDailyNoteSkill()

        mock_dt.now.return_value = datetime(2025, 6, 15, 8, 0, 0, tzinfo=UTC)
        skill.execute(CreateDailyNoteInput(content="First entry"))

        mock_dt.now.return_value = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        skill.execute(CreateDailyNoteInput(content="Second entry"))

        mock_dt.now.return_value = datetime(2025, 6, 15, 18, 0, 0, tzinfo=UTC)
        result = skill.execute(CreateDailyNoteInput(content="Third entry"))

        daily_file = tmp_path / "vault_out" / "daily" / "2025-06-15.md"
        text = daily_file.read_text()
        assert "- 08:00 — First entry\n" in text
        assert "- 12:00 — Second entry\n" in text
        assert "- 18:00 — Third entry\n" in text
        assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()

    @patch("kavi.skills.create_daily_note.datetime")
    def test_output_date_matches_filename(self, mock_dt, tmp_path: Path):
        mock_dt.now.return_value = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        skill = CreateDailyNoteSkill()
        result = skill.execute(CreateDailyNoteInput(content="New year"))

        assert result.date == "2025-01-01"
        assert "2025-01-01.md" in result.path

    @patch("kavi.skills.create_daily_note.datetime")
    def test_validate_and_run(self, mock_dt, tmp_path: Path):
        mock_dt.now.return_value = FIXED_DT
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        skill = CreateDailyNoteSkill()
        result = skill.validate_and_run({"content": "Via validate_and_run"})

        assert "path" in result
        assert "date" in result
        assert "sha256" in result
        assert (tmp_path / "vault_out" / "daily" / "2025-06-15.md").exists()

    def test_validate_and_run_invalid_input(self):
        skill = CreateDailyNoteSkill()
        with pytest.raises(Exception):
            skill.validate_and_run({})  # missing content

    @patch("kavi.skills.create_daily_note.datetime")
    def test_creates_directory_structure(self, mock_dt, tmp_path: Path):
        mock_dt.now.return_value = FIXED_DT
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        skill = CreateDailyNoteSkill()
        skill.execute(CreateDailyNoteInput(content="Test"))

        assert (tmp_path / "vault_out" / "daily").is_dir()
