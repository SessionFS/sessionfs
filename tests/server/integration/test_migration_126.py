"""Migration 126: partial unique index on review rounds in agent_runs.

SQLite: the prior schema is the real metadata without the index, stamped at
125. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain (001-125)
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
_INDEX = "uq_agent_run_review_round"

_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m126', 'm126@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m126', 'p', 'acme/m126', 'u_m126', '')",
)


def _run(run_id: str, *, rnd: int | None, step: str | None = None) -> str:
    return (
        "INSERT INTO agent_runs (id, project_id, persona_name, tool, trigger_source, "
        "status, created_at, ticket_id, role, review_round, workflow_step_id) VALUES "
        f"('{run_id}', 'proj_m126', 'r', 'generic', 'manual', 'passed', "
        f"CURRENT_TIMESTAMP, 'tk_1', 'reviewer', "
        f"{'NULL' if rnd is None else rnd}, {'NULL' if step is None else repr(step)})"
    )


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _pre_126(path: Path, *extra: str) -> Config:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP INDEX {_INDEX}"))
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t=1)))
        for stmt in extra:
            conn.execute(sa.text(stmt))
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "125")
    return config


def _indexes(path: Path) -> set[str]:
    # Inspector skips expression indexes, so read sqlite_master directly.
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return {
                r[0]
                for r in conn.execute(sa.text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'agent_runs'"
                )).all()
            }
    finally:
        engine.dispose()


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m126.db"
    config = _pre_126(path)
    command.upgrade(config, "126")
    assert _INDEX in _indexes(path)
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_run("run_a", rnd=1)))
            conn.execute(sa.text(_run("run_b", rnd=1, step="review-2")))
            conn.execute(sa.text(_run("run_c", rnd=None)))
            conn.execute(sa.text(_run("run_d", rnd=None)))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_run("run_e", rnd=1)))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_run("run_f", rnd=1, step="review-2")))
    finally:
        engine.dispose()
    command.downgrade(config, "125")
    assert _INDEX not in _indexes(path)
    command.upgrade(config, "126")


def test_existing_duplicates_skip_the_index_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "m126dup.db"
    config = _pre_126(path, _run("run_a", rnd=1), _run("run_b", rnd=1))
    command.upgrade(config, "126")
    assert _INDEX not in _indexes(path)
    assert "NOT created" in capsys.readouterr().out
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM agent_runs")).scalar() == 2
    finally:
        engine.dispose()
    command.downgrade(config, "125")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "125")

    async def run(work):
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(work)
        finally:
            await engine.dispose()

    def seed(conn) -> None:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))
        conn.execute(sa.text(_run("run_a", rnd=1)))

    asyncio.run(run(seed))
    command.upgrade(config, "126")

    def check(conn) -> None:
        conn.execute(sa.text(_run("run_b", rnd=1, step="review-2")))
        conn.execute(sa.text(_run("run_c", rnd=None)))
        conn.execute(sa.text(_run("run_d", rnd=None)))
        for bad in (_run("run_e", rnd=1), _run("run_f", rnd=1, step="review-2")):
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad))
            nested.rollback()

    asyncio.run(run(check))
    command.downgrade(config, "125")

    def gone(conn) -> set[str]:
        return {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_runs'"
            )).all()
        }

    assert _INDEX not in asyncio.run(run(gone))
    command.upgrade(config, "head")
