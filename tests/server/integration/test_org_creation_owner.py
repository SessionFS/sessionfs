"""Every org-creation path makes exactly one owner (tk_d311dcf71be74a97).

Since v0.11.0 an org must have exactly one owner. `POST /api/v1/org`
(`sfs org create`) and `POST /api/v1/admin/orgs` used to insert the creator
as "admin", leaving the org ownerless (no owner transfer, no break-glass, no
SSO-enforcement owner exemption). License activation already created an
owner and is covered in test_activation.py.

SSO JIT provisioning, domain auto-join and invite acceptance only add
members to existing orgs and are capped below owner; they are not creation
paths.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, HelmLicense, Organization, OrgMember, User


async def _user(db: AsyncSession, email: str, tier: str = "team") -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=email.split("@")[0],
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    return user


async def _headers(db: AsyncSession, user: User) -> dict[str, str]:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="k",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def _members(db: AsyncSession, org_id: str) -> list[tuple[str, str]]:
    rows = await db.execute(
        select(OrgMember.user_id, OrgMember.role).where(OrgMember.org_id == org_id)
    )
    return [(r[0], r[1]) for r in rows.all()]


@pytest.mark.asyncio
async def test_create_org_makes_creator_the_single_owner(
    client: AsyncClient, db_session: AsyncSession
):
    creator = await _user(db_session, "creator@example.com")
    headers = await _headers(db_session, creator)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Acme", "slug": "acme-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    members = await _members(db_session, org_id)
    assert members == [(creator.id, "owner")]

    info = await client.get("/api/v1/org", headers=headers)
    assert info.json()["current_user_role"] == "owner"


@pytest.mark.asyncio
async def test_created_org_owner_can_use_owner_only_actions(
    client: AsyncClient, db_session: AsyncSession
):
    """Ownership transfer is owner-only; it used to 403 for the creator."""
    creator = await _user(db_session, "transfer-creator@example.com")
    headers = await _headers(db_session, creator)
    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Xfer", "slug": "xfer-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    admin = await _user(db_session, "second-admin@example.com")
    db_session.add(OrgMember(org_id=org_id, user_id=admin.id, role="admin"))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org_id}/owner/transfer",
        headers=headers,
        json={"to_user_id": admin.id},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_admin_create_org_makes_named_user_the_single_owner(
    client: AsyncClient, db_session: AsyncSession
):
    platform_admin = await _user(db_session, "platform@sessionfs.dev", tier="admin")
    headers = await _headers(db_session, platform_admin)
    target = await _user(db_session, "named-owner@example.com", tier="free")

    resp = await client.post(
        "/api/v1/admin/orgs",
        headers=headers,
        json={"name": "Provisioned", "slug": "provisioned-owner", "owner_user_id": target.id},
    )
    assert resp.status_code == 201, resp.text
    org_id = resp.json()["id"]

    members = await _members(db_session, org_id)
    assert members == [(target.id, "owner")]
    # The platform admin who ran the request is not made a member.
    assert all(uid != platform_admin.id for uid, _ in members)


@pytest.mark.asyncio
async def test_admin_create_org_with_license_makes_named_user_owner(
    client: AsyncClient, db_session: AsyncSession
):
    platform_admin = await _user(db_session, "platform2@sessionfs.dev", tier="admin")
    headers = await _headers(db_session, platform_admin)
    target = await _user(db_session, "licensed-owner@example.com", tier="free")
    lic = HelmLicense(
        id=f"sfs_hl_{uuid.uuid4().hex}",
        org_name="Licensed Co",
        contact_email="licensed-owner@example.com",
        tier="enterprise",
        seats_limit=50,
        status="active",
    )
    db_session.add(lic)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/orgs",
        headers=headers,
        json={
            "name": "Licensed Co",
            "slug": "licensed-owner",
            "owner_user_id": target.id,
            "license_key": lic.id,
        },
    )
    assert resp.status_code == 201, resp.text
    members = await _members(db_session, resp.json()["id"])
    assert members == [(target.id, "owner")]


@pytest.mark.asyncio
async def test_admin_create_org_rejects_an_inactive_owner(
    client: AsyncClient, db_session: AsyncSession
):

    platform_admin = await _user(db_session, "platform3@sessionfs.dev", tier="admin")
    headers = await _headers(db_session, platform_admin)
    target = await _user(db_session, "deactivated-owner@example.com", tier="free")
    target.is_active = False
    await db_session.commit()
    target_id = target.id

    resp = await client.post(
        "/api/v1/admin/orgs",
        headers=headers,
        json={"name": "Dead Owner", "slug": "dead-owner", "owner_user_id": target_id},
    )
    assert resp.status_code == 422, resp.text
    assert "inactive" in resp.text

    orgs = (
        await db_session.execute(select(Organization.id).where(Organization.slug == "dead-owner"))
    ).all()
    assert orgs == []
    memberships = (
        await db_session.execute(select(OrgMember.id).where(OrgMember.user_id == target_id))
    ).all()
    assert memberships == []


@pytest.mark.asyncio
async def test_stripe_checkout_then_org_create_moves_the_subscription_entitlement(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A Team checkout creates a user entitlement bound to (stripe, sub_id).

    Creating the org afterwards must move that binding to the org rather
    than insert a second row with the same (source, source_ref), which
    violated uq_entitlements_source_ref and rolled back the whole create.
    """
    from types import SimpleNamespace

    from sessionfs.server.db.models import Entitlement
    from sessionfs.server.routes.billing import _handle_checkout_completed

    buyer = await _user(db_session, "stripe-buyer@example.com", tier="free")
    buyer.stripe_customer_id = "cus_checkout_org"
    await db_session.commit()
    headers = await _headers(db_session, buyer)
    buyer_id = buyer.id

    stripe_fake.subscription("sub_checkout_org", tier="team", quantity=5, customer="cus_checkout_org")
    metadata = {"user_id": buyer_id, "tier": "team", "seats": "5"}
    session_obj = SimpleNamespace(
        metadata=metadata,
        subscription="sub_checkout_org",
        get=lambda key, default=None: {"customer": "cus_checkout_org"}.get(key, default),
    )
    await _handle_checkout_completed(
        SimpleNamespace(data=SimpleNamespace(object=session_obj), id="evt_checkout_org"),
        db_session,
    )

    before = (
        await db_session.execute(
            select(Entitlement.id, Entitlement.owner_type, Entitlement.owner_id).where(
                Entitlement.source == "stripe",
                Entitlement.source_ref == "sub_checkout_org",
            )
        )
    ).all()
    assert [(r[1], r[2]) for r in before] == [("user", buyer_id)]
    ent_id = before[0][0]

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Paid Co", "slug": "paid-co-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(
                Entitlement.id,
                Entitlement.owner_type,
                Entitlement.owner_id,
                Entitlement.status,
                Entitlement.tier,
            ).where(Entitlement.source == "stripe", Entitlement.source_ref == "sub_checkout_org")
        )
    ).all()
    # Same row, re-owned by the org.
    assert [tuple(r) for r in rows] == [(ent_id, "org", org_id, "active", "team")]

    user_row = (
        await db_session.execute(
            select(User.entitlement_id, User.tier, User.stripe_subscription_id).where(
                User.id == buyer_id
            )
        )
    ).one()
    assert user_row[0] is None
    assert user_row[1] == "free"
    assert user_row[2] is None

    org_row = (
        await db_session.execute(
            select(Organization.entitlement_id, Organization.tier, Organization.stripe_subscription_id)
            .where(Organization.id == org_id)
        )
    ).one()
    assert tuple(org_row) == (ent_id, "team", "sub_checkout_org")
    assert await _members(db_session, org_id) == [(buyer_id, "owner")]


