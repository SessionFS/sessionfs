"""Direct-to-person ticket assignment (GitHub #96).

A ticket may be assigned to a persona (``assigned_to``) or straight to a
teammate (``assigned_user_id`` / ``assigned_user``). This covers the HTTP
contract: create/update by email or id, the one-assignee conflict, the
uniform ``assignee_not_eligible`` 422, clearing, mutual exclusion, the
``assigned_user=`` list filter, the ``assignable-users`` endpoint, and the
notification hand-off (``your_turn`` + ``assignee`` subscribe).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

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
    TicketNotification,
    TicketWatcher,
    User,
)

_NOT_ELIGIBLE = {
    "error": "assignee_not_eligible",
    "message": "Assignee must be an active teammate with access to this project.",
}
_CONFLICT = {
    "error": "assignee_conflict",
    "message": "A ticket has one assignee at a time: a persona or a person, not both.",
}


async def _make_user(
    db: AsyncSession,
    name: str = "alice",
    *,
    tier: str = "team",
    is_active: bool = True,
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        is_active=is_active,
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


async def _make_org(
    db: AsyncSession, *, slug: str | None = None, settings: str | None = "{}"
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=slug or f"o-{uuid.uuid4().hex[:8]}",
        tier="team",
        settings=settings,
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
        name=f"assign-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/a-{uuid.uuid4().hex[:6]}",
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


async def _create_ticket(
    client: AsyncClient, project: Project, key: str, **overrides
) -> dict:
    body = {"title": "Fix the flaky test", "description": "integration coverage"}
    body.update(overrides)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key), json=body
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _notification_rows(
    db: AsyncSession, user_id: str
) -> list[TicketNotification]:
    return (
        await db.execute(
            select(TicketNotification)
            .where(TicketNotification.user_id == user_id)
            .order_by(TicketNotification.id.asc())
        )
    ).scalars().all()


async def _watcher_rows(db: AsyncSession, ticket_id: str) -> list[TicketWatcher]:
    return (
        await db.execute(
            select(TicketWatcher)
            .where(TicketWatcher.ticket_id == ticket_id)
            .order_by(TicketWatcher.user_id)
        )
    ).scalars().all()


# ── Create with a person ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_assigned_to_person_by_email(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)

    body = await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email
    )

    assert body["assigned_to"] is None
    assert body["assigned_user_id"] == assignee.id
    assert body["assigned_user"] == {
        "id": assignee.id,
        "display_name": assignee.display_name,
        "email": assignee.email,
    }

    row = (await db_session.execute(select(Ticket).where(Ticket.id == body["id"]))).scalar_one()
    assert row.assigned_user_id == assignee.id
    assert row.assigned_to is None

    # The assignee is handed "your turn" and auto-subscribed as assignee.
    notes = await _notification_rows(db_session, assignee.id)
    assert [n.kind for n in notes] == ["your_turn"]
    assert notes[0].summary == f"Assigned to {assignee.display_name}"
    watchers = await _watcher_rows(db_session, body["id"])
    assert {(w.user_id, w.source) for w in watchers} == {
        (owner.id, "creator"),
        (assignee.id, "assignee"),
    }


@pytest.mark.asyncio
async def test_create_assigned_to_person_by_id(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)

    body = await _create_ticket(
        client, project, owner_key, assigned_user_id=assignee.id
    )

    assert body["assigned_user_id"] == assignee.id
    assert body["assigned_user"]["id"] == assignee.id


# ── Update / reassignment ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_assigns_person_and_clears_persona(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    await _make_persona(db_session, project, owner, "atlas")
    ticket_id = (await _create_ticket(client, project, owner_key, assigned_to="atlas"))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_user": assignee.email},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_user_id"] == assignee.id
    assert body["assigned_to"] is None

    # The assignee receives the hand-off on reassignment.
    notes = await _notification_rows(db_session, assignee.id)
    assert [n.kind for n in notes] == ["your_turn"]
    assert notes[0].summary == f"Assigned to {assignee.display_name}"


@pytest.mark.asyncio
async def test_update_persona_clears_person(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    await _make_persona(db_session, project, owner, "atlas")
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": "atlas"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_to"] == "atlas"
    assert body["assigned_user_id"] is None
    assert body["assigned_user"] is None


# ── Conflict / eligibility / clearing ─────────────────────────────


@pytest.mark.asyncio
async def test_assignee_conflict_422(client: AsyncClient, db_session: AsyncSession):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)

    for payload in (
        {"assigned_to": "atlas", "assigned_user": assignee.email},
        {"assigned_user": assignee.email, "assigned_user_id": assignee.id},
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(owner_key),
            json={"title": "conflict", "description": "x", **payload},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["details"]["error"] == "assignee_conflict"
        assert resp.json()["error"]["message"] == _CONFLICT["message"]


@pytest.mark.asyncio
async def test_ineligible_assignee_identical_422(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    other_org_member, _ = await _make_user(db_session, name="outsider")
    inactive, _ = await _make_user(db_session, name="ghost", is_active=False)
    org = await _make_org(db_session)
    other_org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, other_org, other_org_member)
    await _make_member(db_session, org, inactive)
    project = await _make_project(db_session, owner, org)

    for token in (
        other_org_member.email,          # active, but no standing on this project
        other_org_member.id,
        inactive.email,                   # standing but not active
        "nobody@example.com",             # unknown
        str(uuid.uuid4()),                # unknown id
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(owner_key),
            json={"title": "t", "description": "x", "assigned_user": token},
        )
        assert resp.status_code == 422, resp.text
        # Every miss — unknown, inactive, or no read standing — returns the
        # identical error code and message so existence/membership is never
        # revealed.
        assert resp.json()["error"]["details"]["error"] == "assignee_not_eligible"
        assert resp.json()["error"]["message"] == _NOT_ELIGIBLE["message"]


@pytest.mark.asyncio
async def test_explicit_null_unassigns_person(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_user_id": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_user_id"] is None
    assert body["assigned_user"] is None


# ── List filter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filter_assigned_user_me(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, assignee_key = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)

    mine = (await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email, title="mine"
    ))["id"]
    await _create_ticket(client, project, owner_key, title="unassigned")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(assignee_key),
        params={"assigned_user": "me"},
    )
    assert resp.status_code == 200, resp.text
    assert [t["id"] for t in resp.json()] == [mine]

    # An explicit id is an equivalent filter.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(assignee_key),
        params={"assigned_user": assignee.id},
    )
    assert [t["id"] for t in resp.json()] == [mine]


# ── assignable-users ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assignable_users_lists_active_readers_sorted(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="alice")
    bob, _ = await _make_user(db_session, name="bob")
    carol, _ = await _make_user(db_session, name="carol")
    ghost, _ = await _make_user(db_session, name="ghost", is_active=False)
    outsider, _ = await _make_user(db_session, name="outsider")
    captured, _ = await _make_user(db_session, name="captured")

    org = await _make_org(db_session)
    other_org = await _make_org(db_session)
    for u, role in ((owner, "admin"), (bob, "member"), (carol, "member"), (ghost, "member")):
        await _make_member(db_session, org, u, role=role)
    await _make_member(db_session, other_org, outsider)
    project = await _make_project(db_session, owner, org)
    # A captured-session-only reader has access but is NOT a teammate you can
    # hand work to — deliberately excluded.
    await _make_session(db_session, captured, project)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/assignable-users", headers=_hdrs(owner_key)
    )
    assert resp.status_code == 200, resp.text
    users = resp.json()["users"]
    ids = [u["id"] for u in users]

    assert owner.id in ids
    assert bob.id in ids
    assert carol.id in ids
    assert ghost.id not in ids       # inactive
    assert outsider.id not in ids    # other org
    assert captured.id not in ids    # captured-session only

    # Sorted by display name/email; everyone here is an editor in an
    # unrestricted org.
    assert [u["display_name"] for u in users] == sorted(u["display_name"] for u in users)
    assert all(u["can_edit"] for u in users)


@pytest.mark.asyncio
async def test_assignable_users_restricted_org_excludes_plain_members(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="alice")
    member, _ = await _make_user(db_session, name="bob")
    org = await _make_org(
        db_session,
        settings='{"general": {"restrict_project_visibility": true}}',
    )
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member, role="member")
    project = await _make_project(db_session, owner, org)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/assignable-users", headers=_hdrs(owner_key)
    )
    assert resp.status_code == 200, resp.text
    ids = [u["id"] for u in resp.json()["users"]]
    assert owner.id in ids
    # A plain member without a group grant has no read standing in a
    # restricted org, so they are not assignable.
    assert member.id not in ids


@pytest.mark.asyncio
async def test_assignable_users_service_key_403(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="alice")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    # A service key has no person to speak for.
    svc_key = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(svc_key),
            name="svc",
            scopes='["*"]',
            key_kind="service",
            is_active=True,
            org_id=org.id,
            project_ids=f'["{project.id}"]',
            service_key_name="triage-bot",
            created_by_user_id=owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/assignable-users", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 403, resp.text


# ── Suggested tickets do not hand off until approved ──────────────


@pytest.mark.asyncio
async def test_suggested_ticket_does_not_hand_off_until_approved(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)

    # Agent-sourced tasks start 'suggested': the assignee is neither
    # notified nor subscribed until an editor approves.
    body = await _create_ticket(
        client,
        project,
        owner_key,
        source="agent",
        acceptance_criteria=["it works"],
        description="a meaningfully long description",
        assigned_user=assignee.email,
    )
    assert body["status"] == "suggested"
    assert await _notification_rows(db_session, assignee.id) == []
    watchers = await _watcher_rows(db_session, body["id"])
    assert {(w.user_id, w.source) for w in watchers} == {(owner.id, "creator")}

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{body['id']}/approve",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, resp.text

    notes = await _notification_rows(db_session, assignee.id)
    assert [n.kind for n in notes] == ["your_turn"]


# ── Explicit null unassigns the named side (dashboard sends both keys) ──


@pytest.mark.asyncio
async def test_explicit_null_unassigns_persona(
    client: AsyncClient, db_session: AsyncSession
):
    """F1 — an explicit ``assigned_to: null`` clears the persona, not just
    when paired with a person token."""
    owner, owner_key = await _make_user(db_session, name="owner")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    await _make_persona(db_session, project, owner, "atlas")
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_to="atlas"
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_to"] is None


@pytest.mark.asyncio
async def test_both_null_unassigns_person(
    client: AsyncClient, db_session: AsyncSession
):
    """F1 — the dashboard always sends both keys; a both-null body clears a
    person assignment."""
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": None, "assigned_user_id": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_to"] is None
    assert body["assigned_user_id"] is None
    assert body["assigned_user"] is None


# ── TicketEdit rows store ids, not display labels ──────────────────


@pytest.mark.asyncio
async def test_person_assignment_edits_store_ids_not_labels(
    client: AsyncClient, db_session: AsyncSession
):
    """F8 — ``assigned_user_id`` edits store the user id in old/new and render
    a readable label only in the diff comment."""
    from sessionfs.server.db.models import TicketEdit

    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    ticket_id = (await _create_ticket(client, project, owner_key))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_user_id": assignee.id},
    )
    assert resp.status_code == 200, resp.text

    edit = (
        await db_session.execute(
            select(TicketEdit).where(
                TicketEdit.ticket_id == ticket_id,
                TicketEdit.field_name == "assigned_user_id",
            )
        )
    ).scalar_one()
    assert json.loads(edit.old_value) is None
    assert json.loads(edit.new_value) == assignee.id

    comments_r = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/comments",
        headers=_hdrs(owner_key),
    )
    system = [
        c for c in comments_r.json() if c.get("author_persona") == "system"
    ]
    assert len(system) == 1
    assert "Assigned to Jane" in system[0]["content"]


@pytest.mark.asyncio
async def test_person_unassign_diff_renders_label(
    client: AsyncClient, db_session: AsyncSession
):
    """F8 — unassign stores ``None`` and renders the remembered label."""
    from sessionfs.server.db.models import TicketEdit

    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_user_id=assignee.id
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": None, "assigned_user_id": None},
    )
    assert resp.status_code == 200, resp.text

    edit = (
        await db_session.execute(
            select(TicketEdit).where(
                TicketEdit.ticket_id == ticket_id,
                TicketEdit.field_name == "assigned_user_id",
            )
        )
    ).scalar_one()
    assert json.loads(edit.old_value) == assignee.id
    assert json.loads(edit.new_value) is None

    comments_r = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/comments",
        headers=_hdrs(owner_key),
    )
    system = [
        c for c in comments_r.json() if c.get("author_persona") == "system"
    ]
    assert len(system) == 1
    assert "Unassigned (was Jane)" in system[0]["content"]


@pytest.mark.asyncio
async def test_same_display_name_reassignment_stores_distinct_ids(
    client: AsyncClient, db_session: AsyncSession
):
    """F8 — two teammates sharing a display name never compare equal because
    the audit row stores their distinct ids."""
    from sessionfs.server.db.models import TicketEdit

    owner, owner_key = await _make_user(db_session, name="owner")
    jane_a, _ = await _make_user(db_session, name="jane")
    jane_b, _ = await _make_user(db_session, name="jane")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, jane_a)
    await _make_member(db_session, org, jane_b)
    project = await _make_project(db_session, owner, org)
    ticket_id = (await _create_ticket(
        client, project, owner_key, assigned_user_id=jane_a.id
    ))["id"]

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_user_id": jane_b.id},
    )
    assert resp.status_code == 200, resp.text

    edits = (
        await db_session.execute(
            select(TicketEdit).where(
                TicketEdit.ticket_id == ticket_id,
                TicketEdit.field_name == "assigned_user_id",
            )
        )
    ).scalars().all()
    assert [(json.loads(e.old_value), json.loads(e.new_value)) for e in edits] == [
        (jane_a.id, jane_b.id)
    ]


# ── Editability gate applies to explicit-null person tokens ────────


@pytest.mark.asyncio
async def test_explicit_null_person_token_respects_editability_gate(
    client: AsyncClient, db_session: AsyncSession
):
    """F2 — ``{assigned_user_id: null}`` is a work-defining change, so a
    captured-session-only creator can't unassign an in-progress ticket."""
    owner, owner_key = await _make_user(db_session, name="owner")
    assignee, _ = await _make_user(db_session, name="jane")
    creator, creator_key = await _make_user(db_session, name="creator")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    # creator is a captured-session-only reader: access, but no edit standing.
    await _make_session(db_session, creator, project)

    ticket_id = (await _create_ticket(
        client, project, creator_key, assigned_user=assignee.email
    ))["id"]

    # Move past open so work-defining fields are locked for non-editors.
    row = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    row.status = "in_progress"
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(creator_key),
        json={"assigned_user_id": None},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"


