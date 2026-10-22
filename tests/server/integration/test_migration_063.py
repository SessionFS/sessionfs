"""Migration 063 hosted marketplace round-trip.

The prior schema is seeded from the REAL SQLAlchemy metadata (every table the
application declares, minus the ones 063 itself creates) rather than a
three-table stub. A minimal stub is what let the v0.13.x SSO migrations ship
two prod-only failures: `object already exists` (063 creating a table/index an
EARLIER migration already owns is invisible when the earlier objects were never
created) and dialect divergence. Seeding from metadata closes the first class
locally; the second is closed by running 001..063 on real PostgreSQL, which
`tests/server/integration/test_migration_063.py::test_full_chain_on_postgres`
does when SFS_TEST_PG_URL is set and the `migrations-postgres` CI gate does on
every push.
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

# Tables migration 063 creates; everything else is pre-063 state.
MIGRATION_063_TABLES = {
    "skill_packages",
    "skill_package_versions",
    "skill_marketplace_listings",
    "skill_version_tools",
    "skill_consent_receipts",
    "skill_marketplace_registries",
    "skill_install_requests",
    "org_skill_assignments",
}


# Resolve off __file__, not the process CWD, so the suite runs from anywhere.
# (src/ ships publicly, so unlike docs/design/ this needs no skip guard.)
# tests/server/integration/<file> -> parents[3] is the repository root.
_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
assert Path(_MIGRATIONS).is_dir(), _MIGRATIONS


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _build_pre_063_db(path: Path) -> None:
    """Create the FULL application schema minus the 063 tables.

    If 063 tried to create any table or index that already exists at 062, the
    upgrade below fails here instead of in production.
    """
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [
        table
        for name, table in Base.metadata.tables.items()
        if name not in MIGRATION_063_TABLES
    ]
    assert len(prior) == len(Base.metadata.tables) - len(MIGRATION_063_TABLES), (
        "MIGRATION_063_TABLES drifted from the models: "
        f"{sorted(MIGRATION_063_TABLES - set(Base.metadata.tables))}"
    )
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()


@pytest.fixture()
def pre_marketplace_db(tmp_path: Path) -> Path:
    path = tmp_path / "m063.db"
    _build_pre_063_db(path)
    command.stamp(_cfg(path), "062")
    return path


# The prior schema is the REAL one, so base rows must satisfy the real NOT NULL
# columns; these helpers keep the marketplace assertions readable.
def _insert(connection: sqlite3.Connection, table_name: str, **values: object) -> None:
    """INSERT one row, filling every NOT NULL column the real schema declares.

    `create_all` emits only SERVER defaults, so Python-side column defaults
    (`default=False`, `default="free"`, ...) must be supplied here.
    """
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


def _orgs(connection: sqlite3.Connection, *ids: str) -> None:
    for org_id in ids:
        _insert(connection, "organizations", id=org_id, name=org_id, slug=org_id)



def test_pre_063_schema_is_the_real_schema_not_a_stub(pre_marketplace_db: Path) -> None:
    """Guard the guard: the fixture must seed the whole app schema.

    A regression to a hand-written 3-table stub would silently restore the
    blind spot this test exists to remove.
    """
    connection = sqlite3.connect(str(pre_marketplace_db))
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    expected = set(Base.metadata.tables) - MIGRATION_063_TABLES
    assert expected <= tables
    assert not (MIGRATION_063_TABLES & tables)
    assert len(expected) > 50, f"only {len(expected)} prior tables seeded"


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head then downgrade to 062 and back, on REAL PostgreSQL.

    Catches the two prod-only classes SQLite cannot: dialect divergence
    (constraint-vs-index, partial indexes) and object-already-exists against
    the true prior schema. Point SFS_TEST_PG_URL at an EMPTY scratch database;
    the chain is run from base.
    """
    reset_pg_schema()  # the chain runs from an empty database
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    # env.py resolves the URL from SFS_TEST_PG_URL via SFS_DATABASE_URL and
    # normalizes it to asyncpg, exactly as the deploy migrate-job does.
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "062")
    command.upgrade(config, "head")


