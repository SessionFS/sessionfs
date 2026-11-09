"""Migration 135: ``distilled_from`` joins ``ck_artifact_edges_relation``.

SQLite: the prior schema is the real metadata with artifact_edges rebuilt in
its 131 shape (the old CHECK), stamped at 134. The downgrade turns
distilled_from edges into derived_from, dropping one only when the pair
already has a derived_from edge. PostgreSQL: ``test_upgrade_on_postgres``
runs the real chain to 134 when SFS_TEST_PG_URL is set.
"""

from __future__ import annotations

import importlib
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
_m135 = importlib.import_module(
    "sessionfs.server.db.migrations.versions.135_artifact_edge_distilled_from"
)

_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m135', 'm135@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m135', 'p', 'acme/m135', 'u_m135', '')",
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_t', 'proj_m135', 'model', 'teacher', '1', 'hf://a/t')",
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_s', 'proj_m135', 'model', 'student', '1', 'hf://a/s')",
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_u', 'proj_m135', 'model', 'student2', '1', 'hf://a/u')",
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_old', 'proj_m135', 'art_t', 'art_u', 'fine_tuned_from')",
)
_DISTILLED = (
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_d1', 'proj_m135', 'art_t', 'art_s', 'distilled_from')"
)
# A distilled_from edge whose pair already has a derived_from edge.
_DISTILLED_TWIN = (
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_d2', 'proj_m135', 'art_t', 'art_u', 'distilled_from')",
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_x2', 'proj_m135', 'art_t', 'art_u', 'derived_from')",
)
_UNKNOWN = (
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_bad', 'proj_m135', 'art_s', 'art_u', 'copied_from')"
)
_AFTER_DOWNGRADE = {
    "ae_old": "fine_tuned_from",
    "ae_d1": "derived_from",
    "ae_x2": "derived_from",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _relations(conn) -> dict[str, str]:
    return dict(conn.execute(sa.text("SELECT id, relation FROM artifact_edges")).all())


@pytest.fixture
def pre_135_db(tmp_path: Path) -> Path:
    path = tmp_path / "m135.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE artifact_edges"))
    referenced = sa.MetaData()
    referenced.reflect(engine, only=["users", "projects", "artifacts"])
    _m135._table(_m135._RELATION_OLD).to_metadata(referenced).create(engine)
    with engine.begin() as conn:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t=1)))
    # The 131 CHECK refuses the new relation.
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(sa.text(_DISTILLED))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "134")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_135_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_135_db}")
    command.upgrade(config, "135")
    engine = sa.create_engine(f"sqlite:///{pre_135_db}")
    try:
        with engine.begin() as conn:
            assert _relations(conn) == {"ae_old": "fine_tuned_from"}  # data survived the rebuild
            conn.execute(sa.text(_DISTILLED))
            for stmt in _DISTILLED_TWIN:
                conn.execute(sa.text(stmt))
        refused = (
            _UNKNOWN,
            _DISTILLED.replace("ae_d1", "ae_d9"),  # duplicate (parent, child, relation)
            _DISTILLED.replace("ae_d1", "ae_d8").replace("'art_s'", "'art_t'"),  # self edge
        )
        for stmt in refused:
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(stmt))
        indexes = {i["name"] for i in sa.inspect(engine).get_indexes("artifact_edges")}
        assert {"idx_artifact_edges_child", "idx_artifact_edges_project"} <= indexes
    finally:
        engine.dispose()

    command.downgrade(config, "134")
    engine = sa.create_engine(f"sqlite:///{pre_135_db}")
    try:
        with engine.begin() as conn:
            assert _relations(conn) == _AFTER_DOWNGRADE
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_DISTILLED.replace("ae_d1", "ae_d3").replace("'art_s'", "'art_u'")))
    finally:
        engine.dispose()
    command.upgrade(config, "135")


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


def _refused(conn, stmt: str) -> None:
    nested = conn.begin_nested()
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(sa.text(stmt))
    nested.rollback()


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "134")

    def seed(conn) -> None:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))
        _refused(conn, _DISTILLED)

    _pg(url, seed)
    command.upgrade(config, "135")

    def check(conn) -> None:
        conn.execute(sa.text(_DISTILLED))
        for stmt in _DISTILLED_TWIN:
            conn.execute(sa.text(stmt))
        _refused(conn, _UNKNOWN)

    _pg(url, check)
    command.downgrade(config, "134")

    def after(conn) -> None:
        assert _relations(conn) == _AFTER_DOWNGRADE
        _refused(conn, _DISTILLED.replace("ae_d1", "ae_d3").replace("'art_s'", "'art_u'"))

    _pg(url, after)
    command.upgrade(config, "head")
