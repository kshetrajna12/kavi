"""Failure drill suite — deterministic exercise of D011 iteration/retry.

Each drill engineers a specific, predictable failure on attempt 1, then
verifies that:
1. The failure is classified with the correct FailureKind
2. Facts are extracted with relevant details
3. A RESEARCH_NOTE artifact is produced
4. The retry BUILD_PACKET is enriched with research context
5. Attempt 2 succeeds (verify passes, promote to TRUSTED)

Five drills:
- VERIFY_LINT (ruff): unused import triggers F401 (real ruff)
- VERIFY_LINT (mypy): type return mismatch (stubbed)
- VERIFY_TEST (pytest): logic bug caught by tests (stubbed)
- VERIFY_POLICY: forbidden subprocess import (real scanner)
- GATE_VIOLATION: build-time diff gate failure
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kavi.artifacts.writer import write_artifact
from kavi.forge.build import build_skill, mark_build_failed, mark_build_succeeded
from kavi.forge.promote import promote_skill
from kavi.forge.propose import propose_skill
from kavi.forge.research import FailureKind, research_skill
from kavi.forge.verify import CheckResult, SubprocessRunner, verify_skill
from kavi.ledger.db import init_db
from kavi.ledger.models import (
    ArtifactKind,
    ProposalStatus,
    SideEffectClass,
    VerificationStatus,
    get_proposal,
)
from kavi.policies.scanner import Policy, ScanResult, format_report, scan_file

# ---------------------------------------------------------------------------
# DrillRunner — real policy + invariant, selective ruff, stubbed mypy/pytest
# ---------------------------------------------------------------------------


class DrillRunner:
    """Tool runner for failure drills.

    Always runs real policy scanner and invariant checker (fast, AST-based).
    Optionally runs real ruff. Stubs mypy and pytest with configurable results.
    """

    _sub = SubprocessRunner()

    def __init__(
        self,
        *,
        real_ruff: bool = False,
        ruff_ok: bool = True,
        mypy_ok: bool = True,
        pytest_ok: bool = True,
    ) -> None:
        self._real_ruff = real_ruff
        self._ruff_ok = ruff_ok
        self._mypy_ok = mypy_ok
        self._pytest_ok = pytest_ok

    def run_ruff(self, skill_file: Path, cwd: Path) -> CheckResult:
        if self._real_ruff:
            return self._sub.run_ruff(skill_file, cwd)
        return CheckResult(ok=self._ruff_ok)

    def run_mypy(self, skill_file: Path, cwd: Path) -> CheckResult:
        return CheckResult(ok=self._mypy_ok)

    def run_pytest(self, cwd: Path) -> CheckResult:
        return CheckResult(ok=self._pytest_ok)

    def run_policy_scan(self, skill_file: Path, policy: Policy) -> CheckResult:
        violations = scan_file(skill_file, policy)
        scan_result = ScanResult(violations=violations, files_scanned=1)
        detail = format_report(scan_result) if not scan_result.ok else ""
        return CheckResult(ok=scan_result.ok, detail=detail)

    def run_invariant_check(
        self,
        skill_file: Path,
        *,
        expected_side_effect: str,
        proposal_name: str,
        project_root: Path,
    ) -> CheckResult:
        from kavi.forge.invariants import check_invariants

        result = check_invariants(
            skill_file,
            expected_side_effect=expected_side_effect,
            proposal_name=proposal_name,
            project_root=project_root,
        )
        detail = ""
        if not result.ok:
            detail = "\n".join(
                f"- [{v.check}] {v.message}" for v in result.violations
            )
        return CheckResult(ok=result.ok, detail=detail)


# ---------------------------------------------------------------------------
# Flawed and fixed skill code for each drill
# ---------------------------------------------------------------------------

IO_SCHEMA = '{"input": {"text": "string"}, "output": {"result": "string"}}'

# Drill 1: VERIFY_LINT (ruff) — unused import triggers F401
RUFF_FLAWED = '''\
"""drill_ruff skill — ruff lint failure drill."""

from __future__ import annotations

import os

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillRuffInput(SkillInput):
    text: str


class DrillRuffOutput(SkillOutput):
    length: int


class DrillRuffSkill(BaseSkill):
    name = "drill_ruff"
    description = "Drill: ruff lint failure"
    input_model = DrillRuffInput
    output_model = DrillRuffOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillRuffInput) -> DrillRuffOutput:
        return DrillRuffOutput(length=len(input_data.text))
'''

RUFF_FIXED = '''\
"""drill_ruff skill — ruff lint failure drill (fixed)."""

from __future__ import annotations

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillRuffInput(SkillInput):
    text: str


class DrillRuffOutput(SkillOutput):
    length: int


class DrillRuffSkill(BaseSkill):
    name = "drill_ruff"
    description = "Drill: ruff lint failure"
    input_model = DrillRuffInput
    output_model = DrillRuffOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillRuffInput) -> DrillRuffOutput:
        return DrillRuffOutput(length=len(input_data.text))
'''

# Drill 2: VERIFY_LINT (mypy) — type return mismatch (stubbed)
# In real life, mypy would catch result=input_data.value (int vs str).
# Since mypy is stubbed, we use the same clean code for both attempts.
MYPY_CODE = '''\
"""drill_mypy skill — mypy type failure drill."""

from __future__ import annotations

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillMypyInput(SkillInput):
    value: int


class DrillMypyOutput(SkillOutput):
    result: str


class DrillMypySkill(BaseSkill):
    name = "drill_mypy"
    description = "Drill: mypy type failure"
    input_model = DrillMypyInput
    output_model = DrillMypyOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillMypyInput) -> DrillMypyOutput:
        return DrillMypyOutput(result=str(input_data.value))
'''

# Drill 3: VERIFY_TEST (pytest) — logic bug (stubbed)
# In real life, tests would catch upper=text (should be text.upper()).
# Since pytest is stubbed, we use clean code for both attempts.
PYTEST_CODE = '''\
"""drill_pytest skill — pytest failure drill."""

from __future__ import annotations

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillPytestInput(SkillInput):
    text: str


class DrillPytestOutput(SkillOutput):
    upper: str


class DrillPytestSkill(BaseSkill):
    name = "drill_pytest"
    description = "Drill: pytest failure"
    input_model = DrillPytestInput
    output_model = DrillPytestOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillPytestInput) -> DrillPytestOutput:
        return DrillPytestOutput(upper=input_data.text.upper())
'''

# Drill 4: VERIFY_POLICY — forbidden subprocess import
POLICY_FLAWED = '''\
"""drill_policy skill — policy violation drill."""

from __future__ import annotations

import subprocess

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillPolicyInput(SkillInput):
    text: str


class DrillPolicyOutput(SkillOutput):
    result: str


class DrillPolicySkill(BaseSkill):
    name = "drill_policy"
    description = "Drill: policy violation"
    input_model = DrillPolicyInput
    output_model = DrillPolicyOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillPolicyInput) -> DrillPolicyOutput:
        result = subprocess.run(
            ["echo", input_data.text], capture_output=True, text=True,
        )
        return DrillPolicyOutput(result=result.stdout)
'''

POLICY_FIXED = '''\
"""drill_policy skill — policy violation drill (fixed)."""

from __future__ import annotations

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillPolicyInput(SkillInput):
    text: str


class DrillPolicyOutput(SkillOutput):
    result: str


class DrillPolicySkill(BaseSkill):
    name = "drill_policy"
    description = "Drill: policy violation"
    input_model = DrillPolicyInput
    output_model = DrillPolicyOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillPolicyInput) -> DrillPolicyOutput:
        return DrillPolicyOutput(result=input_data.text.strip())
'''

# Drill 5: GATE_VIOLATION — code for attempt 2 (attempt 1 has no code)
GATE_CODE = '''\
"""drill_gate skill — gate violation drill."""

from __future__ import annotations

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class DrillGateInput(SkillInput):
    text: str


class DrillGateOutput(SkillOutput):
    echoed: str


class DrillGateSkill(BaseSkill):
    name = "drill_gate"
    description = "Drill: gate violation"
    input_model = DrillGateInput
    output_model = DrillGateOutput
    side_effect_class = "READ_ONLY"

    def execute(self, input_data: DrillGateInput) -> DrillGateOutput:
        return DrillGateOutput(echoed=input_data.text)
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture()
def artifacts_dir(tmp_path: Path):
    d = tmp_path / "artifacts_out"
    d.mkdir()
    return d


@pytest.fixture()
def registry_path(tmp_path: Path):
    reg = tmp_path / "registry.yaml"
    reg.write_text("skills: []\n")
    return reg


@pytest.fixture()
def policy():
    return Policy(
        forbidden_imports=["subprocess", "os.system", "pty", "paramiko"],
        allowed_network=False,
        allowed_write_paths=["./vault_out/", "./artifacts_out/"],
        forbid_dynamic_exec=True,
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestFailureDrills:
    """Deterministic failure drills for D011 iteration/retry.

    Each drill follows the cycle:
    propose → build1(mark succeeded) → write flawed code → verify1(FAIL) →
    research(classify) → build2(enriched packet) → write fixed code →
    verify2(PASS) → promote(TRUSTED)
    """

    def _verify_drill_cycle(
        self,
        db,
        artifacts_dir,
        tmp_path,
        policy,
        registry_path,
        *,
        name: str,
        flawed_code: str,
        fixed_code: str,
        fail_runner: DrillRunner,
        pass_runner: DrillRunner,
        expected_kind: FailureKind,
        expected_fact_substring: str,
    ):
        """Run one complete VERIFY_* failure drill cycle."""
        # 1. Propose
        proposal, _ = propose_skill(
            db,
            name=name,
            description=f"Drill: {name}",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.READ_ONLY,
            output_dir=artifacts_dir,
        )

        # 2. Build attempt 1 — mark succeeded (simulating builder produced code)
        build1, packet1 = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        assert build1.attempt_number == 1
        mark_build_succeeded(db, build1.id)

        # 3. Write flawed skill code at conventional path
        skill_dir = tmp_path / "src" / "kavi" / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / f"{name}.py"
        skill_file.write_text(flawed_code)

        # 4. Verify attempt 1 — should FAIL on the targeted check
        v1, report1 = verify_skill(
            db,
            proposal_id=proposal.id,
            policy=policy,
            output_dir=artifacts_dir,
            project_root=tmp_path,
            runner=fail_runner,
        )
        assert v1.status == VerificationStatus.FAILED, (
            f"Expected FAILED for {name} attempt 1, got {v1.status}"
        )

        # 5. Research — classify the failure
        analysis, research_art = research_skill(
            db, build_id=build1.id, output_dir=artifacts_dir,
        )
        assert analysis.kind == expected_kind, (
            f"Expected {expected_kind} but got {analysis.kind}"
        )
        assert any(expected_fact_substring in f for f in analysis.facts), (
            f"Expected '{expected_fact_substring}' in facts: {analysis.facts}"
        )
        research_path = Path(research_art.path)
        assert research_path.exists()
        assert expected_kind.value in research_path.read_text()

        # 6. Build attempt 2 (retry) — enriched BUILD_PACKET
        build2, packet2 = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        assert build2.attempt_number == 2
        assert build2.parent_build_id == build1.id
        p2_content = Path(packet2.path).read_text()
        assert "Research Findings" in p2_content or "Previous Attempt" in p2_content, (
            "Retry BUILD_PACKET missing research context"
        )

        # 7. Write fixed code + mark build 2 succeeded
        skill_file.write_text(fixed_code)
        mark_build_succeeded(db, build2.id)

        # 8. Verify attempt 2 — should PASS
        v2, _ = verify_skill(
            db,
            proposal_id=proposal.id,
            policy=policy,
            output_dir=artifacts_dir,
            project_root=tmp_path,
            runner=pass_runner,
        )
        assert v2.status == VerificationStatus.PASSED, (
            f"Expected PASSED for {name} attempt 2, got {v2.status}"
        )

        # 9. Promote — TRUSTED
        promote_skill(
            db,
            proposal_id=proposal.id,
            project_root=tmp_path,
            registry_path=registry_path,
        )
        p = get_proposal(db, proposal.id)
        assert p is not None
        assert p.status == ProposalStatus.TRUSTED

    def test_drill_ruff_unused_import(
        self, db, artifacts_dir, tmp_path, policy, registry_path,
    ):
        """VERIFY_LINT: unused import (F401) caught by real ruff.

        Attempt 1: `import os` unused → ruff F401 → VERIFY_LINT.
        Attempt 2: import removed → ruff passes → promote to TRUSTED.
        """
        self._verify_drill_cycle(
            db, artifacts_dir, tmp_path, policy, registry_path,
            name="drill_ruff",
            flawed_code=RUFF_FLAWED,
            fixed_code=RUFF_FIXED,
            fail_runner=DrillRunner(real_ruff=True),
            pass_runner=DrillRunner(real_ruff=True),
            expected_kind=FailureKind.VERIFY_LINT,
            expected_fact_substring="ruff",
        )

    def test_drill_mypy_type_mismatch(
        self, db, artifacts_dir, tmp_path, policy, registry_path,
    ):
        """VERIFY_LINT (mypy): type return mismatch (stubbed).

        Attempt 1: mypy stubbed as FAIL → VERIFY_LINT with 'mypy' in facts.
        Attempt 2: mypy stubbed as PASS → promote to TRUSTED.
        """
        self._verify_drill_cycle(
            db, artifacts_dir, tmp_path, policy, registry_path,
            name="drill_mypy",
            flawed_code=MYPY_CODE,
            fixed_code=MYPY_CODE,
            fail_runner=DrillRunner(mypy_ok=False),
            pass_runner=DrillRunner(mypy_ok=True),
            expected_kind=FailureKind.VERIFY_LINT,
            expected_fact_substring="mypy",
        )

    def test_drill_pytest_logic_bug(
        self, db, artifacts_dir, tmp_path, policy, registry_path,
    ):
        """VERIFY_TEST: logic bug caught by tests (stubbed).

        Attempt 1: pytest stubbed as FAIL → VERIFY_TEST with 'pytest' in facts.
        Attempt 2: pytest stubbed as PASS → promote to TRUSTED.
        """
        self._verify_drill_cycle(
            db, artifacts_dir, tmp_path, policy, registry_path,
            name="drill_pytest",
            flawed_code=PYTEST_CODE,
            fixed_code=PYTEST_CODE,
            fail_runner=DrillRunner(pytest_ok=False),
            pass_runner=DrillRunner(pytest_ok=True),
            expected_kind=FailureKind.VERIFY_TEST,
            expected_fact_substring="pytest",
        )

    def test_drill_policy_forbidden_import(
        self, db, artifacts_dir, tmp_path, policy, registry_path,
    ):
        """VERIFY_POLICY: forbidden subprocess import caught by real scanner.

        Attempt 1: `import subprocess` → policy violation → VERIFY_POLICY.
        Attempt 2: subprocess removed → policy passes → promote to TRUSTED.

        Note: In production, VERIFY_POLICY triggers SECURITY_CLASS escalation
        (D011), requiring human review before retry.
        """
        self._verify_drill_cycle(
            db, artifacts_dir, tmp_path, policy, registry_path,
            name="drill_policy",
            flawed_code=POLICY_FLAWED,
            fixed_code=POLICY_FIXED,
            fail_runner=DrillRunner(),
            pass_runner=DrillRunner(),
            expected_kind=FailureKind.VERIFY_POLICY,
            expected_fact_substring="Policy",
        )

    def test_drill_gate_violation(
        self, db, artifacts_dir, tmp_path, policy, registry_path,
    ):
        """GATE_VIOLATION: build-time diff gate failure.

        Attempt 1: build fails — Claude modified pyproject.toml outside allowlist.
        Research classifies as GATE_VIOLATION with disallowed file in facts.
        Attempt 2: enriched BUILD_PACKET, correct code → verify → promote.
        """
        # 1. Propose
        proposal, _ = propose_skill(
            db,
            name="drill_gate",
            description="Drill: gate violation",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.READ_ONLY,
            output_dir=artifacts_dir,
        )

        # 2. Build attempt 1 — mark FAILED (gate violation)
        build1, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        assert build1.attempt_number == 1
        mark_build_failed(
            db,
            build1.id,
            summary=(
                "Diff gate failed: Allowed: "
                "['src/kavi/skills/drill_gate.py', "
                "'tests/test_skill_drill_gate.py'], "
                "Violations: ['pyproject.toml'], Missing: []"
            ),
        )

        # Write build log artifact with gate details for research
        build_log_content = (
            f"# Build Log: drill_gate\n\n"
            f"## Metadata\n"
            f"- **Build ID**: `{build1.id}`\n"
            f"- **Proposal ID**: `{proposal.id}`\n\n"
            f"## Exit code: 0\n\n"
            f"## Diff Allowlist Gate: FAIL\n"
            f"- Changed (untracked): ['src/kavi/skills/drill_gate.py', "
            f"'tests/test_skill_drill_gate.py', 'pyproject.toml']\n"
            f"- Allowed: ['src/kavi/skills/drill_gate.py', "
            f"'tests/test_skill_drill_gate.py']\n"
            f"- Violations: ['pyproject.toml']\n"
            f"- Required missing: []\n"
        )
        write_artifact(
            db,
            content=build_log_content,
            path=artifacts_dir / f"build_log_{build1.id}.md",
            kind=ArtifactKind.BUILD_LOG,
            related_id=proposal.id,
        )

        # 3. Research — classify as GATE_VIOLATION
        analysis, research_art = research_skill(
            db, build_id=build1.id, output_dir=artifacts_dir,
        )
        assert analysis.kind == FailureKind.GATE_VIOLATION
        assert any("pyproject.toml" in f for f in analysis.facts)
        research_path = Path(research_art.path)
        assert research_path.exists()
        assert "GATE_VIOLATION" in research_path.read_text()

        # 4. Build attempt 2 (retry) — enriched BUILD_PACKET
        build2, packet2 = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        assert build2.attempt_number == 2
        assert build2.parent_build_id == build1.id
        p2_content = Path(packet2.path).read_text()
        assert "Research Findings" in p2_content or "Previous Attempt" in p2_content

        # 5. Write correct code + mark build 2 succeeded
        skill_dir = tmp_path / "src" / "kavi" / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "drill_gate.py").write_text(GATE_CODE)
        mark_build_succeeded(db, build2.id)

        # 6. Verify — should pass
        v, _ = verify_skill(
            db,
            proposal_id=proposal.id,
            policy=policy,
            output_dir=artifacts_dir,
            project_root=tmp_path,
            runner=DrillRunner(),
        )
        assert v.status == VerificationStatus.PASSED

        # 7. Promote — TRUSTED
        promote_skill(
            db,
            proposal_id=proposal.id,
            project_root=tmp_path,
            registry_path=registry_path,
        )
        p = get_proposal(db, proposal.id)
        assert p is not None
        assert p.status == ProposalStatus.TRUSTED
