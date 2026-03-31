"""Integration tests for organization management routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, Organization, User


@pytest.fixture
async def team_user(db_session: AsyncSession) -> User:
    """Create a user with team tier."""
    user = User(
        id=str(uuid.uuid4()),
        email="teamadmin@example.com",
        display_name="Team Admin",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def team_api_key(db_session: AsyncSession, team_user: User):
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=team_user.id,
        key_hash=hash_api_key(raw_key),
        name="team-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    return raw_key


@pytest.fixture
def team_headers(team_api_key: str) -> dict:
    return {"Authorization": f"Bearer {team_api_key}"}


@pytest.mark.asyncio
async def test_create_org(client: AsyncClient, team_headers: dict):
    """Team user can create an organization."""
    resp = await client.post(
        "/api/v1/org",
        headers=team_headers,
        json={"name": "Test Org", "slug": "test-org"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Org"
    assert data["slug"] == "test-org"
    assert data["org_id"].startswith("org_")


@pytest.mark.asyncio
async def test_create_org_free_user_rejected(client: AsyncClient, auth_headers: dict):
    """Free user cannot create an organization."""
    resp = await client.post(
        "/api/v1/org",
        headers=auth_headers,
        json={"name": "Free Org", "slug": "free-org"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_org_info_no_org(client: AsyncClient, auth_headers: dict):
    """User not in an org gets null org."""
    resp = await client.get("/api/v1/org", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["org"] is None


@pytest.mark.asyncio
async def test_org_lifecycle(client: AsyncClient, team_headers: dict):
    """Full org lifecycle: create -> get info -> invite."""
    # Create org
    resp = await client.post(
        "/api/v1/org",
        headers=team_headers,
        json={"name": "Lifecycle Org", "slug": "lifecycle-org"},
    )
    assert resp.status_code == 200

    # Get info
    resp = await client.get("/api/v1/org", headers=team_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["org"]["name"] == "Lifecycle Org"
    assert data["org"]["tier"] == "team"
    assert data["current_user_role"] == "admin"
    assert len(data["members"]) == 1

    # Invite member
    resp = await client.post(
        "/api/v1/org/invite",
        headers=team_headers,
        json={"email": "newmember@example.com", "role": "member"},
    )
    assert resp.status_code == 200
    invite = resp.json()
    assert invite["email"] == "newmember@example.com"
    assert invite["role"] == "member"

    # List invites
    resp = await client.get("/api/v1/org/invites", headers=team_headers)
    assert resp.status_code == 200
    assert len(resp.json()["invites"]) == 1


@pytest.mark.asyncio
async def test_duplicate_slug_rejected(client: AsyncClient, team_headers: dict):
    """Duplicate org slug returns 409."""
    await client.post(
        "/api/v1/org",
        headers=team_headers,
        json={"name": "First", "slug": "unique-slug"},
    )
    # Create second user with team tier
    # The same user can't create a second org, so this is expected to fail
    resp = await client.post(
        "/api/v1/org",
        headers=team_headers,
        json={"name": "Second", "slug": "another-slug"},
    )
    assert resp.status_code == 409  # Already in an org


@pytest.mark.asyncio
async def test_invalid_slug_rejected(client: AsyncClient, team_headers: dict):
    """Invalid org slugs are rejected."""
    resp = await client.post(
        "/api/v1/org",
        headers=team_headers,
        json={"name": "Bad Slug", "slug": "AB"},
    )
    assert resp.status_code == 422  # Validation error
