"""Migration 113: ``agent_runs.review_round`` (Review Loop R3, track D3).

SQLite: the prior schema is the real metadata minus the new column, stamped
at 112. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and asserts the column type and nullability.
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


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table(table):
            return set()
        return {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()


@pytest.fixture
def pre_113_db(tmp_path: Path) -> Path:
    path = tmp_path / "m113.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-113 schema: the real metadata minus the new column.
        conn.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN review_round"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "112")
    return path


def test_upgrade_adds_column_and_downgrade_reverses(pre_113_db: Path) -> None:
    assert "review_round" not in _columns(pre_113_db, "agent_runs")

    config = _cfg(f"sqlite+aiosqlite:///{pre_113_db}")
    command.upgrade(config, "113")
    assert "review_round" in _columns(pre_113_db, "agent_runs")

    command.downgrade(config, "112")
    assert "review_round" not in _columns(pre_113_db, "agent_runs")
    command.upgrade(config, "113")
    assert "review_round" in _columns(pre_113_db, "agent_runs")


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None, str]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-112) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "112")
    command.upgrade(config, "113")

    def check(conn) -> None:
        cols = _pg_columns(conn, "agent_runs")
        assert cols["review_round"] == ("integer", None, "YES"), cols["review_round"]

    _pg(url, check)
    command.downgrade(config, "112")

    def gone(conn) -> None:
        assert "review_round" not in _pg_columns(conn, "agent_runs")

    _pg(url, gone)
    command.upgrade(config, "head")