@pytest.mark.asyncio
async def test_org_create_refuses_a_subscription_bound_to_someone_else(
    client: AsyncClient, db_session: AsyncSession
):
    """If the user's subscription id is already bound to another owner's
    entitlement, org creation answers 409 instead of a unique-index 500."""
    from sessionfs.server.db.models import Entitlement

    other = await _user(db_session, "other-holder@example.com", tier="team")
    db_session.add(
        Entitlement(
            owner_type="user",
            owner_id=other.id,
            source="stripe",
            source_ref="sub_shared_conflict",
            tier="team",
            status="active",
        )
    )
    buyer = await _user(db_session, "conflict-buyer@example.com", tier="team")
    buyer.stripe_customer_id = "cus_conflict"
    buyer.stripe_subscription_id = "sub_shared_conflict"
    await db_session.commit()
    headers = await _headers(db_session, buyer)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Clash", "slug": "clash-owner"}
    )
    assert resp.status_code == 409, resp.text
    orgs = (
        await db_session.execute(select(Organization.id).where(Organization.slug == "clash-owner"))
    ).all()
    assert orgs == []


@pytest.mark.asyncio
async def test_org_create_refuses_an_ambiguous_subscription_pointer(
    client: AsyncClient, db_session: AsyncSession
):
    """Two owners sharing one subscription pointer (migration 075 leaves it
    unbound for manual repair) must not let the first org creator rebind
    their manual entitlement and steal the subscription: 409, a
    reconciliation row, and both users' Stripe pointers untouched."""
    from sessionfs.server.db.models import BillingReconciliation, Entitlement

    alice = await _user(db_session, "alice-ambiguous@example.com", tier="team")
    alice.stripe_customer_id = "cus_ambiguous_a"
    alice.stripe_subscription_id = "sub_ambiguous_shared"
    db_session.add(
        Entitlement(
            owner_type="user",
            owner_id=alice.id,
            source="manual",
            source_ref=None,
            tier="team",
            status="active",
        )
    )
    bob = await _user(db_session, "bob-ambiguous@example.com", tier="team")
    bob.stripe_customer_id = "cus_ambiguous_b"
    bob.stripe_subscription_id = "sub_ambiguous_shared"
    await db_session.commit()
    headers = await _headers(db_session, alice)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Ambiguous", "slug": "ambiguous-owner"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["error"] == "ambiguous_subscription_pointer"

    # No org was created.
    orgs = (
        await db_session.execute(select(Organization.id).where(Organization.slug == "ambiguous-owner"))
    ).all()
    assert orgs == []

    # A durable reconciliation row flags the manual repair.
    rec = (
        await db_session.execute(
            select(BillingReconciliation).where(
                BillingReconciliation.kind == "ambiguous_subscription_pointer"
            )
        )
    ).scalar_one_or_none()
    assert rec is not None
    assert rec.stripe_subscription_id == "sub_ambiguous_shared"

    # Neither owner's Stripe pointers were wiped (especially Bob's customer
    # pointer, which the webhook path's _clear_legacy_user_pointers would
    # have cleared had the adoption been granted).
    alice_id, bob_id = alice.id, bob.id
    pointers = (
        await db_session.execute(
            select(User.id, User.stripe_customer_id, User.stripe_subscription_id).where(
                User.id.in_([alice_id, bob_id])
            )
        )
    ).all()
    by_id = {r[0]: (r[1], r[2]) for r in pointers}
    assert by_id[alice_id] == ("cus_ambiguous_a", "sub_ambiguous_shared")
    assert by_id[bob_id] == ("cus_ambiguous_b", "sub_ambiguous_shared")

    # Alice's manual entitlement was not rebound to Stripe.
    ent = (
        await db_session.execute(
            select(Entitlement).where(
                Entitlement.owner_type == "user",
                Entitlement.owner_id == alice_id,
            )
        )
    ).scalar_one_or_none()
    assert ent is not None
    assert ent.source == "manual"
    assert ent.source_ref is None


