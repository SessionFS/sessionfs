"""Migration 115: the loop_share_links table (Review Loop R3, track C).

SQLite: the prior schema is the real metadata minus the new table, stamped at
112. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and asserts the column types, nullability, the inline
``kind`` CHECK constraint and the ``token_hash`` uniqueness constraint.
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
    "id",
    "project_id",
    "kind",
    "ticket_id",
    "release_version",
    "month",
    "token_hash",
    "created_by_user_id",
    "created_actor_type",
    "created_service_key_id",
    "created_service_key_name",
    "created_at",
    "expires_at",
    "revoked_at",
    "revoked_by_user_id",
    "revoked_actor_type",
    "revoked_service_key_id",
    "revoked_service_key_name",
}

# name -> (pg data_type, character_maximum_length, is_nullable)
PG_COLUMNS = {
    "id": ("character varying", 64, "NO"),
    "project_id": ("character varying", 64, "NO"),
    "kind": ("character varying", 20, "NO"),
    "ticket_id": ("character varying", 64, "YES"),
    "release_version": ("character varying", 100, "YES"),
    "month": ("character varying", 7, "YES"),
    "token_hash": ("character varying", 64, "NO"),
    "created_by_user_id": ("character varying", 64, "YES"),
    "created_actor_type": ("character varying", 20, "NO"),
    "created_service_key_id": ("character varying", 36, "YES"),
    "created_service_key_name": ("character varying", 100, "YES"),
    "created_at": ("timestamp with time zone", None, "NO"),
    "expires_at": ("timestamp with time zone", None, "NO"),
    "revoked_at": ("timestamp with time zone", None, "YES"),
    "revoked_by_user_id": ("character varying", 64, "YES"),
    "revoked_actor_type": ("character varying", 20, "YES"),
    "revoked_service_key_id": ("character varying", 36, "YES"),
    "revoked_service_key_name": ("character varying", 100, "YES"),
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


def _seed_project(conn) -> None:
    """A minimal org → user → project chain so a share link has a home."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m115', 'o', 'o-m115')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m115', 'm115@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m115', 'p', 'u_m115')"
    ))


@pytest.fixture
def pre_115_db(tmp_path: Path) -> Path:
    path = tmp_path / "m115.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-115 schema: the real metadata minus the new table.
        conn.execute(sa.text("DROP TABLE loop_share_links"))
        _seed_project(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "114")
    return path


def test_upgrade_adds_table_and_downgrade_reverses(pre_115_db: Path) -> None:
    assert "loop_share_links" not in _columns(pre_115_db, "loop_share_links")

    config = _cfg(f"sqlite+aiosqlite:///{pre_115_db}")
    command.upgrade(config, "115")
    assert COLUMNS <= _columns(pre_115_db, "loop_share_links")

    # Server defaults, the kind CHECK and token_hash uniqueness are real.
    engine = sa.create_engine(f"sqlite:///{pre_115_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO loop_share_links (id, project_id, kind, token_hash, expires_at) "
                "VALUES ('lsl_m115', 'p_m115', 'ship_report', 'h1', CURRENT_TIMESTAMP)"
            ))
            row = conn.execute(sa.text(
                "SELECT created_actor_type, created_at IS NOT NULL "
                "FROM loop_share_links WHERE id = 'lsl_m115'"
            )).one()
            assert row == ("user", 1)
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO loop_share_links (id, project_id, kind, token_hash, expires_at) "
                    "VALUES ('lsl_m115_bad', 'p_m115', 'bogus', 'h2', CURRENT_TIMESTAMP)"
                ))
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO loop_share_links (id, project_id, kind, token_hash, expires_at) "
                    "VALUES ('lsl_m115_dup', 'p_m115', 'ship_report', 'h1', CURRENT_TIMESTAMP)"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "114")
    assert "loop_share_links" not in _columns(pre_115_db, "loop_share_links")
    command.upgrade(config, "115")
    assert COLUMNS <= _columns(pre_115_db, "loop_share_links")


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
    """Against the full real prior chain (001-114) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "114")

    def seed(conn) -> None:
        _seed_project(conn)

    _pg(url, seed)
    command.upgrade(config, "115")

    def check(conn) -> None:
        cols = _pg_columns(conn, "loop_share_links")
        for name, expected in PG_COLUMNS.items():
            assert cols[name] == expected, (name, cols[name])

        conn.execute(sa.text(
            "INSERT INTO loop_share_links (id, project_id, kind, token_hash, expires_at) "
            "VALUES ('lsl_m115_pg', 'p_m115', 'ship_report', 'h1', CURRENT_TIMESTAMP)"
        ))
        row = conn.execute(sa.text(
            "SELECT created_actor_type FROM loop_share_links WHERE id = 'lsl_m115_pg'"
        )).one()
        assert row == ("user",)

    _pg(url, check)
    command.downgrade(config, "114")

    def gone(conn) -> None:
        assert "loop_share_links" not in _pg_columns(conn, "loop_share_links")

    _pg(url, gone)
    command.upgrade(config, "head")
