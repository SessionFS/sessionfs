"""Migration 054 (work queues) round-trip + atomic-claim concurrency.

tk_529a64620db846f5 (WQ-P1). Two concerns:

  1. Direct SQLite upgrade()/downgrade() round-trip — mirrors the
     test_migration_053 harness: build the minimal pre-054 schema with raw
     sqlite3, stamp at 053, upgrade to 054, and assert the three tables +
     idx_wqi_claim exist (and that the inline CHECKs reject a bad mode /
     item_status), then downgrade and assert they're gone. The broken pre-003
     PG-only migration chain is bypassed — stamp at 053, upgrade only runs
     053→054.

  2. Two-independent-session atomic-claim test — proves no double-claim: two
     separate connections call the REAL claim_work_queue_item helper on the
     SAME contested item; exactly one returns True (rowcount==1). The
     PostgreSQL equivalent (row-lock serialization) is cloud-deploy-verify —
     same precedent as the multi-repo merge SELECT FOR UPDATE condition.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import WorkQueue, WorkQueueItem
from sessionfs.server.services.work_queues import claim_work_queue_item


def _build_pre_054_db(db_path: Path) -> None:
    """Minimal prerequisite schema for the tables migration 054 references."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY)")
    conn.execute("CREATE TABLE agent_runs (id VARCHAR(64) PRIMARY KEY)")
    conn.execute("INSERT INTO projects (id) VALUES ('proj-1')")
    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def migration_054_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_054_test.db"
    _build_pre_054_db(db_path)
    command.stamp(_cfg(db_path), "053")
    return db_path


@pytest.fixture
def upgraded_054_db_path(migration_054_db_path: Path) -> Path:
    """A DB already upgraded through 054.

    The async claim tests must NOT call command.upgrade themselves — alembic's
    env.py drives the aiosqlite upgrade via asyncio.run(), which raises inside
    the pytest event loop (asyncio_mode='auto'). Running the upgrade here, in a
    synchronous fixture, keeps the migration off the running loop.
    """
    command.upgrade(_cfg(migration_054_db_path), "054")
    return migration_054_db_path


class TestMigration054:
    def test_upgrade_creates_tables_and_claim_index(self, migration_054_db_path):
        command.upgrade(_cfg(migration_054_db_path), "054")
        conn = sqlite3.connect(str(migration_054_db_path))

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "work_queues" in tables
        assert "work_queue_items" in tables
        assert "work_queue_runs" in tables

        # Cursor split + directive-lease columns present on items.
        item_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('work_queue_items')"
            ).fetchall()
        }
        for col in (
            "last_seen_comment_at",
            "last_seen_comment_id",
            "last_acked_comment_at",
            "last_acked_comment_id",
            "open_directive_id",
            "open_directive_run_id",
            "item_status",
            "next_eligible_at",
            "attempts",
        ):
            assert col in item_cols, f"Missing column: {col}"

        # work_queue_runs links to agent_runs via nullable agent_run_id.
        run_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('work_queue_runs')"
            ).fetchall()
        }
        assert "agent_run_id" in run_cols
        assert "work_queue_item_id" in run_cols

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_wqi_claim" in indexes
        conn.close()

    def test_check_constraints_enforced(self, migration_054_db_path):
        command.upgrade(_cfg(migration_054_db_path), "054")
        conn = sqlite3.connect(str(migration_054_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Bad mode → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_queues "
                "(id, project_id, name, mode, status, created_by_user_id) "
                "VALUES ('wq-bad', 'proj-1', 'q', 'not_a_mode', 'active', 'u1')"
            )
            conn.commit()
        conn.rollback()

        # Valid queue → ok.
        conn.execute(
            "INSERT INTO work_queues "
            "(id, project_id, name, mode, status, created_by_user_id) "
            "VALUES ('wq-ok', 'proj-1', 'q', 'review_until_clean', 'active', 'u1')"
        )
        conn.commit()

        # Bad item_status → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_queue_items "
                "(id, work_queue_id, ticket_id, item_status) "
                "VALUES ('wqi-bad', 'wq-ok', 'tk-1', 'bogus')"
            )
            conn.commit()
        conn.rollback()

        # Valid item → ok.
        conn.execute(
            "INSERT INTO work_queue_items "
            "(id, work_queue_id, ticket_id, item_status) "
            "VALUES ('wqi-ok', 'wq-ok', 'tk-1', 'pending')"
        )
        conn.commit()
        conn.close()

    def test_downgrade_removes_tables(self, migration_054_db_path):
        cfg = _cfg(migration_054_db_path)
        command.upgrade(cfg, "054")
        command.downgrade(cfg, "053")
        conn = sqlite3.connect(str(migration_054_db_path))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "work_queues" not in tables
        assert "work_queue_items" not in tables
        assert "work_queue_runs" not in tables
        conn.close()


