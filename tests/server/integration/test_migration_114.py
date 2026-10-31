"""Migration 114: pull_requests, releases, gates and deployments (track F1).

SQLite: the prior schema is the real metadata minus the six new tables,
stamped at 112. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain
when SFS_TEST_PG_URL is set and asserts column types, nullability, the inline
CHECK constraints and the ``(project_id, provider, repo, number)`` /
``(project_id, version)`` uniqueness constraints.
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

COLUMNS = {
    "pull_requests": {
        "id", "project_id", "provider", "external_id", "repo", "number",
        "title", "state", "author_user_id", "branch", "head_sha",
        "merged_at", "merge_commit_sha", "external_url", "created_at",
        "updated_at",
    },
    "ticket_pr_links": {"ticket_id", "pr_id", "link_source"},
    "releases": {
        "id", "project_id", "version", "tag_sha", "previous_tag_sha",
        "released_at", "source", "created_by_user_id", "service_key_id",
        "created_at",
    },
    "release_prs": {"release_id", "pr_id"},
    "release_gates": {
        "id", "release_id", "kind", "status", "run_id", "url", "recorded_at",
    },
    "deployments": {
        "id", "release_id", "environment", "deployed_at", "source",
    },
}

# name -> (pg data_type, character_maximum_length, is_nullable)
PG_COLUMNS = {
    "pull_requests": {
        "id": ("character varying", 64, "NO"),
        "project_id": ("character varying", 64, "NO"),
        "provider": ("character varying", 20, "NO"),
        "external_id": ("character varying", 200, "YES"),
        "repo": ("character varying", 200, "NO"),
        "number": ("integer", None, "NO"),
        "title": ("character varying", 500, "NO"),
        "state": ("character varying", 20, "NO"),
        "author_user_id": ("character varying", 64, "YES"),
        "branch": ("character varying", 200, "NO"),
        "head_sha": ("character varying", 64, "YES"),
        "merged_at": ("timestamp with time zone", None, "YES"),
        "merge_commit_sha": ("character varying", 64, "YES"),
        "external_url": ("text", None, "YES"),
        "created_at": ("timestamp with time zone", None, "NO"),
        "updated_at": ("timestamp with time zone", None, "NO"),
    },
    "ticket_pr_links": {
        "ticket_id": ("character varying", 64, "NO"),
        "pr_id": ("character varying", 64, "NO"),
        "link_source": ("character varying", 20, "NO"),
    },
    "releases": {
        "id": ("character varying", 64, "NO"),
        "project_id": ("character varying", 64, "NO"),
        "version": ("character varying", 100, "NO"),
        "tag_sha": ("character varying", 64, "NO"),
        "previous_tag_sha": ("character varying", 64, "YES"),
        "released_at": ("timestamp with time zone", None, "YES"),
        "source": ("character varying", 20, "NO"),
        "created_by_user_id": ("character varying", 64, "YES"),
        "service_key_id": ("character varying", 36, "YES"),
        "created_at": ("timestamp with time zone", None, "NO"),
    },
    "release_prs": {
        "release_id": ("character varying", 64, "NO"),
        "pr_id": ("character varying", 64, "NO"),
    },
    "release_gates": {
        "id": ("character varying", 64, "NO"),
        "release_id": ("character varying", 64, "NO"),
        "kind": ("character varying", 64, "NO"),
        "status": ("character varying", 20, "NO"),
        "run_id": ("character varying", 64, "YES"),
        "url": ("text", None, "YES"),
        "recorded_at": ("timestamp with time zone", None, "NO"),
    },
    "deployments": {
        "id": ("character varying", 64, "NO"),
        "release_id": ("character varying", 64, "NO"),
        "environment": ("character varying", 64, "NO"),
        "deployed_at": ("timestamp with time zone", None, "NO"),
        "source": ("character varying", 20, "NO"),
    },
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table(table):
            return set()
        return {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()


def _seed_project(conn) -> None:
    """A minimal org → user → project chain so PR/release rows have a home."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m114', 'o', 'o-m114')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m114', 'm114@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))
    conn.execute(sa.text(
        "INSERT INTO projects (id, name, owner_id) VALUES ('p_m114', 'p', 'u_m114')"
    ))


@pytest.fixture
def pre_114_db(tmp_path: Path) -> Path:
    path = tmp_path / "m114.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-114 schema: the real metadata minus the six tables.
        for table in ("deployments", "release_gates", "release_prs", "releases",
                      "ticket_pr_links", "pull_requests"):
            conn.execute(sa.text(f"DROP TABLE {table}"))
        _seed_project(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "113")
    return path


def test_upgrade_adds_tables_and_downgrade_reverses(pre_114_db: Path) -> None:
    for table in COLUMNS:
        assert table not in _columns(pre_114_db, table)

    config = _cfg(f"sqlite+aiosqlite:///{pre_114_db}")
    command.upgrade(config, "114")
    for table, cols in COLUMNS.items():
        assert cols <= _columns(pre_114_db, table), table

    # Server defaults + uniqueness + CHECK are real, not just Python-side.
    engine = sa.create_engine(f"sqlite:///{pre_114_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO pull_requests (id, project_id, provider, repo, number, title, branch) "
                "VALUES ('pr_m114', 'p_m114', 'github', 'o/r', 7, 'T', 'main')"
            ))
            row = conn.execute(sa.text(
                "SELECT state FROM pull_requests WHERE id = 'pr_m114'"
            )).one()
            assert row == ("open",)
            # Uniqueness on (project, provider, repo, number).
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO pull_requests (id, project_id, provider, repo, number, title, branch) "
                    "VALUES ('pr_m114_dup', 'p_m114', 'github', 'o/r', 7, 'T', 'main')"
                ))
            # CHECK on provider.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO pull_requests (id, project_id, provider, repo, number, title, branch) "
                    "VALUES ('pr_m114_bad', 'p_m114', 'bitbucket', 'o/r', 8, 'T', 'main')"
                ))

            conn.execute(sa.text(
                "INSERT INTO releases (id, project_id, version, tag_sha) "
                "VALUES ('rel_m114', 'p_m114', '1.0.0', 'abc123')"
            ))
            row = conn.execute(sa.text(
                "SELECT source FROM releases WHERE id = 'rel_m114'"
            )).one()
            assert row == ("manual",)
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO releases (id, project_id, version, tag_sha) "
                    "VALUES ('rel_m114_dup', 'p_m114', '1.0.0', 'abc123')"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "113")
    for table in COLUMNS:
        assert table not in _columns(pre_114_db, table)
    command.upgrade(config, "114")
    for table, cols in COLUMNS.items():
        assert cols <= _columns(pre_114_db, table), table


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None, str]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-113) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "113")

    def seed(conn) -> None:
        _seed_project(conn)

    _pg(url, seed)
    command.upgrade(config, "114")

    def check(conn) -> None:
        for table, expected in PG_COLUMNS.items():
            cols = _pg_columns(conn, table)
            for name, wanted in expected.items():
                assert cols[name] == wanted, (table, name, cols[name])

    _pg(url, check)
    command.downgrade(config, "113")

    def gone(conn) -> None:
        for table in PG_COLUMNS:
            assert not _pg_columns(conn, table)

    _pg(url, gone)
    command.upgrade(config, "head")
