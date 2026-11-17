"""deferred_jobs on PostgreSQL (tk_e3eecad8c3874d92): the upsert's ON CONFLICT
branch (generation bump, claimed rows keep their lease) and the claim/complete
protocol run on the real dialect."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, DeferredJob
from sessionfs.server.services import deferred_jobs

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"),
]


@pytest.fixture
async def factory():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=5)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _row(factory) -> DeferredJob | None:
    async with factory() as db:
        return (await db.execute(select(DeferredJob))).scalar_one_or_none()


async def test_upsert_and_protocol_on_postgres(factory, monkeypatch) -> None:
    kind = deferred_jobs.KIND_MERGE_RECHECK
    runs: list[str] = []

    async def handler(subject_id, f, context):
        runs.append(subject_id)
        if len(runs) == 1:
            async with f() as db:
                await deferred_jobs.request_job(db, kind, subject_id)
                await db.commit()
            row = await _row(f)
            # Claimed rows keep their lease when re-requested.
            assert row.generation == 3 and row.claim_token is not None
            assert row.available_at is not None
            assert row.available_at > datetime.now(timezone.utc)

    monkeypatch.setitem(deferred_jobs._HANDLERS, kind, handler)
    async with factory() as db:
        await deferred_jobs.request_job(db, kind, "tk_pg")
        await db.commit()
        await deferred_jobs.request_job(db, kind, "tk_pg")
        await db.commit()
    row = await _row(factory)
    assert row.generation == 2 and row.available_at is None and row.attempts == 0

    # The re-request made mid-run runs again within the same call.
    assert await deferred_jobs.run_job(kind, "tk_pg", session_factory=factory) == "done"
    assert runs == ["tk_pg", "tk_pg"]
    assert await _row(factory) is None


async def test_checkpoint_is_fenced_on_postgres(factory, monkeypatch) -> None:
    kind = deferred_jobs.KIND_MERGE_ORG_RECHECK
    outcomes: list[bool] = []

    async def handler(subject_id, f, context):
        claim = context["claim"]
        outcomes.append(await deferred_jobs.checkpoint(f, claim, "tk_100"))
        if len(outcomes) == 1:
            async with f() as db:  # a newer request lands
                await deferred_jobs.request_job(db, kind, subject_id)
                await db.commit()
            outcomes.append(await deferred_jobs.checkpoint(f, claim, "tk_200"))

    monkeypatch.setitem(deferred_jobs._HANDLERS, kind, handler)
    async with factory() as db:
        await deferred_jobs.request_job(db, kind, "org_pg", payload='{"a":1}')
        await db.commit()
    row = await _row(factory)
    assert row.payload == '{"a":1}'
    await deferred_jobs.run_job(kind, "org_pg", session_factory=factory)
    # First checkpoint lands; after the re-request the stale run's checkpoint
    # is refused, and the re-run (from no cursor) checkpoints again.
    assert outcomes == [True, False, True]
    assert await _row(factory) is None