class TestAtomicClaim:
    """Two-independent-session claim: exactly one worker wins a contested item.

    The PostgreSQL equivalent (row-lock serialization of the two concurrent
    UPDATEs) is cloud-deploy-verify — SQLite's single-writer serialization
    proves the rowcount==1 status-flip guard here; PG's row lock provides the
    same guarantee in prod (precedent: the multi-repo merge SELECT FOR UPDATE
    condition).
    """

    @pytest.mark.asyncio
    async def test_only_one_session_wins(self, upgraded_054_db_path):
        migration_054_db_path = upgraded_054_db_path

        url = f"sqlite+aiosqlite:///{migration_054_db_path}"
        engine_a = create_async_engine(url)
        engine_b = create_async_engine(url)
        maker_a = async_sessionmaker(engine_a, expire_on_commit=False)
        maker_b = async_sessionmaker(engine_b, expire_on_commit=False)

        # Seed one eligible item (pending, next_eligible_at in the past) THROUGH
        # SQLAlchemy so the stored datetime format matches what the helper's
        # `next_eligible_at <= now` comparison binds (raw-string seeding with a
        # different ISO format would lexically mis-compare on SQLite TEXT).
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        async with maker_a() as seed:
            seed.add(
                WorkQueue(
                    id="wq-1",
                    project_id="proj-1",
                    name="q",
                    mode="review_until_clean",
                    status="active",
                    created_by_user_id="u1",
                )
            )
            seed.add(
                WorkQueueItem(
                    id="wqi-1",
                    work_queue_id="wq-1",
                    ticket_id="tk-1",
                    item_status="pending",
                    next_eligible_at=past,
                )
            )
            await seed.commit()

        try:
            # Two independent sessions/connections race for the SAME item.
            async with maker_a() as sess_a, maker_b() as sess_b:
                won_a = await claim_work_queue_item(
                    sess_a, item_id="wqi-1", run_id="wqr-a"
                )
                await sess_a.commit()

                won_b = await claim_work_queue_item(
                    sess_b, item_id="wqi-1", run_id="wqr-b"
                )
                await sess_b.commit()

            # Exactly one wins — no double-claim.
            assert (won_a, won_b) == (True, False)

            # The winner stamped its run_id and flipped the status.
            check = sqlite3.connect(str(migration_054_db_path))
            status, run_id = check.execute(
                "SELECT item_status, open_directive_run_id "
                "FROM work_queue_items WHERE id='wqi-1'"
            ).fetchone()
            check.close()
            assert status == "active"
            assert run_id == "wqr-a"
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    @pytest.mark.asyncio
    async def test_future_next_eligible_not_claimable(self, upgraded_054_db_path):
        migration_054_db_path = upgraded_054_db_path
        url = f"sqlite+aiosqlite:///{migration_054_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        try:
            async with maker() as seed:
                seed.add(
                    WorkQueue(
                        id="wq-2",
                        project_id="proj-1",
                        name="q2",
                        mode="review_until_clean",
                        status="active",
                        created_by_user_id="u1",
                    )
                )
                seed.add(
                    WorkQueueItem(
                        id="wqi-2",
                        work_queue_id="wq-2",
                        ticket_id="tk-2",
                        item_status="pending",
                        next_eligible_at=future,
                    )
                )
                await seed.commit()

            async with maker() as sess:
                won = await claim_work_queue_item(
                    sess, item_id="wqi-2", run_id="wqr-x"
                )
                await sess.commit()
            assert won is False
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_open_directive_lease_not_fresh_claimed(self, upgraded_054_db_path):
        """An item with an OPEN directive lease is never fresh-claimed, even
        when its backoff has expired — that belongs to the step engine's
        re-emit path, and a fresh claim would clobber open_directive_run_id.
        (Codex R1 on tk_529a64620db846f5.)
        """
        migration_054_db_path = upgraded_054_db_path
        url = f"sqlite+aiosqlite:///{migration_054_db_path}"
        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        try:
            async with maker() as seed:
                seed.add(
                    WorkQueue(
                        id="wq-3",
                        project_id="proj-1",
                        name="q3",
                        mode="review_until_clean",
                        status="active",
                        created_by_user_id="u1",
                    )
                )
                seed.add(
                    WorkQueueItem(
                        id="wqi-3",
                        work_queue_id="wq-3",
                        ticket_id="tk-3",
                        item_status="waiting",
                        open_directive_id="dir_1",
                        open_directive_run_id="wqr_old",
                        next_eligible_at=past,
                    )
                )
                await seed.commit()

            async with maker() as sess:
                won = await claim_work_queue_item(
                    sess, item_id="wqi-3", run_id="wqr_new"
                )
                await sess.commit()
            assert won is False

            # Lease untouched — the existing directive run still owns it.
            check = sqlite3.connect(str(migration_054_db_path))
            status, run_id = check.execute(
                "SELECT item_status, open_directive_run_id "
                "FROM work_queue_items WHERE id='wqi-3'"
            ).fetchone()
            check.close()
            assert status == "waiting"
            assert run_id == "wqr_old"
        finally:
            await engine.dispose()
