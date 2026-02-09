"""Tests for skills loader."""

from pathlib import Path

import pytest

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput
from kavi.skills.loader import list_skills, load_registry, save_registry


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
