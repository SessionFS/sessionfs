"""Migration 088: chunked, resumable session uploads (GitHub #91).

Adds the ``session_uploads`` and ``session_upload_parts`` tables plus the two
pending-upload indexes. Mirrors the 051/053 harness: build a minimal
prerequisite schema with raw sqlite3, stamp at 083, and run only 083→088
(the pre-003 PG-only chain stays bypassed).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3]
    / "src"
    / "sessionfs"
    / "server"
    / "db"
    / "migrations"
)


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def migration_088_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_088_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    # Only prerequisite 088 needs: the users table its FK points at.
    conn.execute(
        "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) UNIQUE)"
    )
    conn.commit()
    conn.close()

    command.stamp(_cfg(db_path), "087")
    return db_path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_upgrade_creates_upload_tables_and_indexes(migration_088_db_path: Path) -> None:
    command.upgrade(_cfg(migration_088_db_path), "088")
    conn = sqlite3.connect(str(migration_088_db_path))

    tables = _table_names(conn)
    assert "session_uploads" in tables
    assert "session_upload_parts" in tables

    upload_cols = {
        r[1] for r in conn.execute("PRAGMA table_info('session_uploads')").fetchall()
    }
    for col in (
        "id",
        "user_id",
        "session_id",
        "total_size",
        "sha256",
        "part_size",
        "part_count",
        "status",
        "completing_since",
        "completion_token",
        "result_etag",
        "result_size",
        "result_synced_at",
        "result_created",
        "created_at",
        "expires_at",
    ):
        assert col in upload_cols, f"session_uploads missing column: {col}"
    assert "if_match" not in upload_cols, "if_match was dropped; complete takes If-Match from the request"

    part_cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info('session_upload_parts')").fetchall()
    }
    for col in ("upload_id", "part_number", "size", "sha256"):
        assert col in part_cols, f"session_upload_parts missing column: {col}"

    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='session_uploads'"
        ).fetchall()
    }
    assert "idx_session_uploads_user_status" in indexes
    assert "idx_session_uploads_status_expires" in indexes

    # The child FK targets the parent; the parent defaults status to pending.
    assert conn.execute(
        "SELECT dflt_value FROM pragma_table_info('session_uploads') WHERE name='status'"
    ).fetchone()[0] == "'pending'"
    conn.close()


def test_downgrade_drops_upload_tables(migration_088_db_path: Path) -> None:
    cfg = _cfg(migration_088_db_path)
    command.upgrade(cfg, "088")
    command.downgrade(cfg, "087")

    conn = sqlite3.connect(str(migration_088_db_path))
    tables = _table_names(conn)
    assert "session_upload_parts" not in tables
    assert "session_uploads" not in tables
    conn.close()
