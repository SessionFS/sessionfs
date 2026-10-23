"""Migration 081: operator context on admin_actions (GitHub #84).

SQLite: the prior schema is the real metadata minus the new columns and
indexes. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
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
_NEW_COLUMNS = {"reason", "before_state", "after_state", "source"}
_NEW_INDEXES = {"idx_admin_actions_created_at", "idx_admin_actions_target"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _inspect(path: Path) -> tuple[set[str], set[str]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("admin_actions")}
        idx = {i["name"] for i in insp.get_indexes("admin_actions")}
        return cols, idx
    finally:
        engine.dispose()


@pytest.fixture
def pre_081_db(tmp_path: Path) -> Path:
    path = tmp_path / "m081.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name in _NEW_INDEXES:
            conn.execute(sa.text(f"DROP INDEX {name}"))
        for column in ("reason", "before_state", "after_state", "source"):
            conn.execute(sa.text(f"ALTER TABLE admin_actions DROP COLUMN {column}"))
        # SQLite does not enforce the admin_id FK here; no users row needed.
        conn.execute(sa.text(
            "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id, details) "
            "VALUES ('aa_old', 'u_m081', 'tier_change', 'user', 'u_x', '{}')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "080")
    return path


def test_upgrade_adds_columns_and_downgrade_reverses(pre_081_db: Path) -> None:
    cols, idx = _inspect(pre_081_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)

    config = _cfg(f"sqlite+aiosqlite:///{pre_081_db}")
    command.upgrade(config, "081")
    cols, idx = _inspect(pre_081_db)
    assert _NEW_COLUMNS <= cols
    assert _NEW_INDEXES <= idx

    engine = sa.create_engine(f"sqlite:///{pre_081_db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT source, reason, before_state, after_state "
                "FROM admin_actions WHERE id = 'aa_old'"
            )).one()
        # Every pre-081 row was written by the server.
        assert tuple(row) == ("api", None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, "080")
    cols, idx = _inspect(pre_081_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)

    command.upgrade(config, "081")
    cols, _ = _inspect(pre_081_db)
    assert _NEW_COLUMNS <= cols


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
    """Against the full real prior chain (001-080) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "080")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg081', 'pg081@example.com', 'u', 'admin', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id) "
            "VALUES ('aa_pg081', 'u_pg081', 'verify', 'user', 'u_pg081')"
        ))

    _pg(url, seed)
    command.upgrade(config, "081")

    def check(conn) -> None:
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'admin_actions'"
            ))
        }
        assert cols["reason"] == ("YES", "text")
        assert cols["before_state"] == ("YES", "text")
        assert cols["after_state"] == ("YES", "text")
        assert cols["source"] == ("NO", "character varying")
        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'admin_actions'"
            ))
        }
        assert _NEW_INDEXES <= indexes
        assert conn.execute(sa.text(
            "SELECT source FROM admin_actions WHERE id = 'aa_pg081'"
        )).scalar_one() == "api"
        conn.execute(sa.text(
            "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id, "
            "reason, before_state, after_state, source) VALUES ('aa_pg081b', 'u_pg081', "
            "'user_merge', 'user', 'u_dup', 'typo signup', '{\"a\": 1}', '{\"a\": 2}', "
            "'operator_record')"
        ))

    _pg(url, check)
    command.downgrade(config, "080")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'admin_actions'"
            ))
        }
        assert not (_NEW_COLUMNS & cols)
        assert conn.execute(sa.text("SELECT count(*) FROM admin_actions")).scalar_one() == 2

    _pg(url, gone)
    command.upgrade(config, "head")
