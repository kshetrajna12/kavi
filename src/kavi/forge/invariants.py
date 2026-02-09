"""Invariant checker — structural governance gate for skills.

Three sub-checks:
1. Structural conformance (AST): class extends BaseSkill with required attrs
2. Scope containment (git diff): only expected files modified
3. Extended safety (AST): no __import__(), no importlib.import_module()
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InvariantViolation:
    check: str
    message: str
    line: int | None = None


@dataclass
class InvariantResult:
    ok: bool
    structural_ok: bool
    scope_ok: bool
    safety_ok: bool
    violations: list[InvariantViolation] = field(default_factory=list)


# --- Check 1: Structural conformance ---

REQUIRED_ATTRS = {"name", "description", "input_model", "output_model", "side_effect_class"}


def _check_structural(skill_file: Path, expected_side_effect: str) -> list[InvariantViolation]:
    """Verify skill file has a class extending BaseSkill with required attrs."""
    violations: list[InvariantViolation] = []

    if not skill_file.exists():
        violations.append(InvariantViolation(
            check="structural", message=f"Skill file not found: {skill_file}",
        ))
        return violations

    source = skill_file.read_text()
    try:
        tree = ast.parse(source, filename=str(skill_file))
    except SyntaxError as e:
        violations.append(InvariantViolation(
            check="structural", message=f"Syntax error: {e}", line=e.lineno,
        ))
        return violations

    # Find classes that extend BaseSkill
    skill_classes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name == "BaseSkill":
                    skill_classes.append(node)

    if not skill_classes:
        violations.append(InvariantViolation(
            check="structural",
            message="No class extending BaseSkill found",
        ))
        return violations

    # Check required attrs on first BaseSkill subclass
    cls = skill_classes[0]
    assigned_attrs: set[str] = set()
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    assigned_attrs.add(target.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            assigned_attrs.add(item.target.id)

    missing = REQUIRED_ATTRS - assigned_attrs
    if missing:
        violations.append(InvariantViolation(
            check="structural",
            message=f"Missing required attrs: {', '.join(sorted(missing))}",
            line=cls.lineno,
        ))

    # Check side_effect_class value matches proposal
    if expected_side_effect:
        for item in cls.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == "side_effect_class":
                        if isinstance(item.value, ast.Constant) and isinstance(
                            item.value.value, str
                        ):
                            if item.value.value != expected_side_effect:
                                violations.append(InvariantViolation(
                                    check="structural",
                                    message=(
                                        f"side_effect_class is '{item.value.value}', "
                                        f"expected '{expected_side_effect}'"
                                    ),
                                    line=item.lineno,
                                ))

    return violations


# --- Check 2: Scope containment ---

PROTECTED_PATHS = frozenset({
    "src/kavi/forge/",
    "src/kavi/ledger/",
    "src/kavi/policies/",
    "src/kavi/cli.py",
    "src/kavi/config.py",
    "pyproject.toml",
})


def _check_scope(
    proposal_name: str,
    project_root: Path,
) -> list[InvariantViolation]:
    """Check that only expected files were modified (skill + test).

    Uses git diff --name-only against HEAD. Skipped if not a git repo
    or no prior commits exist.
    """
    violations: list[InvariantViolation] = []

    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=project_root, timeout=10,
        )
        if result.returncode != 0:
            return violations  # Not a git repo or no commits — skip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return violations  # git not available — skip

    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not changed:
        return violations

    expected_prefix = f"src/kavi/skills/{proposal_name}"
    test_prefix = f"tests/test_skill_{proposal_name}"

    for path in changed:
        if path.startswith(expected_prefix) or path.startswith(test_prefix):
            continue
        for protected in PROTECTED_PATHS:
            if path.startswith(protected):
                violations.append(InvariantViolation(
                    check="scope",
                    message=f"Protected path modified: {path}",
                ))
                break

    return violations


# --- Check 3: Extended safety ---


def _check_extended_safety(skill_file: Path) -> list[InvariantViolation]:
    """Check for __import__() and importlib.import_module() calls."""
    violations: list[InvariantViolation] = []

    if not skill_file.exists():
        return violations

    source = skill_file.read_text()
    try:
        tree = ast.parse(source, filename=str(skill_file))
    except SyntaxError:
        return violations  # Already caught by structural check

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # __import__()
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                violations.append(InvariantViolation(
                    check="safety",
                    message="__import__() call detected",
                    line=node.lineno,
                ))
            # importlib.import_module()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
            ):
                violations.append(InvariantViolation(
                    check="safety",
                    message="importlib.import_module() call detected",
                    line=node.lineno,
                ))

    return violations


# --- Top-level orchestrator ---


def check_invariants(
    skill_file: Path,
    *,
    expected_side_effect: str,
    proposal_name: str,
    project_root: Path,
) -> InvariantResult:
    """Run all invariant checks and return combined result."""
    structural_violations = _check_structural(skill_file, expected_side_effect)
    scope_violations = _check_scope(proposal_name, project_root)
    safety_violations = _check_extended_safety(skill_file)

    structural_ok = len(structural_violations) == 0
    scope_ok = len(scope_violations) == 0
    safety_ok = len(safety_violations) == 0

    return InvariantResult(
        ok=structural_ok and scope_ok and safety_ok,
        structural_ok=structural_ok,
        scope_ok=scope_ok,
        safety_ok=safety_ok,
        violations=structural_violations + scope_violations + safety_violations,
    )
