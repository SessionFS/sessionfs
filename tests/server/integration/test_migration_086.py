"""Migration 086: weekly org activity digest (GitHub #97).

SQLite: the prior schema is the real metadata minus the new column and
table. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set.
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


def _inspect(path: Path) -> tuple[set[str], set[str], list[str], set[str]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("org_members")}
        tables = set(insp.get_table_names())
        if "org_digest_runs" in tables:
            pk = insp.get_pk_constraint("org_digest_runs")["constrained_columns"]
            digest_cols = {c["name"] for c in insp.get_columns("org_digest_runs")}
        else:
            pk = []
            digest_cols = set()
        return cols, tables, list(pk), digest_cols
    finally:
        engine.dispose()


@pytest.fixture
def pre_086_db(tmp_path: Path) -> Path:
    path = tmp_path / "m086.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE org_digest_runs"))
        conn.execute(sa.text("ALTER TABLE org_members DROP COLUMN digest_opt_out"))
        # SQLite does not enforce the org/user FKs here; no parent rows needed.
        conn.execute(sa.text(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES ('org_m086', 'u_m086', 'member')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "085")
    return path


def test_upgrade_adds_column_and_table_and_downgrade_reverses(pre_086_db: Path) -> None:
    cols, tables, pk, digest_cols = _inspect(pre_086_db)
    assert "digest_opt_out" not in cols
    assert "org_digest_runs" not in tables

    config = _cfg(f"sqlite+aiosqlite:///{pre_086_db}")
    command.upgrade(config, "086")
    cols, tables, pk, digest_cols = _inspect(pre_086_db)
    assert "digest_opt_out" in cols
    assert "org_digest_runs" in tables
    assert pk == ["org_id", "week_start"]
    assert {"recipient_count", "sent_count"} <= digest_cols

    engine = sa.create_engine(f"sqlite:///{pre_086_db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT digest_opt_out FROM org_members WHERE org_id = 'org_m086'"
            )).one()
            assert tuple(row) == (0,)
            # The PK makes re-inserting the same (org, week) a conflict.
            conn.execute(sa.text(
                "INSERT INTO org_digest_runs (org_id, week_start, sent_at, recipient_count, sent_count) "
                "VALUES ('org_m086', '2026-10-19', '2026-10-20', 2, 2)"
            ))
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO org_digest_runs (org_id, week_start, sent_at, recipient_count, sent_count) "
                    "VALUES ('org_m086', '2026-10-19', '2026-10-21', 1, 1)"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "085")
    cols, tables, _, _ = _inspect(pre_086_db)
    assert "digest_opt_out" not in cols
    assert "org_digest_runs" not in tables

    command.upgrade(config, "086")
    cols, tables, _, _ = _inspect(pre_086_db)
    assert "digest_opt_out" in cols
    assert "org_digest_runs" in tables


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
    command.upgrade(config, "085")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg086', 'pg086@example.com', 'u', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, tier) "
            "VALUES ('org_pg086', 'Org', 'org-pg086', 'team')"
        ))
        conn.execute(sa.text(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES ('org_pg086', 'u_pg086', 'owner')"
        ))

    _pg(url, seed)
    command.upgrade(config, "086")

    def check(conn) -> None:
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'org_members' AND column_name = 'digest_opt_out'"
            ))
        }
        assert cols["digest_opt_out"] == ("NO", "boolean")
        assert conn.execute(sa.text(
            "SELECT digest_opt_out FROM org_members WHERE org_id = 'org_pg086'"
        )).scalar_one() is False
        pk = conn.execute(sa.text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'org_digest_runs'::regclass AND i.indisprimary"
        )).scalars().all()
        assert sorted(pk) == ["org_id", "week_start"]
        digest_cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'org_digest_runs'"
            ))
        }
        assert {"recipient_count", "sent_count"} <= digest_cols

    _pg(url, check)
    command.downgrade(config, "085")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'org_members'"
            ))
        }
        assert "digest_opt_out" not in cols
        tables = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ))
        }
        assert "org_digest_runs" not in tables

    _pg(url, gone)
    command.upgrade(config, "head")
