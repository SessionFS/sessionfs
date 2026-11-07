"""Approval notifications (approval policies A2) through the generalised
notification table: approval_requested goes to every eligible approver,
approval_decided to the requester side, and eligibility is re-checked on
every inbox / unread-count / digest read. The agent delta stays tickets-only."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, TicketNotification
from sessionfs.server.email_templates import ticket_notification_email
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    HEAD,
    hdrs,
    make_policy,
    make_world,
)
from tests.server.integration.test_ticket_watchers import (
    _RecordingProvider,
    _platform_admin_headers,
)

pytestmark = pytest.mark.asyncio


async def _approval_world(db: AsyncSession):
    w = await make_world(db)
    await make_policy(db, w.org)
    r = await svc.ticket_readiness(db, w.ticket, HEAD)
    await db.commit()
    return w, r["approvals"][0]["request_id"]


async def _inbox(client: AsyncClient, key: str) -> list[dict]:
    resp = await client.get("/api/v1/me/ticket-notifications", headers=hdrs(key))
    assert resp.status_code == 200, resp.text
    return [i for i in resp.json()["items"] if i["subject_kind"] == "approval"]


async def _count(client: AsyncClient, key: str) -> int:
    resp = await client.get("/api/v1/me/ticket-notifications/unread-count", headers=hdrs(key))
    assert resp.status_code == 200, resp.text
    return resp.json()["count"]


async def test_requested_goes_to_eligible_approvers_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, rid = await _approval_world(db_session)
    rows = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.subject_kind == "approval")
        )
    ).scalars().all()
    # Owner and both admins fit "admin"; the implementer and reviewer don't.
    assert {r.user_id for r in rows} == {w.owner.id, w.admin.id, w.admin2.id}
    assert all(r.ticket_id is None and r.subject_id == rid for r in rows)

    (item,) = await _inbox(client, w.admin_key)
    assert item["kind"] == "approval_requested"
    assert item["subject"]["id"] == rid
    assert item["subject"]["title"] == "Auth changes: Auth change"
    assert item["subject"]["state"] == "pending"
    assert item["subject"]["target_kind"] == "merge"
    assert item["project_id"] == w.project.id
    assert await _count(client, w.admin_key) == 1
    assert await _inbox(client, w.implementer_key) == []


async def test_eligibility_rechecked_after_decision_and_lost_membership(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, rid = await _approval_world(db_session)
    await svc.decide(db_session, request_id=rid, approver=w.admin,
                     decision="approve", reason="ok")
    await db_session.commit()

    # The request is no longer open: the other approvers' rows disappear.
    assert await _inbox(client, w.admin2_key) == []
    assert await _count(client, w.admin2_key) == 0
    # The implementer (ticket creator) gets the decision.
    (decided,) = await _inbox(client, w.implementer_key)
    assert decided["kind"] == "approval_decided"
    assert decided["summary"] == "Approval granted"
    assert decided["subject"]["state"] == "approved"

    # Losing project access hides the decided row too.
    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == w.implementer.id))
    await db_session.commit()
    assert await _inbox(client, w.implementer_key) == []
    assert await _count(client, w.implementer_key) == 0


async def test_requested_hidden_when_approver_leaves_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, _rid = await _approval_world(db_session)
    assert len(await _inbox(client, w.admin2_key)) == 1
    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == w.admin2.id))
    await db_session.commit()
    assert await _inbox(client, w.admin2_key) == []
    assert await _count(client, w.admin2_key) == 0


async def test_agent_delta_skips_approval_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, _rid = await _approval_world(db_session)
    resp = await client.get("/api/v1/me/ticket-updates", headers=hdrs(w.admin_key))
    assert resp.status_code == 200, resp.text
    assert [i for i in resp.json()["items"] if i["subject_kind"] == "approval"] == []


async def test_digest_emails_eligible_and_consumes_stale(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, rid = await _approval_world(db_session)
    # admin2 leaves before the sweep: their row is consumed, never emailed.
    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == w.admin2.id))
    await db_session.commit()
    _, admin_headers = await _platform_admin_headers(db_session)
    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    sent_to = {s["to"] for s in recorder.sends}
    assert sent_to == {w.owner.email, w.admin.email}
    item = recorder.sends[0]["notifications"][0]
    assert item["subject_kind"] == "approval" and item["approval_id"] == rid
    assert item["kind"] == "approval_requested"
    stale = (
        await db_session.execute(
            select(TicketNotification).where(
                TicketNotification.user_id == w.admin2.id,
                TicketNotification.subject_kind == "approval",
            )
        )
    ).scalar_one()
    await db_session.refresh(stale)
    assert stale.emailed_at is not None


async def test_email_template_links_approvals() -> None:
    html = ticket_notification_email(
        notifications=[{
            "subject_kind": "approval", "ticket_title": "Auth changes: Auth change",
            "ticket_id": None, "project_id": "proj_1", "approval_id": "ar_1",
            "kind": "approval_requested", "summary": "Approval requested",
        }],
        dashboard_url="https://app.example.com",
    )
    assert "/approvals?request=ar_1" in html
    assert "Approval requested" in html and "View approval" in html
