"""Kavi doctor — fast, actionable healthcheck for the local environment.

Pure functions returning CheckResult models. Never raises; errors are
captured in check results. Does not import forge code or mutate anything.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI


@dataclass
class CheckResult:
    """Result of a single healthcheck."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    message: str
    remediation: str | None = None


@dataclass
class DoctorReport:
    """Aggregate report from all healthchecks."""

    checks: list[CheckResult] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )

    @property
    def overall_status(self) -> str:
        statuses = {c.status for c in self.checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "remediation": c.remediation,
                }
                for c in self.checks
            ],
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Check 1: Config + paths
# ---------------------------------------------------------------------------

def check_vault_path(vault_out: Path) -> CheckResult:
    """Verify vault output directory exists and is a directory."""
    if vault_out.exists() and vault_out.is_dir():
        return CheckResult("vault_path", "ok", f"Vault exists: {vault_out}")
    return CheckResult(
        "vault_path", "fail",
        f"Vault directory missing: {vault_out}",
        f"mkdir -p {vault_out}",
    )


def check_registry_path(registry_path: Path) -> CheckResult:
    """Verify registry YAML exists and is readable."""
    if registry_path.exists() and os.access(registry_path, os.R_OK):
        return CheckResult("registry_path", "ok", f"Registry readable: {registry_path}")
    if not registry_path.exists():
        return CheckResult(
            "registry_path", "fail",
            f"Registry file missing: {registry_path}",
            "Re-install kavi or restore registry.yaml from git",
        )
    return CheckResult(
        "registry_path", "fail",
        f"Registry file not readable: {registry_path}",
        f"chmod +r {registry_path}",
    )


def check_execution_log_path(log_path: Path) -> CheckResult:
    """Verify execution log path is writable (or parent is writable)."""
    if log_path.exists():
        if os.access(log_path, os.W_OK):
            return CheckResult("execution_log", "ok", f"Log writable: {log_path}")
        return CheckResult(
            "execution_log", "fail",
            f"Log file not writable: {log_path}",
            f"chmod +w {log_path}",
        )
    # File doesn't exist — check parent
    parent = log_path.parent
    if parent.exists() and os.access(parent, os.W_OK):
        return CheckResult(
            "execution_log", "ok",
            f"Log parent writable (log will be created): {parent}",
        )
    return CheckResult(
        "execution_log", "warn",
        f"Log parent not writable or missing: {parent}",
        f"mkdir -p {parent}",
    )


def check_config_paths(
    vault_out: Path,
    registry_path: Path,
    log_path: Path,
) -> list[CheckResult]:
    """Run all config/path checks."""
    return [
        check_vault_path(vault_out),
        check_registry_path(registry_path),
        check_execution_log_path(log_path),
    ]


# ---------------------------------------------------------------------------
# Check 2: Registry integrity
# ---------------------------------------------------------------------------

def check_registry_integrity(registry_path: Path) -> list[CheckResult]:
    """Validate registry YAML, skill loadability, and hash trust."""
    results: list[CheckResult] = []

    # Parse YAML
    try:
        import yaml

        with open(registry_path) as f:
            data = yaml.safe_load(f)
        skills = data.get("skills", []) if data else []
    except FileNotFoundError:
        results.append(CheckResult(
            "registry_parse", "fail",
            f"Registry file not found: {registry_path}",
            "Restore registry.yaml from git or re-promote skills",
        ))
        return results
    except Exception as exc:
        results.append(CheckResult(
            "registry_parse", "fail",
            f"Registry YAML parse error: {exc}",
            "Fix syntax in registry.yaml",
        ))
        return results

    results.append(CheckResult(
        "registry_parse", "ok",
        f"Registry YAML valid ({len(skills)} skill(s))",
    ))

    if not skills:
        return results

    # Check for duplicate names
    names = [s.get("name", "") for s in skills]
    seen: set[str] = set()
    dupes: list[str] = []
    for n in names:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    if dupes:
        results.append(CheckResult(
            "registry_duplicates", "fail",
            f"Duplicate skill names: {', '.join(dupes)}",
            "Remove duplicate entries from registry.yaml",
        ))

    # Check each skill
    for entry in skills:
        name = entry.get("name", "<unknown>")
        module_path = entry.get("module_path", "")
        expected_hash = entry.get("hash")

        if not module_path:
            results.append(CheckResult(
                f"skill_{name}", "fail",
                f"Skill '{name}': missing module_path",
                "Fix registry.yaml entry",
            ))
            continue

        # Try importing the module
        parts = module_path.rsplit(".", 1)
        module_name = parts[0] if parts else module_path
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            results.append(CheckResult(
                f"skill_{name}", "fail",
                f"Skill '{name}': import failed — {exc}",
                f"Check that {module_name} exists and has no import errors",
            ))
            continue

        # Hash verification (coerce to str — YAML may parse hex as int)
        expected_hash = str(expected_hash) if expected_hash is not None else None
        if not expected_hash:
            results.append(CheckResult(
                f"skill_{name}", "warn",
                f"Skill '{name}': no hash in registry (trust check skipped)",
                "kavi promote-skill <proposal_id> to store hash",
            ))
            continue

        source_file = getattr(mod, "__file__", None)
        if source_file is None:
            results.append(CheckResult(
                f"skill_{name}", "fail",
                f"Skill '{name}': cannot locate source file",
                "Reinstall the kavi package",
            ))
            continue

        actual_hash = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            results.append(CheckResult(
                f"skill_{name}", "fail",
                f"Skill '{name}': hash drift — "
                f"expected {expected_hash[:12]}..., got {actual_hash[:12]}...",
                f"Re-verify and re-promote the skill, or restore the file from git:\n"
                f"  git checkout -- {source_file}",
            ))
        else:
            results.append(CheckResult(
                f"skill_{name}", "ok",
                f"Skill '{name}': hash verified",
            ))

    return results


