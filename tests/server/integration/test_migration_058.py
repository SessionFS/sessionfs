"""Migration 058 (SSO browser-login) round-trip: upgrade/downgrade on SQLite.

Mirrors the test_migration_057 harness: build a minimal pre-058 schema
(users + oidc_login_attempts), stamp at 057, upgrade to 058 and assert the
sso_exchange_codes table + oidc_login_attempts.client_flow column + the
expires_at index; then downgrade and assert reversal.

The PostgreSQL proof (dialect divergence + real prior schema) is the
`migrations-postgres` CI gate running `alembic upgrade head` on postgres:16 —
this SQLite round-trip is the fast local check only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_058_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    # users — referenced by sso_exchange_codes.user_id
    conn.execute("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)")
    conn.execute("INSERT INTO users (id) VALUES ('user-1')")
    # oidc_login_attempts — 058 ADDs client_flow to it (minimal prior shape).
    conn.execute(
        "CREATE TABLE oidc_login_attempts ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  state VARCHAR(128) NOT NULL,"
        "  nonce VARCHAR(128) NOT NULL,"
        "  pkce_verifier_hash VARCHAR(128) NOT NULL,"
        "  status VARCHAR(20) NOT NULL DEFAULT 'pending',"
        "  expires_at TIMESTAMP NOT NULL,"
        "  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO oidc_login_attempts "
        "(id, state, nonce, pkce_verifier_hash, expires_at) "
        "VALUES ('ola-1', 's', 'n', 'h', datetime('now', '+10 minutes'))"
    )
    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture()
def migration_058_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "m058.db"
    _build_pre_058_db(db_path)
    command.stamp(_cfg(db_path), "057")
    return db_path


class TestMigration058Upgrade:
    def test_upgrade_adds_client_flow_default_cli(self, migration_058_db_path):
        command.upgrade(_cfg(migration_058_db_path), "058")
        conn = sqlite3.connect(str(migration_058_db_path))
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info('oidc_login_attempts')")}
        assert "client_flow" in cols
        # Existing row backfilled to 'cli' by the server_default.
        val = conn.execute(
            "SELECT client_flow FROM oidc_login_attempts WHERE id='ola-1'"
        ).fetchone()[0]
        assert val == "cli"
        conn.close()

    def test_upgrade_creates_exchange_codes_table(self, migration_058_db_path):
        command.upgrade(_cfg(migration_058_db_path), "058")
        conn = sqlite3.connect(str(migration_058_db_path))
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "sso_exchange_codes" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info('sso_exchange_codes')")}
        assert {"code_hash", "binding_hash", "user_id", "org_id",
                "link_method", "status", "expires_at", "consumed_at"} <= cols
        idx = {
            r[1] for r in conn.execute("PRAGMA index_list('sso_exchange_codes')")
        }
        assert "ix_sso_exchange_codes_expires_at" in idx
        conn.close()

    def test_status_check_constraint_rejects_bad_value(self, migration_058_db_path):
        command.upgrade(_cfg(migration_058_db_path), "058")
        conn = sqlite3.connect(str(migration_058_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sso_exchange_codes "
                "(id, code_hash, binding_hash, user_id, link_method, status, expires_at, created_at) "
                "VALUES ('x', 'ch', 'bh', 'user-1', 'jit_provision', 'BOGUS', "
                "datetime('now'), datetime('now'))"
            )
        conn.close()


class TestMigration058Downgrade:
    def test_downgrade_reverses_all_changes(self, migration_058_db_path):
        cfg = _cfg(migration_058_db_path)
        command.upgrade(cfg, "058")
        command.downgrade(cfg, "057")
        conn = sqlite3.connect(str(migration_058_db_path))
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "sso_exchange_codes" not in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info('oidc_login_attempts')")}
        assert "client_flow" not in cols
        conn.close()
