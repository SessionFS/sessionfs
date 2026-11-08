"""Reminders REST API (tk_8a858d6354fc4ef3, R1).

Covers create/list/get/patch/delete, reminders for someone else (allowed and
refused without an existence leak), source-access loss, the note / open caps,
the write rate limit, due_at bounds, snooze and reschedule clearing
``notified_at``, project meeting/ticket listings, service-key scope and minter
rules, the due sweep (once under concurrent runs) and N+1-free listing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.models import (
    ApiKey,
    MeetingActionItem,
    MeetingTranscript,
    OrgMember,
    Project,
    Reminder,
    TicketNotification,
    User,
)
from sessionfs.server.routes import reminders as reminders_routes
from sessionfs.server.services import reminders as svc
from tests.server.integration.test_ticket_watchers import (
    _create_ticket,
    _hdrs,
    _make_member,
    _make_org,
    _make_project,
    _make_user,
    _platform_admin_headers,
)

pytestmark = pytest.mark.asyncio

NOT_FOUND = {"error": {"code": "404", "message": "Reminder not found", "details": {}}}


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _soon(minutes: int = 60) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(minutes=minutes))


async def _create(client: AsyncClient, key: str, **body) -> dict:
    payload = {"title": "Book a follow-up", "due_at": _soon()}
    payload.update(body)
    resp = await client.post("/api/v1/me/reminders", headers=_hdrs(key), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _team(db: AsyncSession):
    """An org with an admin owner, a member, a project and the member's key."""
    owner, owner_key = await _make_user(db, "lead")
    member, member_key = await _make_user(db, "mate")
    org = await _make_org(db)
    project = await _make_project(db, owner, org)
    await _make_member(db, org, owner, role="admin")
    await _make_member(db, org, member)
    return owner, owner_key, member, member_key, org, project


async def _meeting(db: AsyncSession, project: Project, user: User) -> tuple[str, str]:
    content = "Ada: we should follow up\nTolu: agreed"
    meeting = MeetingTranscript(
        id=f"mtg_{uuid.uuid4().hex[:16]}",
        org_id=project.org_id,
        project_id=project.id,
        title="Weekly sync",
        source="manual_paste",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        line_count=2,
        created_by_user_id=user.id,
    )
    db.add(meeting)
    await db.flush()
    item = MeetingActionItem(
        id=f"mai_{uuid.uuid4().hex[:16]}",
        transcript_id=meeting.id,
        kind="action",
        description="Book the follow-up meeting",
        line_start=1,
        line_end=2,
        status="pending",
        extracted_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    return meeting.id, item.id


async def _service_key(
    db: AsyncSession,
    *,
    owner: User,
    org_id: str,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(raw),
            name="rem-svc",
            created_at=datetime.now(timezone.utc),
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(scopes),
            service_key_name="rem-svc",
            created_by_user_id=owner.id,
            project_ids=json.dumps(project_ids) if project_ids is not None else None,
        )
    )
    await db.commit()
    return raw


# ── CRUD ──


