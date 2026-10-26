"""Migration 094: helper_actions (session-helper confirm-first proposals).

SQLite: the real metadata minus the new table, stamped at 093. PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.
"""

from __future__ import annotations

import os
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

NEW_TABLE = "helper_actions"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_094_db(tmp_path: Path) -> Path:
    path = tmp_path / "m094.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name != NEW_TABLE]
    assert len(prior) == len(Base.metadata.tables) - 1
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) "
            "VALUES ('org_m094', 'o', 'o-m094', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
            "VALUES ('u_m094', 'm094@example.com', 'u', 'enterprise', 1, 1, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO helper_conversations (id, user_id, title) "
            "VALUES ('hc_m094', 'u_m094', 't')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "093")
    return path


def _tables(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _cols(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns(NEW_TABLE)}
    finally:
        engine.dispose()


def _indexes(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {i["name"] for i in sa.inspect(engine).get_indexes(NEW_TABLE)}
    finally:
        engine.dispose()


def test_upgrade_creates_table_and_downgrade_drops(pre_094_db: Path) -> None:
    assert NEW_TABLE not in _tables(pre_094_db)
    config = _cfg(f"sqlite+aiosqlite:///{pre_094_db}")

    command.upgrade(config, "094")
    assert NEW_TABLE in _tables(pre_094_db)
    assert {
        "id", "user_id", "conversation_id", "message_id", "kind",
        "params", "preview", "status", "result", "created_at",
        "expires_at", "decided_at",
    } <= _cols(pre_094_db)

    assert {
        "idx_helper_actions_user_status", "idx_helper_actions_conversation",
    } <= _indexes(pre_094_db)

    # status defaults to 'pending'.
    engine = sa.create_engine(f"sqlite:///{pre_094_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO helper_actions "
                "(id, user_id, conversation_id, kind, expires_at) "
                "VALUES ('ha_m094', 'u_m094', 'hc_m094', 'set_auto_narrative', CURRENT_TIMESTAMP)"
            ))
            status = conn.execute(sa.text(
                "SELECT status FROM helper_actions WHERE id = 'ha_m094'"
            )).scalar()
        assert status == "pending"
    finally:
        engine.dispose()

    command.downgrade(config, "093")
    assert NEW_TABLE not in _tables(pre_094_db)
    command.upgrade(config, "094")
    assert NEW_TABLE in _tables(pre_094_db)


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
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-093) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "093")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) "
            "VALUES ('org_pg094', 'o', 'o-pg094', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
            "VALUES ('u_pg094', 'pg094@example.com', 'u', 'enterprise', true, true, now())"
        ))
        conn.execute(sa.text(
            "INSERT INTO helper_conversations (id, user_id, title) "
            "VALUES ('hc_pg094', 'u_pg094', 't')"
        ))

    _pg(url, seed)

    try:
        command.upgrade(config, "094")

        def check(conn) -> None:
            cols = {
                r[0]
                for r in conn.execute(sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'helper_actions'"
                ))
            }
            assert {
                "id", "user_id", "conversation_id", "message_id", "kind",
                "params", "preview", "status", "result", "created_at",
                "expires_at", "decided_at",
            } <= cols
            indexes = set(
                conn.execute(sa.text(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'helper_actions'"
                )).scalars()
            )
            assert {
                "idx_helper_actions_user_status", "idx_helper_actions_conversation",
            } <= indexes
            conn.execute(sa.text(
                "INSERT INTO helper_actions (id, user_id, conversation_id, kind, expires_at) "
                "VALUES ('ha_pg094', 'u_pg094', 'hc_pg094', 'trash_sessions', now())"
            ))
            assert conn.execute(sa.text(
                "SELECT status FROM helper_actions WHERE id = 'ha_pg094'"
            )).scalar() == "pending"
            # ON DELETE CASCADE from helper_conversations and users.
            conn.execute(sa.text(
                "DELETE FROM helper_conversations WHERE id = 'hc_pg094'"
            ))
            assert conn.execute(sa.text(
                "SELECT count(*) FROM helper_actions WHERE conversation_id = 'hc_pg094'"
            )).scalar() == 0

        _pg(url, check)
        command.downgrade(config, "093")

        def gone(conn) -> None:
            names = set(
                conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )).scalars()
            )
            assert NEW_TABLE not in names

        _pg(url, gone)
        command.upgrade(config, "head")
    finally:
        def cleanup(conn) -> None:
            conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg094'"))

        _pg(url, cleanup)
