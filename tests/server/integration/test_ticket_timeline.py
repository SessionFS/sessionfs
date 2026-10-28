"""GitHub #113 — the append-only ticket timeline.

Covers the event log (create / transitions / assignment / edits / scheduling
/ Jira verbs), the due-date + estimate contract, the events endpoint and the
creator/resolver provenance fields on ``TicketResponse``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    OrgMember,
    Organization,
    Project,
    Session,
    Ticket,
    TicketEvent,
    User,
)
from sessionfs.server.services.ticket_events import record_ticket_event


async def _make_user(
    db: AsyncSession,
    name: str = "alice",
    *,
    tier: str = "team",
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            scopes='["*"]',
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:8]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _make_member(
    db: AsyncSession, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"timeline-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/tl-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _make_session(db: AsyncSession, user: User, project: Project) -> None:
    now = datetime.now(timezone.utc)
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            title="synced",
            source_tool="claude-code",
            blob_key=f"sessions/{user.id}/x.tar.gz",
            etag=uuid.uuid4().hex,
            created_at=now,
            updated_at=now,
            uploaded_at=now,
            project_id=project.id,
        )
    )
    await db.commit()


async def _service_key(
    db: AsyncSession,
    minter: User,
    org: Organization,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name="timeline-bot",
            project_ids=json.dumps(project_ids) if project_ids is not None else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _create(
    client: AsyncClient, project: Project, key: str, **overrides
) -> dict:
    body = {"title": "Walk the timeline", "description": "integration coverage"}
    body.update(overrides)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key), json=body
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _events(client: AsyncClient, project: Project, key: str, ticket_id: str) -> list[dict]:
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/events",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["events"]


async def _event_actions(db: AsyncSession, ticket_id: str) -> list[tuple[str, str | None]]:
    rows = (
        await db.execute(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
        )
    ).scalars().all()
    return [(e.action, e.field) for e in rows]


# ── Create records the create + scheduling + assignment events ─────


@pytest.mark.asyncio
async def test_create_records_events_and_recent_events(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, "atlas")

    body = await _create(
        client,
        project,
        key,
        due_date="2026-11-15",
        estimated_effort="2d 4h",
        assigned_to="atlas",
    )
    assert body["due_date"] == "2026-11-15"
    assert body["estimated_effort"] == "2d 4h"
    assert body["created_by"]["user_id"] == owner.id
    assert body["created_by"]["email"] == owner.email

    actions = await _event_actions(db_session, body["id"])
    # created, due_date_set, estimate_set, assigned — newest first in the
    # endpoint, but the DB read is chronological.
    assert ("created", None) in actions
    assert ("due_date_set", "due_date") in actions
    assert ("estimate_set", "estimated_effort") in actions
    assert ("assigned", "assigned_to") in actions

    # recent_events on the single GET carry the newest 10, newest first.
    detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{body['id']}",
        headers=_hdrs(key),
    )
    assert detail.status_code == 200, detail.text
    recent = detail.json()["recent_events"]
    assert {e["action"] for e in recent} == {
        "created", "due_date_set", "estimate_set", "assigned",
    }
    assigned = next(e for e in recent if e["action"] == "assigned")
    assert assigned["after"] == {"persona": "atlas"}
    assert assigned["actor"]["user_id"] == owner.id


# ── Every FSM transition records status_changed ──────────────────


@pytest.mark.asyncio
async def test_full_task_fsm_records_status_events(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, "atlas")

    tk = await _create(client, project, key, assigned_to="atlas")
    tid = tk["id"]

    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/start", headers=_hdrs(key)
    )
    assert start.status_code == 200, start.text
    block = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/block", headers=_hdrs(key)
    )
    assert block.status_code == 200, block.text
    unblock = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/unblock", headers=_hdrs(key)
    )
    assert unblock.status_code == 200, unblock.text
    complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/complete",
        headers=_hdrs(key),
        json={"notes": "Done.", "changed_files": ["src/x.py"]},
    )
    assert complete.status_code == 200, complete.text
    accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/accept", headers=_hdrs(key)
    )
    assert accept.status_code == 200, accept.text

    actions = [a for a, _ in await _event_actions(db_session, tid)]
    for expected in ("created", "assigned", "status_changed"):
        assert expected in actions
    # created, assigned, start, block, unblock, complete, accept
    assert actions.count("status_changed") == 5

    # resolved_by is stamped once the ticket is accepted.
    detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(key)
    )
    assert detail.json()["resolved_by"]["user_id"] == owner.id


@pytest.mark.asyncio
async def test_accept_close_reopen_pass_service_key_label(
    db_session: AsyncSession,
):
    """accept/close/reopen carry a service key's name as actor_label."""
    from sessionfs.server.services import ticket_transitions

    owner, _ = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    async def make_ticket(kind: str, status: str) -> Ticket:
        ticket = Ticket(
            id=f"tk_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            title="t",
            kind=kind,
            status=status,
            created_by_user_id=owner.id,
        )
        db_session.add(ticket)
        await db_session.commit()
        await db_session.refresh(ticket)
        return ticket

    reopen = await make_ticket("task", "review")
    await ticket_transitions.reopen_ticket(
        db_session,
        project=project,
        ticket=reopen,
        actor_user_id=None,
        actor_type="service_key",
        service_key_name="timeline-bot",
    )
    await db_session.commit()

    issue = await make_ticket("issue", "in_progress")
    await ticket_transitions.close_issue(
        db_session,
        project=project,
        ticket=issue,
        actor_user_id=None,
        actor_type="service_key",
        service_key_name="timeline-bot",
    )
    await db_session.commit()

    accept = await make_ticket("task", "review")
    await ticket_transitions.accept_ticket(
        db_session,
        project_id=project.id,
        ticket_id=accept.id,
        lease_epoch=None,
        actor_user_id=None,
        actor_type="service_key",
        service_key_name="timeline-bot",
    )
    await db_session.commit()

    for ticket_id in (reopen.id, issue.id, accept.id):
        event = (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.action == "status_changed",
                )
            )
        ).scalars().one()
        assert event.actor_type == "service"
        assert event.actor_label == "timeline-bot"
        assert event.actor_user_id is None


