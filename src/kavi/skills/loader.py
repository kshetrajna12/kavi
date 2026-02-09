"""Skill loader — import and instantiate skills from registry."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from kavi.skills.base import BaseSkill


def load_registry(registry_path: Path) -> list[dict[str, Any]]:
    """Load the skill registry YAML file."""
    with open(registry_path) as f:
        data = yaml.safe_load(f)
    return data.get("skills", []) if data else []


def save_registry(registry_path: Path, skills: list[dict[str, Any]]) -> None:
    """Write the skill registry YAML file."""
    with open(registry_path, "w") as f:
        yaml.dump({"skills": skills}, f, default_flow_style=False, sort_keys=False)


def import_skill(module_path: str) -> type[BaseSkill]:
    """Import a skill class from a dotted module path (e.g. 'module.ClassName')."""
    parts = module_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid module path: {module_path}. Expected 'module.ClassName'.")
    module_name, class_name = parts
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not isinstance(cls, type) or not issubclass(cls, BaseSkill):
        raise TypeError(f"{module_path} is not a BaseSkill subclass")
    return cls


def load_skill(registry_path: Path, skill_name: str) -> BaseSkill:
    """Load and instantiate a skill by name from the registry."""
    entries = load_registry(registry_path)
    for entry in entries:
        if entry["name"] == skill_name:
            cls = import_skill(entry["module_path"])
            return cls()
    raise KeyError(f"Skill '{skill_name}' not found in registry")


def list_skills(registry_path: Path) -> list[dict[str, Any]]:
    """List all registered skills."""
    return load_registry(registry_path)
