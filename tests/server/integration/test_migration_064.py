"""Migration 064 agent-feedback round-trip.

Mirrors test_migration_063.py: the prior schema is seeded from the REAL
SQLAlchemy metadata (minus the table 064 itself creates) so an
`object already exists` drift from an earlier migration fails locally
instead of in production. Dialect divergence (CHECK constraints render
differently on PG vs SQLite) is covered by
`test_full_chain_on_postgres` when SFS_TEST_PG_URL is set, and by the
`migrations-postgres` CI gate on every push.

064 creates exactly one table, `agent_feedback`, with three inline CHECK
constraints (feedback_type / verdict / source enums)
and four indexes.
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

# Tables migration 064 creates; everything else is pre-064 state.
MIGRATION_064_TABLES = {"agent_feedback"}


# Resolve off __file__, not the process CWD, so the suite runs from anywhere.
_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
assert Path(_MIGRATIONS).is_dir(), _MIGRATIONS


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _build_pre_064_db(path: Path) -> None:
    """Create the FULL application schema minus the agent_feedback table."""
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [
        table
        for name, table in Base.metadata.tables.items()
        if name not in MIGRATION_064_TABLES
    ]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_064_TABLES), (
        "MIGRATION_064_TABLES drifted from the models: "
        f"{sorted(MIGRATION_064_TABLES - set(Base.metadata.tables))}"
    )
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()


@pytest.fixture()
def pre_feedback_db(tmp_path: Path) -> Path:
    path = tmp_path / "m064.db"
    _build_pre_064_db(path)
    command.stamp(_cfg(path), "063")
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


def _users(connection: sqlite3.Connection, *ids: str) -> None:
    for user_id in ids:
        _insert(connection, "users", id=user_id, email=f"{user_id}@example.com")


def _projects(connection: sqlite3.Connection, *ids: str, owner: str = "user_1") -> None:
    for project_id in ids:
        _insert(
            connection,
            "projects",
            id=project_id,
            name=project_id,
            git_remote_normalized=f"github.com/example/{project_id}",
            owner_id=owner,
        )


def test_pre_064_schema_is_the_real_schema_not_a_stub(pre_feedback_db: Path) -> None:
    """Guard the guard: the fixture must seed the whole app schema."""
    connection = sqlite3.connect(str(pre_feedback_db))
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    expected = set(Base.metadata.tables) - MIGRATION_064_TABLES
    assert expected <= tables
    assert not (MIGRATION_064_TABLES & tables)
    assert len(expected) > 50, f"only {len(expected)} prior tables seeded"


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head then downgrade to 063 and back, on REAL PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "063")
    command.upgrade(config, "head")


def test_upgrade_creates_agent_feedback_table_and_indexes(pre_feedback_db: Path) -> None:
    command.upgrade(_cfg(pre_feedback_db), "064")
    connection = sqlite3.connect(str(pre_feedback_db))
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "agent_feedback" in tables
    indexes = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    for name in (
        "idx_agent_feedback_project_created",
        "idx_agent_feedback_run",
        "idx_agent_feedback_ticket",
        "idx_agent_feedback_comment",
    ):
        assert name in indexes, name
    columns = {
        row[1]: row for row in connection.execute("PRAGMA table_info(agent_feedback)")
    }
    # Audit triple + classification + three nullable targets present.
    assert {
        "id", "project_id", "agent_run_id", "ticket_id", "comment_id",
        "feedback_type", "verdict", "label", "final_owner", "notes",
        "source", "created_by_user_id", "actor_type", "service_key_id",
        "service_key_name", "created_at",
    } <= set(columns)
    # source defaults to 'human'; actor_type defaults to 'user'.
    assert columns["source"][4] == "'human'"
    assert columns["actor_type"][4] == "'user'"
    connection.close()


def test_upgrade_enforces_enum_checks(pre_feedback_db: Path) -> None:
    command.upgrade(_cfg(pre_feedback_db), "064")
    connection = sqlite3.connect(str(pre_feedback_db))
    _users(connection, "user_1")
    _projects(connection, "prj_1")

    def insert(**values: object) -> None:
        base = {
            "id": "afb_" + "0" * 24,
            "project_id": "prj_1",
            "feedback_type": "route",
            "verdict": "negative",
            "ticket_id": "tk_1",
        }
        base.update(values)
        connection.execute(
            "INSERT INTO agent_feedback "
            "(id, project_id, agent_run_id, ticket_id, comment_id, feedback_type, "
            "verdict, label, final_owner, notes, source, created_by_user_id, "
            "actor_type, service_key_id, service_key_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                base["id"], base["project_id"], base.get("agent_run_id"),
                base.get("ticket_id"), base.get("comment_id"),
                base["feedback_type"], base["verdict"], base.get("label"),
                base.get("final_owner"), base.get("notes"), base.get("source", "human"),
                base.get("created_by_user_id"), base.get("actor_type", "user"),
                base.get("service_key_id"), base.get("service_key_name"),
            ),
        )

    # Valid row succeeds (route / negative / wrong_route with a ticket target).
    insert()
    # Bad feedback_type.
    with pytest.raises(sqlite3.IntegrityError):
        insert(id="afb_" + "1" * 24, feedback_type="bogus")
    # Bad verdict.
    with pytest.raises(sqlite3.IntegrityError):
        insert(id="afb_" + "2" * 24, verdict="meh")
    # Bad source.
    with pytest.raises(sqlite3.IntegrityError):
        insert(id="afb_" + "3" * 24, source="machine")
    # A row whose targets were all deleted (SET NULL) is kept, not refused:
    # the API requires a target on create, the database must not block the
    # later delete of the run/ticket/comment it judged.
    insert(id="afb_" + "4" * 24, ticket_id=None)
    connection.close()


def test_downgrade_removes_agent_feedback_table(pre_feedback_db: Path) -> None:
    config = _cfg(pre_feedback_db)
    command.upgrade(config, "064")
    command.downgrade(config, "063")
    connection = sqlite3.connect(str(pre_feedback_db))
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "agent_feedback" not in tables
    connection.close()
