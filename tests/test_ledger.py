"""Tests for ledger schema and CRUD operations."""

from pathlib import Path

import pytest

from kavi.ledger.db import init_db
from kavi.ledger.models import (
    Artifact,
    ArtifactKind,
    Build,
    BuildStatus,
    Promotion,
    ProposalStatus,
    SideEffectClass,
    SkillProposal,
    Verification,
    VerificationStatus,
    get_artifacts_for_related,
    get_build,
    get_builds_for_proposal,
    get_latest_verification,
    get_proposal,
    insert_artifact,
    insert_build,
    insert_promotion,
    insert_proposal,
    insert_verification,
    list_proposals,
    update_build,
    update_proposal_status,
)


@pytest.fixture()
def db(tmp_path: Path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture()
def sample_proposal():
    return SkillProposal(
        name="write_note",
        description="Write a markdown note",
        io_schema_json='{"input": {"path": "string"}, "output": {"written_path": "string"}}',
        side_effect_class=SideEffectClass.FILE_WRITE,
    )


class TestInitDb:
    def test_creates_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [row["name"] for row in tables]
        assert "skill_proposals" in names
        assert "builds" in names
        assert "verifications" in names
        assert "promotions" in names
        assert "artifacts" in names

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn1 = init_db(db_path)
        conn1.close()
        conn2 = init_db(db_path)
        tables = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) > 0
        conn2.close()


class TestProposals:
    def test_insert_and_get(self, db, sample_proposal):
        inserted = insert_proposal(db, sample_proposal)
        assert inserted.id == sample_proposal.id

        fetched = get_proposal(db, sample_proposal.id)
        assert fetched is not None
        assert fetched.name == "write_note"
        assert fetched.status == ProposalStatus.PROPOSED

    def test_get_missing(self, db):
        assert get_proposal(db, "nonexistent") is None

    def test_update_status(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        update_proposal_status(db, sample_proposal.id, ProposalStatus.BUILT)
        fetched = get_proposal(db, sample_proposal.id)
        assert fetched is not None
        assert fetched.status == ProposalStatus.BUILT

    def test_list_all(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        proposals = list_proposals(db)
        assert len(proposals) == 1

    def test_list_by_status(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        assert len(list_proposals(db, status=ProposalStatus.PROPOSED)) == 1
        assert len(list_proposals(db, status=ProposalStatus.TRUSTED)) == 0


class TestBuilds:
    def test_insert_and_get(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        build = Build(proposal_id=sample_proposal.id, branch_name="skill/write_note-abc")
        insert_build(db, build)

        fetched = get_build(db, build.id)
        assert fetched is not None
        assert fetched.status == BuildStatus.STARTED

    def test_update_build(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        build = Build(proposal_id=sample_proposal.id, branch_name="skill/write_note-abc")
        insert_build(db, build)

        update_build(db, build.id, status=BuildStatus.SUCCEEDED, summary="All good")
        fetched = get_build(db, build.id)
        assert fetched is not None
        assert fetched.status == BuildStatus.SUCCEEDED
        assert fetched.summary == "All good"

    def test_builds_for_proposal(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        b1 = Build(proposal_id=sample_proposal.id, branch_name="skill/write_note-001")
        b2 = Build(proposal_id=sample_proposal.id, branch_name="skill/write_note-002")
        insert_build(db, b1)
        insert_build(db, b2)

        builds = get_builds_for_proposal(db, sample_proposal.id)
        assert len(builds) == 2


class TestVerifications:
    def test_insert_and_get_latest(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        v = Verification(
            proposal_id=sample_proposal.id,
            status=VerificationStatus.PASSED,
            ruff_ok=True, mypy_ok=True, pytest_ok=True, policy_ok=True,
            report_path="artifacts_out/report.md",
        )
        insert_verification(db, v)

        latest = get_latest_verification(db, sample_proposal.id)
        assert latest is not None
        assert latest.status == VerificationStatus.PASSED
        assert latest.ruff_ok is True


class TestPromotions:
    def test_insert(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        promo = Promotion(
            proposal_id=sample_proposal.id,
            from_status="VERIFIED",
            to_status="TRUSTED",
            approved_by="kshetrajna",
        )
        inserted = insert_promotion(db, promo)
        assert inserted.approved_by == "kshetrajna"


class TestArtifacts:
    def test_insert_and_query(self, db, sample_proposal):
        insert_proposal(db, sample_proposal)
        art = Artifact(
            kind=ArtifactKind.SKILL_SPEC,
            path="artifacts_out/write_note_spec.md",
            sha256="abc123",
            related_id=sample_proposal.id,
        )
        insert_artifact(db, art)

        artifacts = get_artifacts_for_related(db, sample_proposal.id)
        assert len(artifacts) == 1
        assert artifacts[0].kind == ArtifactKind.SKILL_SPEC
        assert artifacts[0].sha256 == "abc123"
