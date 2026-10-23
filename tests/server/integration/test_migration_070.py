"""Migration 070 (install disclosure / D2 consent) round-trip.

The prior schema is the REAL SQLAlchemy metadata minus the columns 070 adds.
The PostgreSQL leg runs when SFS_TEST_PG_URL is set.
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
from tests.pg_helpers import reset_pg_schema, wait_for_lock_waiters

COLUMNS = {
    "skill_package_versions": ("capability_manifest", "capability_digest", "manifest_version", "publication_seq"),
    "skill_consent_receipts": (
        "capability_digest",
        "consented_manifest",
        "carried_forward_from_receipt_id",
        "consent_rule_version",
    ),
    "org_skill_assignments": (
        "capability_digest",
        "consent_rule_version",
        "superseded_at",
        "superseded_by_assignment_id",
        "package_id",
    ),
    "skill_install_requests": (
        "approved_capability_digest", "approved_consent_rule_version", "package_id", "grant_scope_key",
        "superseded_at", "superseded_by_request_id",
    ),
}
INDEX = "idx_skill_versions_capability_digest"
SEQ_INDEX = "idx_skill_versions_publication_seq"

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _build_pre_070(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    connection.execute(f"DROP INDEX {INDEX}")
    connection.execute(f"DROP INDEX {SEQ_INDEX}")
    connection.execute("DROP INDEX uq_org_skill_assignment_package_target")
    connection.execute("DROP INDEX uq_org_skill_assignment_package_device")
    connection.execute("DROP INDEX uq_skill_request_active_grant")
    for table, columns in COLUMNS.items():
        for column in columns:
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.commit()
    connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _seed(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
        "VALUES ('u1', 'u1@example.com', 'pro', '2026-01-01 00:00:00', 1, 1)"
    )
    connection.execute(
        "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, "
        "source_type, source_locator) VALUES ('pk1', 'user', 'u1', 'ns', 'n', 'N', 'hosted', 'x')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v1', 'pk1', 'sha256:aa', 's', '{}', 'passed', 'k1', 1)"
    )
    connection.execute(
        "INSERT INTO skill_consent_receipts (id, version_id, content_hash, actor_type, capability, "
        "scope) VALUES ('r1', 'v1', 'sha256:aa', 'user', 'instructions', 'project')"
    )


def test_upgrade_adds_columns_with_safe_defaults_and_downgrade_removes_them(tmp_path: Path) -> None:
    path = tmp_path / "m070.db"
    _build_pre_070(path)
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "069")
    connection = sqlite3.connect(str(path))
    _seed(connection)
    connection.commit()
    for table, columns in COLUMNS.items():
        assert not set(columns) & _columns(connection, table)
    connection.close()

    command.upgrade(config, "070")
    connection = sqlite3.connect(str(path))
    for table, columns in COLUMNS.items():
        assert set(columns) <= _columns(connection, table), table
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert INDEX in indexes
    assert SEQ_INDEX in indexes
    # Existing rows: unknown manifest (fails closed), rule version 1, and the
    # backfilled publication order (a single version is sequence 1).
    assert connection.execute(
        "SELECT capability_manifest, capability_digest, manifest_version, publication_seq "
        "FROM skill_package_versions"
    ).fetchone() == ("{}", None, 0, 1)
    assert connection.execute(
        "SELECT consent_rule_version, carried_forward_from_receipt_id FROM skill_consent_receipts"
    ).fetchone() == (1, None)
    connection.close()

    command.downgrade(config, "069")
    connection = sqlite3.connect(str(path))
    for table, columns in COLUMNS.items():
        assert not set(columns) & _columns(connection, table), table
    # CHECK constraints survived (no table rebuild).
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO skill_consent_receipts (id, version_id, content_hash, actor_type, capability, "
            "scope) VALUES ('r2', 'v1', 'sha256:aa', 'user', 'bogus', 'project')"
        )
    assert connection.execute("SELECT count(*) FROM skill_consent_receipts").fetchone() == (1,)
    connection.close()
    command.upgrade(config, "070")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_round_trip() -> None:
    """001..head, down to 069 and back, on REAL PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    config = _cfg(os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "069")
    command.upgrade(config, "head")



