"""For You remind on PostgreSQL: a double click (two concurrent requests on
two connections) creates exactly one reminder — the duplicate check runs
under the ticket and recipient locks."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, Reminder, TicketDependency
from tests.pg_helpers import reset_pg_schema_async
from tests.server.integration.test_attention_feed import _member, _org, _project, _ticket, _user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_concurrent_reminds_create_one_reminder(tmp_path: Path) -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db
    from sessionfs.server.storage.local import LocalBlobStore

    await reset_pg_schema_async()
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=6)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.state.blob_store = LocalBlobStore(tmp_path / "blobs")

        async with factory() as db:
            me, key = await _user(db, "ola")
            kemi, _ = await _user(db, "kemi")
            org = await _org(db)
            await _member(db, org, me)
            await _member(db, org, kemi)
            p = await _project(db, me, org)
            mine = await _ticket(db, p, me, assigned_user_id=me.id)
            blocker = await _ticket(db, p, kemi, assigned_user_id=kemi.id)
            db.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
            await db.commit()

        headers = {"Authorization": f"Bearer {key}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            feed = (await client.get("/api/v1/me/for-you", headers=headers)).json()
            flow = feed["collaboration"]["projects"][0]["flows"][0]
            body = {"flow_key": flow["flow_key"], "message": "Is it on track?"}
            results = await asyncio.gather(*(
                client.post("/api/v1/me/for-you/collaboration/remind", headers=headers, json=body)
                for _ in range(2)
            ))
        codes = sorted(r.status_code for r in results)
        assert codes == [201, 409], [r.text for r in results]
        async with factory() as db:
            rows = (await db.execute(select(Reminder).where(Reminder.created_by_user_id == me.id))).scalars().all()
            assert len(rows) == 1
    finally:
        await engine.dispose()
