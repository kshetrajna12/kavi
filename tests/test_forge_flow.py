"""End-to-end tests for the forge pipeline.

Tests the full flow: propose → build → verify → promote → run
using a stub skill (write_note) without invoking Claude Code.

Fast tests use StubRunner (no subprocesses).
One slow integration test uses SubprocessRunner (real tools).
"""

from pathlib import Path

import pytest

from kavi.forge.build import build_skill, diff_allowlist_gate, mark_build_succeeded
from kavi.forge.promote import promote_skill
from kavi.forge.propose import propose_skill
from kavi.forge.verify import CheckResult, SubprocessRunner, verify_skill
from kavi.ledger.db import init_db
from kavi.ledger.models import (
    ProposalStatus,
    SideEffectClass,
    VerificationStatus,
    get_proposal,
)
from kavi.policies.scanner import Policy, ScanResult, scan_file
from kavi.skills.loader import TrustError, list_skills, load_skill

# --- Stub runner for fast tests ---


class StubRunner:
    """Fast test runner — returns canned results, no subprocesses."""

    def __init__(
        self, *,
        ruff_ok: bool = True,
        mypy_ok: bool = True,
        pytest_ok: bool = True,
        policy_scan_real: bool = True,
        invariant_check_real: bool = True,
    ) -> None:
        self._ruff_ok = ruff_ok
        self._mypy_ok = mypy_ok
        self._pytest_ok = pytest_ok
        self._policy_scan_real = policy_scan_real
        self._invariant_check_real = invariant_check_real

    def run_ruff(self, skill_file: Path, cwd: Path) -> CheckResult:
        return CheckResult(ok=self._ruff_ok)

    def run_mypy(self, skill_file: Path, cwd: Path) -> CheckResult:
        return CheckResult(ok=self._mypy_ok)

    def run_pytest(self, cwd: Path) -> CheckResult:
        return CheckResult(ok=self._pytest_ok)

    def run_policy_scan(self, skill_file: Path, policy: Policy) -> CheckResult:
        if self._policy_scan_real:
            violations = scan_file(skill_file, policy)
            scan_result = ScanResult(violations=violations, files_scanned=1)
            return CheckResult(ok=scan_result.ok)
        return CheckResult(ok=True)

    def run_invariant_check(
        self, skill_file: Path, *, expected_side_effect: str,
        proposal_name: str, project_root: Path,
    ) -> CheckResult:
        if self._invariant_check_real:
            from kavi.forge.invariants import check_invariants
            result = check_invariants(
                skill_file,
                expected_side_effect=expected_side_effect,
                proposal_name=proposal_name,
                project_root=project_root,
            )
            return CheckResult(ok=result.ok)
        return CheckResult(ok=True)


# --- Fixtures ---


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


@pytest.fixture()
def stub_runner():
    return StubRunner()


@pytest.fixture()
def failing_policy_runner():
    """Stub where ruff/mypy/pytest pass but policy scan is real."""
    return StubRunner(policy_scan_real=True)


# A minimal write_note skill implementation for testing
WRITE_NOTE_SKILL_CODE = '''\
"""write_note skill — writes a markdown note to the vault."""

from pathlib import Path

from pydantic import BaseModel

from kavi.skills.base import BaseSkill


class WriteNoteInput(BaseModel):
    path: str
    title: str
    body: str


class WriteNoteOutput(BaseModel):
    written_path: str
    sha256: str


class WriteNoteSkill(BaseSkill):
    name = "write_note"
    description = "Write a markdown note to the vault"
    input_model = WriteNoteInput
    output_model = WriteNoteOutput
    side_effect_class = "FILE_WRITE"

    def execute(self, input_data: WriteNoteInput) -> WriteNoteOutput:
        import hashlib

        vault_root = Path("./vault_out")
        full_path = vault_root / input_data.path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        content = f"""---
title: {input_data.title}
---

{input_data.body}
"""
        full_path.write_text(content, encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return WriteNoteOutput(written_path=str(full_path), sha256=sha)
'''


@pytest.fixture()
def skill_file(tmp_path: Path):
    """Write a stub write_note skill to disk."""
    skills_dir = tmp_path / "src" / "kavi" / "skills"
    skills_dir.mkdir(parents=True)
    f = skills_dir / "write_note.py"
    f.write_text(WRITE_NOTE_SKILL_CODE)
    return f


