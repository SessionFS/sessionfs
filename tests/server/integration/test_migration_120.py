"""Migration 120: approval policies, requests and decisions.

SQLite: the prior schema is the real metadata with the three approval tables
dropped, stamped at 119. PostgreSQL: ``test_upgrade_on_postgres`` runs the
real chain (001-119) when SFS_TEST_PG_URL is set and asserts every unique key
and CHECK.
"""

from __future__ import annotations

import os
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

_TABLES = ("approval_decisions", "approval_requests", "approval_policies")

_POLICY = (
    "INSERT INTO approval_policies (id, org_id, lineage_id, name, target_kind, requirements{extra}) "
    "VALUES ('{pid}', 'org_m120', '{pid}', 'Auth', '{kind}', '[]'{extra_val})"
)


def _policy(pid: str, kind: str = "merge", extra: str = "", extra_val: str = "") -> str:
    return _POLICY.format(pid=pid, kind=kind, extra=extra, extra_val=extra_val)


def _request(rid: str, fp: str = "a" * 40, state: str = "pending", pid: str = "ap_1",
             now: str = "CURRENT_TIMESTAMP") -> str:
    return (
        "INSERT INTO approval_requests (id, policy_id, org_id, project_id, target_kind, "
        "target_id, target_fingerprint, state, expires_at) "
        f"VALUES ('{rid}', '{pid}', 'org_m120', 'proj_m120', 'merge', 'tk_m120', '{fp}', "
        f"'{state}', {now})"
    )


def _decision(did: str, rid: str = "ar_1", decision: str = "approve", reason: str = "ok",
              idx: int = 0) -> str:
    return (
        "INSERT INTO approval_decisions (id, request_id, approver_user_id, requirement_index, "
        f"decision, reason) VALUES ('{did}', '{rid}', 'u_m120', {idx}, '{decision}', '{reason}')"
    )


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> set[str]:
    inspector = sa.inspect(engine)
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


_BAD = (
    _policy("ap_bad1", kind="ticket_close"),
    _policy("ap_bad2", extra=", version", extra_val=", 0"),
    _policy("ap_bad3", extra=", filter", extra_val=", '" + "x" * 8193 + "'"),
    _request("ar_bad1", state="open"),
    _request("ar_dup", fp="a" * 40),  # duplicate of ar_1's unique key
    _decision("ad_bad1", decision="maybe"),
    _decision("ad_bad2", reason="r" * 2001),
    _decision("ad_dup"),  # second vote by the same approver
)


@pytest.fixture
def pre_120_db(tmp_path: Path) -> Path:
    path = tmp_path / "m120.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in _TABLES:
            conn.execute(sa.text(f"DROP TABLE {table}"))
        for col in ("pending_supersede_ids", "approval_pending"):
            conn.execute(sa.text(f"ALTER TABLE knowledge_entries DROP COLUMN {col}"))
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m120', 'm120@example.com', 1, 'free', CURRENT_TIMESTAMP, 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) VALUES ('org_m120', 'o', 'o-m120')"
        ))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "119")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_120_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_120_db}")
    command.upgrade(config, "120")
    engine = sa.create_engine(f"sqlite:///{pre_120_db}")
    try:
        assert {"lineage_id", "version", "supersedes_id", "blocking", "active", "filter"} <= (
            _columns(engine, "approval_policies")
        )
        assert {"target_fingerprint", "state", "expires_at"} <= _columns(
            engine, "approval_requests"
        )
        assert {"requirement_index", "decision", "reason"} <= _columns(
            engine, "approval_decisions"
        )
        with engine.begin() as conn:
            conn.execute(sa.text(_policy("ap_1")))
            assert conn.execute(sa.text(
                "SELECT version, blocking, active FROM approval_policies WHERE id = 'ap_1'"
            )).one() == (1, 1, 1)
            conn.execute(sa.text(_request("ar_1")))
            # Same target, a different sha: a separate request.
            conn.execute(sa.text(_request("ar_2", fp="b" * 40)))
            conn.execute(sa.text(_decision("ad_1")))
        for bad in _BAD:
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(bad))
        with engine.begin() as conn:
            _reraise_and_former_member(conn)
        assert {"approval_pending", "pending_supersede_ids"} <= _columns(
            engine, "knowledge_entries"
        )
    finally:
        engine.dispose()

    command.downgrade(config, "119")
    engine = sa.create_engine(f"sqlite:///{pre_120_db}")
    try:
        for table in _TABLES:
            assert not _columns(engine, table)
    finally:
        engine.dispose()
    command.upgrade(config, "120")


def _reraise_and_former_member(conn, now: str = "CURRENT_TIMESTAMP") -> None:
    """Only PENDING rows are unique per key: a rejected request can be raised
    again beside the old row. A decision may carry a NULL approver (the user
    was deleted) and a second NULL-approver decision is still allowed."""
    conn.execute(sa.text(_request("ar_r1", fp="c" * 40, state="rejected", now=now)))
    conn.execute(sa.text(_request("ar_r2", fp="c" * 40, state="pending", now=now)))
    conn.execute(sa.text(
        "INSERT INTO approval_decisions (id, request_id, approver_user_id, requirement_index, "
        "decision, reason) VALUES ('ad_n1', 'ar_r1', NULL, 0, 'approve', ''), "
        "('ad_n2', 'ar_r1', NULL, 0, 'approve', '')"
    ))


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
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-119) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "119")

    def seed(conn) -> None:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
            "VALUES ('u_m120', 'm120@example.com', true, 'free', now(), true)"
        ))
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug) VALUES ('org_m120', 'o', 'o-m120')"
        ))

    _pg(url, seed)
    command.upgrade(config, "120")

    def check(conn) -> None:
        conn.execute(sa.text(_policy("ap_1")))
        assert tuple(conn.execute(sa.text(
            "SELECT version, blocking, active FROM approval_policies WHERE id = 'ap_1'"
        )).one()) == (1, True, True)
        conn.execute(sa.text(_request("ar_1", now="now()")))
        conn.execute(sa.text(_request("ar_2", fp="b" * 40, now="now()")))
        conn.execute(sa.text(_decision("ad_1")))
        for bad in _BAD:
            bad = bad.replace("CURRENT_TIMESTAMP", "now()")
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad))
            nested.rollback()
        _reraise_and_former_member(conn, now="now()")
        # Deleting the approver keeps the decision with a NULL approver.
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u_m120'"))
        assert conn.execute(sa.text(
            "SELECT approver_user_id FROM approval_decisions WHERE id = 'ad_1'"
        )).scalar() is None
        # Deleting a policy version cascades to its requests and decisions.
        conn.execute(sa.text("DELETE FROM approval_policies WHERE id = 'ap_1'"))
        assert conn.execute(sa.text("SELECT count(*) FROM approval_decisions")).scalar() == 0

    _pg(url, check)
    command.downgrade(config, "119")

    def gone(conn) -> None:
        for table in _TABLES:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalar() == 0

    _pg(url, gone)
    command.upgrade(config, "head")
