"""Migration 134: idempotency_keys (tk_bd80efd511da4a2b).

SQLite: the prior schema is the real metadata with ``idempotency_keys``
dropped, stamped at 133. PostgreSQL: ``test_upgrade_on_postgres`` runs the
real chain up to 133 when SFS_TEST_PG_URL is set.
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
_COLUMNS = {
    "scope_hash",
    "request_hash",
    "status",
    "lease_token",
    "response_status",
    "response_headers",
    "response_body",
    "replayable",
    "created_at",
    "completed_at",
    "locked_until",
}
_INSERT = (
    "INSERT INTO idempotency_keys (scope_hash, request_hash, status) "
    "VALUES (:s, :r, :st)"
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_134_db(tmp_path: Path) -> Path:
    path = tmp_path / "m134.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE idempotency_keys"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "133")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_134_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_134_db}")
    command.upgrade(config, "134")
    engine = sa.create_engine(f"sqlite:///{pre_134_db}")
    try:
        inspector = sa.inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("idempotency_keys")}
        assert set(cols) == _COLUMNS
        assert cols["response_body"]["nullable"] is True
        assert inspector.get_pk_constraint("idempotency_keys")["constrained_columns"] == [
            "scope_hash"
        ]
        assert "idx_idempotency_keys_created" in {
            i["name"] for i in inspector.get_indexes("idempotency_keys")
        }
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"s": "a" * 64, "r": "b" * 64, "st": "in_progress"})
            row = conn.execute(sa.text("SELECT replayable, created_at FROM idempotency_keys")).one()
            assert not row[0] and row[1] is not None
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"s": "a" * 64, "r": "c" * 64, "st": "completed"})
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(_INSERT), {"s": "d" * 64, "r": "c" * 64, "st": "bogus"})
    finally:
        engine.dispose()

    command.downgrade(config, "133")
    engine = sa.create_engine(f"sqlite:///{pre_134_db}")
    try:
        assert not sa.inspect(engine).has_table("idempotency_keys")
    finally:
        engine.dispose()
    command.upgrade(config, "134")


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
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "133")
    command.upgrade(config, "134")

    def check(conn) -> None:
        cols = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'idempotency_keys'"
            ))
        }
        assert set(cols) == _COLUMNS
        assert cols["response_body"] == "bytea"
        assert cols["created_at"] == "timestamp with time zone"
        conn.execute(sa.text(_INSERT), {"s": "a" * 64, "r": "b" * 64, "st": "in_progress"})
        conn.execute(
            sa.text("UPDATE idempotency_keys SET response_body = :b WHERE scope_hash = :s"),
            {"b": b"\x00\xff", "s": "a" * 64},
        )
        assert conn.execute(sa.text("SELECT response_body FROM idempotency_keys")).scalar() == b"\x00\xff"
        for params in (
            {"s": "a" * 64, "r": "c" * 64, "st": "completed"},
            {"s": "d" * 64, "r": "c" * 64, "st": "bogus"},
        ):
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(_INSERT), params)
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, "133")

    def gone(conn) -> None:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'idempotency_keys'"
        )).scalar() == 0

    _pg(url, gone)
    command.upgrade(config, "head")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_reservation_flow_on_postgres() -> None:
    """The middleware's reserve / take-over / complete / release on real PG."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server import idempotency as idem
    from sessionfs.server.db.engine import get_db
    from tests.pg_helpers import upgrade_pg_to_head

    reset_pg_schema()
    upgrade_pg_to_head()
    url = os.environ["SFS_TEST_PG_URL"]

    async def _go() -> None:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def override():
            async with factory() as session:
                yield session

        scope = {"app": SimpleNamespace(dependency_overrides={get_db: override})}
        lease = timedelta(minutes=5)
        try:
            async def reserve(r_hash: str):
                async with factory() as s:
                    return await idem._reserve(s, "h" * 64, r_hash, lease)

            # Two racers on one key: exactly one wins.
            first, second = await asyncio.gather(reserve("r" * 64), reserve("r" * 64))
            outcomes = sorted([first.outcome, second.outcome])
            assert outcomes == ["in_progress", "reserved"], outcomes
            winner = first if first.outcome == "reserved" else second
            assert (await reserve("x" * 64)).outcome == "mismatch"

            # Expire the lease; a retry takes it over and the old holder is fenced.
            async with factory() as s:
                await s.execute(
                    sa.update(idem.IdempotencyKey).values(
                        locked_until=datetime.now(timezone.utc) - timedelta(seconds=1)
                    )
                )
                await s.commit()
            taken = await reserve("r" * 64)
            assert taken.outcome == "reserved" and taken.token != winner.token
            assert winner.token is not None and taken.token is not None
            await idem._complete(scope, "h" * 64, winner.token, 201, [], b"stale")
            await idem._release(scope, "h" * 64, winner.token)
            await idem._complete(
                scope, "h" * 64, taken.token, 201, [(b"content-type", b"application/json")], b"{}"
            )
            replay = await reserve("r" * 64)
            assert replay.outcome == "replay" and replay.row is not None
            assert replay.row.response_body == b"{}"

            async with factory() as s:
                old = datetime.now(timezone.utc) - timedelta(hours=25)
                await s.execute(sa.update(idem.IdempotencyKey).values(created_at=old))
                await s.commit()
                assert await idem.sweep_idempotency_keys(s) == {"deleted": 1, "batches": 1}
        finally:
            await engine.dispose()

    asyncio.run(_go())
