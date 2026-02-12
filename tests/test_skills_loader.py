"""Tests for skills loader."""

import hashlib
from pathlib import Path

import pytest

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput
from kavi.skills.loader import (
    TrustError,
    list_skills,
    load_registry,
    load_skill,
    save_registry,
)


@pytest.fixture()
def empty_registry(tmp_path: Path):
    reg = tmp_path / "registry.yaml"
    reg.write_text("skills: []\n")
    return reg


@pytest.fixture()
def populated_registry(tmp_path: Path):
    reg = tmp_path / "registry.yaml"
    save_registry(reg, [
        {
            "name": "test_skill",
            "module_path": "tests.test_skills_loader.MockSkill",
            "description": "A test skill",
            "side_effect_class": "READ_ONLY",
            "required_secrets": [],
            "version": "1.0.0",
            "hash": "abc123",
        },
    ])
    return reg


# --- Mock skill for testing ---

class MockInput(SkillInput):
    value: str


class MockOutput(SkillOutput):
    result: str


class MockSkill(BaseSkill):
    name = "test_skill"
    description = "A test skill"
    input_model = MockInput
    output_model = MockOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: MockInput) -> MockOutput:  # type: ignore[override]
        return MockOutput(result=f"processed: {input_data.value}")


class TestRegistry:
    def test_load_empty(self, empty_registry):
        skills = load_registry(empty_registry)
        assert skills == []

    def test_save_and_load(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        entries = [{"name": "foo", "module_path": "bar.Baz"}]
        save_registry(reg, entries)
        loaded = load_registry(reg)
        assert len(loaded) == 1
        assert loaded[0]["name"] == "foo"

    def test_list_skills(self, populated_registry):
        skills = list_skills(populated_registry)
        assert len(skills) == 1
        assert skills[0]["name"] == "test_skill"


class TestBaseSkill:
    def test_validate_and_run(self):
        skill = MockSkill()
        result = skill.validate_and_run({"value": "hello"})
        assert result == {"result": "processed: hello"}

    def test_invalid_input_raises(self):
        skill = MockSkill()
        with pytest.raises(Exception):  # Pydantic validation error
            skill.validate_and_run({"wrong_field": "x"})

    def test_skill_attributes(self):
        skill = MockSkill()
        assert skill.name == "test_skill"
        assert skill.side_effect_class == "READ_ONLY"


class TestTrustEnforcement:
    """Tests for runtime trust verification (D010)."""

    def _this_file_hash(self) -> str:
        """Compute the hash of this test file (where MockSkill lives)."""
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def test_load_skill_with_valid_hash(self, tmp_path: Path) -> None:
        """Skill loads when file hash matches registry."""
        reg = tmp_path / "registry.yaml"
        save_registry(reg, [{
            "name": "test_skill",
            "module_path": "tests.test_skills_loader.MockSkill",
            "description": "A test skill",
            "side_effect_class": "READ_ONLY",
            "required_secrets": [],
            "version": "1.0.0",
            "hash": self._this_file_hash(),
        }])
        skill = load_skill(reg, "test_skill")
        assert skill.name == "test_skill"

    def test_load_skill_rejects_tampered_hash(self, tmp_path: Path) -> None:
        """Skill refuses to load when file hash doesn't match registry."""
        reg = tmp_path / "registry.yaml"
        save_registry(reg, [{
            "name": "test_skill",
            "module_path": "tests.test_skills_loader.MockSkill",
            "description": "A test skill",
            "side_effect_class": "READ_ONLY",
            "required_secrets": [],
            "version": "1.0.0",
            "hash": "deadbeef" * 8,  # wrong hash
        }])
        with pytest.raises(TrustError, match="failed trust check"):
            load_skill(reg, "test_skill")

    def test_load_skill_rejects_missing_hash(self, tmp_path: Path) -> None:
        """Skill refuses to load when registry has no hash."""
        reg = tmp_path / "registry.yaml"
        save_registry(reg, [{
            "name": "test_skill",
            "module_path": "tests.test_skills_loader.MockSkill",
            "description": "A test skill",
            "side_effect_class": "READ_ONLY",
            "required_secrets": [],
            "version": "1.0.0",
        }])
        with pytest.raises(TrustError, match="no hash"):
            load_skill(reg, "test_skill")

    def test_load_skill_rejects_empty_hash(self, tmp_path: Path) -> None:
        """Skill refuses to load when registry hash is empty string."""
        reg = tmp_path / "registry.yaml"
        save_registry(reg, [{
            "name": "test_skill",
            "module_path": "tests.test_skills_loader.MockSkill",
            "description": "A test skill",
            "side_effect_class": "READ_ONLY",
            "required_secrets": [],
            "version": "1.0.0",
            "hash": "",
        }])
        with pytest.raises(TrustError, match="no hash"):
            load_skill(reg, "test_skill")

    def test_load_skill_not_found(self, tmp_path: Path) -> None:
        """KeyError when skill name not in registry."""
        reg = tmp_path / "registry.yaml"
        save_registry(reg, [])
        with pytest.raises(KeyError, match="not_real"):
            load_skill(reg, "not_real")
