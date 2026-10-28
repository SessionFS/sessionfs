"""Migration 103: Jira sprint + parent/epic columns (GitHub #113, C7/C8).

SQLite: the prior schema is the real metadata minus the six additive columns,
stamped at 102. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain
when SFS_TEST_PG_URL is set and asserts the column types, nullability and that
existing connection/link rows stay NULL across the additive upgrade.
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

CONNECTION_COLUMNS = {
    # column -> PostgreSQL character_maximum_length
    "sprint_field_id": 64,
    "epic_link_field_id": 64,
}
LINK_COLUMNS = {
    "jira_sprint_id": 32,
    "jira_sprint_name": 200,
    "jira_sprint_state": 20,
    "jira_parent_key": 50,
    "jira_project_key": 50,
}
# Timestamp columns have no varchar length; checked separately.
LINK_TIMESTAMP_COLUMNS = {"unknown_since"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _seed(conn) -> None:
    """A real org → user → project → ticket chain plus a connection and link,
    so the additive upgrade can be shown to leave existing rows untouched."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m103', 'o', 'o-m103')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m103', 'm103@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m103', 'p', 'u_m103')"
    ))
    conn.execute(sa.text(
        "INSERT INTO tickets (id, project_id, title, created_by_user_id) "
        "VALUES ('t_m103', 'p_m103', 'ticket', 'u_m103')"
    ))
    conn.execute(sa.text(
        "INSERT INTO org_tracker_connections "
        "(org_id, site_url, account_email, encrypted_api_token, connected_by_user_id) "
        "VALUES ('org_m103', 'https://acme.atlassian.net', 'a@b.com', 'tok', 'u_m103')"
    ))
    conn.execute(sa.text(
        "INSERT INTO ticket_tracker_links (ticket_id, org_id) VALUES ('t_m103', 'org_m103')"
    ))


@pytest.fixture
def pre_103_db(tmp_path: Path) -> Path:
    path = tmp_path / "m103.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in ("sprint_field_id", "epic_link_field_id"):
            conn.execute(sa.text(f"ALTER TABLE org_tracker_connections DROP COLUMN {column}"))
        for column in (
            "jira_sprint_id",
            "jira_sprint_name",
            "jira_sprint_state",
            "jira_parent_key",
            "jira_project_key",
            "unknown_since",
        ):
            conn.execute(sa.text(f"ALTER TABLE ticket_tracker_links DROP COLUMN {column}"))
        _seed(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "102")
    return path


def test_upgrade_adds_nullable_columns_and_downgrade_reverses(pre_103_db: Path) -> None:
    assert CONNECTION_COLUMNS.keys().isdisjoint(_columns(pre_103_db, "org_tracker_connections"))
    assert LINK_COLUMNS.keys().isdisjoint(_columns(pre_103_db, "ticket_tracker_links"))
    assert LINK_TIMESTAMP_COLUMNS.isdisjoint(_columns(pre_103_db, "ticket_tracker_links"))

    config = _cfg(f"sqlite+aiosqlite:///{pre_103_db}")
    command.upgrade(config, "103")
    assert CONNECTION_COLUMNS.keys() <= _columns(pre_103_db, "org_tracker_connections")
    assert LINK_COLUMNS.keys() <= _columns(pre_103_db, "ticket_tracker_links")
    assert LINK_TIMESTAMP_COLUMNS <= _columns(pre_103_db, "ticket_tracker_links")

    # Existing rows keep NULL for the additive columns (no backfill).
    engine = sa.create_engine(f"sqlite:///{pre_103_db}")
    try:
        with engine.connect() as conn:
            conn_row = conn.execute(sa.text(
                "SELECT sprint_field_id, epic_link_field_id "
                "FROM org_tracker_connections WHERE org_id = 'org_m103'"
            )).one()
            link_row = conn.execute(sa.text(
                "SELECT jira_sprint_id, jira_sprint_name, jira_sprint_state, jira_parent_key, "
                "jira_project_key, unknown_since "
                "FROM ticket_tracker_links WHERE ticket_id = 't_m103'"
            )).one()
        assert conn_row == (None, None)
        assert link_row == (None, None, None, None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, "102")
    assert CONNECTION_COLUMNS.keys().isdisjoint(_columns(pre_103_db, "org_tracker_connections"))
    assert LINK_COLUMNS.keys().isdisjoint(_columns(pre_103_db, "ticket_tracker_links"))
    assert LINK_TIMESTAMP_COLUMNS.isdisjoint(_columns(pre_103_db, "ticket_tracker_links"))
    command.upgrade(config, "103")
    assert CONNECTION_COLUMNS.keys() <= _columns(pre_103_db, "org_tracker_connections")
    assert LINK_COLUMNS.keys() <= _columns(pre_103_db, "ticket_tracker_links")
    assert LINK_TIMESTAMP_COLUMNS <= _columns(pre_103_db, "ticket_tracker_links")


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-102) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "102")

    def seed(conn) -> None:
        _seed(conn)

    _pg(url, seed)
    command.upgrade(config, "103")

    def check(conn) -> None:
        conn_cols = _pg_columns(conn, "org_tracker_connections")
        link_cols = _pg_columns(conn, "ticket_tracker_links")
        for name, length in CONNECTION_COLUMNS.items():
            assert conn_cols[name][0] == "character varying", name
            assert conn_cols[name][1] == length, name
            assert conn_cols[name][2] == "YES", name
        for name, length in LINK_COLUMNS.items():
            assert link_cols[name][0] == "character varying", name
            assert link_cols[name][1] == length, name
            assert link_cols[name][2] == "YES", name
        # ``unknown_since`` is a nullable timestamptz, not varchar.
        assert link_cols["unknown_since"][0] == "timestamp with time zone"
        assert link_cols["unknown_since"][2] == "YES"
        # Existing rows are untouched by the additive upgrade.
        row = conn.execute(sa.text(
            "SELECT sprint_field_id, epic_link_field_id "
            "FROM org_tracker_connections WHERE org_id = 'org_m103'"
        )).one()
        assert row == (None, None)
        row = conn.execute(sa.text(
            "SELECT jira_sprint_id, jira_sprint_name, jira_sprint_state, jira_parent_key, "
            "jira_project_key, unknown_since "
            "FROM ticket_tracker_links WHERE ticket_id = 't_m103'"
        )).one()
        assert row == (None, None, None, None, None, None)

    _pg(url, check)
    command.downgrade(config, "102")

    def gone(conn) -> None:
        conn_cols = _pg_columns(conn, "org_tracker_connections")
        link_cols = _pg_columns(conn, "ticket_tracker_links")
        assert CONNECTION_COLUMNS.keys().isdisjoint(conn_cols)
        assert LINK_COLUMNS.keys().isdisjoint(link_cols)
        assert LINK_TIMESTAMP_COLUMNS.isdisjoint(link_cols)

    _pg(url, gone)
    command.upgrade(config, "head")
