"""Migration 089: scheduled KB compile settings + compile trigger (GitHub #93).

SQLite: the prior schema is the real metadata minus the new columns.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
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

# table -> new column names
_NEW_COLUMNS = {
    "projects": (
        "compile_schedule",
        "compile_after_claims",
        "last_scheduled_compile_at",
        "last_decay_at",
    ),
    "context_compilations": ("trigger",),
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path) -> dict[str, set[str]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in _NEW_COLUMNS}
    finally:
        engine.dispose()


@pytest.fixture
def pre_089_db(tmp_path: Path) -> Path:
    path = tmp_path / "m089.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, columns in _NEW_COLUMNS.items():
            for column in columns:
                conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified, is_active, created_at) "
            "VALUES ('u_m089', 'm089@example.com', 'u', 'free', true, true, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, owner_id) VALUES ('p_m089', 'P', 'u_m089')"
        ))
        conn.execute(sa.text(
            "INSERT INTO context_compilations (project_id, user_id, entries_compiled) "
            "VALUES ('p_m089', 'u_m089', 3)"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "088")
    return path


def test_upgrade_adds_columns_backfills_defaults_and_downgrade_reverses(pre_089_db: Path) -> None:
    cols = _columns(pre_089_db)
    for table, columns in _NEW_COLUMNS.items():
        assert not (set(columns) & cols[table])

    config = _cfg(f"sqlite+aiosqlite:///{pre_089_db}")
    command.upgrade(config, "089")
    cols = _columns(pre_089_db)
    for table, columns in _NEW_COLUMNS.items():
        assert set(columns) <= cols[table]

    engine = sa.create_engine(f"sqlite:///{pre_089_db}")
    try:
        with engine.connect() as conn:
            project = conn.execute(sa.text(
                "SELECT compile_schedule, compile_after_claims, last_scheduled_compile_at, last_decay_at "
                "FROM projects WHERE id = 'p_m089'"
            )).one()
            # Pre-089 rows backfill to the server default 'off' (existing
            # projects opt OUT of the sweep; new projects opt in explicitly).
            assert tuple(project) == ("off", None, None, None)
            trigger = conn.execute(sa.text(
                "SELECT trigger FROM context_compilations WHERE project_id = 'p_m089'"
            )).scalar_one()
            # Every pre-089 compile was manual.
            assert trigger == "manual"
    finally:
        engine.dispose()

    command.downgrade(config, "088")
    cols = _columns(pre_089_db)
    for table, columns in _NEW_COLUMNS.items():
        assert not (set(columns) & cols[table])

    command.upgrade(config, "089")  # idempotent
    cols = _columns(pre_089_db)
    assert "compile_schedule" in cols["projects"]
    assert "trigger" in cols["context_compilations"]


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
    """Against the full real prior chain (001-083) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "088")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified, is_active, created_at) "
            "VALUES ('u_pg089', 'pg089@example.com', 'u', 'free', true, true, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, owner_id) VALUES ('p_pg089', 'P', 'u_pg089')"
        ))
        conn.execute(sa.text(
            "INSERT INTO context_compilations (project_id, user_id, entries_compiled) "
            "VALUES ('p_pg089', 'u_pg089', 3)"
        ))

    _pg(url, seed)
    command.upgrade(config, "089")

    def check(conn) -> None:
        def project_cols() -> dict:
            return {
                r[0]: (r[1], r[2])
                for r in conn.execute(sa.text(
                    "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                    "WHERE table_name = 'projects'"
                ))
            }
        cols = project_cols()
        assert cols["compile_schedule"] == ("NO", "character varying")
        assert cols["compile_after_claims"] == ("YES", "integer")
        assert cols["last_scheduled_compile_at"] == ("YES", "timestamp with time zone")
        assert cols["last_decay_at"] == ("YES", "timestamp with time zone")

        comp_cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'context_compilations'"
            ))
        }
        assert comp_cols["trigger"] == ("NO", "character varying")

        assert conn.execute(sa.text(
            "SELECT compile_schedule FROM projects WHERE id = 'p_pg089'"
        )).scalar_one() == "off"
        assert conn.execute(sa.text(
            "SELECT trigger FROM context_compilations WHERE project_id = 'p_pg089'"
        )).scalar_one() == "manual"

    _pg(url, check)
    command.downgrade(config, "088")

    def gone(conn) -> None:
        pcols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'"
            ))
        }
        assert not (set(_NEW_COLUMNS["projects"]) & pcols)
        ccols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'context_compilations'"
            ))
        }
        assert "trigger" not in ccols

    _pg(url, gone)
    command.upgrade(config, "head")
