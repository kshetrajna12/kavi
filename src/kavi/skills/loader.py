"""Skill loader — import and instantiate skills from registry."""

from __future__ import annotations

import hashlib
import importlib
import warnings
from pathlib import Path
from typing import Any

import yaml

from kavi.skills.base import BaseSkill


class TrustError(Exception):
    """Raised when a skill file hash does not match the registry."""


def load_registry(registry_path: Path) -> list[dict[str, Any]]:
    """Load the skill registry YAML file."""
    with open(registry_path) as f:
        data = yaml.safe_load(f)
    return data.get("skills", []) if data else []


def save_registry(registry_path: Path, skills: list[dict[str, Any]]) -> None:
    """Write the skill registry YAML file."""
    with open(registry_path, "w") as f:
        yaml.dump({"skills": skills}, f, default_flow_style=False, sort_keys=False)


def _import_skill(module_path: str) -> type[BaseSkill]:
    """Import a skill class from a dotted module path (e.g. 'module.ClassName').

    Private — only called by load_skill after trust verification.
    """
    parts = module_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid module path: {module_path}. Expected 'module.ClassName'.")
    module_name, class_name = parts
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not isinstance(cls, type) or not issubclass(cls, BaseSkill):
        raise TypeError(f"{module_path} is not a BaseSkill subclass")
    return cls


def _verify_trust(module_path: str, expected_hash: str) -> None:
    """Re-hash the skill source file and compare against the registry hash.

    Raises TrustError if the hash does not match or the source file
    cannot be located.
    """
    parts = module_path.rsplit(".", 1)
    module_name = parts[0]
    mod = importlib.import_module(module_name)
    source_file = getattr(mod, "__file__", None)
    if source_file is None:
        raise TrustError(
            f"Cannot locate source file for module '{module_name}'"
        )
    actual_hash = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise TrustError(
            f"Skill '{module_path}' failed trust check: "
            f"expected hash {expected_hash[:12]}…, "
            f"got {actual_hash[:12]}…"
        )


def load_skill(registry_path: Path, skill_name: str) -> BaseSkill:
    """Load and instantiate a skill by name from the registry.

    Verifies the skill file hash against the registry before execution.
    Raises TrustError if the hash does not match.
    """
    entries = load_registry(registry_path)
    for entry in entries:
        if entry["name"] == skill_name:
            expected_hash = entry.get("hash")
            if expected_hash:
                _verify_trust(entry["module_path"], expected_hash)
            else:
                warnings.warn(
                    f"Skill '{skill_name}' has no hash in registry — "
                    "trust check skipped (re-promote to fix)",
                    UserWarning,
                    stacklevel=2,
                )
            cls = _import_skill(entry["module_path"])
            return cls()
    raise KeyError(f"Skill '{skill_name}' not found in registry")


def list_skills(registry_path: Path) -> list[dict[str, Any]]:
    """List all registered skills."""
    return load_registry(registry_path)
