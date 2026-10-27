"""Migration 100: Jira two-way sync + completion evidence schema.

SQLite: the prior schema is the real metadata minus the new table and the new
columns, stamped at 100's parent. PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.

100's parent (099) is owned by another branch and re-chained at merge time, so
this test skips until that parent script is present in the versions directory.
"""

from __future__ import annotations

import os
import re
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
_VERSIONS = Path(_MIGRATIONS) / "versions"

NEW_TABLES = {"tracker_sync_events"}
EVENT_COLUMNS = {
    "id", "org_id", "link_id", "ticket_id", "direction", "kind", "payload",
    "jira_event_id", "created_at", "applied", "error",
    "state", "retry_count", "next_attempt_at", "updated_at", "dedupe_key",
}
CONNECTION_COLUMNS = {
    "encrypted_webhook_secret", "webhook_secret_hash", "webhook_secret_last4",
    "sync_status", "sync_comments", "status_map", "jira_account_id",
    "webhook_auth_failure_count", "webhook_auth_failure_window_start",
    "last_webhook_auth_failure_at",
}
LINK_COLUMNS = {"last_synced_at", "last_inbound_at"}
COMMENT_COLUMNS = {"source"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _down_revision() -> str:
    text = (_VERSIONS / "100_jira_sync.py").read_text()
    match = re.search(r'^down_revision\s*=\s*["\'](.+?)["\']', text, re.M)
    assert match, "could not read down_revision from the 100 migration"
    return match.group(1)


def _parent_present() -> bool:
    prior = _down_revision()
    return any(p.name.startswith(f"{prior}_") for p in _VERSIONS.iterdir())


def _require_parent() -> str:
    prior = _down_revision()
    if not _parent_present():
        pytest.skip(f"parent revision {prior!r} not merged yet")
    return prior


def _seed_ticket(conn) -> None:
    """A real user → project → ticket chain, so the link's FK to tickets holds
    (``ticket_tracker_links.ticket_id`` references ``tickets.id``)."""
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m100', 'm100@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m100', 'p', 'u_m100')"
    ))
    conn.execute(sa.text(
        "INSERT INTO tickets (id, project_id, title, created_by_user_id) "
        "VALUES ('t_m100', 'p_m100', 'ticket', 'u_m100')"
    ))


def _exercise(conn, integrity_error: type[Exception]) -> None:
    """Behaviour shared by both dialects: valid rows insert with the outbox
    defaults, the unique (link_id, jira_event_id) and (link_id, dedupe_key)
    constraints refuse duplicates per link but allow NULLs, and deleting a
    link cascades its sync events."""
    _seed_ticket(conn)
    conn.execute(sa.text(
        "INSERT INTO ticket_tracker_links (ticket_id, org_id, provider, status) "
        "VALUES ('t_m100', 'org_m100', 'jira', 'linked')"
    ))
    conn.execute(sa.text(
        "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
        "kind, payload, jira_event_id) "
        "VALUES ('ev_m100a', 'org_m100', 't_m100', 't_m100', 'inbound', 'status', "
        "'{}', 'jevt-1')"
    ))
    nested = conn.begin_nested()
    with pytest.raises(integrity_error):
        # Same (link, jira_event_id) twice → idempotency duplicate.
        conn.execute(sa.text(
            "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
            "kind, payload, jira_event_id) "
            "VALUES ('ev_m100b', 'org_m100', 't_m100', 't_m100', 'inbound', 'status', "
            "'{}', 'jevt-1')"
        ))
    nested.rollback()

    row = conn.execute(sa.text(
        "SELECT state, retry_count FROM tracker_sync_events WHERE id = 'ev_m100a'"
    )).one()
    assert (row[0], row[1]) == ("done", 0)

    # One evidence row per (link, completion): dedupe_key is unique per link.
    conn.execute(sa.text(
        "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
        "kind, payload, state, dedupe_key) "
        "VALUES ('ev_m100e', 'org_m100', 't_m100', 't_m100', 'outbound', 'evidence', "
        "'{}', 'pending', 'evidence:1')"
    ))
    nested = conn.begin_nested()
    with pytest.raises(integrity_error):
        conn.execute(sa.text(
            "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
            "kind, payload, state, dedupe_key) "
            "VALUES ('ev_m100f', 'org_m100', 't_m100', 't_m100', 'outbound', 'evidence', "
            "'{}', 'pending', 'evidence:1')"
        ))
    nested.rollback()

    # Multiple NULL jira_event_id rows (outbound) are allowed.
    conn.execute(sa.text(
        "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
        "kind, payload, jira_event_id) "
        "VALUES ('ev_m100c', 'org_m100', 't_m100', 't_m100', 'outbound', 'status', "
        "'{}', NULL)"
    ))
    conn.execute(sa.text(
        "INSERT INTO tracker_sync_events (id, org_id, link_id, ticket_id, direction, "
        "kind, payload, jira_event_id) "
        "VALUES ('ev_m100d', 'org_m100', 't_m100', 't_m100', 'outbound', 'comment', "
        "'{}', NULL)"
    ))

    # deleting the link cascades its sync events.
    conn.execute(sa.text("DELETE FROM ticket_tracker_links WHERE ticket_id = 't_m100'"))
    assert conn.execute(sa.text(
        "SELECT count(*) FROM tracker_sync_events WHERE link_id = 't_m100'"
    )).scalar() == 0


