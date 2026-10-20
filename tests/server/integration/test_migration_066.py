"""Migration 066 marketplace-takedown round-trip.

Mirrors test_migration_063/064: the prior schema is seeded from the REAL
SQLAlchemy metadata minus what 066 adds (two tables, plus
org_skill_assignments.revoked_at and the revoked-aware partial unique
indexes, which are rewound to their 063 shape here). Dialect divergence is
covered by `test_full_chain_on_postgres` when SFS_TEST_PG_URL is set and by
the `migrations-postgres` CI gate.
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

MIGRATION_066_TABLES = {
    "skill_package_reports",
    "skill_revocation_events",
    "skill_blocked_content_hashes",
    "skill_grant_generations",
}

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
assert Path(_MIGRATIONS).is_dir(), _MIGRATIONS

# org_skill_assignments exactly as 063 created it (065 shape): no revoked_at,
# a cascading FK on version_id, and the pre-066 partial unique indexes.
_PRE_066_ASSIGNMENTS_DDL = """CREATE TABLE org_skill_assignments (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    org_id VARCHAR(64) NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    version_id VARCHAR(64) NOT NULL REFERENCES skill_package_versions (id) ON DELETE CASCADE,
    content_hash VARCHAR(80) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
    target_id VARCHAR(100) DEFAULT '*' NOT NULL,
    target_user_id VARCHAR(36) REFERENCES users (id) ON DELETE CASCADE,
    mode VARCHAR(20) DEFAULT 'optional' NOT NULL,
    target_tools TEXT DEFAULT '[]' NOT NULL,
    policy_snapshot TEXT DEFAULT '{}' NOT NULL,
    created_by_user_id VARCHAR(36) REFERENCES users (id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_org_skill_assignment_target_type CHECK (target_type IN ('org', 'user', 'device')),
    CONSTRAINT ck_org_skill_assignment_target_binding CHECK (
        (target_type = 'org' AND target_id = '*' AND target_user_id IS NULL) OR
        (target_type = 'user' AND target_user_id IS NOT NULL AND target_id = target_user_id) OR
        (target_type = 'device' AND target_user_id IS NOT NULL AND target_id != '*')),
    CONSTRAINT ck_org_skill_assignment_mode CHECK (mode IN ('required', 'optional'))
)"""
_PRE_066_ASSIGNMENT_INDEXES = (
    "CREATE UNIQUE INDEX uq_org_skill_assignment_org_user ON org_skill_assignments "
    "(org_id, version_id, target_type, target_id) WHERE target_type != 'device'",
    "CREATE UNIQUE INDEX uq_org_skill_assignment_device ON org_skill_assignments "
    "(org_id, version_id, target_user_id, target_id) WHERE target_type = 'device'",
    "CREATE INDEX idx_org_skill_assignment_target ON org_skill_assignments "
    "(org_id, target_type, target_id)",
    "CREATE INDEX idx_org_skill_assignment_version ON org_skill_assignments (version_id)",
    "CREATE INDEX idx_org_skill_assignment_target_user ON org_skill_assignments (target_user_id)",
    "CREATE INDEX idx_org_skill_assignment_created_by ON org_skill_assignments (created_by_user_id)",
)


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _build_pre_066_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [
        table for name, table in Base.metadata.tables.items() if name not in MIGRATION_066_TABLES
    ]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_066_TABLES), (
        "MIGRATION_066_TABLES drifted from the models: "
        f"{sorted(MIGRATION_066_TABLES - set(Base.metadata.tables))}"
    )
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()
    # Rewind org_skill_assignments to its exact 065 shape (with the cascading
    # version FK that 066 removes).
    connection = sqlite3.connect(str(path))
    connection.execute("DROP TABLE org_skill_assignments")
    connection.execute(_PRE_066_ASSIGNMENTS_DDL)
    connection.execute("ALTER TABLE skill_marketplace_listings DROP COLUMN owner_state")
    connection.execute("ALTER TABLE skill_packages DROP COLUMN owner_state")
    connection.execute("ALTER TABLE skill_packages DROP COLUMN platform_state")
    for statement in _PRE_066_ASSIGNMENT_INDEXES:
        connection.execute(statement)
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_066_db(tmp_path: Path) -> Path:
    path = tmp_path / "m066.db"
    _build_pre_066_db(path)
    command.stamp(_cfg(path), "065")
    return path


def _seed_assignment(connection: sqlite3.Connection, assignment_id: str = "a1") -> None:
    connection.execute(
        "INSERT OR IGNORE INTO users (id, email, tier, created_at, is_active, email_verified) "
        "VALUES ('u1', 'u1@example.com', 'pro', '2026-01-01 00:00:00', 1, 1)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO organizations (id, name, slug, tier) VALUES ('org1', 'Org', 'org1', 'team')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO skill_packages (id, owner_type, owner_id, org_id, namespace, name, "
        "display_name, source_type, source_locator) "
        "VALUES ('pk1', 'org', 'org1', 'org1', 'ns', 'n', 'N', 'hosted', 'x')"
    )
    connection.execute(
        "INSERT OR IGNORE INTO skill_package_versions (id, package_id, content_hash, "
        "agent_plugin_schema, manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v1', 'pk1', 'sha256:aa', 's', '{}', 'passed', 'k1', 1)"
    )
    connection.execute(
        "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, target_id) "
        f"VALUES ('{assignment_id}', 'org1', 'v1', 'sha256:aa', 'org', '*')"  # noqa: S608 - test-local
    )


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def test_pre_066_schema_is_the_real_schema_not_a_stub(pre_066_db: Path) -> None:
    connection = sqlite3.connect(str(pre_066_db))
    tables = _names(connection, "table")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(org_skill_assignments)")}
    connection.close()
    assert not (MIGRATION_066_TABLES & tables)
    assert "revoked_at" not in columns
    for table in ("skill_marketplace_listings", "skill_packages"):
        names = {row[1] for row in sqlite3.connect(str(pre_066_db)).execute(f"PRAGMA table_info({table})")}
        assert "owner_state" not in names, table
    assert len(tables) > 50


def test_upgrade_creates_tables_constraints_and_revocable_assignments(pre_066_db: Path) -> None:
    connection = sqlite3.connect(str(pre_066_db))
    _seed_assignment(connection)
    connection.commit()
    connection.close()

    command.upgrade(_cfg(pre_066_db), "066")
    connection = sqlite3.connect(str(pre_066_db))
    assert MIGRATION_066_TABLES <= _names(connection, "table")
    indexes = _names(connection, "index")
    for name in (
        "uq_skill_report_open_reporter_package",
        "idx_skill_report_package_status",
        "idx_skill_report_status_created",
        "uq_skill_revocation_changed_at",
        "idx_skill_revocation_package",
        "uq_org_skill_assignment_org_user",
        "uq_org_skill_assignment_device",
    ):
        assert name in indexes, name

    # A revoked assignment never blocks a fresh one; two live ones still clash.
    connection.execute("UPDATE org_skill_assignments SET revoked_at = '2026-01-02 00:00:00' WHERE id = 'a1'")
    _seed_assignment(connection, "a2")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_assignment(connection, "a3")

    # One OPEN report per (reporter, package).
    insert_report = (
        "INSERT INTO skill_package_reports (id, package_id, reporter_user_id, reason, status, "
        "scope_kind) VALUES (?, 'pk1', 'u1', ?, ?, 'platform')"
    )
    connection.execute(insert_report, ("r1", "spam", "open"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert_report, ("r2", "spam", "open"))
    connection.execute(insert_report, ("r3", "spam", "dismissed"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert_report, ("r4", "bogus", "dismissed"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert_report, ("r5", "spam", "pending"))
    scoped_report = (
        "INSERT INTO skill_package_reports (id, package_id, reporter_user_id, reason, status, "
        "scope_kind, scope_org_id) VALUES (?, 'pk1', NULL, 'spam', 'dismissed', ?, ?)"
    )
    connection.execute(scoped_report, ("r6", "org", "org1"))
    with pytest.raises(sqlite3.IntegrityError):  # org scope needs an org
        connection.execute(scoped_report, ("r7", "org", None))
    with pytest.raises(sqlite3.IntegrityError):  # platform scope carries no org
        connection.execute(scoped_report, ("r8", "platform", "org1"))

    # The durable hash blocklist has no FK to packages / versions: it outlives them.
    connection.execute(
        "INSERT INTO skill_blocked_content_hashes (content_hash, scope_org_id, source_package_id, "
        "source_version_id, reason, actor_role, blocked_at) "
        "VALUES ('sha256:aa', NULL, 'gone-package', 'gone-version', 'r', 'platform_admin', "
        "'2026-01-01 00:00:00')"
    )
    assert "idx_skill_blocked_hash_active" in _names(connection, "index")

    insert_event = (
        "INSERT INTO skill_revocation_events (kind, package_id, state, reason, actor_role, visibility, "
        "org_id, owner_user_id, changed_at) VALUES ('package', 'pk1', ?, 'r', 'platform_admin', ?, ?, ?, ?)"
    )
    connection.execute(insert_event, ("blocked", "public", None, None, "2026-01-01 00:00:00.000000"))
    connection.execute(insert_event, ("restored", "org", "org1", None, "2026-01-01 00:00:00.000001"))
    ids = [row[0] for row in connection.execute("SELECT id FROM skill_revocation_events ORDER BY changed_at")]
    assert ids == sorted(ids) and ids[0] < ids[1]
    with pytest.raises(sqlite3.IntegrityError):  # changed_at is unique
        connection.execute(insert_event, ("yanked", "public", None, None, "2026-01-01 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # bad state
        connection.execute(insert_event, ("deleted", "public", None, None, "2026-01-03 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # org scope without org_id
        connection.execute(insert_event, ("yanked", "org", None, None, "2026-01-04 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # user scope without owner
        connection.execute(insert_event, ("yanked", "user", None, None, "2026-01-05 00:00:00.000000"))
    content_event = (
        "INSERT INTO skill_revocation_events (kind, package_id, version_id, content_hash, state, "
        "block_scope, reason, actor_role, visibility, changed_at) "
        "VALUES (?, ?, NULL, ?, 'blocked', ?, 'r', 'platform_admin', 'public', ?)"
    )
    connection.execute(content_event, ("content", None, "sha256:aa", "global", "2026-01-06 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # content events never name a package
        connection.execute(content_event, ("content", "pk1", "sha256:aa", "global", "2026-01-07 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # content events need a hash
        connection.execute(content_event, ("content", None, None, "global", "2026-01-08 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # bad block_scope
        connection.execute(content_event, ("content", None, "sha256:aa", "planet", "2026-01-09 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # bad kind
        connection.execute(content_event, ("other", None, "sha256:aa", "global", "2026-01-10 00:00:00.000000"))
    scoped_event = (
        "INSERT INTO skill_revocation_events (kind, content_hash, state, block_scope, scope_org_id, "
        "reason, actor_role, visibility, org_id, changed_at) "
        "VALUES ('content', 'sha256:aa', 'blocked', ?, ?, 'r', 'org_admin', ?, ?, ?)"
    )
    connection.execute(scoped_event, ("org", "org1", "org", "org1", "2026-01-11 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # an org block must name its org
        connection.execute(scoped_event, ("org", None, "org", "org1", "2026-01-12 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # only org/owner events carry scope_org_id
        connection.execute(scoped_event, ("global", "org1", "public", None, "2026-01-13 00:00:00.000000"))
    # Owner-layer yanks: with the owning org, or without one (personal package).
    connection.execute(scoped_event, ("owner", "org1", "org", "org1", "2026-01-14 00:00:00.000000"))
    owner_personal = (
        "INSERT INTO skill_revocation_events (kind, package_id, version_id, state, block_scope, "
        "scope_org_id, reason, actor_role, visibility, owner_user_id, changed_at) "
        "VALUES ('version', 'pk1', 'v1', 'yanked', 'owner', NULL, 'r', 'owner', 'user', 'u1', "
        "'2026-01-15 00:00:00.000000')"
    )
    connection.execute(owner_personal)
    with pytest.raises(sqlite3.IntegrityError):  # a NULL block_scope never carries an org
        connection.execute(
            "INSERT INTO skill_revocation_events (kind, package_id, version_id, state, scope_org_id, "
            "reason, actor_role, visibility, changed_at) VALUES ('version', 'pk1', 'v1', 'yanked', "
            "'org1', 'r', 'platform_admin', 'public', '2026-01-16 00:00:00.000000')"
        )
    connection.commit()
    connection.close()


def test_downgrade_restores_065_shape(pre_066_db: Path) -> None:
    config = _cfg(pre_066_db)
    command.upgrade(config, "066")
    connection = sqlite3.connect(str(pre_066_db))
    _seed_assignment(connection)
    connection.execute("UPDATE org_skill_assignments SET revoked_at = '2026-01-02 00:00:00' WHERE id = 'a1'")
    _seed_assignment(connection, "a2")
    connection.commit()
    connection.close()

    command.downgrade(config, "065")
    connection = sqlite3.connect(str(pre_066_db))
    assert not (MIGRATION_066_TABLES & _names(connection, "table"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(org_skill_assignments)")}
    assert "revoked_at" not in columns
    # Revoked rows are removed (they would otherwise grant again); live rows kept.
    assert [row[0] for row in connection.execute("SELECT id FROM org_skill_assignments")] == ["a2"]
    # The 063 unique indexes are back and enforced.
    with pytest.raises(sqlite3.IntegrityError):
        _seed_assignment(connection, "a3")
    # CHECK constraints survived (no table rebuild).
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, target_id) "
            "VALUES ('a4', 'org1', 'v1', 'sha256:aa', 'galaxy', '*')"
        )
    connection.close()
    command.upgrade(config, "066")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head then downgrade to 065 and back, on REAL PostgreSQL."""
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "065")
    command.upgrade(config, "head")


