"""Migration 060 (capture_health on users) round-trip: upgrade/downgrade on SQLite.

Mirrors the test_migration_059 harness: build a minimal pre-060 schema
(users without capture_health), stamp at 059, upgrade to 060 and assert
the new column; then downgrade and assert reversal.
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
    conn.execute(
        "CREATE TABLE users ("
        "  id VARCHAR(36) PRIMARY KEY,"
        "  email VARCHAR(255) NOT NULL UNIQUE,"
        "  display_name VARCHAR(255),"
        "  email_verified BOOLEAN NOT NULL DEFAULT 0,"
        "  tier VARCHAR(20) NOT NULL DEFAULT 'free',"
        "  stripe_customer_id VARCHAR(64),"
        "  stripe_subscription_id VARCHAR(64),"
        "  tier_updated_at TIMESTAMP,"
        "  storage_used_bytes BIGINT NOT NULL DEFAULT 0,"
        "  beta_pro_expires_at TIMESTAMP,"
        "  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),"
        "  is_active BOOLEAN NOT NULL DEFAULT 1,"
        "  sync_mode VARCHAR(20) NOT NULL DEFAULT 'off',"
        "  sync_debounce INTEGER NOT NULL DEFAULT 30,"
        "  audit_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',"
        "  summarize_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',"
        "  last_client_version VARCHAR(20),"
        "  last_client_platform VARCHAR(50),"
        "  last_client_device VARCHAR(100),"
        "  last_sync_at TIMESTAMP,"
        "  default_org_id VARCHAR(64),"
        "  entitlement_id INTEGER"
        ")"
    )
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('u1', 'test@example.com')"
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
    command.stamp(_cfg(db_path), "059")
    return db_path


class TestMigration060Upgrade:
    def test_upgrade_adds_capture_health_column(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "060")
        conn = sqlite3.connect(str(migration_060_db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info('users')")}
        assert "capture_health" in cols, "Expected column capture_health not found"
        conn.close()

    def test_existing_row_has_null_capture_health(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "060")
        conn = sqlite3.connect(str(migration_060_db_path))
        row = conn.execute(
            "SELECT capture_health FROM users WHERE id='u1'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # nullable — backfill not needed
        conn.close()

    def test_can_write_capture_health(self, migration_060_db_path):
        command.upgrade(_cfg(migration_060_db_path), "060")
        conn = sqlite3.connect(str(migration_060_db_path))
        conn.execute(
            "UPDATE users SET capture_health = "
            "'[{\"name\":\"claude-code\",\"health\":\"degraded\"}]' "
            "WHERE id='u1'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT capture_health FROM users WHERE id='u1'"
        ).fetchone()
        assert row is not None
        assert "degraded" in (row[0] or "")
        conn.close()


class TestMigration060Downgrade:
    def test_downgrade_removes_capture_health(self, migration_060_db_path):
        cfg = _cfg(migration_060_db_path)
        command.upgrade(cfg, "060")
        command.downgrade(cfg, "059")
        conn = sqlite3.connect(str(migration_060_db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info('users')")}
        assert "capture_health" not in cols, (
            "Column capture_health should have been removed"
        )
        conn.close()
