"""Migration 059 (telemetry funnel events) round-trip: upgrade/downgrade on SQLite.

Mirrors the test_migration_058 harness: build a minimal pre-059 schema
(telemetry_events without event/event_ts/tool), stamp at 058, upgrade to 059
and assert the three new columns; then downgrade and assert reversal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_059_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE telemetry_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  install_id VARCHAR(64) NOT NULL,"
        "  version VARCHAR(20) NOT NULL,"
        "  os VARCHAR(50) NOT NULL,"
        "  tools_active TEXT NOT NULL DEFAULT '[]',"
        "  sessions_captured_24h INTEGER NOT NULL DEFAULT 0,"
        "  avg_session_size_bytes BIGINT NOT NULL DEFAULT 0,"
        "  features_used TEXT NOT NULL DEFAULT '[]',"
        "  errors_24h INTEGER NOT NULL DEFAULT 0,"
        "  tier VARCHAR(20) NOT NULL DEFAULT 'free',"
        "  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO telemetry_events "
        "(install_id, version, os) "
        "VALUES ('abc123', '0.15.0', 'darwin')"
    )
    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture()
def migration_059_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "m059.db"
    _build_pre_059_db(db_path)
    command.stamp(_cfg(db_path), "058")
    return db_path


class TestMigration059Upgrade:
    def test_upgrade_adds_three_nullable_columns(self, migration_059_db_path):
        command.upgrade(_cfg(migration_059_db_path), "059")
        conn = sqlite3.connect(str(migration_059_db_path))
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info('telemetry_events')")}
        for col_name in ("event", "event_ts", "tool"):
            assert col_name in cols, f"Expected column {col_name} not found"
        conn.close()

    def test_existing_row_has_null_new_columns(self, migration_059_db_path):
        command.upgrade(_cfg(migration_059_db_path), "059")
        conn = sqlite3.connect(str(migration_059_db_path))
        row = conn.execute(
            "SELECT event, event_ts, tool FROM telemetry_events WHERE install_id='abc123'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # event
        assert row[1] is None  # event_ts
        assert row[2] is None  # tool
        conn.close()

    def test_can_insert_with_event_fields(self, migration_059_db_path):
        command.upgrade(_cfg(migration_059_db_path), "059")
        conn = sqlite3.connect(str(migration_059_db_path))
        conn.execute(
            "INSERT INTO telemetry_events "
            "(install_id, version, os, event, event_ts, tool) "
            "VALUES ('def456', '0.15.0', 'linux', 'heartbeat', "
            "'2026-08-11T12:00:00Z', 'claude-code')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT event, event_ts, tool FROM telemetry_events WHERE install_id='def456'"
        ).fetchone()
        assert row == ("heartbeat", "2026-08-11T12:00:00Z", "claude-code")
        conn.close()


class TestMigration059Downgrade:
    def test_downgrade_removes_three_columns(self, migration_059_db_path):
        cfg = _cfg(migration_059_db_path)
        command.upgrade(cfg, "059")
        command.downgrade(cfg, "058")
        conn = sqlite3.connect(str(migration_059_db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info('telemetry_events')")}
        for col_name in ("event", "event_ts", "tool"):
            assert col_name not in cols, f"Column {col_name} should have been removed"
        conn.close()
