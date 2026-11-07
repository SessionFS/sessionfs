"""Reminders on meeting items (tk_8a858d6354fc4ef3, R2).

Extraction only SUGGESTS a reminder (``suggested_reminder`` on an item with a
due date); ``POST …/items/{item}/reminder`` creates it (created_via
``meeting``, source = the item and its cited lines). Promoting the item links
its reminders to the new ticket. Group meetings get the same route plus the
reminder listing, behind group visibility.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    MeetingActionItem,
    ProjectGroupMember,
    Reminder,
    User,
)
from tests.server.integration.test_meeting_actions_api import (
    _headers,
    _item,
    _meeting,
    _member,
    _org,
    _org_project_setup,
    _participant,
    _promote,
)
from tests.server.integration.test_meetings_api import _group, _user

pytestmark = pytest.mark.asyncio


async def _dated_item(db: AsyncSession, meeting_id: str, *, days: int = 3, **kw) -> MeetingActionItem:
    item = await _item(db, meeting_id, line_start=2, line_end=3, **kw)
    item.due_date = date.today() + timedelta(days=days)
    await db.commit()
    await db.refresh(item)
    return item


async def _reminders(db: AsyncSession) -> list[Reminder]:
    stmt = select(Reminder).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars())


async def test_detail_exposes_suggestion_and_route_creates_it(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    _, project, headers = await _org_project_setup(db_session, test_user)
    meeting = await _meeting(db_session, project=project)
    owner = await _participant(db_session, meeting.id, "Ada", resolution_method="exact_email")
    owner.user_id = test_user.id
    await db_session.commit()
    item = await _dated_item(
        db_session, meeting.id, description="Book the follow-up", owner_participant_id=owner.id
    )
    undated = await _item(db_session, meeting.id, kind="decision", description="Keep Postgres")
    due_date = item.due_date

    detail = await client.get(f"/api/v1/meetings/{meeting.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    by_id = {i["id"]: i for i in detail.json()["action_items"]}
    suggestion = by_id[item.id]["suggested_reminder"]
    assert suggestion["title"] == "Book the follow-up"
    assert suggestion["due_date"] == due_date.isoformat()
    assert suggestion["lines"] == "2-3"
    assert suggestion["owner_user_id"] == test_user.id
    assert by_id[undated.id]["suggested_reminder"] is None
    # Suggesting never creates a row.
    assert await _reminders(db_session) == []

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/{item.id}/reminder",
        headers=headers,
        json={"timezone": "Europe/London"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_via"] == "meeting"
    assert body["source"]["kind"] == "meeting_item"
    assert body["source"]["id"] == item.id
    assert body["source"]["lines"] == "2-3"
    assert body["timezone"] == "Europe/London"
    [row] = await _reminders(db_session)
    assert row.project_id == project.id and row.owner_user_id == test_user.id

    listing = await client.get(
        f"/api/v1/projects/{project.id}/meetings/{meeting.id}/reminders", headers=headers
    )
    assert [r["id"] for r in listing.json()["items"]] == [body["id"]]


async def test_item_without_due_date_needs_due_at(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    _, project, headers = await _org_project_setup(db_session, test_user)
    meeting = await _meeting(db_session, project=project)
    item = await _item(db_session, meeting.id)
    url = f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/{item.id}/reminder"
    resp = await client.post(url, headers=headers)
    assert resp.status_code == 422
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    resp = await client.post(url, headers=headers, json={"due_at": due, "title": "Chase"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "Chase"


async def test_every_miss_is_the_same_404(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    _, project, headers = await _org_project_setup(db_session, test_user)
    _, other_project, _ = await _org_project_setup(db_session, await _user(db_session, "zed"))
    meeting = await _meeting(db_session, project=project)
    item = await _dated_item(db_session, meeting.id)
    stranger = await _user(db_session, "eve")
    stranger_headers = await _headers(db_session, stranger)
    cases = [
        (headers, f"/api/v1/projects/{other_project.id}/meetings/{meeting.id}/items/{item.id}/reminder"),
        (headers, f"/api/v1/projects/{project.id}/meetings/meet_nope/items/{item.id}/reminder"),
        (headers, f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/mai_nope/reminder"),
        (stranger_headers, f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/{item.id}/reminder"),
    ]
    bodies = set()
    for hdrs, url in cases:
        resp = await client.post(url, headers=hdrs)
        assert resp.status_code == 404, (url, resp.text)
        bodies.add(resp.text)
    assert len(bodies) == 1
    assert await _reminders(db_session) == []


async def test_promote_links_item_reminders_to_the_ticket(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    _, project, headers = await _org_project_setup(db_session, test_user)
    meeting = await _meeting(db_session, project=project)
    item = await _dated_item(db_session, meeting.id)
    other = await _dated_item(db_session, meeting.id)
    for it in (item, other):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/{it.id}/reminder",
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    item_id = item.id

    promoted = await _promote(client, meeting, item, headers)
    assert promoted.status_code == 200, promoted.text
    ticket_id = promoted.json()["ticket_id"]

    rows = {r.source_id: r for r in await _reminders(db_session)}
    assert rows[item_id].linked_ticket_id == ticket_id
    assert rows[other.id].linked_ticket_id is None

    on_ticket = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/reminders", headers=headers
    )
    assert on_ticket.status_code == 200
    assert [r["source"]["id"] for r in on_ticket.json()["items"]] == [item_id]

    # A reminder added after the promotion is linked straight away.
    late = await client.post(
        f"/api/v1/projects/{project.id}/meetings/{meeting.id}/items/{item_id}/reminder",
        headers=headers,
        json={"title": "Second nudge"},
    )
    assert late.status_code == 201, late.text
    assert late.json()["linked_ticket_id"] == ticket_id


async def test_group_meeting_reminders_follow_group_visibility(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="owner")
    headers = await _headers(db_session, test_user)
    group = await _group(db_session, org, test_user)
    meeting = await _meeting(db_session, group=group)
    item = await _dated_item(db_session, meeting.id)

    create_url = f"/api/v1/groups/{group.id}/meetings/{meeting.id}/items/{item.id}/reminder"
    list_url = f"/api/v1/groups/{group.id}/meetings/{meeting.id}/reminders"
    created = await client.post(create_url, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["source"]["group_id"] == group.id
    listing = await client.get(list_url, headers=headers)
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()["items"]] == [created.json()["id"]]
    assert listing.json()["counts"]["open"] == 1

    # An org member with no group standing: the same 404 as a missing meeting.
    outsider = await _user(db_session, "olu")
    await _member(db_session, org, outsider, role="member")
    out_headers = await _headers(db_session, outsider)
    missing = await client.get(
        f"/api/v1/groups/{group.id}/meetings/meet_missing/reminders", headers=out_headers
    )
    denied = await client.get(list_url, headers=out_headers)
    assert denied.status_code == missing.status_code == 404
    assert denied.text == missing.text
    assert (await client.post(create_url, headers=out_headers)).status_code == 404

    # Given group standing, they see the listing — but not someone else's
    # private reminder.
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=outsider.id, role="reader"))
    await db_session.commit()
    seen = await client.get(list_url, headers=out_headers)
    assert seen.status_code == 200
    assert seen.json()["items"] == []

    # Wrong group in the path is a miss too.
    other_group = await _group(db_session, org, test_user)
    wrong = await client.get(
        f"/api/v1/groups/{other_group.id}/meetings/{meeting.id}/reminders", headers=headers
    )
    assert wrong.status_code == 404


async def test_group_reminders_need_a_groups_plan(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session, tier="team")
    await _member(db_session, org, test_user, role="owner")
    headers = await _headers(db_session, test_user)
    group = await _group(db_session, org, test_user)
    meeting = await _meeting(db_session, group=group)
    org.tier = "free"
    await db_session.commit()
    resp = await client.get(f"/api/v1/groups/{group.id}/meetings/{meeting.id}/reminders", headers=headers)
    assert resp.status_code in (403, 404)