async def test_create_list_get_patch_delete(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session, "solo")
    created = await _create(client, key, title="  Renew   the cert ", timezone="Europe/London")
    assert created["title"] == "Renew the cert"
    assert created["status"] == "open"
    assert created["owner_user_id"] == user.id == created["created_by_user_id"]
    assert created["is_owner"] and created["is_creator"]
    assert created["created_via"] == "manual"
    assert created["source"] is None and created["source_hidden"] is False
    assert created["timezone"] == "Europe/London"
    assert created["id"].startswith("rem_")

    listing = await client.get("/api/v1/me/reminders", headers=_hdrs(key))
    assert [r["id"] for r in listing.json()["items"]] == [created["id"]]

    got = await client.get(f"/api/v1/me/reminders/{created['id']}", headers=_hdrs(key))
    assert got.status_code == 200 and got.json()["note_count"] == 0

    patched = await client.patch(
        f"/api/v1/me/reminders/{created['id']}",
        headers=_hdrs(key),
        json={"title": "Renew the TLS cert", "due_at": _soon(120)},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Renew the TLS cert"
    assert patched.json()["original_due_at"] == created["original_due_at"]

    closed = await client.patch(
        f"/api/v1/me/reminders/{created['id']}", headers=_hdrs(key), json={"status": "done"}
    )
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "done" and body["closed_by_user_id"] == user.id
    # Closed reminders leave the default (open) list and can't close twice.
    assert (await client.get("/api/v1/me/reminders", headers=_hdrs(key))).json()["items"] == []
    done = await client.get("/api/v1/me/reminders?status=done", headers=_hdrs(key))
    assert len(done.json()["items"]) == 1
    again = await client.patch(
        f"/api/v1/me/reminders/{created['id']}", headers=_hdrs(key), json={"status": "dismissed"}
    )
    assert again.status_code == 409
    assert _code(again) == "reminder_not_open"

    deleted = await client.delete(f"/api/v1/me/reminders/{created['id']}", headers=_hdrs(key))
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/me/reminders/{created['id']}", headers=_hdrs(key))
    assert gone.status_code == 404 and gone.json() == NOT_FOUND


async def test_naive_due_at_uses_timezone(client: AsyncClient, db_session: AsyncSession):
    _, key = await _make_user(db_session, "tz")
    local = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0, tzinfo=None
    )
    created = await _create(
        client, key, due_at=local.isoformat(), timezone="America/New_York"
    )
    due = datetime.fromisoformat(created["due_at"].replace("Z", "+00:00"))
    assert due.utcoffset() == timedelta(0)
    assert due.hour in (13, 14)  # 09:00 New York is 13:00 or 14:00 UTC
    bad = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(key),
        json={"title": "x", "due_at": _soon(), "timezone": "Mars/Olympus"},
    )
    assert bad.status_code == 422 and _code(bad) == "invalid_timezone"


async def test_other_users_get_the_same_404(client: AsyncClient, db_session: AsyncSession):
    _, key = await _make_user(db_session, "alice")
    _, other_key = await _make_user(db_session, "eve")
    rem = await _create(client, key)
    for method, path, body in (
        ("get", f"/api/v1/me/reminders/{rem['id']}", None),
        ("patch", f"/api/v1/me/reminders/{rem['id']}", {"status": "done"}),
        ("delete", f"/api/v1/me/reminders/{rem['id']}", None),
        ("post", f"/api/v1/me/reminders/{rem['id']}/snooze", {"preset": "1h"}),
        ("get", f"/api/v1/me/reminders/{rem['id']}/notes", None),
        ("post", f"/api/v1/me/reminders/{rem['id']}/notes", {"body": "hi"}),
    ):
        kwargs = {"headers": _hdrs(other_key)}
        if body is not None:
            kwargs["json"] = body
        resp = await getattr(client, method)(path, **kwargs)
        missing = await getattr(client, method)(
            path.replace(rem["id"], "rem_doesnotexist"), **kwargs
        )
        assert resp.status_code == missing.status_code == 404, (method, path)
        assert resp.json() == missing.json() == NOT_FOUND
    listing = await client.get("/api/v1/me/reminders", headers=_hdrs(other_key))
    assert listing.json()["items"] == []


async def test_due_at_bounds(client: AsyncClient, db_session: AsyncSession):
    _, key = await _make_user(db_session, "bounds")
    now = datetime.now(timezone.utc)
    for due in (now - timedelta(hours=2), now + timedelta(days=731)):
        resp = await client.post(
            "/api/v1/me/reminders", headers=_hdrs(key), json={"title": "x", "due_at": _iso(due)}
        )
        assert resp.status_code == 422
        assert _code(resp) == "due_at_out_of_range"
    # Slightly in the past is fine (a just-missed time).
    await _create(client, key, due_at=_iso(now - timedelta(minutes=30)))
    rem = await _create(client, key)
    resp = await client.patch(
        f"/api/v1/me/reminders/{rem['id']}",
        headers=_hdrs(key),
        json={"due_at": _iso(now + timedelta(days=800))},
    )
    assert resp.status_code == 422


async def test_dlp_redacts_title_and_notes(client: AsyncClient, db_session: AsyncSession):
    _, key = await _make_user(db_session, "dlp")
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    rem = await _create(client, key, title=f"rotate {secret} today")
    assert secret not in rem["title"]
    note = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/notes",
        headers=_hdrs(key),
        json={"body": f"key was {secret}"},
    )
    assert note.status_code == 201, note.text
    assert secret not in note.json()["body"]


# ── Sources and other owners ──


