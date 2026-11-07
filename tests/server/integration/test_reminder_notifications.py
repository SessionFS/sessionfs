"""Reminder notifications through the generalised ticket_notifications table.

The inbox, unread count and digest email handle ``subject_kind='reminder'``
rows next to ticket rows; access is re-checked through the reminder (owner /
creator + the live source); the agent delta stays tickets-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgMember,
    Reminder,
    TicketNotification,
    UserNotificationSettings,
)
from sessionfs.server.services.reminders import reminder_notification
from tests.server.integration.test_ticket_watchers import (
    _RecordingProvider,
    _backdate_updated_at,
    _comment,
    _create_ticket,
    _hdrs,
    _make_member,
    _make_org,
    _make_project,
    _make_user,
    _platform_admin_headers,
)

pytestmark = pytest.mark.asyncio


async def _reminder(
    db: AsyncSession,
    owner_id: str,
    *,
    creator_id: str | None = None,
    title: str = "Book follow-up",
    source: tuple[str, str] | None = None,
    project_id: str | None = None,
    notify: bool = True,
) -> Reminder:
    now = datetime.now(timezone.utc)
    r = Reminder(
        id=f"rem_{uuid.uuid4().hex[:24]}",
        owner_user_id=owner_id,
        created_by_user_id=creator_id or owner_id,
        created_via="manual",
        title=title,
        due_at=now - timedelta(minutes=1),
        original_due_at=now - timedelta(minutes=1),
        source_kind=source[0] if source else None,
        source_id=source[1] if source else None,
        project_id=project_id,
        notified_at=now if notify else None,
    )
    db.add(r)
    await db.flush()
    if notify:
        db.add(reminder_notification(r, now))
    await db.commit()
    return r


async def test_inbox_lists_reminder_and_ticket_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, member_key, "member joins")
    await _comment(client, project, ticket_id, owner_key, "owner replies")
    rem = await _reminder(db_session, member.id, title="Chase vendor")

    resp = await client.get("/api/v1/me/ticket-notifications", headers=_hdrs(member_key))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    kinds = {(i["subject_kind"], i["subject"]["kind"]) for i in items}
    assert kinds == {("ticket", "ticket"), ("reminder", "reminder")}
    rem_item = next(i for i in items if i["subject_kind"] == "reminder")
    assert rem_item["subject"]["id"] == rem.id
    assert rem_item["subject"]["title"] == "Chase vendor"
    assert rem_item["subject"]["due_at"] is not None
    assert rem_item["kind"] == "reminder_due"
    assert rem_item["ticket_id"] is None
    ticket_item = next(i for i in items if i["subject_kind"] == "ticket")
    assert ticket_item["ticket_id"] == ticket_id
    assert ticket_item["subject"] == {
        "kind": "ticket", "id": ticket_id, "title": ticket_item["ticket_title"], "due_at": None,
    }

    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(member_key)
    )
    assert count.json()["count"] == 2

    # Marking the reminder row read lowers the count and changes the ETag.
    etag = count.headers["ETag"]
    marked = await client.post(
        "/api/v1/me/ticket-notifications/read",
        headers=_hdrs(member_key),
        json={"ids": [rem_item["id"]]},
    )
    assert marked.json()["updated"] == 1
    count2 = await client.get(
        "/api/v1/me/ticket-notifications/unread-count",
        headers={**_hdrs(member_key), "If-None-Match": etag},
    )
    assert count2.status_code == 200
    assert count2.json()["count"] == 1


async def test_agent_delta_stays_tickets_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, member_key, "member joins")
    await _comment(client, project, ticket_id, owner_key, "owner replies")
    await _reminder(db_session, member.id)
    await _backdate_updated_at(db_session, member.id)

    resp = await client.get("/api/v1/me/ticket-updates", headers=_hdrs(member_key))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["ticket_id"] == ticket_id
    assert items[0]["subject_kind"] == "ticket"


async def test_reminder_row_hidden_when_owner_loses_source(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An owner who did not create the reminder stops seeing its notification
    once they can no longer read the source; a self-made reminder stays."""
    creator, creator_key = await _make_user(db_session, "creator")
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    project = await _make_project(db_session, creator, org)
    await _make_member(db_session, org, creator, role="admin")
    await _make_member(db_session, org, owner)
    ticket_id = await _create_ticket(client, project, creator_key)
    await _reminder(
        db_session, owner.id, creator_id=creator.id, source=("ticket", ticket_id),
        project_id=project.id, title="For you",
    )
    await _reminder(
        db_session, owner.id, source=("ticket", ticket_id), project_id=project.id,
        title="Mine",
    )
    listing = await client.get("/api/v1/me/ticket-notifications", headers=_hdrs(owner_key))
    assert {i["subject"]["title"] for i in listing.json()["items"]} == {"For you", "Mine"}

    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == owner.id))
    await db_session.commit()

    listing = await client.get("/api/v1/me/ticket-notifications", headers=_hdrs(owner_key))
    assert [i["subject"]["title"] for i in listing.json()["items"]] == ["Mine"]
    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(owner_key)
    )
    assert count.json()["count"] == 1


