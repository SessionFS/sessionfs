"""Migration 093: ticket watchers, notifications, watch cursors and settings.

The "ticket watchers" feature needs four tables: ``ticket_watchers`` (one row
per followed ticket), ``ticket_notifications`` (server-generated events with a
monotonic BigInteger cursor), ``ticket_watch_cursors`` (per-user/per-persona
read position) and ``user_notification_settings`` (per-user email preference).
The migration's parent is read dynamically so a release re-chain never needs a
test edit; the SQLite round-trip proves the tables appear on upgrade and are
dropped on downgrade, and the PostgreSQL test runs against the full prior chain.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)

_WATCHERS_TABLE = "ticket_watchers"
_NOTIFICATIONS_TABLE = "ticket_notifications"
_CURSORS_TABLE = "ticket_watch_cursors"
_SETTINGS_TABLE = "user_notification_settings"

_NEW_TABLES = {
    _WATCHERS_TABLE,
    _NOTIFICATIONS_TABLE,
    _CURSORS_TABLE,
    _SETTINGS_TABLE,
}

_WATCHERS_COLUMNS = {"ticket_id", "user_id", "persona_name", "source", "muted", "created_at"}
_NOTIFICATIONS_COLUMNS = {
    "id", "user_id", "ticket_id", "project_id", "kind", "summary", "actor_type",
    "actor_user_id", "service_key_name", "event_count", "created_at", "updated_at",
    "read_at", "emailed_at",
}
_CURSORS_COLUMNS = {"user_id", "persona_name", "last_seen_notification_id", "updated_at"}
_SETTINGS_COLUMNS = {"user_id", "ticket_email", "updated_at"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    """093's parent, read from the script so a re-chain needs no edit."""
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("093")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


def _prior_metadata() -> sa.MetaData:
    """Everything in its 092 shape — i.e. before the four new tables exist."""
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name not in _NEW_TABLES:
            table.to_metadata(metadata)
    return metadata


@pytest.fixture
def pre_093_db(tmp_path: Path) -> Path:
    path = tmp_path / "m093.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _prior_revision())
    return path


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


def _indexes(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA index_list({table})")
    return {row[1] for row in rows}


def test_sqlite_round_trip(pre_093_db: Path) -> None:
    connection = sqlite3.connect(str(pre_093_db))
    # Prior shape: none of the four tables exist yet.
    assert _NEW_TABLES.isdisjoint(_tables(connection))
    connection.close()

    config = _cfg(f"sqlite+aiosqlite:///{pre_093_db}")
    command.upgrade(config, "093")

    connection = sqlite3.connect(str(pre_093_db))
    assert _NEW_TABLES <= _tables(connection)

    assert _WATCHERS_COLUMNS <= _columns(connection, _WATCHERS_TABLE)
    assert _NOTIFICATIONS_COLUMNS <= _columns(connection, _NOTIFICATIONS_TABLE)
    assert _CURSORS_COLUMNS <= _columns(connection, _CURSORS_TABLE)
    assert _SETTINGS_COLUMNS <= _columns(connection, _SETTINGS_TABLE)

    # The indexes the models rely on for lookups and delta/read queries.
    assert {
        "idx_ticket_watchers_ticket",
        "idx_ticket_watchers_user",
    } <= _indexes(connection, _WATCHERS_TABLE)
    assert {
        "idx_ticket_notifications_user_id",
        "idx_ticket_notifications_user_read",
        "idx_ticket_notifications_ticket",
        "idx_ticket_notifications_sweep",
        "idx_ticket_notifications_read_updated",
        "idx_ticket_notifications_emailed_updated",
    } <= _indexes(connection, _NOTIFICATIONS_TABLE)

    # Rows insert with the server defaults (source "explicit", muted false,
    # event_count 1, actor_type "user", cursor 0, ticket_email "your_turn").
    connection.execute(
        "INSERT INTO ticket_watchers (ticket_id, user_id) VALUES ('t_093', 'u_093')"
    )
    assert connection.execute(
        "SELECT source, muted FROM ticket_watchers WHERE ticket_id = 't_093'"
    ).fetchone() == ("explicit", 0)

    connection.execute(
        "INSERT INTO ticket_notifications (user_id, ticket_id, project_id, kind, summary) "
        "VALUES ('u_093', 't_093', 'p_093', 'comment', 'New comment')"
    )
    assert connection.execute(
        "SELECT event_count, actor_type FROM ticket_notifications WHERE ticket_id = 't_093'"
    ).fetchone() == (1, "user")

    connection.execute(
        "INSERT INTO ticket_watch_cursors (user_id) VALUES ('u_093')"
    )
    assert connection.execute(
        "SELECT persona_name, last_seen_notification_id FROM ticket_watch_cursors "
        "WHERE user_id = 'u_093'"
    ).fetchone() == ("", 0)

    connection.execute(
        "INSERT INTO user_notification_settings (user_id) VALUES ('u_093')"
    )
    assert connection.execute(
        "SELECT ticket_email FROM user_notification_settings WHERE user_id = 'u_093'"
    ).fetchone() == ("your_turn",)
    connection.commit()
    connection.close()

    command.downgrade(config, _prior_revision())

    connection = sqlite3.connect(str(pre_093_db))
    assert _NEW_TABLES.isdisjoint(_tables(connection))
    connection.close()


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, _prior_revision())

    def check(conn) -> None:
        rows = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('ticket_watchers', 'ticket_notifications', "
                "'ticket_watch_cursors', 'user_notification_settings')"
            )
        ).all()
        assert rows == []

    _pg(url, check)
    command.upgrade(config, "093")

    def check_upgraded(conn) -> None:
        rows = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('ticket_watchers', 'ticket_notifications', "
                "'ticket_watch_cursors', 'user_notification_settings')"
            )
        ).all()
        assert {r[0] for r in rows} == _NEW_TABLES

    _pg(url, check_upgraded)
    command.downgrade(config, _prior_revision())
    _pg(url, check)


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
