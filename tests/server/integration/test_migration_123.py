"""Migration 123: attention_hides.

SQLite: the prior schema is the real metadata with ``attention_hides``
dropped, stamped at 122. PostgreSQL: ``test_upgrade_on_postgres`` runs the
real chain up to 122 when SFS_TEST_PG_URL is set.
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


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_123_db(tmp_path: Path) -> Path:
    path = tmp_path / "m123.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE attention_hides"))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m123', 'm123@example.com', 1, 'free', CURRENT_TIMESTAMP, 1)"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "122")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_123_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_123_db}")
    command.upgrade(config, "123")
    engine = sa.create_engine(f"sqlite:///{pre_123_db}")
    try:
        inspector = sa.inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("attention_hides")}
        assert set(cols) == {"user_id", "item_key", "version_key", "hidden_until", "created_at"}
        assert cols["hidden_until"]["nullable"] is True
        assert inspector.get_pk_constraint("attention_hides")["constrained_columns"] == [
            "user_id", "item_key",
        ]
        with engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.execute(sa.text(
                "INSERT INTO attention_hides (user_id, item_key, version_key) "
                "VALUES ('u_m123', 'k1', 'v1')"
            ))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO attention_hides (user_id, item_key, version_key) "
                "VALUES ('u_m123', 'k1', 'v2')"
            ))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO attention_hides (user_id, item_key, version_key) "
                "VALUES ('u_m123', :k, 'v')"
            ), {"k": "x" * 201})
        with engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.execute(sa.text("DELETE FROM users WHERE id = 'u_m123'"))
            assert conn.execute(sa.text("SELECT count(*) FROM attention_hides")).scalar() == 0
    finally:
        engine.dispose()

    command.downgrade(config, "122")
    engine = sa.create_engine(f"sqlite:///{pre_123_db}")
    try:
        assert not sa.inspect(engine).has_table("attention_hides")
    finally:
        engine.dispose()
    command.upgrade(config, "123")


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
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "122")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m123', 'm123@example.com', true, 'free', now(), true)"
        ))

    _pg(url, seed)
    command.upgrade(config, "123")

    def check(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO attention_hides (user_id, item_key, version_key) "
            "VALUES ('u_m123', 'k1', 'v1')"
        ))
        for bad, params in (
            ("INSERT INTO attention_hides (user_id, item_key, version_key) "
             "VALUES ('u_m123', 'k1', 'v2')", {}),
            ("INSERT INTO attention_hides (user_id, item_key, version_key) "
             "VALUES ('u_missing', 'k2', 'v')", {}),
        ):
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad), params)
            nested.rollback()
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_m123'"))
        assert conn.execute(sa.text("SELECT count(*) FROM attention_hides")).scalar() == 0

    _pg(url, check)
    command.downgrade(config, "122")

    def gone(conn) -> None:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'attention_hides'"
        )).scalar() == 0

    _pg(url, gone)
    command.upgrade(config, "head")
