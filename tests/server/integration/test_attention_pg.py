"""For You on real PostgreSQL: every provider's SQL runs on PG, each
provider on its own pooled connection, and the feed meets its latency
budget on a seeded dataset (p95 target 300 ms; asserted generously)."""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base
from sessionfs.server.services import attention as svc
from tests.server.integration.test_attention_feed import _seed_everything, _user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_feed_on_postgres() -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=4)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            me, _ = await _user(db)
            await _seed_everything(db, me, 15)
            timings = []
            for _ in range(10):
                started = time.perf_counter()
                feed = await svc.build_feed(db, svc.AttentionScope(user_id=me.id))
                timings.append(time.perf_counter() - started)
            kinds = {i.kind for i in feed.items}
            assert {
                "ticket", "reminder", "meeting_action", "knowledge_changes", "proof",
                "loop_harmful_findings", "loop_checklist",
            } <= kinds
            assert all(sec["error"] is None for sec in feed.sections.values()), feed.sections
            p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
            assert p95 < 1.0, timings
    finally:
        await engine.dispose()
