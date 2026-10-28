"""Migration 102: ticket due dates, estimates and the event log (GitHub #113).

SQLite: the prior schema is the real metadata minus the two scheduling
columns and the ``ticket_events`` table. PostgreSQL: ``test_upgrade_on_postgres``
runs the real chain when ``SFS_TEST_PG_URL`` is set and checks the CASCADE FK
from ``ticket_events.ticket_id``.
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


def _tables(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _event_columns(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("ticket_events")}
    finally:
        engine.dispose()


def _event_indexes(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {i["name"] for i in sa.inspect(engine).get_indexes("ticket_events")}
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


def strip_102(conn: sa.Connection) -> None:
    """Rebuild the schema (SQLite) without the 102 columns and event table."""
    # Dropping ticket_events also drops its index in SQLite.
    conn.execute(sa.text("DROP TABLE ticket_events"))
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
    kept = [
        p.strip()
        for p in _split_top_level(body)
        if "due_date" not in p and "estimated_effort" not in p
    ]
    conn.execute(sa.text(f"CREATE TABLE tickets ({', '.join(kept)})"))
    for sql in indexes:
        conn.execute(sa.text(sql))


@pytest.fixture
def pre_102_db(tmp_path: Path) -> Path:
    path = tmp_path / "m102.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        strip_102(conn)
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m102', 'o', 'o', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified, created_at, is_active) "
            "VALUES ('u_m102', 'm102@example.com', 'u', 'team', true, CURRENT_TIMESTAMP, true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_m102', 'p', 'github.com/x/m102', '', 'u_m102')"
        ))
        conn.execute(sa.text(
            "INSERT INTO tickets (id, project_id, title, created_by_user_id, status) "
            "VALUES ('tk_old', 'proj_m102', 'old', 'u_m102', 'open')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "101")
    return path


def test_upgrade_adds_columns_and_events_table_and_downgrade_reverses(pre_102_db: Path) -> None:
    assert "due_date" not in _columns(pre_102_db)
    assert "estimated_effort" not in _columns(pre_102_db)
    assert "ticket_events" not in _tables(pre_102_db)

    config = _cfg(f"sqlite+aiosqlite:///{pre_102_db}")
    command.upgrade(config, "102")
    assert "due_date" in _columns(pre_102_db)
    assert "estimated_effort" in _columns(pre_102_db)
    assert "ticket_events" in _tables(pre_102_db)
    assert {
        "seq", "id", "ticket_id", "project_id", "action", "field", "before_value",
        "after_value", "actor_type", "actor_user_id", "actor_persona",
        "actor_label", "created_at",
    } <= _event_columns(pre_102_db)
    assert "idx_ticket_events_ticket_seq" in _event_indexes(pre_102_db)

    engine = sa.create_engine(f"sqlite:///{pre_102_db}")
    try:
        with engine.connect() as conn:
            due = conn.execute(sa.text(
                "SELECT due_date FROM tickets WHERE id = 'tk_old'"
            )).scalar_one()
            effort = conn.execute(sa.text(
                "SELECT estimated_effort FROM tickets WHERE id = 'tk_old'"
            )).scalar_one()
        # Existing tickets carry no scheduling fields on upgrade (no backfill).
        assert due is None
        assert effort is None
    finally:
        engine.dispose()

    command.downgrade(config, "101")
    assert "due_date" not in _columns(pre_102_db)
    assert "estimated_effort" not in _columns(pre_102_db)
    assert "ticket_events" not in _tables(pre_102_db)
    command.upgrade(config, "102")
    assert "ticket_events" in _tables(pre_102_db)


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
    """Against the full real prior chain (001-101) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "101")
    command.upgrade(config, "102")

    def check(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tickets'"
            ))
        }
        assert {"due_date", "estimated_effort"} <= cols

        event_cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'ticket_events'"
            ))
        }
        assert {
            "seq", "id", "ticket_id", "project_id", "action", "field", "before_value",
            "after_value", "actor_type", "actor_user_id", "actor_persona",
            "actor_label", "created_at",
        } <= event_cols

        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'ticket_events'"
            ))
        }
        assert "idx_ticket_events_ticket_seq" in indexes

        # The event's project_id is denormalised; deleting the ticket cascades.
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_pg102', 'o', 'o-pg102', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg102', 'pg102@example.com', 'owner', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_pg102', 'p', 'github.com/x/pg102', '', 'u_pg102')"
        ))
        conn.execute(sa.text(
            "INSERT INTO tickets (id, project_id, title, created_by_user_id, status, due_date, estimated_effort) "
            "VALUES ('tk_pg102', 'proj_pg102', 't', 'u_pg102', 'open', '2026-11-15', '3d')"
        ))
        conn.execute(sa.text(
            "INSERT INTO ticket_events (id, ticket_id, project_id, action, actor_type) "
            "VALUES ('evt_pg102', 'tk_pg102', 'proj_pg102', 'created', 'user')"
        ))
        # seq is a real autoincrement identity/sequence, not null.
        seq = conn.execute(sa.text(
            "SELECT seq FROM ticket_events WHERE id = 'evt_pg102'"
        )).scalar_one()
        assert isinstance(seq, int) and seq >= 1
        row = conn.execute(sa.text(
            "SELECT due_date, estimated_effort FROM tickets WHERE id = 'tk_pg102'"
        )).one()
        assert row[0].isoformat() == "2026-11-15"
        assert row[1] == "3d"

        conn.execute(sa.text("DELETE FROM tickets WHERE id = 'tk_pg102'"))
        remaining = conn.execute(sa.text(
            "SELECT count(*) FROM ticket_events WHERE ticket_id = 'tk_pg102'"
        )).scalar_one()
        assert remaining == 0
        conn.execute(sa.text("DELETE FROM projects WHERE id = 'proj_pg102'"))
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg102'"))
        conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg102'"))

    _pg(url, check)
    command.downgrade(config, "101")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tickets'"
            ))
        }
        assert "due_date" not in cols
        assert "estimated_effort" not in cols
        tables = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ))
        }
        assert "ticket_events" not in tables

    _pg(url, gone)
    command.upgrade(config, "head")
