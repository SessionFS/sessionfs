"""Migration 112: the loop_checklist_items table (Review Loop R3, track E1).

SQLite: the prior schema is the real metadata minus the new table, stamped at
111. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and asserts the column types, nullability, the inline
CHECK constraints and the ``(project_id, slug)`` uniqueness constraint.
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
    "slug",
    "title",
    "check_text",
    "status",
    "source",
    "source_finding_refs",
    "times_found",
    "last_found_at",
    "created_by_user_id",
    "accepted_by_user_id",
    "created_actor_type",
    "created_service_key_id",
    "created_service_key_name",
    "accepted_actor_type",
    "accepted_service_key_id",
    "accepted_service_key_name",
    "accepted_at",
    "created_at",
    "updated_at",
}

# name -> (pg data_type, character_maximum_length, is_nullable)
PG_COLUMNS = {
    "id": ("character varying", 64, "NO"),
    "project_id": ("character varying", 64, "NO"),
    "slug": ("character varying", 64, "NO"),
    "title": ("character varying", 200, "NO"),
    "check_text": ("text", None, "NO"),
    "status": ("character varying", 20, "NO"),
    "source": ("character varying", 20, "NO"),
    "source_finding_refs": ("text", None, "YES"),
    "times_found": ("integer", None, "NO"),
    "last_found_at": ("timestamp with time zone", None, "YES"),
    "created_by_user_id": ("character varying", 64, "YES"),
    "accepted_by_user_id": ("character varying", 64, "YES"),
    "created_actor_type": ("character varying", 20, "NO"),
    "created_service_key_id": ("character varying", 36, "YES"),
    "created_service_key_name": ("character varying", 100, "YES"),
    "accepted_actor_type": ("character varying", 20, "YES"),
    "accepted_service_key_id": ("character varying", 36, "YES"),
    "accepted_service_key_name": ("character varying", 100, "YES"),
    "accepted_at": ("timestamp with time zone", None, "YES"),
    "created_at": ("timestamp with time zone", None, "NO"),
    "updated_at": ("timestamp with time zone", None, "NO"),
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
    """A minimal org → user → project chain so a checklist row has a home."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m112', 'o', 'o-m112')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m112', 'm112@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m112', 'p', 'u_m112')"
    ))


@pytest.fixture
def pre_112_db(tmp_path: Path) -> Path:
    path = tmp_path / "m112.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-112 schema: the real metadata minus the new table.
        conn.execute(sa.text("DROP TABLE loop_checklist_items"))
        _seed_project(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "111")
    return path


def test_upgrade_adds_table_and_downgrade_reverses(pre_112_db: Path) -> None:
    assert "loop_checklist_items" not in _columns(pre_112_db, "loop_checklist_items")

    config = _cfg(f"sqlite+aiosqlite:///{pre_112_db}")
    command.upgrade(config, "112")
    assert COLUMNS <= _columns(pre_112_db, "loop_checklist_items")

    # Server defaults + uniqueness are real, not just Python-side.
    engine = sa.create_engine(f"sqlite:///{pre_112_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO loop_checklist_items (id, project_id, slug, title, check_text) "
                "VALUES ('lci_m112', 'p_m112', 'a-trap', 'A trap', 'Check it.')"
            ))
            row = conn.execute(sa.text(
                "SELECT status, source, times_found FROM loop_checklist_items WHERE id = 'lci_m112'"
            )).one()
            assert row == ("proposed", "manual", 0)
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO loop_checklist_items (id, project_id, slug, title, check_text) "
                    "VALUES ('lci_m112_dup', 'p_m112', 'a-trap', 'Dup', 'Dup.')"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "111")
    assert "loop_checklist_items" not in _columns(pre_112_db, "loop_checklist_items")
    command.upgrade(config, "112")
    assert COLUMNS <= _columns(pre_112_db, "loop_checklist_items")


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
    """Against the full real prior chain (001-111) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "111")

    def seed(conn) -> None:
        _seed_project(conn)

    _pg(url, seed)
    command.upgrade(config, "112")

    def check(conn) -> None:
        cols = _pg_columns(conn, "loop_checklist_items")
        for name, expected in PG_COLUMNS.items():
            assert cols[name] == expected, (name, cols[name])

        conn.execute(sa.text(
            "INSERT INTO loop_checklist_items (id, project_id, slug, title, check_text) "
            "VALUES ('lci_m112_pg', 'p_m112', 'a-trap', 'A trap', 'Check it.')"
        ))
        row = conn.execute(sa.text(
            "SELECT status, source, times_found FROM loop_checklist_items WHERE id = 'lci_m112_pg'"
        )).one()
        assert row == ("proposed", "manual", 0)

    _pg(url, check)
    command.downgrade(config, "111")

    def gone(conn) -> None:
        assert "loop_checklist_items" not in _pg_columns(conn, "loop_checklist_items")

    _pg(url, gone)
    command.upgrade(config, "head")
