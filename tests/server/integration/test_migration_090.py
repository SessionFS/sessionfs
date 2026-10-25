"""Migration 090: make retrieval-audit project_id nullable (session helper piece 3).

A helper turn can be scoped to sessions that span projects (or have none), so
``retrieval_audit_contexts.project_id`` and ``retrieval_audit_events.project_id``
become nullable.  API-created contexts/events still always carry one; this
migration only relaxes the column so helper-created rows may be NULL.

SQLite round-trip proves the nullability change in both directions against the
real prior shape (project_id NOT NULL at 083).  PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
_CONTEXT_TABLE = "retrieval_audit_contexts"
_EVENT_TABLE = "retrieval_audit_events"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    """090's parent (083), read from the script so a re-chain needs no edit."""
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("090")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


def _context_083(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        _CONTEXT_TABLE,
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column("persona_name", sa.String(50), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("idx_retrieval_ctx_project", "project_id"),
        sa.Index("idx_retrieval_ctx_ticket", "ticket_id"),
    )


def _event_083(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        _EVENT_TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("context_id", sa.String(64), sa.ForeignKey("retrieval_audit_contexts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("returned_refs", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source", sa.String(20), nullable=False, server_default="mcp"),
        sa.Column("caller_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Index("idx_retrieval_event_context", "context_id", "created_at"),
        sa.Index("idx_retrieval_event_session", "session_id"),
    )


def _prior_metadata() -> sa.MetaData:
    """All tables in their 083 shape: retrieval-audit project_id NOT NULL."""
    metadata = sa.MetaData()
    overridden = {_CONTEXT_TABLE, _EVENT_TABLE}
    for name, table in Base.metadata.tables.items():
        if name not in overridden:
            table.to_metadata(metadata)
    _context_083(metadata)
    _event_083(metadata)
    return metadata


@pytest.fixture
def pre_090_db(tmp_path: Path) -> Path:
    path = tmp_path / "m090.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _prior_revision())
    return path


def _project_id_nullable(connection: sqlite3.Connection, table: str) -> bool:
    for row in connection.execute(f"PRAGMA table_info({table})"):
        if row[1] == "project_id":
            return not bool(row[3])  # "notnull" column is 1 when NOT NULL
    raise AssertionError(f"{table}.project_id not found")


def test_sqlite_round_trip(pre_090_db: Path) -> None:
    connection = sqlite3.connect(str(pre_090_db))
    # Prior shape: project_id is NOT NULL on both tables.
    assert _project_id_nullable(connection, _CONTEXT_TABLE) is False
    assert _project_id_nullable(connection, _EVENT_TABLE) is False
    # Seed a context + event that the API could write (project_id present).
    connection.execute(
        "INSERT INTO retrieval_audit_contexts (id, project_id) VALUES ('ctx_090', 'proj_090')"
    )
    connection.execute(
        "INSERT INTO retrieval_audit_events (context_id, project_id, tool_name) "
        "VALUES ('ctx_090', 'proj_090', 'search_sessions')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO retrieval_audit_contexts (id, project_id) VALUES ('ctx_null', NULL)"
        )
    connection.commit()
    connection.close()

    config = _cfg(f"sqlite+aiosqlite:///{pre_090_db}")
    command.upgrade(config, "090")

    connection = sqlite3.connect(str(pre_090_db))
    assert _project_id_nullable(connection, _CONTEXT_TABLE) is True
    assert _project_id_nullable(connection, _EVENT_TABLE) is True
    # The existing non-NULL row survives the batch table recreation.
    assert connection.execute(
        "SELECT project_id FROM retrieval_audit_contexts WHERE id = 'ctx_090'"
    ).fetchone() == ("proj_090",)
    # A helper turn without a single project can now write a NULL project_id.
    connection.execute(
        "INSERT INTO retrieval_audit_contexts (id, project_id) VALUES ('ctx_helper', NULL)"
    )
    connection.execute(
        "INSERT INTO retrieval_audit_events (context_id, project_id, tool_name, source) "
        "VALUES ('ctx_helper', NULL, 'get_project_context', 'helper')"
    )
    connection.commit()
    connection.close()

    # The downgrade deletes the NULL helper rows itself before re-asserting NOT
    # NULL; the batch table recreation fails (rather than dropping a row) while
    # any NULL is present, so the migration owns this cleanup.
    command.downgrade(config, _prior_revision())

    connection = sqlite3.connect(str(pre_090_db))
    assert _project_id_nullable(connection, _CONTEXT_TABLE) is False
    assert _project_id_nullable(connection, _EVENT_TABLE) is False
    # The NULL helper event and its NULL-project context are both gone.
    assert connection.execute(
        "SELECT COUNT(*) FROM retrieval_audit_contexts WHERE project_id IS NULL"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT COUNT(*) FROM retrieval_audit_events WHERE project_id IS NULL"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT project_id FROM retrieval_audit_contexts WHERE id = 'ctx_090'"
    ).fetchone() == ("proj_090",)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO retrieval_audit_contexts (id, project_id) VALUES ('ctx_null2', NULL)"
        )
    connection.close()

    command.upgrade(config, "090")

    connection = sqlite3.connect(str(pre_090_db))
    assert _project_id_nullable(connection, _CONTEXT_TABLE) is True
    assert _project_id_nullable(connection, _EVENT_TABLE) is True
    connection.close()


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
    rows = conn.execute(
        sa.text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'project_id'"
        ),
        {"t": table},
    ).all()
    return {r[0]: r[1] for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    prior = _prior_revision()
    command.upgrade(config, prior)

    def check(conn) -> None:
        assert _pg_nullable(conn, _CONTEXT_TABLE) == {"project_id": "NO"}
        assert _pg_nullable(conn, _EVENT_TABLE) == {"project_id": "NO"}

    _pg(url, check)
    command.upgrade(config, "090")

    def check_upgraded(conn) -> None:
        assert _pg_nullable(conn, _CONTEXT_TABLE) == {"project_id": "YES"}
        assert _pg_nullable(conn, _EVENT_TABLE) == {"project_id": "YES"}

    _pg(url, check_upgraded)

    # Seed the NULL-project rows a helper turn can write post-090.  Downgrade
    # must delete them itself before re-asserting NOT NULL.
    def seed_null_rows(conn) -> None:
        conn.execute(
            sa.text(
                "INSERT INTO retrieval_audit_contexts (id, project_id) "
                "VALUES ('ctx_helper', NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO retrieval_audit_events "
                "(context_id, project_id, tool_name, source) "
                "VALUES ('ctx_helper', NULL, 'get_project_context', 'helper')"
            )
        )

    _pg(url, seed_null_rows)

    command.downgrade(config, prior)
    _pg(url, check)

    def assert_null_rows_deleted(conn) -> None:
        ctx_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM retrieval_audit_contexts WHERE project_id IS NULL"
            )
        ).scalar_one()
        event_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM retrieval_audit_events WHERE project_id IS NULL"
            )
        ).scalar_one()
        assert ctx_count == 0
        assert event_count == 0

    _pg(url, assert_null_rows_deleted)
    command.upgrade(config, "head")
