"""Tests for SECRET_READ governance infrastructure (D013)."""

from __future__ import annotations

import ast
import sqlite3
import textwrap
from pathlib import Path

from kavi.ledger.db import init_db
from kavi.ledger.models import (
    SideEffectClass,
    SkillProposal,
    insert_proposal,
)
from kavi.policies.scanner import Policy, PolicyViolation, _Visitor, scan_file

# ---------------------------------------------------------------------------
# 1. SECRET_READ enum
# ---------------------------------------------------------------------------

def test_secret_read_enum_value():
    assert SideEffectClass.SECRET_READ == "SECRET_READ"
    assert SideEffectClass("SECRET_READ") is SideEffectClass.SECRET_READ


def test_secret_read_in_enum_members():
    values = [e.value for e in SideEffectClass]
    assert "SECRET_READ" in values


# ---------------------------------------------------------------------------
# 2. DB migration v5 accepts SECRET_READ proposals
# ---------------------------------------------------------------------------

def test_migration_v5_accepts_secret_read(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    proposal = SkillProposal(
        name="test_secret_skill",
        description="Test skill with SECRET_READ",
        io_schema_json="{}",
        side_effect_class=SideEffectClass.SECRET_READ,
        required_secrets_json='["API_KEY"]',
    )
    insert_proposal(conn, proposal)

    # Verify round-trip
    from kavi.ledger.models import get_proposal
    fetched = get_proposal(conn, proposal.id)
    assert fetched is not None
    assert fetched.side_effect_class == SideEffectClass.SECRET_READ
    assert fetched.required_secrets_json == '["API_KEY"]'
    conn.close()


def test_migration_v5_upgrades_existing_db(tmp_path: Path):
    """Simulate an existing v4 DB and verify migration to v5."""
    db_path = tmp_path / "test.db"
    # Create a v4-style database manually
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE skill_proposals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            io_schema_json TEXT NOT NULL,
            side_effect_class TEXT NOT NULL CHECK (
                side_effect_class IN ('READ_ONLY', 'FILE_WRITE', 'NETWORK', 'MONEY', 'MESSAGING')
            ),
            required_secrets_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'PROPOSED',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE builds (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
            branch_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'STARTED',
            summary TEXT,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            parent_build_id TEXT REFERENCES builds(id)
        );
        CREATE TABLE verifications (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
            status TEXT NOT NULL,
            ruff_ok INTEGER NOT NULL DEFAULT 0,
            mypy_ok INTEGER NOT NULL DEFAULT 0,
            pytest_ok INTEGER NOT NULL DEFAULT 0,
            policy_ok INTEGER NOT NULL DEFAULT 0,
            invariant_ok INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE promotions (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            related_id TEXT
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (4);
    """)
    # Insert a v4 proposal (NETWORK, not SECRET_READ)
    conn.execute(
        "INSERT INTO skill_proposals (id, name, description, io_schema_json, "
        "side_effect_class, status, created_at) "
        "VALUES ('old1', 'old_skill', 'test', '{}', 'NETWORK', 'PROPOSED', "
        "'2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Now open via init_db which should run migration 5
    conn = init_db(db_path)

    # Old data preserved
    from kavi.ledger.models import get_proposal
    old = get_proposal(conn, "old1")
    assert old is not None
    assert old.name == "old_skill"

    # SECRET_READ now accepted
    new_proposal = SkillProposal(
        name="secret_skill",
        description="Test",
        io_schema_json="{}",
        side_effect_class=SideEffectClass.SECRET_READ,
    )
    insert_proposal(conn, new_proposal)
    fetched = get_proposal(conn, new_proposal.id)
    assert fetched is not None
    assert fetched.side_effect_class == SideEffectClass.SECRET_READ
    conn.close()


# ---------------------------------------------------------------------------
# 3. SkillInfo surfaces required_secrets from registry
# ---------------------------------------------------------------------------

def test_skillinfo_required_secrets():
    from kavi.consumer.shim import SkillInfo
    info = SkillInfo(
        name="test",
        description="test",
        side_effect_class="NETWORK",
        version="1.0.0",
        source_hash="abc",
        input_schema={},
        output_schema={},
        required_secrets=["API_KEY", "SECRET_TOKEN"],
    )
    assert info.required_secrets == ["API_KEY", "SECRET_TOKEN"]


def test_skillinfo_defaults_empty_secrets():
    from kavi.consumer.shim import SkillInfo
    info = SkillInfo(
        name="test",
        description="test",
        side_effect_class="READ_ONLY",
        version="1.0.0",
        source_hash="abc",
        input_schema={},
        output_schema={},
    )
    assert info.required_secrets == []


# ---------------------------------------------------------------------------
# 4. Promote preserves required_secrets from proposal
# ---------------------------------------------------------------------------

def test_promote_preserves_required_secrets(tmp_path: Path):
    """Promote should write required_secrets from proposal, not hardcode []."""
    import yaml

    from kavi.forge.promote import promote_skill
    from kavi.ledger.models import (
        ProposalStatus,
        Verification,
        VerificationStatus,
        insert_verification,
        update_proposal_status,
    )

    db_path = tmp_path / "test.db"
    conn = init_db(db_path)

    # Create proposal with required_secrets
    proposal = SkillProposal(
        name="test_promote",
        description="Test promote secrets",
        io_schema_json="{}",
        side_effect_class=SideEffectClass.NETWORK,
        required_secrets_json='["MY_API_KEY"]',
    )
    insert_proposal(conn, proposal)

    # Move through pipeline: PROPOSED -> BUILT -> VERIFIED
    update_proposal_status(conn, proposal.id, ProposalStatus.BUILT)
    update_proposal_status(conn, proposal.id, ProposalStatus.VERIFIED)

    # Insert passing verification
    v = Verification(
        proposal_id=proposal.id,
        status=VerificationStatus.PASSED,
        ruff_ok=True,
        mypy_ok=True,
        pytest_ok=True,
        policy_ok=True,
        invariant_ok=True,
    )
    insert_verification(conn, v)

    # Create skill file
    project_root = tmp_path / "project"
    skill_dir = project_root / "src" / "kavi" / "skills"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "test_promote.py"
    skill_file.write_text("# skill\n")

    # Create registry
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text("skills: []\n")

    promote_skill(
        conn,
        proposal_id=proposal.id,
        project_root=project_root,
        registry_path=registry_path,
    )

    # Check registry has required_secrets from proposal
    with open(registry_path) as f:
        data = yaml.safe_load(f)
    skills = data["skills"]
    assert len(skills) == 1
    assert skills[0]["required_secrets"] == ["MY_API_KEY"]
    conn.close()


# ---------------------------------------------------------------------------
# 5. Secret-leak scanner
# ---------------------------------------------------------------------------

def _make_policy() -> Policy:
    return Policy(
        forbidden_imports=[],
        allowed_network=False,
        allowed_write_paths=[],
        forbid_dynamic_exec=True,
    )


def _scan_code(code: str) -> list[PolicyViolation]:
    """Parse code and scan with the secret_leak rule."""
    source = textwrap.dedent(code)
    tree = ast.parse(source, filename="<test>")
    visitor = _Visitor(_make_policy(), "<test>")
    visitor.visit(tree)
    return [v for v in visitor.violations if v.rule == "secret_leak"]


def test_secret_leak_print_environ():
    violations = _scan_code("""
        import os
        print(os.environ["API_KEY"])
    """)
    assert len(violations) == 1
    assert violations[0].rule == "secret_leak"


def test_secret_leak_print_getenv():
    violations = _scan_code("""
        import os
        print(os.getenv("API_KEY"))
    """)
    assert len(violations) == 1
    assert violations[0].rule == "secret_leak"


def test_secret_leak_fstring_interpolation():
    violations = _scan_code("""
        import os
        print(f"key={os.environ['API_KEY']}")
    """)
    assert len(violations) == 1
    assert violations[0].rule == "secret_leak"


def test_secret_leak_fstring_getenv():
    violations = _scan_code("""
        import os
        print(f"key={os.getenv('API_KEY')}")
    """)
    assert len(violations) == 1
    assert violations[0].rule == "secret_leak"


def test_no_leak_plain_print():
    violations = _scan_code("""
        print("hello world")
    """)
    assert len(violations) == 0


def test_no_leak_env_access_without_print():
    violations = _scan_code("""
        import os
        key = os.environ["API_KEY"]
        header = f"Bearer {key}"
    """)
    assert len(violations) == 0


def test_secret_leak_logging_call():
    violations = _scan_code("""
        import os
        import logging
        logging.info(os.environ["SECRET"])
    """)
    assert len(violations) == 1


def test_secret_leak_scan_file(tmp_path: Path):
    """Integration: scan_file detects secret_leak in a file."""
    code = tmp_path / "bad_skill.py"
    code.write_text(textwrap.dedent("""\
        import os
        def run():
            print(os.environ["SECRET_KEY"])
    """))
    violations = scan_file(code, _make_policy())
    secret_violations = [v for v in violations if v.rule == "secret_leak"]
    assert len(secret_violations) == 1


# ---------------------------------------------------------------------------
# 6. NETWORK and SECRET_READ trigger agent confirmation
# ---------------------------------------------------------------------------

def test_confirm_network_skill():
    from kavi.agent.constants import CONFIRM_SIDE_EFFECTS
    assert "NETWORK" in CONFIRM_SIDE_EFFECTS


def test_confirm_secret_read_skill():
    from kavi.agent.constants import CONFIRM_SIDE_EFFECTS
    assert "SECRET_READ" in CONFIRM_SIDE_EFFECTS


def test_confirm_file_write_still_works():
    from kavi.agent.constants import CONFIRM_SIDE_EFFECTS
    assert "FILE_WRITE" in CONFIRM_SIDE_EFFECTS


def test_read_only_no_confirm():
    from kavi.agent.constants import CONFIRM_SIDE_EFFECTS
    assert "READ_ONLY" not in CONFIRM_SIDE_EFFECTS
