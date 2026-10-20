"""Org owners carry admin standing everywhere an org admin does.

Since v0.11.0 an org's owner holds ``OrgRole.OWNER``, which ranks above
``admin``. Several checks compared ``role == "admin"`` literally and so
rejected owners: Team checkout, the org billing portal, project
transfers (including an org whose only admin-level member is its owner
being an org→org target), and the project-admin branch of
``user_is_project_admin``. These tests pin owner standing on each, plus a
source grep so a new literal comparison can't slip back in.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    User,
)
from sessionfs.server.roles import ADMIN_ROLES, roles_at_least

# ── helpers ──


async def _make_user(db: AsyncSession, name: str) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
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
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_org(db: AsyncSession, members: list[tuple[User, str]]) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Owner Org",
        slug=f"owner-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    db.add(org)
    await db.commit()
    for user, role in members:
        db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()
    return org


async def _make_project(db: AsyncSession, owner: User, org_id: str | None) -> Project:
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="owner-project",
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        org_id=org_id,
    )
    db.add(proj)
    await db.commit()
    return proj


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class _FakeStripe:
    """Records the calls billing routes make; never touches the network."""

    def __init__(self) -> None:
        self.portal_customers: list[str] = []
        self.checkout_customers: list[str] = []

        fake = self

        class _Customer:
            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(id=f"cus_{uuid.uuid4().hex[:8]}")

            @staticmethod
            async def create_async(**kwargs):
                return _Customer.create(**kwargs)

        class _CheckoutSession:
            @staticmethod
            def create(**kwargs):
                fake.checkout_customers.append(kwargs["customer"])
                return SimpleNamespace(url="https://checkout.example/session")

            @staticmethod
            async def create_async(**kwargs):
                return _CheckoutSession.create(**kwargs)

            @staticmethod
            async def list_async(**kwargs):
                # The pre-checkout sweep finds nothing outstanding.
                return SimpleNamespace(data=[])

        class _Subscription:
            @staticmethod
            async def list_async(**kwargs):
                return SimpleNamespace(data=[])

        class _PortalSession:
            @staticmethod
            def create(**kwargs):
                fake.portal_customers.append(kwargs["customer"])
                return SimpleNamespace(url="https://portal.example/session")

            @staticmethod
            async def create_async(**kwargs):
                return _PortalSession.create(**kwargs)

        self.Customer = _Customer
        self.Subscription = _Subscription
        self.checkout = SimpleNamespace(Session=_CheckoutSession)
        self.billing_portal = SimpleNamespace(Session=_PortalSession)


@pytest.fixture
def fake_stripe(monkeypatch):
    from sessionfs.server.routes import billing

    fake = _FakeStripe()
    monkeypatch.setattr(billing, "_get_stripe", lambda: fake)
    monkeypatch.setitem(billing.TIER_PRICE_MAP, "team", "price_team_test")
    return fake


# ── role helpers ──


def test_admin_roles_include_owner():
    assert set(ADMIN_ROLES) == {"owner", "admin"}
    assert roles_at_least("owner") == ("owner",)
    assert roles_at_least("member") == ("owner", "admin", "member")


# ── billing ──


@pytest.mark.asyncio
async def test_owner_can_start_team_checkout(
    client: AsyncClient, db_session: AsyncSession, fake_stripe: _FakeStripe
) -> None:
    owner, key = await _make_user(db_session, "owner")
    await _make_org(db_session, [(owner, "owner")])

    resp = await client.post(
        "/api/v1/billing/checkout", json={"tier": "team", "seats": 3}, headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.example/session"
    assert len(fake_stripe.checkout_customers) == 1


@pytest.mark.asyncio
async def test_member_still_refused_team_checkout(
    client: AsyncClient, db_session: AsyncSession, fake_stripe: _FakeStripe
) -> None:
    owner, _ = await _make_user(db_session, "owner")
    member, key = await _make_user(db_session, "member")
    await _make_org(db_session, [(owner, "owner"), (member, "member")])

    resp = await client.post(
        "/api/v1/billing/checkout", json={"tier": "team"}, headers=_hdrs(key)
    )
    assert resp.status_code == 403, resp.text
    assert fake_stripe.checkout_customers == []


@pytest.mark.asyncio
async def test_owner_opens_org_billing_portal(
    client: AsyncClient, db_session: AsyncSession, fake_stripe: _FakeStripe
) -> None:
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session, [(owner, "owner")])
    org.stripe_customer_id = "cus_org_owner"
    await db_session.commit()

    resp = await client.post("/api/v1/billing/portal", json={}, headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    # Routed to the ORG customer, not down the non-admin path (which
    # would 403 because the owner has no personal subscription).
    assert fake_stripe.portal_customers == ["cus_org_owner"]


@pytest.mark.asyncio
async def test_member_without_personal_sub_refused_org_portal(
    client: AsyncClient, db_session: AsyncSession, fake_stripe: _FakeStripe
) -> None:
    owner, _ = await _make_user(db_session, "owner")
    member, key = await _make_user(db_session, "member")
    org = await _make_org(db_session, [(owner, "owner"), (member, "member")])
    org.stripe_customer_id = "cus_org_member"
    await db_session.commit()

    resp = await client.post("/api/v1/billing/portal", json={}, headers=_hdrs(key))
    assert resp.status_code == 403, resp.text
    assert fake_stripe.portal_customers == []


# ── project transfers ──


@pytest.mark.asyncio
async def test_owner_initiates_and_owner_only_org_is_org_to_org_target(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Source-org owner initiates; the destination org's ONLY admin-level
    member is its owner, who must be resolved as the target and accept."""
    src_owner, src_key = await _make_user(db_session, "srcowner")
    dst_owner, dst_key = await _make_user(db_session, "dstowner")
    src_org = await _make_org(db_session, [(src_owner, "owner")])
    # src_owner must belong to the destination org to initiate into it.
    dst_org = await _make_org(db_session, [(dst_owner, "owner"), (src_owner, "member")])
    project = await _make_project(db_session, owner=src_owner, org_id=src_org.id)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": dst_org.id},
        headers=_hdrs(src_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["target_user_id"] == dst_owner.id

    resp = await client.post(
        f"/api/v1/transfers/{body['id']}/accept", headers=_hdrs(dst_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "accepted"
    await db_session.refresh(project)
    assert project.org_id == dst_org.id


@pytest.mark.asyncio
async def test_org_with_only_members_is_still_not_a_target(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    src_owner, src_key = await _make_user(db_session, "srcowner")
    plain, _ = await _make_user(db_session, "plain")
    src_org = await _make_org(db_session, [(src_owner, "owner")])
    dst_org = await _make_org(db_session, [(plain, "member"), (src_owner, "member")])
    project = await _make_project(db_session, owner=src_owner, org_id=src_org.id)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": dst_org.id},
        headers=_hdrs(src_key),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_owner_initiates_org_to_personal(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, owner_key = await _make_user(db_session, "owner")
    bob, bob_key = await _make_user(db_session, "bob")
    org = await _make_org(db_session, [(owner, "owner"), (bob, "member")])
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target_user_id"] == bob.id


# ── project access ──


@pytest.mark.asyncio
async def test_owner_passes_project_admin_branch(db_session: AsyncSession) -> None:
    from sessionfs.server.auth.project_access import user_is_project_admin

    org_owner, _ = await _make_user(db_session, "orgowner")
    creator, _ = await _make_user(db_session, "creator")
    member, _ = await _make_user(db_session, "member")
    org = await _make_org(
        db_session, [(org_owner, "owner"), (creator, "member"), (member, "member")]
    )
    project = await _make_project(db_session, owner=creator, org_id=org.id)

    assert await user_is_project_admin(db_session, org_owner.id, project) is True
    assert await user_is_project_admin(db_session, member.id, project) is False


# ── source guard ──

# Literal role comparisons that are deliberately owner-exclusive (or
# compare against "owner" itself). Anything else must use
# has_minimum_role / ADMIN_ROLES. Format: (relative path, stripped line).
_ALLOWED_LITERAL_ADMIN = {
    # Last-owner successor must be an admin: the only owner is the user
    # being deactivated (one-owner-per-org index), and the successor is
    # promoted to owner.
    ("server/routes/admin.py", 'OrgMember.role == "admin",'),
    # Ownership can only be transferred to an existing admin (an owner
    # target would be the current owner).
    ("server/routes/org_members.py", 'if target_role != "admin":'),
    # Compares the REQUESTED new role (an owner demoting themselves to
    # admin), not the caller's standing.
    ("server/routes/org_members.py", 'if new_role == "admin":'),
    ("server/routes/org_members.py", 'if target_member is None or target_member.role != "admin":'),
    # GitHub's own org-membership role from the GitHub API, not a SessionFS
    # org role.
    ("server/services/github_oauth.py", 'and payload.get("role") == "admin"'),
}

_LITERAL_ADMIN_RE = re.compile(
    r"""role\b[^\n=!]{0,40}(==|!=)\s*['"]admin['"]|['"]admin['"]\s*(==|!=)\s*\S*role\b"""
)


def test_no_unintended_literal_admin_role_comparisons() -> None:
    src = Path(__file__).resolve().parents[3] / "src" / "sessionfs"
    offenders = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        if "/migrations/" in rel:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _LITERAL_ADMIN_RE.search(line) and (rel, stripped) not in _ALLOWED_LITERAL_ADMIN:
                offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "Literal admin role comparisons exclude org owners; use "
        "has_minimum_role(role, 'admin') or OrgMember.role.in_(ADMIN_ROLES):\n"
        + "\n".join(offenders)
    )
