"""Migration 071 (marketplace publishers) round-trip.

The prior schema is the REAL SQLAlchemy metadata minus what 071 adds: five
tables, skill_packages.publisher_id, and the listing moderation columns
(with their CHECK and index). The PostgreSQL leg runs when SFS_TEST_PG_URL
is set (and in the `migrations-postgres` CI gate).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

MIGRATION_071_TABLES = {
    "skill_publishers",
    "skill_publisher_verifications",
    "skill_reserved_names",
    "skill_trusted_publishers",
    "skill_publish_tokens",
    "skill_github_installation_states",
}
_DROPPED = {
    "skill_packages": {"publisher_id"},
    "skill_marketplace_listings": {
        "moderation_state",
        "moderation_reason",
        "reviewed_by_user_id",
        "reviewed_at",
        "signature_metadata",
    },
}
_DROPPED_CONSTRAINTS = {"ck_skill_listing_moderation_state"}
_DROPPED_INDEXES = {"idx_skill_listing_moderation"}

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _prior_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name in MIGRATION_071_TABLES:
            continue
        copy = table.to_metadata(metadata)
        for column_name in _DROPPED.get(name, set()):
            column = copy.c[column_name]
            for constraint in list(copy.constraints):
                if isinstance(constraint, sa.ForeignKeyConstraint) and column_name in constraint.column_keys:
                    copy.constraints.discard(constraint)
            for foreign_key in list(copy.foreign_keys):
                if foreign_key.parent is column:
                    copy.foreign_keys.discard(foreign_key)
            copy._columns.remove(column)  # noqa: SLF001 - test-only schema rewind
        for constraint in list(copy.constraints):
            if constraint.name in _DROPPED_CONSTRAINTS:
                copy.constraints.discard(constraint)
        for index in list(copy.indexes):
            if index.name in _DROPPED_INDEXES:
                copy.indexes.discard(index)
    return metadata


@pytest.fixture()
def pre_071_db(tmp_path: Path) -> Path:
    path = tmp_path / "m071.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    engine.dispose()
    # The prior schema is the current models minus 071, i.e. the 070 shape.
    command.stamp(_cfg(path), "070")
    return path


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def _seed(connection: sqlite3.Connection) -> None:
    for user_id, tier in (("u1", "pro"), ("adm", "admin")):
        connection.execute(
            "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
            "VALUES (?, ?, ?, '2026-01-01 00:00:00', 1, 1)",
            (user_id, f"{user_id}@example.com", tier),
        )
    for package_id, namespace in (("pk_pub", "ns"), ("pk_user", "ns"), ("pk_sys", "sessionfs"), ("pk_fake", "sessionfs")):
        connection.execute(
            "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, "
            "source_type, source_locator) VALUES (?, 'user', 'u1', ?, ?, 'N', 'hosted', 'x')",
            (package_id, namespace, package_id),
        )
        connection.execute(
            "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
            "manifest_json, dlp_status, blob_key, blob_size_bytes) "
            "VALUES (?, ?, ?, 's', '{}', 'passed', ?, 1)",
            (f"v_{package_id}", package_id, f"sha256:{package_id}", f"k_{package_id}"),
        )
    rows = (
        ("l_public", "pk_pub", "v_pk_pub", "public", None, "verified", "adm"),
        ("l_blocked", "pk_pub", "v_pk_pub", "public", None, "blocked", "adm"),
        ("l_user", "pk_user", "v_pk_user", "user", "u1", "unverified", "u1"),
        # The launch catalog: platform admin, public, `sessionfs` namespace.
        ("l_sys", "pk_sys", "v_pk_sys", "public", None, "verified", "adm"),
        # `sessionfs` namespace but NOT published by a platform admin.
        ("l_fake", "pk_fake", "v_pk_fake", "public", None, "verified", "u1"),
    )
    for listing_id, package_id, version_id, visibility, owner, state, publisher in rows:
        connection.execute(
            "INSERT INTO skill_marketplace_listings (id, collection, package_id, version_id, "
            "owner_user_id, name, visibility, verification_state, published_by_user_id) "
            "VALUES (?, ?, ?, ?, ?, 'n', ?, ?, ?)",
            (listing_id, listing_id, package_id, version_id, owner, visibility, state, publisher),
        )


def test_pre_071_schema_is_real(pre_071_db: Path) -> None:
    connection = sqlite3.connect(str(pre_071_db))
    assert not (MIGRATION_071_TABLES & _names(connection, "table"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(skill_marketplace_listings)")}
    assert "moderation_state" not in columns and "verification_state" in columns
    connection.close()


def test_upgrade_creates_publishers_and_backfills(pre_071_db: Path) -> None:
    connection = sqlite3.connect(str(pre_071_db))
    _seed(connection)
    connection.commit()
    connection.close()

    command.upgrade(_cfg(pre_071_db), "071")
    connection = sqlite3.connect(str(pre_071_db))
    assert MIGRATION_071_TABLES <= _names(connection, "table")
    indexes = _names(connection, "index")
    for name in (
        "idx_skill_publishers_org",
        "idx_skill_publishers_user",
        "uq_skill_publisher_verification_subject",
        "idx_skill_publisher_verifications_installation",
        "idx_skill_trusted_publishers_repo",
        "idx_skill_publish_tokens_expires",
        "idx_skill_listing_moderation",
    ):
        assert name in indexes, name
    states = dict(connection.execute("SELECT id, moderation_state FROM skill_marketplace_listings"))
    # No moderation backfill: legacy blocks stay on verification_state (still
    # authoritative), so the existing restore path can lift them.
    assert states == {
        "l_public": "approved", "l_blocked": "approved", "l_user": "approved",
        "l_sys": "approved", "l_fake": "approved",
    }
    assert connection.execute(
        "SELECT id, owner_type, namespace, namespace_normalized FROM skill_publishers"
    ).fetchall() == [("pub_sessionfs", "system", "sessionfs", "sessionfs")]
    publishers = dict(connection.execute("SELECT id, publisher_id FROM skill_packages"))
    # Only SessionFS-controlled content gets the platform publisher.
    assert publishers == {"pk_pub": None, "pk_user": None, "pk_sys": "pub_sessionfs", "pk_fake": None}

    with pytest.raises(sqlite3.IntegrityError):  # moderation CHECK
        connection.execute("UPDATE skill_marketplace_listings SET moderation_state = 'maybe'")
    with pytest.raises(sqlite3.IntegrityError):  # owner scope CHECK
        connection.execute(
            "INSERT INTO skill_publishers (id, owner_type, namespace, namespace_normalized) "
            "VALUES ('p_bad', 'org', 'acme', 'acme')"
        )
    connection.execute(
        "INSERT INTO skill_publishers (id, owner_type, user_id, namespace, namespace_normalized) "
        "VALUES ('p1', 'user', 'u1', 'acme', 'acme')"
    )
    assert connection.execute(
        "SELECT required_domains, auth_generation FROM skill_publishers WHERE id = 'p1'"
    ).fetchone() == ("[]", 0)
    with pytest.raises(sqlite3.IntegrityError):  # global skeleton uniqueness
        connection.execute(
            "INSERT INTO skill_publishers (id, owner_type, user_id, namespace, namespace_normalized) "
            "VALUES ('p2', 'user', 'u1', 'acrne', 'acme')"
        )
    verification = (
        "INSERT INTO skill_publisher_verifications (id, publisher_id, method, subject, "
        "subject_display, status) VALUES (?, 'p1', 'dns_txt', 'acme.com', 'acme.com', ?)"
    )
    connection.execute(verification, ("v1", "verified"))
    connection.execute(verification, ("v2", "lapsed"))
    with pytest.raises(sqlite3.IntegrityError):  # one verified holder per subject
        connection.execute(verification, ("v3", "verified"))
    connection.execute(
        "INSERT INTO skill_trusted_publishers (id, publisher_id, repository_owner_id, repository_id, "
        "repository, workflow_path) VALUES ('t1', 'p1', 1, 2, 'o/r', '.github/workflows/r.yml')"
    )
    assert connection.execute(
        "SELECT reusable_workflow, allow_workflow_run FROM skill_trusted_publishers"
    ).fetchone() == (None, 0)
    connection.execute("INSERT INTO skill_github_installation_states (installation_id) VALUES (42)")
    assert connection.execute(
        "SELECT suspended_at, deleted_at FROM skill_github_installation_states"
    ).fetchone() == (None, None)
    token = (
        "INSERT INTO skill_publish_tokens (id, token_hash, trusted_publisher_id, publisher_id, "
        "oidc_issuer, oidc_jti, expires_at) VALUES (?, ?, 't1', 'p1', 'iss', 'jti-1', '2030-01-01')"
    )
    connection.execute(token, ("tok1", "a" * 64))
    assert connection.execute("SELECT auth_generation FROM skill_publish_tokens").fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError):  # jti replay guard
        connection.execute(token, ("tok2", "b" * 64))
    connection.commit()
    connection.close()


def test_downgrade_restores_069_shape(pre_071_db: Path) -> None:
    config = _cfg(pre_071_db)
    connection = sqlite3.connect(str(pre_071_db))
    _seed(connection)
    connection.commit()
    connection.close()
    command.upgrade(config, "071")
    connection = sqlite3.connect(str(pre_071_db))
    connection.execute("UPDATE skill_marketplace_listings SET moderation_state = 'pending_review' WHERE id = 'l_user'")
    connection.commit()
    connection.close()

    command.downgrade(config, "070")
    connection = sqlite3.connect(str(pre_071_db))
    assert not (MIGRATION_071_TABLES & _names(connection, "table"))
    listing_columns = {row[1] for row in connection.execute("PRAGMA table_info(skill_marketplace_listings)")}
    assert not (_DROPPED["skill_marketplace_listings"] & listing_columns)
    package_columns = {row[1] for row in connection.execute("PRAGMA table_info(skill_packages)")}
    assert "publisher_id" not in package_columns
    # A listing still awaiting review stays undeliverable after the downgrade.
    published = dict(connection.execute("SELECT id, is_published FROM skill_marketplace_listings"))
    assert published == {"l_public": 1, "l_blocked": 1, "l_user": 0, "l_sys": 1, "l_fake": 1}
    connection.close()
    command.upgrade(config, "071")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 069 and back, on REAL PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "069")
    command.upgrade(config, "head")


def test_upgrade_canonicalizes_and_dedupes_sso_domains(pre_071_db: Path) -> None:
    connection = sqlite3.connect(str(pre_071_db))
    for org in ("orgA", "orgB"):
        connection.execute(
            "INSERT INTO organizations (id, name, slug, tier) VALUES (?, ?, ?, 'team')", (org, org, org)
        )
    rows = (
        ("odv_a_pending", "orgA", "Acme.com.", "pending", "2026-01-01 00:00:00"),
        ("odv_a_verified", "orgA", "acme.com", "verified", "2026-01-02 00:00:00"),
        ("odv_b_verified", "orgB", "ACME.COM", "verified", "2026-01-03 00:00:00"),
        ("odv_b_other", "orgB", "Other.Example.", "pending", "2026-01-04 00:00:00"),
    )
    for row_id, org, domain, status, created in rows:
        connection.execute(
            "INSERT INTO org_domain_verifications (id, org_id, domain, method, verification_token, status, "
            "created_at) VALUES (?, ?, ?, 'dns_txt', 't', ?, ?)",
            (row_id, org, domain, status, created),
        )
    connection.commit()
    connection.close()
    command.upgrade(_cfg(pre_071_db), "071")
    connection = sqlite3.connect(str(pre_071_db))
    result = connection.execute(
        "SELECT id, domain, status FROM org_domain_verifications ORDER BY id"
    ).fetchall()
    assert result == [
        ("odv_a_verified", "acme.com", "verified"),  # same-org duplicate removed, verified kept
        ("odv_b_other", "other.example", "pending"),
        ("odv_b_verified", "acme.com", "failed"),  # second org's verification demoted
    ]
    connection.close()