async def test_digest_includes_reminder_due_in_your_turn_mode(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``your_turn`` mode: a due reminder alone triggers the digest,
    and reminder rows don't wait for the coalescing delay."""
    user, _ = await _make_user(db_session, "solo")
    rem = await _reminder(db_session, user.id, title="Renew the cert")
    _, admin_headers = await _platform_admin_headers(db_session)
    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    assert sweep.json()["users_emailed"] == 1
    assert len(recorder.sends) == 1
    sent = recorder.sends[0]
    assert sent["to"] == user.email
    (item,) = sent["notifications"]
    assert item["subject_kind"] == "reminder"
    assert item["reminder_id"] == rem.id
    assert item["ticket_title"] == "Renew the cert"
    assert item["kind"] == "reminder_due"

    # Idempotent: the row is claimed, a second sweep sends nothing.
    recorder.sends.clear()
    again = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert again.status_code == 200
    assert recorder.sends == []


async def test_digest_off_mode_and_closed_reminder_are_not_emailed(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet, _ = await _make_user(db_session, "quiet")
    await _reminder(db_session, quiet.id)
    db_session.add(UserNotificationSettings(user_id=quiet.id, ticket_email="off"))
    closer, _ = await _make_user(db_session, "closer")
    closed = await _reminder(db_session, closer.id)
    closed.status = "done"
    await db_session.commit()

    _, admin_headers = await _platform_admin_headers(db_session)
    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    assert recorder.sends == []
    rows = (
        await db_session.execute(
            select(TicketNotification.emailed_at).where(
                TicketNotification.user_id.in_([quiet.id, closer.id])
            )
        )
    ).scalars().all()
    # Both consumed so they never resurface.
    assert len(rows) == 2 and all(r is not None for r in rows)


async def test_digest_mixes_ticket_and_reminder_rows(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, member_key, "member joins")
    await _comment(client, project, ticket_id, owner_key, "owner replies")
    await _backdate_updated_at(db_session, member.id)
    await _reminder(db_session, member.id)

    _, admin_headers = await _platform_admin_headers(db_session)
    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    (sent,) = [s for s in recorder.sends if s["to"] == member.email]
    assert {n["subject_kind"] for n in sent["notifications"]} == {"ticket", "reminder"}


async def test_email_template_links_reminders() -> None:
    from sessionfs.server.email_templates import (
        ticket_notification_email,
        ticket_notification_text,
    )

    items = [
        {
            "subject_kind": "reminder", "ticket_title": "Renew <cert>", "ticket_id": None,
            "project_id": None, "reminder_id": "rem_abc", "kind": "reminder_due",
            "summary": "Reminder due",
        },
        {
            "ticket_title": "Fix", "ticket_id": "tk_1", "project_id": "proj_1",
            "kind": "comment", "summary": "New comment",
        },
    ]
    html = ticket_notification_email(notifications=items, dashboard_url="https://app.x/")
    assert "https://app.x/reminders?reminder=rem_abc" in html
    assert "View reminder" in html and "Reminder due" in html
    assert "Renew &lt;cert&gt;" in html
    assert "https://app.x/projects/proj_1?tab=work&sub=tickets&ticket=tk_1" in html
    text = ticket_notification_text(notifications=items, dashboard_url="https://app.x")
    assert "https://app.x/reminders?reminder=rem_abc" in text
