"""GET /api/v1/tickets/{ticket_id} — ticket lookup by id alone.

Used by the session helper's ticket-link preview. Authorization is the
project-scoped GET's, run against the ticket's own project; a missing ticket
and an unreadable one must be indistinguishable.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, OrgMember, Organization, Project, User


async def _make_user(db: AsyncSession, name: str, tier: str = "team") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
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
    return org


async def _add_member(db: AsyncSession, org: Organization, user: User, role: str = "member") -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role, joined_at=datetime.now(timezone.utc)))
    await db.commit()


async def _make_project(db: AsyncSession, owner: User, org: Organization | None = None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"Widgets {uuid.uuid4().hex[:4]}",
        git_remote_normalized=f"acme/w-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    return project


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _service_key(
    db: AsyncSession, minter: User, org: Organization, scopes: list[str], project_ids: list[str] | None = None
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
            service_key_name="preview-bot",
            project_ids=json.dumps(project_ids) if project_ids is not None else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _ticket_with_comments(client: AsyncClient, project: Project, key: str, comments: int = 0) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Fix the flaky upload test",
            "description": "The part upload test times out on CI.",
            "priority": "high",
            "acceptance_criteria": ["passes 50 runs", "no sleep()"],
        },
    )
    assert resp.status_code == 201, resp.text
    tid = resp.json()["id"]
    for i in range(comments):
        r = await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tid}/comments",
            headers=_hdrs(key),
            json={"content": f"comment {i}"},
        )
        assert r.status_code == 201, r.text
    return tid


@pytest.mark.asyncio
async def test_member_gets_ticket_with_project_and_recent_comments(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    await _add_member(db_session, org, owner, "admin")
    await _add_member(db_session, org, member)
    project = await _make_project(db_session, owner, org)
    tid = await _ticket_with_comments(client, project, owner_key, comments=5)

    resp = await client.get(f"/api/v1/tickets/{tid}", headers=_hdrs(member_key))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    scoped = await client.get(f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(member_key))
    assert scoped.status_code == 200, scoped.text
    # Same shape as the project-scoped GET, plus the preview fields.
    for key, value in scoped.json().items():
        assert body[key] == value, key
    assert body["project_id"] == project.id
    assert body["project_name"] == project.name
    assert body["comment_count"] == 5
    assert [c["content"] for c in body["recent_comments"]] == ["comment 4", "comment 3", "comment 2"]


@pytest.mark.asyncio
async def test_non_member_and_missing_ticket_get_identical_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    _stranger, stranger_key = await _make_user(db_session, "stranger")
    project = await _make_project(db_session, owner)
    tid = await _ticket_with_comments(client, project, owner_key)

    hidden = await client.get(f"/api/v1/tickets/{tid}", headers=_hdrs(stranger_key))
    missing = await client.get(f"/api/v1/tickets/tk_{'0' * 16}", headers=_hdrs(stranger_key))
    assert hidden.status_code == 404
    assert missing.status_code == 404
    assert hidden.json() == missing.json()


@pytest.mark.asyncio
async def test_unauthenticated_is_rejected(client: AsyncClient, db_session: AsyncSession):
    resp = await client.get(f"/api/v1/tickets/tk_{'0' * 16}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_service_key_matches_project_scoped_route(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    await _add_member(db_session, org_a, owner, "admin")
    await _add_member(db_session, org_b, owner, "admin")
    proj_a = await _make_project(db_session, owner, org_a)
    proj_a2 = await _make_project(db_session, owner, org_a)
    proj_b = await _make_project(db_session, owner, org_b)
    tk_a = await _ticket_with_comments(client, proj_a, owner_key, comments=1)
    tk_a2 = await _ticket_with_comments(client, proj_a2, owner_key)
    tk_b = await _ticket_with_comments(client, proj_b, owner_key)

    reader = await _service_key(db_session, owner, org_a, ["tickets:read"], project_ids=[proj_a.id])

    ok = await client.get(f"/api/v1/tickets/{tk_a}", headers=_hdrs(reader))
    assert ok.status_code == 200, ok.text
    assert ok.json()["recent_comments"][0]["content"] == "comment 0"
    scoped_ok = await client.get(f"/api/v1/projects/{proj_a.id}/tickets/{tk_a}", headers=_hdrs(reader))
    assert scoped_ok.status_code == 200

    # Other org, and outside the key's project allowlist: the project route
    # refuses (403); the by-id route hides the ticket with the same 404 as a
    # missing id.
    missing = await client.get(f"/api/v1/tickets/tk_{'f' * 16}", headers=_hdrs(reader))
    for project, tid in ((proj_b, tk_b), (proj_a2, tk_a2)):
        scoped = await client.get(f"/api/v1/projects/{project.id}/tickets/{tid}", headers=_hdrs(reader))
        assert scoped.status_code == 403
        by_id = await client.get(f"/api/v1/tickets/{tid}", headers=_hdrs(reader))
        assert by_id.status_code == 404
        assert by_id.json() == missing.json()

    # A service key without tickets:read is refused on both routes alike.
    writer_only = await _service_key(db_session, owner, org_a, ["knowledge:read"])
    scoped = await client.get(f"/api/v1/projects/{proj_a.id}/tickets/{tk_a}", headers=_hdrs(writer_only))
    by_id = await client.get(f"/api/v1/tickets/{tk_a}", headers=_hdrs(writer_only))
    assert scoped.status_code == by_id.status_code == 403
    assert by_id.json() == scoped.json()


@pytest.mark.asyncio
async def test_plan_gate_still_applies_for_readers(client: AsyncClient, db_session: AsyncSession):
    """A reader whose project plan lacks tickets gets the same upgrade error as
    the project-scoped route (they can see the project, so nothing leaks)."""
    owner, _owner_key = await _make_user(db_session, "owner", tier="pro")
    project = await _make_project(db_session, owner)
    from sessionfs.server.db.models import Ticket

    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="legacy",
        description="",
        priority="medium",
        created_by_user_id=owner.id,
        status="open",
    )
    db_session.add(ticket)
    await db_session.commit()

    scoped = await client.get(f"/api/v1/projects/{project.id}/tickets/{ticket.id}", headers=_hdrs(_owner_key))
    by_id = await client.get(f"/api/v1/tickets/{ticket.id}", headers=_hdrs(_owner_key))
    assert scoped.status_code == by_id.status_code == 403
    assert by_id.json() == scoped.json()
