"""Forge: verify-skill — run quality gates and policy scanner."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

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


def _run_tool(cmd: list[str], cwd: Path | None = None) -> bool:
    """Run a CLI tool, return True if it exits 0."""
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, cwd=cwd, timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def verify_skill(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    skill_file: Path,
    policy: Policy,
    output_dir: Path,
    project_root: Path | None = None,
) -> tuple[Verification, Artifact]:
    """Run all verification checks on a skill and record results.

    Checks:
    1. ruff (linting)
    2. mypy (type checking)
    3. pytest (unit tests)
    4. Policy scanner (forbidden patterns)
    """
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    cwd = project_root or Path.cwd()

    # Run checks
    ruff_ok = _run_tool(["ruff", "check", str(skill_file)], cwd=cwd)
    mypy_ok = _run_tool(["mypy", str(skill_file)], cwd=cwd)
    pytest_ok = _run_tool(["pytest", "-q", "--tb=short"], cwd=cwd)

    # Policy scan
    violations = scan_file(skill_file, policy)
    scan_result = ScanResult(violations=violations, files_scanned=1)
    policy_ok = scan_result.ok

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

    if not policy_ok:
        report_lines.append("\n## Policy Violations\n")
        report_lines.append(format_report(scan_result))

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
