"""Migration 121: meeting_action_items.owner_hint (nullable, additive).

SQLite: the prior schema is the real metadata with the column dropped, stamped
at 120. PostgreSQL: runs the real chain when SFS_TEST_PG_URL is set.
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


def _columns(engine: sa.Engine) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("meeting_action_items")}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m121.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE meeting_action_items DROP COLUMN owner_hint"))
    assert "owner_hint" not in _columns(engine)
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "120")

    command.upgrade(config, "121")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        col = _columns(engine)["owner_hint"]
        assert col["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, "120")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert "owner_hint" not in _columns(engine)
    finally:
        engine.dispose()


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


def _pg_owner_hint(conn) -> str | None:
    return conn.execute(sa.text(
        "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'meeting_action_items' AND column_name = 'owner_hint'"
    )).scalar_one_or_none()


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="needs SFS_TEST_PG_URL")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "120")
    assert _pg(url, _pg_owner_hint) is None
    command.upgrade(config, "121")
    assert _pg(url, _pg_owner_hint) == "YES"
    command.downgrade(config, "120")
    assert _pg(url, _pg_owner_hint) is None
