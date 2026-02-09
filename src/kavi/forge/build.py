"""Forge: build-skill — generate code via Claude Code."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kavi.artifacts.writer import write_build_packet
from kavi.forge.paths import skill_file_path, skill_test_path
from kavi.ledger.models import (
    Artifact,
    Build,
    BuildStatus,
    ProposalStatus,
    get_proposal,
    insert_build,
    update_build,
    update_proposal_status,
)


def _create_build_packet_content(
    name: str,
    description: str,
    io_schema: str,
    side_effect_class: str,
) -> str:
    """Generate the BUILD_PACKET.md content for Claude Code."""
    return f"""# Build Packet: {name}

## Task
Generate a Kavi skill implementation for "{name}".

## Skill Specification
- **Name**: {name}
- **Description**: {description}
- **Side Effect Class**: {side_effect_class}

## I/O Schema
```json
{io_schema}
```

## Requirements
1. Create `src/kavi/skills/{name}.py` implementing `BaseSkill`
2. The skill class must define: name, description, input_model, output_model, side_effect_class
3. Implement the `execute()` method
4. Use Pydantic models for input/output validation
5. Do NOT use any forbidden imports (subprocess, os.system, eval, exec)
6. Only write to allowed paths: ./vault_out/, ./artifacts_out/

## File Structure
```
src/kavi/skills/{name}.py  — skill implementation
tests/test_skill_{name}.py — unit tests
```
"""


def build_skill(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    output_dir: Path,
    branch_name: str | None = None,
) -> tuple[Build, Artifact]:
    """Start a skill build: create branch, write build packet, record build.

    The actual Claude Code invocation is handled externally.
    This function prepares the build packet and records the build in the ledger.
    """
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    if proposal.status != ProposalStatus.PROPOSED:
        raise ValueError(
            f"Proposal '{proposal_id}' has status {proposal.status}, expected PROPOSED"
        )

    if branch_name is None:
        branch_name = f"skill/{proposal.name}-{proposal.id[:8]}"

    # Create build record
    build = Build(
        proposal_id=proposal_id,
        branch_name=branch_name,
    )
    insert_build(conn, build)

    # Write build packet
    content = _create_build_packet_content(
        name=proposal.name,
        description=proposal.description,
        io_schema=proposal.io_schema_json,
        side_effect_class=proposal.side_effect_class.value,
    )
    artifact = write_build_packet(
        conn, content=content, proposal_id=proposal_id, output_dir=output_dir,
    )

    return build, artifact


def mark_build_succeeded(
    conn: sqlite3.Connection,
    build_id: str,
    summary: str = "Build completed",
) -> None:
    """Mark a build as succeeded and update proposal status to BUILT."""
    build = Build.model_validate(
        dict(conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone())
    )
    update_build(
        conn, build_id,
        status=BuildStatus.SUCCEEDED,
        summary=summary,
    )
    update_proposal_status(conn, build.proposal_id, ProposalStatus.BUILT)


def mark_build_failed(
    conn: sqlite3.Connection,
    build_id: str,
    summary: str = "Build failed",
) -> None:
    """Mark a build as failed."""
    update_build(
        conn, build_id,
        status=BuildStatus.FAILED,
        summary=summary,
    )


def detect_build_result(proposal_name: str, project_root: Path) -> bool:
    """Check if both skill file and test file exist at conventional paths.

    Returns True if both files are present, False otherwise.
    """
    skill = skill_file_path(proposal_name, project_root)
    test = skill_test_path(proposal_name, project_root)
    return skill.is_file() and test.is_file()
