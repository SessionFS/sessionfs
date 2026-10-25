"""Migration 085: agent_runs replay provenance + prompt/context (GitHub #95).

SQLite: the prior schema is the real metadata minus the new columns and
index. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
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
_NEW_COLUMNS = {"replayed_from", "prompt", "context_refs"}
_NEW_INDEXES = {"idx_agent_run_replayed_from"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _inspect(path: Path) -> tuple[set[str], set[str]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("agent_runs")}
        idx = {i["name"] for i in insp.get_indexes("agent_runs")}
        return cols, idx
    finally:
        engine.dispose()


@pytest.fixture
def pre_085_db(tmp_path: Path) -> Path:
    path = tmp_path / "m085.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name in _NEW_INDEXES:
            conn.execute(sa.text(f"DROP INDEX {name}"))
        for column in ("replayed_from", "prompt", "context_refs"):
            conn.execute(sa.text(f"ALTER TABLE agent_runs DROP COLUMN {column}"))
        # SQLite does not enforce the project_id FK here; no projects row
        # needed. The non-NULL columns all carry server defaults.
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, project_id, persona_name) "
            "VALUES ('run_old', 'proj_x', 'atlas')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "084")
    return path


def test_upgrade_adds_columns_and_downgrade_reverses(pre_085_db: Path) -> None:
    cols, idx = _inspect(pre_085_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)

    config = _cfg(f"sqlite+aiosqlite:///{pre_085_db}")
    command.upgrade(config, "085")
    cols, idx = _inspect(pre_085_db)
    assert _NEW_COLUMNS <= cols
    assert _NEW_INDEXES <= idx

    engine = sa.create_engine(f"sqlite:///{pre_085_db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT replayed_from, prompt, context_refs "
                "FROM agent_runs WHERE id = 'run_old'"
            )).one()
        # Every pre-085 row is a non-replay; all three columns are NULL.
        assert tuple(row) == (None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, "084")
    cols, idx = _inspect(pre_085_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)

    command.upgrade(config, "085")
    cols, _ = _inspect(pre_085_db)
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
    """Against the full real prior chain (001-083) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "084")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg085', 'pg085@example.com', 'u', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_pg085', 'p', 'github.com/x/pg085', '', 'u_pg085')"
        ))
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, project_id, persona_name) "
            "VALUES ('run_pg085', 'proj_pg085', 'atlas')"
        ))

    _pg(url, seed)
    command.upgrade(config, "085")

    def check(conn) -> None:
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'agent_runs'"
            ))
        }
        assert cols["replayed_from"] == ("YES", "character varying")
        assert cols["prompt"] == ("YES", "text")
        assert cols["context_refs"] == ("YES", "text")
        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_runs'"
            ))
        }
        assert _NEW_INDEXES <= indexes
        row = conn.execute(sa.text(
            "SELECT replayed_from, prompt, context_refs FROM agent_runs WHERE id = 'run_pg085'"
        )).one()
        assert tuple(row) == (None, None, None)

    _pg(url, check)
    command.downgrade(config, "084")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_runs'"
            ))
        }
        assert not (_NEW_COLUMNS & cols)
        assert conn.execute(sa.text("SELECT count(*) FROM agent_runs")).scalar_one() == 1

    _pg(url, gone)
    command.upgrade(config, "head")