IO_SCHEMA = '''{
    "input": {"path": "string", "title": "string", "body": "string"},
    "output": {"written_path": "string", "sha256": "string"}
}'''


class TestFullForgeFlow:
    """Test the complete propose → build → (stub) → verify → promote flow."""

    def test_propose_creates_proposal_and_spec(self, db, artifacts_dir):
        proposal, artifact = propose_skill(
            db,
            name="write_note",
            description="Write a markdown note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        assert proposal.status == ProposalStatus.PROPOSED
        assert Path(artifact.path).exists()
        assert "write_note" in Path(artifact.path).read_text()

    def test_build_creates_packet(self, db, artifacts_dir):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, packet = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        assert build.branch_name.startswith("skill/write_note-")
        assert Path(packet.path).exists()
        content = Path(packet.path).read_text()
        assert "Build Packet" in content
        assert "write_note" in content

    def test_build_rejects_non_proposed(self, db, artifacts_dir):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(db, proposal_id=proposal.id, output_dir=artifacts_dir)
        mark_build_succeeded(db, build.id)

        # Can't build again — status is now BUILT
        with pytest.raises(ValueError, match="expected PROPOSED"):
            build_skill(db, proposal_id=proposal.id, output_dir=artifacts_dir)

    def test_verify_with_clean_skill(
        self, db, artifacts_dir, skill_file, policy, stub_runner, tmp_path,
    ):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        verification, report = verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=stub_runner,
        )
        assert verification.policy_ok is True
        assert verification.ruff_ok is True
        assert verification.mypy_ok is True
        assert verification.pytest_ok is True
        assert verification.status == VerificationStatus.PASSED
        assert Path(report.path).exists()

    def test_verify_catches_bad_skill(self, db, artifacts_dir, tmp_path, policy):
        proposal, _ = propose_skill(
            db, name="bad_skill", description="A bad skill",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        # Write a bad skill file at the conventional path
        bad_dir = tmp_path / "src" / "kavi" / "skills"
        bad_dir.mkdir(parents=True)
        bad_file = bad_dir / "bad_skill.py"
        bad_file.write_text("import subprocess\neval('x')\n")

        # Use stub for ruff/mypy/pytest but real policy scan
        runner = StubRunner(policy_scan_real=True)
        verification, _ = verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=runner,
        )
        assert verification.policy_ok is False
        assert verification.status == VerificationStatus.FAILED

    def test_verify_fails_on_ruff(self, db, artifacts_dir, skill_file, policy, tmp_path):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        runner = StubRunner(ruff_ok=False)
        verification, _ = verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=runner,
        )
        assert verification.ruff_ok is False
        assert verification.status == VerificationStatus.FAILED

    def test_promote_updates_registry(
        self, db, artifacts_dir, skill_file, policy, registry_path, tmp_path,
    ):
        # Full flow: propose → build → verify (stub) → promote
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        # Verify with all-pass stub
        verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=StubRunner(),
        )

        promotion = promote_skill(
            db, proposal_id=proposal.id,
            project_root=tmp_path, registry_path=registry_path,
        )
        assert promotion.to_status == "TRUSTED"

        # Check registry was updated
        skills = list_skills(registry_path)
        assert len(skills) == 1
        assert skills[0]["name"] == "write_note"
        assert skills[0]["hash"]
        assert skills[0]["module_path"] == "kavi.skills.write_note.WriteNoteSkill"

        # Check proposal status
        p = get_proposal(db, proposal.id)
        assert p is not None
        assert p.status == ProposalStatus.TRUSTED

    def test_promote_rejects_non_verified(
        self, db, artifacts_dir, skill_file, registry_path, tmp_path,
    ):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        with pytest.raises(ValueError, match="expected VERIFIED"):
            promote_skill(
                db, proposal_id=proposal.id,
                project_root=tmp_path, registry_path=registry_path,
            )

    def test_full_pipeline_propose_through_run(
        self, db, artifacts_dir, policy, registry_path, tmp_path,
        monkeypatch,
    ):
        """End-to-end: propose → build(stub) → verify → promote → load → run.

        Uses the real installed write_note.py content (not the test stub)
        so that promote's hash matches what _verify_trust reads at import time.
        """
        import kavi.skills.write_note as wn_mod

        # Place the real installed write_note.py at the convention path in tmp_path
        real_content = Path(wn_mod.__file__).read_text()
        skills_dir = tmp_path / "src" / "kavi" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "write_note.py").write_text(real_content)

        # propose
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        # build (stub — skip Claude, mark succeeded)
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        # verify
        verification, _ = verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=StubRunner(),
        )
        assert verification.status == VerificationStatus.PASSED

        # promote — hashes the file in tmp_path (same content as installed)
        promote_skill(
            db, proposal_id=proposal.id,
            project_root=tmp_path, registry_path=registry_path,
        )

        # load + trust check + run
        skill = load_skill(registry_path, "write_note")
        monkeypatch.chdir(tmp_path)
        result = skill.validate_and_run({
            "path": "test.md", "title": "Test", "body": "Hello",
        })
        assert "written_path" in result
        assert "sha256" in result
        assert (tmp_path / "vault_out" / "test.md").exists()

    def test_trust_enforcement_blocks_tampered_skill(
        self, db, artifacts_dir, skill_file, policy, registry_path, tmp_path,
    ):
        """After promote, tampering the skill file blocks load_skill."""
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)
        verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=StubRunner(),
        )
        promote_skill(
            db, proposal_id=proposal.id,
            project_root=tmp_path, registry_path=registry_path,
        )

        # Tamper with the skill file after promotion
        skill_file.write_text(WRITE_NOTE_SKILL_CODE + "\n# tampered\n")

        with pytest.raises(TrustError, match="failed trust check"):
            load_skill(registry_path, "write_note")


