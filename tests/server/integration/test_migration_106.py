"""Migration 106: agent-run record fields (Review Loop R3, track A).

SQLite: simulate the pre-106 schema by dropping the new columns (and their
inline CHECK constraints + index) from the current metadata, stamp 105, then
exercise upgrade/downgrade. PostgreSQL: run the real chain when
SFS_TEST_PG_URL is set and verify the column types, CHECK constraints and
the (project_id, parent_run_id) index.
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

_NEW_COLUMNS = frozenset({
    "model", "provider",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "cost_usd", "cost_basis",
    "role", "branch", "base_sha", "head_sha",
    "parent_run_id", "continues_run_id", "work_queue_id", "work_queue_item_id",
    "exit_reason", "occurred_at",
})

_CHECK_NAMES = (
    "ck_agent_run_cost_basis",
    "ck_agent_run_role",
    "ck_agent_run_exit_reason",
)

_INDEX_NAME = "idx_agent_run_project_parent"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("agent_runs")}
    finally:
        engine.dispose()


def _indexes(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {i["name"] for i in sa.inspect(engine).get_indexes("agent_runs")}
    finally:
        engine.dispose()


@pytest.fixture
def pre_106_db(tmp_path: Path) -> Path:
    path = tmp_path / "m106.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    # The pre-106 agent_runs table: none of the new columns, no inline
    # CHECKs (they reference the new columns) and no parent index.
    meta = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(meta)
    runs = meta.tables["agent_runs"]
    for name in _CHECK_NAMES:
        runs.constraints.discard(
            next(c for c in runs.constraints if getattr(c, "name", None) == name)
        )
    runs.indexes.discard(
        next(ix for ix in runs.indexes if getattr(ix, "name", None) == _INDEX_NAME)
    )
    for col in _NEW_COLUMNS:
        runs._columns.remove(runs.c[col])
    meta.create_all(engine)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "105")
    return path


def test_upgrade_adds_columns_and_index(pre_106_db: Path) -> None:
    assert not (_NEW_COLUMNS & _columns(pre_106_db))
    assert _INDEX_NAME not in _indexes(pre_106_db)

    config = _cfg(f"sqlite+aiosqlite:///{pre_106_db}")
    command.upgrade(config, "106")

    cols = _columns(pre_106_db)
    for col in _NEW_COLUMNS:
        assert col in cols
    assert _INDEX_NAME in _indexes(pre_106_db)

    command.downgrade(config, "105")
    assert not (_NEW_COLUMNS & _columns(pre_106_db))
    assert _INDEX_NAME not in _indexes(pre_106_db)

    command.upgrade(config, "106")
    for col in _NEW_COLUMNS:
        assert col in _columns(pre_106_db)


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "105")
    command.upgrade(config, "106")

    async def _go() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                rows = (await conn.execute(sa.text(
                    "SELECT column_name, data_type, numeric_precision, numeric_scale "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'agent_runs' AND column_name IN "
                    "('model', 'cost_usd', 'input_tokens', 'role')"
                ))).all()
                by_name = {r[0]: r for r in rows}
                assert by_name["model"][1] == "character varying"
                assert by_name["role"][1] == "character varying"
                assert by_name["cost_usd"][1] == "numeric"
                assert by_name["cost_usd"][2] == 12 and by_name["cost_usd"][3] == 6
                assert by_name["input_tokens"][1] == "bigint"

                checks = {r[0] for r in (await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "JOIN pg_class ON pg_class.oid = conrelid "
                    "WHERE pg_class.relname = 'agent_runs' AND contype = 'c'"
                ))).all()}
                assert set(_CHECK_NAMES) <= checks

                indexes = {r[0] for r in (await conn.execute(sa.text(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_runs'"
                ))).all()}
                assert _INDEX_NAME in indexes
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, "105")
    command.upgrade(config, "head")