async def test_reminder_for_teammate_on_ticket(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, mate, mate_key, org, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    rem = await _create(
        client, lead_key, owner=mate.email.upper(), source={"kind": "ticket", "id": ticket_id}
    )
    assert rem["owner_user_id"] == mate.id
    assert rem["created_by_user_id"] == lead.id
    assert rem["source"]["kind"] == "ticket"
    assert rem["source"]["project_id"] == project.id
    stored = await db_session.get(Reminder, rem["id"])
    assert stored.org_id == org.id and stored.project_id == project.id

    mine = await client.get("/api/v1/me/reminders", headers=_hdrs(mate_key))
    assert [r["id"] for r in mine.json()["items"]] == [rem["id"]]
    item = mine.json()["items"][0]
    assert item["is_owner"] and not item["is_creator"]

    # Owner can't rename (creator only); creator can't snooze (owner only).
    rename = await client.patch(
        f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(mate_key), json={"title": "x"}
    )
    assert rename.status_code == 403
    snooze = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/snooze", headers=_hdrs(lead_key), json={"preset": "1h"}
    )
    assert snooze.status_code == 403
    # Both can add notes.
    for k in (lead_key, mate_key):
        resp = await client.post(
            f"/api/v1/me/reminders/{rem['id']}/notes", headers=_hdrs(k), json={"body": "noted"}
        )
        assert resp.status_code == 201

    # The ticket listing shows it to both, and to nobody else.
    path = f"/api/v1/projects/{project.id}/tickets/{ticket_id}/reminders"
    for k in (lead_key, mate_key):
        resp = await client.get(path, headers=_hdrs(k))
        assert resp.status_code == 200, resp.text
        assert [r["id"] for r in resp.json()["items"]] == [rem["id"]]
        assert resp.json()["counts"] == {"open": 1, "done": 0, "dismissed": 0}
        assert resp.json()["items"][0]["note_count"] == 2


async def test_owner_not_eligible_has_no_existence_leak(
    client: AsyncClient, db_session: AsyncSession
):
    lead, lead_key, mate, _, org, project = await _team(db_session)
    outsider, _ = await _make_user(db_session, "outsider")
    inactive, _ = await _make_user(db_session, "gone")
    inactive.is_active = False
    await db_session.commit()
    ticket_id = await _create_ticket(client, project, lead_key)
    source = {"kind": "ticket", "id": ticket_id}
    bodies = []
    for owner in (
        outsider.email,  # exists, can't read the source
        f"nobody-{uuid.uuid4().hex[:6]}@example.com",  # doesn't exist
        str(uuid.uuid4()),  # unknown id
        inactive.id,  # inactive
    ):
        resp = await client.post(
            "/api/v1/me/reminders",
            headers=_hdrs(lead_key),
            json={"title": "x", "due_at": _soon(), "owner": owner, "source": source},
        )
        assert resp.status_code == 422
        bodies.append(resp.json())
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["error"]["code"] == "reminder_owner_not_eligible"
    # Without a source a reminder can only be for yourself.
    resp = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(lead_key),
        json={"title": "x", "due_at": _soon(), "owner": mate.email},
    )
    assert resp.status_code == 422 and resp.json() == bodies[0]


async def test_creator_must_read_source(client: AsyncClient, db_session: AsyncSession):
    _, lead_key, _, _, _, project = await _team(db_session)
    _, outsider_key = await _make_user(db_session, "outsider")
    ticket_id = await _create_ticket(client, project, lead_key)
    seen = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(outsider_key),
        json={"title": "x", "due_at": _soon(), "source": {"kind": "ticket", "id": ticket_id}},
    )
    missing = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(outsider_key),
        json={"title": "x", "due_at": _soon(), "source": {"kind": "ticket", "id": "tk_nope"}},
    )
    assert seen.status_code == missing.status_code == 404
    assert seen.json() == missing.json()


