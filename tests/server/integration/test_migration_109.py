"""Migration 109 (retrieval context service-key provenance) round-trip.

Two additive columns on `retrieval_audit_contexts`: `actor_type` (NOT NULL,
server default 'user') and `service_key_id` (nullable). The prior schema is
the REAL SQLAlchemy metadata minus what 109 adds, rewound by dropping both
columns and stamping at 108. The PostgreSQL leg runs when SFS_TEST_PG_URL is
set (and in the `migrations-postgres` CI gate).
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


def _build_pre_109_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    connection.execute("ALTER TABLE retrieval_audit_contexts DROP COLUMN service_key_id")
    connection.execute("ALTER TABLE retrieval_audit_contexts DROP COLUMN actor_type")
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_109_db(tmp_path: Path) -> Path:
    path = tmp_path / "m109.db"
    _build_pre_109_db(path)
    command.stamp(_cfg(path), "108")
    return path


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1] for row in connection.execute("PRAGMA table_info(retrieval_audit_contexts)")
    }


def test_upgrade_adds_service_key_provenance(pre_109_db: Path) -> None:
    command.upgrade(_cfg(pre_109_db), "109")
    connection = sqlite3.connect(str(pre_109_db))
    cols = _columns(connection)
    assert "actor_type" in cols
    assert "service_key_id" in cols
    connection.execute(
        "INSERT INTO retrieval_audit_contexts (id, project_id, ticket_id) "
        "VALUES ('c1', 'p1', 'tk_x')"
    )
    # actor_type defaults to 'user', service_key_id to NULL.
    assert connection.execute(
        "SELECT actor_type, service_key_id FROM retrieval_audit_contexts WHERE id = 'c1'"
    ).fetchone() == ("user", None)
    connection.execute(
        "UPDATE retrieval_audit_contexts SET actor_type = 'service_key', "
        "service_key_id = 'sk_abc123' WHERE id = 'c1'"
    )
    assert connection.execute(
        "SELECT actor_type, service_key_id FROM retrieval_audit_contexts WHERE id = 'c1'"
    ).fetchone() == ("service_key", "sk_abc123")
    connection.commit()
    connection.close()


def test_downgrade_restores_108_shape(pre_109_db: Path) -> None:
    config = _cfg(pre_109_db)
    command.upgrade(config, "109")
    connection = sqlite3.connect(str(pre_109_db))
    connection.execute(
        "UPDATE retrieval_audit_contexts SET actor_type = 'service_key', "
        "service_key_id = 'sk_abc123'"
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "108")
    connection = sqlite3.connect(str(pre_109_db))
    cols = _columns(connection)
    assert "actor_type" not in cols
    assert "service_key_id" not in cols
    connection.close()
    command.upgrade(config, "109")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 108 and back, on REAL PostgreSQL."""
    reset_pg_schema()
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "108")
    command.upgrade(config, "head")
