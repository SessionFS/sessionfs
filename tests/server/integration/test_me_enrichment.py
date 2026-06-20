"""Integration tests for P2 /me enrichment — effective_tier + org_id + org_name + org_role."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Organization, OrgMember, User


@pytest.mark.asyncio
async def test_me_includes_effective_tier(client: AsyncClient, auth_headers: dict):
    """/me response includes effective_tier alongside legacy tier."""
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "effective_tier" in data
    assert "tier" in data
    assert data["effective_tier"] in ("free", "starter", "pro", "team", "enterprise")


@pytest.mark.asyncio
async def test_me_includes_org_fields_null_when_no_membership(
    client: AsyncClient, auth_headers: dict
):
    """/me org fields are null when the user has no org membership."""
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["org_id"] is None
    assert data["org_name"] is None
    assert data["org_role"] is None


@pytest.mark.asyncio
async def test_me_keeps_backward_compat_fields(client: AsyncClient, auth_headers: dict):
    """/me still returns legacy fields: tier, default_org_id."""
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "tier" in data
    assert "default_org_id" in data
    assert "user_id" in data
    assert "email" in data


@pytest.mark.asyncio
async def test_me_does_not_500_on_duplicate_membership(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """A user momentarily holding two OrgMember rows (the self-activation vs
    org-join race) must not 500 their own /me. /me uses .first() over a
    deterministic ORDER BY, so the earliest-joined membership is returned
    rather than raising MultipleResultsFound. (Shield LOW tk_29b3e43f1ee94130.)
    """
    now = datetime.now(timezone.utc)
    org_a = Organization(id="org_dup_a", name="Alpha Org", slug="alpha-dup", tier="team")
    org_b = Organization(id="org_dup_b", name="Beta Org", slug="beta-dup", tier="team")
    db_session.add_all([org_a, org_b])
    await db_session.flush()
    db_session.add_all(
        [
            OrgMember(
                org_id="org_dup_a",
                user_id=test_user.id,
                role="admin",
                joined_at=now - timedelta(days=2),
            ),
            OrgMember(
                org_id="org_dup_b",
                user_id=test_user.id,
                role="member",
                joined_at=now - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Earliest-joined membership wins deterministically — no 500.
    assert data["org_id"] == "org_dup_a"
    assert data["org_name"] == "Alpha Org"
    assert data["org_role"] == "admin"


@pytest.mark.asyncio
async def test_me_no_n_plus_one(client: AsyncClient, auth_headers: dict):
    """/me does not trigger N+1 queries — single request returns in one response."""
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    # All fields are present in one response — no pagination or follow-up needed.
    for field in ("user_id", "email", "effective_tier", "org_id", "org_name", "org_role"):
        assert field in data, f"Missing field: {field}"
