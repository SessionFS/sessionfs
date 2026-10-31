"""Migration 116: per-org delivery webhooks and idempotency events (track F2).

SQLite: the prior schema is the real metadata minus the two new tables,
stamped at 114. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain
when SFS_TEST_PG_URL is set and asserts column types, nullability, the inline
CHECK constraints and the ``(org_id, provider)`` / ``(provider, org_id,
idempotency_key)`` primary-key uniqueness.
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

COLUMNS = {
    "org_delivery_webhooks": {
        "org_id", "provider", "encrypted_secret", "secret_last4",
        "created_by_user_id", "rotated_at",
    },
    "delivery_webhook_events": {
        "provider", "org_id", "idempotency_key", "received_at",
    },
}

# name -> (pg data_type, character_maximum_length, is_nullable)
PG_COLUMNS = {
    "org_delivery_webhooks": {
        "org_id": ("character varying", 64, "NO"),
        "provider": ("character varying", 20, "NO"),
        "encrypted_secret": ("text", None, "NO"),
        "secret_last4": ("character varying", 4, "NO"),
        "created_by_user_id": ("character varying", 64, "YES"),
        "rotated_at": ("timestamp with time zone", None, "NO"),
    },
    "delivery_webhook_events": {
        "provider": ("character varying", 20, "NO"),
        "org_id": ("character varying", 64, "NO"),
        "idempotency_key": ("character varying", 64, "NO"),
        "received_at": ("timestamp with time zone", None, "NO"),
    },
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table(table):
            return set()
        return {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()


def _seed_org(conn) -> None:
    """A minimal org + user so delivery-webhook rows have a home."""
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug) VALUES ('org_m116', 'o', 'o-m116')"
    ))
    conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_m116', 'm116@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    ))


@pytest.fixture
def pre_116_db(tmp_path: Path) -> Path:
    path = tmp_path / "m116.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate the pre-116 schema: the real metadata minus the two tables.
        conn.execute(sa.text("DROP TABLE delivery_webhook_events"))
        conn.execute(sa.text("DROP TABLE org_delivery_webhooks"))
        _seed_org(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "115")
    return path


def test_upgrade_adds_tables_and_downgrade_reverses(pre_116_db: Path) -> None:
    for table in COLUMNS:
        assert table not in _columns(pre_116_db, table)

    config = _cfg(f"sqlite+aiosqlite:///{pre_116_db}")
    command.upgrade(config, "116")
    for table, cols in COLUMNS.items():
        assert cols <= _columns(pre_116_db, table), table

    # Server defaults + composite-PK uniqueness + CHECK are real, not just
    # Python-side.
    engine = sa.create_engine(f"sqlite:///{pre_116_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO org_delivery_webhooks (org_id, provider, encrypted_secret, secret_last4) "
                "VALUES ('org_m116', 'github', 'enc', 'abcd')"
            ))
            row = conn.execute(sa.text(
                "SELECT rotated_at, created_by_user_id FROM org_delivery_webhooks "
                "WHERE org_id = 'org_m116' AND provider = 'github'"
            )).one()
            assert row[0] is not None  # server_default now()
            assert row[1] is None  # created_by_user_id nullable
            # Uniqueness on (org, provider).
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO org_delivery_webhooks (org_id, provider, encrypted_secret, secret_last4) "
                    "VALUES ('org_m116', 'github', 'enc2', 'wxyz')"
                ))
            # CHECK on provider.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO org_delivery_webhooks (org_id, provider, encrypted_secret, secret_last4) "
                    "VALUES ('org_m116', 'bitbucket', 'enc3', 'bbbb')"
                ))

            conn.execute(sa.text(
                "INSERT INTO delivery_webhook_events (provider, org_id, idempotency_key) "
                "VALUES ('github', 'org_m116', 'delivery-1')"
            ))
            row = conn.execute(sa.text(
                "SELECT received_at FROM delivery_webhook_events "
                "WHERE provider = 'github' AND org_id = 'org_m116' AND idempotency_key = 'delivery-1'"
            )).one()
            assert row[0] is not None
            # Uniqueness on (provider, org, idempotency_key).
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO delivery_webhook_events (provider, org_id, idempotency_key) "
                    "VALUES ('github', 'org_m116', 'delivery-1')"
                ))
            # CHECK on provider.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO delivery_webhook_events (provider, org_id, idempotency_key) "
                    "VALUES ('bitbucket', 'org_m116', 'delivery-2')"
                ))
    finally:
        engine.dispose()

    command.downgrade(config, "115")
    for table in COLUMNS:
        assert table not in _columns(pre_116_db, table)
    command.upgrade(config, "116")
    for table, cols in COLUMNS.items():
        assert cols <= _columns(pre_116_db, table), table


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None, str]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-115) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "115")

    def seed(conn) -> None:
        _seed_org(conn)

    _pg(url, seed)
    command.upgrade(config, "116")

    def check(conn) -> None:
        for table, expected in PG_COLUMNS.items():
            cols = _pg_columns(conn, table)
            for name, wanted in expected.items():
                assert cols[name] == wanted, (table, name, cols[name])

    _pg(url, check)
    command.downgrade(config, "115")

    def gone(conn) -> None:
        for table in PG_COLUMNS:
            assert not _pg_columns(conn, table)

    _pg(url, gone)
    command.upgrade(config, "head")
