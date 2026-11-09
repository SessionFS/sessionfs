"""Migration 136: org_concurrency_slots.

SQLite: the prior schema is the real metadata with ``org_concurrency_slots``
dropped, stamped at the previous revision. PostgreSQL: ``test_upgrade_on_postgres``
runs the real chain when SFS_TEST_PG_URL is set.
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
_VERSIONS = Path(_MIGRATIONS) / "versions"
_COLUMNS = {"scope_key", "route_class", "slot_no", "holder", "acquired_at", "expires_at"}
_INSERT = (
    "INSERT INTO org_concurrency_slots "
    "(scope_key, route_class, slot_no, holder, acquired_at, expires_at) "
    "VALUES (:k, 'compile', 0, :h, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def _previous() -> str:
    source = (_VERSIONS / "136_org_concurrency_slots.py").read_text()
    for line in source.splitlines():
        if line.startswith("down_revision"):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("no down_revision")


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_136_db(tmp_path: Path) -> Path:
    path = tmp_path / "m136.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE org_concurrency_slots"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _previous())
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_136_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_136_db}")
    command.upgrade(config, "136")
    engine = sa.create_engine(f"sqlite:///{pre_136_db}")
    try:
        inspector = sa.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("org_concurrency_slots")} == _COLUMNS
        assert inspector.get_pk_constraint("org_concurrency_slots")["constrained_columns"] == [
            "scope_key",
            "route_class",
            "slot_no",
        ]
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"k": "org:a", "h": "x"})
        # One holder per numbered slot.
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"k": "org:a", "h": "y"})
    finally:
        engine.dispose()

    command.downgrade(config, _previous())
    engine = sa.create_engine(f"sqlite:///{pre_136_db}")
    try:
        assert not sa.inspect(engine).has_table("org_concurrency_slots")
    finally:
        engine.dispose()
    command.upgrade(config, "136")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, _previous())
    command.upgrade(config, "136")

    def check(conn) -> None:
        cols = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'org_concurrency_slots'"
            ))
        }
        assert set(cols) == _COLUMNS
        assert cols["expires_at"] == "timestamp with time zone"
        conn.execute(sa.text(_INSERT), {"k": "org:a", "h": "x"})
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(_INSERT), {"k": "org:a", "h": "y"})
        nested.rollback()

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(check)
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, _previous())
    command.upgrade(config, "head")
