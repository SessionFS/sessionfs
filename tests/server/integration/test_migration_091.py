"""Migration 091: Jira tracker integration tables (GitHub #98).

Purely additive — three new tables, no existing rows change. SQLite round-trip
verifies the tables, columns, PK/unique/index/FK shapes, and that downgrade
drops them. PostgreSQL runs the real chain when SFS_TEST_PG_URL is set.
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

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)

_NEW_TABLES = {
    "org_tracker_connections",
    "tracker_project_mappings",
    "ticket_tracker_links",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_metadata() -> sa.MetaData:
    """089's schema — Base.metadata minus the three 091 tables."""
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name not in _NEW_TABLES:
            table.to_metadata(metadata)
    return metadata


@pytest.fixture
def pre_091_db(tmp_path: Path) -> Path:
    path = tmp_path / "m091.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "090")
    return path


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }


def _fk_target(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        if row[3] == column:
            return row[2]
    return None


def test_sqlite_round_trip(pre_091_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_091_db}")
    command.upgrade(config, "091")

    conn = sqlite3.connect(str(pre_091_db))
    assert _NEW_TABLES <= _tables(conn)

    assert {
        "org_id", "provider", "site_url", "account_email", "encrypted_api_token",
        "default_template", "enabled", "connected_by_user_id", "last_verified_at",
        "created_at", "updated_at",
    } <= _columns(conn, "org_tracker_connections")
    assert {
        "id", "org_id", "project_id", "jira_project_key", "issue_type", "template",
        "done_status_name", "created_by_user_id", "created_at", "updated_at",
    } <= _columns(conn, "tracker_project_mappings")
    assert {
        "ticket_id", "org_id", "provider", "status", "claim_token", "site_url",
        "issue_id", "issue_key", "issue_url", "last_pushed_at", "last_pushed_by_user_id",
        "last_known_status", "last_status_checked_at", "payload_hash",
        "last_published_text", "last_push_failed_at", "last_push_failure",
    } <= _columns(conn, "ticket_tracker_links")

    # Primary keys + the mapping's unique project_id + the two org indexes.
    pk = {
        row[1] for row in conn.execute(
            "SELECT * FROM pragma_table_info('org_tracker_connections') WHERE pk > 0"
        )
    }
    assert pk == {"org_id"}
    pk_links = {
        row[1] for row in conn.execute(
            "SELECT * FROM pragma_table_info('ticket_tracker_links') WHERE pk > 0"
        )
    }
    assert pk_links == {"ticket_id"}
    indexes = _indexes(conn)
    assert {"idx_tracker_mapping_org", "idx_ticket_tracker_link_org",
            "idx_ticket_tracker_link_status_checked"} <= indexes
    # SQLite enforces the named UNIQUE constraint with an auto-index; check
    # the uniqueness on project_id via index_list rather than its name.
    unique_project_id = any(
        row[2] == 1 and row[3] == "u"
        and {c[2] for c in conn.execute(f"PRAGMA index_info({row[1]})")} == {"project_id"}
        for row in conn.execute("PRAGMA index_list(tracker_project_mappings)")
    )
    assert unique_project_id

    # Foreign keys (CASCADE).
    assert _fk_target(conn, "org_tracker_connections", "org_id") == "organizations"
    assert _fk_target(conn, "tracker_project_mappings", "project_id") == "projects"
    assert _fk_target(conn, "ticket_tracker_links", "ticket_id") == "tickets"
    conn.close()

    command.downgrade(config, "090")
    conn = sqlite3.connect(str(pre_091_db))
    assert not (_NEW_TABLES & _tables(conn))
    conn.close()

    command.upgrade(config, "091")  # idempotent re-upgrade
    conn = sqlite3.connect(str(pre_091_db))
    assert _NEW_TABLES <= _tables(conn)
    conn.close()


def _pg(url: str, work) -> object:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_round_trip_on_postgres() -> None:
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "090")
    command.upgrade(config, "091")

    def check(conn) -> None:
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('org_tracker_connections', 'tracker_project_mappings', 'ticket_tracker_links')"
        )).all()
        assert {r[0] for r in rows} == _NEW_TABLES
        uniques = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'tracker_project_mappings'::regclass"
            ))
        }
        assert "uq_tracker_mapping_project" in uniques

    _pg(url, check)
    command.downgrade(config, "090")

    def after_downgrade(conn) -> None:
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('org_tracker_connections', 'tracker_project_mappings', 'ticket_tracker_links')"
        )).all()
        assert rows == []

    _pg(url, after_downgrade)
    command.upgrade(config, "head")
