"""Migration 101: tickets.assigned_user_id (GitHub #96).

SQLite: the prior schema is the real metadata minus the new column and
index. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and checks the SET NULL FK.
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
        return {c["name"] for c in sa.inspect(engine).get_columns("tickets")}
    finally:
        engine.dispose()


def _indexes(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {i["name"] for i in sa.inspect(engine).get_indexes("tickets")}
    finally:
        engine.dispose()


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def strip_101(conn: sa.Connection) -> None:
    """Rebuild tickets (SQLite) without the 101 column and index, keeping
    its other indexes and columns."""
    conn.execute(sa.text("DROP INDEX idx_ticket_assigned_user"))
    ddl = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'tickets'")
    ).scalar_one()
    indexes = [
        r[0]
        for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'tickets' AND sql IS NOT NULL"
        ))
    ]
    conn.execute(sa.text("DROP TABLE tickets"))
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    kept = [p.strip() for p in _split_top_level(body) if "assigned_user_id" not in p]
    conn.execute(sa.text(f"CREATE TABLE tickets ({', '.join(kept)})"))
    for sql in indexes:
        conn.execute(sa.text(sql))


@pytest.fixture
def pre_101_db(tmp_path: Path) -> Path:
    path = tmp_path / "m101.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        strip_101(conn)
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m101', 'o', 'o', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified, created_at, is_active) "
            "VALUES ('u_m101', 'm101@example.com', 'u', 'team', true, CURRENT_TIMESTAMP, true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_m101', 'p', 'github.com/x/m101', '', 'u_m101')"
        ))
        conn.execute(sa.text(
            "INSERT INTO tickets (id, project_id, title, created_by_user_id, status) "
            "VALUES ('tk_old', 'proj_m101', 'old', 'u_m101', 'open')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "100")
    return path


def test_upgrade_adds_null_column_and_index_and_downgrade_reverses(pre_101_db: Path) -> None:
    assert "assigned_user_id" not in _columns(pre_101_db)
    assert "idx_ticket_assigned_user" not in _indexes(pre_101_db)
    config = _cfg(f"sqlite+aiosqlite:///{pre_101_db}")
    command.upgrade(config, "101")
    assert "assigned_user_id" in _columns(pre_101_db)
    assert "idx_ticket_assigned_user" in _indexes(pre_101_db)
    engine = sa.create_engine(f"sqlite:///{pre_101_db}")
    try:
        with engine.connect() as conn:
            value = conn.execute(sa.text(
                "SELECT assigned_user_id FROM tickets WHERE id = 'tk_old'"
            )).scalar_one()
        # Existing tickets are unassigned (person) on upgrade.
        assert value is None
    finally:
        engine.dispose()
    command.downgrade(config, "100")
    assert "assigned_user_id" not in _columns(pre_101_db)
    assert "idx_ticket_assigned_user" not in _indexes(pre_101_db)
    command.upgrade(config, "101")
    assert "assigned_user_id" in _columns(pre_101_db)


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
    """Against the full real prior chain (001-100) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "100")
    command.upgrade(config, "101")

    def check(conn) -> None:
        names = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'tickets'::regclass"
            ))
        }
        assert "fk_tickets_assigned_user" in names
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_pg101', 'o', 'o-pg101', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg101_owner', 'pg101-owner@example.com', 'owner', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg101_assignee', 'pg101-assignee@example.com', 'assignee', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_pg101', 'p', 'github.com/x/pg101', '', 'u_pg101_owner')"
        ))
        conn.execute(sa.text(
            "INSERT INTO tickets (id, project_id, title, created_by_user_id, assigned_user_id, status) "
            "VALUES ('tk_pg101', 'proj_pg101', 't', 'u_pg101_owner', 'u_pg101_assignee', 'open')"
        ))
        # Deleting the assignee clears the assignment instead of failing or cascading.
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg101_assignee'"))
        value = conn.execute(sa.text(
            "SELECT assigned_user_id FROM tickets WHERE id = 'tk_pg101'"
        )).scalar_one()
        assert value is None
        conn.execute(sa.text("DELETE FROM tickets WHERE id = 'tk_pg101'"))
        conn.execute(sa.text("DELETE FROM projects WHERE id = 'proj_pg101'"))
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg101_owner'"))
        conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg101'"))

    _pg(url, check)
    command.downgrade(config, "100")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tickets'"
            ))
        }
        assert "assigned_user_id" not in cols

    _pg(url, gone)
    command.upgrade(config, "head")
