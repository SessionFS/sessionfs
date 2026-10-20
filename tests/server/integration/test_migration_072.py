"""Migration 072 — org-level personas (GitHub #70).

The schema is seeded from the REAL SQLAlchemy metadata (post-072 shape),
stamped 072 and downgraded to 069 so the downgrade runs against the
metadata-shaped table, then upgraded again. PostgreSQL is covered by
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

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
assert Path(_MIGRATIONS).is_dir(), _MIGRATIONS


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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


@pytest.fixture()
def pre_072_db(tmp_path: Path) -> Path:
    """A database at revision 069 with the pre-072 agent_personas shape."""
    path = tmp_path / "m072.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    command.stamp(_cfg(path), "072")
    command.downgrade(_cfg(path), "071")
    connection = _connect(path)
    _insert(connection, "users", id="u1", email="u1@example.com")
    _insert(connection, "organizations", id="org_1", name="Acme", slug="acme")
    _insert(
        connection, "projects", id="p1", name="p1",
        git_remote_normalized="github.com/acme/p1", owner_id="u1", org_id="org_1",
    )
    _insert(
        connection, "agent_personas", id="per_legacy", project_id="p1",
        name="atlas", role="Backend", created_by="u1",
    )
    connection.commit()
    connection.close()
    return path


def _columns(path: Path) -> dict[str, int]:
    connection = _connect(path)
    rows = connection.execute("PRAGMA table_info(agent_personas)").fetchall()
    connection.close()
    # name -> notnull flag
    return {row[1]: row[3] for row in rows}


def test_downgraded_shape_has_no_org_id(pre_072_db: Path) -> None:
    columns = _columns(pre_072_db)
    assert "org_id" not in columns
    assert columns["project_id"] == 1


def test_upgrade_reports_legacy_case_collisions_without_failing(
    pre_072_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _connect(pre_072_db)
    _insert(
        connection, "agent_personas", id="per_legacy_upper", project_id="p1",
        name="Atlas", role="Backend", created_by="u1",
    )
    connection.commit()
    connection.close()
    command.upgrade(_cfg(pre_072_db), "072")
    out = capsys.readouterr().out
    assert "collide case-insensitively" in out
    assert "project=p1 name='atlas' rows=2" in out
    assert _columns(pre_072_db)["org_id"] == 0  # migration still applied


def test_page_slug_uniqueness_matches_migration_019() -> None:
    """GH #70 — uq_kp_project_slug exists in the DB since migration 019; the
    model now declares it too, so create_all schemas enforce it."""
    table = Base.metadata.tables["knowledge_pages"]
    names = {c.name for c in table.constraints}
    assert "uq_kp_project_slug" in names


def test_upgrade_keeps_existing_personas_and_adds_org_scope(pre_072_db: Path) -> None:
    command.upgrade(_cfg(pre_072_db), "072")
    columns = _columns(pre_072_db)
    assert columns["org_id"] == 0
    assert columns["project_id"] == 0

    connection = _connect(pre_072_db)
    row = connection.execute(
        "SELECT project_id, org_id, name FROM agent_personas WHERE id = 'per_legacy'"
    ).fetchone()
    assert row == ("p1", None, "atlas")

    # Org persona with the same name as a project persona is allowed.
    connection.execute(
        "INSERT INTO agent_personas (id, org_id, name, role, created_by) "
        "VALUES ('per_org', 'org_1', 'atlas', 'Backend', 'u1')"
    )
    connection.commit()

    # Case-insensitive uniqueness per org.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_personas (id, org_id, name, role, created_by) "
            "VALUES ('per_org2', 'org_1', 'ATLAS', 'Backend', 'u1')"
        )
    # XOR: both set.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_personas (id, project_id, org_id, name, role, created_by) "
            "VALUES ('per_both', 'p1', 'org_1', 'prism', 'Frontend', 'u1')"
        )
    # XOR: neither set.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_personas (id, name, role, created_by) "
            "VALUES ('per_none', 'prism', 'Frontend', 'u1')"
        )
    # Existing project uniqueness is unchanged.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_personas (id, project_id, name, role, created_by) "
            "VALUES ('per_dup', 'p1', 'atlas', 'Backend', 'u1')"
        )
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_personas'"
        )
    }
    assert {"uq_persona_org_lower_name", "idx_persona_org_active"} <= indexes
    connection.close()


def test_downgrade_drops_org_personas_and_restores_shape(pre_072_db: Path) -> None:
    config = _cfg(pre_072_db)
    command.upgrade(config, "072")
    connection = _connect(pre_072_db)
    connection.execute(
        "INSERT INTO agent_personas (id, org_id, name, role, created_by) "
        "VALUES ('per_org', 'org_1', 'release', 'Gov', 'u1')"
    )
    connection.commit()
    connection.close()

    command.downgrade(config, "071")
    columns = _columns(pre_072_db)
    assert "org_id" not in columns
    assert columns["project_id"] == 1
    connection = _connect(pre_072_db)
    ids = {row[0] for row in connection.execute("SELECT id FROM agent_personas")}
    connection.close()
    assert ids == {"per_legacy"}

    command.upgrade(config, "072")
    assert _columns(pre_072_db)["org_id"] == 0


def test_upgrade_backfills_handoff_source_project_id(pre_072_db: Path) -> None:
    """GH #70 — the durable handoff anchor backfill: session.project_id
    first, then the legacy remote resolution through project_repos, and
    NULL when nothing resolves."""
    connection = _connect(pre_072_db)
    _insert(
        connection, "projects", id="p2", name="p2",
        git_remote_normalized="github.com/acme/p2", owner_id="u1", org_id="org_1",
    )
    _insert(
        connection, "project_repos", id="pr2", project_id="p2",
        git_remote_normalized="github.com/acme/p2", is_primary=1,
    )
    # s1: direct link. s2: legacy (NULL project_id) but its remote is a
    # repo of p2. s3: unresolvable.
    _insert(
        connection, "sessions", id="s1", user_id="u1", project_id="p1",
        git_remote_normalized="github.com/acme/p1", source_tool="claude-code",
        blob_key="b/s1", etag="e1",
    )
    _insert(
        connection, "sessions", id="s2", user_id="u1", project_id=None,
        git_remote_normalized="github.com/acme/p2", source_tool="claude-code",
        blob_key="b/s2", etag="e2",
    )
    _insert(
        connection, "sessions", id="s3", user_id="u1", project_id=None,
        git_remote_normalized="github.com/acme/nowhere", source_tool="claude-code",
        blob_key="b/s3", etag="e3",
    )
    for hid, sid in (("h1", "s1"), ("h2", "s2"), ("h3", "s3")):
        _insert(
            connection, "handoffs", id=hid, session_id=sid, sender_id="u1",
            recipient_email="r@example.com", status="pending",
            expires_at="2027-01-01 00:00:00",
        )
    connection.commit()
    connection.close()

    command.upgrade(_cfg(pre_072_db), "072")

    connection = _connect(pre_072_db)
    rows = dict(
        connection.execute(
            "SELECT id, source_project_id FROM handoffs ORDER BY id"
        ).fetchall()
    )
    assert rows == {"h1": "p1", "h2": "p2", "h3": None}
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='handoffs'"
        )
    }
    assert "idx_handoffs_source_project_id" in indexes
    connection.close()

    # Downgrade removes the anchor cleanly.
    command.downgrade(_cfg(pre_072_db), "071")
    connection = _connect(pre_072_db)
    handoff_cols = {
        row[1]
        for row in connection.execute("PRAGMA table_info(handoffs)").fetchall()
    }
    assert "source_project_id" not in handoff_cols
    connection.close()


def test_upgrade_backfills_legacy_remote_and_follows_tombstones(
    pre_072_db: Path,
) -> None:
    """GH #70 — the legacy remote backfill must mirror
    `resolve_project_by_remote`: the `projects.git_remote_normalized`
    column (not just `project_repos`) for a NULL-project_id session, then
    a bounded follow of merge tombstones so the durable anchor names the
    LIVE project rather than the tombstone."""
    connection = _connect(pre_072_db)
    # Live merge target first (FK target for the tombstone below).
    _insert(
        connection, "projects", id="p_live", name="live",
        git_remote_normalized="github.com/acme/live", owner_id="u1", org_id="org_1",
    )
    # A project merged into p_live. Its git_remote_normalized is NOT
    # cleared by merge (legacy), so a session on its remote resolves to
    # the tombstone first and must be followed to p_live.
    _insert(
        connection, "projects", id="p_tomb", name="tomb",
        git_remote_normalized="github.com/acme/tomb", owner_id="u1", org_id="org_1",
        merged_into_project_id="p_live",
    )
    # Legacy-remote project reachable ONLY via projects.git_remote_normalized
    # (no project_repos row) — the second read of the dual-read resolution.
    _insert(
        connection, "projects", id="p_col", name="col",
        git_remote_normalized="github.com/acme/col", owner_id="u1", org_id="org_1",
    )
    _insert(
        connection, "sessions", id="s_col", user_id="u1", project_id=None,
        git_remote_normalized="github.com/acme/col", source_tool="claude-code",
        blob_key="b/s_col", etag="e_col",
    )
    _insert(
        connection, "sessions", id="s_tomb", user_id="u1", project_id=None,
        git_remote_normalized="github.com/acme/tomb", source_tool="claude-code",
        blob_key="b/s_tomb", etag="e_tomb",
    )
    for hid, sid in (("h_col", "s_col"), ("h_tomb", "s_tomb")):
        _insert(
            connection, "handoffs", id=hid, session_id=sid, sender_id="u1",
            recipient_email="r@example.com", status="pending",
            expires_at="2027-01-01 00:00:00",
        )
    connection.commit()
    connection.close()

    command.upgrade(_cfg(pre_072_db), "072")

    connection = _connect(pre_072_db)
    rows = dict(
        connection.execute(
            "SELECT id, source_project_id FROM handoffs ORDER BY id"
        ).fetchall()
    )
    assert rows == {"h_col": "p_col", "h_tomb": "p_live"}
    connection.close()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head then downgrade to 069 and back, on REAL PostgreSQL."""
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "071")
    command.upgrade(config, "head")
