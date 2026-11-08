"""PostgreSQL: extraction suggests an existing ticket and Link ties the item
to it (0.29.11). Runs only with SFS_TEST_PG_URL.

Exercises the shortlist SQL (substr, coalesce on timestamps, the readable-
project subquery), the guarded pending → linked UPDATE, the comment insert,
the ticket event and the reminder re-link in one PostgreSQL transaction,
then unlink and a re-extract that keeps the linked item.
"""

from __future__ import annotations

import json
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import sessionfs.server.services.meeting_extraction as extraction_mod
from sessionfs.server.db.models import (
    Base,
    MeetingActionItem,
    ProjectGroupAccess,
    TicketComment,
    TicketEvent,
    User,
)
from tests.server.integration.test_meeting_actions_api import (
    _fake_resolve,
    _headers,
    _meeting,
    _member,
    _org,
    _project,
)
from tests.server.integration.test_meeting_ticket_links import (
    TRANSCRIPT,
    _capture_stream,
    _ticket,
)
from tests.server.integration.test_meetings_api import _group

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


def _mock(monkeypatch, items: list[dict]) -> None:
    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        extraction_mod,
        "stream_chat",
        _capture_stream(json.dumps({"participants": [], "items": items}), {}),
    )


async def test_suggest_link_unlink_on_postgres(
    client: AsyncClient, db_session: AsyncSession, test_user: User, monkeypatch
) -> None:
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    headers = await _headers(db_session, test_user)
    ticket = await _ticket(db_session, project, "Login redirect loop on Safari")
    meeting = await _meeting(db_session, project=project, content=TRANSCRIPT)

    _mock(monkeypatch, [
        {"kind": "action", "description": "Bob fixes it.", "line_start": 2, "line_end": 2,
         "existing_ticket_id": ticket.id},
        {"kind": "action", "description": "Fix the Safari login redirect loop.",
         "line_start": 1, "line_end": 2},
    ])
    resp = await client.post(f"/api/v1/meetings/{meeting.id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["action_items"]
    assert sorted(i["suggestion_source"] for i in items) == ["model", "text"]
    model_item = next(i for i in items if i["suggestion_source"] == "model")

    link = await client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{model_item['id']}/link", headers=headers
    )
    assert link.status_code == 200, link.text
    assert link.json()["item"]["linked_ticket"]["id"] == ticket.id
    comment = (await db_session.execute(
        select(TicketComment).where(TicketComment.ticket_id == ticket.id)
    )).scalar_one()
    assert comment.content.startswith('Raised again in meeting "Planning"')
    assert (await db_session.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.action == "conversation_linked"
        )
    )).scalar_one() is not None

    # A re-extract keeps the linked item and replaces the pending one.
    _mock(monkeypatch, [
        {"kind": "action", "description": "Something new.", "line_start": 3, "line_end": 3},
    ])
    again = await client.post(f"/api/v1/meetings/{meeting.id}/extract", headers=headers)
    assert again.status_code == 200, again.text
    assert sorted(i["status"] for i in again.json()["action_items"]) == ["linked", "pending"]

    unlink = await client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{model_item['id']}/unlink", headers=headers
    )
    assert unlink.status_code == 200, unlink.text
    row = (await db_session.execute(
        select(MeetingActionItem)
        .where(MeetingActionItem.id == model_item["id"])
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert (row.status, row.linked_ticket_id) == ("pending", None)


async def test_group_link_on_postgres(
    client: AsyncClient, db_session: AsyncSession, test_user: User, monkeypatch
) -> None:
    org = await _org(db_session, tier="enterprise")
    await _member(db_session, org, test_user, role="owner")
    headers = await _headers(db_session, test_user)
    group = await _group(db_session, org, test_user)
    in_group = await _project(db_session, test_user, org=org)
    db_session.add(ProjectGroupAccess(group_id=group.id, project_id=in_group.id))
    await db_session.commit()
    ticket = await _ticket(db_session, in_group, "Login redirect loop on Safari")
    meeting = await _meeting(db_session, group=group, content=TRANSCRIPT)

    _mock(monkeypatch, [
        {"kind": "action", "description": "Bob fixes it.", "line_start": 2, "line_end": 2,
         "existing_ticket_id": ticket.id},
    ])
    resp = await client.post(f"/api/v1/meetings/{meeting.id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    item = resp.json()["action_items"][0]
    assert item["suggested_ticket"]["id"] == ticket.id

    link = await client.post(
        f"/api/v1/meetings/{meeting.id}/action-items/{item['id']}/link", headers=headers
    )
    assert link.status_code == 200, link.text
    assert link.json()["item"]["linked_ticket"]["project_id"] == in_group.id
