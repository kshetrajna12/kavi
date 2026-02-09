"""Tests for write_note skill."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kavi.skills import write_note
from kavi.skills.write_note import WriteNoteInput, WriteNoteOutput, WriteNoteSkill


@pytest.fixture(autouse=True)
def _isolate_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect VAULT_OUT to tmp_path for every test."""
    monkeypatch.setattr(write_note, "VAULT_OUT", tmp_path / "vault_out")


class TestWriteNoteModels:
    """Pydantic model validation tests."""

    def test_valid_input(self):
        inp = WriteNoteInput(path="notes/hello.md", title="Hello", body="World")
        assert inp.path == "notes/hello.md"
        assert inp.title == "Hello"
        assert inp.body == "World"

    def test_missing_field_raises(self):
        with pytest.raises(Exception):
            WriteNoteInput(path="a.md", title="T")  # type: ignore[call-arg]

    def test_output_model(self):
        out = WriteNoteOutput(written_path="vault_out/a.md", sha256="abc123")
        assert out.written_path == "vault_out/a.md"
        assert out.sha256 == "abc123"


class TestWriteNoteSkill:
    """Skill execution tests."""

    def test_attributes(self):
        skill = WriteNoteSkill()
        assert skill.name == "write_note"
        assert skill.description == "Write a markdown note to the vault"
        assert skill.input_model is WriteNoteInput
        assert skill.output_model is WriteNoteOutput
        assert skill.side_effect_class == "FILE_WRITE"

    def test_execute_writes_file(self, tmp_path: Path):
        skill = WriteNoteSkill()
        vault = tmp_path / "vault_out"

        result = skill.execute(
            WriteNoteInput(path="daily/2025-01-01.md", title="New Year", body="Happy new year!")
        )

        expected_content = "# New Year\n\nHappy new year!\n"
        expected_hash = hashlib.sha256(expected_content.encode()).hexdigest()

        written = vault / "daily" / "2025-01-01.md"
        assert written.exists()
        assert written.read_text() == expected_content
        assert result.sha256 == expected_hash
        assert result.written_path == str(vault / "daily" / "2025-01-01.md")

    def test_execute_flat_path(self, tmp_path: Path):
        skill = WriteNoteSkill()
        result = skill.execute(
            WriteNoteInput(path="readme.md", title="Readme", body="Some content.")
        )
        written = tmp_path / "vault_out" / "readme.md"
        assert written.exists()
        assert "# Readme" in written.read_text()
        assert result.sha256 == hashlib.sha256(written.read_bytes()).hexdigest()

    def test_rejects_absolute_path(self):
        skill = WriteNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(WriteNoteInput(path="/etc/passwd", title="T", body="B"))

    def test_rejects_traversal(self):
        skill = WriteNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(WriteNoteInput(path="../escape.md", title="T", body="B"))

    def test_rejects_nested_traversal(self):
        skill = WriteNoteSkill()
        with pytest.raises(ValueError, match="Invalid path"):
            skill.execute(WriteNoteInput(path="a/../../escape.md", title="T", body="B"))

    def test_validate_and_run(self, tmp_path: Path):
        skill = WriteNoteSkill()
        result = skill.validate_and_run({
            "path": "test.md",
            "title": "Test",
            "body": "Body text",
        })
        assert "written_path" in result
        assert "sha256" in result
        assert (tmp_path / "vault_out" / "test.md").exists()

    def test_validate_and_run_invalid_input(self):
        skill = WriteNoteSkill()
        with pytest.raises(Exception):
            skill.validate_and_run({"path": "x.md"})  # missing title and body