# ---------------------------------------------------------------------------
# Check 3: Sparkstation connectivity
# ---------------------------------------------------------------------------

def check_sparkstation(
    base_url: str,
    timeout: float = 1.0,
) -> CheckResult:
    """Best-effort Sparkstation connectivity check with short timeout."""
    try:
        client = OpenAI(api_key="dummy-key", base_url=base_url, timeout=timeout)
        client.models.list()
        return CheckResult(
            "sparkstation", "ok",
            f"Sparkstation reachable at {base_url}",
        )
    except Exception:
        return CheckResult(
            "sparkstation", "warn",
            f"Sparkstation unreachable at {base_url}. "
            "Features impacted: summarize_note may fallback; search_notes uses lexical fallback",
            "Start Sparkstation or check SPARK_BASE_URL in config.py",
        )


# ---------------------------------------------------------------------------
# Check 4: Toolchain availability
# ---------------------------------------------------------------------------

def check_toolchain() -> list[CheckResult]:
    """Lightweight toolchain checks."""
    results: list[CheckResult] = []

    # Python version
    v = sys.version_info
    if v >= (3, 11):
        results.append(CheckResult(
            "python_version", "ok",
            f"Python {v.major}.{v.minor}.{v.micro}",
        ))
    else:
        results.append(CheckResult(
            "python_version", "fail",
            f"Python {v.major}.{v.minor}.{v.micro} (requires >=3.11)",
            "Install Python 3.11+ and recreate the virtualenv",
        ))

    # uv
    if shutil.which("uv"):
        results.append(CheckResult("uv", "ok", "uv available"))
    else:
        results.append(CheckResult(
            "uv", "warn",
            "uv not found on PATH",
            "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh",
        ))

    # ruff (importable check)
    try:
        subprocess.run(
            ["ruff", "--version"],
            capture_output=True, timeout=5,
        )
        results.append(CheckResult("ruff", "ok", "ruff available"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        results.append(CheckResult(
            "ruff", "warn",
            "ruff not found",
            "uv add --dev ruff",
        ))

    return results


# ---------------------------------------------------------------------------
# Check 5: Log sanity
# ---------------------------------------------------------------------------

def check_log_sanity(log_path: Path) -> CheckResult:
    """Check JSONL log for parseable records and report malformed lines."""
    if not log_path.exists():
        return CheckResult(
            "log_sanity", "ok",
            f"No execution log yet (will be created at {log_path})",
        )

    total = 0
    valid = 0
    malformed = 0
    has_version = 0

    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    data = json.loads(line)
                    valid += 1
                    if "v" in data:
                        has_version += 1
                except (json.JSONDecodeError, ValueError):
                    malformed += 1
    except Exception as exc:
        return CheckResult(
            "log_sanity", "fail",
            f"Cannot read execution log: {exc}",
            f"Check file permissions: {log_path}",
        )

    if malformed == 0:
        return CheckResult(
            "log_sanity", "ok",
            f"Execution log: {valid} valid record(s), "
            f"{has_version} with record_version",
        )

    return CheckResult(
        "log_sanity", "warn",
        f"Execution log: {valid} valid, {malformed} malformed line(s) of {total} total",
        "Malformed lines are skipped during reads but may indicate corruption",
    )


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_all_checks(
    vault_out: Path,
    registry_path: Path,
    log_path: Path,
    spark_base_url: str,
    spark_timeout: float = 1.0,
) -> DoctorReport:
    """Run every healthcheck and return an aggregate report."""
    report = DoctorReport()

    report.checks.extend(check_config_paths(vault_out, registry_path, log_path))
    report.checks.extend(check_registry_integrity(registry_path))
    report.checks.append(check_sparkstation(spark_base_url, timeout=spark_timeout))
    report.checks.extend(check_toolchain())
    report.checks.append(check_log_sanity(log_path))

    return report
