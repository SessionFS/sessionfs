"""Migration 110 (tickets.loop_config) round-trip.

A single additive column: `tickets.loop_config` (nullable JSON-as-Text) that
carries a review loop's runtime config on the parent issue ticket only. The
prior schema is the REAL SQLAlchemy metadata minus what 110 adds, rewound by
dropping the column and stamping at 109. The PostgreSQL leg runs when
SFS_TEST_PG_URL is set (and in the `migrations-postgres` CI gate).
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


def _build_pre_110_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    # 110 adds tickets.loop_config AND the work_queues actor-provenance
    # columns. Rewind ALL of them so the pre-110 schema really is 109, not
    # "HEAD metadata minus loop_config" (which would make the 110 upgrade try
    # to re-add actor_type and fail with a duplicate column).
    connection.execute("ALTER TABLE tickets DROP COLUMN loop_config")
    connection.execute("ALTER TABLE work_queues DROP COLUMN actor_type")
    connection.execute("ALTER TABLE work_queues DROP COLUMN service_key_id")
    connection.execute("ALTER TABLE work_queues DROP COLUMN service_key_name")
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_110_db(tmp_path: Path) -> Path:
    path = tmp_path / "m110.db"
    _build_pre_110_db(path)
    command.stamp(_cfg(path), "109")
    return path


def _columns(connection: sqlite3.Connection, table: str = "tickets") -> set[str]:
    return {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
    }


def test_upgrade_adds_loop_config(pre_110_db: Path) -> None:
    command.upgrade(_cfg(pre_110_db), "110")
    connection = sqlite3.connect(str(pre_110_db))
    assert "loop_config" in _columns(connection)
    connection.execute(
        "INSERT INTO tickets (id, project_id, title, status, created_by_user_id) "
        "VALUES ('tk_x', 'proj_x', 'loop', 'open', 'u1')"
    )
    # NULL by default (only loop parent issues populate it).
    assert connection.execute(
        "SELECT loop_config FROM tickets WHERE id = 'tk_x'"
    ).fetchone() == (None,)
    connection.execute(
        "UPDATE tickets SET loop_config = '{\"queue_id\": \"wq_x\"}' WHERE id = 'tk_x'"
    )
    assert connection.execute(
        "SELECT loop_config FROM tickets WHERE id = 'tk_x'"
    ).fetchone() == ('{"queue_id": "wq_x"}',)
    connection.commit()
    connection.close()


def test_downgrade_restores_109_shape(pre_110_db: Path) -> None:
    config = _cfg(pre_110_db)
    command.upgrade(config, "110")
    connection = sqlite3.connect(str(pre_110_db))
    connection.execute(
        "UPDATE tickets SET loop_config = '{\"queue_id\": \"wq_x\"}'"
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "109")
    connection = sqlite3.connect(str(pre_110_db))
    assert "loop_config" not in _columns(connection)
    assert "actor_type" not in _columns(connection, "work_queues")
    assert "service_key_id" not in _columns(connection, "work_queues")
    assert "service_key_name" not in _columns(connection, "work_queues")
    connection.close()
    command.upgrade(config, "110")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 109 and back, on REAL PostgreSQL."""
    reset_pg_schema()
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "109")
    command.upgrade(config, "head")
