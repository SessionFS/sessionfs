"""Migration 129: actor provenance columns on release_gates and deployments."""

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
_TABLES = ("release_gates", "deployments")
_COLUMNS = {"actor_type", "actor_user_id", "service_key_id", "service_key_name"}

_SEED = [
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_rel', 'rel@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
    "VALUES ('proj_rel', 'rel', 'acme/rel', '', 'u_rel')",
    "INSERT INTO releases (id, project_id, version, tag_sha, source) "
    "VALUES ('rel_old', 'proj_rel', '1.0.0', '', 'manual')",
    "INSERT INTO release_gates (id, release_id, kind, status) "
    "VALUES ('rg_old', 'rel_old', 'ci', 'failed')",
    "INSERT INTO deployments (id, release_id, environment, source) "
    "VALUES ('dep_old', 'rel_old', 'prod', 'manual')",
]


_GATES_128 = """
CREATE TABLE release_gates (
    id VARCHAR(64) PRIMARY KEY,
    release_id VARCHAR(64) NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    kind VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'passed',
    run_id VARCHAR(64),
    url TEXT,
    recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_release_gates_release_kind UNIQUE (release_id, kind),
    CONSTRAINT ck_release_gates_status CHECK (status IN ('passed', 'failed', 'skipped'))
)
"""
_DEPLOYMENTS_128 = """
CREATE TABLE deployments (
    id VARCHAR(64) PRIMARY KEY,
    release_id VARCHAR(64) NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    environment VARCHAR(64) NOT NULL,
    deployed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(20) NOT NULL DEFAULT 'manual',
    CONSTRAINT ck_deployments_source CHECK (source IN ('argocd', 'helm', 'manual'))
)
"""


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m129.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(
        engine,
        tables=[t for name, t in Base.metadata.tables.items() if name not in _TABLES],
    )
    # The two tables in their 128 shape (create_all would build the 129 one).
    with engine.begin() as conn:
        conn.execute(sa.text(_GATES_128))
        conn.execute(sa.text(_DEPLOYMENTS_128))
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t=1)))
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "128")
    command.upgrade(config, "129")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        for table in _TABLES:
            assert _COLUMNS <= _columns(engine, table)
        with engine.begin() as conn:
            # Existing rows keep NULL provenance (never guessed).
            gate = conn.execute(sa.text(
                "SELECT status, actor_type, actor_user_id, service_key_id, "
                "service_key_name FROM release_gates WHERE id = 'rg_old'"
            )).one()
            assert tuple(gate) == ("failed", None, None, None, None)
            dep = conn.execute(sa.text(
                "SELECT environment, actor_type FROM deployments WHERE id = 'dep_old'"
            )).one()
            assert tuple(dep) == ("prod", None)
            conn.execute(sa.text(
                "UPDATE release_gates SET actor_type = 'user', actor_user_id = 'u_rel' "
                "WHERE id = 'rg_old'"
            ))
    finally:
        engine.dispose()

    command.downgrade(config, "128")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        for table in _TABLES:
            assert not (_COLUMNS & _columns(engine, table))
        with engine.begin() as conn:
            assert conn.execute(sa.text(
                "SELECT status FROM release_gates WHERE id = 'rg_old'"
            )).scalar_one() == "failed"
    finally:
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


def _pg_columns(table: str):
    def work(conn) -> set[str]:
        return set(conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            f"AND table_name = '{table}'"
        )).scalars().all())

    return work


def _pg_constraints(table: str):
    def work(conn) -> set[str]:
        return set(conn.execute(sa.text(
            f"SELECT conname FROM pg_constraint WHERE conrelid = '{table}'::regclass"
        )).scalars().all())

    return work


def _pg_raises(url: str, stmt: str) -> bool:
    def work(conn):
        try:
            with conn.begin_nested():
                conn.execute(sa.text(stmt))
        except sa.exc.IntegrityError:
            return True
        return False

    return bool(_pg(url, work))


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="needs SFS_TEST_PG_URL")
def test_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "128")

    def seed(conn):
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))

    _pg(url, seed)
    for table in _TABLES:
        assert not (_COLUMNS & _pg(url, _pg_columns(table)))

    command.upgrade(config, "129")
    for table in _TABLES:
        assert _COLUMNS <= _pg(url, _pg_columns(table))
        assert {f"ck_{table}_actor_type", f"fk_{table}_actor_user"} <= _pg(
            url, _pg_constraints(table)
        )
    gate = _pg(url, lambda conn: tuple(conn.execute(sa.text(
        "SELECT status, actor_type, actor_user_id FROM release_gates WHERE id = 'rg_old'"
    )).one()))
    assert gate == ("failed", None, None)

    assert _pg_raises(url, "UPDATE release_gates SET actor_type = 'bogus' WHERE id = 'rg_old'")
    assert _pg_raises(url, "UPDATE deployments SET actor_type = 'bogus' WHERE id = 'dep_old'")
    assert _pg_raises(
        url, "UPDATE deployments SET actor_user_id = 'u_missing' WHERE id = 'dep_old'"
    )
    _pg(url, lambda conn: conn.execute(sa.text(
        "UPDATE release_gates SET actor_type = 'user', actor_user_id = 'u_rel' "
        "WHERE id = 'rg_old'"
    )))
    _pg(url, lambda conn: conn.execute(sa.text(
        "UPDATE deployments SET actor_type = 'webhook' WHERE id = 'dep_old'"
    )))
    # Deleting the recording user nulls actor_user_id (ON DELETE SET NULL);
    # the gate itself survives.
    _pg(url, lambda conn: conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_gone', 'gone@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    )))
    _pg(url, lambda conn: conn.execute(sa.text(
        "UPDATE deployments SET actor_type = 'user', actor_user_id = 'u_gone' "
        "WHERE id = 'dep_old'"
    )))
    _pg(url, lambda conn: conn.execute(sa.text("DELETE FROM users WHERE id = 'u_gone'")))
    dep = _pg(url, lambda conn: tuple(conn.execute(sa.text(
        "SELECT actor_type, actor_user_id FROM deployments WHERE id = 'dep_old'"
    )).one()))
    assert dep == ("user", None)

    command.downgrade(config, "128")
    for table in _TABLES:
        assert not (_COLUMNS & _pg(url, _pg_columns(table)))
        assert not ({f"ck_{table}_actor_type", f"fk_{table}_actor_user"} & _pg(
            url, _pg_constraints(table)
        ))
    command.upgrade(config, "129")
    command.upgrade(config, "head")