def test_upgrade_creates_marketplace_tables_and_indexes(pre_marketplace_db: Path) -> None:
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "skill_packages",
        "skill_package_versions",
        "skill_marketplace_listings",
        "skill_consent_receipts",
        "skill_marketplace_registries",
        "skill_install_requests",
        "org_skill_assignments",
    } <= tables
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_skill_listing_visibility_state" in indexes
    assert "idx_skill_request_project_status" in indexes
    assert "idx_org_skill_assignment_target" in indexes
    # B6: FK columns that every join/lookup filters on are indexed.
    assert "idx_skill_listing_package" in indexes
    assert "idx_skill_listing_owner_user" in indexes
    assert "idx_org_skill_assignment_version" in indexes
    # S LOW-1: every FK column that joins/lookups filter on has a LEADING index.
    for name in (
        "idx_skill_packages_created_by",
        "idx_skill_versions_created_by",
        "idx_skill_listing_version",
        "idx_skill_listing_org",
        "idx_skill_listing_published_by",
        "idx_skill_registry_created_by",
        "idx_skill_request_version",
        "idx_skill_request_target_user",
        "idx_skill_request_approved_by",
        "idx_org_skill_assignment_target_user",
        "idx_org_skill_assignment_created_by",
    ):
        assert name in indexes, name
    assignment_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(org_skill_assignments)")
    }
    assert "target_user_id" in assignment_columns
    request_columns = {
        row[1]: row for row in connection.execute("PRAGMA table_info(skill_install_requests)")
    }
    # A3: request origin column, NOT NULL, defaults to 'api'.
    assert request_columns["requested_via"][3] == 1
    assert request_columns["requested_via"][4] == "'api'"
    # P1-3: consent-challenge columns (hash at rest, expiry, attempt counter).
    assert "approval_challenge_hash" in request_columns
    # R16 P2-1: issuance rate-limit anchor.
    assert "approval_challenge_issued_at" in request_columns
    # P2-1: service-key provenance on install requests.
    assert request_columns["requested_actor_type"][4] == "'user'"
    assert "requested_by_service_key_id" in request_columns
    assert "requested_by_service_key_name" in request_columns
    receipt_columns = {row[1] for row in connection.execute("PRAGMA table_info(skill_consent_receipts)")}
    # P2-5: receipts are bound to the request + project that produced them.
    assert {"install_request_id", "project_id"} <= receipt_columns
    assert "idx_skill_consent_request" in indexes
    assert "approval_challenge_expires_at" in request_columns
    assert request_columns["approval_challenge_attempts"][3] == 1
    assert request_columns["approval_challenge_attempts"][4] in ("0", "'0'")
    connection.close()


def test_upgrade_enforces_requested_via_check(pre_marketplace_db: Path) -> None:
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    _users(connection, "user_1")
    _projects(connection, "prj_1")
    connection.execute(
        "INSERT INTO skill_packages "
        "(id, owner_type, owner_id, namespace, name, display_name, source_type, source_locator) "
        "VALUES ('spk_1', 'user', 'user_1', 'personal', 'demo', 'Demo', 'upload', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions "
        "(id, package_id, content_hash, agent_plugin_schema, manifest_json, dlp_status, "
        "blob_key, blob_size_bytes) VALUES "
        "('skv_1', 'spk_1', 'sha256:abc', 'schema', '{}', 'passed', 'blob', 1)"
    )
    connection.execute(
        "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, "
        "target_scope, requested_via) VALUES ('sir_1', 'prj_1', 'skv_1', 'sha256:abc', 'project', 'mcp')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, "
            "target_scope, requested_via) VALUES ('sir_2', 'prj_1', 'skv_1', 'sha256:abc', 'project', 'agent')"
        )
    connection.close()


def test_upgrade_enforces_visibility_owner_invariant(pre_marketplace_db: Path) -> None:
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    _users(connection, "user_1")
    connection.execute(
        "INSERT INTO skill_packages "
        "(id, owner_type, owner_id, namespace, name, display_name, source_type, source_locator) "
        "VALUES ('spk_1', 'user', 'user_1', 'personal', 'demo', 'Demo', 'upload', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions "
        "(id, package_id, content_hash, agent_plugin_schema, manifest_json, dlp_status, "
        "blob_key, blob_size_bytes) VALUES "
        "('skv_1', 'spk_1', 'sha256:abc', 'schema', '{}', 'passed', 'blob', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_marketplace_listings "
            "(id, package_id, version_id, name, visibility, owner_user_id) "
            "VALUES ('sml_1', 'spk_1', 'skv_1', 'demo', 'public', 'user_1')"
        )
    connection.close()


def test_downgrade_removes_marketplace_tables(pre_marketplace_db: Path) -> None:
    config = _cfg(pre_marketplace_db)
    command.upgrade(config, "063")
    command.downgrade(config, "062")
    connection = sqlite3.connect(str(pre_marketplace_db))
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "skill_packages" not in tables
    assert "skill_package_versions" not in tables
    connection.close()


