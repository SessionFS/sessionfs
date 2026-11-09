"""A person can belong to several organizations (CEO decision 2026-11-09).

Covers invite acceptance into a second org, the same-org duplicate refusal,
per-org seat caps, auto-join into a second org, default-org selection and
movement on removal, /auth/me's `orgs` list, tier resolution (default org
for unnamed requests, the named org for /orgs/{org_id} routes) and that
service keys stay bound to one org.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgDomainVerification,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.auto_join import maybe_auto_join
from sessionfs.server.tier_gate import (
    build_org_user_context,
    get_effective_tier,
    get_user_org_membership,
)
from sessionfs.server.tiers import Tier


async def _org(db: AsyncSession, name: str, *, tier: str = "team", seats: int = 10) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=seats,
    )
    db.add(org)
    await db.commit()
    return org


async def _user(db: AsyncSession, email: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    return user


async def _headers(db: AsyncSession, user: User) -> dict[str, str]:
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="k"))
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def _join(
    db: AsyncSession, org: Organization, user: User, role: str = "member", *, minutes_ago: int = 0
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
    )
    await db.commit()


async def _invite(db: AsyncSession, org: Organization, email: str, inviter: User) -> OrgInvite:
    inv = OrgInvite(
        id=f"inv_{secrets.token_hex(8)}",
        org_id=org.id,
        email=email,
        role="member",
        invited_by=inviter.id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(inv)
    await db.commit()
    return inv


async def _memberships(db: AsyncSession, user_id: str) -> set[str]:
    return set(
        (
            await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user_id))
        ).scalars().all()
    )


async def _default(db: AsyncSession, user_id: str) -> str | None:
    return (
        await db.execute(select(User.default_org_id).where(User.id == user_id))
    ).scalar_one()


# ── invite acceptance ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_second_org_invite_keeps_first_membership_and_default(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    admin = await _user(db_session, "admin-b@example.com")
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    await _join(db_session, org_a, test_user, minutes_ago=60)
    await _join(db_session, org_b, admin, "owner")
    inv = await _invite(db_session, org_b, test_user.email, admin)

    resp = await client.post(f"/api/v1/org/invite/{inv.id}/accept", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == org_b.id
    # The read-time default (earliest joined) is still the first org, and
    # joining never writes the stored pointer.
    assert body["default_org_id"] == org_a.id

    assert await _memberships(db_session, test_user.id) == {org_a.id, org_b.id}
    assert await _default(db_session, test_user.id) is None


@pytest.mark.asyncio
async def test_first_invite_accept_leaves_stored_default_alone(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    admin = await _user(db_session, "admin@example.com")
    org = await _org(db_session, "Acme")
    await _join(db_session, org, admin, "owner")
    inv = await _invite(db_session, org, test_user.email, admin)

    resp = await client.post(f"/api/v1/org/invite/{inv.id}/accept", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    # Resolved at read time; the stored pointer stays NULL so `sfs project
    # init` without a flag keeps creating personal projects, as before.
    assert resp.json()["default_org_id"] == org.id
    assert await _default(db_session, test_user.id) is None
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert me["default_org_id"] is None
    assert me["org_id"] == org.id


@pytest.mark.asyncio
async def test_accept_invite_to_org_already_member_is_409(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    admin = await _user(db_session, "admin@example.com")
    org = await _org(db_session, "Acme")
    await _join(db_session, org, admin, "owner")
    await _join(db_session, org, test_user)
    inv = await _invite(db_session, org, test_user.email, admin)

    resp = await client.post(f"/api/v1/org/invite/{inv.id}/accept", headers=auth_headers)
    assert resp.status_code == 409
    assert "already a member of this organization" in resp.text
    rows = (
        await db_session.execute(
            select(OrgMember).where(
                OrgMember.org_id == org.id, OrgMember.user_id == test_user.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_seat_cap_is_per_org(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    """A full second org refuses the join; the first org's free seats don't
    count, and the user's existing membership doesn't use a seat there."""
    admin = await _user(db_session, "admin@example.com")
    org_a = await _org(db_session, "Acme", seats=50)
    org_b = await _org(db_session, "Globex", seats=1)
    await _join(db_session, org_a, test_user)
    await _join(db_session, org_b, admin, "owner")  # fills Globex's only seat
    inv = await _invite(db_session, org_b, test_user.email, admin)

    resp = await client.post(f"/api/v1/org/invite/{inv.id}/accept", headers=auth_headers)
    assert resp.status_code == 403
    assert "seat_limit" in resp.text
    assert await _memberships(db_session, test_user.id) == {org_a.id}

    # Raise Globex's cap: the same invite now works.
    org_b.seats_limit = 2
    await db_session.commit()
    resp = await client.post(f"/api/v1/org/invite/{inv.id}/accept", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert await _memberships(db_session, test_user.id) == {org_a.id, org_b.id}


# ── domain auto-join ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_join_second_org(db_session: AsyncSession):
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    db_session.add(
        OrgDomainVerification(
            id=f"odv_{secrets.token_hex(12)}",
            org_id=org_b.id,
            domain="globex.com",
            method="dns_txt",
            verification_token=secrets.token_urlsafe(16),
            status="verified",
            verified_at=datetime.now(timezone.utc),
        )
    )
    org_b.settings = json.dumps({"general": {"auto_join_domains": ["globex.com"]}})
    await db_session.commit()

    user = await _user(db_session, "pat@globex.com")
    await _join(db_session, org_a, user, "admin", minutes_ago=30)

    await maybe_auto_join(user, db_session)

    roles = dict(
        (
            await db_session.execute(
                select(OrgMember.org_id, OrgMember.role).where(OrgMember.user_id == user.id)
            )
        ).all()
    )
    assert roles == {org_a.id: "admin", org_b.id: "member"}
    assert await _default(db_session, user.id) is None


@pytest.mark.asyncio
async def test_auto_join_first_org_leaves_stored_default_alone(db_session: AsyncSession):
    org = await _org(db_session, "Globex")
    db_session.add(
        OrgDomainVerification(
            id=f"odv_{secrets.token_hex(12)}",
            org_id=org.id,
            domain="globex.com",
            method="dns_txt",
            verification_token=secrets.token_urlsafe(16),
            status="verified",
            verified_at=datetime.now(timezone.utc),
        )
    )
    org.settings = json.dumps({"general": {"auto_join_domains": ["globex.com"]}})
    await db_session.commit()
    user = await _user(db_session, "pat@globex.com")

    await maybe_auto_join(user, db_session)

    assert await _memberships(db_session, user.id) == {org.id}
    assert await _default(db_session, user.id) is None
    assert (await get_user_org_membership(user.id, db_session)).org_id == org.id


# ── default-org selection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_org_selection_rules(db_session: AsyncSession):
    user = await _user(db_session, "pat@example.com")
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    await _join(db_session, org_b, user, minutes_ago=120)  # earliest joined
    await _join(db_session, org_a, user, minutes_ago=10)

    # No pointer → earliest joined.
    assert (await get_user_org_membership(user.id, db_session)).org_id == org_b.id
    # Pointer at a membership → that org.
    user.default_org_id = org_a.id
    await db_session.commit()
    assert (await get_user_org_membership(user.id, db_session)).org_id == org_a.id
    # Pointer at an org they are no longer in → earliest joined again.
    org_c = await _org(db_session, "Initech")
    user.default_org_id = org_c.id
    await db_session.commit()
    assert (await get_user_org_membership(user.id, db_session)).org_id == org_b.id


@pytest.mark.asyncio
async def test_set_default_org_requires_membership(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    await _join(db_session, org_a, test_user)

    resp = await client.put(
        "/api/v1/auth/me/default-org", headers=auth_headers, json={"org_id": org_b.id}
    )
    assert resp.status_code == 403
    await _join(db_session, org_b, test_user)
    resp = await client.put(
        "/api/v1/auth/me/default-org", headers=auth_headers, json={"org_id": org_b.id}
    )
    assert resp.status_code == 200
    assert resp.json()["default_org_id"] == org_b.id


# ── removal ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removal_from_default_org_keeps_other_and_clears_default(
    client: AsyncClient, db_session: AsyncSession
):
    admin = await _user(db_session, "admin@example.com")
    admin_headers = await _headers(db_session, admin)
    member = await _user(db_session, "pat@example.com")
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    org_c = await _org(db_session, "Initech")
    await _join(db_session, org_a, admin, "owner")
    await _join(db_session, org_a, member, minutes_ago=300)
    await _join(db_session, org_c, member, minutes_ago=100)
    await _join(db_session, org_b, member, minutes_ago=200)
    member.default_org_id = org_a.id
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/orgs/{org_a.id}/members/{member.id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    assert await _memberships(db_session, member.id) == {org_b.id, org_c.id}
    # The stored pointer is cleared; the read-time rule picks the earliest
    # joined of the rest (Globex).
    assert await _default(db_session, member.id) is None
    assert (await get_user_org_membership(member.id, db_session)).org_id == org_b.id


@pytest.mark.asyncio
async def test_removal_from_non_default_org_keeps_default(
    client: AsyncClient, db_session: AsyncSession
):
    admin = await _user(db_session, "admin@example.com")
    admin_headers = await _headers(db_session, admin)
    member = await _user(db_session, "pat@example.com")
    org_a = await _org(db_session, "Acme")
    org_b = await _org(db_session, "Globex")
    await _join(db_session, org_b, admin, "owner")
    await _join(db_session, org_a, member)
    await _join(db_session, org_b, member)
    member.default_org_id = org_a.id
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/orgs/{org_b.id}/members/{member.id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert await _memberships(db_session, member.id) == {org_a.id}
    assert await _default(db_session, member.id) == org_a.id


@pytest.mark.asyncio
async def test_removal_from_last_org_clears_default(
    client: AsyncClient, db_session: AsyncSession
):
    admin = await _user(db_session, "admin@example.com")
    admin_headers = await _headers(db_session, admin)
    member = await _user(db_session, "pat@example.com")
    org = await _org(db_session, "Acme")
    await _join(db_session, org, admin, "owner")
    await _join(db_session, org, member)
    member.default_org_id = org.id
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{member.id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert await _memberships(db_session, member.id) == set()
    assert await _default(db_session, member.id) is None


# ── /auth/me ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_lists_every_org_and_describes_the_default(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org_a = await _org(db_session, "Acme", tier="team")
    org_b = await _org(db_session, "Globex", tier="enterprise")
    await _join(db_session, org_a, test_user, "member", minutes_ago=60)
    await _join(db_session, org_b, test_user, "admin", minutes_ago=5)

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert [(o["org_id"], o["role"], o["is_default"]) for o in me["orgs"]] == [
        (org_a.id, "member", True),
        (org_b.id, "admin", False),
    ]
    assert me["org_id"] == org_a.id
    assert me["org_role"] == "member"
    assert me["effective_tier"] == "team"

    resp = await client.put(
        "/api/v1/auth/me/default-org", headers=auth_headers, json={"org_id": org_b.id}
    )
    assert resp.status_code == 200
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert me["org_id"] == org_b.id
    assert me["org_name"] == "Globex"
    assert me["org_role"] == "admin"
    assert me["effective_tier"] == "enterprise"
    assert [o["is_default"] for o in me["orgs"]] == [False, True]


@pytest.mark.asyncio
async def test_me_without_orgs(client: AsyncClient, auth_headers: dict):
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert me["orgs"] == []
    assert me["org_id"] is None


# ── tier resolution ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unnamed_tier_uses_default_org_not_best_org(db_session: AsyncSession):
    user = await _user(db_session, "pat@example.com")
    org_team = await _org(db_session, "Acme", tier="team")
    org_ent = await _org(db_session, "Globex", tier="enterprise")
    await _join(db_session, org_team, user, minutes_ago=60)
    await _join(db_session, org_ent, user, minutes_ago=5)

    # Default (earliest joined) is the Team org — no escalation to Enterprise.
    assert await get_effective_tier(user, db_session) == Tier.TEAM
    user.default_org_id = org_ent.id
    await db_session.commit()
    assert await get_effective_tier(user, db_session) == Tier.ENTERPRISE


@pytest.mark.asyncio
async def test_named_org_context_uses_that_org(db_session: AsyncSession):
    user = await _user(db_session, "pat@example.com")
    org_free = await _org(db_session, "Acme", tier="free")
    org_ent = await _org(db_session, "Globex", tier="enterprise")
    stranger = await _org(db_session, "Initech", tier="enterprise")
    await _join(db_session, org_free, user, "member", minutes_ago=60)
    await _join(db_session, org_ent, user, "admin", minutes_ago=5)

    ctx = await build_org_user_context(user, org_ent.id, db_session)
    assert ctx is not None
    assert ctx.org.id == org_ent.id
    assert ctx.role == "admin"
    assert ctx.effective_tier == Tier.ENTERPRISE
    assert await build_org_user_context(user, stranger.id, db_session) is None


@pytest.mark.asyncio
async def test_org_admin_routes_follow_the_named_org(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    """An admin of a SECOND org manages its service keys and trusted
    reviewers even though their default org is another one where they are a
    plain member on a lower plan."""
    org_a = await _org(db_session, "Acme", tier="free")
    org_b = await _org(db_session, "Globex", tier="team")
    await _join(db_session, org_a, test_user, "member", minutes_ago=60)
    await _join(db_session, org_b, test_user, "admin", minutes_ago=5)
    test_user.default_org_id = org_a.id
    await db_session.commit()

    resp = await client.get(f"/api/v1/orgs/{org_b.id}/service-keys", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/v1/orgs/{org_b.id}/trusted-reviewers", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    # Their default org: a plain member on Free gets the plan refusal there.
    resp = await client.get(f"/api/v1/orgs/{org_a.id}/service-keys", headers=auth_headers)
    assert resp.status_code == 403

    # A service key minted for Globex is bound to Globex only.
    resp = await client.post(
        f"/api/v1/orgs/{org_b.id}/service-keys",
        headers=auth_headers,
        json={"name": "ci", "scopes": ["tickets:read"]},
    )
    assert resp.status_code in (200, 201), resp.text
    key_id = resp.json()["id"] if "id" in resp.json() else resp.json()["key"]["id"]
    row = (await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()
    assert row.key_kind == "service"
    assert row.org_id == org_b.id


@pytest.mark.asyncio
async def test_non_member_named_org_admin_route_is_404(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org_a = await _org(db_session, "Acme", tier="team")
    org_b = await _org(db_session, "Globex", tier="team")
    await _join(db_session, org_a, test_user, "admin")

    resp = await client.get(f"/api/v1/orgs/{org_b.id}/service-keys", headers=auth_headers)
    assert resp.status_code == 404


# ── org creation / activation while in another org ─────────────────────


@pytest.mark.asyncio
async def test_member_of_another_org_with_own_team_plan_can_create_org(
    client: AsyncClient, db_session: AsyncSession
):
    user = await _user(db_session, "founder@example.com")
    user.tier = "team"
    await db_session.commit()
    headers = await _headers(db_session, user)
    org_a = await _org(db_session, "Acme")
    await _join(db_session, org_a, user, "member", minutes_ago=60)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Side Project", "slug": "side-project-x"}
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["org_id"]
    roles = dict(
        (
            await db_session.execute(
                select(OrgMember.org_id, OrgMember.role).where(OrgMember.user_id == user.id)
            )
        ).all()
    )
    assert roles == {org_a.id: "member", new_id: "owner"}
    assert await _default(db_session, user.id) is None


@pytest.mark.asyncio
async def test_member_on_free_personal_plan_cannot_create_org(
    client: AsyncClient, db_session: AsyncSession
):
    """Membership in a paid org never funds a new org: the creator's own plan
    decides."""
    user = await _user(db_session, "member@example.com")
    headers = await _headers(db_session, user)
    org_a = await _org(db_session, "Acme", tier="enterprise")
    await _join(db_session, org_a, user, "admin")

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Free Ride", "slug": "free-ride-x"}
    )
    assert resp.status_code == 403
    assert await _memberships(db_session, user.id) == {org_a.id}


@pytest.mark.asyncio
async def test_create_first_org_leaves_default_null_as_before(
    client: AsyncClient, db_session: AsyncSession
):
    """Creating an org never wrote default_org_id before multi-org; it still
    doesn't (the read-time rule resolves the only membership)."""
    user = await _user(db_session, "founder@example.com")
    user.tier = "team"
    await db_session.commit()
    headers = await _headers(db_session, user)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Solo", "slug": "solo-org-x"}
    )
    assert resp.status_code == 200, resp.text
    assert await _default(db_session, user.id) is None
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["org_id"] == resp.json()["org_id"]
    assert me["default_org_id"] is None


