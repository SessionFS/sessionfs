"""Migration 098: project-group knowledge context columns (GitHub #101, phase 2).

SQLite: the prior schema is the real metadata minus the six new
``project_groups`` columns, stamped at 098's parent. PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.

Both paths seed a group before the upgrade and another after it, then check
both carry the opt-out defaults. The parent revision is read from the
migration's ``down_revision`` (re-chained at merge time if another migration
lands first); the tests skip only if that parent script is missing.
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

_NEW_COLUMNS = (
    "compile_enabled",
    "context_document",
    "context_compiled_at",
    "compiled_by_user_id",
    "context_sources",
    "compile_attempted_at",
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _down_revision() -> str:
    text = (_VERSIONS / "098_group_context.py").read_text()
    match = re.search(r'^down_revision\s*=\s*["\'](.+?)["\']', text, re.M)
    assert match, "could not read down_revision from the 098 migration"
    return match.group(1)


def _parent_present() -> bool:
    prior = _down_revision()
    return any(p.name.startswith(f"{prior}_") for p in _VERSIONS.iterdir())


def _require_parent() -> str:
    prior = _down_revision()
    if not _parent_present():
        pytest.skip(f"parent revision {prior!r} not merged yet")
    return prior


_PRE_GROUP = "pg_m098"  # inserted before the upgrade (backfilled)
_POST_GROUP = "pg_m098b"  # inserted after the upgrade (column defaults)

_INSERT_POST_GROUP = (
    "INSERT INTO project_groups (id, org_id, slug, name) "
    f"VALUES ('{_POST_GROUP}', 'org_m098', 'core-2', 'Core 2')"
)


def _exercise(conn, group_ids: tuple[str, ...]) -> None:
    """Behaviour shared by both dialects: every row in ``group_ids`` (both
    backfilled and newly inserted) carries the opt-out defaults, and the flag
    can be flipped on."""
    assert group_ids
    for group_id in group_ids:
        row = conn.execute(sa.text(
            "SELECT compile_enabled, context_document, context_compiled_at, "
            "compiled_by_user_id, context_sources, compile_attempted_at "
            "FROM project_groups "
            "WHERE id = :g"
        ), {"g": group_id}).one()
        assert bool(row[0]) is False
        assert row[1] == ""
        assert row[2] is None
        assert row[3] is None
        assert row[4] is None
        assert row[5] is None

    conn.execute(
        sa.text("UPDATE project_groups SET compile_enabled = true WHERE id = :g"),
        {"g": group_ids[0]},
    )
    assert bool(conn.execute(
        sa.text("SELECT compile_enabled FROM project_groups WHERE id = :g"),
        {"g": group_ids[0]},
    ).scalar_one()) is True


@pytest.fixture
def pre_098_db(tmp_path: Path) -> Path:
    prior = _require_parent()
    path = tmp_path / "m098.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in _NEW_COLUMNS:
            conn.execute(sa.text(f"ALTER TABLE project_groups DROP COLUMN {column}"))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES ('org_m098', 'Org', 'org-m098')"
        ))
        conn.execute(sa.text(
            "INSERT INTO project_groups (id, org_id, slug, name) "
            "VALUES ('pg_m098', 'org_m098', 'core', 'Core')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), prior)
    return path


def test_sqlite_upgrade_backfills_defaults_and_downgrade_reverses(pre_098_db: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{pre_098_db}")
    insp = sa.inspect(engine)
    existing = {c["name"] for c in insp.get_columns("project_groups")}
    assert not (set(_NEW_COLUMNS) & existing)
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{pre_098_db}")
    command.upgrade(config, "098")

    engine = sa.create_engine(f"sqlite:///{pre_098_db}")
    insp = sa.inspect(engine)
    try:
        present = {c["name"] for c in insp.get_columns("project_groups")}
        assert set(_NEW_COLUMNS) <= present
        # A brand-new row keeps the opt-out defaults too.
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT_POST_GROUP))
        with engine.connect() as conn:
            _exercise(conn, (_PRE_GROUP, _POST_GROUP))
    finally:
        engine.dispose()

    command.downgrade(config, _down_revision())
    engine = sa.create_engine(f"sqlite:///{pre_098_db}")
    insp = sa.inspect(engine)
    remaining = {c["name"] for c in insp.get_columns("project_groups")}
    assert not (set(_NEW_COLUMNS) & remaining)
    engine.dispose()

    command.upgrade(config, "098")  # idempotent
    engine = sa.create_engine(f"sqlite:///{pre_098_db}")
    insp = sa.inspect(engine)
    assert "compile_enabled" in {c["name"] for c in insp.get_columns("project_groups")}
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
            "VALUES ('org_m098', 'Org', 'org-m098')"
        ))
        conn.execute(sa.text(
            "INSERT INTO project_groups (id, org_id, slug, name) "
            "VALUES ('pg_m098', 'org_m098', 'core', 'Core')"
        ))

    _pg(url, seed)
    command.upgrade(config, "098")

    def check(conn) -> None:
        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'project_groups'"
            ))
        }
        assert cols["compile_enabled"] == ("NO", "boolean")
        assert cols["context_document"] == ("NO", "text")
        assert cols["context_compiled_at"] == ("YES", "timestamp with time zone")
        assert cols["compiled_by_user_id"] == ("YES", "character varying")
        assert cols["context_sources"][0] == "YES"
        assert cols["context_sources"][1] in ("json", "jsonb")
        assert cols["compile_attempted_at"] == ("YES", "timestamp with time zone")
        conn.execute(sa.text(_INSERT_POST_GROUP))
        _exercise(conn, (_PRE_GROUP, _POST_GROUP))

    _pg(url, check)

    command.downgrade(config, prior)

    def gone(conn) -> None:
        cols = set(conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'project_groups'"
        )).scalars())
        assert not (set(_NEW_COLUMNS) & cols)

    _pg(url, gone)
    command.upgrade(config, "head")
