"""Review loop guarantees under real concurrency, on PostgreSQL.

Runs only with SFS_TEST_PG_URL (site docs: /review-loop-guarantees/). Each
race is forced, not hoped for: a third connection holds the lock the two
racers need (the project scope advisory lock, or the row they update), both
racers are observed waiting on it through pg_stat_activity, and only then is
the lock released.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    AgentRun,
    Base,
    Ticket,
    TicketComment,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.services.scope_locks import advisory_key
from tests.pg_helpers import wait_for_lock_waiters
from tests.server.integration.test_loop_workers import (
    _env,
    _finish,
    _item,
    _p,
    _round,
    _start_worker,
)
from tests.server.integration.test_loops_api import _hdrs

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
    ),
]

SHA1 = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
async def db_engine():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    from sessionfs.server.services import jira_sync, merge_checks

    pending = [
        t for t in (*merge_checks._background_tasks, *jira_sync._background_tasks)
        if not t.done()
    ]
    if pending:
        await asyncio.wait(pending, timeout=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class _Held:
    """A lock held by a third connection until ``release``."""

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


async def _hold_project(engine, project_id: str) -> _Held:
    return await _hold(
        engine, "SELECT pg_advisory_xact_lock(:k)", {"k": advisory_key("project", project_id)}
    )


async def _race(engine, held: _Held, *coros):
    """Start every racer, wait until all of them block on ``held``, release."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await wait_for_lock_waiters(engine, len(tasks), timeout=30)
    finally:
        await held.release()
    return await asyncio.gather(*tasks)


