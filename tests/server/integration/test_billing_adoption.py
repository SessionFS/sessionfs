"""Stripe events after org creation adopted the creator's subscription.

Creating an org moves the creator's Stripe customer, subscription and
entitlement to the org. Checkout events whose metadata still names the user
(a checkout that was pending when the org was created, or a replay) must
follow the subscription to its current owner and never create a second
(stripe, subscription_id) binding — otherwise later update/delete webhooks
collide on uq_entitlements_source_ref, roll back, and a cancellation never
lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Entitlement, Organization, StripeEvent, User
from sessionfs.server.routes import billing
from sessionfs.server.routes.billing import _handle_checkout_completed, _process_stripe_event
from sessionfs.server.services.entitlements import resolve_entitlement


def _event(obj, event_type: str, event_id: str | None = None):
    return SimpleNamespace(
        data=SimpleNamespace(object=obj),
        id=event_id or f"evt_{uuid.uuid4().hex[:16]}",
        type=event_type,
    )


def _checkout(user_id: str, sub: str, cus: str, *, tier="team", seats=5, org_id=None, event_id=None):
    metadata = {"user_id": user_id, "tier": tier, "seats": str(seats)}
    if org_id:
        metadata["org_id"] = org_id
    obj = SimpleNamespace(
        metadata=metadata,
        subscription=sub,
        get=lambda key, default=None: {"customer": cus}.get(key, default),
    )
    return _event(obj, "checkout.session.completed", event_id)


def _subscription(sub: str, cus: str, event_type: str, *, status="active", seats=5):
    items = {"data": [{"price": {"product": "prod_team"}, "quantity": seats}]}
    obj = SimpleNamespace(
        id=sub,
        customer=cus,
        status=status,
        get=lambda key, default=None: {"items": items}.get(key, default),
    )
    return _event(obj, event_type)


async def _user(db: AsyncSession, email: str, **kw) -> tuple[User, dict]:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        tier=kw.pop("tier", "team"),
        email_verified=True,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(user)
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
    return user, {"Authorization": f"Bearer {raw}"}


async def _bindings(db: AsyncSession, sub: str) -> list[tuple]:
    db.expire_all()
    rows = await db.execute(
        select(Entitlement.owner_type, Entitlement.owner_id, Entitlement.status, Entitlement.tier)
        .where(Entitlement.source == "stripe", Entitlement.source_ref == sub)
    )
    return [tuple(r) for r in rows.all()]


async def _create_org(client: AsyncClient, headers: dict, slug: str) -> str:
    resp = await client.post("/api/v1/org", headers=headers, json={"name": slug, "slug": slug})
    assert resp.status_code == 200, resp.text
    return resp.json()["org_id"]


@pytest.mark.asyncio
async def test_delayed_personal_checkout_after_org_creation_lands_on_the_org(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    # The user started a personal checkout (customer created), then created
    # the org before checkout.session.completed arrived.
    user, headers = await _user(db_session, "pending@example.com", stripe_customer_id="cus_pending")
    user_id = user.id
    org_id = await _create_org(client, headers, "pending-co")

    # The delayed event still names only the user.
    stripe_fake.subscription("sub_pending", tier="team", quantity=5, customer="cus_pending")
    result = await _process_stripe_event(
        _checkout(user_id, "sub_pending", "cus_pending"), db_session
    )
    assert result == {"status": "ok"}

    assert await _bindings(db_session, "sub_pending") == [("org", org_id, "active", "team")]
    assert await resolve_entitlement("user", user_id, db_session) is None
    row = (
        await db_session.execute(
            select(User.stripe_subscription_id, User.stripe_customer_id).where(User.id == user_id)
        )
    ).one()
    assert tuple(row) == (None, None)
    org = (
        await db_session.execute(
            select(Organization.stripe_subscription_id, Organization.stripe_customer_id)
            .where(Organization.id == org_id)
        )
    ).one()
    assert tuple(org) == ("sub_pending", "cus_pending")

    # Later lifecycle events reach the org row cleanly.
    stripe_fake.subscription("sub_pending", tier="team", quantity=8, customer="cus_pending")
    assert await _process_stripe_event(
        _subscription("sub_pending", "cus_pending", "customer.subscription.updated", seats=8),
        db_session,
    ) == {"status": "ok"}
    seats = (
        await db_session.execute(select(Organization.seats_limit).where(Organization.id == org_id))
    ).scalar_one()
    assert seats == 8

    stripe_fake.subscription("sub_pending", status="canceled", tier="team", customer="cus_pending")
    assert await _process_stripe_event(
        _subscription("sub_pending", "cus_pending", "customer.subscription.deleted"), db_session
    ) == {"status": "ok"}
    assert await _bindings(db_session, "sub_pending") == [("org", org_id, "canceled", "free")]
    assert await resolve_entitlement("org", org_id, db_session) is None
    assert await resolve_entitlement("user", user_id, db_session) is None


@pytest.mark.asyncio
async def test_replayed_checkout_after_adoption_never_creates_a_second_binding(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user, headers = await _user(db_session, "replay@example.com", tier="free", stripe_customer_id="cus_replay")
    user_id = user.id
    stripe_fake.subscription("sub_replay", tier="team", quantity=5, customer="cus_replay")
    checkout = _checkout(user_id, "sub_replay", "cus_replay", event_id="evt_replay_original")
    await _process_stripe_event(checkout, db_session)
    assert await _bindings(db_session, "sub_replay") == [("user", user_id, "active", "team")]

    org_id = await _create_org(client, headers, "replay-co")
    assert await _bindings(db_session, "sub_replay") == [("org", org_id, "active", "team")]

    # Same event id: idempotency marker.
    assert await _process_stripe_event(checkout, db_session) == {"status": "already_processed"}
    # Same checkout under a new delivery id, and a direct handler replay.
    assert await _process_stripe_event(
        _checkout(user_id, "sub_replay", "cus_replay"), db_session
    ) == {"status": "ok"}
    await _handle_checkout_completed(_checkout(user_id, "sub_replay", "cus_replay"), db_session)
    await db_session.commit()
    assert await _bindings(db_session, "sub_replay") == [("org", org_id, "active", "team")]
    assert await resolve_entitlement("user", user_id, db_session) is None

    # Cancellation reaches the org row; a checkout replayed afterwards does
    # not resurrect it.
    stripe_fake.subscription("sub_replay", status="canceled", tier="team", customer="cus_replay")
    assert await _process_stripe_event(
        _subscription("sub_replay", "cus_replay", "customer.subscription.deleted"), db_session
    ) == {"status": "ok"}
    assert await _bindings(db_session, "sub_replay") == [("org", org_id, "canceled", "free")]
    assert await _process_stripe_event(
        _checkout(user_id, "sub_replay", "cus_replay"), db_session
    ) == {"status": "ok"}
    assert await _bindings(db_session, "sub_replay") == [("org", org_id, "canceled", "free")]
    assert await resolve_entitlement("user", user_id, db_session) is None
    # A second delete is a no-op, not a binding collision.
    assert await _process_stripe_event(
        _subscription("sub_replay", "cus_replay", "customer.subscription.deleted"), db_session
    ) == {"status": "ok"}
    assert await _bindings(db_session, "sub_replay") == [("org", org_id, "canceled", "free")]


@pytest.mark.asyncio
async def test_handler_failure_rolls_back_effects_and_the_idempotency_marker(
    db_session: AsyncSession, stripe_fake,
):
    user, _ = await _user(db_session, "atomic@example.com", tier="free", stripe_customer_id="cus_atomic")
    user_id = user.id
    stripe_fake.subscription("sub_atomic", tier="team", quantity=5, customer="cus_atomic")
    event = _checkout(user_id, "sub_atomic", "cus_atomic", event_id="evt_atomic")

    async def explode(evt, db, fetched=None):
        await _handle_checkout_completed(evt, db, fetched)
        raise RuntimeError("boom after effects")

    with patch.dict(billing._WEBHOOK_HANDLERS, {"checkout.session.completed": explode}):
        with pytest.raises(RuntimeError):
            await _process_stripe_event(event, db_session)

    assert await _bindings(db_session, "sub_atomic") == []
    marker = (
        await db_session.execute(select(StripeEvent).where(StripeEvent.event_id == "evt_atomic"))
    ).scalar_one_or_none()
    assert marker is None
    # Stripe's retry then applies it exactly once.
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}
    assert await _bindings(db_session, "sub_atomic") == [("user", user_id, "active", "team")]


@pytest.mark.asyncio
async def test_adopted_user_cannot_start_a_second_personal_subscription(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user, headers = await _user(db_session, "portal@example.com", tier="free", stripe_customer_id="cus_portal")
    stripe_fake.subscription("sub_portal", tier="team", quantity=5, customer="cus_portal")
    await _process_stripe_event(_checkout(user.id, "sub_portal", "cus_portal"), db_session)
    await _create_org(client, headers, "portal-co")

    with patch.dict(
        billing.TIER_PRICE_MAP, {"starter": "price_s", "pro": "price_p", "team": "price_t"}
    ):
        # Personal plans are refused for org members.
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        assert resp.status_code == 400, resp.text
        assert "org_member_restriction" in resp.text
        # The org already holds the adopted subscription.
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "team"})
        assert resp.status_code == 409, resp.text
        # No personal portal: the subscription is the org's now.
        resp = await client.post(
            "/api/v1/billing/portal", headers=headers, json={"scope": "personal"}
        )
        assert resp.status_code == 400, resp.text
        # The default portal is the org's customer.
        resp = await client.post("/api/v1/billing/portal", headers=headers, json={})
        assert resp.status_code == 200, resp.text
        assert stripe_fake.portal_customers == ["cus_portal"]
    assert stripe_fake.checkout_calls == []
    assert stripe_fake.customer_calls == []