def test_upgrade_backfills_package_and_supersedes_duplicate_assignments(tmp_path: Path) -> None:
    path = tmp_path / "dup.db"
    _build_pre_070(path)
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "069")
    connection = sqlite3.connect(str(path))
    _seed(connection)
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v2', 'pk1', 'sha256:bb', 's', '{}', 'passed', 'k2', 1)"
    )
    connection.execute("INSERT INTO organizations (id, name, slug, tier) VALUES ('o1', 'O', 'o1', 'team')")
    for assignment_id, version_id, created in (("a1", "v1", "2026-01-01"), ("a2", "v2", "2026-01-02")):
        connection.execute(
            "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, "
            "target_id, mode, created_at) VALUES (?, 'o1', ?, 'sha256:x', 'org', '*', 'required', ?)",
            (assignment_id, version_id, created),
        )
    connection.commit()
    connection.close()
    command.upgrade(config, "070")
    connection = sqlite3.connect(str(path))
    rows = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT id, package_id, revoked_at IS NOT NULL, superseded_by_assignment_id FROM org_skill_assignments"
        )
    }
    assert rows["a1"] == ("pk1", 1, "a2")
    assert rows["a2"] == ("pk1", 0, None)
    # One active assignment per (org, package, target) is enforced.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, "
            "target_id, mode, package_id) VALUES ('a3', 'o1', 'v1', 'sha256:x', 'org', '*', 'required', 'pk1')"
        )
    connection.close()


def test_upgrade_backfills_publication_seq_in_created_at_then_id_order(tmp_path: Path) -> None:
    """The publication_seq backfill orders already-published versions by
    created_at, breaking a same-second tie by id — the best signal legacy rows
    carry — so new publishes (MAX + 1) always sort after every backfilled row."""
    path = tmp_path / "seq.db"
    _build_pre_070(path)
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "069")
    connection = sqlite3.connect(str(path))
    _seed(connection)  # u1, pk1, v1
    connection.execute("UPDATE skill_package_versions SET created_at = '2026-02-01 00:00:00' WHERE id = 'v1'")
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes, created_at) "
        "VALUES ('v2', 'pk1', 'sha256:bb', 's', '{}', 'passed', 'k2', 1, '2026-02-01 00:00:00')"
    )
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes, created_at) "
        "VALUES ('v3', 'pk1', 'sha256:cc', 's', '{}', 'passed', 'k3', 1, '2026-01-01 00:00:00')"
    )
    connection.commit()
    connection.close()
    command.upgrade(config, "070")
    connection = sqlite3.connect(str(path))
    order = dict(connection.execute("SELECT id, publication_seq FROM skill_package_versions").fetchall())
    connection.close()
    assert order["v3"] < order["v1"] < order["v2"]  # created_at, then id for the tie


def test_upgrade_fails_closed_when_active_assignments_tie(tmp_path: Path) -> None:
    """Two active assignments for one package/target with the SAME created_at
    tie for "newest": migration 070 must fail closed rather than keep one by
    random id."""
    path = tmp_path / "tie.db"
    _build_pre_070(path)
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "069")
    connection = sqlite3.connect(str(path))
    _seed(connection)
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v2', 'pk1', 'sha256:bb', 's', '{}', 'passed', 'k2', 1)"
    )
    connection.execute("INSERT INTO organizations (id, name, slug, tier) VALUES ('o1', 'O', 'o1', 'team')")
    for assignment_id, version_id in (("a1", "v1"), ("a2", "v2")):
        connection.execute(
            "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, "
            "target_id, mode, created_at) VALUES (?, 'o1', ?, 'sha256:x', 'org', '*', 'required', "
            "'2026-01-01 00:00:00')",
            (assignment_id, version_id),
        )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="cannot choose which skill assignment stays active"):
        command.upgrade(config, "070")