@pytest.mark.asyncio
async def test_approve_and_dismiss_record_approved_and_dismissed(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, "atlas")

    tk = await _create(
        client,
        project,
        key,
        source="agent",
        description="x" * 20,
        acceptance_criteria=["works"],
        assigned_to="atlas",
    )
    assert tk["status"] == "suggested"
    tid = tk["id"]

    approve = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/approve", headers=_hdrs(key)
    )
    assert approve.status_code == 200, approve.text
    dismiss = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/dismiss", headers=_hdrs(key)
    )
    assert dismiss.status_code == 200, dismiss.text

    actions = [a for a, _ in await _event_actions(db_session, tid)]
    assert "approved" in actions
    assert "dismissed" in actions
    assert ("status_changed") in actions


# ── Assignment / edit / scheduling events via update ─────────────


@pytest.mark.asyncio
async def test_update_records_field_assignment_and_scheduling_events(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="bob")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    await _make_persona(db_session, project, owner, "atlas")

    tk = await _create(client, project, key, assigned_to="atlas")
    tid = tk["id"]

    # A persona reassignment + scheduling fields in one PUT.
    upd = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=_hdrs(key),
        json={"assigned_user_id": assignee.id, "due_date": "2026-12-01", "estimated_effort": "30m"},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["due_date"] == "2026-12-01"
    assert upd.json()["estimated_effort"] == "30m"

    events = await _events(client, project, key, tid)
    actions = [(e["action"], e["field"]) for e in events]
    # assignment cleared the persona (unassigned) and set the person (assigned)
    assert ("assigned", "assigned_user_id") in actions
    assert ("unassigned", "assigned_to") in actions
    assert ("due_date_set", "due_date") in actions
    assert ("estimate_set", "estimated_effort") in actions

    # Explicit null clears both scheduling fields and records the clear.
    clear = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=_hdrs(key),
        json={"due_date": None, "estimated_effort": None},
    )
    assert clear.status_code == 200, clear.text
    assert clear.json()["due_date"] is None
    assert clear.json()["estimated_effort"] is None


@pytest.mark.asyncio
async def test_description_edit_stores_only_length_stub(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    tk = await _create(client, project, key, description="before text")
    tid = tk["id"]
    # A short (<200 char) description must still be stubbed: description
    # bodies are never stored in the timeline, regardless of length.
    short_desc = "a short description"

    upd = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=_hdrs(key),
        json={"description": short_desc},
    )
    assert upd.status_code == 200, upd.text

    events = await _events(client, project, key, tid)
    field_edited = [e for e in events if e["field"] == "description"][0]
    assert field_edited["action"] == "field_edited"
    assert field_edited["after"] == {"changed": True, "length": len(short_desc)}
    assert short_desc not in json.dumps(field_edited)


@pytest.mark.asyncio
async def test_list_field_edit_caps_items_to_first_20(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    tk = await _create(client, project, key)
    tid = tk["id"]
    refs = [f"ses_{i:04d}" for i in range(25)]

    upd = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=_hdrs(key),
        json={"related_sessions": refs},
    )
    assert upd.status_code == 200, upd.text

    events = await _events(client, project, key, tid)
    field_edited = [e for e in events if e["field"] == "related_sessions"][0]
    assert field_edited["action"] == "field_edited"
    assert field_edited["after"] == {
        "count": 25,
        "items": refs[:20],
    }