async def test_source_access_lost_hides_from_owner_not_creator(
    client: AsyncClient, db_session: AsyncSession
):
    lead, lead_key, mate, mate_key, _, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    for_mate = await _create(
        client, lead_key, owner=mate.id, source={"kind": "ticket", "id": ticket_id}
    )
    own = await _create(client, mate_key, source={"kind": "ticket", "id": ticket_id})

    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == mate.id))
    await db_session.commit()

    # The owner who didn't create it now gets the generic 404.
    resp = await client.get(f"/api/v1/me/reminders/{for_mate['id']}", headers=_hdrs(mate_key))
    assert resp.status_code == 404 and resp.json() == NOT_FOUND
    # Their own reminder stays, with the source link hidden.
    mine = await client.get(f"/api/v1/me/reminders/{own['id']}", headers=_hdrs(mate_key))
    assert mine.status_code == 200
    assert mine.json()["source"] is None and mine.json()["source_hidden"] is True
    listing = await client.get("/api/v1/me/reminders", headers=_hdrs(mate_key))
    assert [r["id"] for r in listing.json()["items"]] == [own["id"]]
    # The creator still sees the reminder they made.
    creator_view = await client.get(
        f"/api/v1/me/reminders/{for_mate['id']}", headers=_hdrs(lead_key)
    )
    assert creator_view.status_code == 200
    assert creator_view.json()["source"]["id"] == ticket_id


async def test_meeting_and_item_sources(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, mate, mate_key, _, project = await _team(db_session)
    meeting_id, item_id = await _meeting(db_session, project, lead)
    on_item = await _create(
        client,
        lead_key,
        owner=mate.id,
        source={"kind": "meeting_item", "id": item_id, "lines": "1-2"},
    )
    assert on_item["source"]["meeting_id"] == meeting_id
    assert on_item["source"]["lines"] == "1-2"
    await _create(client, lead_key, source={"kind": "meeting", "id": meeting_id})
    resp = await client.get(
        f"/api/v1/projects/{project.id}/meetings/{meeting_id}/reminders", headers=_hdrs(lead_key)
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 2
    mate_view = await client.get(
        f"/api/v1/projects/{project.id}/meetings/{meeting_id}/reminders", headers=_hdrs(mate_key)
    )
    assert [r["id"] for r in mate_view.json()["items"]] == [on_item["id"]]
    bad_lines = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(lead_key),
        json={
            "title": "x", "due_at": _soon(),
            "source": {"kind": "meeting", "id": meeting_id, "lines": "a-b"},
        },
    )
    assert bad_lines.status_code == 422

    # Project listings: an outsider (and a wrong project) get the same 404.
    _, outsider_key = await _make_user(db_session, "outsider")
    a = await client.get(
        f"/api/v1/projects/{project.id}/meetings/{meeting_id}/reminders",
        headers=_hdrs(outsider_key),
    )
    b = await client.get(
        f"/api/v1/projects/{project.id}/meetings/mtg_missing/reminders", headers=_hdrs(lead_key)
    )
    assert a.status_code == b.status_code == 404 and a.json() == b.json()


async def test_session_source_is_owner_only(client: AsyncClient, db_session: AsyncSession):
    from sessionfs.server.db.models import Session

    lead, lead_key, mate, _, _, project = await _team(db_session)
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=lead.id,
        title="debugging",
        source_tool="claude-code",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        blob_key="k",
        etag="e1",
    )
    db_session.add(session)
    await db_session.commit()
    rem = await _create(client, lead_key, source={"kind": "session", "id": session.id})
    assert rem["source"]["title"] == "debugging"
    # A teammate can't read the creator's session, so they can't own one.
    resp = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(lead_key),
        json={
            "title": "x", "due_at": _soon(), "owner": mate.id,
            "source": {"kind": "session", "id": session.id},
        },
    )
    assert resp.status_code == 422


# ── State changes ──


async def test_snooze_and_reschedule_clear_notified_at(
    client: AsyncClient, db_session: AsyncSession
):
    _, key = await _make_user(db_session, "snoozer")
    rem = await _create(client, key, timezone="Europe/London")
    for preset, extra in (
        ("1h", {}),
        ("tomorrow", {}),
        ("next_week", {}),
        ("custom", {"until": _soon(600)}),
    ):
        await db_session.execute(
            update(Reminder)
            .where(Reminder.id == rem["id"])
            .values(notified_at=datetime.now(timezone.utc))
        )
        await db_session.commit()
        resp = await client.post(
            f"/api/v1/me/reminders/{rem['id']}/snooze",
            headers=_hdrs(key),
            json={"preset": preset, **extra},
        )
        assert resp.status_code == 200, (preset, resp.text)
        assert resp.json()["notified_at"] is None
        assert datetime.fromisoformat(resp.json()["due_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
    assert resp.json()["snooze_count"] == 4
    tomorrow = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/snooze", headers=_hdrs(key), json={"preset": "tomorrow"}
    )
    due = datetime.fromisoformat(tomorrow.json()["due_at"].replace("Z", "+00:00"))
    from zoneinfo import ZoneInfo

    assert due.astimezone(ZoneInfo("Europe/London")).hour == 9

    await db_session.execute(
        update(Reminder).where(Reminder.id == rem["id"]).values(notified_at=datetime.now(timezone.utc))
    )
    await db_session.commit()
    resched = await client.patch(
        f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(key), json={"due_at": _soon(30)}
    )
    assert resched.status_code == 200 and resched.json()["notified_at"] is None

    custom_past = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/snooze",
        headers=_hdrs(key),
        json={"preset": "custom", "until": _iso(datetime.now(timezone.utc) - timedelta(minutes=5))},
    )
    assert custom_past.status_code == 422

    await client.patch(
        f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(key), json={"status": "dismissed"}
    )
    closed = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/snooze", headers=_hdrs(key), json={"preset": "1h"}
    )
    assert closed.status_code == 409


