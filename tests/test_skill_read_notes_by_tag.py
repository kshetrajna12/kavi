"""Tests for the read_notes_by_tag skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from kavi.skills.read_notes_by_tag import (
    ReadNotesByTagInput,
    ReadNotesByTagOutput,
    ReadNotesByTagSkill,
)


@pytest.fixture(autouse=True)
def _isolate_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kavi.skills.read_notes_by_tag.VAULT_OUT", tmp_path / "vault_out"
    )


def _vault(tmp_path: Path) -> Path:
    return tmp_path / "vault_out"


class TestReadNotesByTagModels:
    def test_input_model(self) -> None:
        inp = ReadNotesByTagInput(tag="project")
        assert inp.tag == "project"

    def test_output_model(self) -> None:
        out = ReadNotesByTagOutput(notes=[], count=0)
        assert out.notes == []
        assert out.count == 0


class TestReadNotesByTagSkill:
    def test_attributes(self) -> None:
        skill = ReadNotesByTagSkill()
        assert skill.name == "read_notes_by_tag"
        assert skill.description == "Read all notes matching a tag from the vault"
        assert skill.side_effect_class == "READ_ONLY"
        assert skill.input_model is ReadNotesByTagInput
        assert skill.output_model is ReadNotesByTagOutput

    def test_empty_vault(self, tmp_path: Path) -> None:
        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="anything"))
        assert result.notes == []
        assert result.count == 0

    def test_vault_missing(self, tmp_path: Path) -> None:
        """Vault directory doesn't exist at all."""
        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="test"))
        assert result.count == 0

    def test_finds_tagged_note(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# My Note\n\nSome text #project\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="project"))
        assert result.count == 1
        assert result.notes[0].path == "note.md"
        assert result.notes[0].title == "My Note"

    def test_tag_with_hash_prefix(self, tmp_path: Path) -> None:
        """User passes '#project' — leading # should be stripped."""
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# Title\n\n#project\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="#project"))
        assert result.count == 1

    def test_no_match(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# Title\n\nNo tags here.\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="missing"))
        assert result.count == 0
        assert result.notes == []

    def test_multiple_matches(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "a.md").write_text("# Alpha\n\n#work\n")
        (vault / "b.md").write_text("# Beta\n\nTagged #work here\n")
        (vault / "c.md").write_text("# Gamma\n\nNo tag\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="work"))
        assert result.count == 2
        paths = {n.path for n in result.notes}
        assert paths == {"a.md", "b.md"}

    def test_nested_directory(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        sub = vault / "daily"
        sub.mkdir(parents=True)
        (sub / "2025-01-01.md").write_text("# New Year\n\n#journal\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="journal"))
        assert result.count == 1
        assert result.notes[0].path == "daily/2025-01-01.md"

    def test_tag_not_substring(self, tmp_path: Path) -> None:
        """#project should not match #projects."""
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# Title\n\n#projects\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="project"))
        assert result.count == 0

    def test_heading_not_treated_as_tag(self, tmp_path: Path) -> None:
        """Markdown headings (# Heading) should not be matched as tags."""
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# project\n\nBody text.\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="project"))
        assert result.count == 0

    def test_fallback_title_from_filename(self, tmp_path: Path) -> None:
        """When no H1 heading exists, use the file stem as title."""
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "ideas.md").write_text("Just some text #brainstorm\n")

        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="brainstorm"))
        assert result.count == 1
        assert result.notes[0].title == "ideas"

    def test_empty_tag(self, tmp_path: Path) -> None:
        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag=""))
        assert result.count == 0

    def test_whitespace_tag(self, tmp_path: Path) -> None:
        skill = ReadNotesByTagSkill()
        result = skill.execute(ReadNotesByTagInput(tag="   "))
        assert result.count == 0

    def test_validate_and_run(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path)
        vault.mkdir(parents=True)
        (vault / "test.md").write_text("# Test\n\n#todo\n")

        skill = ReadNotesByTagSkill()
        result = skill.validate_and_run({"tag": "todo"})
        assert result["count"] == 1
        assert result["notes"][0]["path"] == "test.md"
        assert result["notes"][0]["title"] == "Test"
