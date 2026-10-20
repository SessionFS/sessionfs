"""Migration 069 (instruction scanning) round-trip.

Mirrors test_migration_066: the prior schema is the REAL SQLAlchemy metadata
minus what 069 adds (three tables, three skill_package_versions columns) with
skill_revocation_events rewound to its 066 CHECK. The PostgreSQL leg runs
when SFS_TEST_PG_URL is set (and in the `migrations-postgres` CI gate).
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

MIGRATION_069_TABLES = {"skill_scan_rulesets", "skill_version_scans", "skill_scan_findings"}
MIGRATION_069_VERSION_COLUMNS = ("scan_summary", "quarantined_at", "quarantine_reason")

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _build_pre_069_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [
        table for name, table in Base.metadata.tables.items() if name not in MIGRATION_069_TABLES
    ]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_069_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    for column in MIGRATION_069_VERSION_COLUMNS:
        connection.execute(f"ALTER TABLE skill_package_versions DROP COLUMN {column}")
    # Rewind the revocation CHECK to its 066 shape (rebuild, as SQLite must).
    connection.execute("ALTER TABLE skill_revocation_events RENAME TO old_events")
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='old_events' AND sql IS NOT NULL"
    ).fetchall():
        connection.execute(f"DROP INDEX {name}")
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='old_events'"
    ).fetchone()[0]
    sql = sql.replace('"old_events"', "skill_revocation_events").replace("old_events", "skill_revocation_events")
    sql = sql.replace(", 'quarantined')", ")")
    assert "quarantined" not in sql
    connection.execute(sql)
    connection.execute("DROP TABLE old_events")
    for statement in (
        "CREATE UNIQUE INDEX uq_skill_revocation_changed_at ON skill_revocation_events (changed_at)",
        "CREATE INDEX idx_skill_revocation_package ON skill_revocation_events (package_id, version_id, id)",
        "CREATE INDEX idx_skill_revocation_org ON skill_revocation_events (org_id, changed_at)",
        "CREATE INDEX idx_skill_revocation_owner ON skill_revocation_events (owner_user_id, changed_at)",
        "CREATE INDEX idx_skill_revocation_actor ON skill_revocation_events (actor_user_id)",
    ):
        connection.execute(statement)
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_069_db(tmp_path: Path) -> Path:
    path = tmp_path / "m069.db"
    _build_pre_069_db(path)
    command.stamp(_cfg(path), "066")
    return path


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def _seed_version(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
        "VALUES ('u1', 'u1@example.com', 'pro', '2026-01-01 00:00:00', 1, 1)"
    )
    connection.execute(
        "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, "
        "source_type, source_locator) VALUES ('pk1', 'user', 'u1', 'ns', 'n', 'N', 'hosted', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v1', 'pk1', 'sha256:aa', 's', '{}', 'passed', 'k1', 1)"
    )


_EVENT = (
    "INSERT INTO skill_revocation_events (kind, package_id, version_id, state, block_scope, reason, "
    "actor_role, visibility, owner_user_id, changed_at) "
    "VALUES ('version', 'pk1', 'v1', ?, 'global', 'r', 'system', 'user', 'u1', ?)"
)


def test_pre_069_schema_is_real_and_rejects_quarantine(pre_069_db: Path) -> None:
    connection = sqlite3.connect(str(pre_069_db))
    assert not (MIGRATION_069_TABLES & _names(connection, "table"))
    _seed_version(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(_EVENT, ("quarantined", "2026-01-01 00:00:00.000000"))
    connection.close()


def test_upgrade_adds_scanning_and_quarantine_state(pre_069_db: Path) -> None:
    connection = sqlite3.connect(str(pre_069_db))
    _seed_version(connection)
    connection.execute(_EVENT, ("blocked", "2026-01-01 00:00:00.000000"))
    connection.commit()
    connection.close()

    command.upgrade(_cfg(pre_069_db), "069")
    connection = sqlite3.connect(str(pre_069_db))
    assert MIGRATION_069_TABLES <= _names(connection, "table")
    indexes = _names(connection, "index")
    for name in (
        "idx_skill_version_scans_version_scanned",
        "idx_skill_version_scans_ruleset_scanned",
        "idx_skill_scan_findings_scan",
        # The revocation table rebuild kept every 066 index.
        "uq_skill_revocation_changed_at",
        "idx_skill_revocation_package",
        "idx_skill_revocation_org",
        "idx_skill_revocation_owner",
        "idx_skill_revocation_actor",
    ):
        assert name in indexes, name
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(skill_package_versions)")}
    assert set(MIGRATION_069_VERSION_COLUMNS) <= set(columns)
    assert connection.execute("SELECT scan_summary, quarantined_at FROM skill_package_versions").fetchone() == ("{}", None)
    # Existing events survived the rebuild; the new state is accepted, others still checked.
    assert connection.execute("SELECT state FROM skill_revocation_events").fetchall() == [("blocked",)]
    connection.execute(_EVENT, ("quarantined", "2026-01-02 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(_EVENT, ("deleted", "2026-01-03 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # kind CHECK kept
        connection.execute(_EVENT.replace("'version'", "'bogus'"), ("blocked", "2026-01-05 00:00:00"))
    with pytest.raises(sqlite3.IntegrityError):  # changed_at still unique
        connection.execute(_EVENT, ("restored", "2026-01-02 00:00:00.000000"))
    with pytest.raises(sqlite3.IntegrityError):  # scope CHECK kept
        connection.execute(
            "INSERT INTO skill_revocation_events (kind, package_id, state, reason, actor_role, "
            "visibility, changed_at) VALUES ('package', 'pk1', 'yanked', 'r', 'owner', 'user', "
            "'2026-01-04 00:00:00')"
        )

    connection.execute(
        "INSERT INTO skill_scan_rulesets (version, rule_count) VALUES ('2026.10.1', 37)"
    )
    connection.execute(
        "INSERT INTO skill_version_scans (id, version_id, ruleset_version, max_severity, trigger) "
        "VALUES ('s1', 'v1', '2026.10.1', 'high', 'publish')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # one scan per (version, ruleset)
        connection.execute(
            "INSERT INTO skill_version_scans (id, version_id, ruleset_version, max_severity, trigger) "
            "VALUES ('s2', 'v1', '2026.10.1', 'high', 'rescan')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_version_scans (id, version_id, ruleset_version, max_severity, trigger) "
            "VALUES ('s3', 'v1', '2026.10.1', 'warn', 'rescan')"
        )
    connection.execute(
        "INSERT INTO skill_scan_findings (scan_id, rule_id, family, severity, file_path, line, col, "
        "message, line_sha) VALUES ('s1', 'r.x', 'f', 'high', 'SKILL.md', 1, 1, 'm', ?)",
        ("0" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_scan_findings (scan_id, rule_id, family, severity, file_path, line, col, "
            "message, line_sha) VALUES ('s1', 'r.x', 'f', 'none', 'SKILL.md', 1, 1, 'm', ?)",
            ("0" * 64,),
        )
    connection.commit()
    connection.close()


def test_downgrade_restores_066_shape(pre_069_db: Path) -> None:
    config = _cfg(pre_069_db)
    command.upgrade(config, "069")
    connection = sqlite3.connect(str(pre_069_db))
    _seed_version(connection)
    connection.execute(_EVENT, ("blocked", "2026-01-01 00:00:00.000000"))
    connection.execute(_EVENT, ("quarantined", "2026-01-02 00:00:00.000000"))
    connection.commit()
    connection.close()

    command.downgrade(config, "066")
    connection = sqlite3.connect(str(pre_069_db))
    assert not (MIGRATION_069_TABLES & _names(connection, "table"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(skill_package_versions)")}
    assert not (set(MIGRATION_069_VERSION_COLUMNS) & columns)
    # Quarantine events are dropped (the 066 CHECK cannot hold them); others kept.
    assert connection.execute("SELECT state FROM skill_revocation_events").fetchall() == [("blocked",)]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(_EVENT, ("quarantined", "2026-01-03 00:00:00.000000"))
    # skill_package_versions kept its CHECK constraints (no table rebuild).
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
            "manifest_json, dlp_status, blob_key, blob_size_bytes) "
            "VALUES ('v2', 'pk1', 'sha256:bb', 's', '{}', 'maybe', 'k2', 1)"
        )
    connection.close()
    command.upgrade(config, "069")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 066 and back, on REAL PostgreSQL."""
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "066")
    command.upgrade(config, "head")