async def _entitlements(db: AsyncSession) -> list[tuple]:
    from sessionfs.server.db.models import Entitlement

    db.expire_all()
    rows = await db.execute(
        select(
            Entitlement.id,
            Entitlement.owner_type,
            Entitlement.owner_id,
            Entitlement.status,
            Entitlement.source,
            Entitlement.source_ref,
        ).order_by(Entitlement.id)
    )
    return [tuple(r) for r in rows.all()]


@pytest.mark.asyncio
async def test_org_create_moves_a_050_legacy_manual_entitlement(
    client: AsyncClient, db_session: AsyncSession
):
    """Migration 050 backfilled paid users as source='manual', source_ref=NULL
    even when they had a Stripe subscription. Org creation must move that
    row (rebinding it to the subscription) instead of leaving the user with
    an active paid entitlement next to a new org row."""
    from sessionfs.server.db.models import Entitlement

    buyer = await _user(db_session, "legacy-paid@example.com", tier="team")
    buyer.stripe_customer_id = "cus_legacy"
    buyer.stripe_subscription_id = "sub_legacy"
    legacy = Entitlement(
        owner_type="user",
        owner_id=buyer.id,
        source="manual",
        source_ref=None,
        tier="team",
        status="active",
    )
    db_session.add(legacy)
    await db_session.commit()
    buyer.entitlement_id = legacy.id
    await db_session.commit()
    buyer_id, legacy_id = buyer.id, legacy.id
    headers = await _headers(db_session, buyer)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Legacy Co", "slug": "legacy-co-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    assert await _entitlements(db_session) == [
        (legacy_id, "org", org_id, "active", "stripe", "sub_legacy")
    ]
    user_row = (
        await db_session.execute(
            select(User.entitlement_id, User.tier).where(User.id == buyer_id)
        )
    ).one()
    assert tuple(user_row) == (None, "free")
    # Leaving the org would not leave any paid user entitlement behind.
    from sessionfs.server.services.entitlements import resolve_entitlement

    assert await resolve_entitlement("user", buyer_id, db_session) is None


