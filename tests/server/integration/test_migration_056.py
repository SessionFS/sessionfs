"""Migration 056 (SSO-P1-fix) round-trip: upgrade/downgrade + uniqueness + partial-unique.

tk_cb774646864f414b. Mirrors the test_migration_055 harness:
direct SQLite upgrade()/downgrade() with a minimal pre-056 schema stamped
at 055, then upgrade to 056 and assert the re-keyed external_identities
index + sso_break_glass_grants + email normalization; then downgrade and
assert reversal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_056_db(db_path: Path) -> None:
    """Minimal prerequisite schema for the tables migration 056 references."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # organizations — referenced by external_identities.org_idp_id FK chain
    # (via org_identity_providers), sso_break_glass_grants.org_id,
    # and org_members.org_id.
    conn.execute(
        "CREATE TABLE organizations ("
        "  id VARCHAR(64) PRIMARY KEY"
        ")"
    )
    conn.execute("INSERT INTO organizations (id) VALUES ('org-1')")

    # users — referenced by external_identities.user_id,
    # sso_break_glass_grants.admin_user_id/issued_by_user_id,
    # org_members.user_id, and the email normalize step.
    conn.execute(
        "CREATE TABLE users ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  email VARCHAR(255) NOT NULL"
        ")"
    )
    conn.execute("INSERT INTO users (id, email) VALUES ('user-1', 'User@Acme.COM')")
    conn.execute("INSERT INTO users (id, email) VALUES ('user-2', 'admin@acme.com')")
    conn.execute("INSERT INTO users (id, email) VALUES ('user-3', 'already_lower@acme.com')")

    # org_identity_providers — FK target of external_identities.org_idp_id.
    conn.execute(
        "CREATE TABLE org_identity_providers ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  org_id VARCHAR(64) NOT NULL,"
        "  display_name VARCHAR(100) NOT NULL,"
        "  issuer VARCHAR(500) NOT NULL,"
        "  client_id VARCHAR(255) NOT NULL,"
        "  client_secret_ref VARCHAR(255) NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO org_identity_providers "
        "(id, org_id, display_name, issuer, client_id, client_secret_ref) "
        "VALUES ('oidp-1', 'org-1', 'Test IdP', 'https://iss.example.com', 'c1', 'r1')"
    )

    # external_identities — the table we're altering.
    # Create with the 055 shape (uq_external_identity_issuer_sub unique
    # on provider_issuer+subject).
    conn.execute(
        "CREATE TABLE external_identities ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  user_id VARCHAR(64) NOT NULL,"
        "  org_idp_id VARCHAR(64) NOT NULL,"
        "  provider_issuer VARCHAR(500) NOT NULL,"
        "  subject VARCHAR(255) NOT NULL,"
        "  email_at_link VARCHAR(255) NOT NULL,"
        "  linked_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  link_method VARCHAR(30) NOT NULL DEFAULT 'jit_provision',"
        "  last_login_at TEXT,"
        "  deactivated_at TEXT,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    # On SQLite, Alembic renders named UniqueConstraint inside
    # op.create_table as a standalone CREATE UNIQUE INDEX — mirror that.
    conn.execute(
        "CREATE UNIQUE INDEX uq_external_identity_issuer_sub "
        "ON external_identities (provider_issuer, subject)"
    )

    # org_members — add the table so we can create the unique constraint.
    conn.execute(
        "CREATE TABLE org_members ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  org_id VARCHAR(64) NOT NULL,"
        "  user_id VARCHAR(36) NOT NULL,"
        "  role VARCHAR(20) NOT NULL DEFAULT 'member',"
        "  joined_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) "
        "VALUES ('org-1', 'user-1', 'member')"
    )

    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location", "src/sessionfs/server/db/migrations"
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def migration_056_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_056_test.db"
    _build_pre_056_db(db_path)
    command.stamp(_cfg(db_path), "055")
    return db_path


class TestMigration056:
    # ── upgrade assertions ──────────────────────────────────────────

    def test_upgrade_creates_new_indexes_and_table(self, migration_056_db_path):
        command.upgrade(_cfg(migration_056_db_path), "056")
        conn = sqlite3.connect(str(migration_056_db_path))

        # New table exists.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sso_break_glass_grants" in tables
        assert "external_identities" in tables
        assert "org_members" in tables

        # sso_break_glass_grants columns.
        sbg_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('sso_break_glass_grants')"
            ).fetchall()
        }
        for col in (
            "id", "org_id", "admin_user_id", "issued_by_user_id",
            "expires_at", "revoked_at", "created_at",
        ):
            assert col in sbg_cols, f"Missing column: {col}"

        # Indexes.
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        # New unique index on external_identities.
        assert "uq_external_identity_idp_sub" in indexes
        # Old unique index is GONE.
        assert "uq_external_identity_issuer_sub" not in indexes
        # Org member uniqueness.
        assert "uq_org_members_org_user" in indexes
        # Break-glass indexes.
        assert "idx_sbg_org" in indexes
        assert "uq_sbg_one_active_per_admin" in indexes

        conn.close()

    def test_upgrade_normalizes_emails(self, migration_056_db_path):
        command.upgrade(_cfg(migration_056_db_path), "056")
        conn = sqlite3.connect(str(migration_056_db_path))

        emails = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT id, email FROM users"
            ).fetchall()
        }
        # Mixed-case email normalized.
        assert emails["user-1"] == "user@acme.com"
        assert emails["user-2"] == "admin@acme.com"
        # Already-lower email unchanged.
        assert emails["user-3"] == "already_lower@acme.com"

        conn.close()

    # ── uniqueness enforcement ──────────────────────────────────────

    def test_external_identity_unique_on_org_idp_sub(
        self, migration_056_db_path
    ):
        """Duplicate (org_idp_id, subject) → rejected."""
        command.upgrade(_cfg(migration_056_db_path), "056")
        conn = sqlite3.connect(str(migration_056_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Add a second IdP row so we can test cross-IdP non-collision.
        conn.execute(
            "INSERT INTO org_identity_providers "
            "(id, org_id, display_name, issuer, client_id, client_secret_ref) "
            "VALUES ('oidp-2', 'org-1', 'Second IdP', 'https://iss2.example.com', 'c2', 'r2')"
        )
        conn.commit()

        # First identity.
        conn.execute(
            "INSERT INTO external_identities "
            "(id, user_id, org_idp_id, provider_issuer, subject, "
            " email_at_link, link_method) "
            "VALUES ('eid-1', 'user-1', 'oidp-1', 'https://iss.example.com', 'sub1', "
            "        'e@x.com', 'jit_provision')"
        )
        conn.commit()

        # Same (org_idp_id, subject) → UNIQUE violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO external_identities "
                "(id, user_id, org_idp_id, provider_issuer, subject, "
                " email_at_link, link_method) "
                "VALUES ('eid-dup', 'user-2', 'oidp-1', 'https://iss.example.com', 'sub1', "
                "        'e2@x.com', 'jit_provision')"
            )
            conn.commit()
        conn.rollback()

        # Same subject, DIFFERENT org_idp_id → OK (different IdP).
        conn.execute(
            "INSERT INTO external_identities "
            "(id, user_id, org_idp_id, provider_issuer, subject, "
            " email_at_link, link_method) "
            "VALUES ('eid-2', 'user-1', 'oidp-2', 'https://iss2.example.com', 'sub1', "
            "        'e@x.com', 'jit_provision')"
        )
        conn.commit()

        conn.close()

    def test_org_member_unique_org_user(self, migration_056_db_path):
        """Duplicate (org_id, user_id) → rejected."""
        command.upgrade(_cfg(migration_056_db_path), "056")
        conn = sqlite3.connect(str(migration_056_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # First membership exists from _build_pre_056_db (user-1, org-1).

        # Same (org_id, user_id) → UNIQUE violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES ('org-1', 'user-1', 'admin')"
            )
            conn.commit()
        conn.rollback()

        # Different user, same org → OK.
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES ('org-1', 'user-2', 'member')"
        )
        conn.commit()

        # Same user, different org → OK (we need a second org).
        conn.execute("INSERT INTO organizations (id) VALUES ('org-2')")
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES ('org-2', 'user-1', 'member')"
        )
        conn.commit()

        conn.close()

    def test_break_glass_one_active_per_admin(self, migration_056_db_path):
        """Partial-unique: two active (non-revoked) grants for the same admin → rejected."""
        command.upgrade(_cfg(migration_056_db_path), "056")
        conn = sqlite3.connect(str(migration_056_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # First active grant.
        conn.execute(
            "INSERT INTO sso_break_glass_grants "
            "(id, org_id, admin_user_id, issued_by_user_id, expires_at) "
            "VALUES ('sbg-1', 'org-1', 'user-1', 'user-2', "
            "        datetime('now', '+1 hour'))"
        )
        conn.commit()

        # Second active grant for the same (org, admin) → UNIQUE violation
        # (partial unique WHERE revoked_at IS NULL).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sso_break_glass_grants "
                "(id, org_id, admin_user_id, issued_by_user_id, expires_at) "
                "VALUES ('sbg-2', 'org-1', 'user-1', 'user-2', "
                "        datetime('now', '+2 hours'))"
            )
            conn.commit()
        conn.rollback()

        # Revoke the first grant → now a new active grant should be allowed.
        conn.execute(
            "UPDATE sso_break_glass_grants SET revoked_at = datetime('now') "
            "WHERE id = 'sbg-1'"
        )
        conn.commit()

        conn.execute(
            "INSERT INTO sso_break_glass_grants "
            "(id, org_id, admin_user_id, issued_by_user_id, expires_at) "
            "VALUES ('sbg-3', 'org-1', 'user-1', 'user-2', "
            "        datetime('now', '+1 hour'))"
        )
        conn.commit()

        # Different admin, same org → OK.
        conn.execute(
            "INSERT INTO sso_break_glass_grants "
            "(id, org_id, admin_user_id, issued_by_user_id, expires_at) "
            "VALUES ('sbg-4', 'org-1', 'user-2', 'user-1', "
            "        datetime('now', '+1 hour'))"
        )
        conn.commit()

        conn.close()

    def test_upgrade_dedupes_org_members_before_unique_index(self, tmp_path):
        """Pre-existing duplicate (org_id, user_id) rows are deduped in the
        same migration so the unique-index creation can't abort the
        migrate-job. Survivor = highest role priority, then earliest
        joined_at, then lowest id."""
        db_path = tmp_path / "migration_056_dupe.db"
        _build_pre_056_db(db_path)

        # Inject duplicates BEFORE stamping/upgrading.
        # Group A (org-1, user-1): member already seeded by _build_pre_056_db
        # (id=1). Add an owner (later joined_at) + an admin → owner must win
        # despite being newest, because role priority dominates joined_at.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role, joined_at) "
            "VALUES ('org-1', 'user-1', 'admin', '2024-01-02 00:00:00')"
        )
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role, joined_at) "
            "VALUES ('org-1', 'user-1', 'owner', '2024-06-01 00:00:00')"
        )
        # Group B (org-1, user-2): two members, different joined_at →
        # earliest joined_at survives.
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role, joined_at) "
            "VALUES ('org-1', 'user-2', 'member', '2024-03-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role, joined_at) "
            "VALUES ('org-1', 'user-2', 'member', '2024-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        command.stamp(_cfg(db_path), "055")
        command.upgrade(_cfg(db_path), "056")

        conn = sqlite3.connect(str(db_path))
        # Group A: exactly one survivor, and it's the owner.
        rows_a = conn.execute(
            "SELECT role FROM org_members "
            "WHERE org_id='org-1' AND user_id='user-1'"
        ).fetchall()
        assert len(rows_a) == 1
        assert rows_a[0][0] == "owner"

        # Group B: exactly one survivor, the earliest joined_at.
        rows_b = conn.execute(
            "SELECT joined_at FROM org_members "
            "WHERE org_id='org-1' AND user_id='user-2'"
        ).fetchall()
        assert len(rows_b) == 1
        assert rows_b[0][0] == "2024-01-01 00:00:00"

        # The unique index exists (creation did not abort).
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "uq_org_members_org_user" in indexes
        conn.close()

    # ── downgrade ───────────────────────────────────────────────────

    def test_downgrade_reverses_all_changes(self, migration_056_db_path):
        cfg = _cfg(migration_056_db_path)
        command.upgrade(cfg, "056")
        command.downgrade(cfg, "055")
        conn = sqlite3.connect(str(migration_056_db_path))

        # sso_break_glass_grants table is gone.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sso_break_glass_grants" not in tables

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        # New indexes are gone.
        assert "uq_external_identity_idp_sub" not in indexes
        assert "uq_org_members_org_user" not in indexes
        assert "idx_sbg_org" not in indexes
        assert "uq_sbg_one_active_per_admin" not in indexes

        # Old unique index is BACK.
        assert "uq_external_identity_issuer_sub" in indexes

        conn.close()
