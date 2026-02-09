"""Artifact writer — markdown output + sha256 hashing."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from kavi.ledger.models import Artifact, ArtifactKind, insert_artifact


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def content_hash(content: str) -> str:
    """Compute sha256 hex digest of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_artifact(
    conn: sqlite3.Connection,
    *,
    content: str,
    path: Path,
    kind: ArtifactKind,
    related_id: str | None = None,
) -> Artifact:
    """Write content to disk and record in ledger. Returns the artifact record."""
    _ensure_dir(path)
    path.write_text(content, encoding="utf-8")

    sha = content_hash(content)
    artifact = Artifact(
        kind=kind,
        path=str(path),
        sha256=sha,
        related_id=related_id,
    )
    return insert_artifact(conn, artifact)


def write_skill_spec(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    io_schema: str,
    side_effect_class: str,
    required_secrets: str,
    proposal_id: str,
    output_dir: Path,
) -> Artifact:
    """Write a SKILL_SPEC markdown artifact for a proposal."""
    content = f"""# Skill Specification: {name}

## Description
{description}

## Side Effect Class
{side_effect_class}

## Required Secrets
{required_secrets}

## I/O Schema
```json
{io_schema}
```
"""
    path = output_dir / f"{name}_spec.md"
    return write_artifact(
        conn, content=content, path=path, kind=ArtifactKind.SKILL_SPEC,
        related_id=proposal_id,
    )


def write_verification_report(
    conn: sqlite3.Connection,
    *,
    content: str,
    proposal_id: str,
    output_dir: Path,
) -> Artifact:
    """Write a VERIFICATION_REPORT artifact."""
    path = output_dir / f"verification_{proposal_id}.md"
    return write_artifact(
        conn, content=content, path=path, kind=ArtifactKind.VERIFICATION_REPORT,
        related_id=proposal_id,
    )


def write_build_packet(
    conn: sqlite3.Connection,
    *,
    content: str,
    build_id: str,
    output_dir: Path,
    proposal_id: str | None = None,
) -> Artifact:
    """Write a BUILD_PACKET artifact, keyed by build_id (unique per attempt)."""
    path = output_dir / f"build_packet_{build_id}.md"
    return write_artifact(
        conn, content=content, path=path, kind=ArtifactKind.BUILD_PACKET,
        related_id=proposal_id,
    )


def write_note(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    path: Path,
    related_id: str | None = None,
) -> Artifact:
    """Write a markdown note (Obsidian-compatible) to the vault."""
    content = f"""---
title: {title}
---

{body}
"""
    return write_artifact(
        conn, content=content, path=path, kind=ArtifactKind.NOTE,
        related_id=related_id,
    )
