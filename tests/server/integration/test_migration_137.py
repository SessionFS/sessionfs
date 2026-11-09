"""Migration 137: merge_check_posts (ordering of merge check posts).

SQLite: the prior schema is the real metadata with ``merge_check_posts``
dropped, stamped at 136. PostgreSQL: ``test_upgrade_on_postgres`` runs the
real chain when SFS_TEST_PG_URL is set.
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
    "project_id",
    "provider",
    "repo",
    "head_sha",
    "eval_seq",
    "posted_seq",
    "posted_state",
    "posted_summary",
    "updated_at",
}
_PROJECT = (
    "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
    "VALUES ('proj_m137', 'm137', 'acme/m137', '', 'u_m137')"
)
_INSERT = (
    "INSERT INTO merge_check_posts (project_id, provider, repo, head_sha) "
    "VALUES ('proj_m137', :p, 'acme/m137', :sha)"
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_137_db(tmp_path: Path) -> Path:
    path = tmp_path / "m137.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE merge_check_posts"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "136")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_137_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_137_db}")
    command.upgrade(config, "137")
    engine = sa.create_engine(f"sqlite:///{pre_137_db}")
    try:
        inspector = sa.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("merge_check_posts")} == _COLUMNS
        assert inspector.get_pk_constraint("merge_check_posts")["constrained_columns"] == [
            "project_id", "provider", "repo", "head_sha",
        ]
        with engine.begin() as conn:
            conn.execute(sa.text(_PROJECT))
            conn.execute(sa.text(_INSERT), {"p": "github", "sha": "a" * 40})
            row = conn.execute(
                sa.text("SELECT eval_seq, posted_seq, updated_at FROM merge_check_posts")
            ).one()
            assert row[0] == 0 and row[1] == 0 and row[2] is not None
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"p": "github", "sha": "a" * 40})
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"p": "bitbucket", "sha": "b" * 40})
    finally:
        engine.dispose()

    command.downgrade(config, "136")
    engine = sa.create_engine(f"sqlite:///{pre_137_db}")
    try:
        assert not sa.inspect(engine).has_table("merge_check_posts")
    finally:
        engine.dispose()
    command.upgrade(config, "137")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "136")
    command.upgrade(config, "137")

    def check(conn) -> None:
        cols = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'merge_check_posts'"
            ))
        }
        assert set(cols) == _COLUMNS
        assert cols["eval_seq"] == "bigint"

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(check)
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, "136")
    command.upgrade(config, "137")