# ── Limits ──


async def test_open_cap(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    _, key = await _make_user(db_session, "busy")
    monkeypatch.setattr(svc, "MAX_OPEN_PER_USER", 2)
    first = await _create(client, key)
    await _create(client, key)
    resp = await client.post(
        "/api/v1/me/reminders", headers=_hdrs(key), json={"title": "x", "due_at": _soon()}
    )
    assert resp.status_code == 409 and _code(resp) == "reminder_limit_reached"
    # Closing one frees a slot.
    await client.patch(
        f"/api/v1/me/reminders/{first['id']}", headers=_hdrs(key), json={"status": "done"}
    )
    await _create(client, key)


async def test_notes_cap(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    _, key = await _make_user(db_session, "noter")
    monkeypatch.setattr(svc, "MAX_NOTES_PER_REMINDER", 2)
    rem = await _create(client, key)
    path = f"/api/v1/me/reminders/{rem['id']}/notes"
    for i in range(2):
        assert (await client.post(path, headers=_hdrs(key), json={"body": f"n{i}"})).status_code == 201
    resp = await client.post(path, headers=_hdrs(key), json={"body": "n3"})
    assert resp.status_code == 409 and _code(resp) == "reminder_note_limit"
    notes = await client.get(path, headers=_hdrs(key))
    assert [n["body"] for n in notes.json()["items"]] == ["n0", "n1"]
    too_long = await client.post(path, headers=_hdrs(key), json={"body": "x" * 4001})
    assert too_long.status_code == 422


async def test_write_rate_limit(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    _, key = await _make_user(db_session, "spammer")
    monkeypatch.setattr(
        reminders_routes, "_write_limiter", SlidingWindowRateLimiter(max_requests=2)
    )
    await _create(client, key)
    await _create(client, key)
    resp = await client.post(
        "/api/v1/me/reminders", headers=_hdrs(key), json={"title": "x", "due_at": _soon()}
    )
    assert resp.status_code == 429 and _code(resp) == "rate_limited"
    # Reads are not limited.
    assert (await client.get("/api/v1/me/reminders", headers=_hdrs(key))).status_code == 200


# ── Service keys ──


async def test_service_key_acts_as_minter(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, mate, _, org, project = await _team(db_session)
    other_project = await _make_project(db_session, lead, org)
    ticket_id = await _create_ticket(client, project, lead_key)
    other_ticket = await _create_ticket(client, other_project, lead_key)
    svc_key = await _service_key(
        db_session, owner=lead, org_id=org.id,
        scopes=["reminders:read", "reminders:write"], project_ids=[project.id],
    )
    rem = await _create(client, svc_key, source={"kind": "ticket", "id": ticket_id})
    assert rem["owner_user_id"] == lead.id and rem["created_via"] == "api"

    # Only for the minter, never for someone else (same 422 as any miss).
    resp = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(svc_key),
        json={"title": "x", "due_at": _soon(), "owner": mate.id,
              "source": {"kind": "ticket", "id": ticket_id}},
    )
    assert resp.status_code == 422
    assert _code(resp) == "reminder_owner_not_eligible"
    # Sources outside the allowlist look missing; sourceless is refused.
    outside = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(svc_key),
        json={"title": "x", "due_at": _soon(), "source": {"kind": "ticket", "id": other_ticket}},
    )
    assert outside.status_code == 404
    sourceless = await client.post(
        "/api/v1/me/reminders", headers=_hdrs(svc_key), json={"title": "x", "due_at": _soon()}
    )
    assert sourceless.status_code == 422

    # The minter's own reminders outside the key's scope stay invisible to it.
    personal = await _create(client, lead_key)
    elsewhere = await _create(client, lead_key, source={"kind": "ticket", "id": other_ticket})
    listing = await client.get("/api/v1/me/reminders", headers=_hdrs(svc_key))
    assert [r["id"] for r in listing.json()["items"]] == [rem["id"]]
    for rid in (personal["id"], elsewhere["id"]):
        resp = await client.get(f"/api/v1/me/reminders/{rid}", headers=_hdrs(svc_key))
        assert resp.status_code == 404 and resp.json() == NOT_FOUND
    # The listing route maps the service key's cross-scope 403 to the 404.
    resp = await client.get(
        f"/api/v1/projects/{other_project.id}/tickets/{other_ticket}/reminders",
        headers=_hdrs(svc_key),
    )
    assert resp.status_code == 404


async def test_service_key_scopes(client: AsyncClient, db_session: AsyncSession):
    lead, _, _, _, org, _ = await _team(db_session)
    read_only = await _service_key(
        db_session, owner=lead, org_id=org.id, scopes=["reminders:read"]
    )
    no_scope = await _service_key(db_session, owner=lead, org_id=org.id, scopes=["tickets:read"])
    resp = await client.post(
        "/api/v1/me/reminders", headers=_hdrs(read_only), json={"title": "x", "due_at": _soon()}
    )
    assert resp.status_code == 403
    assert (await client.get("/api/v1/me/reminders", headers=_hdrs(read_only))).status_code == 200
    assert (await client.get("/api/v1/me/reminders", headers=_hdrs(no_scope))).status_code == 403


async def test_scopes_are_in_the_catalog() -> None:
    from sessionfs.server.schemas.api_keys import VALID_SCOPES

    assert {"reminders:read", "reminders:write"} <= VALID_SCOPES


# ── Due sweep ──


async def _due(db: AsyncSession, owner: User, *, creator: User | None = None, **extra) -> Reminder:
    now = datetime.now(timezone.utc)
    r = Reminder(
        id=svc.new_reminder_id(),
        owner_user_id=owner.id,
        created_by_user_id=(creator or owner).id,
        title="Due now",
        due_at=now - timedelta(minutes=1),
        original_due_at=now - timedelta(minutes=1),
        **extra,
    )
    db.add(r)
    await db.commit()
    return r


async def test_sweep_notifies_once_and_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session, "due")
    due = await _due(db_session, user)
    later = await _create(client, key, due_at=_soon(120))
    _, admin_headers = await _platform_admin_headers(db_session)

    first = await client.post("/api/v1/admin/sweeps/reminders-due", headers=admin_headers, json={})
    assert first.status_code == 200, first.text
    assert first.json()["reminders_notified"] == 1
    second = await client.post("/api/v1/admin/sweeps/reminders-due", headers=admin_headers, json={})
    assert second.json()["reminders_notified"] == 0

    rows = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.user_id == user.id)
        )
    ).scalars().all()
    assert [(r.subject_kind, r.subject_id, r.kind) for r in rows] == [
        ("reminder", due.id, "reminder_due")
    ]
    inbox = await client.get("/api/v1/me/ticket-notifications", headers=_hdrs(key))
    assert inbox.json()["items"][0]["subject"]["id"] == due.id

    # Snoozing clears notified_at, so once due again it notifies again.
    await client.post(
        f"/api/v1/me/reminders/{later['id']}/snooze", headers=_hdrs(key), json={"preset": "1h"}
    )
    listed = await client.get(f"/api/v1/me/reminders/{due.id}", headers=_hdrs(key))
    assert listed.json()["is_due"] is True and listed.json()["notified_at"] is not None


