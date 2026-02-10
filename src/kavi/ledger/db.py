"""Database connection and schema management."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_proposals (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    io_schema_json TEXT NOT NULL,
    side_effect_class TEXT NOT NULL CHECK (
        side_effect_class IN ('READ_ONLY', 'FILE_WRITE', 'NETWORK',
                              'SECRET_READ', 'MONEY', 'MESSAGING')
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
    summary TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    parent_build_id TEXT REFERENCES builds(id)
);

CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
    status TEXT NOT NULL CHECK (status IN ('FAILED', 'PASSED')),
    ruff_ok INTEGER NOT NULL DEFAULT 0,
    mypy_ok INTEGER NOT NULL DEFAULT 0,
    pytest_ok INTEGER NOT NULL DEFAULT 0,
    policy_ok INTEGER NOT NULL DEFAULT 0,
    invariant_ok INTEGER NOT NULL DEFAULT 0,
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
        kind IN ('SKILL_SPEC', 'PATCH_SUMMARY', 'VERIFICATION_REPORT',
                 'NOTE', 'BUILD_PACKET', 'BUILD_LOG', 'RESEARCH_NOTE')
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


MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE verifications ADD COLUMN invariant_ok INTEGER NOT NULL DEFAULT 0",
    ],
    3: [
        # Widen artifacts.kind CHECK to include BUILD_LOG.
        # SQLite cannot ALTER CHECK constraints, so recreate the table.
        """CREATE TABLE artifacts_new (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (
                kind IN ('SKILL_SPEC', 'PATCH_SUMMARY', 'VERIFICATION_REPORT',
                         'NOTE', 'BUILD_PACKET', 'BUILD_LOG')
            ),
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            related_id TEXT
        )""",
        "INSERT INTO artifacts_new SELECT * FROM artifacts",
        "DROP TABLE artifacts",
        "ALTER TABLE artifacts_new RENAME TO artifacts",
    ],
    4: [
        # Add attempt lineage to builds (D011)
        "ALTER TABLE builds ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE builds ADD COLUMN parent_build_id TEXT REFERENCES builds(id)",
        # Widen artifacts.kind CHECK to include RESEARCH_NOTE
        """CREATE TABLE artifacts_new (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (
                kind IN ('SKILL_SPEC', 'PATCH_SUMMARY', 'VERIFICATION_REPORT',
                         'NOTE', 'BUILD_PACKET', 'BUILD_LOG', 'RESEARCH_NOTE')
            ),
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            related_id TEXT
        )""",
        "INSERT INTO artifacts_new SELECT * FROM artifacts",
        "DROP TABLE artifacts",
        "ALTER TABLE artifacts_new RENAME TO artifacts",
    ],
    5: [
        # Widen side_effect_class CHECK to include SECRET_READ (D013)
        # SQLite cannot ALTER CHECK constraints, so recreate the table.
        """CREATE TABLE skill_proposals_new (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            io_schema_json TEXT NOT NULL,
            side_effect_class TEXT NOT NULL CHECK (
                side_effect_class IN ('READ_ONLY', 'FILE_WRITE', 'NETWORK',
                                      'SECRET_READ', 'MONEY', 'MESSAGING')
            ),
            required_secrets_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (
                status IN ('PROPOSED', 'REJECTED', 'BUILT', 'VERIFIED', 'TRUSTED')
            ),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )""",
        "INSERT INTO skill_proposals_new SELECT * FROM skill_proposals",
        "DROP TABLE skill_proposals",
        "ALTER TABLE skill_proposals_new RENAME TO skill_proposals",
    ],
}


def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return int(row["version"]) if row else 0


def _run_migrations(conn: sqlite3.Connection, current: int) -> None:
    # Temporarily disable FK checks — table recreates (e.g. migration 5)
    # drop referenced tables. PRAGMA must run outside a transaction.
    conn.execute("PRAGMA foreign_keys=OFF")
    for version in sorted(MIGRATIONS):
        if version > current:
            for sql in MIGRATIONS[version]:
                conn.execute(sql)
            conn.execute(
                "UPDATE schema_version SET version = ?", (version,),
            )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize database with schema. Idempotent."""
    conn = get_connection(db_path)

    # Check if schema is already initialized
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cursor.fetchone() is not None:
        current = _get_schema_version(conn)
        if current < SCHEMA_VERSION:
            _run_migrations(conn, current)
        return conn

    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn
