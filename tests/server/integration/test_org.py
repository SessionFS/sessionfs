"""Integration tests for organization management routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import User


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
async def test_create_org_enterprise_user_no_stripe(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.24 tk_17b39010f9a64cba regression — enterprise user with
    no Stripe fields (every manual-license customer) used to hit a
    SQLAlchemy unit-of-work FK ordering bug that 500'd against
    PostgreSQL. The Stripe-paying path autoflushed the Organization
    row before OrgMember was queued; the no-stripe path did not, and
    SQLAlchemy didn't reliably topo-sort the two pending INSERTs.

    najitestech (GH #51, 2026-05-28) was the first enterprise customer
    to surface this. The fix is an explicit `await db.flush()` after
    `db.add(org)` in the route. This test exercises the no-stripe code
    path on SQLite with PRAGMA foreign_keys=ON so the FK violation
    surfaces locally without spinning up PostgreSQL.
    """
    from sqlalchemy import text

    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    # PRAGMA foreign_keys=ON makes SQLite enforce FKs like PostgreSQL.
    # Without this, SQLite silently accepts an OrgMember INSERT against
    # a missing Organization row and the bug doesn't reproduce in
    # tests even though it 500s in prod.
    await db_session.execute(text("PRAGMA foreign_keys = ON"))

    user = User(
        id=str(uuid.uuid4()),
        email="enterprise-no-stripe@example.com",
        display_name="Enterprise Customer",
        tier="enterprise",
        stripe_customer_id=None,
        stripe_subscription_id=None,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    raw_key = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="enterprise-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw_key}"}

    resp = await client.post(
        "/api/v1/org",
        headers=headers,
        json={"name": "Najite Global", "slug": "najite-global"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Najite Global"
    assert data["slug"] == "najite-global"
    assert data["org_id"].startswith("org_")

    # Both rows landed: Organization first (FK target), then OrgMember.
    org_row = (
        await db_session.execute(
            text("SELECT id, tier FROM organizations WHERE id = :id"),
            {"id": data["org_id"]},
        )
    ).one_or_none()
    assert org_row is not None
    assert org_row[1] == "enterprise"
    member_row = (
        await db_session.execute(
            text(
                "SELECT user_id, role FROM org_members "
                "WHERE org_id = :id AND user_id = :uid"
            ),
            {"id": data["org_id"], "uid": user.id},
        )
    ).one_or_none()
    assert member_row is not None
    assert member_row[1] == "admin"


@pytest.mark.asyncio
async def test_create_org_from_stripe_user_preserves_provenance(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 MEDIUM regression — when a Stripe-paying user creates an org,
    the route clears User.stripe_* (transfer of ownership) via a bulk
    update(User) that synchronizes the in-memory user object. The entitlement
    source/source_ref must be derived from a SNAPSHOT taken before the clear,
    or the org's entitlement is written as source='manual'/source_ref=None
    despite being genuinely Stripe-funded.
    """
    from sqlalchemy import text

    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    sub_id = "sub_provenance_123"
    user = User(
        id=str(uuid.uuid4()),
        email="stripe-payer@example.com",
        display_name="Stripe Payer",
        tier="team",
        stripe_customer_id="cus_provenance_123",
        stripe_subscription_id=sub_id,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    raw_key = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="stripe-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw_key}"}

    resp = await client.post(
        "/api/v1/org",
        headers=headers,
        json={"name": "Stripe Org", "slug": "stripe-org"},
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    # The org's active entitlement must carry the Stripe provenance.
    ent_row = (
        await db_session.execute(
            text(
                "SELECT source, source_ref FROM entitlements "
                "WHERE owner_type='org' AND owner_id=:id AND status='active'"
            ),
            {"id": org_id},
        )
    ).one_or_none()
    assert ent_row is not None
    assert ent_row[0] == "stripe", f"expected source='stripe', got {ent_row[0]!r}"
    assert ent_row[1] == sub_id, f"expected source_ref={sub_id!r}, got {ent_row[1]!r}"

    # And the user-level Stripe fields were transferred (cleared).
    user_row = (
        await db_session.execute(
            text("SELECT stripe_subscription_id FROM users WHERE id=:id"),
            {"id": user.id},
        )
    ).one_or_none()
    assert user_row is not None
    assert user_row[0] is None


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
