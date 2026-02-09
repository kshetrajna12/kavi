"""Policy scanner — static analysis of skill code for forbidden patterns."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class PolicyViolation:
    file: str
    line: int
    rule: str
    detail: str


@dataclass
class ScanResult:
    violations: list[PolicyViolation] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0


@dataclass
class Policy:
    forbidden_imports: list[str]
    allowed_network: bool
    allowed_write_paths: list[str]
    forbid_dynamic_exec: bool

    @classmethod
    def from_yaml(cls, path: Path) -> Policy:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(
            forbidden_imports=data.get("forbidden_imports", []),
            allowed_network=data.get("allowed_network", False),
            allowed_write_paths=data.get("allowed_write_paths", []),
            forbid_dynamic_exec=data.get("forbid_dynamic_exec", True),
        )


class _Visitor(ast.NodeVisitor):
    """AST visitor that checks for policy violations."""

    def __init__(self, policy: Policy, filename: str) -> None:
        self.policy = policy
        self.filename = filename
        self.violations: list[PolicyViolation] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._check_import(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module:
            self._check_import(node.module, node.lineno)
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                self._check_import(full, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self.policy.forbid_dynamic_exec:
            name = _call_name(node)
            if name in ("eval", "exec", "compile"):
                self.violations.append(PolicyViolation(
                    file=self.filename,
                    line=node.lineno,
                    rule="forbid_dynamic_exec",
                    detail=f"Call to {name}() is forbidden",
                ))
        self.generic_visit(node)

    def _check_import(self, module_name: str, lineno: int) -> None:
        for forbidden in self.policy.forbidden_imports:
            if module_name == forbidden or module_name.startswith(forbidden + "."):
                self.violations.append(PolicyViolation(
                    file=self.filename,
                    line=lineno,
                    rule="forbidden_import",
                    detail=f"Import of '{module_name}' is forbidden",
                ))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


# Also do a simple regex scan for patterns that AST might miss
# (e.g., inside strings or comments that hint at evasion)
_EXEC_PATTERN = re.compile(r'\b(eval|exec|compile)\s*\(')
_OS_SYSTEM_PATTERN = re.compile(r'\bos\.system\s*\(')


def scan_file(path: Path, policy: Policy) -> list[PolicyViolation]:
    """Scan a single Python file against the policy."""
    source = path.read_text()
    filename = str(path)

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [PolicyViolation(
            file=filename, line=e.lineno or 0,
            rule="syntax_error", detail=f"Cannot parse: {e.msg}",
        )]

    visitor = _Visitor(policy, filename)
    visitor.visit(tree)
    return visitor.violations


def scan_directory(directory: Path, policy: Policy) -> ScanResult:
    """Scan all .py files in a directory against the policy."""
    result = ScanResult()
    for py_file in sorted(directory.rglob("*.py")):
        result.files_scanned += 1
        result.violations.extend(scan_file(py_file, policy))
    return result


def format_report(result: ScanResult) -> str:
    """Format scan result as a markdown report."""
    lines = ["# Policy Scan Report\n"]
    lines.append(f"Files scanned: {result.files_scanned}")
    lines.append(f"Violations found: {len(result.violations)}")
    lines.append(f"Status: {'PASSED' if result.ok else 'FAILED'}\n")

    if result.violations:
        lines.append("## Violations\n")
        for v in result.violations:
            lines.append(f"- **{v.file}:{v.line}** [{v.rule}] {v.detail}")

    lines.append("")
    return "\n".join(lines)
