"""Migration 097: org-custom Jira templates + link template pinning.

SQLite: the prior schema is the real metadata minus the two new tables and
the two new link columns, stamped at 097's parent. PostgreSQL:
``test_upgrade_on_postgres`` runs the real chain when SFS_TEST_PG_URL is set.

097's parent (096) is owned by another branch and re-chained at merge time, so
this test skips until that parent script is present in the versions directory.
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

NEW_TABLES = {"jira_templates", "jira_template_versions"}
TEMPLATE_COLUMNS = {
    "id", "org_id", "slug", "name", "description", "sections", "version",
    "created_by_user_id", "updated_by_user_id", "created_at", "updated_at",
    "archived_at",
}
VERSION_COLUMNS = {
    "id", "template_id", "version", "sections", "created_by_user_id", "created_at",
}
LINK_COLUMNS = {"template_slug", "template_version"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _down_revision() -> str:
    text = (_VERSIONS / "097_jira_custom_templates.py").read_text()
    match = re.search(r'^down_revision\s*=\s*["\'](.+?)["\']', text, re.M)
    assert match, "could not read down_revision from the 097 migration"
    return match.group(1)


def _parent_present() -> bool:
    prior = _down_revision()
    return any(p.name.startswith(f"{prior}_") for p in _VERSIONS.iterdir())


def _require_parent() -> str:
    prior = _down_revision()
    if not _parent_present():
        pytest.skip(f"parent revision {prior!r} not merged yet")
    return prior


def _exercise(conn, integrity_error: type[Exception]) -> None:
    """Behaviour shared by both dialects: valid rows insert, the unique
    constraints refuse duplicates, and deleting a template cascades its
    version snapshots."""
    conn.execute(sa.text(
        "INSERT INTO jira_templates (id, org_id, slug, name, sections, "
        "created_by_user_id, updated_by_user_id) "
        "VALUES ('jt_m097', 'org_m097', 'design-lite', 'Design Lite', '[]', "
        "'u_m097', 'u_m097')"
    ))
    bad_rows = [
        # slug is unique per org.
        "INSERT INTO jira_templates (id, org_id, slug, name, sections, "
        "created_by_user_id, updated_by_user_id) "
        "VALUES ('jt_m097b', 'org_m097', 'design-lite', 'Dup', '[]', 'u_m097', 'u_m097')",
    ]
    for stmt in bad_rows:
        nested = conn.begin_nested()
        with pytest.raises(integrity_error):
            conn.execute(sa.text(stmt))
        nested.rollback()

    # version starts at 1.
    assert conn.execute(sa.text(
        "SELECT version FROM jira_templates WHERE id = 'jt_m097'"
    )).scalar() == 1

    # version snapshots are unique per (template, version).
    conn.execute(sa.text(
        "INSERT INTO jira_template_versions (id, template_id, version, sections, "
        "created_by_user_id) "
        "VALUES ('jtv_m097', 'jt_m097', 1, '[]', 'u_m097')"
    ))
    nested = conn.begin_nested()
    with pytest.raises(integrity_error):
        conn.execute(sa.text(
            "INSERT INTO jira_template_versions (id, template_id, version, sections, "
            "created_by_user_id) "
            "VALUES ('jtv_m097b', 'jt_m097', 1, '[]', 'u_m097')"
        ))
    nested.rollback()

    # deleting the template cascades its version snapshots.
    conn.execute(sa.text("DELETE FROM jira_templates WHERE id = 'jt_m097'"))
    assert conn.execute(sa.text(
        "SELECT count(*) FROM jira_template_versions WHERE template_id = 'jt_m097'"
    )).scalar() == 0


@pytest.fixture
def pre_097_db(tmp_path: Path) -> Path:
    prior = _require_parent()
    path = tmp_path / "m097.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE jira_template_versions"))
        conn.execute(sa.text("DROP TABLE jira_templates"))
        conn.execute(sa.text(
            "ALTER TABLE ticket_tracker_links DROP COLUMN template_version"
        ))
        conn.execute(sa.text(
            "ALTER TABLE ticket_tracker_links DROP COLUMN template_slug"
        ))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES ('org_m097', 'o', 'o-m097')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), prior)
    return path


def test_sqlite_upgrade_and_downgrade(pre_097_db: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{pre_097_db}")
    insp = sa.inspect(engine)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    assert LINK_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_tracker_links")})
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{pre_097_db}")
    command.upgrade(config, "097")

    engine = sa.create_engine(f"sqlite:///{pre_097_db}")
    insp = sa.inspect(engine)
    try:
        assert NEW_TABLES <= set(insp.get_table_names())
        assert TEMPLATE_COLUMNS <= {c["name"] for c in insp.get_columns("jira_templates")}
        assert VERSION_COLUMNS <= {c["name"] for c in insp.get_columns("jira_template_versions")}
        assert LINK_COLUMNS <= {c["name"] for c in insp.get_columns("ticket_tracker_links")}
        # New link columns are nullable (existing links have no template).
        link_cols = {c["name"]: c for c in insp.get_columns("ticket_tracker_links")}
        assert link_cols["template_slug"]["nullable"]
        assert link_cols["template_version"]["nullable"]
        with engine.connect() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.commit()
            with conn.begin():
                _exercise(conn, sa.exc.IntegrityError)
    finally:
        engine.dispose()

    command.downgrade(config, _down_revision())
    engine = sa.create_engine(f"sqlite:///{pre_097_db}")
    insp = sa.inspect(engine)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    assert LINK_COLUMNS.isdisjoint({c["name"] for c in insp.get_columns("ticket_tracker_links")})
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
            "VALUES ('org_m097', 'Org', 'org-m097')"
        ))

    _pg(url, seed)
    command.upgrade(config, "097")

    def check(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
        assert NEW_TABLES <= tables
        for table, columns in (
            ("jira_templates", TEMPLATE_COLUMNS),
            ("jira_template_versions", VERSION_COLUMNS),
        ):
            present = set(conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalars())
            assert columns <= present, f"{table} missing {columns - present}"
        link_cols = {
            row[0]: row[1]
            for row in conn.execute(sa.text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'ticket_tracker_links' "
                "AND column_name IN ('template_slug', 'template_version')"
            ))
        }
        assert set(link_cols) == LINK_COLUMNS
        assert all(nullable == "YES" for nullable in link_cols.values())
        _exercise(conn, sa.exc.IntegrityError)

    _pg(url, check)

    command.downgrade(config, prior)

    def gone(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
        assert NEW_TABLES.isdisjoint(tables)
        cols = set(conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'ticket_tracker_links'"
        )).scalars())
        assert LINK_COLUMNS.isdisjoint(cols)

    _pg(url, gone)
    command.upgrade(config, "head")
