"""Migration 119: reminders + ticket notifications generalised by subject.

SQLite: the prior schema is the real metadata with the two reminder tables
dropped and ``ticket_notifications`` rebuilt in its 093 shape, stamped at 118.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain (001-118) when
SFS_TEST_PG_URL is set, seeds a real ticket notification, upgrades, and
asserts the backfill, nullability and every CHECK.
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

_PRE_119_NOTIFICATIONS = """
CREATE TABLE ticket_notifications (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    user_id VARCHAR(36) NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    ticket_id VARCHAR(64) NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    project_id VARCHAR(64) NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    kind VARCHAR(20) NOT NULL,
    summary TEXT NOT NULL,
    actor_type VARCHAR(20) DEFAULT 'user' NOT NULL,
    actor_user_id VARCHAR(36),
    service_key_name VARCHAR(100),
    event_count INTEGER DEFAULT '1' NOT NULL,
    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
    updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
    read_at DATETIME,
    emailed_at DATETIME
)
"""


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


@pytest.fixture
def pre_119_db(tmp_path: Path) -> Path:
    path = tmp_path / "m119.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE reminder_notes"))
        conn.execute(sa.text("DROP TABLE reminders"))
        conn.execute(sa.text("DROP TABLE ticket_notifications"))
        conn.execute(sa.text(_PRE_119_NOTIFICATIONS))
        for name, cols in (
            ("idx_ticket_notifications_user_id", "user_id, id"),
            ("idx_ticket_notifications_user_read", "user_id, read_at"),
            ("idx_ticket_notifications_ticket", "ticket_id"),
            ("idx_ticket_notifications_sweep", "emailed_at, read_at, updated_at"),
            ("idx_ticket_notifications_read_updated", "read_at, updated_at"),
            ("idx_ticket_notifications_emailed_updated", "emailed_at, updated_at"),
        ):
            conn.execute(sa.text(f"CREATE INDEX {name} ON ticket_notifications ({cols})"))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m119', 'm119@example.com', 1, 'free', CURRENT_TIMESTAMP, 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO ticket_notifications (user_id, ticket_id, project_id, kind, summary) "
            "VALUES ('u_m119', 'tk_m119', 'proj_m119', 'comment', 'New comment')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "118")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_119_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_119_db}")
    command.upgrade(config, "119")
    engine = sa.create_engine(f"sqlite:///{pre_119_db}")
    try:
        assert {"id", "owner_user_id", "due_at", "notified_at", "source_kind"} <= set(
            _columns(engine, "reminders")
        )
        assert {"id", "reminder_id", "body"} <= set(_columns(engine, "reminder_notes"))
        assert {
            "actor_type", "service_key_id", "service_key_name", "closed_by_service_key_id",
            "updated_by_user_id", "updated_actor_type", "updated_by_service_key_id",
        } <= set(_columns(engine, "reminders"))
        assert {"actor_type", "service_key_id", "service_key_name"} <= set(
            _columns(engine, "reminder_notes")
        )
        cols = _columns(engine, "ticket_notifications")
        assert cols["ticket_id"]["nullable"] is True
        assert cols["project_id"]["nullable"] is True
        ddl = engine.connect().execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE name = 'ticket_notifications'"
        )).scalar_one()
        # The rebuild keeps AUTOINCREMENT: ids are never reused.
        assert "AUTOINCREMENT" in ddl.upper()
        index_names = {i["name"] for i in sa.inspect(engine).get_indexes("ticket_notifications")}
        assert {"idx_ticket_notifications_user_id", "idx_ticket_notifications_subject"} <= index_names

        with engine.begin() as conn:
            row = conn.execute(sa.text(
                "SELECT subject_kind, subject_id FROM ticket_notifications"
            )).one()
            assert tuple(row) == ("ticket", "tk_m119")
            # A reminder row without ticket/project is allowed.
            conn.execute(sa.text(
                "INSERT INTO ticket_notifications (user_id, subject_kind, subject_id, kind, summary) "
                "VALUES ('u_m119', 'reminder', 'rem_1', 'reminder_due', 'Reminder due')"
            ))
        # A ticket row must name its ticket.
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO ticket_notifications (user_id, subject_kind, kind, summary) "
                "VALUES ('u_m119', 'ticket', 'comment', 'x')"
            ))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO ticket_notifications (user_id, subject_kind, subject_id, kind, summary) "
                "VALUES ('u_m119', 'meeting', 'm', 'x', 'x')"
            ))
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at) "
                "VALUES ('rem_1', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            status, via, snoozes = conn.execute(sa.text(
                "SELECT status, created_via, snooze_count FROM reminders WHERE id = 'rem_1'"
            )).one()
            assert (status, via, snoozes) == ("open", "manual", 0)
            assert conn.execute(sa.text(
                "SELECT actor_type FROM reminders WHERE id = 'rem_1'"
            )).scalar() == "user"
        for bad in (
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, status) "
            "VALUES ('rem_2', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'snoozed')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, created_via) "
            "VALUES ('rem_3', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'email')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, source_kind) "
            "VALUES ('rem_4', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'ticket')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, source_kind, source_id) "
            "VALUES ('rem_5', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'wiki', 'x')",
            "INSERT INTO reminder_notes (id, reminder_id, body) VALUES ('n1', 'rem_1', '"
            + "x" * 4001 + "')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, actor_type) "
            "VALUES ('rem_6', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'bot')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, updated_actor_type) "
            "VALUES ('rem_7', 'u_m119', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'bot')",
            "INSERT INTO reminder_notes (id, reminder_id, body, actor_type) "
            "VALUES ('n2', 'rem_1', 'b', 'bot')",
        ):
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(bad))
    finally:
        engine.dispose()

    command.downgrade(config, "118")
    engine = sa.create_engine(f"sqlite:///{pre_119_db}")
    try:
        assert not _columns(engine, "reminders")
        cols = _columns(engine, "ticket_notifications")
        assert "subject_kind" not in cols
        assert cols["ticket_id"]["nullable"] is False
        with engine.begin() as conn:
            # The reminder row is gone; the ticket row survived.
            assert conn.execute(sa.text("SELECT count(*) FROM ticket_notifications")).scalar() == 1
    finally:
        engine.dispose()
    command.upgrade(config, "119")


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


def _pg_nullable(conn, table: str) -> dict[str, str]:
    rows = conn.execute(sa.text(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: r[1] for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-118) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "118")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m119', 'm119@example.com', true, 'free', now(), true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
            "VALUES ('proj_m119', 'p', 'acme/m119', 'u_m119', '')"
        ))
        conn.execute(sa.text(
            "INSERT INTO tickets (id, project_id, title, created_by_user_id, created_at, updated_at) "
            "VALUES ('tk_m119', 'proj_m119', 't', 'u_m119', now(), now())"
        ))
        conn.execute(sa.text(
            "INSERT INTO ticket_notifications (user_id, ticket_id, project_id, kind, summary) "
            "VALUES ('u_m119', 'tk_m119', 'proj_m119', 'comment', 'New comment')"
        ))

    _pg(url, seed)
    command.upgrade(config, "119")

    def check(conn) -> None:
        cols = _pg_nullable(conn, "ticket_notifications")
        assert cols["ticket_id"] == "YES" and cols["project_id"] == "YES"
        assert cols["subject_kind"] == "NO"
        row = conn.execute(sa.text(
            "SELECT subject_kind, subject_id FROM ticket_notifications"
        )).one()
        assert tuple(row) == ("ticket", "tk_m119")
        conn.execute(sa.text(
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at) "
            "VALUES ('rem_1', 'u_m119', 't', now(), now())"
        ))
        assert conn.execute(sa.text(
            "SELECT actor_type FROM reminders WHERE id = 'rem_1'"
        )).scalar() == "user"
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                conn.execute(sa.text(
                    "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, actor_type) "
                    "VALUES ('rem_bad', 'u_m119', 't', now(), now(), 'bot')"
                ))
        conn.execute(sa.text(
            "INSERT INTO ticket_notifications (user_id, subject_kind, subject_id, kind, summary) "
            "VALUES ('u_m119', 'reminder', 'rem_1', 'reminder_due', 'Reminder due')"
        ))
        conn.execute(sa.text(
            "INSERT INTO reminder_notes (id, reminder_id, author_user_id, body) "
            "VALUES ('n1', 'rem_1', 'u_m119', 'hello')"
        ))
        for bad in (
            "INSERT INTO ticket_notifications (user_id, subject_kind, kind, summary) "
            "VALUES ('u_m119', 'ticket', 'comment', 'x')",
            "INSERT INTO ticket_notifications (user_id, subject_kind, subject_id, kind, summary) "
            "VALUES ('u_m119', 'meeting', 'm', 'x', 'x')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, status) "
            "VALUES ('rem_2', 'u_m119', 't', now(), now(), 'snoozed')",
            "INSERT INTO reminders (id, owner_user_id, title, due_at, original_due_at, source_kind) "
            "VALUES ('rem_3', 'u_m119', 't', now(), now(), 'ticket')",
        ):
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad))
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, "118")

    def gone(conn) -> None:
        assert not _pg_nullable(conn, "reminders")
        cols = _pg_nullable(conn, "ticket_notifications")
        assert "subject_kind" not in cols
        assert cols["ticket_id"] == "NO"
        assert conn.execute(sa.text("SELECT count(*) FROM ticket_notifications")).scalar() == 1

    _pg(url, gone)
    command.upgrade(config, "head")
