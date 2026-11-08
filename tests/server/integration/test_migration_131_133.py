"""Migrations 131-133: artifacts + lineage, eval suites/cases/requirements,
eval runs/results and the grader agent-run link.

SQLite: the prior schema is the real metadata with the eight new tables
dropped and ``agent_runs`` without ``eval_run_id``, stamped at 130.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain to 130 when
SFS_TEST_PG_URL is set, upgrades through 133 and asserts every CHECK and
unique key, including the dialect-aware ``ck_agent_run_role`` swap.
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

_NEW_TABLES = (
    "eval_case_results",
    "eval_runs",
    "artifact_family_requirements",
    "eval_cases",
    "eval_suites",
    "artifact_tags",
    "artifact_edges",
    "artifacts",
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> dict[str, dict]:
    inspector = sa.inspect(engine)
    if not inspector.has_table(table):
        return {}
    return {c["name"]: c for c in inspector.get_columns(table)}


_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m131', 'm131@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m131', 'p', 'acme/m131', 'u_m131', '')",
)

_GOOD = (
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_1', 'proj_m131', 'dataset', 'notes', 'v1', 's3://b/notes')",
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri, stage) "
    "VALUES ('art_2', 'proj_m131', 'model', 'clin', '2.3', 'hf://acme/clin', 'staging')",
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_1', 'proj_m131', 'art_1', 'art_2', 'trained_on')",
    "INSERT INTO artifact_tags (artifact_id, key, value) VALUES ('art_1', 'phi_compliant', 'v3')",
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256) "
    "VALUES ('evs_1', 'proj_m131', 'evl_1', 1, 'clinical', 'Clinical', '{{}}', '{{}}', 'h')",
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256, active) "
    "VALUES ('evs_0', 'proj_m131', 'evl_1', 2, 'clinical', 'Clinical', '{{}}', '{{}}', 'h', {f})",
    "INSERT INTO eval_cases (id, project_id, lineage_id, case_key, added_in_version, content_sha256) "
    "VALUES ('evc_1', 'proj_m131', 'evl_1', 'c1', 1, 'h')",
    "INSERT INTO artifact_family_requirements (id, project_id, kind, name) "
    "VALUES ('afr_1', 'proj_m131', 'model', 'clin')",
    "INSERT INTO eval_runs (id, project_id, suite_id, suite_lineage_id, artifact_id, grader_persona) "
    "VALUES ('evr_1', 'proj_m131', 'evs_1', 'evl_1', 'art_2', 'grader')",
    "INSERT INTO eval_case_results (id, run_id, case_id, case_key, verdict, score, body_sha256) "
    "VALUES ('evx_1', 'evr_1', 'evc_1', 'c1', 'pass', 0.95, 'b')",
)

_BAD = (
    # unknown kind / stage
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_b1', 'proj_m131', 'weights', 'n', 'v', 's3://b/x')",
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri, stage) "
    "VALUES ('art_b2', 'proj_m131', 'model', 'n', 'v', 's3://b/x', 'live')",
    # duplicate family version
    "INSERT INTO artifacts (id, project_id, kind, name, version, external_uri) "
    "VALUES ('art_b3', 'proj_m131', 'dataset', 'notes', 'v1', 's3://b/other')",
    # self edge, unknown relation, duplicate edge
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_b1', 'proj_m131', 'art_1', 'art_1', 'uses')",
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_b2', 'proj_m131', 'art_1', 'art_2', 'copied_from')",
    "INSERT INTO artifact_edges (id, project_id, parent_id, child_id, relation) "
    "VALUES ('ae_b3', 'proj_m131', 'art_1', 'art_2', 'trained_on')",
    # duplicate tag key
    "INSERT INTO artifact_tags (artifact_id, key, value) VALUES ('art_1', 'phi_compliant', 'v4')",
    # second active version in a lineage; active slug clash in the project
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256) "
    "VALUES ('evs_b1', 'proj_m131', 'evl_1', 3, 'clinical', 'C', '{{}}', '{{}}', 'h')",
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256) "
    "VALUES ('evs_b2', 'proj_m131', 'evl_2', 1, 'clinical', 'C', '{{}}', '{{}}', 'h')",
    # bad storage mode, version 0
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256, case_storage) "
    "VALUES ('evs_b3', 'proj_m131', 'evl_3', 1, 's3', 'C', '{{}}', '{{}}', 'h', 'remote')",
    "INSERT INTO eval_suites (id, project_id, lineage_id, version, slug, name, grader, acceptance, definition_sha256) "
    "VALUES ('evs_b4', 'proj_m131', 'evl_4', 0, 's4', 'C', '{{}}', '{{}}', 'h')",
    # duplicate case key in a lineage; retired before added
    "INSERT INTO eval_cases (id, project_id, lineage_id, case_key, added_in_version, content_sha256) "
    "VALUES ('evc_b1', 'proj_m131', 'evl_1', 'c1', 2, 'h')",
    "INSERT INTO eval_cases (id, project_id, lineage_id, case_key, added_in_version, retired_in_version, content_sha256) "
    "VALUES ('evc_b2', 'proj_m131', 'evl_1', 'c2', 2, 2, 'h')",
    # duplicate family requirement
    "INSERT INTO artifact_family_requirements (id, project_id, kind, name) "
    "VALUES ('afr_b1', 'proj_m131', 'model', 'clin')",
    # bad state / verdict / trigger
    "INSERT INTO eval_runs (id, project_id, suite_id, suite_lineage_id, grader_persona, state) "
    "VALUES ('evr_b1', 'proj_m131', 'evs_1', 'evl_1', 'g', 'paused')",
    "INSERT INTO eval_runs (id, project_id, suite_id, suite_lineage_id, grader_persona, verdict) "
    "VALUES ('evr_b2', 'proj_m131', 'evs_1', 'evl_1', 'g', 'ok')",
    "INSERT INTO eval_runs (id, project_id, suite_id, suite_lineage_id, grader_persona, trigger_source) "
    "VALUES ('evr_b3', 'proj_m131', 'evs_1', 'evl_1', 'g', 'cron')",
    # duplicate (run, case), bad verdict, score out of range
    "INSERT INTO eval_case_results (id, run_id, case_id, case_key, verdict, body_sha256) "
    "VALUES ('evx_b1', 'evr_1', 'evc_1', 'c1', 'fail', 'b')",
    "INSERT INTO eval_case_results (id, run_id, case_id, case_key, verdict, body_sha256) "
    "VALUES ('evx_b2', 'evr_1', 'evc_1', 'c1', 'maybe', 'b')",
    "INSERT INTO eval_case_results (id, run_id, case_id, case_key, verdict, score, body_sha256) "
    "VALUES ('evx_b3', 'evr_1', 'evc_1', 'c9', 'pass', 1.5, 'b')",
)


@pytest.fixture
def pre_131_db(tmp_path: Path) -> Path:
    path = tmp_path / "m131.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in _NEW_TABLES:
            conn.execute(sa.text(f"DROP TABLE {table}"))
        conn.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN eval_run_id"))
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t=1)))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "130")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_131_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_131_db}")
    command.upgrade(config, "133")
    engine = sa.create_engine(f"sqlite:///{pre_131_db}")
    try:
        assert {"kind", "name", "version", "external_uri", "stage", "deprecated_at"} <= set(
            _columns(engine, "artifacts")
        )
        assert {"lineage_id", "version", "active", "slug", "case_storage"} <= set(
            _columns(engine, "eval_suites")
        )
        assert {"verdict", "trusted", "grader_service_key_id", "summary"} <= set(
            _columns(engine, "eval_runs")
        )
        assert _columns(engine, "agent_runs")["eval_run_id"]["nullable"] is True
        with engine.begin() as conn:
            for stmt in _GOOD:
                conn.execute(sa.text(stmt.format(f=0)))
        for bad in _BAD:
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(bad.format(f=0)))
    finally:
        engine.dispose()

    command.downgrade(config, "130")
    engine = sa.create_engine(f"sqlite:///{pre_131_db}")
    try:
        for table in _NEW_TABLES:
            assert not _columns(engine, table), table
        assert "eval_run_id" not in _columns(engine, "agent_runs")
    finally:
        engine.dispose()
    command.upgrade(config, "133")


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


_RUN = (
    "INSERT INTO agent_runs (id, project_id, persona_name, tool, trigger_source, status, "
    "created_at, role, eval_run_id) VALUES ('{id}', 'proj_m131', 'grader', 'generic', 'ci', "
    "'running', now(), '{role}', 'evr_1')"
)


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "130")

    def seed(conn) -> None:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))

    _pg(url, seed)
    command.upgrade(config, "133")

    def check(conn) -> None:
        for stmt in _GOOD:
            conn.execute(sa.text(stmt.format(f="false")))
        conn.execute(sa.text(_RUN.format(id="run_g1", role="grader")))
        for bad in (*_BAD, _RUN.format(id="run_b1", role="judge")):
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad.format(f="false")))
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, "130")

    def gone(conn) -> None:
        tables = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )).all()
        }
        for table in _NEW_TABLES:
            assert table not in tables, table
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_runs'"
            )).all()
        }
        assert "eval_run_id" not in cols
        # The downgrade kept the grader run and dropped only its role.
        assert conn.execute(sa.text(
            "SELECT role FROM agent_runs WHERE id = 'run_g1'"
        )).scalar() is None
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "UPDATE agent_runs SET role = 'grader' WHERE id = 'run_g1'"
            ))
        nested.rollback()

    _pg(url, gone)
    command.upgrade(config, "head")