@pytest.fixture
def pre_100_db(tmp_path: Path) -> Path:
    prior = _require_parent()
    path = tmp_path / "m100.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE tracker_sync_events"))
        for column in reversed(sorted(CONNECTION_COLUMNS)):
            conn.execute(sa.text(f"ALTER TABLE org_tracker_connections DROP COLUMN {column}"))
        conn.execute(sa.text("ALTER TABLE ticket_tracker_links DROP COLUMN last_inbound_at"))
        conn.execute(sa.text("ALTER TABLE ticket_tracker_links DROP COLUMN last_synced_at"))
        conn.execute(sa.text("ALTER TABLE ticket_comments DROP COLUMN source"))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES ('org_m100', 'o', 'o-m100')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), prior)
    return path


def test_sqlite_upgrade_and_downgrade(pre_100_db: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{pre_100_db}")
    insp = sa.inspect(engine)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    assert CONNECTION_COLUMNS.isdisjoint(
        {c["name"] for c in insp.get_columns("org_tracker_connections")}
    )
    assert LINK_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_tracker_links")})
    assert COMMENT_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_comments")})
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{pre_100_db}")
    command.upgrade(config, "100")

    engine = sa.create_engine(f"sqlite:///{pre_100_db}")
    insp = sa.inspect(engine)
    try:
        assert NEW_TABLES <= set(insp.get_table_names())
        assert EVENT_COLUMNS <= {c["name"] for c in insp.get_columns("tracker_sync_events")}
        assert CONNECTION_COLUMNS <= {
            c["name"] for c in insp.get_columns("org_tracker_connections")
        }
        assert LINK_COLUMNS <= {c["name"] for c in insp.get_columns("ticket_tracker_links")}
        assert COMMENT_COLUMNS <= {c["name"] for c in insp.get_columns("ticket_comments")}
        # sync toggles default off; comments default 'off'; comment source 'sessionfs'.
        conn_cols = {c["name"]: c for c in insp.get_columns("org_tracker_connections")}
        assert conn_cols["sync_status"]["default"] == "false"
        assert conn_cols["sync_comments"]["default"] == "'off'"
        with engine.connect() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.commit()
            with conn.begin():
                _exercise(conn, sa.exc.IntegrityError)
    finally:
        engine.dispose()

    command.downgrade(config, _down_revision())
    engine = sa.create_engine(f"sqlite:///{pre_100_db}")
    insp = sa.inspect(engine)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    assert CONNECTION_COLUMNS.isdisjoint(
        {c["name"] for c in insp.get_columns("org_tracker_connections")}
    )
    assert LINK_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_tracker_links")})
    assert COMMENT_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_comments")})
    engine.dispose()


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
    """Against the full real prior chain on PostgreSQL."""
    prior = _require_parent()
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, prior)

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES ('org_m100', 'Org', 'org-m100')"
        ))

    _pg(url, seed)
    command.upgrade(config, "100")

    def check(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
        assert NEW_TABLES <= tables
        for table, columns in (
            ("tracker_sync_events", EVENT_COLUMNS),
            ("org_tracker_connections", CONNECTION_COLUMNS),
            ("ticket_tracker_links", LINK_COLUMNS),
            ("ticket_comments", COMMENT_COLUMNS),
        ):
            present = set(conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalars())
            assert columns <= present, f"{table} missing {columns - present}"
        _exercise(conn, sa.exc.IntegrityError)

    _pg(url, check)

    command.downgrade(config, prior)

    def gone(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
        assert NEW_TABLES.isdisjoint(tables)
        for table, columns in (
            ("org_tracker_connections", CONNECTION_COLUMNS),
            ("ticket_tracker_links", LINK_COLUMNS),
            ("ticket_comments", COMMENT_COLUMNS),
        ):
            cols = set(conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalars())
            assert columns.isdisjoint(cols)

    _pg(url, gone)
    command.upgrade(config, "head")
