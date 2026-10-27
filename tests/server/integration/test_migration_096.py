"""Migration 096: project_groups + project_group_members + project_group_access
(the phase-1 backend data model for project groups, GitHub #101).

SQLite: the real metadata minus the three new tables, stamped at 096's parent.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set. The parent revision is read from the script so a
release re-chain needs no test edit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)

NEW_TABLES = {"project_groups", "project_group_members", "project_group_access"}
GROUPS_COLUMNS = {
    "id", "org_id", "slug", "name", "description", "created_by_user_id",
    "created_at", "updated_at", "deleted_at",
}
MEMBERS_COLUMNS = {"group_id", "user_id", "role", "granted_by_user_id", "created_at"}
ACCESS_COLUMNS = {"group_id", "project_id", "added_by_user_id", "created_at"}
GROUPS_INDEXES = {"idx_project_groups_org", "uq_project_groups_org_slug"}
MEMBERS_INDEXES = {"idx_project_group_members_user"}
ACCESS_INDEXES = {"idx_project_group_access_project"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("096")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


_SEED = [
    "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m096', 'o', 'o-m096', '{}')",
    "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
    "VALUES ('u_m096', 'm096@example.com', 'u', 'enterprise', {true}, {true}, CURRENT_TIMESTAMP)",
    "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
    "VALUES ('u_m096b', 'm096b@example.com', 'u2', 'enterprise', {true}, {true}, CURRENT_TIMESTAMP)",
    "INSERT INTO projects (id, name, owner_id, org_id) VALUES ('proj_m096', 'p', 'u_m096', 'org_m096')",
    "INSERT INTO projects (id, name, owner_id, org_id) VALUES ('proj_m096b', 'p2', 'u_m096', 'org_m096')",
]


def _seed_sql(true: str) -> list[str]:
    return [stmt.replace("{true}", true) for stmt in _SEED]


def _exercise(conn, integrity_error: type[Exception]) -> None:
    """Shared behaviour: valid rows insert, the role CHECK, the composite PKs,
    and the partial slug-unique index all refuse bad rows, and deleting a group
    (or a project) cascades the dependent grant rows."""
    conn.execute(sa.text(
        "INSERT INTO project_groups (id, org_id, slug, name) "
        "VALUES ('g_m096', 'org_m096', 'alpha', 'Alpha')"
    ))
    conn.execute(sa.text(
        "INSERT INTO project_group_members (group_id, user_id, role) "
        "VALUES ('g_m096', 'u_m096', 'reader')"
    ))
    conn.execute(sa.text(
        "INSERT INTO project_group_access (group_id, project_id) "
        "VALUES ('g_m096', 'proj_m096')"
    ))

    bad_rows = [
        # Role outside the allowed set.
        "INSERT INTO project_group_members (group_id, user_id, role) "
        "VALUES ('g_m096', 'u_m096b', 'owner')",
        # One grant per (group, user).
        "INSERT INTO project_group_members (group_id, user_id, role) "
        "VALUES ('g_m096', 'u_m096', 'editor')",
        # One project per (group, project).
        "INSERT INTO project_group_access (group_id, project_id) "
        "VALUES ('g_m096', 'proj_m096')",
        # Slug is unique per org among live groups.
        "INSERT INTO project_groups (id, org_id, slug, name) "
        "VALUES ('g_m096b', 'org_m096', 'alpha', 'Alpha 2')",
    ]
    for stmt in bad_rows:
        nested = conn.begin_nested()
        with pytest.raises(integrity_error):
            conn.execute(sa.text(stmt))
        nested.rollback()

    # A soft-deleted group frees its slug: the same (org, slug) can be reused
    # while the original row survives for audit continuity.
    conn.execute(sa.text(
        "UPDATE project_groups SET deleted_at = CURRENT_TIMESTAMP, slug = 'beta' "
        "WHERE id = 'g_m096'"
    ))
    conn.execute(sa.text(
        "INSERT INTO project_groups (id, org_id, slug, name) "
        "VALUES ('g_m096c', 'org_m096', 'beta', 'Beta')"
    ))

    # CASCADE: deleting the group removes its member and access rows.
    conn.execute(sa.text("DELETE FROM project_groups WHERE id = 'g_m096'"))
    assert conn.execute(sa.text(
        "SELECT COUNT(*) FROM project_group_members WHERE group_id = 'g_m096'"
    )).scalar() == 0
    assert conn.execute(sa.text(
        "SELECT COUNT(*) FROM project_group_access WHERE group_id = 'g_m096'"
    )).scalar() == 0

    # CASCADE: deleting a project removes its access rows.
    conn.execute(sa.text(
        "INSERT INTO project_group_access (group_id, project_id) "
        "VALUES ('g_m096c', 'proj_m096b')"
    ))
    conn.execute(sa.text("DELETE FROM projects WHERE id = 'proj_m096b'"))
    assert conn.execute(sa.text(
        "SELECT COUNT(*) FROM project_group_access WHERE project_id = 'proj_m096b'"
    )).scalar() == 0


@pytest.fixture
def pre_096_db(tmp_path: Path) -> Path:
    path = tmp_path / "m096.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name not in NEW_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(NEW_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        for stmt in _seed_sql("1"):
            conn.execute(sa.text(stmt))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _prior_revision())
    return path


def _inspect(path: Path):
    engine = sa.create_engine(f"sqlite:///{path}")
    return engine, sa.inspect(engine)


def test_sqlite_upgrade_and_downgrade(pre_096_db: Path) -> None:
    engine, insp = _inspect(pre_096_db)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{pre_096_db}")
    command.upgrade(config, "096")

    engine, insp = _inspect(pre_096_db)
    try:
        assert NEW_TABLES <= set(insp.get_table_names())
        assert GROUPS_COLUMNS <= {c["name"] for c in insp.get_columns("project_groups")}
        assert MEMBERS_COLUMNS <= {c["name"] for c in insp.get_columns("project_group_members")}
        assert ACCESS_COLUMNS <= {c["name"] for c in insp.get_columns("project_group_access")}
        assert GROUPS_INDEXES <= {i["name"] for i in insp.get_indexes("project_groups")}
        assert MEMBERS_INDEXES <= {i["name"] for i in insp.get_indexes("project_group_members")}
        assert ACCESS_INDEXES <= {i["name"] for i in insp.get_indexes("project_group_access")}
        with engine.connect() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.commit()
            with conn.begin():
                _exercise(conn, sa.exc.IntegrityError)
    finally:
        engine.dispose()

    command.downgrade(config, _prior_revision())
    engine, insp = _inspect(pre_096_db)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    engine.dispose()
    command.upgrade(config, "096")
    engine, insp = _inspect(pre_096_db)
    assert NEW_TABLES <= set(insp.get_table_names())
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
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, _prior_revision())

    def seed(conn) -> None:
        for stmt in _seed_sql("true"):
            conn.execute(sa.text(stmt))

    _pg(url, seed)
    command.upgrade(config, "096")

    def check(conn) -> None:
        for table, columns, indexes in (
            ("project_groups", GROUPS_COLUMNS, GROUPS_INDEXES),
            ("project_group_members", MEMBERS_COLUMNS, MEMBERS_INDEXES),
            ("project_group_access", ACCESS_COLUMNS, ACCESS_INDEXES),
        ):
            present = set(conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalars())
            assert columns <= present, f"{table} missing {columns - present}"
            idx = set(conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"
            ), {"t": table}).scalars())
            assert indexes <= idx, f"{table} missing {indexes - idx}"
        # The role CHECK is enforced server-side.
        role_check = conn.execute(sa.text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'ck_project_group_members_role'"
        )).scalar()
        assert role_check == 1
        _exercise(conn, sa.exc.IntegrityError)

    _pg(url, check)

    command.downgrade(config, _prior_revision())

    def check_downgraded(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )).scalars())
        assert NEW_TABLES.isdisjoint(tables)

    _pg(url, check_downgraded)