@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_concurrent_active_assignments_are_serialized() -> None:
    """Two transactions inserting active assignments of different versions
    of one package for the same target: the second waits for the first and
    then fails on the unique index (one active per org/package/target)."""
    import asyncio

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    insert = sa.text(
        "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, "
        "target_id, mode, package_id) VALUES (:id, 'pgo', :v, 'sha256:x', 'org', '*', 'required', 'pgpk')"
    )

    async def scenario() -> None:
        async with engine.begin() as setup:
            await setup.execute(sa.text("DELETE FROM org_skill_assignments WHERE org_id = 'pgo'"))
            await setup.execute(sa.text("DELETE FROM organizations WHERE id = 'pgo'"))
            await setup.execute(sa.text(
                "INSERT INTO organizations (id, name, slug, tier) VALUES ('pgo', 'O', 'pgo', 'team')"
            ))
        first = await engine.connect()
        second = await engine.connect()
        try:
            await first.begin()
            await first.execute(insert, {"id": "pga1", "v": "pgv1"})
            await second.begin()
            pending = asyncio.ensure_future(second.execute(insert, {"id": "pga2", "v": "pgv2"}))
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()  # blocked on the first transaction's row
            await first.commit()
            with pytest.raises(IntegrityError):
                await pending
        finally:
            await first.close()
            await second.close()
            async with engine.begin() as cleanup:
                await cleanup.execute(sa.text("DELETE FROM org_skill_assignments WHERE org_id = 'pgo'"))
                await cleanup.execute(sa.text("DELETE FROM organizations WHERE id = 'pgo'"))
        await engine.dispose()

    asyncio.run(scenario())



