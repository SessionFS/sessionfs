"""Migration 082: GitLab CI trusted publishing (GitHub #82).

SQLite: the prior schema is the real metadata with skill_trusted_publishers
in its 071 shape (GitHub-only CHECK, NOT NULL GitHub columns, no GitLab
columns). PostgreSQL: ``test_round_trip_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set: an existing GitHub trust (and its token) survives
upgrade, downgrade and re-upgrade; GitLab trusts are removed on downgrade.
"""

from __future__ import annotations

import os
import sqlite3
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
_TABLE = "skill_trusted_publishers"


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    """082's parent, read from the script so a re-chain needs no test edit."""
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("082")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


def _trusted_071(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        _TABLE,
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("publisher_id", sa.String(64), sa.ForeignKey("skill_publishers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("package_name", sa.String(100), nullable=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="github"),
        sa.Column("repository_owner_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(200), nullable=False),
        sa.Column("workflow_path", sa.String(255), nullable=False),
        sa.Column("reusable_workflow", sa.String(300), nullable=True),
        sa.Column("allow_workflow_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("environment", sa.String(255), nullable=True),
        sa.Column("ref_pattern", sa.String(255), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("provider IN ('github')", name="ck_skill_trusted_publisher_provider"),
        sa.Index("idx_skill_trusted_publishers_repo", "provider", "repository_id"),
        sa.Index("idx_skill_trusted_publishers_publisher", "publisher_id"),
    )


def _prior_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name != _TABLE:
            table.to_metadata(metadata)
    _trusted_071(metadata)
    return metadata


@pytest.fixture
def pre_082_db(tmp_path: Path) -> Path:
    path = tmp_path / "m082.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _prior_revision())
    return path


_GITHUB_ROW = (
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
    "repository_id, repository, workflow_path) VALUES ('stp_gh', 'p1', 'github', 1, 2, 'o/r', "
    "'.github/workflows/r.yml')"
)
_GITLAB_ROW = (
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id, gitlab_namespace_id, ref_pattern) VALUES ('stp_gl', 'p1', 'gitlab', "
    "'acme/skills', 'https://gitlab.com', 4242, 77, 'refs/tags/v*')"
)
_TOKEN = (
    "INSERT INTO skill_publish_tokens (id, token_hash, trusted_publisher_id, publisher_id, "
    "oidc_issuer, oidc_jti, expires_at) VALUES (?, ?, ?, 'p1', 'iss', ?, '2030-01-01')"
)
# Rows the new shape CHECK must refuse.
_BAD_ROWS = (
    # Unknown provider.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id) VALUES ('b1', 'p1', 'bitbucket', 'x', 'https://x', 1)",
    # GitLab without a project id.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer) "
    "VALUES ('b2', 'p1', 'gitlab', 'x', 'https://gitlab.com')",
    # GitLab without an issuer.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, "
    "gitlab_project_id) VALUES ('b3', 'p1', 'gitlab', 'x', 5)",
    # GitLab carrying GitHub fields.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id, workflow_path) VALUES ('b4', 'p1', 'gitlab', 'x', 'https://gitlab.com', 5, "
    "'.github/workflows/r.yml')",
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id, allow_workflow_run) VALUES ('b5', 'p1', 'gitlab', 'x', 'https://gitlab.com', 5, true)",
    # GitHub without its ids, or carrying GitLab fields.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, workflow_path) "
    "VALUES ('b6', 'p1', 'github', 'o/r', '.github/workflows/r.yml')",
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
    "repository_id, repository, workflow_path, gitlab_project_id) VALUES ('b7', 'p1', 'github', 1, 2, "
    "'o/r', '.github/workflows/r.yml', 9)",
    # require_protected_environment: GitLab only, and only with an environment.
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
    "repository_id, repository, workflow_path, environment, require_protected_environment) VALUES "
    "('b8', 'p1', 'github', 1, 2, 'o/r', '.github/workflows/r.yml', 'release', true)",
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id, require_protected_environment) VALUES ('b9', 'p1', 'gitlab', 'x', "
    "'https://gitlab.com', 5, true)",
)
_GITLAB_PROTECTED_ROW = (
    "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository, issuer, "
    "gitlab_project_id, environment, require_protected_environment) VALUES ('stp_gl2', 'p1', "
    "'gitlab', 'acme/skills', 'https://gitlab.com', 4243, 'production', true)"
)


def _sqlite_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def _sqlite_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({_TABLE})")}


def test_sqlite_round_trip(pre_082_db: Path) -> None:
    connection = sqlite3.connect(str(pre_082_db))
    connection.execute(
        "INSERT INTO skill_publishers (id, owner_type, namespace, namespace_normalized) "
        "VALUES ('p1', 'system', 'acme', 'acme')"
    )
    connection.execute(_GITHUB_ROW)
    connection.execute(_TOKEN, ("tok_gh", "a" * 64, "stp_gh", "jti-gh"))
    with pytest.raises(sqlite3.IntegrityError):  # the 071 CHECK is github-only
        connection.execute(
            "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
            "repository_id, repository, workflow_path) VALUES ('x', 'p1', 'gitlab', 1, 2, 'o/r', 'w')"
        )
    connection.commit()
    connection.close()

    config = _cfg(f"sqlite+aiosqlite:///{pre_082_db}")
    command.upgrade(config, "082")
    connection = sqlite3.connect(str(pre_082_db))
    assert {"issuer", "gitlab_project_id", "gitlab_namespace_id", "require_protected_environment"} <= _sqlite_columns(connection)
    assert {
        "idx_skill_trusted_publishers_gitlab",
        "idx_skill_trusted_publishers_repo",
        "idx_skill_trusted_publishers_publisher",
    } <= _sqlite_names(connection, "index")
    # The existing GitHub trust is intact (and still valid under the shape CHECK).
    assert connection.execute(
        "SELECT provider, repository_id, repository_owner_id, workflow_path, issuer, gitlab_project_id "
        "FROM skill_trusted_publishers WHERE id = 'stp_gh'"
    ).fetchone() == ("github", 2, 1, ".github/workflows/r.yml", None, None)
    connection.execute(_GITLAB_ROW)
    connection.execute(_GITLAB_PROTECTED_ROW)
    connection.execute(_TOKEN, ("tok_gl", "b" * 64, "stp_gl", "jti-gl"))
    for bad in _BAD_ROWS:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(bad)
    connection.commit()
    connection.close()

    command.downgrade(config, _prior_revision())
    connection = sqlite3.connect(str(pre_082_db))
    assert not ({"issuer", "gitlab_project_id", "gitlab_namespace_id", "require_protected_environment"} & _sqlite_columns(connection))
    assert "idx_skill_trusted_publishers_gitlab" not in _sqlite_names(connection, "index")
    assert "idx_skill_trusted_publishers_repo" in _sqlite_names(connection, "index")
    assert [row[0] for row in connection.execute("SELECT id FROM skill_trusted_publishers")] == ["stp_gh"]
    assert [row[0] for row in connection.execute("SELECT id FROM skill_publish_tokens")] == ["tok_gh"]
    with pytest.raises(sqlite3.IntegrityError):  # back to github-only
        connection.execute(
            "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
            "repository_id, repository, workflow_path) VALUES ('x', 'p1', 'gitlab', 1, 2, 'o/r', 'w')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # NOT NULL restored
        connection.execute(
            "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository) "
            "VALUES ('y', 'p1', 'github', 'o/r')"
        )
    connection.close()

    command.upgrade(config, "082")
    connection = sqlite3.connect(str(pre_082_db))
    assert connection.execute("SELECT count(*) FROM skill_trusted_publishers").fetchone() == (1,)
    connection.close()


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
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    prior = _prior_revision()
    command.upgrade(config, prior)

    def seed(conn) -> None:
        # pub_sessionfs is the platform publisher 071 inserts.
        conn.execute(sa.text(
            "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
            "repository_id, repository, workflow_path) VALUES ('stp_gh', 'pub_sessionfs', 'github', 1, 2, "
            "'o/r', '.github/workflows/r.yml')"
        ))
        conn.execute(sa.text(
            "INSERT INTO skill_publish_tokens (id, token_hash, trusted_publisher_id, publisher_id, "
            "oidc_issuer, oidc_jti, expires_at) VALUES ('tok_gh', :h, 'stp_gh', 'pub_sessionfs', "
            "'iss', 'jti-gh', '2030-01-01')"
        ), {"h": "a" * 64})

    _pg(url, seed)
    command.upgrade(config, "082")

    def check(conn) -> None:
        names = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'skill_trusted_publishers'::regclass"
            ))
        }
        assert {"ck_skill_trusted_publisher_provider", "ck_skill_trusted_publisher_shape"} <= names
        row = conn.execute(sa.text(
            "SELECT provider, repository_id, workflow_path, issuer FROM skill_trusted_publishers "
            "WHERE id = 'stp_gh'"
        )).one()
        assert tuple(row) == ("github", 2, ".github/workflows/r.yml", None)
        conn.execute(sa.text(_GITLAB_ROW.replace("'p1'", "'pub_sessionfs'")))
        conn.execute(sa.text(_GITLAB_PROTECTED_ROW.replace("'p1'", "'pub_sessionfs'")))
        conn.execute(sa.text(
            "INSERT INTO skill_publish_tokens (id, token_hash, trusted_publisher_id, publisher_id, "
            "oidc_issuer, oidc_jti, expires_at) VALUES ('tok_gl', :h, 'stp_gl', 'pub_sessionfs', "
            "'https://gitlab.com', 'jti-gl', '2030-01-01')"
        ), {"h": "b" * 64})
        for bad in _BAD_ROWS:
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad.replace("'p1'", "'pub_sessionfs'")))
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, prior)

    def after_downgrade(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'skill_trusted_publishers'"
            ))
        }
        assert not ({"issuer", "gitlab_project_id", "gitlab_namespace_id", "require_protected_environment"} & cols)
        assert [r[0] for r in conn.execute(sa.text("SELECT id FROM skill_trusted_publishers"))] == ["stp_gh"]
        assert [r[0] for r in conn.execute(sa.text("SELECT id FROM skill_publish_tokens"))] == ["tok_gh"]
        nullable = dict(conn.execute(sa.text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'skill_trusted_publishers' AND column_name IN "
            "('repository_id', 'repository_owner_id', 'workflow_path')"
        )).all())
        assert nullable == {"repository_id": "NO", "repository_owner_id": "NO", "workflow_path": "NO"}
        nested = conn.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO skill_trusted_publishers (id, publisher_id, provider, repository_owner_id, "
                "repository_id, repository, workflow_path) VALUES ('x', 'pub_sessionfs', 'gitlab', 1, 2, "
                "'o/r', 'w')"
            ))
        nested.rollback()

    _pg(url, after_downgrade)
    command.upgrade(config, "082")

    def after_reupgrade(conn) -> None:
        assert [r[0] for r in conn.execute(sa.text("SELECT id FROM skill_trusted_publishers"))] == ["stp_gh"]

    _pg(url, after_reupgrade)