@pytest.mark.asyncio
async def test_org_create_moves_a_manual_entitlement_and_keeps_its_provenance(
    client: AsyncClient, db_session: AsyncSession
):
    """No subscription: the user's active entitlement still moves, with its
    own source kept."""
    from sessionfs.server.db.models import Entitlement

    owner = await _user(db_session, "manual-ent@example.com", tier="enterprise")
    row = Entitlement(
        owner_type="user",
        owner_id=owner.id,
        source="admin_provisioned",
        source_ref=None,
        tier="enterprise",
        status="active",
    )
    db_session.add(row)
    await db_session.commit()
    owner_id, row_id = owner.id, row.id
    headers = await _headers(db_session, owner)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Manual Co", "slug": "manual-co-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]
    assert await _entitlements(db_session) == [
        (row_id, "org", org_id, "active", "admin_provisioned", None)
    ]
    tier = (await db_session.execute(select(User.tier).where(User.id == owner_id))).scalar_one()
    assert tier == "free"


@pytest.mark.asyncio
async def test_org_create_without_any_entitlement_clears_the_user_cache(
    client: AsyncClient, db_session: AsyncSession
):
    owner = await _user(db_session, "no-ent@example.com", tier="team")
    headers = await _headers(db_session, owner)
    owner_id = owner.id

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Bare Co", "slug": "bare-co-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]
    ents = await _entitlements(db_session)
    assert [(e[1], e[2], e[3], e[4]) for e in ents] == [("org", org_id, "active", "manual")]
    user_row = (
        await db_session.execute(select(User.entitlement_id, User.tier).where(User.id == owner_id))
    ).one()
    assert tuple(user_row) == (None, "free")


