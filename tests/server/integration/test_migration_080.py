"""Migration 080: agent_personas.represented_user_id (GitHub #81).

SQLite: the prior schema is the real metadata minus the new column and
index. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set and checks the FK and the org-only CHECK.
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


def _columns(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("agent_personas")}
    finally:
        engine.dispose()


def strip_080(conn: sa.Connection) -> None:
    """Rebuild agent_personas (SQLite) without the 080 column, FK, CHECK
    and index, keeping its other indexes. Also used by the 072 test, whose
    downgrade would otherwise trip over the CHECK naming org_id."""
    conn.execute(sa.text("DROP INDEX idx_persona_represented_user"))
    ddl = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'agent_personas'")
    ).scalar_one()
    indexes = [
        r[0]
        for r in conn.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'agent_personas' AND sql IS NOT NULL"
        ))
    ]
    conn.execute(sa.text("DROP TABLE agent_personas"))
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    kept = [p.strip() for p in _split_top_level(body) if "represented_user" not in p]
    conn.execute(sa.text(f"CREATE TABLE agent_personas ({', '.join(kept)})"))
    for sql in indexes:
        conn.execute(sa.text(sql))


@pytest.fixture
def pre_080_db(tmp_path: Path) -> Path:
    path = tmp_path / "m080.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        strip_080(conn)
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m080', 'o', 'o', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO agent_personas (id, org_id, name, role, created_by) "
            "VALUES ('per_old', 'org_m080', 'kelvin', 'r', 'u')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "079")
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


def test_upgrade_adds_null_column_and_downgrade_reverses(pre_080_db: Path) -> None:
    assert "represented_user_id" not in _columns(pre_080_db)
    config = _cfg(f"sqlite+aiosqlite:///{pre_080_db}")
    command.upgrade(config, "080")
    assert "represented_user_id" in _columns(pre_080_db)
    engine = sa.create_engine(f"sqlite:///{pre_080_db}")
    try:
        with engine.connect() as conn:
            value = conn.execute(sa.text(
                "SELECT represented_user_id FROM agent_personas WHERE id = 'per_old'"
            )).scalar_one()
        # Existing personas grant nobody edit rights on upgrade.
        assert value is None
    finally:
        engine.dispose()
    command.downgrade(config, "079")
    assert "represented_user_id" not in _columns(pre_080_db)
    command.upgrade(config, "080")
    assert "represented_user_id" in _columns(pre_080_db)


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
    """Against the full real prior chain (001-079) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "079")
    command.upgrade(config, "080")

    def check(conn) -> None:
        names = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'agent_personas'::regclass"
            ))
        }
        assert {
            "fk_agent_personas_represented_user",
            "ck_agent_personas_represented_user_org",
            "ck_agent_personas_scope",
        } <= names
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_pg080', 'o', 'o-pg080', '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, display_name, tier, email_verified) "
            "VALUES ('u_pg080', 'pg080@example.com', 'u', 'team', true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
            "VALUES ('proj_pg080', 'p', 'github.com/x/pg080', '', 'u_pg080')"
        ))
        # A project persona cannot represent anyone.
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO agent_personas (id, project_id, name, role, created_by, represented_user_id) "
                "VALUES ('per_pg080p', 'proj_pg080', 'a', 'r', 'u_pg080', 'u_pg080')"
            ))
        nested.rollback()
        conn.execute(sa.text(
            "INSERT INTO agent_personas (id, org_id, name, role, created_by, represented_user_id) "
            "VALUES ('per_pg080', 'org_pg080', 'kelvin', 'r', 'u_pg080', 'u_pg080')"
        ))
        # Deleting the user clears the link instead of failing or cascading.
        conn.execute(sa.text("DELETE FROM projects WHERE id = 'proj_pg080'"))
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg080'"))
        value = conn.execute(sa.text(
            "SELECT represented_user_id FROM agent_personas WHERE id = 'per_pg080'"
        )).scalar_one()
        assert value is None
        conn.execute(sa.text("DELETE FROM organizations WHERE id = 'org_pg080'"))

    _pg(url, check)
    command.downgrade(config, "079")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_personas'"
            ))
        }
        assert "represented_user_id" not in cols

    _pg(url, gone)
    command.upgrade(config, "head")
