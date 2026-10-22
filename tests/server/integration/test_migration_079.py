"""Migration 079: registry polling columns + skill_registry_poll_refs.

SQLite: the prior schema is the real metadata minus the new table and the
new registry columns (dropped with plain ALTER TABLE DROP COLUMN).
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
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
NEW_COLUMNS = [
    "poll_interval_seconds", "tag_pattern", "branch_only_mode", "branch", "credential_secret_ref",
    "publish_namespace", "poll_actor_user_id", "last_poll_at", "last_poll_status",
    "last_poll_error_summary", "consecutive_failures", "next_poll_at", "poll_requested_at",
    "poll_lease_owner", "poll_lease_expires_at",
]


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("skill_marketplace_registries")}
    finally:
        engine.dispose()


def _tables(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.fixture
def pre_079_db(tmp_path: Path) -> Path:
    path = tmp_path / "m079.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name != "skill_registry_poll_refs"]
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX idx_skill_registry_next_poll"))
        conn.execute(sa.text("DROP INDEX idx_skill_registry_poll_actor"))
    # A rebuild-free drop needs no CHECK naming the column: recreate the table
    # the way 063 left it, then copy nothing (it is empty).
    with engine.begin() as conn:
        ddl = conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'skill_marketplace_registries'")
        ).scalar_one()
        conn.execute(sa.text("DROP TABLE skill_marketplace_registries"))
        start = ddl.index("(")
        body = ddl[start + 1 : ddl.rindex(")")]
        kept = []
        for part in [p.strip() for p in _split_top_level(body)]:
            if any(part.startswith(col + " ") for col in NEW_COLUMNS):
                continue
            if "poll_interval_seconds" in part or "last_poll_status" in part or "poll_actor_user_id" in part:
                continue
            kept.append(part)
        conn.execute(sa.text(f"CREATE TABLE skill_marketplace_registries ({', '.join(kept)})"))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m079', 'o', 'o', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO skill_marketplace_registries (id, owner_type, owner_id, org_id, alias, source_url, "
            "policy_state) VALUES ('smr_old', 'org', 'org_m079', 'org_m079', 'old', 'https://x.example/a', 'approved')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "078")
    return path


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


def test_upgrade_adds_columns_with_metadata_only_default_and_downgrade_reverses(pre_079_db: Path) -> None:
    assert not (set(NEW_COLUMNS) & _columns(pre_079_db))
    config = _cfg(f"sqlite+aiosqlite:///{pre_079_db}")
    command.upgrade(config, "079")
    assert set(NEW_COLUMNS) <= _columns(pre_079_db)
    assert "skill_registry_poll_refs" in _tables(pre_079_db)
    engine = sa.create_engine(f"sqlite:///{pre_079_db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT poll_interval_seconds, consecutive_failures, branch_only_mode, next_poll_at "
                "FROM skill_marketplace_registries WHERE id = 'smr_old'"
            )).one()
        # Existing registries never start fetching on upgrade.
        assert tuple(row) == (0, 0, 0, None)
    finally:
        engine.dispose()
    command.downgrade(config, "078")
    assert not (set(NEW_COLUMNS) & _columns(pre_079_db))
    assert "skill_registry_poll_refs" not in _tables(pre_079_db)
    command.upgrade(config, "079")
    assert set(NEW_COLUMNS) <= _columns(pre_079_db)


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
    """Against the full real prior chain (001-078) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "078")
    command.upgrade(config, "079")

    def check(conn) -> None:
        names = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'skill_marketplace_registries'::regclass"
            ))
        }
        assert {"ck_skill_registry_poll_interval", "ck_skill_registry_poll_status",
                "fk_skill_registry_poll_actor", "ck_skill_registry_owner_type"} <= names
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_pg079', 'o', 'o-pg079', '{}') "
            "ON CONFLICT DO NOTHING"
        ))
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO skill_marketplace_registries (id, owner_type, owner_id, org_id, alias, source_url, "
                "poll_interval_seconds) VALUES ('smr_pg079', 'org', 'org_pg079', 'org_pg079', 'a', 'https://x/a', 30)"
            ))
        nested.rollback()
        conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg079'"))

    _pg(url, check)
    command.downgrade(config, "078")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'skill_marketplace_registries'"
            ))
        }
        assert not (set(NEW_COLUMNS) & cols)

    _pg(url, gone)
    command.upgrade(config, "head")
