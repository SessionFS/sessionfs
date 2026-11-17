"""Migration 138: deferred_jobs (durable post-response work, tk_e3eecad8c3874d92).

SQLite: the prior schema is the real metadata with ``deferred_jobs`` dropped,
stamped at 137. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain
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
_COLUMNS = {
    "kind",
    "subject_id",
    "generation",
    "requested_at",
    "available_at",
    "claim_token",
    "attempts",
    "last_error",
    "payload",
    "cursor",
}
_INSERT = (
    "INSERT INTO deferred_jobs (kind, subject_id, requested_at) "
    "VALUES ('merge_recheck', 'tk_m138', CURRENT_TIMESTAMP)"
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_138_db(tmp_path: Path) -> Path:
    path = tmp_path / "m138.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE deferred_jobs"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "137")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_138_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_138_db}")
    command.upgrade(config, "138")
    engine = sa.create_engine(f"sqlite:///{pre_138_db}")
    try:
        inspector = sa.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("deferred_jobs")} == _COLUMNS
        assert inspector.get_pk_constraint("deferred_jobs")["constrained_columns"] == [
            "kind", "subject_id",
        ]
        assert "idx_deferred_jobs_kind_requested" in {
            i["name"] for i in inspector.get_indexes("deferred_jobs")
        }
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT))
            row = conn.execute(
                sa.text("SELECT generation, attempts, claim_token FROM deferred_jobs")
            ).one()
            assert row[0] == 1 and row[1] == 0 and row[2] is None
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT))
    finally:
        engine.dispose()

    command.downgrade(config, "137")
    engine = sa.create_engine(f"sqlite:///{pre_138_db}")
    try:
        assert not sa.inspect(engine).has_table("deferred_jobs")
    finally:
        engine.dispose()
    command.upgrade(config, "138")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "137")
    command.upgrade(config, "138")

    def check(conn) -> None:
        cols = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'deferred_jobs'"
            ))
        }
        assert set(cols) == _COLUMNS
        assert cols["generation"] == "bigint"

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(check)
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, "137")
    command.upgrade(config, "138")
