"""Approval requests and decisions on real PostgreSQL: concurrent triggers
(``raise_ticket_approvals``) converge on one request (insert-or-load on the unique key), concurrent
approvals settle quorum once under the request row lock, and a concurrent
double vote by one person records one decision."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import ApprovalDecision, ApprovalRequest, Base, Ticket, User
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    HEAD,
    make_policy,
    make_world,
    raised_readiness,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_concurrency_on_postgres() -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=6)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            w = await make_world(db)
            await make_policy(db, w.org, requirements=[
                {"who": "org_role", "role": "admin", "count": 2}])

        async def read() -> str:
            async with factory() as s:
                t = await s.get(Ticket, w.ticket.id)
                r = await raised_readiness(s, t, HEAD)
                await s.commit()
                return r["approvals"][0]["request_id"]

        ids = await asyncio.gather(*(read() for _ in range(5)))
        assert len(set(ids)) == 1
        rid = ids[0]

        async def vote(user_id: str) -> str:
            async with factory() as s:
                user = await s.get(User, user_id)
                try:
                    req = await svc.decide(s, request_id=rid, approver=user,
                                           decision="approve", reason="")
                except HTTPException as exc:
                    await s.rollback()
                    return f"refused:{exc.detail['code']}"
                await s.commit()
                return req.state

        # Same person twice at once: one decision, one refusal.
        same = await asyncio.gather(vote(w.admin.id), vote(w.admin.id))
        assert sorted(same) == ["pending", "refused:already_decided"]
        # Two more admins at once: exactly one of them completes quorum.
        results = await asyncio.gather(vote(w.admin2.id), vote(w.owner.id))
        assert "approved" in results

        async with factory() as db:
            req = await db.get(ApprovalRequest, rid)
            assert req.state == "approved"
            rows = (await db.execute(
                select(ApprovalDecision).where(ApprovalDecision.request_id == rid)
            )).scalars().all()
            assert len({d.approver_user_id for d in rows}) == len(rows)
    finally:
        await engine.dispose()


async def test_concurrent_reraise_after_expiry_on_postgres() -> None:
    """An expired request frees its key (the unique index covers PENDING rows
    only): concurrent triggers raise exactly one fresh request through
    the partial-index ON CONFLICT."""
    from datetime import datetime, timedelta, timezone

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=6)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            w = await make_world(db)
            await make_policy(db, w.org)
            first = await raised_readiness(db, w.ticket, HEAD)
            await db.commit()
            old_id = first["approvals"][0]["request_id"]
            req = await db.get(ApprovalRequest, old_id)
            req.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()

        async def read() -> str:
            async with factory() as s:
                t = await s.get(Ticket, w.ticket.id)
                r = await raised_readiness(s, t, HEAD)
                await s.commit()
                return r["approvals"][0]["request_id"]

        ids = await asyncio.gather(*(read() for _ in range(5)))
        assert len(set(ids)) == 1 and ids[0] != old_id
        async with factory() as db:
            states = sorted(
                (
                    await db.execute(
                        select(ApprovalRequest.state).where(
                            ApprovalRequest.target_id == w.ticket.id
                        )
                    )
                ).scalars().all()
            )
            assert states == ["expired", "pending"]
    finally:
        await engine.dispose()