# ── Atomicity: a refused write leaves no event ───────────────────


@pytest.mark.asyncio
async def test_refused_transition_leaves_no_event(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, "atlas")

    tk = await _create(client, project, key, assigned_to="atlas")
    tid = tk["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/start", headers=_hdrs(key)
    )
    before = await _event_actions(db_session, tid)

    # blocking a ticket already in_progress is legal; blocking it twice is not.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/block", headers=_hdrs(key)
    )
    refused = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/block", headers=_hdrs(key)
    )
    assert refused.status_code == 400, refused.text

    after = await _event_actions(db_session, tid)
    assert after == before + [("status_changed", "status")]


# ── Events endpoint: pagination + service-key scope ──────────────


@pytest.mark.asyncio
async def test_events_endpoint_paginates_newest_first(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, "atlas")

    tk = await _create(client, project, key, assigned_to="atlas")
    tid = tk["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/start", headers=_hdrs(key)
    )

    first = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}/events?limit=2",
        headers=_hdrs(key),
    )
    assert first.status_code == 200, first.text
    page1 = first.json()
    assert len(page1["events"]) == 2
    assert page1["next_before"] is not None
    # newest first
    assert page1["events"][0]["action"] == "status_changed"

    second = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}/events?limit=10&before={page1['next_before']}",
        headers=_hdrs(key),
    )
    page2 = second.json()
    ids1 = {e["id"] for e in page1["events"]}
    ids2 = {e["id"] for e in page2["events"]}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_events_endpoint_service_key_scope(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    tk = await _create(client, project, key)

    svc = await _service_key(db_session, owner, org, ["tickets:read"], [project.id])
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/events",
        headers=_hdrs(svc),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["events"]) == 1  # created only

    # A service key NOT allowlisted for this project gets 404/403 like GET.
    # (An empty allowlist means "every project in the org", so point it at a
    # project id the key is not scoped to.)
    other_svc = await _service_key(
        db_session, owner, org, ["tickets:read"], [f"proj_{uuid.uuid4().hex[:16]}"]
    )
    denied = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/events",
        headers=_hdrs(other_svc),
    )
    assert denied.status_code in (403, 404)


@pytest.mark.asyncio
async def test_events_endpoint_non_reader_denied(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    stranger, stranger_key = await _make_user(db_session, name="stranger")
    project = await _make_project(db_session, owner)
    tk = await _create(client, project, key)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/events",
        headers=_hdrs(stranger_key),
    )
    assert resp.status_code in (403, 404)


# ── creator/resolver email masking ───────────────────────────────


@pytest.mark.asyncio
async def test_creator_resolver_email_masking(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="alice")
    captured, captured_key = await _make_user(db_session, name="captured")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    await _make_session(db_session, captured, project)

    tk = await _create(client, project, owner_key, due_date="2026-11-15")
    tid = tk["id"]

    # Owner sees the full creator address; a captured-session reader sees it
    # masked, and a service key inside the org sees it in full.
    detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(owner_key)
    )
    assert detail.json()["created_by"]["email"] == owner.email

    captured_detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(captured_key)
    )
    assert captured_detail.status_code == 200, captured_detail.text
    masked = captured_detail.json()["created_by"]["email"]
    assert masked != owner.email
    assert masked.startswith(owner.email[0] + "***@")


@pytest.mark.asyncio
async def test_service_key_sees_full_emails_and_list_masks_resolved_by(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="alice")
    captured, captured_key = await _make_user(db_session, name="captured")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    await _make_session(db_session, captured, project)
    svc_key = await _service_key(db_session, owner, org, ["tickets:read"], [project.id])

    await _make_persona(db_session, project, owner, "atlas")
    tk = await _create(client, project, owner_key, assigned_to="atlas")
    tid = tk["id"]

    # Move open → in_progress → review so resolver_user_id is stamped and
    # resolved_by becomes non-null.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/start",
        headers=_hdrs(owner_key),
    )
    complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tid}/complete",
        headers=_hdrs(owner_key),
        json={"notes": "Done.", "changed_files": ["src/x.py"]},
    )
    assert complete.status_code == 200, complete.text

    # A service key (org + project allowlisted) sees full creator and resolver
    # addresses on the detail read.
    svc_detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(svc_key)
    )
    assert svc_detail.status_code == 200, svc_detail.text
    assert svc_detail.json()["created_by"]["email"] == owner.email
    assert svc_detail.json()["resolved_by"]["email"] == owner.email

    # A captured-session reader sees both masked.
    captured_detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(captured_key)
    )
    assert captured_detail.status_code == 200, captured_detail.text
    assert captured_detail.json()["resolved_by"]["email"] != owner.email
    assert captured_detail.json()["resolved_by"]["email"].startswith(
        owner.email[0] + "***@"
    )

    # The list endpoint applies the same masking per reader.
    svc_list = await client.get(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(svc_key)
    )
    assert svc_list.status_code == 200, svc_list.text
    svc_row = next(t for t in svc_list.json() if t["id"] == tid)
    assert svc_row["created_by"]["email"] == owner.email
    assert svc_row["resolved_by"]["email"] == owner.email

    captured_list = await client.get(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(captured_key)
    )
    assert captured_list.status_code == 200, captured_list.text
    captured_row = next(t for t in captured_list.json() if t["id"] == tid)
    assert captured_row["created_by"]["email"] != owner.email
    assert captured_row["resolved_by"]["email"] != owner.email
    assert captured_row["resolved_by"]["email"].startswith(owner.email[0] + "***@")


