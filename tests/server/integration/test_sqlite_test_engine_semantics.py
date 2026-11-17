"""What the SQLite test engine does and doesn't do (tk_e3eecad8c3874d92).

``test_policy_writes_are_audited`` lost a policy DELETE's UPDATE after
``deferred_jobs.record_job`` was added. The savepoint was suspected; it isn't
the cause. On aiosqlite (Python's sqlite3 opens a transaction before DML) an
UPDATE followed by ``begin_nested()`` + commit keeps both writes.

The cause is the test engine itself: ``sqlite+aiosqlite://`` uses a
``StaticPool``, so every session in a test shares ONE DBAPI connection and
one transaction. A post-response task that closes its own session (a
ROLLBACK) while a request is mid-transaction undoes that request's
uncommitted writes. Production (PostgreSQL, one connection per session) is
not affected; tests that run post-response tasks concurrently with later
requests must stub the fast path or turn it off.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.asyncio


async def _tables(db_engine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe_p (id INTEGER PRIMARY KEY, active INTEGER)"))
        await conn.execute(text("CREATE TABLE probe_j (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO probe_p VALUES (1, 1)"))


async def test_savepoint_keeps_the_earlier_write_on_sqlite(db_engine) -> None:
    await _tables(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("UPDATE probe_p SET active = 0 WHERE id = 1"))
        async with s.begin_nested():
            await s.execute(text("INSERT INTO probe_j VALUES (1)"))
        await s.commit()
    async with factory() as s:
        assert (await s.execute(text("SELECT active FROM probe_p"))).scalar() == 0
        assert (await s.execute(text("SELECT count(*) FROM probe_j"))).scalar() == 1


async def test_shared_test_connection_lets_another_session_roll_back_a_write(db_engine) -> None:
    """The hazard, pinned down: one session's ROLLBACK undoes another
    session's uncommitted UPDATE because they share the connection."""
    await _tables(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    request = factory()
    background = factory()
    try:
        await request.execute(text("UPDATE probe_p SET active = 0 WHERE id = 1"))
        await background.execute(text("SELECT 1"))
        await background.rollback()  # e.g. a post-response task closing its session
        await request.commit()
    finally:
        await request.close()
        await background.close()
    async with factory() as s:
        assert (await s.execute(text("SELECT active FROM probe_p"))).scalar() == 1  # lost
