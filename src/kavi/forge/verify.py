"""Forge: verify-skill — run quality gates and policy scanner."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kavi.artifacts.writer import write_verification_report
from kavi.ledger.models import (
    Artifact,
    ProposalStatus,
    Verification,
    VerificationStatus,
    get_proposal,
    insert_verification,
    update_proposal_status,
)
from kavi.policies.scanner import Policy, ScanResult, format_report, scan_file

# --- ToolRunner interface ---


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


class ToolRunner(Protocol):
    """Interface for running verification tools. Injectable for testing."""

    def run_ruff(self, skill_file: Path, cwd: Path) -> CheckResult: ...
    def run_mypy(self, skill_file: Path, cwd: Path) -> CheckResult: ...
    def run_pytest(self, cwd: Path) -> CheckResult: ...
    def run_policy_scan(self, skill_file: Path, policy: Policy) -> CheckResult: ...


# --- Default runner (subprocess) ---


class SubprocessRunner:
    """Runs real tools via subprocess. Used in production CLI."""

    def _run(self, cmd: list[str], cwd: Path | None = None) -> CheckResult:
        try:
            result = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, cwd=cwd, timeout=120,
            )
            return CheckResult(
                ok=result.returncode == 0,
                detail=result.stdout + result.stderr,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(ok=False, detail="Timeout")
        except FileNotFoundError as e:
            return CheckResult(ok=False, detail=str(e))

    def run_ruff(self, skill_file: Path, cwd: Path) -> CheckResult:
        return self._run(["ruff", "check", str(skill_file)], cwd=cwd)

    def run_mypy(self, skill_file: Path, cwd: Path) -> CheckResult:
        return self._run(["mypy", str(skill_file)], cwd=cwd)

    def run_pytest(self, cwd: Path) -> CheckResult:
        return self._run(["pytest", "-q", "--tb=short"], cwd=cwd)

    def run_policy_scan(self, skill_file: Path, policy: Policy) -> CheckResult:
        violations = scan_file(skill_file, policy)
        scan_result = ScanResult(violations=violations, files_scanned=1)
        detail = format_report(scan_result) if not scan_result.ok else ""
        return CheckResult(ok=scan_result.ok, detail=detail)


_default_runner = SubprocessRunner()


# --- Core verify function ---


def verify_skill(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    skill_file: Path,
    policy: Policy,
    output_dir: Path,
    project_root: Path | None = None,
    runner: ToolRunner | None = None,
) -> tuple[Verification, Artifact]:
    """Run all verification checks on a skill and record results.

    Checks (via runner):
    1. ruff (linting)
    2. mypy (type checking)
    3. pytest (unit tests)
    4. Policy scanner (forbidden patterns)

    Pass a custom ToolRunner to override tool execution (e.g. for testing).
    """
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    if runner is None:
        runner = _default_runner

    cwd = project_root or Path.cwd()

    # Run checks
    ruff_result = runner.run_ruff(skill_file, cwd)
    mypy_result = runner.run_mypy(skill_file, cwd)
    pytest_result = runner.run_pytest(cwd)
    policy_result = runner.run_policy_scan(skill_file, policy)

    ruff_ok = ruff_result.ok
    mypy_ok = mypy_result.ok
    pytest_ok = pytest_result.ok
    policy_ok = policy_result.ok

    all_ok = ruff_ok and mypy_ok and pytest_ok and policy_ok
    status = VerificationStatus.PASSED if all_ok else VerificationStatus.FAILED

    # Build report
    report_lines = [
        "# Verification Report\n",
        f"Proposal: {proposal_id} ({proposal.name})\n",
        "## Results\n",
        f"- ruff: {'PASS' if ruff_ok else 'FAIL'}",
        f"- mypy: {'PASS' if mypy_ok else 'FAIL'}",
        f"- pytest: {'PASS' if pytest_ok else 'FAIL'}",
        f"- policy: {'PASS' if policy_ok else 'FAIL'}",
        f"\n## Overall: {'PASSED' if all_ok else 'FAILED'}\n",
    ]

    if not policy_ok and policy_result.detail:
        report_lines.append("\n## Policy Violations\n")
        report_lines.append(policy_result.detail)

    report_content = "\n".join(report_lines)

    # Write report artifact
    artifact = write_verification_report(
        conn, content=report_content, proposal_id=proposal_id, output_dir=output_dir,
    )

    # Record verification
    verification = Verification(
        proposal_id=proposal_id,
        status=status,
        ruff_ok=ruff_ok,
        mypy_ok=mypy_ok,
        pytest_ok=pytest_ok,
        policy_ok=policy_ok,
        report_path=artifact.path,
    )
    insert_verification(conn, verification)

    # Update proposal status
    if all_ok:
        update_proposal_status(conn, proposal_id, ProposalStatus.VERIFIED)

    return verification, artifact
