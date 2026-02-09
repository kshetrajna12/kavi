"""Convention-based path derivation for skills.

Given a proposal name (e.g. "write_note"), derive:
- Skill file:   src/kavi/skills/{name}.py
- Test file:    tests/test_skill_{name}.py
- Module path:  kavi.skills.{name}.{CamelCase}Skill
"""

from __future__ import annotations

from pathlib import Path


def _to_camel_case(name: str) -> str:
    """Convert snake_case name to CamelCase."""
    return "".join(part.capitalize() for part in name.split("_"))


def skill_file_path(name: str, project_root: Path) -> Path:
    """Return the conventional path for a skill implementation file."""
    return project_root / "src" / "kavi" / "skills" / f"{name}.py"


def skill_test_path(name: str, project_root: Path) -> Path:
    """Return the conventional path for a skill's test file."""
    return project_root / "tests" / f"test_skill_{name}.py"


def skill_module_path(name: str) -> str:
    """Return the dotted module path for a skill class.

    e.g. "write_note" → "kavi.skills.write_note.WriteNoteSkill"
    """
    class_name = f"{_to_camel_case(name)}Skill"
    return f"kavi.skills.{name}.{class_name}"
