"""Database connection and schema management."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    io_schema_json TEXT NOT NULL,
    side_effect_class TEXT NOT NULL CHECK (
        side_effect_class IN ('READ_ONLY', 'FILE_WRITE', 'NETWORK', 'MONEY', 'MESSAGING')
    ),
    required_secrets_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (
        status IN ('PROPOSED', 'REJECTED', 'BUILT', 'VERIFIED', 'TRUSTED')
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
    branch_name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'STARTED' CHECK (
        status IN ('STARTED', 'FAILED', 'SUCCEEDED')
    ),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
    status TEXT NOT NULL CHECK (status IN ('FAILED', 'PASSED')),
    ruff_ok INTEGER NOT NULL DEFAULT 0,
    mypy_ok INTEGER NOT NULL DEFAULT 0,
    pytest_ok INTEGER NOT NULL DEFAULT 0,
    policy_ok INTEGER NOT NULL DEFAULT 0,
    report_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS promotions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('SKILL_SPEC', 'PATCH_SUMMARY', 'VERIFICATION_REPORT', 'NOTE', 'BUILD_PACKET')
    ),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    related_id TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize database with schema. Idempotent."""
    conn = get_connection(db_path)

    # Check if schema is already initialized
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cursor.fetchone() is not None:
        return conn

    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn
