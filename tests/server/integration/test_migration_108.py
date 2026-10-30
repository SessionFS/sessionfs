"""Migration 108 (verdict → reviewed_commit binding) round-trip.

A single additive column: `ticket_comments.reviewed_commit` (nullable hex
SHA). The prior schema is the REAL SQLAlchemy metadata minus what 108 adds,
rewound by dropping the column and stamping at 107. The PostgreSQL leg runs
when SFS_TEST_PG_URL is set (and in the `migrations-postgres` CI gate).
"""

from __future__ import annotations

import os
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


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _build_pre_108_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    connection.execute("ALTER TABLE ticket_comments DROP COLUMN reviewed_commit")
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_108_db(tmp_path: Path) -> Path:
    path = tmp_path / "m108.db"
    _build_pre_108_db(path)
    command.stamp(_cfg(path), "107")
    return path


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1] for row in connection.execute("PRAGMA table_info(ticket_comments)")
    }


def test_upgrade_adds_reviewed_commit(pre_108_db: Path) -> None:
    command.upgrade(_cfg(pre_108_db), "108")
    connection = sqlite3.connect(str(pre_108_db))
    assert "reviewed_commit" in _columns(connection)
    connection.execute(
        "INSERT INTO ticket_comments (id, ticket_id, author_user_id, content) "
        "VALUES ('c1', 'tk_x', 'u1', 'hi')"
    )
    # NULL by default (only review verdict rows populate it).
    assert connection.execute(
        "SELECT reviewed_commit FROM ticket_comments WHERE id = 'c1'"
    ).fetchone() == (None,)
    connection.execute(
        "UPDATE ticket_comments SET reviewed_commit = 'a1b2c3d4e5f6a7b8' WHERE id = 'c1'"
    )
    assert connection.execute(
        "SELECT reviewed_commit FROM ticket_comments WHERE id = 'c1'"
    ).fetchone() == ("a1b2c3d4e5f6a7b8",)
    connection.commit()
    connection.close()


def test_downgrade_restores_107_shape(pre_108_db: Path) -> None:
    config = _cfg(pre_108_db)
    command.upgrade(config, "108")
    connection = sqlite3.connect(str(pre_108_db))
    connection.execute(
        "UPDATE ticket_comments SET reviewed_commit = 'a1b2c3d4e5f6a7b8'"
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "107")
    connection = sqlite3.connect(str(pre_108_db))
    assert "reviewed_commit" not in _columns(connection)
    connection.close()
    command.upgrade(config, "108")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 107 and back, on REAL PostgreSQL."""
    reset_pg_schema()
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "107")
    command.upgrade(config, "head")
