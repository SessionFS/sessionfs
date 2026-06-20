"""Migration 051 upgrade/downgrade round-trip on SQLite.

Mirrors the migration 050 test harness (test_entitlements_p1.py):
create prerequisite tables with raw sqlite3, stamp, upgrade, verify,
downgrade. The broken migration chain (003 PG-only GIN) is bypassed
entirely — stamp+upgrade only runs 050→051.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture
def migration_051_db_path(tmp_path: Path) -> Path:
    """Create a SQLite DB with the full pre-051 schema (through 050).

    Creates all prerequisite tables that migrations 001–050 expect,
    seeds representative data, stamps at 049, and runs 050.
    The returned DB is ready for 051 upgrade/downgrade tests.
    """
    db_path = tmp_path / "migration_051_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # ── Pre-050 tables (schema these migrations will alter) ──

    conn.execute("""
        CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY,
            email VARCHAR(255) NOT NULL UNIQUE,
            display_name VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            email_verified BOOLEAN NOT NULL DEFAULT 0,
            tier VARCHAR(20) NOT NULL DEFAULT 'free',
            is_active BOOLEAN NOT NULL DEFAULT 1,
            stripe_customer_id VARCHAR(64),
            stripe_subscription_id VARCHAR(64),
            tier_updated_at TIMESTAMP,
            storage_used_bytes BIGINT NOT NULL DEFAULT 0,
            beta_pro_expires_at TIMESTAMP,
            last_client_version VARCHAR(20),
            last_client_platform VARCHAR(50),
            last_client_device VARCHAR(100),
            last_sync_at TIMESTAMP,
            sync_mode VARCHAR(20) NOT NULL DEFAULT 'off',
            sync_debounce INTEGER NOT NULL DEFAULT 30,
            audit_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
            summarize_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
            default_org_id VARCHAR(64)
        )
    """)

    conn.execute("""
        CREATE TABLE organizations (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            slug VARCHAR(100) NOT NULL UNIQUE,
            tier VARCHAR(20) NOT NULL DEFAULT 'team',
            stripe_customer_id VARCHAR(64),
            stripe_subscription_id VARCHAR(64),
            storage_limit_bytes BIGINT NOT NULL DEFAULT 0,
            storage_used_bytes BIGINT NOT NULL DEFAULT 0,
            seats_limit INTEGER NOT NULL DEFAULT 5,
            settings TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE org_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            invited_by VARCHAR(36) REFERENCES users(id),
            invited_at TIMESTAMP,
            joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, user_id)
        )
    """)

    conn.execute("""
        CREATE TABLE helm_licenses (
            id VARCHAR(64) PRIMARY KEY,
            org_name VARCHAR(255) NOT NULL,
            contact_email VARCHAR(255) NOT NULL,
            tier VARCHAR(20) NOT NULL DEFAULT 'enterprise',
            seats_limit INTEGER DEFAULT 25,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            expires_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            license_type VARCHAR(20) NOT NULL DEFAULT 'paid',
            cluster_id VARCHAR(128),
            last_validated_at TIMESTAMP,
            validation_count INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE admin_actions (
            id VARCHAR(36) PRIMARY KEY,
            admin_id VARCHAR(36) NOT NULL REFERENCES users(id),
            action VARCHAR(50) NOT NULL,
            target_type VARCHAR(20) NOT NULL,
            target_id VARCHAR(64) NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── Seed data ──
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-1', 'alice@test.com', 'free')"
    )
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-2', 'bob@test.com', 'free')"
    )
    conn.execute(
        "INSERT INTO organizations (id, name, slug, tier, seats_limit) "
        "VALUES ('org-1', 'Test Org', 'test-org', 'team', 10)"
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) "
        "VALUES ('org-1', 'user-1', 'admin', '2025-01-01')"
    )
    conn.execute(
        "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id) "
        "VALUES ('aa-1', 'user-1', 'admin_create_org', 'organization', 'org-1')"
    )

    conn.commit()
    conn.close()

    # Run migrations 050 so the DB is at the 050 head.
    cfg = Config()
    cfg.set_main_option(
        "script_location", "src/sessionfs/server/db/migrations"
    )
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
    )
    command.stamp(cfg, "049")
    command.upgrade(cfg, "050")

    return db_path


class TestMigration051:
    """Migration 051 upgrade + downgrade on SQLite."""

    @staticmethod
    def _alembic_cfg(db_path):
        cfg = Config()
        cfg.set_main_option(
            "script_location", "src/sessionfs/server/db/migrations"
        )
        cfg.set_main_option(
            "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
        )
        return cfg

    def test_upgrade_creates_table_and_indexes(self, migration_051_db_path):
        """Upgrade to 051: org_owner_transfer table + indexes exist."""
        cfg = self._alembic_cfg(migration_051_db_path)
        command.upgrade(cfg, "051")

        conn = sqlite3.connect(str(migration_051_db_path))

        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "org_owner_transfer" in tables

        cols = {
            row[1]: row[2] for row in
            conn.execute("PRAGMA table_info('org_owner_transfer')").fetchall()
        }
        for col in ("id", "org_id", "from_user_id", "to_user_id",
                     "status", "created_at", "expires_at", "accepted_at"):
            assert col in cols, f"Missing column: {col}"

        indexes = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='org_owner_transfer'"
            ).fetchall()
        ]
        assert "idx_org_owner_transfer_org" in indexes
        assert "uq_org_owner_transfer_one_pending" in indexes

        conn.close()

    def test_partial_unique_index_enforces_one_pending(
        self, migration_051_db_path
    ):
        """Partial unique index: only one pending transfer per org."""
        cfg = self._alembic_cfg(migration_051_db_path)
        command.upgrade(cfg, "051")

        conn = sqlite3.connect(str(migration_051_db_path))

        # Insert first pending — ok.
        conn.execute(
            "INSERT INTO org_owner_transfer "
            "(org_id, from_user_id, to_user_id, status) "
            "VALUES ('org-1', 'user-1', 'user-2', 'pending')"
        )
        conn.commit()

        # Second pending for same org — blocked.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_owner_transfer "
                "(org_id, from_user_id, to_user_id, status) "
                "VALUES ('org-1', 'user-2', 'user-1', 'pending')"
            )
            conn.commit()

        # Accepted for same org — ok (not pending).
        conn.execute(
            "INSERT INTO org_owner_transfer "
            "(org_id, from_user_id, to_user_id, status) "
            "VALUES ('org-1', 'user-1', 'user-2', 'accepted')"
        )
        conn.commit()

        conn.close()

    def test_downgrade_removes_table(self, migration_051_db_path):
        """Downgrade from 051 drops the table."""
        cfg = self._alembic_cfg(migration_051_db_path)
        command.upgrade(cfg, "051")

        conn = sqlite3.connect(str(migration_051_db_path))
        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "org_owner_transfer" in tables
        conn.close()

        command.downgrade(cfg, "050")

        conn = sqlite3.connect(str(migration_051_db_path))
        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "org_owner_transfer" not in tables
        conn.close()
