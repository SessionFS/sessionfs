"""Migration 125: work_queue_items.prior_implementer_service_key_ids.

SQLite: the prior schema is the real metadata without the column, stamped at
124. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain (001-124)
when SFS_TEST_PG_URL is set.
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
_COLUMN = "prior_implementer_service_key_ids"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m125.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE work_queue_items DROP COLUMN {_COLUMN}"))
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "124")

    command.upgrade(config, "125")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert _columns(engine, "work_queue_items")[_COLUMN]["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, "124")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert _COLUMN not in _columns(engine, "work_queue_items")
    finally:
        engine.dispose()
    command.upgrade(config, "125")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "124")
    command.upgrade(config, "125")

    async def cols() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                rows = await conn.execute(sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'work_queue_items'"
                ))
                return {r[0] for r in rows.all()}
        finally:
            await engine.dispose()

    assert _COLUMN in asyncio.run(cols())
    command.downgrade(config, "124")
    assert _COLUMN not in asyncio.run(cols())
    command.upgrade(config, "head")
