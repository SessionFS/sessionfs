"""Migration 127: pull_requests head-sha index + delivery event age index."""

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
_INDEXES = {
    "pull_requests": "idx_pull_requests_project_head",
    "delivery_webhook_events": "idx_delivery_webhook_events_received",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _names(engine: sa.Engine, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m127.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name in _INDEXES.values():
            conn.execute(sa.text(f"DROP INDEX {name}"))
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "126")
    command.upgrade(config, "127")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        for table, name in _INDEXES.items():
            assert name in _names(engine, table)
    finally:
        engine.dispose()
    command.downgrade(config, "126")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        for table, name in _INDEXES.items():
            assert name not in _names(engine, table)
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "127")

    async def names() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                rows = await conn.execute(sa.text("SELECT indexname FROM pg_indexes"))
                return {r[0] for r in rows.all()}
        finally:
            await engine.dispose()

    assert set(_INDEXES.values()) <= asyncio.run(names())
    command.downgrade(config, "126")
    assert not set(_INDEXES.values()) & asyncio.run(names())
    command.upgrade(config, "head")
