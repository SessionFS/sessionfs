"""The reminders due sweep on real PostgreSQL: two sweeps running at the same
time on separate connections notify each due reminder exactly once (the
rowcount-1 guarded claim on ``notified_at``)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, Reminder, TicketNotification, User
from sessionfs.server.services import reminders as svc

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_concurrent_sweeps_notify_once_on_postgres() -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=4)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with factory() as db:
            user = User(
                id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", is_active=True
            )
            db.add(user)
            await db.flush()
            ids = []
            for _ in range(30):
                r = Reminder(
                    id=svc.new_reminder_id(),
                    owner_user_id=user.id,
                    created_by_user_id=user.id,
                    title="due",
                    due_at=now - timedelta(minutes=1),
                    original_due_at=now - timedelta(minutes=1),
                )
                db.add(r)
                ids.append(r.id)
            await db.commit()

        async def run() -> dict:
            async with factory() as s:
                return await svc.sweep_due_reminders(s, batch_size=7)

        results = await asyncio.gather(run(), run(), run())
        assert sum(r["reminders_notified"] for r in results) == 30

        async with factory() as db:
            rows = (
                await db.execute(
                    select(TicketNotification.subject_id).where(
                        TicketNotification.user_id == user.id
                    )
                )
            ).scalars().all()
            assert sorted(rows) == sorted(ids)
            stamped = (
                await db.execute(
                    select(Reminder.notified_at).where(Reminder.owner_user_id == user.id)
                )
            ).scalars().all()
            assert all(s is not None for s in stamped)
    finally:
        await engine.dispose()


async def test_delete_racing_sweep_leaves_no_orphan_notification() -> None:
    """The sweep has claimed a due reminder and written its notification but
    not committed; the creator deletes the reminder meanwhile. The delete
    waits on the sweep's row lock and then removes the notification too, so
    no orphan ``reminder_due`` row is left behind."""
    from tests.pg_helpers import wait_for_lock_waiters

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=4)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with factory() as db:
            user = User(
                id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", is_active=True
            )
            db.add(user)
            await db.flush()
            r = Reminder(
                id=svc.new_reminder_id(),
                owner_user_id=user.id,
                created_by_user_id=user.id,
                title="due",
                due_at=now - timedelta(minutes=1),
                original_due_at=now - timedelta(minutes=1),
            )
            db.add(r)
            await db.commit()
            reminder_id, user_id = r.id, user.id

        sweep = factory()
        try:
            assert await svc.claim_due_reminder(sweep, reminder_id, now)
            row = (
                await sweep.execute(select(Reminder).where(Reminder.id == reminder_id))
            ).scalar_one()
            sweep.add(svc.reminder_notification(row, now))
            await sweep.flush()

            async def delete() -> None:
                async with factory() as s:
                    await svc.delete_reminder_rows(s, reminder_id)
                    await s.commit()

            deleter = asyncio.ensure_future(delete())
            await wait_for_lock_waiters(engine, task=deleter)
            await sweep.commit()
            await asyncio.wait_for(deleter, 20)
        finally:
            await sweep.close()

        async with factory() as db:
            left = (
                await db.execute(
                    select(TicketNotification.id).where(TicketNotification.user_id == user_id)
                )
            ).scalars().all()
            assert left == []
            assert (
                await db.execute(select(Reminder.id).where(Reminder.id == reminder_id))
            ).scalar_one_or_none() is None
    finally:
        await engine.dispose()
