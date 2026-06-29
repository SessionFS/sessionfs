"""Migration 055 (OIDC SSO) round-trip: upgrade/downgrade + CHECK + partial-unique.

tk_c2cdbe7114804403 (SSO-P1). Mirrors the test_migration_054 harness:
direct SQLite upgrade()/downgrade() with a minimal pre-055 schema stamped
at 054, then upgrade to 055 and assert the four tables + api_keys.sso_minted
exist (with inline CHECK rejection of bad protocol/status), then downgrade
and assert they're gone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_055_db(db_path: Path) -> None:
    """Minimal prerequisite schema for the tables + FKs migration 055 references."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # organizations — referenced by org_identity_providers, org_domain_verifications,
    # and oidc_login_attempts.
    conn.execute(
        "CREATE TABLE organizations ("
        "  id VARCHAR(64) PRIMARY KEY"
        ")"
    )
    conn.execute("INSERT INTO organizations (id) VALUES ('org-1')")

    # users — referenced by org_identity_providers.created_by_user_id,
    # org_domain_verifications.verified_by_user_id, and external_identities.user_id.
    conn.execute(
        "CREATE TABLE users ("
        "  id VARCHAR(64) PRIMARY KEY"
        ")"
    )
    conn.execute("INSERT INTO users (id) VALUES ('user-1')")

    # api_keys — altered in-place to add sso_minted via batch_alter_table.
    # Minimal shape matching the current api_keys table (migration 042+).
    conn.execute(
        "CREATE TABLE api_keys ("
        "  id VARCHAR(36) PRIMARY KEY,"
        "  user_id VARCHAR(36) NOT NULL,"
        "  key_hash VARCHAR(64) NOT NULL,"
        "  created_at TEXT,"
        "  is_active INTEGER DEFAULT 1,"
        "  key_kind VARCHAR(20) DEFAULT 'user',"
        "  scopes TEXT DEFAULT '[\"*\"]'"
        ")"
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
def migration_055_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_055_test.db"
    _build_pre_055_db(db_path)
    command.stamp(_cfg(db_path), "054")
    return db_path


class TestMigration055:
    def test_upgrade_creates_tables_and_sso_minted_column(
        self, migration_055_db_path
    ):
        command.upgrade(_cfg(migration_055_db_path), "055")
        conn = sqlite3.connect(str(migration_055_db_path))

        # Four new tables.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "org_identity_providers" in tables
        assert "org_domain_verifications" in tables
        assert "external_identities" in tables
        assert "oidc_login_attempts" in tables

        # api_keys still present, now with sso_minted.
        assert "api_keys" in tables

        # org_identity_providers columns.
        idp_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('org_identity_providers')"
            ).fetchall()
        }
        for col in (
            "id", "org_id", "protocol", "display_name", "issuer",
            "client_id", "client_secret_ref", "allowed_scopes",
            "discovery_cache", "discovery_fetched_at",
            "jwks_cache", "jwks_fetched_at", "enabled", "enforced",
            "created_by_user_id", "created_at", "updated_at",
        ):
            assert col in idp_cols, f"Missing column: {col}"

        # org_domain_verifications columns.
        odv_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('org_domain_verifications')"
            ).fetchall()
        }
        for col in (
            "id", "org_id", "domain", "method", "verification_token",
            "status", "verified_at", "verified_by_user_id",
            "last_checked_at", "created_at",
        ):
            assert col in odv_cols, f"Missing column: {col}"

        # external_identities columns.
        eid_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('external_identities')"
            ).fetchall()
        }
        for col in (
            "id", "user_id", "org_idp_id", "provider_issuer", "subject",
            "email_at_link", "linked_at", "link_method", "last_login_at",
            "deactivated_at", "created_at",
        ):
            assert col in eid_cols, f"Missing column: {col}"

        # oidc_login_attempts columns.
        ola_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('oidc_login_attempts')"
            ).fetchall()
        }
        for col in (
            "id", "org_idp_id", "state", "nonce", "pkce_verifier_hash",
            "org_id", "provider_id", "redirect_after", "status",
            "expires_at", "consumed_at", "created_at",
        ):
            assert col in ola_cols, f"Missing column: {col}"

        # api_keys.sso_minted column.
        ak_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('api_keys')"
            ).fetchall()
        }
        assert "sso_minted" in ak_cols

        # Indexes exist.
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_org_idp_org" in indexes
        assert "uq_org_idp_one_enabled_per_org" in indexes
        assert "idx_org_domain_verification_org" in indexes
        assert "uq_org_domain_global_verified" in indexes
        assert "idx_external_identity_user" in indexes
        assert "idx_external_identity_org_idp" in indexes
        # uq_external_identity_issuer_sub is an inline UniqueConstraint
        # inside create_table — SQLite creates an autoindex for it; the
        # constraint IS enforced (verified by test_unique_issuer_sub).
        assert "idx_oidc_login_attempt_state" in indexes

        conn.close()

    def test_check_constraints_enforced(self, migration_055_db_path):
        command.upgrade(_cfg(migration_055_db_path), "055")
        conn = sqlite3.connect(str(migration_055_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Bad protocol → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_identity_providers "
                "(id, org_id, display_name, issuer, client_id, "
                " client_secret_ref, protocol) "
                "VALUES ('oidp-bad1', 'org-1', 'X', 'https://x', 'c', 'r', "
                "        'bad_protocol')"
            )
            conn.commit()
        conn.rollback()

        # Valid protocol → ok.
        conn.execute(
            "INSERT INTO org_identity_providers "
            "(id, org_id, display_name, issuer, client_id, client_secret_ref, protocol) "
            "VALUES ('oidp-ok', 'org-1', 'X', 'https://x', 'c', 'r', 'oidc')"
        )
        conn.commit()

        # Bad domain verification status → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_domain_verifications "
                "(id, org_id, domain, verification_token, status) "
                "VALUES ('odv-bad1', 'org-1', 'acme.com', 't', 'bad_status')"
            )
            conn.commit()
        conn.rollback()

        # Bad domain verification method → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_domain_verifications "
                "(id, org_id, domain, verification_token, method, status) "
                "VALUES ('odv-bad2', 'org-1', 'acme.com', 't', 'bad_method', 'pending')"
            )
            conn.commit()
        conn.rollback()

        # Valid domain verification → ok.
        conn.execute(
            "INSERT INTO org_domain_verifications "
            "(id, org_id, domain, verification_token, status) "
            "VALUES ('odv-ok', 'org-1', 'acme.com', 't', 'pending')"
        )
        conn.commit()

        # Bad link_method → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO external_identities "
                "(id, user_id, org_idp_id, provider_issuer, subject, "
                " email_at_link, link_method) "
                "VALUES ('eid-bad1', 'user-1', 'oidp-ok', 'https://x', 'sub1', "
                "        'e@x.com', 'bad_method')"
            )
            conn.commit()
        conn.rollback()

        # Bad login attempt status → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO oidc_login_attempts "
                "(id, state, nonce, pkce_verifier_hash, status, expires_at) "
                "VALUES ('ola-bad1', 's1', 'n1', 'h1', 'bad_status', "
                "        datetime('now', '+10 minutes'))"
            )
            conn.commit()
        conn.rollback()

        conn.close()

    def test_partial_unique_one_enabled_per_org(self, migration_055_db_path):
        """Two enabled IdPs for the same org → rejected by the partial-unique."""
        command.upgrade(_cfg(migration_055_db_path), "055")
        conn = sqlite3.connect(str(migration_055_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # First enabled IdP.
        conn.execute(
            "INSERT INTO org_identity_providers "
            "(id, org_id, display_name, issuer, client_id, client_secret_ref, "
            " protocol, enabled) "
            "VALUES ('oidp-e1', 'org-1', 'First', 'https://iss1', 'c1', 'r1', "
            "        'oidc', 1)"
        )
        conn.commit()

        # Second enabled IdP for the same org → UNIQUE violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_identity_providers "
                "(id, org_id, display_name, issuer, client_id, client_secret_ref, "
                " protocol, enabled) "
                "VALUES ('oidp-e2', 'org-1', 'Second', 'https://iss2', 'c2', 'r2', "
                "        'oidc', 1)"
            )
            conn.commit()
        conn.rollback()

        # Disabled IdP for same org → OK (partial unique only covers enabled=true).
        conn.execute(
            "INSERT INTO org_identity_providers "
            "(id, org_id, display_name, issuer, client_id, client_secret_ref, "
            " protocol, enabled) "
            "VALUES ('oidp-d1', 'org-1', 'Disabled', 'https://iss3', 'c3', 'r3', "
            "        'oidc', 0)"
        )
        conn.commit()

        conn.close()

    def test_partial_unique_verified_domain_global(self, migration_055_db_path):
        """Two orgs verifying the same domain → rejected."""
        command.upgrade(_cfg(migration_055_db_path), "055")
        conn = sqlite3.connect(str(migration_055_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Add a second org.
        conn.execute("INSERT INTO organizations (id) VALUES ('org-2')")

        # First org verifies the domain.
        conn.execute(
            "INSERT INTO org_domain_verifications "
            "(id, org_id, domain, verification_token, status) "
            "VALUES ('odv-v1', 'org-1', 'acme.com', 'tok1', 'verified')"
        )
        conn.commit()

        # Second org tries to verify the same domain → UNIQUE violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO org_domain_verifications "
                "(id, org_id, domain, verification_token, status) "
                "VALUES ('odv-v2', 'org-2', 'acme.com', 'tok2', 'verified')"
            )
            conn.commit()
        conn.rollback()

        # Same org can have a pending row for the same domain → OK
        # (partial unique only covers status='verified').
        conn.execute(
            "INSERT INTO org_domain_verifications "
            "(id, org_id, domain, verification_token, status) "
            "VALUES ('odv-p1', 'org-1', 'acme.com', 'tok3', 'pending')"
        )
        conn.commit()

        conn.close()

    def test_unique_issuer_sub(self, migration_055_db_path):
        """Duplicate (provider_issuer, subject) → rejected."""
        command.upgrade(_cfg(migration_055_db_path), "055")
        conn = sqlite3.connect(str(migration_055_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Need an IdP row for the FK.
        conn.execute(
            "INSERT INTO org_identity_providers "
            "(id, org_id, display_name, issuer, client_id, client_secret_ref) "
            "VALUES ('oidp-ik', 'org-1', 'Test', 'https://iss', 'c', 'r')"
        )
        conn.commit()

        # First identity.
        conn.execute(
            "INSERT INTO external_identities "
            "(id, user_id, org_idp_id, provider_issuer, subject, "
            " email_at_link, link_method) "
            "VALUES ('eid-1', 'user-1', 'oidp-ik', 'https://iss', 'sub1', "
            "        'e@x.com', 'jit_provision')"
        )
        conn.commit()

        # Same (issuer, subject) → UNIQUE violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO external_identities "
                "(id, user_id, org_idp_id, provider_issuer, subject, "
                " email_at_link, link_method) "
                "VALUES ('eid-2', 'user-1', 'oidp-ik', 'https://iss', 'sub1', "
                "        'e2@x.com', 'jit_provision')"
            )
            conn.commit()
        conn.rollback()

        conn.close()

    def test_downgrade_removes_tables_and_column(self, migration_055_db_path):
        cfg = _cfg(migration_055_db_path)
        command.upgrade(cfg, "055")
        command.downgrade(cfg, "054")
        conn = sqlite3.connect(str(migration_055_db_path))

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "org_identity_providers" not in tables
        assert "org_domain_verifications" not in tables
        assert "external_identities" not in tables
        assert "oidc_login_attempts" not in tables
        # api_keys still exists...
        assert "api_keys" in tables
        # ...but sso_minted column is gone.
        ak_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('api_keys')"
            ).fetchall()
        }
        assert "sso_minted" not in ak_cols

        conn.close()
