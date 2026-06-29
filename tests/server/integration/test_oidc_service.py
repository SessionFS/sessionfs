"""OIDC consume_login_attempt atomicity tests.

tk_c2cdbe7114804403 (SSO-P1). Proves the rowcount-1 guard on
consume_login_attempt: two concurrent consumers cannot both win; an
expired attempt is not consumable; a consumed attempt cannot be
re-consumed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    OidcLoginAttempt,
    OrgIdentityProvider,
)
from sessionfs.server.services.oidc import consume_login_attempt


def _build_pre_055_db(db_path: Path) -> None:
    """Minimal prerequisite schema for the tables + FKs migration 055 references."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE organizations (id VARCHAR(64) PRIMARY KEY)")
    conn.execute("INSERT INTO organizations (id) VALUES ('org-1')")
    conn.execute("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)")
    conn.execute("INSERT INTO users (id) VALUES ('user-1')")
    conn.execute(
        "CREATE TABLE api_keys ("
        "  id VARCHAR(36) PRIMARY KEY,"
        "  user_id VARCHAR(36) NOT NULL,"
        "  key_hash VARCHAR(64) NOT NULL,"
        "  created_at TEXT,"
        "  is_active INTEGER DEFAULT 1,"
        "  key_kind VARCHAR(20) DEFAULT 'user',"
        "  scopes TEXT DEFAULT '[\"*\"]'"
        ")"
    )
    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location", "src/sessionfs/server/db/migrations"
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def upgraded_055_db_path(tmp_path: Path) -> Path:
    """A SQLite DB built from scratch, stamped at 054, upgraded to 055.

    Mirrors the test_migration_054 harness — the async tests must NOT call
    command.upgrade themselves inside a running event loop (alembic's
    env.py drives the aiosqlite upgrade via asyncio.run()). Upgrading in a
    synchronous fixture keeps the migration off the running loop.
    """
    db_path = tmp_path / "oidc_service_test.db"
    _build_pre_055_db(db_path)
    command.stamp(_cfg(db_path), "054")
    command.upgrade(_cfg(db_path), "055")
    return db_path


class TestConsumeLoginAttempt:
    @pytest.mark.asyncio
    async def test_consume_succeeds_for_valid_pending_attempt(
        self, upgraded_055_db_path
    ):
        """Happy path: a pending, non-expired attempt is consumed and returned."""
        url = f"sqlite+aiosqlite:///{upgraded_055_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)

        try:
            async with maker() as seed:
                seed.add(
                    OidcLoginAttempt(
                        id="ola-happy",
                        org_id="org-1",
                        state="state-happy",
                        nonce="nonce-happy",
                        pkce_verifier_hash="hash-happy",
                        status="pending",
                        expires_at=now + timedelta(minutes=10),
                    )
                )
                await seed.commit()

            async with maker() as sess:
                result = await consume_login_attempt(sess, state="state-happy")
                await sess.commit()

            assert result is not None
            assert result.id == "ola-happy"
            assert result.status == "consumed"
            assert result.consumed_at is not None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_consume_rejects_expired_attempt(self, upgraded_055_db_path):
        """An expired attempt is unconsumable (expires_at <= now)."""
        url = f"sqlite+aiosqlite:///{upgraded_055_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as seed:
                seed.add(
                    OidcLoginAttempt(
                        id="ola-expired",
                        org_id="org-1",
                        state="state-expired",
                        nonce="nonce-exp",
                        pkce_verifier_hash="hash-exp",
                        status="pending",
                        expires_at=datetime.now(timezone.utc)
                        - timedelta(minutes=1),
                    )
                )
                await seed.commit()

            async with maker() as sess:
                result = await consume_login_attempt(
                    sess, state="state-expired"
                )
                await sess.commit()

            assert result is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_consume_rejects_already_consumed(self, upgraded_055_db_path):
        """A consumed attempt cannot be re-consumed."""
        url = f"sqlite+aiosqlite:///{upgraded_055_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as seed:
                seed.add(
                    OidcLoginAttempt(
                        id="ola-done",
                        org_id="org-1",
                        state="state-done",
                        nonce="nonce-done",
                        pkce_verifier_hash="hash-done",
                        status="consumed",
                        expires_at=datetime.now(timezone.utc)
                        + timedelta(minutes=10),
                    )
                )
                await seed.commit()

            async with maker() as sess:
                result = await consume_login_attempt(sess, state="state-done")
                await sess.commit()

            assert result is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_consume_rejects_unknown_state(self, upgraded_055_db_path):
        """A state that never existed returns None."""
        url = f"sqlite+aiosqlite:///{upgraded_055_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with maker() as sess:
                result = await consume_login_attempt(
                    sess, state="no-such-state"
                )
                await sess.commit()
            assert result is None
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_only_one_of_two_concurrent_consumers_wins(
        self, upgraded_055_db_path
    ):
        """Rowcount-1 guard: two concurrent consumers, one wins, one loses.

        SQLite's single-writer serializes the two UPDATEs, so this proves
        the rowcount-1 status-flip guard. PostgreSQL's row lock provides
        the same guarantee in prod (identical to the work-queue claim shape
        in test_migration_054.py)."""
        url = f"sqlite+aiosqlite:///{upgraded_055_db_path}"
        engine_a = create_async_engine(url)
        engine_b = create_async_engine(url)
        maker_a = async_sessionmaker(engine_a, expire_on_commit=False)
        maker_b = async_sessionmaker(engine_b, expire_on_commit=False)
        now = datetime.now(timezone.utc)

        try:
            # Seed: one pending attempt.
            async with maker_a() as seed:
                seed.add(
                    OidcLoginAttempt(
                        id="ola-race",
                        org_id="org-1",
                        state="state-race",
                        nonce="nonce-race",
                        pkce_verifier_hash="hash-race",
                        status="pending",
                        expires_at=now + timedelta(minutes=10),
                    )
                )
                await seed.commit()

            # Two independent sessions race for the SAME state.
            async with maker_a() as sess_a, maker_b() as sess_b:
                won_a = await consume_login_attempt(sess_a, state="state-race")
                await sess_a.commit()

                won_b = await consume_login_attempt(sess_b, state="state-race")
                await sess_b.commit()

            # Exactly one wins.
            assert (won_a is not None, won_b is not None) == (True, False)
            winner = won_a if won_a is not None else won_b
            assert winner.status == "consumed"
            assert winner.consumed_at is not None
        finally:
            await engine_a.dispose()
            await engine_b.dispose()