async def test_snooze_and_reschedule_drop_unread_due_notification(
    client: AsyncClient, db_session: AsyncSession
):
    """A reminder pushed later is no longer due: its unread "Reminder due"
    row leaves the inbox and the digest; a read one stays as history."""
    user, key = await _make_user(db_session, "pushed")
    user_id = user.id
    snoozed_id = (await _due(db_session, user)).id
    rescheduled_id = (await _due(db_session, user)).id
    read_id = (await _due(db_session, user)).id
    _, admin_headers = await _platform_admin_headers(db_session)
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=admin_headers, json={})
    assert resp.json()["reminders_notified"] == 3
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.subject_id == read_id)
        .values(read_at=datetime.now(timezone.utc))
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/me/reminders/{snoozed_id}/snooze", headers=_hdrs(key), json={"preset": "1h"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.patch(
        f"/api/v1/me/reminders/{rescheduled_id}", headers=_hdrs(key), json={"due_at": _soon(90)}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/me/reminders/{read_id}/snooze", headers=_hdrs(key), json={"preset": "1h"}
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(TicketNotification.subject_id).where(TicketNotification.user_id == user_id)
        )
    ).scalars().all()
    assert rows == [read_id]
    unread = await client.get(
        "/api/v1/me/ticket-notifications", headers=_hdrs(key), params={"unread": "true"}
    )
    assert unread.json()["items"] == []


