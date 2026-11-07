"""PostgreSQL: extraction persists items whose owner is a participant created
in the same run (GitHub #124). Runs only with SFS_TEST_PG_URL.

MeetingParticipant and MeetingActionItem have no relationship(), so the unit
of work does not order their inserts by the owner foreign key. SQLite never
enforced it; PostgreSQL rejected every first extraction with a matched owner.
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.db.models import Base, User
from tests.server.integration.test_meeting_actions_api import (
    _EXTRACT_ONE,
    _headers,
    _install_extract_mocks,
    _meeting,
    _project,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_extract_with_owned_items_on_postgres(
    client: AsyncClient, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(db_session, test_user)
    meeting = await _meeting(db_session, project=project)
    headers = await _headers(db_session, test_user)
    await _install_extract_mocks(monkeypatch, _EXTRACT_ONE)

    for _ in range(3):  # first run and re-runs both persist
        resp = await client.post(f"/api/v1/meetings/{meeting.id}/extract", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = {p["id"] for p in body["participants"]}
        owned = [i for i in body["action_items"] if i["owner_participant_id"]]
        assert owned, body["action_items"]
        assert all(i["owner_participant_id"] in ids for i in owned)
