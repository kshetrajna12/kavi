"""Forge: promote-skill — elevate verified skill to TRUSTED."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from kavi.forge.paths import skill_file_path, skill_module_path
from kavi.ledger.models import (
    Promotion,
    ProposalStatus,
    get_latest_verification,
    get_proposal,
    insert_promotion,
    update_proposal_status,
)
from kavi.skills.loader import load_registry, save_registry


def _compute_skill_hash(path: Path) -> str:
    """Compute sha256 of the skill source file."""
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()


def promote_skill(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    project_root: Path,
    registry_path: Path,
    approved_by: str = "kshetrajna",
    version: str = "1.0.0",
) -> Promotion:
    """Promote a verified skill to TRUSTED.

    Skill file and module path are derived from the proposal name
    using convention-based paths.

    Requirements:
    - Proposal must have status VERIFIED
    - Latest verification must be PASSED
    """
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    if proposal.status != ProposalStatus.VERIFIED:
        raise ValueError(
            f"Proposal '{proposal_id}' has status {proposal.status}, expected VERIFIED"
        )

    verification = get_latest_verification(conn, proposal_id)
    if verification is None or verification.status.value != "PASSED":
        raise ValueError(f"No passing verification found for proposal '{proposal_id}'")

    # Derive paths from proposal name
    skill_file = skill_file_path(proposal.name, project_root)
    module_path = skill_module_path(proposal.name)

    # Compute hash of skill file
    skill_hash = _compute_skill_hash(skill_file)

    # Update registry.yaml
    skills = load_registry(registry_path)
    # Remove any existing entry with same name
    skills = [s for s in skills if s["name"] != proposal.name]
    mod_base = module_path.rsplit(".", 1)[0]
    class_stem = proposal.name.title().replace("_", "")
    skills.append({
        "name": proposal.name,
        "module_path": module_path,
        "description": proposal.description,
        "input_model": f"{mod_base}.{class_stem}Input",
        "output_model": f"{mod_base}.{class_stem}Output",
        "side_effect_class": proposal.side_effect_class.value,
        "required_secrets": [],
        "version": version,
        "hash": skill_hash,
    })
    save_registry(registry_path, skills)

    # Update proposal status
    update_proposal_status(conn, proposal_id, ProposalStatus.TRUSTED)

    # Record promotion
    promotion = Promotion(
        proposal_id=proposal_id,
        from_status=ProposalStatus.VERIFIED.value,
        to_status=ProposalStatus.TRUSTED.value,
        approved_by=approved_by,
    )
    insert_promotion(conn, promotion)

    return promotion
