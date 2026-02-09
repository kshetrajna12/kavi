"""Forge: propose-skill — create a skill proposal."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kavi.artifacts.writer import write_skill_spec
from kavi.ledger.models import (
    Artifact,
    SideEffectClass,
    SkillProposal,
    insert_proposal,
)


def propose_skill(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    io_schema_json: str,
    side_effect_class: SideEffectClass,
    required_secrets: list[str] | None = None,
    output_dir: Path,
) -> tuple[SkillProposal, Artifact]:
    """Create a skill proposal and write the SPEC artifact.

    Returns the proposal and the spec artifact.
    """
    secrets = required_secrets or []
    secrets_json = json.dumps(secrets)

    # Validate io_schema is valid JSON
    json.loads(io_schema_json)

    proposal = SkillProposal(
        name=name,
        description=description,
        io_schema_json=io_schema_json,
        side_effect_class=side_effect_class,
        required_secrets_json=secrets_json,
    )
    insert_proposal(conn, proposal)

    artifact = write_skill_spec(
        conn,
        name=name,
        description=description,
        io_schema=io_schema_json,
        side_effect_class=side_effect_class.value,
        required_secrets=secrets_json,
        proposal_id=proposal.id,
        output_dir=output_dir,
    )

    return proposal, artifact
