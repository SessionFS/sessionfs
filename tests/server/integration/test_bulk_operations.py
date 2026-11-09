"""All-or-nothing bulk routes: org invites, group member grants and meeting
participant resolution.

Each bulk route takes at most 100 items, refuses duplicates, authorizes like
its single route, writes nothing when any item fails (422 with a per-item
result list) and returns a per-item result list on success.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    MeetingParticipant,
    MeetingTranscript,
    OrgAuditEvent,
    OrgInvite,
    Organization,
    ProjectGroupMember,
    User,
)
from tests.server.integration.test_meetings_api import (
    _group,
    _meeting_participant,
    _member,
    _org,
    _persona,
    _project,
    _service_key,
    _user,
    _user_headers,
)
from tests.server.integration.test_org_invite_email import RecordingProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _admin_org(db: AsyncSession, *, seats: int = 20) -> tuple[Organization, User, dict]:
    org = await _org(db)
    org.seats_limit = seats
    await db.commit()
    admin = await _user(db, "Admin")
    await _member(db, org, admin, role="admin")
    return org, admin, await _user_headers(db, admin)


async def _invite_rows(db: AsyncSession, org_id: str) -> list[OrgInvite]:
    return list(
        (await db.execute(select(OrgInvite).where(OrgInvite.org_id == org_id))).scalars()
    )


def _error(resp) -> dict:
    return resp.json()["error"]


# ── Bulk org invites ──


async def test_bulk_invite_success_without_email_returns_accept_links(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, _admin, hdr = await _admin_org(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/invites/bulk",
        headers=hdr,
        json={
            "invites": [
                {"email": " One@Example.com "},
                {"email": "two@example.com", "role": "admin"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["invited"] == 2
    assert [r["email"] for r in body["results"]] == ["one@example.com", "two@example.com"]
    assert all(r["ok"] and r["invite_id"] for r in body["results"])
    # No email provider on the test app: nothing sent, accept links returned.
    assert all(r["email_sent"] is False for r in body["results"])
    assert all(
        r["accept_url"].endswith(f"/invites?highlight={r['invite_id']}")
        for r in body["results"]
    )
    rows = {r.email: r for r in await _invite_rows(db_session, org.id)}
    assert rows["two@example.com"].role == "admin"
    assert len(rows) == 2


async def test_bulk_invite_sends_emails_after_commit(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    rec = RecordingProvider()
    client._transport.app.state.email_service = rec  # type: ignore[attr-defined]
    org, _admin, hdr = await _admin_org(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/invites/bulk",
        headers=hdr,
        json={"invites": [{"email": "a@example.com"}, {"email": "b@example.com"}]},
    )
    assert resp.status_code == 200, resp.text
    assert all(r["email_sent"] for r in resp.json()["results"])
    assert sorted(m["to"] for m in rec.sent) == ["a@example.com", "b@example.com"]


async def test_bulk_invite_one_bad_item_rolls_back_all(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, admin, hdr = await _admin_org(db_session)
    member = await _user(db_session, "Member", email="member@example.com")
    await _member(db_session, org, member)
    db_session.add(
        OrgInvite(
            id="inv_active000001",
            org_id=org.id,
            email="pending@example.com",
            role="member",
            invited_by=admin.id,
            created_at=_now(),
            expires_at=_now() + timedelta(days=3),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/invites/bulk",
        headers=hdr,
        json={
            "invites": [
                {"email": "new@example.com"},
                {"email": "member@example.com"},
                {"email": "pending@example.com"},
                {"email": "x@example.com", "role": "owner"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    err = _error(resp)
    assert err["code"] == "bulk_item_failed"
    results = err["details"]["results"]
    assert [r["ok"] for r in results] == [True, False, False, False]
    assert [r["code"] for r in results[1:]] == ["already_member", "invite_active", "invalid_role"]
    assert "Not applied" in results[0]["message"]
    rows = await _invite_rows(db_session, org.id)
    assert [r.email for r in rows] == ["pending@example.com"]


async def test_bulk_invite_limits_duplicates_and_seat_cap(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, _admin, hdr = await _admin_org(db_session, seats=3)
    url = f"/api/v1/orgs/{org.id}/invites/bulk"

    too_many = await client.post(
        url, headers=hdr, json={"invites": [{"email": f"u{i}@example.com"} for i in range(101)]}
    )
    assert too_many.status_code == 422
    assert _error(too_many)["code"] == "too_many_items"

    dup = await client.post(
        url,
        headers=hdr,
        json={"invites": [{"email": "a@example.com"}, {"email": "A@example.com "}]},
    )
    assert dup.status_code == 422
    assert _error(dup)["code"] == "duplicate_items"
    assert _error(dup)["details"]["duplicates"] == ["a@example.com"]

    unknown = await client.post(url, headers=hdr, json={"invites": [{"email": "a@x.io", "x": 1}]})
    assert unknown.status_code == 422

    # 1 member + 3 invites > 3 seats: refused for the whole batch.
    seats = await client.post(
        url,
        headers=hdr,
        json={"invites": [{"email": f"s{i}@example.com"} for i in range(3)]},
    )
    assert seats.status_code == 403
    assert seats.json()["error"]["details"]["error"] == "seat_limit"
    assert await _invite_rows(db_session, org.id) == []

    ok = await client.post(
        url,
        headers=hdr,
        json={"invites": [{"email": f"s{i}@example.com"} for i in range(2)]},
    )
    assert ok.status_code == 200, ok.text


async def test_bulk_invite_authorization_matches_single_route(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, admin, _hdr = await _admin_org(db_session)
    member = await _user(db_session, "Plain")
    await _member(db_session, org, member)
    member_hdr = await _user_headers(db_session, member)
    body = {"invites": [{"email": "z@example.com"}]}

    single = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite", headers=member_hdr, json={"email": "z@example.com"}
    )
    bulk = await client.post(f"/api/v1/orgs/{org.id}/invites/bulk", headers=member_hdr, json=body)
    assert single.status_code == bulk.status_code == 403

    missing = await client.post("/api/v1/orgs/org_missing/invites/bulk", headers=member_hdr, json=body)
    assert missing.status_code == 404

    svc = await _service_key(db_session, owner=admin, org_id=org.id, scopes=["tickets:write"])
    svc_single = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite", headers=svc, json={"email": "z@example.com"}
    )
    svc_bulk = await client.post(f"/api/v1/orgs/{org.id}/invites/bulk", headers=svc, json=body)
    assert svc_single.status_code == svc_bulk.status_code == 403
    assert svc_bulk.json()["error"]["code"] == "service_key_not_allowed"
    assert await _invite_rows(db_session, org.id) == []


async def test_bulk_invite_idempotency_key_replays(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, _admin, hdr = await _admin_org(db_session)
    url = f"/api/v1/orgs/{org.id}/invites/bulk"
    body = {"invites": [{"email": "idem@example.com"}]}
    keyed = {**hdr, "Idempotency-Key": "bulk-invite-1"}
    first = await client.post(url, headers=keyed, json=body)
    second = await client.post(url, headers=keyed, json=body)
    assert first.status_code == second.status_code == 200
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert first.json() == second.json()
    assert len(await _invite_rows(db_session, org.id)) == 1


async def test_single_invite_reports_email_not_sent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, _admin, hdr = await _admin_org(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite", headers=hdr, json={"email": "solo@example.com"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_sent"] is False
    assert resp.json()["accept_url"].endswith(f"highlight={resp.json()['invite_id']}")


# ── Bulk group member grants ──


async def _group_setup(db: AsyncSession):
    org = await _org(db)
    admin = await _user(db, "GAdmin")
    await _member(db, org, admin, role="admin")
    group = await _group(db, org, admin)
    members = []
    for i in range(3):
        u = await _user(db, f"M{i}")
        await _member(db, org, u)
        members.append(u)
    return org, admin, await _user_headers(db, admin), group, members


async def _grants(db: AsyncSession, group_id: str) -> dict[str, str]:
    rows = (
        await db.execute(
            select(ProjectGroupMember).where(ProjectGroupMember.group_id == group_id)
        )
    ).scalars()
    return {r.user_id: r.role for r in rows}


async def test_bulk_grant_success_and_audit(client: AsyncClient, db_session: AsyncSession) -> None:
    org, _admin, hdr, group, members = await _group_setup(db_session)
    db_session.add(
        ProjectGroupMember(
            group_id=group.id, user_id=members[0].id, role="reader", created_at=_now()
        )
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/bulk",
        headers=hdr,
        json={
            "grants": [
                {"user_id": members[0].id, "role": "editor"},
                {"user_id": members[1].id, "role": "reader"},
                {"user_id": members[2].id, "role": "admin"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["granted"] == 3 and all(r["ok"] for r in body["results"])
    assert {m["user_id"] for m in body["group"]["members"]} == {m.id for m in members}
    assert await _grants(db_session, group.id) == {
        members[0].id: "editor",
        members[1].id: "reader",
        members[2].id: "admin",
    }
    events = (
        await db_session.execute(
            select(func.count()).select_from(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "project_group_member_granted",
            )
        )
    ).scalar_one()
    assert events == 3


async def test_bulk_grant_bad_item_rolls_back_all(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, _admin, hdr, group, members = await _group_setup(db_session)
    outsider = await _user(db_session, "Outsider")
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/bulk",
        headers=hdr,
        json={
            "grants": [
                {"user_id": members[0].id, "role": "editor"},
                {"user_id": outsider.id, "role": "reader"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    results = _error(resp)["details"]["results"]
    assert [r["ok"] for r in results] == [True, False]
    assert results[1]["code"] == "not_org_member"
    assert await _grants(db_session, group.id) == {}


async def test_bulk_grant_limits_duplicates_and_authorization(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org, admin, hdr, group, members = await _group_setup(db_session)
    url = f"/api/v1/orgs/{org.id}/groups/{group.id}/members/bulk"

    many = await client.post(
        url, headers=hdr, json={"grants": [{"user_id": f"u{i}", "role": "reader"} for i in range(101)]}
    )
    assert many.status_code == 422 and _error(many)["code"] == "too_many_items"

    dup = await client.post(
        url,
        headers=hdr,
        json={
            "grants": [
                {"user_id": members[0].id, "role": "reader"},
                {"user_id": members[0].id, "role": "admin"},
            ]
        },
    )
    assert dup.status_code == 422 and _error(dup)["code"] == "duplicate_items"

    body = {"grants": [{"user_id": members[1].id, "role": "reader"}]}
    # A plain member without a grant: same 404 as the single grant.
    plain_hdr = await _user_headers(db_session, members[0])
    single = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{members[1].id}",
        headers=plain_hdr,
        json={"role": "reader"},
    )
    bulk = await client.post(url, headers=plain_hdr, json=body)
    assert single.status_code == bulk.status_code == 404

    # An editor grant has standing but not admin: same 403 as the single grant.
    db_session.add(
        ProjectGroupMember(group_id=group.id, user_id=members[0].id, role="editor", created_at=_now())
    )
    await db_session.commit()
    single = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{members[1].id}",
        headers=plain_hdr,
        json={"role": "reader"},
    )
    bulk = await client.post(url, headers=plain_hdr, json=body)
    assert single.status_code == bulk.status_code == 403

    outsider_hdr = await _user_headers(db_session, await _user(db_session, "Out"))
    assert (await client.post(url, headers=outsider_hdr, json=body)).status_code == 404

    svc = await _service_key(db_session, owner=admin, org_id=org.id, scopes=["tickets:write"])
    svc_resp = await client.post(url, headers=svc, json=body)
    assert svc_resp.status_code == 403
    assert svc_resp.json()["error"]["code"] == "service_key_not_allowed"
    assert members[1].id not in await _grants(db_session, group.id)


# ── Bulk meeting participant resolution ──


async def _meeting_setup(
    db: AsyncSession, client: AsyncClient, hdr: dict, owner: User, *, restrict: bool = False
):
    settings = '{"general": {"restrict_project_visibility": true}}' if restrict else "{}"
    org = await _org(db, settings=settings)
    await _member(db, org, owner, role="owner")
    project = await _project(db, owner, org=org)
    colleague = await _user(db, "Colleague")
    await _member(db, org, colleague)
    await _persona(db, org, "Analyst")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=hdr,
        json={"title": "t", "content": "hello"},
    )
    assert resp.status_code == 200, resp.text
    meeting_id = resp.json()["id"]
    p1 = await _meeting_participant(db, meeting_id, "Ana")
    p2 = await _meeting_participant(db, meeting_id, "Col")
    return org, project, colleague, meeting_id, p1, p2


async def test_bulk_resolve_participants_success(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    _org_row, _project_row, colleague, meeting_id, p1, p2 = await _meeting_setup(
        db_session, client, auth_headers, test_user
    )
    resp = await client.post(
        f"/api/v1/meetings/{meeting_id}/participants/resolve-bulk",
        headers=auth_headers,
        json={
            "participants": [
                {"participant_id": p1.id, "persona_name": "analyst"},
                {"participant_id": p2.id, "user_id": colleague.id},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 2
    assert body["results"][0]["participant"]["persona_name"] == "Analyst"
    assert body["results"][1]["participant"]["user_id"] == colleague.id
    assert all(r["participant"]["resolution_method"] == "manual" for r in body["results"])


async def test_bulk_resolve_bad_item_rolls_back_all(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    _o, _p, colleague, meeting_id, p1, p2 = await _meeting_setup(
        db_session, client, auth_headers, test_user
    )
    stranger = await _user(db_session, "Stranger")
    resp = await client.post(
        f"/api/v1/meetings/{meeting_id}/participants/resolve-bulk",
        headers=auth_headers,
        json={
            "participants": [
                {"participant_id": p1.id, "user_id": colleague.id},
                {"participant_id": p2.id, "user_id": stranger.id},
                {"participant_id": "mp_missing", "user_id": colleague.id},
                {"participant_id": "mp_neither"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    results = _error(resp)["details"]["results"]
    assert [r["ok"] for r in results] == [True, False, False, False]
    assert results[1]["code"] == "participant_not_eligible"
    assert results[2]["code"] == "participant_not_found"
    assert results[3]["code"] == "participant_not_found"
    rows = (
        await db_session.execute(
            select(MeetingParticipant).where(MeetingParticipant.transcript_id == meeting_id)
        )
    ).scalars().all()
    for row in rows:
        await db_session.refresh(row)
        assert row.resolution_method == "unresolved" and row.user_id is None

    # Exactly-one rule on a real participant is a per-item refusal too.
    both = await client.post(
        f"/api/v1/meetings/{meeting_id}/participants/resolve-bulk",
        headers=auth_headers,
        json={"participants": [{"participant_id": p1.id}]},
    )
    assert both.status_code == 422
    assert _error(both)["details"]["results"][0]["code"] == "invalid_target"


async def test_bulk_resolve_limits_and_authorization(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project, colleague, meeting_id, p1, p2 = await _meeting_setup(
        db_session, client, auth_headers, test_user, restrict=True
    )
    url = f"/api/v1/meetings/{meeting_id}/participants/resolve-bulk"
    many = await client.post(
        url,
        headers=auth_headers,
        json={"participants": [{"participant_id": f"mp_{i}", "user_id": "x"} for i in range(101)]},
    )
    assert many.status_code == 422 and _error(many)["code"] == "too_many_items"
    dup = await client.post(
        url,
        headers=auth_headers,
        json={
            "participants": [
                {"participant_id": p1.id, "user_id": colleague.id},
                {"participant_id": p1.id, "persona_name": "Analyst"},
            ]
        },
    )
    assert dup.status_code == 422 and _error(dup)["code"] == "duplicate_items"

    body = {"participants": [{"participant_id": p1.id, "user_id": colleague.id}]}
    # An org member without standing on the project (restricted visibility)
    # gets exactly what the single route gives.
    viewer_hdr = await _user_headers(db_session, colleague)
    single = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{p1.id}",
        headers=viewer_hdr,
        json={"user_id": colleague.id},
    )
    bulk = await client.post(url, headers=viewer_hdr, json=body)
    assert single.status_code == bulk.status_code == 404
    assert single.json() == bulk.json()

    outsider_hdr = await _user_headers(db_session, await _user(db_session, "Out"))
    assert (await client.post(url, headers=outsider_hdr, json=body)).status_code == 404
    # Wrong kind is the same "Meeting not found".
    wrong_kind = await client.post(url + "?kind=chat", headers=auth_headers, json=body)
    assert wrong_kind.status_code == 404

    svc = await _service_key(
        db_session, owner=test_user, org_id=org.id, scopes=["meetings:write"], project_ids=[project.id]
    )
    svc_single = await client.put(
        f"/api/v1/meetings/{meeting_id}/participants/{p1.id}", headers=svc, json={"user_id": colleague.id}
    )
    svc_bulk = await client.post(url, headers=svc, json=body)
    assert svc_single.status_code == svc_bulk.status_code == 403
    assert svc_bulk.json()["error"]["code"] == "service_key_not_allowed"


async def test_bulk_resolve_chat_thread(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org = await _org(db_session)
    await _member(db_session, org, test_user, role="owner")
    project = await _project(db_session, test_user, org=org)
    colleague = await _user(db_session, "Chatter")
    await _member(db_session, org, colleague)
    content = "[2026-11-01 10:00] Chatter: hi"
    chat = MeetingTranscript(
        id=f"meet_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        kind="chat",
        provider="paste",
        channel_name="general",
        title="chat",
        source="chat_export",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        line_count=1,
        dlp_findings=0,
        extraction_status="none",
        created_by_user_id=test_user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db_session.add(chat)
    await db_session.commit()
    p = await _meeting_participant(db_session, chat.id, "Chatter")
    url = f"/api/v1/meetings/{chat.id}/participants/resolve-bulk"
    body = {"participants": [{"participant_id": p.id, "user_id": colleague.id}]}
    assert (await client.post(url + "?kind=meeting", headers=auth_headers, json=body)).status_code == 404
    resp = await client.post(url + "?kind=chat", headers=auth_headers, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["participant"]["user_id"] == colleague.id

