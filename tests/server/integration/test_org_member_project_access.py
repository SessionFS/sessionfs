"""v0.10.22 — org-member project access regression tests.

Pins the predicate change in `auth/project_access.user_can_access_project`
that closes tk_7a457574c5624e12:

A user key reaches a project iff ANY of these holds:
  1. Owns it.
  2. Member of the project's org.   <-- ADDED in v0.10.22
  3. Has captured a session on the project's git remote (legacy
     fallback for personal projects).

Before this fix, a brand-new org member who had not yet cloned the
repo got 403 on every org-scoped artifact: personas, KB entries,
wiki pages, tickets, agent runs. The model.py comment at line 187
documented the intended predicate; the code never matched it.

These tests lock the new behavior on both the per-route read gates
and on the project listing endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    KnowledgeEntry,
    KnowledgePage,
    OrgMember,
    Organization,
    Project,
    User,
)


# ── helpers ──


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(
    db: AsyncSession, *, email: str | None = None, tier: str = "pro"
) -> tuple[User, dict[str, str]]:
    user = User(
        id=str(uuid.uuid4()),
        email=email or f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="member",
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"key-{uuid.uuid4().hex[:6]}",
            created_at=_now(),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _make_org(db: AsyncSession, *, tier: str = "team") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier=tier,
    )
    db.add(org)
    await db.commit()
    return org


async def _add_org_member(
    db: AsyncSession, *, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            invited_by=user.id,
            invited_at=_now(),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession,
    *,
    owner: User,
    org: Organization | None = None,
    git_remote: str | None = None,
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Test Project",
        git_remote_normalized=git_remote or f"github.com/test/{uuid.uuid4().hex[:8]}",
        context_document="# ctx",
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


# ── read-gate tests (each maps to one of the 6 affected route families) ──


@pytest.mark.asyncio
async def test_org_member_can_list_personas(client, db_session: AsyncSession):
    """The headline symptom from the 2026-05-23 incident: a brand-new
    org member who has captured no sessions can `GET /personas` on an
    org-scoped project they're a member of."""
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            name="scout",
            role="Analyst",
            content="# scout",
            specializations="[]",
            is_active=True,
            version=1,
            created_by=owner.id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=member_hdr
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "scout"


@pytest.mark.asyncio
async def test_org_member_can_read_knowledge_entries(
    client, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    db_session.add(
        KnowledgeEntry(
            project_id=project.id,
            session_id=f"ses_{uuid.uuid4().hex[:12]}",
            user_id=owner.id,
            entry_type="decision",
            content="canonical decision",
            confidence=0.9,
            created_at=_now(),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/entries", headers=member_hdr
    )
    assert resp.status_code == 200, resp.text
    assert any(e["content"] == "canonical decision" for e in resp.json())


@pytest.mark.asyncio
async def test_org_member_can_read_wiki_pages(client, db_session: AsyncSession):
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    db_session.add(
        KnowledgePage(
            id=f"kp_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            slug="overview",
            title="Overview",
            content="# Overview\n\nproject.",
            page_type="topic",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/pages", headers=member_hdr
    )
    assert resp.status_code == 200, resp.text
    titles = [p["title"] for p in resp.json()]
    assert "Overview" in titles


@pytest.mark.asyncio
async def test_org_member_can_list_tickets(client, db_session: AsyncSession):
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets", headers=member_hdr
    )
    # 200 (possibly empty list) is the correct shape — 403 is the bug.
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_org_member_can_list_agent_runs(client, db_session: AsyncSession):
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs", headers=member_hdr
    )
    assert resp.status_code == 200, resp.text


# ── project-listing test (the dashboard / `sfs project` discoverability hole) ──


@pytest.mark.asyncio
async def test_org_member_sees_org_scoped_project_in_listing(
    client, db_session: AsyncSession
):
    """`GET /api/v1/projects` previously only OR'd owner OR
    own-session-remotes. A new org member would see an empty list
    even though they had every right to read the team's projects."""
    owner, _ = await _make_user(db_session)
    member, member_hdr = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org=org, user=member)
    project = await _make_project(db_session, owner=owner, org=org)

    resp = await client.get("/api/v1/projects/", headers=member_hdr)
    assert resp.status_code == 200, resp.text
    ids = [p["id"] for p in resp.json()]
    assert project.id in ids


# ── isolation tests — cross-org and personal-project still gate correctly ──


@pytest.mark.asyncio
async def test_non_member_cross_org_denied(client, db_session: AsyncSession):
    """Member of org A must NOT reach a project scoped to org B even
    when they know the project_id. The fix must not regress isolation."""
    owner, _ = await _make_user(db_session)
    outsider, outsider_hdr = await _make_user(db_session)
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    await _add_org_member(db_session, org=org_a, user=outsider)
    project_in_b = await _make_project(db_session, owner=owner, org=org_b)

    resp = await client.get(
        f"/api/v1/projects/{project_in_b.id}/personas", headers=outsider_hdr
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_personal_project_unchanged_no_session_no_access(
    client, db_session: AsyncSession
):
    """A project with org_id IS NULL keeps the legacy
    owner-or-captured-session predicate. An unrelated user gets 403,
    same as before — org-aware predicate adds access; it does not
    remove existing checks."""
    owner, _ = await _make_user(db_session)
    other, other_hdr = await _make_user(db_session)
    project = await _make_project(db_session, owner=owner, org=None)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=other_hdr
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_owner_still_has_access_without_org_membership(
    client, db_session: AsyncSession
):
    """Owner reaches their personal project regardless of org state.
    Pins the first predicate; the legacy path must keep working."""
    owner, owner_hdr = await _make_user(db_session)
    project = await _make_project(db_session, owner=owner, org=None)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=owner_hdr
    )
    assert resp.status_code == 200, resp.text
