"""Migration 107 (wiki revision service-key audit columns) round-trip.

Three additive nullable columns on `wiki_page_revisions`: `actor_type`,
`service_key_id`, `service_key_name`. The prior schema is the REAL
SQLAlchemy metadata minus what 107 adds, rewound by dropping the columns
and stamping at 106. The PostgreSQL leg runs when SFS_TEST_PG_URL is set
(and in the `migrations-postgres` CI gate).
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


def _build_pre_107_db(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    connection.execute("ALTER TABLE wiki_page_revisions DROP COLUMN actor_type")
    connection.execute("ALTER TABLE wiki_page_revisions DROP COLUMN service_key_id")
    connection.execute("ALTER TABLE wiki_page_revisions DROP COLUMN service_key_name")
    connection.commit()
    connection.close()


@pytest.fixture()
def pre_107_db(tmp_path: Path) -> Path:
    path = tmp_path / "m107.db"
    _build_pre_107_db(path)
    command.stamp(_cfg(path), "106")
    return path


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1] for row in connection.execute("PRAGMA table_info(wiki_page_revisions)")
    }


def test_upgrade_adds_service_key_audit_columns(pre_107_db: Path) -> None:
    command.upgrade(_cfg(pre_107_db), "107")
    connection = sqlite3.connect(str(pre_107_db))
    for column in ("actor_type", "service_key_id", "service_key_name"):
        assert column in _columns(connection)
    connection.execute(
        "INSERT INTO wiki_page_revisions "
        "(project_id, page_slug, revision_number, title) "
        "VALUES ('proj_x', 'architecture', 1, 'Architecture')"
    )
    # NULL by default (human writes leave the service-key provenance empty).
    assert connection.execute(
        "SELECT actor_type, service_key_id, service_key_name "
        "FROM wiki_page_revisions WHERE revision_number = 1"
    ).fetchone() == (None, None, None)
    connection.execute(
        "UPDATE wiki_page_revisions SET actor_type = 'service_key', "
        "service_key_id = 'sk_1', service_key_name = 'svc' "
        "WHERE revision_number = 1"
    )
    assert connection.execute(
        "SELECT actor_type, service_key_id, service_key_name "
        "FROM wiki_page_revisions WHERE revision_number = 1"
    ).fetchone() == ("service_key", "sk_1", "svc")
    connection.commit()
    connection.close()


def test_downgrade_restores_106_shape(pre_107_db: Path) -> None:
    config = _cfg(pre_107_db)
    command.upgrade(config, "107")
    connection = sqlite3.connect(str(pre_107_db))
    connection.execute(
        "UPDATE wiki_page_revisions SET actor_type = 'service_key', "
        "service_key_id = 'sk_1', service_key_name = 'svc'"
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "106")
    connection = sqlite3.connect(str(pre_107_db))
    for column in ("actor_type", "service_key_id", "service_key_name"):
        assert column not in _columns(connection)
    connection.close()
    command.upgrade(config, "107")


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 106 and back, on REAL PostgreSQL."""
    reset_pg_schema()
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "106")
    command.upgrade(config, "head")