async def _impl_runs(db: AsyncSession, ticket_id: str) -> list[AgentRun]:
    return list(
        (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.ticket_id == ticket_id, AgentRun.role == "implementer")
                .order_by(AgentRun.created_at)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _epoch(db: AsyncSession, ticket_id: str) -> int:
    return (
        await db.execute(
            select(Ticket.lease_epoch)
            .where(Ticket.id == ticket_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ── start worker ──


async def test_concurrent_retry_of_a_first_start_opens_one_run(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-start-pg] A start and its retry racing on an OPEN ticket open one
    run and bump the lease epoch once; both get the same run id and epoch."""
    e = await _env(db_session, client)
    before = await _epoch(db_session, e.child_id)
    held = await _hold_project(db_engine, e.project.id)
    r1, r2 = await _race(db_engine, held, _start_worker(client, e), _start_worker(client, e))
    assert r1.status_code == r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert sorted([r1.json()["idempotent"], r2.json()["idempotent"]]) == [False, True]
    assert r1.json()["lease_epoch"] == r2.json()["lease_epoch"] == before + 1
    assert len(await _impl_runs(db_session, e.child_id)) == 1
    assert await _epoch(db_session, e.child_id) == before + 1


async def test_concurrent_takeovers_leave_exactly_one_holder(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-takeover-pg] Two different workers starting at once both succeed in
    turn (each start bumps the epoch); the later start holds the lease and
    only its run can record a head."""
    e = await _env(db_session, client)
    before = await _epoch(db_session, e.child_id)
    held = await _hold_project(db_engine, e.project.id)
    a, b = await _race(
        db_engine, held,
        _start_worker(client, e, key=e.owner_key),
        _start_worker(client, e, key=e.member_key),
    )
    assert a.status_code == b.status_code == 201, (a.text, b.text)
    assert {a.json()["lease_epoch"], b.json()["lease_epoch"]} == {before + 1, before + 2}
    holder, loser = (a, b) if a.json()["lease_epoch"] == before + 2 else (b, a)
    hkey = e.owner_key if holder is a else e.member_key
    lkey = e.member_key if holder is a else e.owner_key
    fenced = await _finish(client, e, loser.json()["run_id"], key=lkey)
    assert fenced.status_code == 409, fenced.text
    assert fenced.json()["error"]["details"]["error"] == "lease_superseded"
    ok = await _finish(client, e, holder.json()["run_id"], key=hkey)
    assert ok.status_code == 200, ok.text


# ── finish ──


async def test_concurrent_identical_finishes_add_usage_once(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-finish-pg] Two identical finishes of one run racing: one applies,
    one replays, usage is added once."""
    e = await _env(db_session, client)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    held = await _hold(
        db_engine, "SELECT id FROM agent_runs WHERE id = :id FOR UPDATE", {"id": run_id}
    )
    usage = {"input_tokens": 300, "output_tokens": 7}
    f1, f2 = await _race(
        db_engine, held,
        _finish(client, e, run_id, usage=usage),
        _finish(client, e, run_id, usage=usage),
    )
    assert f1.status_code == f2.status_code == 200, (f1.text, f2.text)
    assert sorted([f1.json()["idempotent"], f2.json()["idempotent"]]) == [False, True]
    run = (await _impl_runs(db_session, e.child_id))[0]
    assert (run.input_tokens, run.output_tokens) == (300, 7)


async def test_concurrent_finishes_with_different_heads_one_wins(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-finish-conflict-pg] Two finishes of one run with different heads:
    exactly one lands, the other gets 409 head_sha_conflict."""
    e = await _env(db_session, client)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    held = await _hold(
        db_engine, "SELECT id FROM agent_runs WHERE id = :id FOR UPDATE", {"id": run_id}
    )
    f1, f2 = await _race(
        db_engine, held,
        _finish(client, e, run_id, head_sha=SHA1),
        _finish(client, e, run_id, head_sha=SHA2),
    )
    assert sorted([f1.status_code, f2.status_code]) == [200, 409], (f1.text, f2.text)
    loser = f1 if f1.status_code == 409 else f2
    assert loser.json()["error"]["details"]["error"] == "head_sha_conflict"


# ── review rounds ──


async def test_concurrent_identical_rounds_record_one_round(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-round-pg] The same round posted twice at once (two pods, one
    reviewer credential): one reviewer run, one verdict, both answers carry
    the same run id."""
    e = await _env(db_session, client)
    held = await _hold_project(db_engine, e.project.id)
    r1, r2 = await _race(db_engine, held, _round(client, e), _round(client, e))
    assert r1.status_code == r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert sorted([r1.json()["idempotent"], r2.json()["idempotent"]]) == [False, True]
    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == e.child_id)
        )
    ).scalars().all()
    assert len([c for c in comments if "R1 review" in c.content]) == 1


async def test_concurrent_rounds_with_one_number_from_two_reviewers(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-round-taken-pg] Two different reviewers claiming one round number
    at once: one records it, the other gets 409 review_round_taken."""
    e = await _env(db_session, client)
    held = await _hold_project(db_engine, e.project.id)
    r1, r2 = await _race(
        db_engine, held,
        _round(client, e, key=e.reviewer_key),
        _round(client, e, key=e.member_key),
    )
    assert sorted([r1.status_code, r2.status_code]) == [201, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["details"]["error"] == "review_round_taken"


# ── close item ──


async def test_concurrent_human_closes_settle_once(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-close-pg] Two people closing one parked item at once: one 200, one
    409, one close record."""
    e = await _env(db_session, client, max_review_rounds=1)
    await _round(client, e)
    item = await _item(db_session, e)
    assert item.item_status == "needs_human"
    url = _p(e, f"loops/{e.loop_id}/children/{e.child_id}/close")
    held = await _hold(
        db_engine, "SELECT id FROM work_queue_items WHERE id = :id FOR UPDATE", {"id": item.id}
    )
    c1, c2 = await _race(
        db_engine, held,
        client.post(url, headers=_hdrs(e.owner_key)),
        client.post(url, headers=_hdrs(e.owner_key)),
    )
    assert sorted([c1.status_code, c2.status_code]) == [200, 409], (c1.text, c2.text)
    closes = (
        await db_session.execute(
            select(WorkQueueRun).where(
                WorkQueueRun.work_queue_item_id == item.id,
                WorkQueueRun.outcome == "closed_by_human",
            )
        )
    ).scalars().all()
    assert len(closes) == 1


# ── budget parking ──


async def test_concurrent_spend_parks_the_item_once(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    """[G-budget-pg] A finish and a round that each stay under the token
    budget but together pass it, racing: they serialize on the project lock,
    so the second one sees the first one's committed spend and parks the item
    exactly once (one needs_human record)."""
    e = await _env(db_session, client, token_budget=100)
    run_id = (await _start_worker(client, e, key=e.member_key)).json()["run_id"]
    item = await _item(db_session, e)
    usage = {"input_tokens": 60, "output_tokens": 0}
    held = await _hold_project(db_engine, e.project.id)
    f, r = await _race(
        db_engine, held,
        _finish(client, e, run_id, key=e.member_key, usage=usage),
        _round(client, e, usage=usage),
    )
    assert f.status_code == 200 and r.status_code == 201, (f.text, r.text)
    assert (await _item(db_session, e)).item_status == "needs_human"
    parks = (
        await db_session.execute(
            select(WorkQueueRun).where(
                WorkQueueRun.work_queue_item_id == item.id,
                WorkQueueRun.outcome == "needs_human",
            )
        )
    ).scalars().all()
    assert len(parks) == 1
    s = await _start_worker(client, e, key=e.member_key)
    assert s.status_code == 409
    assert s.json()["error"]["details"]["error"] == "loop_needs_human"


# ── work-queue claim and settle ──


async def test_concurrent_claims_of_one_item_have_one_winner(db_engine):
    """[G-claim-pg] Two wakes claiming one work-queue item at once: the
    guarded UPDATE lets exactly one of them win."""
    from sessionfs.server.services import work_queues as wq
    from tests.server.integration.test_work_queue_settle_verdict import _setup

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        *_rest, item = await _setup(db, registered=False)
        item_id = item.id

    async def claim(run_id: str) -> bool:
        async with factory() as s:
            won = await wq.claim_work_queue_item(s, item_id=item_id, run_id=run_id)
            await s.commit()
            return won

    held = await _hold(
        db_engine, "SELECT id FROM work_queue_items WHERE id = :id FOR UPDATE",
        {"id": item_id},
    )
    results = await _race(db_engine, held, claim("wqr_a"), claim("wqr_b"))
    assert sorted(results) == [False, True], results
    async with factory() as db:
        row = await db.get(WorkQueueItem, item_id)
        assert row.item_status == "active"
        assert row.open_directive_run_id == ("wqr_a" if results[0] else "wqr_b")


async def test_concurrent_settles_of_one_directive_post_one_verdict(db_engine):
    """[G-settle-pg] Two pods settling the same work-queue directive with a
    verdict at once: one settles, the other replays, one verdict comment."""
    from sessionfs.server.services import work_queues as wq
    from tests.server.integration.test_work_queue_settle_verdict import (
        R1_CHANGES,
        _emit_directive,
        _setup,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        org, user, project, ticket, queue, item = await _setup(db, registered=True)
        d = await _emit_directive(db, queue)
        await db.commit()

    async def settle() -> str:
        async with factory() as s:
            q = await s.get(WorkQueue, queue.id)
            res = await wq.complete_work_queue_step(
                s, queue=q, item_id=d["item_id"], directive_id=d["directive_id"],
                ticket_id=ticket.id, outcome="posted_review", comment_id=None,
                agent_run_id=None, failed=False, verdict_content=R1_CHANGES,
                actor_user_id=user.id, actor_org_id=org.id, actor_service_key_id=None,
                actor_type="user", service_key_name=None,
            )
            await s.commit()
            return res.status

    held = await _hold(
        db_engine, "SELECT id FROM work_queue_items WHERE id = :id FOR UPDATE",
        {"id": d["item_id"]},
    )
    results = await _race(db_engine, held, settle(), settle())
    assert sorted(results) == ["idempotent_replay", "settled"], results
    async with factory() as db:
        verdicts = (
            await db.execute(
                select(TicketComment).where(
                    TicketComment.ticket_id == ticket.id,
                    TicketComment.author_persona == "codex-reviewer",
                )
            )
        ).scalars().all()
        assert len(verdicts) == 1
        row = await db.get(WorkQueueItem, d["item_id"])
        assert row.open_directive_id is None