async def test_sweep_skips_owner_who_lost_source(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, mate, _, _, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    await _due(
        db_session, mate, creator=lead, source_kind="ticket", source_id=ticket_id,
        project_id=project.id,
    )
    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == mate.id))
    await db_session.commit()
    _, admin_headers = await _platform_admin_headers(db_session)
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=admin_headers, json={})
    assert resp.json()["reminders_notified"] == 0
    assert resp.json()["reminders_skipped_access"] == 1
    rows = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.user_id == mate.id)
        )
    ).scalars().all()
    assert rows == []


async def test_concurrent_sweeps_claim_once(db_engine, db_session: AsyncSession):
    user, _ = await _make_user(db_session, "race")
    reminders = [await _due(db_session, user) for _ in range(5)]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def run() -> dict:
        async with factory() as s:
            return await svc.sweep_due_reminders(s)

    a, b = await asyncio.gather(run(), run())
    assert a["reminders_notified"] + b["reminders_notified"] == 5
    rows = (
        await db_session.execute(
            select(TicketNotification.subject_id).where(TicketNotification.user_id == user.id)
        )
    ).scalars().all()
    assert sorted(rows) == sorted(r.id for r in reminders)


# ── No N+1 ──


async def test_list_query_count_is_flat(client: AsyncClient, db_session: AsyncSession, db_engine):
    lead, lead_key, mate, _, _, project = await _team(db_session)
    tickets = [await _create_ticket(client, project, lead_key) for _ in range(12)]

    async def count_queries(path: str) -> int:
        statements: list[str] = []

        def before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(db_engine.sync_engine, "before_cursor_execute", before)
        try:
            resp = await client.get(path, headers=_hdrs(lead_key))
            assert resp.status_code == 200
        finally:
            event.remove(db_engine.sync_engine, "before_cursor_execute", before)
        return len(statements)

    for t in tickets[:2]:
        await _create(client, lead_key, owner=mate.id, source={"kind": "ticket", "id": t})
    small = await count_queries("/api/v1/me/reminders")
    for t in tickets[2:]:
        await _create(client, lead_key, owner=mate.id, source={"kind": "ticket", "id": t})
    large = await count_queries("/api/v1/me/reminders")
    assert large == small, (small, large)
    listing = await client.get("/api/v1/me/reminders", headers=_hdrs(lead_key))
    assert len(listing.json()["items"]) == 12


async def test_list_filters_and_cursor(client: AsyncClient, db_session: AsyncSession):
    _, lead_key, _, _, _, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    on_ticket = await _create(client, lead_key, due_at=_soon(30), source={"kind": "ticket", "id": ticket_id})
    plain = [await _create(client, lead_key, due_at=_soon(60 + i)) for i in range(3)]
    by_source = await client.get(
        f"/api/v1/me/reminders?source=ticket:{ticket_id}", headers=_hdrs(lead_key)
    )
    assert [r["id"] for r in by_source.json()["items"]] == [on_ticket["id"]]
    by_kind = await client.get("/api/v1/me/reminders?source=ticket", headers=_hdrs(lead_key))
    assert len(by_kind.json()["items"]) == 1
    bad = await client.get("/api/v1/me/reminders?source=wiki", headers=_hdrs(lead_key))
    assert bad.status_code == 422
    before = await client.get(
        "/api/v1/me/reminders",
        params={"due_before": _soon(45)},
        headers=_hdrs(lead_key),
    )
    assert [r["id"] for r in before.json()["items"]] == [on_ticket["id"]]

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/api/v1/me/reminders", params=params, headers=_hdrs(lead_key))).json()
        seen.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [on_ticket["id"], *[p["id"] for p in plain]]