# ── teams follow the team's org ────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_routes_follow_the_teams_org(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    from sessionfs.server.db.models import Team

    org_a = await _org(db_session, "Acme", tier="free")
    org_b = await _org(db_session, "Globex", tier="team")
    stranger = await _org(db_session, "Initech", tier="team")
    await _join(db_session, org_a, test_user, "member", minutes_ago=60)
    await _join(db_session, org_b, test_user, "admin", minutes_ago=5)
    test_user.default_org_id = org_a.id
    team_b = Team(id=f"team_{uuid.uuid4().hex[:12]}", org_id=org_b.id, name="Ops", slug="ops")
    team_x = Team(id=f"team_{uuid.uuid4().hex[:12]}", org_id=stranger.id, name="X", slug="x")
    db_session.add_all([team_b, team_x])
    await db_session.commit()

    resp = await client.get(f"/api/v1/teams/{team_b.id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/v1/teams/{team_b.id}/members", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    # A team in an org the caller isn't in answers exactly as before: the
    # default org (Free) refuses on plan first.
    resp = await client.get(f"/api/v1/teams/{team_x.id}", headers=auth_headers)
    assert resp.status_code in (403, 404)
    test_user.default_org_id = org_b.id
    await db_session.commit()
    resp = await client.get(f"/api/v1/teams/{team_x.id}", headers=auth_headers)
    assert resp.status_code == 404
