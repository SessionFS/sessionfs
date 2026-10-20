"""Migration 065 — grandfather existing usage of project-scoped features.

The prior schema is seeded from the REAL SQLAlchemy metadata (minus the
table 065 creates), like test_migration_064.py, so drift from an earlier
migration fails locally. PostgreSQL is covered by
`test_full_chain_on_postgres` when SFS_TEST_PG_URL is set, and by the
`migrations-postgres` CI gate.
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

MIGRATION_065_TABLES = {"project_feature_grandfathers"}

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
assert Path(_MIGRATIONS).is_dir(), _MIGRATIONS


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


@pytest.fixture()
def pre_065_db(tmp_path: Path) -> Path:
    path = tmp_path / "m065.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for n, t in Base.metadata.tables.items() if n not in MIGRATION_065_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_065_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()
    command.stamp(_cfg(path), "064")
    return path


def _insert(connection: sqlite3.Connection, table_name: str, **values: object) -> None:
    """INSERT one row, filling every NOT NULL column the real schema declares."""
    table = Base.metadata.tables[table_name]
    payload = dict(values)
    for column in table.columns:
        if column.name in payload or column.nullable or column.server_default is not None:
            continue
        default = column.default
        if default is None:
            continue
        if getattr(default, "is_scalar", False):
            payload[column.name] = default.arg
        elif getattr(default, "is_callable", False):
            value = default.arg(None)
            payload[column.name] = value.isoformat(sep=" ") if hasattr(value, "isoformat") else value
    columns = ", ".join(payload)
    placeholders = ", ".join("?" for _ in payload)
    connection.execute(
        f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})",  # noqa: S608 - test-local
        tuple(payload.values()),
    )


def _seed(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    _insert(connection, "users", id="user_1", email="user_1@example.com")
    for project_id in ("p_empty", "p_persona", "p_ticket", "p_run", "p_feedback"):
        _insert(
            connection,
            "projects",
            id=project_id,
            name=project_id,
            git_remote_normalized=f"github.com/example/{project_id}",
            owner_id="user_1",
        )
    _insert(
        connection, "agent_personas", id="per_1", project_id="p_persona",
        name="atlas", role="Backend", created_by="user_1",
    )
    _insert(
        connection, "tickets", id="tk_1", project_id="p_ticket", title="t",
        created_by_user_id="user_1",
    )
    _insert(connection, "agent_runs", id="run_1", project_id="p_run", persona_name="atlas")
    _insert(
        connection, "agent_feedback", id="afb_1", project_id="p_feedback",
        feedback_type="route", verdict="positive",
    )
    connection.commit()
    connection.close()


def _grandfathered(path: Path) -> set[tuple[str, str]]:
    connection = sqlite3.connect(str(path))
    rows = set(connection.execute(
        "SELECT project_id, feature FROM project_feature_grandfathers"
    ))
    connection.close()
    return rows


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head then downgrade to 064 and back, on REAL PostgreSQL."""
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "064")
    command.upgrade(config, "head")


def test_upgrade_records_existing_usage_per_feature(pre_065_db: Path) -> None:
    _seed(pre_065_db)
    command.upgrade(_cfg(pre_065_db), "065")
    projects = ("p_empty", "p_persona", "p_ticket", "p_run", "p_feedback")
    expected = {(p, "project_context") for p in projects} | {
        ("p_persona", "agent_personas"),
        ("p_ticket", "agent_tickets"),
        ("p_run", "agent_runs"),
        ("p_feedback", "agent_runs"),
    }
    assert _grandfathered(pre_065_db) == expected


def test_upgrade_on_empty_database(pre_065_db: Path) -> None:
    command.upgrade(_cfg(pre_065_db), "065")
    assert _grandfathered(pre_065_db) == set()


def test_downgrade_removes_table(pre_065_db: Path) -> None:
    config = _cfg(pre_065_db)
    command.upgrade(config, "065")
    command.downgrade(config, "064")
    connection = sqlite3.connect(str(pre_065_db))
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    assert "project_feature_grandfathers" not in tables