def _version_fks(connection: sqlite3.Connection) -> list[tuple]:
    return [
        row for row in connection.execute("PRAGMA foreign_key_list(org_skill_assignments)")
        if row[2] == "skill_package_versions"
    ]


def _assignment_schema(connection: sqlite3.Connection) -> dict[str, str | None]:
    return {
        name: sql
        for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'org_skill_assignments'"
        )
    }


def test_pre_066_assignments_carry_the_cascading_version_fk(pre_066_db: Path) -> None:
    connection = sqlite3.connect(str(pre_066_db))
    fks = _version_fks(connection)
    connection.close()
    assert len(fks) == 1 and fks[0][3] == "version_id" and fks[0][6] == "CASCADE"


def test_upgrade_rebuilds_assignments_identical_to_a_fresh_schema(pre_066_db: Path, tmp_path: Path) -> None:
    connection = sqlite3.connect(str(pre_066_db))
    _seed_assignment(connection)
    connection.commit()
    connection.close()
    command.upgrade(_cfg(pre_066_db), "066")

    fresh_path = tmp_path / "fresh.db"
    engine = sa.create_engine(f"sqlite:///{fresh_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    upgraded = sqlite3.connect(str(pre_066_db))
    fresh = sqlite3.connect(str(fresh_path))
    # Columns added by later migrations (070) are not part of the 066 shape.
    fresh.execute("DROP INDEX uq_org_skill_assignment_package_target")
    fresh.execute("DROP INDEX uq_org_skill_assignment_package_device")
    for column in (
        "package_id", "superseded_by_assignment_id", "superseded_at", "consent_rule_version",
        "capability_digest",
    ):
        fresh.execute(f"ALTER TABLE org_skill_assignments DROP COLUMN {column}")
    try:
        assert _version_fks(upgraded) == []
        # Table DDL (columns, CHECKs, remaining FKs) and every index match a
        # fresh create_all of the current model.
        assert _assignment_schema(upgraded) == _assignment_schema(fresh)
        # Rows survived the rebuild.
        assert [row[0] for row in upgraded.execute("SELECT id FROM org_skill_assignments")] == ["a1"]
    finally:
        upgraded.close()
        fresh.close()


def test_revoked_assignment_survives_version_delete_with_foreign_keys_on(pre_066_db: Path) -> None:
    command.upgrade(_cfg(pre_066_db), "066")
    connection = sqlite3.connect(str(pre_066_db))
    connection.execute("PRAGMA foreign_keys = ON")
    _seed_assignment(connection)
    connection.execute("UPDATE org_skill_assignments SET revoked_at = '2026-01-02 00:00:00' WHERE id = 'a1'")
    connection.commit()
    connection.execute("DELETE FROM skill_package_versions WHERE id = 'v1'")
    connection.commit()
    rows = list(connection.execute("SELECT id, version_id, content_hash, revoked_at FROM org_skill_assignments"))
    connection.close()
    assert rows == [("a1", "v1", "sha256:aa", "2026-01-02 00:00:00")]


def test_downgrade_restores_the_cascading_version_fk(pre_066_db: Path) -> None:
    config = _cfg(pre_066_db)
    command.upgrade(config, "066")
    connection = sqlite3.connect(str(pre_066_db))
    _seed_assignment(connection)
    connection.commit()
    connection.close()
    command.downgrade(config, "065")
    connection = sqlite3.connect(str(pre_066_db))
    fks = _version_fks(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(org_skill_assignments)")}
    rows = [row[0] for row in connection.execute("SELECT id FROM org_skill_assignments")]
    connection.close()
    assert len(fks) == 1 and fks[0][6] == "CASCADE"
    assert "revoked_at" not in columns
    assert rows == ["a1"]
    command.upgrade(config, "066")
    connection = sqlite3.connect(str(pre_066_db))
    assert _version_fks(connection) == []
    connection.close()
