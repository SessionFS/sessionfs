"""Review-request guarantees under real concurrency, on PostgreSQL.

Runs only with SFS_TEST_PG_URL. The Community one-active-runner cap is a
check-then-insert with no DB-enforced installation-wide unique index, so it is
serialized by a transaction advisory lock on a stable key. This test forces
the race: a third connection holds that lock, both enrols are observed waiting
on it, and only then is it released.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.db.models import Base
from sessionfs.server.services.scope_locks import advisory_key
from tests.pg_helpers import wait_for_lock_waiters
from tests.server.integration.test_loops_api import _hdrs, _make_project, _make_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
    ),
]


@pytest.fixture
async def db_engine():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    from sessionfs.server import post_response

    pending = [t for t in post_response._tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class _Held:
    def __init__(self, conn, trans) -> None:
        self.conn, self.trans = conn, trans

    async def release(self) -> None:
        await self.trans.commit()
        await self.conn.close()


async def _hold(engine, sql: str, params: dict) -> _Held:
    conn = await engine.connect()
    trans = await conn.begin()
    await conn.execute(text(sql), params)
    return _Held(conn, trans)


async def _race(engine, held: _Held, *coros):
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await wait_for_lock_waiters(engine, len(tasks), timeout=30)
    finally:
        await held.release()
    return await asyncio.gather(*tasks)


async def test_concurrent_community_enrol_allows_one_runner(
    client: AsyncClient, db_session: AsyncSession, db_engine, monkeypatch
):
    """[RR-community-pg] Two owners enrolling at once in the Community
    edition serialize on the installation slot: one 200, one 409 runner_limit."""
    from sessionfs.server.routes import review_runners

    monkeypatch.setattr(review_runners, "get_edition", lambda: "community")

    alice, alice_key = await _make_user(db_session, "alice")
    bob, bob_key = await _make_user(db_session, "bob")
    alice_project = await _make_project(db_session, alice)
    bob_project = await _make_project(db_session, bob)

    def enrol(key: str, project) -> None:
        return client.post(
            "/api/v1/me/review-runners",
            headers=_hdrs(key),
            json={"name": "local", "project_ids": [project.id]},
        )

    held = await _hold(
        db_engine,
        "SELECT pg_advisory_xact_lock(:k)",
        {"k": advisory_key("community_runner", "slot")},
    )
    r1, r2 = await _race(
        db_engine,
        held,
        enrol(alice_key, alice_project),
        enrol(bob_key, bob_project),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["details"]["error"] == "runner_limit"
