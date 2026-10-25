"""Migration 083: canonical git remotes (GitHub #88).

SQLite: the real metadata stamped at 082. PostgreSQL:
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

# project id -> legacy remote; repo id -> (project id, remote)
_PROJECTS = {
    "p_ssh": "ssh://git@gitlab.example.com:2222/team/app.git",  # alone: rewritten
    "p_canon": "group/project",  # canonical twin already exists
    "p_twin": "https://gitlab.example.com/group/project/",  # collides with p_canon: kept
    "p_ph": "reclaimed:p_ph",  # pre-073 placeholder: untouched
    "p_none": None,
    "p_pair": None,  # two repo rows that canonicalize to the same value
    "p_x": "ssh://git@a.example/shared/lib",  # two non-canonical forms,
    "p_y": "https://b.example/shared/lib/",  # no canonical row: first wins
}
_REPOS = {
    "r_ssh": ("p_ssh", "ssh://git@gitlab.example.com:2222/team/app.git"),
    "r_canon": ("p_canon", "group/project"),
    "r_twin": ("p_twin", "https://gitlab.example.com/group/project/"),
    "r_scp": ("p_none", "deploy@gitlab.example.com:other/repo.git"),
    "r_pair1": ("p_pair", "ssh://git@gl.example/g/pair"),
    "r_pair2": ("p_pair", "g/pair/"),
    "r_home": ("p_none", "alice@box:~/app"),  # personal server: unchanged
}
_SESSIONS = {
    "s1": "ssh://git@gitlab.example.com:2222/team/app.git",
    "s2": "group/project/",
    "s3": "group/project",
    "s4": None,
    "s5": "https://gitlab.example.com/group/project/",  # p_twin's: kept
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _seed(conn) -> None:
    # Build the rows from the ORM models so every column's Python default is
    # filled in on both dialects, but insert only the columns the 082 schema
    # actually has: later migrations add model columns that don't exist yet.
    from datetime import datetime, timezone

    from sessionfs.server.db.models import Project, ProjectRepo, Session, User

    def insert(obj) -> None:
        table = sa.Table(obj.__table__.name, sa.MetaData(), autoload_with=conn)
        values = {}
        for column in obj.__table__.columns:
            if column.name not in table.c:
                continue
            value = getattr(obj, column.key, None)
            if value is None and column.default is not None:
                arg = column.default.arg
                value = arg(None) if callable(arg) else arg
            if value is not None:
                values[column.name] = value
        conn.execute(table.insert().values(**values))

    now = datetime.now(timezone.utc)
    insert(User(id="u_m083", email="m083@example.com", tier="free", created_at=now,
                email_verified=True))
    for pid, remote in _PROJECTS.items():
        insert(Project(id=pid, name=pid, git_remote_normalized=remote, owner_id="u_m083"))
    for rid, (pid, remote) in _REPOS.items():
        insert(ProjectRepo(id=rid, project_id=pid, git_remote_normalized=remote))
    for sid, remote in _SESSIONS.items():
        insert(Session(id=sid, user_id="u_m083", source_tool="claude-code", blob_key="b",
                       etag="e", git_remote_normalized=remote, created_at=now,
                       updated_at=now))


def _check(conn) -> None:
    def col(table: str) -> dict:
        return dict(conn.execute(sa.text(f"SELECT id, git_remote_normalized FROM {table}")).all())

    assert col("projects") == {
        "p_ssh": "team/app",
        "p_canon": "group/project",
        "p_twin": "https://gitlab.example.com/group/project/",
        "p_ph": "reclaimed:p_ph",
        "p_none": None,
        "p_pair": None,
        "p_x": "shared/lib",
        "p_y": "https://b.example/shared/lib/",
    }
    repos = col("project_repos")
    assert {k: repos[k] for k in ("r_ssh", "r_canon", "r_twin", "r_scp", "r_home")} == {
        "r_ssh": "team/app",
        "r_canon": "group/project",
        "r_twin": "https://gitlab.example.com/group/project/",
        "r_scp": "other/repo",
        "r_home": "alice@box:~/app",
    }
    # Exactly one of the pair took the canonical value; the other was kept.
    assert sorted([repos["r_pair1"], repos["r_pair2"]]) == ["g/pair", "g/pair/"] or sorted(
        [repos["r_pair1"], repos["r_pair2"]]
    ) == ["g/pair", "ssh://git@gl.example/g/pair"]
    assert col("sessions") == {
        "s1": "team/app", "s2": "group/project", "s3": "group/project", "s4": None,
        "s5": "https://gitlab.example.com/group/project/",
    }


def test_upgrade_rewrites_remotes_without_collisions(tmp_path: Path, capsys) -> None:
    path = tmp_path / "m083.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _seed(conn)
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "082")

    command.upgrade(config, "083")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            _check(conn)
    finally:
        engine.dispose()
    out = capsys.readouterr().out
    assert "p_twin" in out and "p_canon" in out

    command.downgrade(config, "082")  # no-op
    command.upgrade(config, "083")  # idempotent


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
    """Against the full real prior chain (001-082) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "082")
    _pg(url, _seed)
    command.upgrade(config, "083")
    _pg(url, _check)
    command.downgrade(config, "082")
    command.upgrade(config, "head")