@pytest.mark.asyncio
async def test_failed_write_after_event_added_leaves_no_event(
    client: AsyncClient, db_session: AsyncSession
):
    """An event is added inside the caller's transaction, never committed.

    ``record_ticket_event`` only ``db.add``-s the row; the route owns the
    commit. Simulate a write whose event lands but whose later step fails by
    rolling the session back — the event must disappear with it.
    """
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    body = await _create(client, project, key)
    tid = body["id"]

    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == tid))
    ).scalar_one()
    await record_ticket_event(
        db_session,
        ticket,
        "field_edited",
        field="title",
        before="Walk the timeline",
        after="Walk the timeline (renamed)",
        actor_user_id=owner.id,
    )

    # A later step in the same write raises, so the route rolls back.
    await db_session.rollback()

    # The committed "created" event survives, but the rolled-back
    # field_edited event must not have landed anywhere.
    field_edits = (
        await db_session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == tid,
                TicketEvent.action == "field_edited",
                TicketEvent.field == "title",
            )
        )
    ).scalars().all()
    assert field_edits == []


# ── due_date / estimate validation and list filters ──────────────


@pytest.mark.asyncio
async def test_due_date_and_estimate_validation(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    bad_due = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "x", "due_date": "not-a-date"},
    )
    assert bad_due.status_code == 422, bad_due.text

    bad_estimate = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "x", "estimated_effort": "three days"},
    )
    assert bad_estimate.status_code == 422, bad_estimate.text

    too_long = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "x", "estimated_effort": "1d " * 16},
    )
    assert too_long.status_code == 422, too_long.text

    # Canonical single-space form is stored.
    ok = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "x", "estimated_effort": "2d  4h"},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["estimated_effort"] == "2d 4h"

    # Compact tokens without whitespace ("2d4h") are accepted and normalized.
    compact = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "x", "estimated_effort": "2d4h"},
    )
    assert compact.status_code == 201, compact.text
    assert compact.json()["estimated_effort"] == "2d 4h"


@pytest.mark.asyncio
async def test_list_due_before_and_overdue_filters(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    today = date.today()
    past = (today - timedelta(days=5)).isoformat()
    future = (today + timedelta(days=5)).isoformat()

    overdue = await _create(client, project, key, title="overdue", due_date=past)
    overdue2 = await _create(client, project, key, title="overdue2", due_date=past)
    await _create(client, project, key, title="upcoming", due_date=future)
    await _create(client, project, key, title="no-due")

    due_before = await client.get(
        f"/api/v1/projects/{project.id}/tickets?due_before={today.isoformat()}",
        headers=_hdrs(key),
    )
    assert {t["id"] for t in due_before.json()} == {overdue["id"], overdue2["id"]}

    overdue_list = await client.get(
        f"/api/v1/projects/{project.id}/tickets?overdue=true",
        headers=_hdrs(key),
    )
    assert {t["id"] for t in overdue_list.json()} == {overdue["id"], overdue2["id"]}

    # A past-due ticket that is still in flight stays in the overdue list.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{overdue['id']}/start",
        headers=_hdrs(key),
    )
    still_overdue = await client.get(
        f"/api/v1/projects/{project.id}/tickets?overdue=true",
        headers=_hdrs(key),
    )
    assert {t["id"] for t in still_overdue.json()} == {overdue["id"], overdue2["id"]}

    # Once cancelled it drops out.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{overdue2['id']}/dismiss",
        headers=_hdrs(key),
    )
    no_longer = await client.get(
        f"/api/v1/projects/{project.id}/tickets?overdue=true",
        headers=_hdrs(key),
    )
    assert {t["id"] for t in no_longer.json()} == {overdue["id"]}