@pytest.mark.asyncio
async def test_org_create_uses_the_locked_user_row_not_a_stale_snapshot(
    db_session: AsyncSession,
):
    """The User loaded at auth time can be stale by the time the row is
    locked (a webhook downgraded it, or it was deactivated). The tier and
    active checks must read the refreshed, locked row."""
    from fastapi import HTTPException
    from sqlalchemy import update

    from sessionfs.server.routes.org import CreateOrgRequest, create_organization

    user = await _user(db_session, "stale-tier@example.com", tier="team")
    # Change the row behind the identity map's back.
    await db_session.execute(
        update(User)
        .where(User.id == user.id)
        .values(tier="free")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert user.tier == "team"  # stale snapshot

    with pytest.raises(HTTPException) as exc:
        await create_organization(
            CreateOrgRequest(name="Stale", slug="stale-tier"), user=user, db=db_session
        )
    assert exc.value.status_code == 403

    user2 = await _user(db_session, "stale-active@example.com", tier="team")
    await db_session.execute(
        update(User)
        .where(User.id == user2.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert user2.is_active is True  # stale snapshot
    with pytest.raises(HTTPException) as exc:
        await create_organization(
            CreateOrgRequest(name="Stale2", slug="stale-active"), user=user2, db=db_session
        )
    assert exc.value.status_code == 403
    await db_session.rollback()
    orgs = (
        await db_session.execute(
            select(Organization.id).where(Organization.slug.in_(["stale-tier", "stale-active"]))
        )
    ).all()
    assert orgs == []


@pytest.mark.asyncio
async def test_admin_create_org_validates_the_locked_owner_row(db_session: AsyncSession):
    """A deactivation the identity map hasn't seen must still block
    provisioning the user as owner."""
    from fastapi import HTTPException
    from sqlalchemy import update

    from sessionfs.server.routes.admin import admin_create_org

    platform_admin = await _user(db_session, "platform4@sessionfs.dev", tier="admin")
    target = await _user(db_session, "stale-owner@example.com", tier="free")
    await db_session.execute(
        update(User)
        .where(User.id == target.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert target.is_active is True  # stale snapshot

    with pytest.raises(HTTPException) as exc:
        await admin_create_org(
            {"name": "Stale Owner", "slug": "stale-owner", "owner_user_id": target.id},
            admin=platform_admin,
            db=db_session,
        )
    assert exc.value.status_code == 422
    await db_session.rollback()
    orgs = (
        await db_session.execute(select(Organization.id).where(Organization.slug == "stale-owner"))
    ).all()
    assert orgs == []


@pytest.mark.asyncio
async def test_org_create_keeps_a_newer_webhook_applied_entitlement(
    client: AsyncClient, db_session: AsyncSession, stripe_fake, monkeypatch
):
    """A webhook can grant the creator's subscription between org creation's
    Stripe prefetch and its billing locks. The prefetched state is then
    stale; org creation must keep the adopted row's newer plan and
    generation instead of overwriting them and lowering
    stripe_synced_generation."""
    from sessionfs.server.db.models import Entitlement
    from sessionfs.server.routes import billing

    buyer = await _user(db_session, "interleave@example.com", tier="team")
    buyer.stripe_customer_id = "cus_interleave"
    buyer.stripe_subscription_id = "sub_interleave"
    await db_session.commit()
    headers = await _headers(db_session, buyer)
    buyer_id = buyer.id

    # The prefetch sees team / quantity 5 (generation N).
    stripe_fake.subscription(
        "sub_interleave", tier="team", quantity=5, customer="cus_interleave"
    )

    real_fetch = billing._fetch_subscription_state
    newer_gen: dict[str, int] = {}

    async def prefetch_then_grant(db, sub_id):
        fetched = await real_fetch(db, sub_id)
        # A webhook lands before the billing locks and applies a newer state:
        # the plan changed to enterprise at generation N+1.
        gen = await billing._next_fetch_generation(db)
        newer_gen["gen"] = gen
        db.add(
            Entitlement(
                owner_type="user",
                owner_id=buyer_id,
                source="stripe",
                source_ref="sub_interleave",
                tier="enterprise",
                status="active",
                stripe_synced_generation=gen,
            )
        )
        await db.commit()
        return fetched

    monkeypatch.setattr(billing, "_fetch_subscription_state", prefetch_then_grant)

    resp = await client.post(
        "/api/v1/org", headers=headers, json={"name": "Interleave", "slug": "interleave-owner"}
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    db_session.expire_all()
    org = (
        await db_session.execute(
            select(Organization.tier, Organization.seats_limit, Organization.storage_limit_bytes)
            .where(Organization.id == org_id)
        )
    ).one()
    assert tuple(org) == ("enterprise", 25, 0)

    ent = (
        await db_session.execute(
            select(Entitlement).where(
                Entitlement.source == "stripe",
                Entitlement.source_ref == "sub_interleave",
            )
        )
    ).scalar_one()
    assert ent.owner_type == "org"
    assert ent.owner_id == org_id
    assert ent.tier == "enterprise"
    assert ent.stripe_synced_generation == newer_gen["gen"]
