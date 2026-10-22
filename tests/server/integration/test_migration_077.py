"""Migration 077 (native catalogs + works-in) round-trip.

The prior schema is the REAL SQLAlchemy metadata minus the four tables 077
adds. The PostgreSQL leg runs when SFS_TEST_PG_URL is set (and in the
`migrations-postgres` CI gate).
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

MIGRATION_077_TABLES = {
    "skill_feed_catalogs",
    "skill_feed_entries",
    "skill_tool_verifications",
    "skill_verification_jobs",
}

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


@pytest.fixture()
def pre_077_db(tmp_path: Path) -> Path:
    path = tmp_path / "m077.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [table for name, table in Base.metadata.tables.items() if name not in MIGRATION_077_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_077_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()
    command.stamp(_cfg(path), "076")
    return path


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_upgrade_creates_tables_and_constraints(pre_077_db: Path) -> None:
    command.upgrade(_cfg(pre_077_db), "077")
    connection = sqlite3.connect(str(pre_077_db))
    assert MIGRATION_077_TABLES <= _names(connection, "table")
    assert {
        "uq_skill_verification_jobs_open",
        "idx_skill_tool_verifications_lookup",
        "idx_skill_feed_entries_version",
    } <= _names(connection, "index")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "INSERT INTO skill_feed_catalogs (id, scope_key, scope_type, scope_id, blob_prefix) "
        "VALUES ('c1', 'public', 'public', NULL, 'p')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # public row must not carry a scope id
        connection.execute(
            "INSERT INTO skill_feed_catalogs (id, scope_key, scope_type, scope_id, blob_prefix) "
            "VALUES ('c2', 'public2', 'public', 'x', 'p')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # one row per scope
        connection.execute(
            "INSERT INTO skill_feed_catalogs (id, scope_key, scope_type, scope_id, blob_prefix) "
            "VALUES ('c3', 'public', 'public', NULL, 'p')"
        )
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
    job = (
        "INSERT INTO skill_verification_jobs (id, version_id, tool, level, status) "
        "VALUES (?, 'v1', 'claude-code', 'L1', ?)"
    )
    connection.execute(job, ("j1", "queued"))
    with pytest.raises(sqlite3.IntegrityError):  # one open job per (version, tool, level)
        connection.execute(job, ("j2", "claimed"))
    connection.execute("UPDATE skill_verification_jobs SET status = 'done' WHERE id = 'j1'")
    connection.execute(job, ("j3", "queued"))
    connection.execute(
        "UPDATE skill_verification_jobs SET outcome = 'skipped', outcome_reason = 'blocked' WHERE id = 'j1'"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE skill_verification_jobs SET outcome = 'maybe' WHERE id = 'j1'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_tool_verifications (id, version_id, tool, tool_version, level, status) "
            "VALUES ('t1', 'v1', 'codex', 'x', 'L3', 'pass')"
        )
    connection.execute(
        "INSERT INTO skill_tool_verifications (id, version_id, tool, tool_version, level, status) "
        "VALUES ('t1', 'v1', 'codex', 'x', 'L0', 'pass')"
    )
    connection.execute(
        "INSERT INTO skill_feed_entries (catalog_id, version_id, tool, plugin_name, projection_hash, "
        "plugin_path) VALUES ('c1', 'v1', 'codex', 'ns--n', 'sha256:x', 'plugins/ns--n')"
    )
    connection.execute("DELETE FROM skill_package_versions WHERE id = 'v1'")
    assert connection.execute("SELECT COUNT(*) FROM skill_feed_entries").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM skill_tool_verifications").fetchone()[0] == 0
    connection.commit()
    connection.close()


def test_downgrade_removes_only_077_tables(pre_077_db: Path) -> None:
    config = _cfg(pre_077_db)
    command.upgrade(config, "077")
    command.downgrade(config, "076")
    connection = sqlite3.connect(str(pre_077_db))
    tables = _names(connection, "table")
    assert not (MIGRATION_077_TABLES & tables)
    assert "skill_package_versions" in tables
    connection.close()
    command.upgrade(config, "077")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 076 and back, on REAL PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "076")
    command.upgrade(config, "head")
