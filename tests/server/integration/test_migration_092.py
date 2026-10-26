"""Migration 092: helper_conversations + helper_messages tables.

Session helper piece 4 adds two tables for the Enterprise helper: one row per
thread (``helper_conversations``) and one per message (``helper_messages``),
with a CASCADE FK from messages to conversations.  The migration's parent is
read dynamically so a release re-chain never needs a test edit; the SQLite
round-trip proves the tables appear on upgrade and are dropped on downgrade.
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

_CONVERSATION_TABLE = "helper_conversations"
_MESSAGE_TABLE = "helper_messages"

_CONVERSATION_COLUMNS = {
    "id", "user_id", "org_id", "title", "scope", "created_at", "updated_at", "deleted_at",
}
_MESSAGE_COLUMNS = {
    "id", "conversation_id", "role", "content", "tool_name", "tool_call_id",
    "tool_args", "citations", "key_source", "input_tokens", "output_tokens",
    "stop_reason", "created_at",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    """092's parent, read from the script so a re-chain needs no edit."""
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("092")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


def _prior_metadata() -> sa.MetaData:
    """Everything in its 090 shape — i.e. before the two helper tables exist."""
    # Later migrations add tables that reference the helper tables (e.g.
    # helper_actions); leave those out too, transitively.
    excluded = {_CONVERSATION_TABLE, _MESSAGE_TABLE}
    changed = True
    while changed:
        changed = False
        for name, table in Base.metadata.tables.items():
            if name not in excluded and any(
                fk.column.table.name in excluded for fk in table.foreign_keys
            ):
                excluded.add(name)
                changed = True
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name not in excluded:
            table.to_metadata(metadata)
    return metadata


@pytest.fixture
def pre_092_db(tmp_path: Path) -> Path:
    path = tmp_path / "m092.db"
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


def test_sqlite_round_trip(pre_092_db: Path) -> None:
    connection = sqlite3.connect(str(pre_092_db))
    # Prior shape: the two helper tables do not exist yet.
    assert _CONVERSATION_TABLE not in _tables(connection)
    assert _MESSAGE_TABLE not in _tables(connection)
    connection.close()

    config = _cfg(f"sqlite+aiosqlite:///{pre_092_db}")
    command.upgrade(config, "092")

    connection = sqlite3.connect(str(pre_092_db))
    assert _CONVERSATION_TABLE in _tables(connection)
    assert _MESSAGE_TABLE in _tables(connection)

    assert _CONVERSATION_COLUMNS <= _columns(connection, _CONVERSATION_TABLE)
    assert _MESSAGE_COLUMNS <= _columns(connection, _MESSAGE_TABLE)

    # The two indexes the model relies on for list/order queries.
    assert "idx_helper_conversations_user_updated" in _indexes(
        connection, _CONVERSATION_TABLE
    )
    assert "idx_helper_messages_conversation" in _indexes(connection, _MESSAGE_TABLE)

    # Rows insert with the server defaults (content ""/0, timestamps now()).
    connection.execute(
        "INSERT INTO helper_conversations (id, user_id, org_id) "
        "VALUES ('hc_092', 'u_092', NULL)"
    )
    connection.execute(
        "INSERT INTO helper_messages (id, conversation_id, role) "
        "VALUES ('hm_092', 'hc_092', 'user')"
    )
    connection.commit()
    assert connection.execute(
        "SELECT role FROM helper_messages WHERE id = 'hm_092'"
    ).fetchone() == ("user",)
    connection.close()

    command.downgrade(config, _prior_revision())

    connection = sqlite3.connect(str(pre_092_db))
    assert _CONVERSATION_TABLE not in _tables(connection)
    assert _MESSAGE_TABLE not in _tables(connection)
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
                "AND table_name IN ('helper_conversations', 'helper_messages')"
            )
        ).all()
        assert rows == []

    _pg(url, check)
    command.upgrade(config, "092")

    def check_upgraded(conn) -> None:
        rows = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('helper_conversations', 'helper_messages')"
            )
        ).all()
        assert {r[0] for r in rows} == {"helper_conversations", "helper_messages"}

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
