"""Org daily rescan cap on real PostgreSQL (two sessions).

The reservation locks the org row while it counts the day's rescans and
inserts its own, so a concurrent request waits and then counts it: the cap
cannot be exceeded by requests that all saw the same count.

Only runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database
(row locks are no-ops on SQLite). The schema is dropped and recreated.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, OrgAuditEvent, Organization

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run",
)


@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_the_daily_cap(monkeypatch):
    from sessionfs.server.routes import marketplace_scanning

    monkeypatch.setattr(marketplace_scanning, "ORG_RESCANS_PER_DAY", 3)
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    async with sessions() as db:
        db.add(Organization(id=org_id, name="Corp", slug=f"c-{uuid.uuid4().hex[:6]}", tier="team"))
        await db.flush()
        for _ in range(2):  # two of today's three rescans already used
            db.add(OrgAuditEvent(id=f"oae_{secrets.token_hex(12)}", org_id=org_id,
                                 org_name_snapshot="Corp", event_type="skill_rescan"))
        await db.commit()

    async def reserve(db):
        return await marketplace_scanning.reserve_org_rescan(
            db, org_id, org_name="Corp", actor_user_id=None, actor_email=None,
            actor_role="owner", force=False,
        )

    try:
        async with sessions() as first, sessions() as second:
            held = await reserve(first)  # holds the org row lock, not committed
            assert held is not None
            waiting = asyncio.create_task(reserve(second))
            await asyncio.sleep(0.5)
            assert not waiting.done()  # blocked on the org row lock
            await first.commit()
            assert await asyncio.wait_for(waiting, 10) is None  # sees the committed third
            await second.rollback()
        async with sessions() as db:
            count = (
                await db.execute(
                    select(func.count(OrgAuditEvent.id)).where(
                        OrgAuditEvent.org_id == org_id, OrgAuditEvent.event_type == "skill_rescan"
                    )
                )
            ).scalar_one()
        assert count == 3
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
