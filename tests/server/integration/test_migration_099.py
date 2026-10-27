"""Migration 099: Confluence documentation integration tables (GitHub #99).

Purely additive — three new tables plus a ``publish`` opt-in column on
``knowledge_pages``, no existing rows change. SQLite round-trip verifies the
tables, columns, PK/unique/index/FK shapes, the ``publish`` default, and that
downgrade drops them. PostgreSQL runs the real chain when SFS_TEST_PG_URL is
set. The parent revision is read from the migration's ``down_revision``
(re-chained at merge time if another migration lands first); the tests skip
only if that parent script is missing.
"""

from __future__ import annotations

import os
import re
import sqlite3
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

_NEW_TABLES = {
    "org_doc_connections",
    "doc_space_mappings",
    "doc_page_links",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _down_revision() -> str:
    text = (_VERSIONS / "099_confluence.py").read_text()
    match = re.search(r'^down_revision\s*=\s*["\'](.+?)["\']', text, re.M)
    assert match, "could not read down_revision from the 099 migration"
    return match.group(1)


def _parent_present() -> bool:
    prior = _down_revision()
    return any(p.name.startswith(f"{prior}_") for p in _VERSIONS.iterdir())


def _require_parent() -> str:
    prior = _down_revision()
    if not _parent_present():
        pytest.skip(f"parent revision {prior!r} not merged yet")
    return prior


@pytest.fixture
def pre_099_db(tmp_path: Path) -> Path:
    prior = _require_parent()
    path = tmp_path / "m099.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in _NEW_TABLES:
            conn.execute(sa.text(f"DROP TABLE {table}"))
        conn.execute(sa.text("ALTER TABLE knowledge_pages DROP COLUMN publish"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), prior)
    return path


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }


def _fk_target(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        if row[3] == column:
            return row[2]
    return None


def _unique_columns(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if row[2] != 1:  # not unique
            continue
        cols = tuple(
            c[2] for c in conn.execute(f"PRAGMA index_info({row[1]})")
        )
        out.add(cols)
    return out


def test_sqlite_round_trip(pre_099_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_099_db}")
    command.upgrade(config, "099")

    conn = sqlite3.connect(str(pre_099_db))
    assert _NEW_TABLES <= _tables(conn)

    assert {
        "org_id", "provider", "site_url", "account_email", "encrypted_api_token",
        "default_template", "batch_window_minutes", "enabled",
        "connected_by_user_id", "last_verified_at", "created_at", "updated_at",
    } <= _columns(conn, "org_doc_connections")
    assert {
        "id", "org_id", "project_id", "space_key", "space_id", "parent_page_id",
        "template", "publish_context", "auto_publish_pages", "created_by_user_id",
        "last_attempted_at", "created_at", "updated_at",
    } <= _columns(conn, "doc_space_mappings")
    assert {
        "id", "org_id", "project_id", "source_kind", "source_key", "site_url", "space_key",
        "page_id", "version", "last_content_hash", "last_published_storage",
        "template_slug", "template_version",
        "status", "claim_token", "claimed_at", "last_pushed_at",
        "last_pushed_by_user_id", "last_push_failed_at", "last_push_failure",
        "created_at", "updated_at",
    } <= _columns(conn, "doc_page_links")

    # The publish opt-in column landed on knowledge_pages with a false default.
    publish_cols = [
        row for row in conn.execute("PRAGMA table_info(knowledge_pages)")
        if row[1] == "publish"
    ]
    assert len(publish_cols) == 1
    assert publish_cols[0][3] == 1  # NOT NULL
    # A fresh row without an explicit publish value defaults to false.
    conn.execute(
        "INSERT INTO knowledge_pages (id, project_id, slug, title, page_type) "
        "VALUES ('kp_m099', 'proj_m099', 'a', 'A', 'wiki')"
    )
    assert bool(conn.execute(
        "SELECT publish FROM knowledge_pages WHERE id = 'kp_m099'"
    ).fetchone()[0]) is False

    # Primary keys.
    pk_conn = {
        row[1] for row in conn.execute(
            "SELECT * FROM pragma_table_info('org_doc_connections') WHERE pk > 0"
        )
    }
    assert pk_conn == {"org_id"}
    assert {
        row[1] for row in conn.execute(
            "SELECT * FROM pragma_table_info('doc_space_mappings') WHERE pk > 0"
        )
    } == {"id"}
    assert {
        row[1] for row in conn.execute(
            "SELECT * FROM pragma_table_info('doc_page_links') WHERE pk > 0"
        )
    } == {"id"}

    # Unique constraints.
    assert ("project_id",) in _unique_columns(conn, "doc_space_mappings")
    assert ("project_id", "source_kind", "source_key") in _unique_columns(
        conn, "doc_page_links"
    )

    # Indexes.
    indexes = _indexes(conn)
    assert {
        "idx_doc_space_mapping_org",
        "idx_doc_space_mapping_attempted",
        "uq_doc_page_link_site_page",
        "idx_doc_page_link_org",
        "idx_doc_page_link_status",
    } <= indexes

    # Foreign keys (CASCADE).
    assert _fk_target(conn, "org_doc_connections", "org_id") == "organizations"
    assert _fk_target(conn, "doc_space_mappings", "org_id") == "organizations"
    assert _fk_target(conn, "doc_space_mappings", "project_id") == "projects"
    assert _fk_target(conn, "doc_page_links", "project_id") == "projects"
    conn.close()

    command.downgrade(config, _down_revision())
    conn = sqlite3.connect(str(pre_099_db))
    assert not (_NEW_TABLES & _tables(conn))
    assert "publish" not in _columns(conn, "knowledge_pages")
    conn.close()

    command.upgrade(config, "099")  # idempotent re-upgrade
    conn = sqlite3.connect(str(pre_099_db))
    assert _NEW_TABLES <= _tables(conn)
    conn.close()


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
def test_round_trip_on_postgres() -> None:
    prior = _require_parent()
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, prior)
    command.upgrade(config, "099")

    def check(conn) -> None:
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('org_doc_connections', 'doc_space_mappings', 'doc_page_links')"
        )).all()
        assert {r[0] for r in rows} == _NEW_TABLES

        uniques = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'doc_page_links'::regclass"
            ))
        }
        assert "uq_doc_page_link_source" in uniques
        mapping_uniques = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'doc_space_mappings'::regclass"
            ))
        }
        assert "uq_doc_space_mapping_project" in mapping_uniques

        cols = {
            r[0]: (r[1], r[2])
            for r in conn.execute(sa.text(
                "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'knowledge_pages' "
                "AND column_name = 'publish'"
            ))
        }
        assert cols["publish"] == ("NO", "boolean")

        new_cols = {
            (r[0], r[1]): r[2]
            for r in conn.execute(sa.text(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND ("
                "(table_name = 'doc_space_mappings' AND column_name IN ('space_id', 'last_attempted_at')) "
                "OR (table_name = 'doc_page_links' AND column_name IN ('site_url', 'space_key', 'last_published_storage')))"
            ))
        }
        assert new_cols == {
            ("doc_space_mappings", "space_id"): "YES",
            ("doc_space_mappings", "last_attempted_at"): "YES",
            ("doc_page_links", "site_url"): "YES",
            ("doc_page_links", "space_key"): "YES",
            ("doc_page_links", "last_published_storage"): "YES",
        }
        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'doc_space_mappings'"
            ))
        }
        assert "idx_doc_space_mapping_attempted" in indexes
        link_indexes = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'doc_page_links'"
            ))
        }
        assert "UNIQUE" in link_indexes["uq_doc_page_link_site_page"]
        assert "page_id IS NOT NULL" in link_indexes["uq_doc_page_link_site_page"]

    _pg(url, check)

    command.downgrade(config, prior)

    def after_downgrade(conn) -> None:
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('org_doc_connections', 'doc_space_mappings', 'doc_page_links')"
        )).all()
        assert rows == []
        cols = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'knowledge_pages' "
            "AND column_name = 'publish'"
        )).all()
        assert cols == []

    _pg(url, after_downgrade)
    command.upgrade(config, "head")