async def _chat(db: AsyncSession, project: Project, user: User) -> tuple[str, str]:
    """A chat thread (kind='chat') with one action item."""
    content = "Ada: we should follow up\nTolu: agreed"
    chat = MeetingTranscript(
        id=f"chat_{uuid.uuid4().hex[:16]}",
        org_id=project.org_id,
        project_id=project.id,
        kind="chat",
        provider="slack",
        channel_name="#infra",
        title="#infra",
        source="chat_export",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        line_count=2,
        created_by_user_id=user.id,
    )
    db.add(chat)
    await db.flush()
    item = MeetingActionItem(
        id=f"mai_{uuid.uuid4().hex[:16]}",
        transcript_id=chat.id,
        kind="action",
        description="Book the follow-up",
        line_start=1,
        line_end=2,
        status="pending",
        due_date=(datetime.now(timezone.utc) + timedelta(days=3)).date(),
        extracted_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    return chat.id, item.id


async def test_chat_item_source(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, mate, mate_key, _, project = await _team(db_session)
    chat_id, chat_item = await _chat(db_session, project, lead)
    meeting_id, meeting_item = await _meeting(db_session, project, lead)

    on_item = await _create(
        client, lead_key, owner=mate.id,
        source={"kind": "chat_item", "id": chat_item, "lines": "1-2"},
    )
    assert on_item["source"]["kind"] == "chat_item"
    assert on_item["source"]["meeting_id"] == chat_id
    assert on_item["source"]["title"] == "Book the follow-up"
    # The owner (a project reader) sees it with its source.
    got = await client.get(f"/api/v1/me/reminders/{on_item['id']}", headers=_hdrs(mate_key))
    assert got.status_code == 200 and got.json()["source"]["id"] == chat_item

    # The thread's reminders strip lists chat_item reminders too.
    listed = await client.get(
        f"/api/v1/projects/{project.id}/meetings/{chat_id}/reminders", headers=_hdrs(lead_key)
    )
    assert [r["id"] for r in listed.json()["items"]] == [on_item["id"]]

    # A meeting_item id on a chat thread, a chat_item id on a meeting, and an
    # id that doesn't exist are all the same 404.
    async def _miss(source: dict):
        resp = await client.post(
            "/api/v1/me/reminders", headers=_hdrs(lead_key),
            json={"title": "x", "due_at": _soon(), "source": source},
        )
        assert resp.status_code == 404
        return resp.json()

    a = await _miss({"kind": "meeting_item", "id": chat_item})
    b = await _miss({"kind": "chat_item", "id": meeting_item})
    c = await _miss({"kind": "chat_item", "id": "mai_missing"})
    assert a == b == c

    # An outsider can't use a chat item as a source.
    _, outsider_key = await _make_user(db_session, "outsider")
    resp = await client.post(
        "/api/v1/me/reminders", headers=_hdrs(outsider_key),
        json={"title": "x", "due_at": _soon(), "source": {"kind": "chat_item", "id": chat_item}},
    )
    assert resp.status_code == 404 and resp.json() == a

    # A deleted thread hides the source from the owner who didn't create it.
    await db_session.execute(
        update(MeetingTranscript)
        .where(MeetingTranscript.id == chat_id)
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await db_session.commit()
    gone = await client.get(f"/api/v1/me/reminders/{on_item['id']}", headers=_hdrs(mate_key))
    assert gone.status_code == 404


async def test_item_reminder_route_on_chat_thread_uses_chat_item(
    client: AsyncClient, db_session: AsyncSession
):
    lead, lead_key, _, _, _, project = await _team(db_session)
    chat_id, chat_item = await _chat(db_session, project, lead)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings/{chat_id}/items/{chat_item}/reminder",
        headers=_hdrs(lead_key),
        json={},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"]["kind"] == "chat_item"
    assert resp.json()["source"]["lines"] == "1-2"
    row = (
        await db_session.execute(select(Reminder).where(Reminder.id == resp.json()["id"]))
    ).scalar_one()
    assert (row.source_kind, row.source_id, row.created_via) == ("chat_item", chat_item, "meeting")