@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_grant_scope_lock_serializes_two_approvals() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.services.skill_disclosure import grant_scope_lock_key, lock_grant_scope

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    key = grant_scope_lock_key("prj", "pk", "project", None, None)

    async def scenario() -> None:
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            await lock_grant_scope(first, key)
            pending = asyncio.ensure_future(lock_grant_scope(second, key))
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()  # the second approval waits
            await first.commit()
            await asyncio.wait_for(pending, 5)
            await second.commit()
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_advance_and_package_block_do_not_deadlock() -> None:
    """A whole-package block (version rows in id order, then assignments) and
    an advance (version rows in id order, then the assignment) take locks in
    the same order: the second waits, neither deadlocks."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.routes.marketplace_disclosure import lock_versions_in_order

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])

    async def scenario() -> None:
        async with engine.begin() as setup:
            for statement in (
                "DELETE FROM org_skill_assignments WHERE org_id = 'dlo'",
                "DELETE FROM skill_package_versions WHERE package_id = 'dlpk'",
                "DELETE FROM skill_packages WHERE id = 'dlpk'",
                "DELETE FROM organizations WHERE id = 'dlo'",
                "INSERT INTO organizations (id, name, slug, tier) VALUES ('dlo', 'O', 'dlo', 'team')",
                "INSERT INTO skill_packages (id, owner_type, owner_id, org_id, namespace, name, display_name, "
                "source_type, source_locator) VALUES ('dlpk', 'org', 'dlo', 'dlo', 'ns', 'n', 'N', 'hosted', 'x')",
                "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
                "manifest_json, dlp_status, blob_key, blob_size_bytes) VALUES "
                "('dlv1', 'dlpk', 'sha256:d1', 's', '{}', 'passed', 'dk1', 1), "
                "('dlv2', 'dlpk', 'sha256:d2', 's', '{}', 'passed', 'dk2', 1)",
                "INSERT INTO org_skill_assignments (id, org_id, version_id, content_hash, target_type, target_id, "
                "mode, package_id) VALUES ('dla1', 'dlo', 'dlv1', 'sha256:d1', 'org', '*', 'required', 'dlpk')",
            ):
                await setup.execute(sa.text(statement))
        async with AsyncSession(engine) as block, AsyncSession(engine) as advance:
            # Block: version rows in id order, then the assignment rows.
            await block.execute(sa.text(
                "SELECT id FROM skill_package_versions WHERE package_id = 'dlpk' ORDER BY id FOR UPDATE"
            ))

            async def run_advance() -> None:
                await lock_versions_in_order(advance, ["dlv2", "dlv1"])
                await advance.execute(sa.text("SELECT id FROM org_skill_assignments WHERE id = 'dla1' FOR UPDATE"))

            pending = asyncio.ensure_future(run_advance())
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()  # waits on the version rows, holds nothing the block needs
            await block.execute(sa.text(
                "UPDATE org_skill_assignments SET revoked_at = now() WHERE version_id IN ('dlv1', 'dlv2')"
            ))
            await block.commit()
            await asyncio.wait_for(pending, 5)
            await advance.commit()
        async with engine.begin() as cleanup:
            for statement in (
                "DELETE FROM org_skill_assignments WHERE org_id = 'dlo'",
                "DELETE FROM skill_package_versions WHERE package_id = 'dlpk'",
                "DELETE FROM skill_packages WHERE id = 'dlpk'",
                "DELETE FROM organizations WHERE id = 'dlo'",
            ):
                await cleanup.execute(sa.text(statement))
        await engine.dispose()

    asyncio.run(scenario())



@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_project_scope_approvals_from_different_users_serialize() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.services.skill_disclosure import grant_scope_lock_key, lock_grant_scope

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])

    async def scenario() -> None:
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            await lock_grant_scope(first, grant_scope_lock_key("prj", "pk", "project", "alice", None))
            pending = asyncio.ensure_future(
                lock_grant_scope(second, grant_scope_lock_key("prj", "pk", "project", "bob", None))
            )
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()
            await first.commit()
            await asyncio.wait_for(pending, 5)
            await second.commit()
        await engine.dispose()

    asyncio.run(scenario())



def test_upgrade_supersedes_duplicate_active_install_approvals(tmp_path: Path) -> None:
    path = tmp_path / "req.db"
    _build_pre_070(path)
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "069")
    connection = sqlite3.connect(str(path))
    _seed(connection)
    connection.execute(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes) "
        "VALUES ('v2', 'pk1', 'sha256:bb', 's', '{}', 'passed', 'k2', 1)"
    )
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, owner_id) VALUES ('p1', 'P', 'g/x', 'u1')"
    )
    for request_id, version_id, user, decided in (
        ("r1", "v1", "u1", "2026-01-01 00:00:00"), ("r2", "v2", "u2", "2026-01-02 00:00:00"),
    ):
        connection.execute(
            "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, target_scope, "
            "target_user_id, status, decided_at) VALUES (?, 'p1', ?, 'sha256:x', 'project', ?, 'approved', ?)",
            (request_id, version_id, user, decided),
        )
    connection.execute(
        "INSERT INTO skill_consent_receipts (id, version_id, content_hash, actor_type, capability, scope, "
        "install_request_id) VALUES ('rc1', 'v1', 'sha256:aa', 'user', 'instructions', 'project', 'r1')"
    )
    connection.commit()
    connection.close()
    command.upgrade(config, "070")
    connection = sqlite3.connect(str(path))
    rows = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT id, status, package_id, grant_scope_key, superseded_by_request_id FROM skill_install_requests"
        )
    }
    # Project scope ignores the requester: one grant scope, the older one superseded.
    assert rows["r1"] == ("revoked", "pk1", "project", "r2")
    assert rows["r2"] == ("approved", "pk1", "project", None)
    assert connection.execute("SELECT revoked_at IS NOT NULL FROM skill_consent_receipts WHERE id = 'rc1'").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE skill_install_requests SET status = 'approved' WHERE id = 'r1'")
    connection.close()



@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_one_active_approval_per_grant_scope() -> None:
    import asyncio

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    insert = sa.text(
        "INSERT INTO skill_install_requests (id, project_id, version_id, content_hash, target_scope, "
        "status, package_id, grant_scope_key) VALUES (:id, 'pgp', :v, 'sha256:x', 'project', 'approved', "
        "'pgpk', 'project')"
    )

    async def scenario() -> None:
        async with engine.begin() as setup:
            for statement in (
                "DELETE FROM skill_install_requests WHERE project_id = 'pgp'",
                "DELETE FROM projects WHERE id = 'pgp'",
                "DELETE FROM users WHERE id = 'pgu'",
                "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
                "VALUES ('pgu', 'pgu@example.com', 'pro', now(), true, true)",
                "INSERT INTO projects (id, name, git_remote_normalized, owner_id) VALUES ('pgp', 'P', 'g/pgp', 'pgu')",
            ):
                await setup.execute(sa.text(statement))
        async with engine.begin() as first:
            await first.execute(insert, {"id": "pgr1", "v": "pgv1"})
        with pytest.raises(IntegrityError):
            async with engine.begin() as second:
                await second.execute(insert, {"id": "pgr2", "v": "pgv2"})
        async with engine.begin() as cleanup:
            await cleanup.execute(sa.text("DELETE FROM skill_install_requests WHERE project_id = 'pgp'"))
            await cleanup.execute(sa.text("DELETE FROM projects WHERE id = 'pgp'"))
            await cleanup.execute(sa.text("DELETE FROM users WHERE id = 'pgu'"))
        await engine.dispose()

    asyncio.run(scenario())



@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_revocation_waits_for_an_approval_in_the_same_grant_scope() -> None:
    """Approval holds the grant-scope lock (project scope, requester A); a
    revocation of an earlier approval in that scope (requester B) computes
    the SAME key and waits until the approval commits."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.services.skill_disclosure import grant_scope_lock_key, lock_grant_scope

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])

    async def scenario() -> None:
        async with AsyncSession(engine) as approval, AsyncSession(engine) as revocation:
            await lock_grant_scope(approval, grant_scope_lock_key("prj", "pk", "project", "alice", None))
            await approval.execute(sa.text("SELECT 1"))
            pending = asyncio.ensure_future(
                lock_grant_scope(revocation, grant_scope_lock_key("prj", "pk", "project", "bob", None))
            )
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()
            await approval.commit()
            await asyncio.wait_for(pending, 5)
            await revocation.commit()
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_publication_seq_is_monotonic_under_the_publisher_lock() -> None:
    """Two publishes race for the same package: the second waits on the owner
    row lock, then allocates MAX(publication_seq) + 1, so it never collides
    with the first even when both start within the same second."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.routes.marketplace import _lock_user_row

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    alloc = sa.text(
        "SELECT COALESCE(MAX(publication_seq), 0) + 1 FROM skill_package_versions WHERE package_id = 'seqpk'"
    )
    insert = sa.text(
        "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
        "manifest_json, dlp_status, blob_key, blob_size_bytes, publication_seq) "
        "VALUES (:id, 'seqpk', :hash, 's', '{}', 'passed', :key, 1, :seq)"
    )

    async def scenario() -> None:
        async with engine.begin() as setup:
            for statement in (
                "DELETE FROM skill_package_versions WHERE package_id = 'seqpk'",
                "DELETE FROM skill_packages WHERE id = 'seqpk'",
                "DELETE FROM users WHERE id = 'sequ'",
                "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
                "VALUES ('sequ', 'sequ@example.com', 'pro', now(), true, true)",
                "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, "
                "source_type, source_locator) VALUES ('seqpk', 'user', 'sequ', 'ns', 'n', 'N', 'hosted', 'x')",
            ):
                await setup.execute(sa.text(statement))
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            await _lock_user_row(first, "sequ")
            seq1 = (await first.execute(alloc)).scalar_one()
            await first.execute(insert, {"id": "seqv1", "hash": "sha256:s1", "key": "k1", "seq": seq1})

            async def publish_second() -> int:
                await _lock_user_row(second, "sequ")
                seq2 = (await second.execute(alloc)).scalar_one()
                await second.execute(insert, {"id": "seqv2", "hash": "sha256:s2", "key": "k2", "seq": seq2})
                return seq2

            pending = asyncio.ensure_future(publish_second())
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()  # the second waits on the owner-row lock
            await first.commit()
            seq2 = await asyncio.wait_for(pending, 5)
            await second.commit()
            assert seq1 == 1 and seq2 == 2  # MAX + 1 under the lock, never a tie
        async with engine.begin() as cleanup:
            for statement in (
                "DELETE FROM skill_package_versions WHERE package_id = 'seqpk'",
                "DELETE FROM skill_packages WHERE id = 'seqpk'",
                "DELETE FROM users WHERE id = 'sequ'",
            ):
                await cleanup.execute(sa.text(statement))
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_postgres_held_update_recheck_waits_for_a_takedown() -> None:
    """The held-update listing's final deliverability re-check takes the
    content hash's SHARED advisory lock, so it waits for an in-flight block
    (which holds it exclusively) instead of offering a version the block is
    about to revoke."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from sessionfs.server.db.models import SkillPackageVersion
    from sessionfs.server.routes.marketplace import _take_delivery_locks, lock_content_hashes

    command.upgrade(_cfg(os.environ["SFS_TEST_PG_URL"]), "head")
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])

    async def scenario() -> None:
        version = SkillPackageVersion(id="heldv", content_hash="sha256:held")
        async with AsyncSession(engine) as takedown, AsyncSession(engine) as listing:
            await lock_content_hashes(takedown, ["sha256:held"])  # exclusive advisory lock

            async def recheck() -> None:
                await _take_delivery_locks(listing, version)  # shared advisory lock

            pending = asyncio.ensure_future(recheck())
            await wait_for_lock_waiters(engine, task=pending)
            assert not pending.done()  # the re-check waits for the block to commit
            await takedown.commit()
            await asyncio.wait_for(pending, 5)
            await listing.commit()
        await engine.dispose()

    asyncio.run(scenario())