@pytest.mark.slow
class TestVerifyIntegration:
    """Integration test that runs real tools via SubprocessRunner.

    Skipped by default. Run with: pytest -m slow
    """

    def test_real_verify_clean_skill(self, db, artifacts_dir, skill_file, policy, tmp_path):
        proposal, _ = propose_skill(
            db, name="write_note", description="Write a note",
            io_schema_json=IO_SCHEMA,
            side_effect_class=SideEffectClass.FILE_WRITE,
            output_dir=artifacts_dir,
        )
        build, _ = build_skill(
            db, proposal_id=proposal.id, output_dir=artifacts_dir,
        )
        mark_build_succeeded(db, build.id)

        verification, report = verify_skill(
            db, proposal_id=proposal.id,
            policy=policy, output_dir=artifacts_dir,
            project_root=tmp_path, runner=SubprocessRunner(),
        )
        assert verification.policy_ok is True
        assert Path(report.path).exists()


class TestDiffAllowlistGate:
    """Tests for the diff allowlist gate (D009)."""

    def _init_git_sandbox(self, path: Path) -> None:
        """Initialize a git repo with an initial commit for diff detection."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(path), capture_output=True, check=True,
        )
        # Create a dummy file so we have an initial commit
        (path / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(path), capture_output=True, check=True,
        )

    def test_only_allowed_files_pass(self, tmp_path: Path) -> None:
        self._init_git_sandbox(tmp_path)

        skill_dir = tmp_path / "src" / "kavi" / "skills"
        skill_dir.mkdir(parents=True)
        (skill_dir / "write_note.py").write_text("# skill")

        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_skill_write_note.py").write_text("# test")

        result = diff_allowlist_gate("write_note", tmp_path)
        assert result.ok is True
        assert len(result.allowed) == 2
        assert result.violations == []

    def test_extra_file_fails(self, tmp_path: Path) -> None:
        self._init_git_sandbox(tmp_path)

        skill_dir = tmp_path / "src" / "kavi" / "skills"
        skill_dir.mkdir(parents=True)
        (skill_dir / "write_note.py").write_text("# skill")

        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_skill_write_note.py").write_text("# test")

        # Also modify a disallowed file
        (tmp_path / "pyproject.toml").write_text("# hacked")

        result = diff_allowlist_gate("write_note", tmp_path)
        assert result.ok is False
        assert "pyproject.toml" in result.violations

    def test_missing_required_files_fails(self, tmp_path: Path) -> None:
        self._init_git_sandbox(tmp_path)

        # Only create the skill, not the test
        skill_dir = tmp_path / "src" / "kavi" / "skills"
        skill_dir.mkdir(parents=True)
        (skill_dir / "write_note.py").write_text("# skill")

        result = diff_allowlist_gate("write_note", tmp_path)
        assert result.ok is False

    def test_no_changes_fails(self, tmp_path: Path) -> None:
        self._init_git_sandbox(tmp_path)

        result = diff_allowlist_gate("write_note", tmp_path)
        assert result.ok is False
        assert "No files" in result.violations[0]
