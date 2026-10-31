"""Migration 117: the release_tickets join table (Review Loop R3, Brief R1).

SQLite: the prior schema is the real metadata minus the new table, stamped at
116. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and asserts column types, nullability and the
``(release_id, ticket_id)`` composite-primary-key uniqueness.
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

COLUMNS = {
    "release_id",
    "ticket_id",
    "linked_by_user_id",
    "service_key_id",
    "created_at",
}

# name -> (pg data_type, character_maximum_length, is_nullable)
PG_COLUMNS = {
    "release_id": ("character varying", 64, "NO"),
    "ticket_id": ("character varying", 64, "NO"),
    "linked_by_user_id": ("character varying", 64, "YES"),
    "service_key_id": ("character varying", 36, "YES"),
    "created_at": ("timestamp with time zone", None, "NO"),
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table(table):
            return set()
        return {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()


def _seed_release_and_ticket(conn) -> None:
    """A minimal org → user → project → release + ticket so a link has a home."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m117', 'o', 'o-m117')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m117', 'm117@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m117', 'p', 'u_m117')"
    ))
    conn.execute(sa.text(
        "INSERT INTO releases (id, project_id, version, tag_sha) "
        "VALUES ('rel_m117', 'p_m117', '1.0.0', 'a')"
    ))
    conn.execute(sa.text(
        "INSERT INTO tickets (id, project_id, title, description, created_by_user_id) "
        "VALUES ('tk_m117', 'p_m117', 't', '', 'u_m117')"
    ))


@pytest.fixture
def pre_117_db(tmp_path: Path) -> Path:
    path = tmp_path / "m117.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-117 schema: the real metadata minus the new table.
        conn.execute(sa.text("DROP TABLE release_tickets"))
        _seed_release_and_ticket(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "116")
    return path


def test_upgrade_adds_table_and_downgrade_reverses(pre_117_db: Path) -> None:
    assert "release_tickets" not in _columns(pre_117_db, "release_tickets")

    config = _cfg(f"sqlite+aiosqlite:///{pre_117_db}")
    command.upgrade(config, "117")
    assert COLUMNS <= _columns(pre_117_db, "release_tickets")

    # Server default + composite-PK uniqueness are real, not just Python-side.
    engine = sa.create_engine(f"sqlite:///{pre_117_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO release_tickets (release_id, ticket_id) "
                "VALUES ('rel_m117', 'tk_m117')"
            ))
            row = conn.execute(sa.text(
                "SELECT created_at, linked_by_user_id, service_key_id "
                "FROM release_tickets WHERE release_id = 'rel_m117' AND ticket_id = 'tk_m117'"
            )).one()
            assert row[0] is not None  # server_default now()
            assert row[1] is None  # linked_by_user_id nullable
            assert row[2] is None  # service_key_id nullable
            # Uniqueness on (release_id, ticket_id).
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO release_tickets (release_id, ticket_id) "
                    "VALUES ('rel_m117', 'tk_m117')"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "116")
    assert "release_tickets" not in _columns(pre_117_db, "release_tickets")
    command.upgrade(config, "117")
    assert COLUMNS <= _columns(pre_117_db, "release_tickets")


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None, str]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-116) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "116")

    def seed(conn) -> None:
        _seed_release_and_ticket(conn)

    _pg(url, seed)
    command.upgrade(config, "117")

    def check(conn) -> None:
        cols = _pg_columns(conn, "release_tickets")
        for name, expected in PG_COLUMNS.items():
            assert cols[name] == expected, (name, cols[name])

        conn.execute(sa.text(
            "INSERT INTO release_tickets (release_id, ticket_id) "
            "VALUES ('rel_m117', 'tk_m117')"
        ))
        row = conn.execute(sa.text(
            "SELECT created_at FROM release_tickets "
            "WHERE release_id = 'rel_m117' AND ticket_id = 'tk_m117'"
        )).one()
        assert row[0] is not None

    _pg(url, check)
    command.downgrade(config, "116")

    def gone(conn) -> None:
        assert "release_tickets" not in _pg_columns(conn, "release_tickets")

    _pg(url, gone)
    command.upgrade(config, "head")
