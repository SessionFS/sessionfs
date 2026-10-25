"""Ticket-watcher coalescing on real PostgreSQL.

The agent delta is ``id > cursor``, so coalescing must REPLACE a row (new id)
rather than update it in place — otherwise a row upgraded after an agent acked
its id stays invisible. SQLite reuses deleted ``INTEGER PRIMARY KEY`` ids, so
the "new id" behaviour is only observable on PostgreSQL's monotonic sequence;
this file proves it there. The rest of the coalescing behaviour (kind ranking,
event-count merge, summary carry-over) is covered on SQLite in
``test_ticket_watchers.py``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, Project, Ticket, TicketNotification, User
from sessionfs.server.services.ticket_watchers import _coalesce

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_coalesce_replace_gets_new_id_visible_after_ack() -> None:
    """Coalescing an already-acked row must allocate a fresh monotonic id, so
    the upgraded ``your_turn`` row surfaces to a delta reader whose cursor has
    already passed the old id."""
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=1)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com")
            db.add(user)
            await db.flush()
            project = Project(
                id=f"proj_{uuid.uuid4().hex[:16]}",
                name="pg-coalesce",
                owner_id=user.id,
            )
            db.add(project)
            await db.flush()
            ticket = Ticket(
                id=f"tk_{uuid.uuid4().hex[:16]}",
                project_id=project.id,
                title="pg coalesce",
                created_by_user_id=user.id,
            )
            db.add(ticket)
            await db.flush()

            original = TicketNotification(
                user_id=user.id,
                ticket_id=ticket.id,
                project_id=project.id,
                kind="comment",
                summary="first",
            )
            db.add(original)
            await db.commit()
            await db.refresh(original)
            first_id = original.id
            created_at = original.created_at

            now = datetime.now(timezone.utc).replace(microsecond=0)
            await _coalesce(
                db, original, "your_turn", "merged", now,
                actor_type="user", actor_user_id=original.actor_user_id, service_key_name=None,
            )
            await db.commit()

            rows = (
                await db.execute(
                    select(TicketNotification).where(
                        TicketNotification.user_id == user.id
                    )
                )
            ).scalars().all()

            assert [r.kind for r in rows] == ["your_turn"]
            assert rows[0].summary == "merged"
            assert rows[0].event_count == 2
            assert rows[0].id > first_id  # a replacement, not an in-place update
            assert rows[0].created_at == created_at  # kept from the original
            assert abs((rows[0].updated_at - now).total_seconds()) < 1

            # A delta reader whose cursor already passed the old id still sees
            # the upgrade, because the replacement got a fresh monotonic id.
            visible = (
                await db.execute(
                    select(TicketNotification.id).where(
                        TicketNotification.user_id == user.id,
                        TicketNotification.id > first_id,
                    )
                )
            ).scalars().all()
            assert visible == [rows[0].id]
    finally:
        await engine.dispose()
