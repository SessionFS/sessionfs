"""Migration 061 (handoff preview token) round-trip: upgrade/downgrade on SQLite.

Mirrors the test_migration_059 harness: build a minimal pre-060 schema
(handoffs without preview_token_hash), stamp at 059, upgrade to 060
and assert the new column; then downgrade and assert reversal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_060_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    # Minimal handoffs table without preview_token_hash (pre-060 shape).
    conn.execute(
        "CREATE TABLE handoffs ("
        "  id VARCHAR(20) PRIMARY KEY,"
        "  session_id VARCHAR(64) NOT NULL,"
        "  sender_id VARCHAR(36) NOT NULL,"
        "  recipient_email VARCHAR(255),"
        "  recipient_email_normalized VARCHAR(255),"
        "  recipient_id VARCHAR(36),"
        "  recipient_user_id VARCHAR(36),"
        "  recipient_team_id VARCHAR(64),"
        "  message TEXT,"
        "  status VARCHAR(20) DEFAULT 'pending',"
        "  created_at TIMESTAMP DEFAULT (datetime('now')),"
        "  claimed_at TIMESTAMP,"
        "  expires_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+7 days')),"
        "  recipient_session_id VARCHAR(64),"
        "  snapshot_title VARCHAR(500),"
        "  snapshot_tool VARCHAR(100),"
        "  snapshot_model_id VARCHAR(200),"
        "  snapshot_message_count INTEGER,"
        "  snapshot_total_tokens BIGINT,"
        "  ticket_id VARCHAR(64),"
        "  persona_name VARCHAR(50),"
        "  revoked_at TIMESTAMP,"
        "  revoked_by_user_id VARCHAR(36),"
        "  revoke_reason TEXT,"
        "  handoff_kind VARCHAR(20) DEFAULT 'individual',"
        "  viewed_at TIMESTAMP,"
        "  snapshot_persona_name VARCHAR(50),"
        "  snapshot_ticket_title VARCHAR(500),"
        "  sender_tier_snapshot VARCHAR(20)"
        ")"
    )
    conn.execute(
        "INSERT INTO handoffs (id, session_id, sender_id, expires_at) "
        "VALUES ('hnd_test1', 'ses_abc', 'user_1', '2026-09-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture()
def migration_060_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "m060.db"
    _build_pre_060_db(db_path)
    command.stamp(_cfg(db_path), "061")
    return db_path


class TestMigration060Upgrade:
    def test_upgrade_adds_preview_token_hash_column(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "061")
        conn = sqlite3.connect(str(migration_060_db_path))
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info('handoffs')")}
        assert "preview_token_hash" in cols, (
            "Expected column preview_token_hash not found"
        )
        conn.close()

    def test_existing_row_has_null_preview_token_hash(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "061")
        conn = sqlite3.connect(str(migration_060_db_path))
        row = conn.execute(
            "SELECT preview_token_hash FROM handoffs WHERE id='hnd_test1'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # existing rows stay NULL
        conn.close()

    def test_can_insert_with_preview_token_hash(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "061")
        conn = sqlite3.connect(str(migration_060_db_path))
        test_hash = "abc123def456"
        conn.execute(
            "INSERT INTO handoffs (id, session_id, sender_id, expires_at, "
            "preview_token_hash) "
            "VALUES ('hnd_test2', 'ses_def', 'user_2', '2026-09-01T00:00:00Z', ?)",
            (test_hash,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT preview_token_hash FROM handoffs WHERE id='hnd_test2'"
        ).fetchone()
        assert row == (test_hash,)
        conn.close()


class TestMigration060Downgrade:
    def test_downgrade_removes_preview_token_hash(self, migration_060_db_path):
        cfg = _cfg(migration_060_db_path)
        command.upgrade(cfg, "061")
        command.downgrade(cfg, "059")
        conn = sqlite3.connect(str(migration_060_db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info('handoffs')")}
        assert "preview_token_hash" not in cols, (
            "Column preview_token_hash should have been removed"
        )
        conn.close()
