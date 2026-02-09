"""Tests for convention-based skill path derivation."""

from pathlib import Path

from kavi.forge.paths import skill_file_path, skill_module_path, skill_test_path


class TestSkillFilePath:
    def test_simple_name(self, tmp_path: Path) -> None:
        assert skill_file_path("write_note", tmp_path) == (
            tmp_path / "src" / "kavi" / "skills" / "write_note.py"
        )

    def test_single_word(self, tmp_path: Path) -> None:
        assert skill_file_path("summarize", tmp_path) == (
            tmp_path / "src" / "kavi" / "skills" / "summarize.py"
        )


class TestSkillTestPath:
    def test_simple_name(self, tmp_path: Path) -> None:
        assert skill_test_path("write_note", tmp_path) == (
            tmp_path / "tests" / "test_skill_write_note.py"
        )

    def test_single_word(self, tmp_path: Path) -> None:
        assert skill_test_path("summarize", tmp_path) == (
            tmp_path / "tests" / "test_skill_summarize.py"
        )


class TestSkillModulePath:
    def test_snake_case(self) -> None:
        assert skill_module_path("write_note") == (
            "kavi.skills.write_note.WriteNoteSkill"
        )

    def test_single_word(self) -> None:
        assert skill_module_path("summarize") == (
            "kavi.skills.summarize.SummarizeSkill"
        )

    def test_triple_word(self) -> None:
        assert skill_module_path("read_notes_by_tag") == (
            "kavi.skills.read_notes_by_tag.ReadNotesByTagSkill"
        )
