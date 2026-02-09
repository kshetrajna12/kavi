"""Pydantic models and DB operations for ledger tables."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# --- Enums ---

class SideEffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    FILE_WRITE = "FILE_WRITE"
    NETWORK = "NETWORK"
    MONEY = "MONEY"
    MESSAGING = "MESSAGING"


class ProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    BUILT = "BUILT"
    VERIFIED = "VERIFIED"
    TRUSTED = "TRUSTED"


class BuildStatus(StrEnum):
    STARTED = "STARTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


class VerificationStatus(StrEnum):
    FAILED = "FAILED"
    PASSED = "PASSED"


class ArtifactKind(StrEnum):
    SKILL_SPEC = "SKILL_SPEC"
    PATCH_SUMMARY = "PATCH_SUMMARY"
    VERIFICATION_REPORT = "VERIFICATION_REPORT"
    NOTE = "NOTE"
    BUILD_PACKET = "BUILD_PACKET"
    BUILD_LOG = "BUILD_LOG"
    RESEARCH_NOTE = "RESEARCH_NOTE"


# --- Pydantic Models ---

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillProposal(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str
    description: str
    io_schema_json: str
    side_effect_class: SideEffectClass
    required_secrets_json: str = "[]"
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_at: str = Field(default_factory=_now)


class Build(BaseModel):
    id: str = Field(default_factory=_new_id)
    proposal_id: str
    branch_name: str
    started_at: str = Field(default_factory=_now)
    finished_at: str | None = None
    status: BuildStatus = BuildStatus.STARTED
    summary: str | None = None
    attempt_number: int = 1
    parent_build_id: str | None = None


class Verification(BaseModel):
    id: str = Field(default_factory=_new_id)
    proposal_id: str
    status: VerificationStatus
    ruff_ok: bool = False
    mypy_ok: bool = False
    pytest_ok: bool = False
    policy_ok: bool = False
    invariant_ok: bool = False
    report_path: str | None = None
    created_at: str = Field(default_factory=_now)


class Promotion(BaseModel):
    id: str = Field(default_factory=_new_id)
    proposal_id: str
    from_status: str
    to_status: str
    approved_by: str
    created_at: str = Field(default_factory=_now)


class Artifact(BaseModel):
    id: str = Field(default_factory=_new_id)
    kind: ArtifactKind
    path: str
    sha256: str
    created_at: str = Field(default_factory=_now)
    related_id: str | None = None


# --- DB Operations ---

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def insert_proposal(conn: sqlite3.Connection, proposal: SkillProposal) -> SkillProposal:
    conn.execute(
        """INSERT INTO skill_proposals
           (id, name, description, io_schema_json, side_effect_class,
            required_secrets_json, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proposal.id, proposal.name, proposal.description,
            proposal.io_schema_json, proposal.side_effect_class.value,
            proposal.required_secrets_json, proposal.status.value,
            proposal.created_at,
        ),
    )
    conn.commit()
    return proposal


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> SkillProposal | None:
    cursor = conn.execute("SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return SkillProposal(**_row_to_dict(row))


def update_proposal_status(
    conn: sqlite3.Connection, proposal_id: str, status: ProposalStatus
) -> None:
    conn.execute(
        "UPDATE skill_proposals SET status = ? WHERE id = ?",
        (status.value, proposal_id),
    )
    conn.commit()


def list_proposals(
    conn: sqlite3.Connection, status: ProposalStatus | None = None
) -> list[SkillProposal]:
    if status is not None:
        cursor = conn.execute(
            "SELECT * FROM skill_proposals WHERE status = ? ORDER BY created_at",
            (status.value,),
        )
    else:
        cursor = conn.execute("SELECT * FROM skill_proposals ORDER BY created_at")
    return [SkillProposal(**_row_to_dict(row)) for row in cursor.fetchall()]


def insert_build(conn: sqlite3.Connection, build: Build) -> Build:
    conn.execute(
        """INSERT INTO builds
           (id, proposal_id, branch_name, started_at, finished_at, status, summary,
            attempt_number, parent_build_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            build.id, build.proposal_id, build.branch_name,
            build.started_at, build.finished_at, build.status.value,
            build.summary, build.attempt_number, build.parent_build_id,
        ),
    )
    conn.commit()
    return build


def update_build(
    conn: sqlite3.Connection, build_id: str, *,
    status: BuildStatus | None = None,
    finished_at: str | None = None,
    summary: str | None = None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []
    if status is not None:
        updates.append("status = ?")
        params.append(status.value)
    if finished_at is not None:
        updates.append("finished_at = ?")
        params.append(finished_at)
    if summary is not None:
        updates.append("summary = ?")
        params.append(summary)
    if not updates:
        return
    params.append(build_id)
    conn.execute(f"UPDATE builds SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def get_build(conn: sqlite3.Connection, build_id: str) -> Build | None:
    cursor = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return Build(**_row_to_dict(row))


def get_builds_for_proposal(conn: sqlite3.Connection, proposal_id: str) -> list[Build]:
    cursor = conn.execute(
        "SELECT * FROM builds WHERE proposal_id = ? ORDER BY started_at", (proposal_id,)
    )
    return [Build(**_row_to_dict(row)) for row in cursor.fetchall()]


def insert_verification(conn: sqlite3.Connection, v: Verification) -> Verification:
    conn.execute(
        """INSERT INTO verifications
           (id, proposal_id, status, ruff_ok, mypy_ok, pytest_ok, policy_ok,
            invariant_ok, report_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            v.id, v.proposal_id, v.status.value,
            int(v.ruff_ok), int(v.mypy_ok), int(v.pytest_ok), int(v.policy_ok),
            int(v.invariant_ok), v.report_path, v.created_at,
        ),
    )
    conn.commit()
    return v


def get_latest_verification(
    conn: sqlite3.Connection, proposal_id: str
) -> Verification | None:
    cursor = conn.execute(
        "SELECT * FROM verifications WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
        (proposal_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return Verification(**_row_to_dict(row))


def insert_promotion(conn: sqlite3.Connection, promo: Promotion) -> Promotion:
    conn.execute(
        """INSERT INTO promotions
           (id, proposal_id, from_status, to_status, approved_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            promo.id, promo.proposal_id, promo.from_status,
            promo.to_status, promo.approved_by, promo.created_at,
        ),
    )
    conn.commit()
    return promo


def insert_artifact(conn: sqlite3.Connection, artifact: Artifact) -> Artifact:
    conn.execute(
        """INSERT INTO artifacts
           (id, kind, path, sha256, created_at, related_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            artifact.id, artifact.kind.value, artifact.path,
            artifact.sha256, artifact.created_at, artifact.related_id,
        ),
    )
    conn.commit()
    return artifact


def get_artifacts_for_related(
    conn: sqlite3.Connection, related_id: str
) -> list[Artifact]:
    cursor = conn.execute(
        "SELECT * FROM artifacts WHERE related_id = ? ORDER BY created_at",
        (related_id,),
    )
    return [Artifact(**_row_to_dict(row)) for row in cursor.fetchall()]
