"""Migration 105: helper_usage.source tag (GitHub #116, M1b).

SQLite: simulate the pre-105 schema by dropping the column from the current
metadata table, stamp 104, then exercise upgrade/downgrade, the backfill
default for existing rows and the CHECK. PostgreSQL: run the real chain when
SFS_TEST_PG_URL is set.
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


def _columns(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("helper_usage")}
    finally:
        engine.dispose()


def _checks(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_check_constraints("helper_usage")}
    finally:
        engine.dispose()


_INSERT = (
    "INSERT INTO helper_usage (id, user_id, key_source, input_tokens, output_tokens{extra}) "
    "VALUES (:id, 'u1', 'user', 1, 1{extra_val})"
)


@pytest.fixture
def pre_105_db(tmp_path: Path) -> Path:
    path = tmp_path / "m105.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    # The pre-105 helper_usage: no source column and no source CHECK.
    meta = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(meta)
    usage = meta.tables["helper_usage"]
    usage.constraints.discard(
        next(c for c in usage.constraints if getattr(c, "name", None) == "ck_helper_usage_source")
    )
    usage._columns.remove(usage.c.source)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(_INSERT.format(extra="", extra_val="")), {"id": "hu_old"})
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "104")
    return path


def test_upgrade_adds_source_with_default_and_check(pre_105_db: Path) -> None:
    assert "source" not in _columns(pre_105_db)

    config = _cfg(f"sqlite+aiosqlite:///{pre_105_db}")
    command.upgrade(config, "105")

    assert "source" in _columns(pre_105_db)
    assert "ck_helper_usage_source" in _checks(pre_105_db)

    engine = sa.create_engine(f"sqlite:///{pre_105_db}")
    try:
        with engine.begin() as conn:
            # Existing rows default to 'helper'.
            assert conn.execute(
                sa.text("SELECT source FROM helper_usage WHERE id = 'hu_old'")
            ).scalar_one() == "helper"
            conn.execute(
                sa.text(_INSERT.format(extra=", source", extra_val=", 'meeting_extract'")),
                {"id": "hu_m"},
            )
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(_INSERT.format(extra=", source", extra_val=", 'bogus'")),
                    {"id": "hu_bad"},
                )
    finally:
        engine.dispose()

    command.downgrade(config, "104")
    assert "source" not in _columns(pre_105_db)
    assert "ck_helper_usage_source" not in _checks(pre_105_db)

    command.upgrade(config, "105")
    assert "source" in _columns(pre_105_db)


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "104")
    command.upgrade(config, "105")

    async def _go() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                row = (await conn.execute(sa.text(
                    "SELECT data_type, character_maximum_length, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'helper_usage' AND column_name = 'source'"
                ))).one()
                assert row[0] == "character varying" and row[1] == 32
                assert "helper" in row[2]
                checks = {r[0] for r in (await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "JOIN pg_class ON pg_class.oid = conrelid "
                    "WHERE pg_class.relname = 'helper_usage' AND contype = 'c'"
                ))).all()}
                assert "ck_helper_usage_source" in checks
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, "104")
    command.upgrade(config, "head")