def test_device_assignment_uniqueness_is_bound_to_user(pre_marketplace_db: Path) -> None:
    """P2-4: two members may both register device 'runner-1'; one member may not
    assign the same version to the same device twice; org targets stay unique."""
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    _users(connection, "user_1", "user_2")
    _orgs(connection, "org_1")
    connection.execute(
        "INSERT INTO skill_packages "
        "(id, owner_type, owner_id, org_id, namespace, name, display_name, source_type, source_locator) "
        "VALUES ('spk_1', 'org', 'org_1', 'org_1', 'o', 'demo', 'Demo', 'upload', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions "
        "(id, package_id, content_hash, agent_plugin_schema, manifest_json, dlp_status, "
        "blob_key, blob_size_bytes) VALUES "
        "('skv_1', 'spk_1', 'sha256:abc', 'schema', '{}', 'passed', 'blob', 1)"
    )
    insert = (
        "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, "
        "target_id, target_user_id) VALUES (?, 'org_1', 'skv_1', 'sha256:abc', ?, ?, ?)"
    )
    connection.execute(insert, ("osa_1", "device", "runner-1", "user_1"))
    connection.execute(insert, ("osa_2", "device", "runner-1", "user_2"))  # different member: OK
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("osa_3", "device", "runner-1", "user_1"))
    connection.execute(insert, ("osa_4", "org", "*", None))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("osa_5", "org", "*", None))
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"uq_org_skill_assignment_org_user", "uq_org_skill_assignment_device"} <= indexes
    connection.close()


def test_install_request_status_accepts_revoked(pre_marketplace_db: Path) -> None:
    """P2-3: approved/applied grants can be revoked (status 'revoked')."""
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    _users(connection, "user_1")
    _projects(connection, "prj_1")
    connection.execute(
        "INSERT INTO skill_packages "
        "(id, owner_type, owner_id, namespace, name, display_name, source_type, source_locator) "
        "VALUES ('spk_1', 'user', 'user_1', 'personal', 'demo', 'Demo', 'upload', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions "
        "(id, package_id, content_hash, agent_plugin_schema, manifest_json, dlp_status, "
        "blob_key, blob_size_bytes) VALUES "
        "('skv_1', 'spk_1', 'sha256:abc', 'schema', '{}', 'passed', 'blob', 1)"
    )
    connection.execute(
        "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, "
        "target_scope, status) VALUES ('sir_1', 'prj_1', 'skv_1', 'sha256:abc', 'project', 'revoked')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, "
            "target_scope, status) VALUES ('sir_2', 'prj_1', 'skv_1', 'sha256:abc', 'project', 'bogus')"
        )
    connection.close()


def test_upgrade_creates_version_tools_table_and_backfills_from_json(pre_marketplace_db: Path) -> None:
    """P2-2: skill_version_tools carries the per-tool compatibility set so the
    marketplace tool filter runs in SQL; an existing version's JSON is backfilled."""
    command.upgrade(_cfg(pre_marketplace_db), "063")
    connection = sqlite3.connect(str(pre_marketplace_db))
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "skill_version_tools" in tables
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_skill_version_tools_tool" in indexes
    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(skill_version_tools)")}
    assert set(columns) == {"version_id", "tool"}
    assert columns["version_id"][5] == 1 and columns["tool"][5] == 2  # composite PK
    connection.close()

    # Re-run the backfill against a version row that has no tool rows yet.
    from sessionfs.server.db.migrations.versions import __path__ as _versions_path  # noqa: F401
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m063", f"{_MIGRATIONS}/versions/063_skills_marketplace.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    connection = sqlite3.connect(str(pre_marketplace_db))
    _users(connection, "u1")
    connection.execute(
        "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, description, "
        "source_type, source_locator, source_subdir) VALUES "
        "('spk_1', 'user', 'u1', 'ns', 'pkg', 'Pkg', '', 'hosted-upload', 'upload:x', '')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, manifest_json, "
        "compatibility, dlp_status, blob_key, blob_size_bytes) VALUES "
        "('skv_1', 'spk_1', 'sha256:aa', 'schema', '{}', "
        "'{\"codex\": {}, \"claude-code\": {}}', 'passed', 'k', 1)"
    )
    connection.commit()
    connection.close()
    import sqlalchemy as sa
    from alembic import op as alembic_op
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine(f"sqlite:///{pre_marketplace_db}")
    with engine.begin() as bind:
        context = MigrationContext.configure(bind)
        with Operations.context(context):
            assert alembic_op is not None
            module._backfill_version_tools()
    connection = sqlite3.connect(str(pre_marketplace_db))
    rows = sorted(connection.execute("SELECT version_id, tool FROM skill_version_tools"))
    assert rows == [("skv_1", "claude-code"), ("skv_1", "codex")]
    connection.close()


def test_downgrade_removes_version_tools_table(pre_marketplace_db: Path) -> None:
    command.upgrade(_cfg(pre_marketplace_db), "063")
    command.downgrade(_cfg(pre_marketplace_db), "062")
    connection = sqlite3.connect(str(pre_marketplace_db))
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "skill_version_tools" not in tables
    connection.close()