# ── assignable-users requires read standing ────────────────────────


@pytest.mark.asyncio
async def test_assignable_users_requires_read_standing(
    client: AsyncClient, db_session: AsyncSession
):
    """F3 — a captured-session-only reader has access but isn't a teammate;
    they can't enumerate who they may assign to."""
    owner, owner_key = await _make_user(db_session, name="alice")
    captured, captured_key = await _make_user(db_session, name="captured")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    project = await _make_project(db_session, owner, org)
    await _make_session(db_session, captured, project)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/assignable-users",
        headers=_hdrs(captured_key),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "project_member_required"


# ── assignee email is masked for captured-session-only readers ─────


@pytest.mark.asyncio
async def test_assignee_email_masked_for_captured_reader(
    client: AsyncClient, db_session: AsyncSession
):
    """A captured-session-only reader (possibly outside the org) can read the
    project's tickets, but must not learn a teammate's email address from a
    person assignment. Members still see the full address."""
    owner, owner_key = await _make_user(db_session, name="alice")
    assignee, _ = await _make_user(db_session, name="bob")
    assignee.display_name = None
    await db_session.commit()
    captured, captured_key = await _make_user(db_session, name="captured")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    project = await _make_project(db_session, owner, org)
    await _make_session(db_session, captured, project)
    created = await _create_ticket(
        client, project, owner_key, assigned_user=assignee.email
    )
    assert created["assigned_user"]["email"] == assignee.email

    base = f"/api/v1/projects/{project.id}/tickets"
    for key, expect_full in ((owner_key, True), (captured_key, False)):
        detail = await client.get(f"{base}/{created['id']}", headers=_hdrs(key))
        assert detail.status_code == 200, detail.text
        listing = await client.get(base, headers=_hdrs(key))
        assert listing.status_code == 200, listing.text
        by_id = await client.get(
            f"/api/v1/tickets/{created['id']}", headers=_hdrs(key)
        )
        assert by_id.status_code == 200, by_id.text
        emails = [
            detail.json()["assigned_user"]["email"],
            next(t for t in listing.json() if t["id"] == created["id"])[
                "assigned_user"
            ]["email"],
            by_id.json()["assigned_user"]["email"],
        ]
        for email in emails:
            if expect_full:
                assert email == assignee.email
            else:
                assert email != assignee.email
                assert email.startswith(assignee.email[0] + "***@")
