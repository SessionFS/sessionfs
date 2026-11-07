"""Migration 124: workflow templates, step children, agent_runs.workflow_step_id.

SQLite: the prior schema is the real metadata with the two workflow tables
dropped and ``agent_runs`` without ``workflow_step_id``, stamped at 123.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain (001-123) when
SFS_TEST_PG_URL is set and asserts every CHECK and unique key.
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


def _columns(engine: sa.Engine, table: str) -> dict[str, dict]:
    inspector = sa.inspect(engine)
    if not inspector.has_table(table):
        return {}
    return {c["name"]: c for c in inspector.get_columns(table)}


_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m123', 'm123@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m123', 'p', 'acme/m123', 'u_m123', '')",
    "INSERT INTO tickets (id, project_id, title, created_by_user_id, created_at, updated_at) "
    "VALUES ('tk_loop', 'proj_m123', 'loop', 'u_m123', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO tickets (id, project_id, title, created_by_user_id, created_at, updated_at) "
    "VALUES ('tk_c1', 'proj_m123', 'c1', 'u_m123', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO tickets (id, project_id, title, created_by_user_id, created_at, updated_at) "
    "VALUES ('tk_c2', 'proj_m123', 'c2', 'u_m123', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
)

_GOOD = (
    "INSERT INTO workflow_templates (id, project_id, name, lineage_id, version, definition) "
    "VALUES ('wft_1', 'proj_m123', 'n', 'wfl_1', 1, '{}')",
    "INSERT INTO workflow_step_children (id, project_id, loop_ticket_id, ticket_id, step_id, step_index) "
    "VALUES ('wsc_1', 'proj_m123', 'tk_loop', 'tk_c1', 'implement', 0)",
)

_BAD = (
    # neither scope
    "INSERT INTO workflow_templates (id, name, lineage_id, version, definition) "
    "VALUES ('wft_b1', 'n', 'wfl_b1', 1, '{}')",
    # both scopes
    "INSERT INTO workflow_templates (id, org_id, project_id, name, lineage_id, version, definition) "
    "VALUES ('wft_b2', 'org_x', 'proj_m123', 'n', 'wfl_b2', 1, '{}')",
    # second active row in the same lineage
    "INSERT INTO workflow_templates (id, project_id, name, lineage_id, version, definition) "
    "VALUES ('wft_b3', 'proj_m123', 'n', 'wfl_1', 2, '{}')",
    # oversized definition
    "INSERT INTO workflow_templates (id, project_id, name, lineage_id, version, definition) "
    "VALUES ('wft_b4', 'proj_m123', 'n', 'wfl_b4', 1, '" + "x" * 32769 + "')",
    # version 0
    "INSERT INTO workflow_templates (id, project_id, name, lineage_id, version, definition) "
    "VALUES ('wft_b5', 'proj_m123', 'n', 'wfl_b5', 0, '{}')",
    # duplicate step slot
    "INSERT INTO workflow_step_children (id, project_id, loop_ticket_id, ticket_id, step_id, step_index) "
    "VALUES ('wsc_b1', 'proj_m123', 'tk_loop', 'tk_c2', 'implement', 0)",
    # the same ticket in two slots
    "INSERT INTO workflow_step_children (id, project_id, loop_ticket_id, ticket_id, step_id, step_index) "
    "VALUES ('wsc_b2', 'proj_m123', 'tk_loop', 'tk_c1', 'implement', 1)",
    # fan-out index out of range
    "INSERT INTO workflow_step_children (id, project_id, loop_ticket_id, ticket_id, step_id, step_index) "
    "VALUES ('wsc_b3', 'proj_m123', 'tk_loop', 'tk_c2', 'implement', 10)",
)


@pytest.fixture
def pre_124_db(tmp_path: Path) -> Path:
    path = tmp_path / "m124.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE workflow_step_children"))
        conn.execute(sa.text("DROP TABLE workflow_templates"))
        conn.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN workflow_step_id"))
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t=1)))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "123")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_124_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_124_db}")
    command.upgrade(config, "124")
    engine = sa.create_engine(f"sqlite:///{pre_124_db}")
    try:
        assert {
            "id", "org_id", "project_id", "name", "lineage_id", "version",
            "definition", "active", "supersedes_id", "created_by_user_id", "created_at",
        } <= set(_columns(engine, "workflow_templates"))
        assert {"loop_ticket_id", "ticket_id", "step_id", "step_index"} <= set(
            _columns(engine, "workflow_step_children")
        )
        assert _columns(engine, "agent_runs")["workflow_step_id"]["nullable"] is True
        with engine.begin() as conn:
            for stmt in _GOOD:
                conn.execute(sa.text(stmt))
            assert conn.execute(sa.text(
                "SELECT active FROM workflow_templates WHERE id = 'wft_1'"
            )).scalar() in (1, True)
            # An inactive older version beside the active one is fine.
            conn.execute(sa.text(
                "INSERT INTO workflow_templates (id, project_id, name, lineage_id, version, definition, active) "
                "VALUES ('wft_0', 'proj_m123', 'n', 'wfl_1', 2, '{}', 0)"
            ))
        for bad in _BAD:
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(bad))
    finally:
        engine.dispose()

    command.downgrade(config, "123")
    engine = sa.create_engine(f"sqlite:///{pre_124_db}")
    try:
        assert not _columns(engine, "workflow_templates")
        assert not _columns(engine, "workflow_step_children")
        assert "workflow_step_id" not in _columns(engine, "agent_runs")
    finally:
        engine.dispose()
    command.upgrade(config, "124")


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
    """Against the full real prior chain (001-123) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "123")

    def seed(conn) -> None:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))

    _pg(url, seed)
    command.upgrade(config, "124")

    def check(conn) -> None:
        for stmt in _GOOD:
            conn.execute(sa.text(stmt))
        assert conn.execute(sa.text(
            "SELECT active FROM workflow_templates WHERE id = 'wft_1'"
        )).scalar() is True
        conn.execute(sa.text(
            "INSERT INTO agent_runs (id, project_id, persona_name, tool, trigger_source, status, "
            "created_at, workflow_step_id) VALUES ('run_m123', 'proj_m123', 'p', 'generic', 'manual', "
            "'queued', now(), 'implement')"
        ))
        for bad in _BAD:
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad))
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, "123")

    def gone(conn) -> None:
        tables = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )).all()
        }
        assert "workflow_templates" not in tables
        assert "workflow_step_children" not in tables
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_runs'"
            )).all()
        }
        assert "workflow_step_id" not in cols

    _pg(url, gone)
    command.upgrade(config, "head")
