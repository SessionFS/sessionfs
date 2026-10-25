"""Migration 084: org_llm_settings (session-helper org-wide LLM key).

SQLite: the real metadata minus the new table, stamped at 083. PostgreSQL:
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

NEW_TABLE = "org_llm_settings"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_084_db(tmp_path: Path) -> Path:
    path = tmp_path / "m084.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name != NEW_TABLE]
    assert len(prior) == len(Base.metadata.tables) - 1
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) "
            "VALUES ('org_m084', 'o', 'o-m084', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
            "VALUES ('u_m084', 'm084@example.com', 'u', 'enterprise', 1, 1, CURRENT_TIMESTAMP)"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "083")
    return path


def _tables(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _cols(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns(NEW_TABLE)}
    finally:
        engine.dispose()


def test_upgrade_creates_table_and_downgrade_drops(pre_084_db: Path) -> None:
    assert NEW_TABLE not in _tables(pre_084_db)
    config = _cfg(f"sqlite+aiosqlite:///{pre_084_db}")

    command.upgrade(config, "084")
    assert NEW_TABLE in _tables(pre_084_db)
    assert {
        "org_id", "provider", "model", "encrypted_api_key", "base_url",
        "enabled", "updated_by_user_id", "created_at", "updated_at",
    } <= _cols(pre_084_db)

    # enabled defaults to true (SQLite stores booleans as 1/0).
    engine = sa.create_engine(f"sqlite:///{pre_084_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO org_llm_settings "
                "(org_id, provider, model, encrypted_api_key, updated_by_user_id) "
                "VALUES ('org_m084', 'anthropic', 'claude-sonnet-5-5', 'k', 'u_m084')"
            ))
            enabled = conn.execute(sa.text(
                "SELECT enabled FROM org_llm_settings WHERE org_id = 'org_m084'"
            )).scalar()
        assert enabled == 1
    finally:
        engine.dispose()

    command.downgrade(config, "083")
    assert NEW_TABLE not in _tables(pre_084_db)
    command.upgrade(config, "084")
    assert NEW_TABLE in _tables(pre_084_db)


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
    command.upgrade(config, "083")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) "
            "VALUES ('org_pg084', 'o', 'o-pg084', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
            "VALUES ('u_pg084', 'pg084@example.com', 'u', 'enterprise', true, true, now())"
        ))

    _pg(url, seed)

    try:
        command.upgrade(config, "084")

        def check(conn) -> None:
            cols = {
                r[0]
                for r in conn.execute(sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'org_llm_settings'"
                ))
            }
            assert {
                "org_id", "provider", "model", "encrypted_api_key", "base_url",
                "enabled", "updated_by_user_id", "created_at", "updated_at",
            } <= cols
            conn.execute(sa.text(
                "INSERT INTO org_llm_settings (org_id, provider, model, encrypted_api_key) "
                "VALUES ('org_pg084', 'anthropic', 'm', 'k')"
            ))
            assert conn.execute(sa.text(
                "SELECT enabled FROM org_llm_settings WHERE org_id = 'org_pg084'"
            )).scalar() is True
            # ON DELETE CASCADE from organizations.
            conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg084'"))
            assert conn.execute(sa.text(
                "SELECT count(*) FROM org_llm_settings WHERE org_id = 'org_pg084'"
            )).scalar() == 0

        _pg(url, check)
        command.downgrade(config, "083")

        def gone(conn) -> None:
            names = set(
                conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )).scalars()
            )
            assert NEW_TABLE not in names

        _pg(url, gone)
        command.upgrade(config, "head")
    finally:
        def cleanup(conn) -> None:
            conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg084'"))

        _pg(url, cleanup)
