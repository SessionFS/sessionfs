"""Migration 087: admin-proposed project transfer columns (GitHub #96).

SQLite: the prior schema is the real metadata minus the new columns, index
and foreign key. The FK is PostgreSQL-only (matching 079/080), so on SQLite
we rebuild ``project_transfers`` without the 087 columns — SQLite cannot
drop a column that still carries a child FK. PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.
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
_NEW_COLUMNS = {"proposed_by_user_id", "proposal_reason", "expires_at"}
_NEW_INDEXES = {"idx_project_transfers_proposed_by"}
_NEW_FK = "fk_project_transfers_proposed_by_user_id"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _inspect(path: Path) -> tuple[set[str], set[str], set[str]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("project_transfers")}
        idx = {i["name"] for i in insp.get_indexes("project_transfers")}
        fks = {f["name"] for f in insp.get_foreign_keys("project_transfers")}
        return cols, idx, fks
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


def strip_087(conn: sa.Connection) -> None:
    """Rebuild project_transfers (SQLite) without the 087 column, FK and
    index, keeping its other columns, FKs and indexes."""
    conn.execute(sa.text("DROP INDEX idx_project_transfers_proposed_by"))
    ddl = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'project_transfers'")
    ).scalar_one()
    indexes = [
        r[0]
        for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'project_transfers' AND sql IS NOT NULL"
        ))
    ]
    conn.execute(sa.text("DROP TABLE project_transfers"))
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    kept = [
        p.strip()
        for p in _split_top_level(body)
        if not any(token in p for token in _NEW_COLUMNS)
    ]
    conn.execute(sa.text(f"CREATE TABLE project_transfers ({', '.join(kept)})"))
    for sql in indexes:
        conn.execute(sa.text(sql))


@pytest.fixture
def pre_087_db(tmp_path: Path) -> Path:
    path = tmp_path / "m087.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        strip_087(conn)
        # SQLite does not enforce the initiated_by FK here; no users row needed.
        conn.execute(sa.text(
            "INSERT INTO project_transfers "
            "(id, project_id, initiated_by, target_user_id, from_scope, to_scope, state) "
            "VALUES ('xfer_old087', NULL, 'u_087', NULL, 'org_a', 'org_b', 'pending')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "086")
    return path


def test_upgrade_adds_columns_and_downgrade_reverses(pre_087_db: Path) -> None:
    cols, idx, fks = _inspect(pre_087_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)
    assert _NEW_FK not in fks

    config = _cfg(f"sqlite+aiosqlite:///{pre_087_db}")
    command.upgrade(config, "087")
    cols, idx, fks = _inspect(pre_087_db)
    assert _NEW_COLUMNS <= cols
    assert _NEW_INDEXES <= idx
    # The FK is PostgreSQL-only; SQLite has no foreign key for the column.
    assert _NEW_FK not in fks

    engine = sa.create_engine(f"sqlite:///{pre_087_db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT proposed_by_user_id, proposal_reason, expires_at "
                "FROM project_transfers WHERE id = 'xfer_old087'"
            )).one()
        # Every pre-087 row is an owner-initiated transfer: all NULL.
        assert tuple(row) == (None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, "086")
    cols, idx, fks = _inspect(pre_087_db)
    assert not (_NEW_COLUMNS & cols)
    assert not (_NEW_INDEXES & idx)

    command.upgrade(config, "087")
    cols, _, _ = _inspect(pre_087_db)
    assert _NEW_COLUMNS <= cols


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
    """Against the full real prior chain (001-083) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "086")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg087', 'pg087@example.com', 'u', 'admin', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO project_transfers "
            "(id, initiated_by, from_scope, to_scope, state) "
            "VALUES ('xfer_pg087', 'u_pg087', 'org_a', 'org_b', 'pending')"
        ))

    _pg(url, seed)
    command.upgrade(config, "087")

    def check(conn) -> None:
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'project_transfers'"
            ))
        }
        assert cols["proposed_by_user_id"] == ("YES", "character varying")
        assert cols["proposal_reason"] == ("YES", "text")
        assert cols["expires_at"] == ("YES", "timestamp with time zone")
        fks = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'project_transfers'::regclass"
            ))
        }
        assert _NEW_FK in fks
        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'project_transfers'"
            ))
        }
        assert _NEW_INDEXES <= indexes
        assert conn.execute(sa.text(
            "SELECT proposed_by_user_id FROM project_transfers WHERE id = 'xfer_pg087'"
        )).scalar_one() is None

    _pg(url, check)
    command.downgrade(config, "086")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'project_transfers'"
            ))
        }
        assert not (_NEW_COLUMNS & cols)
        assert conn.execute(sa.text(
            "SELECT count(*) FROM project_transfers"
        )).scalar_one() == 1

    _pg(url, gone)
    command.upgrade(config, "head")
