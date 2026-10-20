"""Billing safety: canonical subscription state, exact-binding routing,
retryable failures, dead letters, duplicate subscriptions, pending
checkouts, and the universal billing lock order.

- Stripe events are only triggers: handlers retrieve the subscription's
  CURRENT state (``stripe_fake.subscription(...)``) and apply that, so
  out-of-order delivery (a deletion before the checkout, an older update
  after a newer one) can't grant or roll back the wrong state.
- subscription.updated/deleted reach an entitlement only through its exact
  (stripe, subscription_id) binding (or, for legacy rows, an exact pointer);
  stale or unknown subscriptions are recorded for ops visibility only.
- transient Stripe failures propagate so effects and the idempotency marker
  roll back and Stripe retries; permanently unusable events are committed
  as dead letters with a reconciliation row.
- a second subscription for an owner that already has one is recorded for
  reconciliation, never rebound and never canceled.
- one pending checkout per billing owner, created with a Stripe idempotency
  key.
- (PostgreSQL) org creation, checkout creation, admin changes and webhooks
  serialize on the universal lock order instead of deadlocking.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    BillingCheckoutIntent,
    BillingReconciliation,
    Entitlement,
    Organization,
    OrgMember,
    StripeEvent,
    User,
)
from sessionfs.server.routes import billing
from sessionfs.server.routes.billing import _find_user_or_org_by_customer, _process_stripe_event
from tests.server.stripe_fake import APIConnectionError, FakeStripe, InvalidRequestError


def _event(obj, event_type: str):
    return SimpleNamespace(
        data=SimpleNamespace(object=obj), id=f"evt_{uuid.uuid4().hex[:16]}", type=event_type
    )


def _sub_event(sub: str, cus: str, event_type: str, *, status="active", seats=5):
    """An event payload. Handlers ignore its state and read the canonical
    subscription from Stripe; the payload here can deliberately be stale."""
    items = {"data": [{"price": {"product": "prod_team"}, "quantity": seats}]}
    obj = SimpleNamespace(
        id=sub, customer=cus, status=status,
        get=lambda key, default=None: {"items": items}.get(key, default),
    )
    return _event(obj, event_type)


def _checkout_event(meta: dict, sub, cus):
    obj = SimpleNamespace(
        metadata=meta, subscription=sub,
        get=lambda key, default=None: {"customer": cus}.get(key, default),
    )
    return _event(obj, "checkout.session.completed")


async def _user(db: AsyncSession, **kw) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        tier=kw.pop("tier", "free"),
        email_verified=True,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(user)
    await db.commit()
    return user


async def _org(db: AsyncSession, owner_id: str, **kw) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Org",
        slug=f"o-{uuid.uuid4().hex[:8]}",
        tier=kw.pop("tier", "team"),
        seats_limit=kw.pop("seats_limit", 5),
        storage_limit_bytes=5 * 1024**3,
        **kw,
    )
    db.add(org)
    db.add(OrgMember(org_id=org.id, user_id=owner_id, role="owner"))
    await db.commit()
    return org


async def _ent(db: AsyncSession, owner_type: str, owner_id: str, sub: str | None, *, status="active", tier="team"):
    row = Entitlement(
        owner_type=owner_type, owner_id=owner_id, source="stripe" if sub else "manual",
        source_ref=sub, tier=tier, status=status, seats_limit=5,
    )
    db.add(row)
    await db.commit()
    return row


async def _recon(db: AsyncSession) -> list[tuple]:
    db.expire_all()
    rows = await db.execute(
        select(
            BillingReconciliation.kind,
            BillingReconciliation.stripe_subscription_id,
            BillingReconciliation.owner_type,
            BillingReconciliation.owner_id,
        ).order_by(BillingReconciliation.created_at)
    )
    return [tuple(r) for r in rows.all()]


async def _ents(db: AsyncSession) -> list[tuple]:
    db.expire_all()
    rows = await db.execute(
        select(Entitlement.owner_type, Entitlement.owner_id, Entitlement.source_ref,
               Entitlement.status, Entitlement.tier).order_by(Entitlement.id)
    )
    return [tuple(r) for r in rows.all()]


async def _org_state(db: AsyncSession, org_id: str) -> tuple:
    db.expire_all()
    return tuple((await db.execute(
        select(Organization.tier, Organization.seats_limit, Organization.stripe_subscription_id)
        .where(Organization.id == org_id)
    )).one())


# ── out-of-order delivery: the canonical state wins ──


@pytest.mark.asyncio
async def test_deletion_before_checkout_grants_nothing(db_session: AsyncSession, stripe_fake):
    user = await _user(db_session, stripe_customer_id="cus_early")
    user_id = user.id
    stripe_fake.subscription("sub_early", status="canceled", tier="pro", customer="cus_early")

    # The deletion lands first: nobody holds the subscription yet.
    assert await _process_stripe_event(
        _sub_event("sub_early", "cus_early", "customer.subscription.deleted", status="canceled"),
        db_session,
    ) == {"status": "ok"}
    assert await _recon(db_session) == [("unroutable_subscription_event", "sub_early", None, None)]

    # Then the checkout: Stripe says the subscription is already over.
    assert await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_early", "cus_early"),
        db_session,
    ) == {"status": "ok"}
    assert await _ents(db_session) == []
    db_session.expire_all()
    row = (await db_session.execute(
        select(User.tier, User.stripe_subscription_id).where(User.id == user_id)
    )).one()
    assert tuple(row) == ("free", None)


@pytest.mark.asyncio
async def test_older_update_after_a_newer_one_leaves_the_newer_state(
    db_session: AsyncSession, stripe_fake
):
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_ord", stripe_subscription_id="sub_ord")
    org_id = org.id
    await _ent(db_session, "org", org_id, "sub_ord")

    # Stripe's current state: enterprise, 40 seats (the newer change).
    stripe_fake.subscription("sub_ord", tier="enterprise", quantity=40, customer="cus_ord")
    newer = _sub_event("sub_ord", "cus_ord", "customer.subscription.updated", seats=40)
    older = _sub_event("sub_ord", "cus_ord", "customer.subscription.updated", seats=3)

    assert await _process_stripe_event(newer, db_session) == {"status": "ok"}
    assert await _org_state(db_session, org_id) == ("enterprise", 40, "sub_ord")
    # The older event arrives late; its payload (3 seats) is ignored.
    assert await _process_stripe_event(older, db_session) == {"status": "ok"}
    assert await _org_state(db_session, org_id) == ("enterprise", 40, "sub_ord")
    assert await _ents(db_session) == [("org", org_id, "sub_ord", "active", "enterprise")]


@pytest.mark.asyncio
async def test_a_late_update_after_cancellation_does_not_revive_it(db_session: AsyncSession, stripe_fake):
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_rv", stripe_subscription_id="sub_rv")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_rv", tier="pro")
    stripe_fake.subscription("sub_rv", status="canceled", tier="pro", customer="cus_rv")

    # An "active" update delivered after the cancellation happened.
    await _process_stripe_event(
        _sub_event("sub_rv", "cus_rv", "customer.subscription.updated", status="active"), db_session
    )
    assert await _ents(db_session) == [("user", user_id, "sub_rv", "canceled", "free")]
    await _process_stripe_event(
        _sub_event("sub_rv", "cus_rv", "customer.subscription.deleted", status="canceled"), db_session
    )
    assert await _ents(db_session) == [("user", user_id, "sub_rv", "canceled", "free")]


@pytest.mark.asyncio
async def test_incomplete_checkout_records_a_pending_claim_and_grants_on_activation(
    db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, stripe_customer_id="cus_inc")
    user_id = user.id
    stripe_fake.subscription("sub_inc", status="incomplete", tier="pro", customer="cus_inc")
    await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_inc", "cus_inc"),
        db_session,
    )
    assert await _ents(db_session) == []
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.tier, User.stripe_subscription_id, User.stripe_pending_subscription_id)
        .where(User.id == user_id)
    )).one() == ("free", None, "sub_inc")

    stripe_fake.subscription("sub_inc", status="active", tier="pro", customer="cus_inc")
    await _process_stripe_event(_sub_event("sub_inc", "cus_inc", "customer.subscription.updated"), db_session)
    assert await _ents(db_session) == [("user", user_id, "sub_inc", "active", "pro")]
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.stripe_subscription_id, User.stripe_pending_subscription_id)
        .where(User.id == user_id)
    )).one() == ("sub_inc", None)


@pytest.mark.asyncio
async def test_subscription_retrieve_failure_rolls_back_and_retries(db_session: AsyncSession, stripe_fake):
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, tier="team", stripe_customer_id="cus_t", stripe_subscription_id="sub_t")
    org_id = org.id
    stripe_fake.subscription("sub_t", tier="team", quantity=9, customer="cus_t")
    event = _sub_event("sub_t", "cus_t", "customer.subscription.updated", seats=9)

    stripe_fake.retrieve_error = APIConnectionError("stripe unreachable")
    with pytest.raises(APIConnectionError):
        await _process_stripe_event(event, db_session)
    assert (await db_session.execute(
        select(StripeEvent).where(StripeEvent.event_id == event.id)
    )).scalar_one_or_none() is None
    assert await _recon(db_session) == []
    assert await _org_state(db_session, org_id) == ("team", 5, "sub_t")

    # Stripe's retry then applies the current state once.
    stripe_fake.retrieve_error = None
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}
    assert await _org_state(db_session, org_id) == ("team", 9, "sub_t")


@pytest.mark.asyncio
async def test_checkout_retrieve_failure_rolls_back_and_retries(db_session: AsyncSession, stripe_fake):
    user = await _user(db_session, stripe_customer_id="cus_cr")
    user_id = user.id
    stripe_fake.subscription("sub_cr", tier="pro", customer="cus_cr")
    event = _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_cr", "cus_cr")
    stripe_fake.retrieve_error = APIConnectionError("stripe unreachable")
    with pytest.raises(APIConnectionError):
        await _process_stripe_event(event, db_session)
    assert await _ents(db_session) == []
    stripe_fake.retrieve_error = None
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}
    assert await _ents(db_session) == [("user", user_id, "sub_cr", "active", "pro")]


# ── stale / unknown subscription events (recorded for ops visibility) ──


@pytest.mark.asyncio
async def test_delete_for_a_superseded_subscription_does_not_cancel_the_current_one(
    db_session: AsyncSession, stripe_fake,
):
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_s", stripe_subscription_id="sub_new")
    org_id = org.id
    # The active entitlement is still bound to the old subscription while the
    # org's current pointer is the new one: a delete for the old id is stale.
    await _ent(db_session, "org", org_id, "sub_old")
    stripe_fake.subscription("sub_old", status="canceled", customer="cus_s")

    result = await _process_stripe_event(
        _sub_event("sub_old", "cus_s", "customer.subscription.deleted"), db_session
    )
    assert result == {"status": "ok"}
    assert await _ents(db_session) == [("org", org_id, "sub_old", "active", "team")]
    assert (await _org_state(db_session, org_id))[0] == "team"
    assert await _recon(db_session) == [("stale_subscription_event", "sub_old", "org", org_id)]


@pytest.mark.asyncio
async def test_update_for_a_subscription_the_owner_no_longer_holds_is_ignored(
    db_session: AsyncSession, stripe_fake,
):
    # Legacy pointer match, but the owner's active entitlement is bound to a
    # different subscription: never rebind it.
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_l", stripe_subscription_id="sub_a")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_b", tier="pro")
    stripe_fake.subscription("sub_a", tier="starter", customer="cus_l")

    await _process_stripe_event(_sub_event("sub_a", "cus_l", "customer.subscription.updated"), db_session)
    assert await _ents(db_session) == [("user", user_id, "sub_b", "active", "pro")]
    assert await _recon(db_session) == [("stale_subscription_event", "sub_a", "user", user_id)]


@pytest.mark.asyncio
async def test_unknown_subscription_on_a_shared_customer_touches_nobody(
    db_session: AsyncSession, stripe_fake,
):
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_shared", stripe_subscription_id="sub_p")
    org = await _org(db_session, user.id, stripe_customer_id="cus_shared", stripe_subscription_id="sub_o")
    org_id, user_id = org.id, user.id
    stripe_fake.subscription("sub_unknown", status="canceled", customer="cus_shared")

    u, o = await _find_user_or_org_by_customer("cus_shared", db_session, subscription_id="sub_unknown")
    assert (u, o) == (None, None)

    await _process_stripe_event(
        _sub_event("sub_unknown", "cus_shared", "customer.subscription.deleted"), db_session
    )
    assert await _org_state(db_session, org_id) == ("team", 5, "sub_o")
    user_row = (await db_session.execute(
        select(User.tier, User.stripe_subscription_id).where(User.id == user_id)
    )).one()
    assert tuple(user_row) == ("pro", "sub_p")
    assert await _recon(db_session) == [("unroutable_subscription_event", "sub_unknown", None, None)]


# ── permanently unusable events are dead-lettered ──


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_subscription", "invalid_product", "no_tier", "no_items"])
async def test_permanently_malformed_events_are_dead_lettered(
    db_session: AsyncSession, stripe_fake, case: str
):
    owner = await _user(db_session)
    sub = f"sub_dl_{case}"
    org = await _org(db_session, owner.id, tier="team", stripe_customer_id=f"cus_{case}",
                     stripe_subscription_id=sub, seats_limit=5)
    org_id = org.id
    if case == "invalid_product":
        stripe_fake.subscription(sub, product="prod_missing", customer=f"cus_{case}", quantity=40)
        del stripe_fake.products["prod_missing"]
    elif case == "no_tier":
        stripe_fake.subscription(sub, tier=None, customer=f"cus_{case}", quantity=40)
    elif case == "no_items":
        stripe_fake.subscription(sub, customer=f"cus_{case}")["items"] = {"data": []}
    # no_subscription: Stripe has no such subscription (InvalidRequestError)
    event = _sub_event(sub, f"cus_{case}", "customer.subscription.updated", seats=40)

    assert await _process_stripe_event(event, db_session) == {"status": "dead_letter"}
    outcome = (await db_session.execute(
        select(StripeEvent.outcome).where(StripeEvent.event_id == event.id)
    )).scalar_one()
    assert outcome == "dead_letter"
    assert [r[0] for r in await _recon(db_session)] == ["malformed_event"]
    assert await _org_state(db_session, org_id) == ("team", 5, sub)  # nothing applied


@pytest.mark.asyncio
async def test_checkout_without_metadata_is_dead_lettered(db_session: AsyncSession, stripe_fake):
    event = _checkout_event({"tier": "pro"}, "sub_nometa", "cus_nometa")
    assert await _process_stripe_event(event, db_session) == {"status": "dead_letter"}
    assert [r[0] for r in await _recon(db_session)] == ["malformed_event"]
    assert await _ents(db_session) == []


def test_fake_errors_classify_like_stripe():
    assert billing._is_permanent_stripe_error(InvalidRequestError("x"))
    assert not billing._is_permanent_stripe_error(APIConnectionError("x"))


# ── duplicate subscriptions ──


@pytest.mark.asyncio
async def test_second_subscription_for_an_owner_is_recorded_not_rebound(
    db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_dup", stripe_subscription_id="sub_first")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_first", tier="pro")
    stripe_fake.subscription("sub_second", tier="pro", customer="cus_dup")

    event = _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_second", "cus_dup")
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}

    assert await _ents(db_session) == [("user", user_id, "sub_first", "active", "pro")]
    sub = (await db_session.execute(select(User.stripe_subscription_id).where(User.id == user_id))).scalar_one()
    assert sub == "sub_first"
    assert await _recon(db_session) == [("duplicate_subscription", "sub_second", "user", user_id)]


@pytest.mark.asyncio
async def test_second_org_subscription_is_recorded_not_rebound(db_session: AsyncSession, stripe_fake):
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_od", stripe_subscription_id="sub_o1")
    org_id = org.id
    await _ent(db_session, "org", org_id, "sub_o1")
    stripe_fake.subscription("sub_o2", tier="team", quantity=3, customer="cus_od")
    event = _checkout_event(
        {"user_id": owner.id, "tier": "team", "seats": "3", "org_id": org_id}, "sub_o2", "cus_od"
    )
    await _process_stripe_event(event, db_session)
    assert await _ents(db_session) == [("org", org_id, "sub_o1", "active", "team")]
    assert await _recon(db_session) == [("duplicate_subscription", "sub_o2", "org", org_id)]


@pytest.mark.asyncio
async def test_two_users_claiming_one_subscription_is_ambiguous(
    db_session: AsyncSession, stripe_fake
):
    """Two users hold the same (unbound) subscription pointer. The checkout
    must not grant whichever claimant sorts first: it is recorded as
    ambiguous and neither user's pointer is touched."""
    a = await _user(db_session, tier="team", stripe_customer_id="cus_amb_u",
                    stripe_subscription_id="sub_amb_u")
    b = await _user(db_session, tier="team", stripe_customer_id="cus_amb_u2",
                    stripe_subscription_id="sub_amb_u")
    a_id, b_id = a.id, b.id
    stripe_fake.subscription("sub_amb_u", tier="team", quantity=5, customer="cus_amb_u")

    event = _checkout_event({"user_id": a_id, "tier": "team", "seats": "5"}, "sub_amb_u", "cus_amb_u")
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}

    assert await _recon(db_session) == [("ambiguous_subscription_pointer", "sub_amb_u", None, None)]
    assert await _ents(db_session) == []
    subs = dict((await db_session.execute(
        select(User.id, User.stripe_subscription_id).where(User.id.in_([a_id, b_id]))
    )).all())
    assert subs == {a_id: "sub_amb_u", b_id: "sub_amb_u"}


@pytest.mark.asyncio
async def test_org_and_unrelated_user_claiming_one_subscription_is_ambiguous(
    db_session: AsyncSession, stripe_fake
):
    """An org and an unrelated user (not a member, different customer) both
    hold the pointer. The legacy org-plus-member exception does not apply, so
    the checkout is ambiguous and the unrelated user's pointer survives."""
    owner = await _user(db_session, tier="team")
    await _org(db_session, owner.id, stripe_customer_id="cus_amb_o",
               stripe_subscription_id="sub_amb_o")
    other = await _user(db_session, tier="team", stripe_customer_id="cus_other",
                        stripe_subscription_id="sub_amb_o")
    other_id = other.id
    stripe_fake.subscription("sub_amb_o", tier="team", quantity=5, customer="cus_amb_o")

    event = _checkout_event({"user_id": owner.id, "tier": "team", "seats": "5"},
                            "sub_amb_o", "cus_amb_o")
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}

    assert await _recon(db_session) == [("ambiguous_subscription_pointer", "sub_amb_o", None, None)]
    assert await _ents(db_session) == []
    other_sub = (await db_session.execute(
        select(User.stripe_subscription_id).where(User.id == other_id)
    )).scalar_one()
    assert other_sub == "sub_amb_o"  # not cleared by a grant to the org


@pytest.mark.asyncio
async def test_two_orgs_claiming_one_subscription_do_not_raise(
    db_session: AsyncSession, stripe_fake
):
    """Two orgs hold the same pointer. The old resolver used scalar_one_or_none
    on the org pointer and raised MultipleResultsFound forever; it must now
    record the ambiguity and finish."""
    u1 = await _user(db_session, tier="team")
    u2 = await _user(db_session, tier="team")
    await _org(db_session, u1.id, stripe_customer_id="cus_amb_org",
               stripe_subscription_id="sub_amb_org")
    await _org(db_session, u2.id, stripe_customer_id="cus_amb_org2",
               stripe_subscription_id="sub_amb_org")
    stripe_fake.subscription("sub_amb_org", tier="team", quantity=5, customer="cus_amb_org")

    event = _checkout_event({"user_id": u1.id, "tier": "team", "seats": "5"},
                            "sub_amb_org", "cus_amb_org")
    assert await _process_stripe_event(event, db_session) == {"status": "ok"}

    assert await _recon(db_session) == [("ambiguous_subscription_pointer", "sub_amb_org", None, None)]
    assert await _ents(db_session) == []


@pytest.mark.asyncio
async def test_legacy_org_plus_member_duplicate_grants_to_the_org(
    db_session: AsyncSession, stripe_fake
):
    """The provable legacy shape — one org plus a user who is its member AND
    carries the org's customer — still resolves to the org, and the member's
    duplicate pointer is cleared."""
    owner = await _user(db_session, tier="team", stripe_customer_id="cus_leg",
                        stripe_subscription_id="sub_leg")
    org = await _org(db_session, owner.id, stripe_customer_id="cus_leg",
                     stripe_subscription_id="sub_leg")
    org_id, owner_id = org.id, owner.id
    stripe_fake.subscription("sub_leg", tier="team", quantity=7, customer="cus_leg")

    event = _checkout_event({"user_id": owner_id, "tier": "team", "seats": "7"},
                            "sub_leg", "cus_leg")
    await _process_stripe_event(event, db_session)

    assert await _ents(db_session) == [("org", org_id, "sub_leg", "active", "team")]
    owner_sub = (await db_session.execute(
        select(User.stripe_subscription_id).where(User.id == owner_id)
    )).scalar_one()
    assert owner_sub is None


# ── checkout creation: one pending checkout per owner ──


async def _headers(db: AsyncSession, user: User) -> dict:
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
                  name="k", created_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


_PRICES = {"starter": "p_s", "pro": "p_p", "team": "p_t"}


@pytest.mark.asyncio
async def test_checkout_at_the_end_of_a_window_leaves_stripe_its_minimum_expiry(
    client: AsyncClient, db_session: AsyncSession, stripe_fake, monkeypatch
):
    # A request in the last second of an idempotency window must still give
    # Stripe an expires_at well past its 30-minute minimum, so slow Stripe
    # round-trips (or a retry later in the same window) are not rejected.
    window = billing._CHECKOUT_WINDOW_SECONDS
    base = (int(datetime.now(timezone.utc).timestamp()) // window + 1) * window
    frozen = datetime.fromtimestamp(base - 1, tz=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(billing, "datetime", _Frozen)
    user = await _user(db_session)
    headers = await _headers(db_session, user)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 200, resp.text
    expires_at = stripe_fake.checkout_calls[0]["expires_at"]
    assert expires_at - frozen.timestamp() >= 1800 + 300
    assert expires_at - frozen.timestamp() <= 24 * 3600


@pytest.mark.asyncio
async def test_repeated_checkout_requests_reuse_one_pending_session(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session)
    headers = await _headers(db_session, user)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        first = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        second = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        other = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "starter"})

    calls = stripe_fake.checkout_calls
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1
    key = calls[0]["idempotency_key"]
    assert key.startswith(f"sfs-checkout:user:{user.id}:personal:pro:1:")
    assert calls[0]["metadata"]["checkout_intent_id"].startswith("bci_")
    assert 2400 <= calls[0]["expires_at"] - int(datetime.now(timezone.utc).timestamp()) <= 4200
    assert stripe_fake.customer_calls[0]["idempotency_key"] == f"sfs-customer:user:{user.id}"
    assert other.status_code == 409 and "checkout_pending" in other.text

    intents = (await db_session.execute(
        select(BillingCheckoutIntent.status, BillingCheckoutIntent.stripe_session_id)
        .where(BillingCheckoutIntent.owner_id == user.id)
    )).all()
    assert [tuple(i) for i in intents] == [("pending", f"cs_{stripe_fake._tag}_1")]


@pytest.mark.asyncio
async def test_expired_pending_checkout_allows_a_new_one_and_completion_closes_it(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session)
    user_id = user.id
    headers = await _headers(db_session, user)
    db_session.add(BillingCheckoutIntent(
        id="bci_old", owner_type="user", owner_id=user_id, scope="personal", tier="pro", seats=1,
        idempotency_key="old", status="pending", checkout_url="https://old",
        stripe_session_id="cs_old",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()
    # Stripe confirms the old session expired, so the slot is released.
    stripe_fake.session("cs_old", "expired")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 200 and len(stripe_fake.checkout_calls) == 1
    intent_id = stripe_fake.checkout_calls[0]["metadata"]["checkout_intent_id"]

    stripe_fake.subscription("sub_fresh", tier="pro", customer=stripe_fake.customers[0])
    session_obj = SimpleNamespace(
        id=f"cs_{stripe_fake._tag}_1", metadata={"user_id": user_id, "tier": "pro", "seats": "1",
                             "checkout_intent_id": intent_id},
        subscription="sub_fresh",
        get=lambda key, default=None: {"customer": stripe_fake.customers[0]}.get(key, default),
    )
    await _process_stripe_event(_event(session_obj, "checkout.session.completed"), db_session)
    db_session.expire_all()
    statuses = dict((await db_session.execute(
        select(BillingCheckoutIntent.id, BillingCheckoutIntent.status)
        .where(BillingCheckoutIntent.owner_id == user_id)
    )).all())
    assert statuses == {"bci_old": "expired", intent_id: "completed"}
    assert await _ents(db_session) == [("user", user_id, "sub_fresh", "active", "pro")]


# ── round 6: pending claims, intents, no Stripe I/O under locks, lock races ──


@pytest.mark.asyncio
async def test_expired_incomplete_checkout_never_revokes_an_unrelated_manual_entitlement(
    db_session: AsyncSession, stripe_fake
):
    """An admin-provisioned owner starts Checkout; the subscription stays
    incomplete, then expires. Only the pending claim is touched."""
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_man")
    user_id = user.id
    db_session.add(Entitlement(owner_type="user", owner_id=user_id, source="admin_provisioned",
                               tier="pro", status="active"))
    await db_session.commit()

    stripe_fake.subscription("sub_man", status="incomplete", tier="pro", customer="cus_man")
    await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_man", "cus_man"),
        db_session,
    )
    stripe_fake.subscription("sub_man", status="incomplete_expired", tier="pro", customer="cus_man")
    await _process_stripe_event(
        _sub_event("sub_man", "cus_man", "customer.subscription.updated"), db_session
    )
    db_session.expire_all()
    row = (await db_session.execute(
        select(User.tier, User.stripe_subscription_id, User.stripe_pending_subscription_id)
        .where(User.id == user_id)
    )).one()
    assert tuple(row) == ("pro", None, None)
    ents = (await db_session.execute(
        select(Entitlement.source, Entitlement.status, Entitlement.tier).where(
            Entitlement.owner_id == user_id)
    )).all()
    assert [tuple(e) for e in ents] == [("admin_provisioned", "active", "pro")]


@pytest.mark.asyncio
async def test_late_canceled_checkout_revokes_through_the_exact_binding(
    db_session: AsyncSession, stripe_fake
):
    """A legacy owner (binding backfilled by migration 075) receives a late
    checkout whose subscription is already canceled: it is revoked."""
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_lg", stripe_subscription_id="sub_lg")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_lg", tier="pro")
    stripe_fake.subscription("sub_lg", status="canceled", tier="pro", customer="cus_lg")
    await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_lg", "cus_lg"),
        db_session,
    )
    assert await _ents(db_session) == [("user", user_id, "sub_lg", "canceled", "free")]
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.tier, User.stripe_subscription_id).where(User.id == user_id)
    )).one() == ("free", None)


@pytest.mark.asyncio
async def test_late_canceled_checkout_with_a_bare_pointer_only_clears_the_pointer(
    db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="pro", stripe_customer_id="cus_bp", stripe_subscription_id="sub_bp")
    user_id = user.id
    stripe_fake.subscription("sub_bp", status="canceled", tier="pro", customer="cus_bp")
    await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_bp", "cus_bp"),
        db_session,
    )
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.tier, User.stripe_subscription_id).where(User.id == user_id)
    )).one() == ("pro", None)
    assert await _ents(db_session) == []


@pytest.mark.asyncio
async def test_org_creation_takes_over_the_creators_open_checkout(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team")
    user_id = user.id
    headers = await _headers(db_session, user)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "team", "seats": 2})
        assert resp.status_code == 200, resp.text
        org = await client.post("/api/v1/org", headers=headers, json={"name": "Co", "slug": "takeover-co"})
        assert org.status_code == 200, org.text
        org_id = org.json()["org_id"]
        # The org sees the open checkout and does not start a second one.
        again = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "team", "seats": 5})
    assert again.status_code == 409 and "checkout_pending" in again.text
    assert len(stripe_fake.checkout_calls) == 1
    intents = (await db_session.execute(
        select(BillingCheckoutIntent.owner_type, BillingCheckoutIntent.owner_id,
               BillingCheckoutIntent.status)
    )).all()
    assert [tuple(i) for i in intents] == [("org", org_id, "pending")]
    assert user_id != org_id


@pytest.mark.asyncio
async def test_one_open_checkout_per_stripe_customer(db_session: AsyncSession):
    from sqlalchemy.exc import IntegrityError

    common = dict(scope="personal", tier="pro", seats=1, status="pending",
                  stripe_customer_id="cus_shared_intent",
                  expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    db_session.add(BillingCheckoutIntent(id="bci_a", owner_type="user", owner_id="u_a",
                                         idempotency_key="a", **common))
    await db_session.commit()
    db_session.add(BillingCheckoutIntent(id="bci_b", owner_type="org", owner_id="o_b",
                                         idempotency_key="b", **common))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def _lapsed_intent(db_session, user_id, session_id):
    db_session.add(BillingCheckoutIntent(
        id=f"bci_{session_id}", owner_type="user", owner_id=user_id, scope="personal", tier="pro",
        seats=1, idempotency_key=session_id, status="pending", checkout_url="https://old",
        stripe_session_id=session_id, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_lapsed_checkout_that_completed_remotely_blocks_new_checkouts(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, stripe_customer_id="cus_lapse")
    user_id = user.id
    headers = await _headers(db_session, user)
    await _lapsed_intent(db_session, user_id, "cs_done")
    stripe_fake.session("cs_done", "complete")  # paid just before expiry; webhook delayed
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        first = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        second = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert first.status_code == 409 and "checkout_unresolved" in first.text
    assert second.status_code == 409 and "checkout_unresolved" in second.text
    assert stripe_fake.checkout_calls == []
    db_session.expire_all()
    status = (await db_session.execute(
        select(BillingCheckoutIntent.status).where(BillingCheckoutIntent.id == "bci_cs_done")
    )).scalar_one()
    assert status == "unresolved"


@pytest.mark.asyncio
async def test_lapsed_checkout_fails_closed_when_stripe_is_unreachable(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, stripe_customer_id="cus_down")
    headers = await _headers(db_session, user)
    await _lapsed_intent(db_session, user.id, "cs_unknown")
    stripe_fake.session("cs_unknown", "expired")
    stripe_fake.session_retrieve_error = APIConnectionError("stripe down")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 503 and "billing_unavailable" in resp.text
    assert stripe_fake.checkout_calls == []
    db_session.expire_all()
    status = (await db_session.execute(
        select(BillingCheckoutIntent.status).where(BillingCheckoutIntent.id == "bci_cs_unknown")
    )).scalar_one()
    assert status == "pending"


@pytest.mark.asyncio
async def test_dead_lettered_checkout_leaves_its_intent_unresolved(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session)
    user_id = user.id
    headers = await _headers(db_session, user)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        assert resp.status_code == 200
        intent_id = stripe_fake.checkout_calls[0]["metadata"]["checkout_intent_id"]
        stripe_fake.subscription("sub_bad", tier=None, customer=stripe_fake.customers[0])  # no tier metadata
        session_obj = SimpleNamespace(
            id=f"cs_{stripe_fake._tag}_1", metadata={"user_id": user_id, "tier": "pro", "seats": "1",
                                 "checkout_intent_id": intent_id},
            subscription="sub_bad",
            get=lambda key, default=None: {"customer": stripe_fake.customers[0]}.get(key, default),
        )
        result = await _process_stripe_event(_event(session_obj, "checkout.session.completed"), db_session)
        assert result == {"status": "dead_letter"}
        again = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert again.status_code == 409 and "checkout_unresolved" in again.text
    db_session.expire_all()
    status = (await db_session.execute(
        select(BillingCheckoutIntent.status).where(BillingCheckoutIntent.id == intent_id)
    )).scalar_one()
    assert status == "unresolved"


@pytest.mark.asyncio
async def test_stripe_is_never_called_inside_a_database_transaction(
    db_session: AsyncSession, stripe_fake
):
    from sessionfs.server.tier_gate import UserContext
    from sessionfs.server.tiers import Tier

    calls: list[tuple[str, bool]] = []
    stripe_fake.on_call = lambda name: calls.append((name, db_session.in_transaction()))

    user = await _user(db_session, stripe_customer_id="cus_tx")
    user_id = user.id
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_tx_org", stripe_subscription_id="sub_tx")
    await _ent(db_session, "org", org.id, "sub_tx")
    stripe_fake.subscription("sub_tx", tier="team", quantity=4, customer="cus_tx_org")
    stripe_fake.subscription("sub_tx_user", tier="pro", customer="cus_tx")

    await _process_stripe_event(_sub_event("sub_tx", "cus_tx_org", "customer.subscription.updated"), db_session)
    await _process_stripe_event(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_tx_user", "cus_tx"),
        db_session,
    )
    fresh = await _user(db_session)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        ctx = UserContext(user=fresh, effective_tier=Tier.FREE, org=None, role=None, is_org_user=False)
        await billing.create_checkout(billing.CheckoutRequest(tier="pro"), ctx=ctx, db=db_session)
    names = {name for name, _ in calls}
    assert {"Subscription.retrieve", "Product.retrieve", "Customer.create",
            "checkout.Session.create"} <= names
    assert [c for c in calls if c[1]] == [], calls


@pytest.mark.asyncio
async def test_a_fetch_older_than_the_applied_state_is_refused(db_session: AsyncSession, stripe_fake):
    """Fetches are ordered by their DB-issued generation
    (stripe_fetch_generations.id), never by a wall clock: a fetch whose
    generation is behind the one already applied to the binding is refused."""
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_old", stripe_subscription_id="sub_old2")
    org_id = org.id
    row = await _ent(db_session, "org", org_id, "sub_old2")
    row.stripe_synced_generation = 1_000_000_000  # a much newer fetch was applied
    await db_session.commit()
    stripe_fake.subscription("sub_old2", tier="team", quantity=40, customer="cus_old")
    await _process_stripe_event(_sub_event("sub_old2", "cus_old", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("team", 5, "sub_old2")

    # The binding's generation moving forward (or resetting) lets the next
    # fetch apply again.
    row.stripe_synced_generation = None
    await db_session.commit()
    await _process_stripe_event(_sub_event("sub_old2", "cus_old", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("team", 40, "sub_old2")


@pytest.mark.asyncio
async def test_customer_change_during_locking_restarts_instead_of_locking_late(
    db_session: AsyncSession, stripe_fake, monkeypatch
):
    """lock_billing_rows never takes a customer lock after a row lock: a
    customer that changed under it raises BillingLockRetry and the webhook's
    apply transaction restarts with the new customer."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import entitlements

    user = await _user(db_session, stripe_customer_id="cus_before")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_lr", tier="pro")
    user.stripe_subscription_id = "sub_lr"
    await db_session.commit()
    stripe_fake.subscription("sub_lr", tier="starter", customer="cus_before")

    real_lock = entitlements.lock_stripe_customer
    taken: list[str] = []

    async def racing_lock(db, customer_id):
        taken.append(customer_id)
        if customer_id == "cus_before" and taken.count("cus_before") == 1:
            # A concurrent checkout assigns a new customer between the
            # unlocked read and the row lock.
            await db.execute(sa_update(User).where(User.id == user_id).values(stripe_customer_id="cus_after"))
        return await real_lock(db, customer_id)

    monkeypatch.setattr(entitlements, "lock_stripe_customer", racing_lock)
    result = await _process_stripe_event(_sub_event("sub_lr", "cus_before", "customer.subscription.updated"), db_session)
    assert result == {"status": "ok"}
    # First attempt hit BillingLockRetry (and rolled back, undoing the
    # simulated change in this single-connection test DB); the restart
    # took the locks again and applied.
    assert taken.count("cus_before") == 2
    assert "cus_after" not in taken  # never locked late, after the row
    assert await _ents(db_session) == [("user", user_id, "sub_lr", "active", "starter")]


@pytest.mark.asyncio
async def test_lock_billing_rows_refuses_a_customer_that_changed_under_it(
    db_session: AsyncSession, monkeypatch
):
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import entitlements

    user = await _user(db_session, stripe_customer_id="cus_x1")
    user_id = user.id
    real_lock = entitlements.lock_stripe_customer
    taken: list[str] = []

    async def racing_lock(db, customer_id):
        taken.append(customer_id)
        await db.execute(sa_update(User).where(User.id == user_id).values(stripe_customer_id="cus_x2"))
        return await real_lock(db, customer_id)

    monkeypatch.setattr(entitlements, "lock_stripe_customer", racing_lock)
    with pytest.raises(entitlements.BillingLockRetry):
        await entitlements.lock_billing_rows(db_session, user_ids=[user_id])
    assert taken == ["cus_x1"]  # cus_x2 was never locked after the row
    await db_session.rollback()


def test_billing_lock_retry_answers_409():
    import asyncio as _asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport

    from sessionfs.server.errors import register_exception_handlers
    from sessionfs.server.services.entitlements import BillingLockRetry

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/race")
    async def race():
        raise BillingLockRetry("customer changed")

    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get("/race")

    resp = _asyncio.run(go())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "concurrent_update"


# ── round 7: observations, adoption provenance, personal plans vs orgs ──


@pytest.mark.asyncio
async def test_older_checkout_fetch_cannot_grant_after_a_terminal_observation(
    db_session: AsyncSession, stripe_fake
):
    """The deletion is delivered first (nobody owns the subscription yet)
    and recorded as a terminal observation. A checkout whose Stripe fetch
    predates it — by GENERATION, even with a NEWER wall-clock timestamp —
    is refused, even though no entitlement row exists."""
    from sessionfs.server.db.models import StripeSubscriptionObservation
    from sessionfs.server.routes.billing import (
        _CanonicalSubscription,
        _Fetched,
        _handle_checkout_completed,
    )

    user = await _user(db_session, stripe_customer_id="cus_obs")
    user_id = user.id
    stale_fetch = _Fetched(
        # Handcrafted fetches default to generation 0, older than every real
        # one; the future wall clock must not rescue it.
        "sub_obs", datetime.now(timezone.utc) + timedelta(minutes=5),
        canonical=_CanonicalSubscription("sub_obs", "active", "cus_obs", 1), tier="pro",
    )
    stripe_fake.subscription("sub_obs", status="canceled", tier="pro", customer="cus_obs")
    await _process_stripe_event(
        _sub_event("sub_obs", "cus_obs", "customer.subscription.deleted", status="canceled"), db_session
    )
    obs = (await db_session.execute(
        select(StripeSubscriptionObservation.status, StripeSubscriptionObservation.terminal_at)
        .where(StripeSubscriptionObservation.subscription_id == "sub_obs")
    )).one()
    assert obs[0] == "canceled" and obs[1] is not None

    event = _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_obs", "cus_obs")
    await _handle_checkout_completed(event, db_session, stale_fetch)
    await db_session.commit()
    assert await _ents(db_session) == []
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.tier, User.stripe_subscription_id).where(User.id == user_id)
    )).one() == ("free", None)


@pytest.mark.asyncio
async def test_unroutable_events_still_record_observations(db_session: AsyncSession, stripe_fake):
    from sessionfs.server.db.models import StripeSubscriptionObservation

    stripe_fake.subscription("sub_nobody", status="past_due", tier="team", customer="cus_nobody")
    await _process_stripe_event(
        _sub_event("sub_nobody", "cus_nobody", "customer.subscription.updated"), db_session
    )
    row = (await db_session.execute(
        select(StripeSubscriptionObservation.status, StripeSubscriptionObservation.terminal_at,
               StripeSubscriptionObservation.customer_id)
        .where(StripeSubscriptionObservation.subscription_id == "sub_nobody")
    )).one()
    assert tuple(row) == ("past_due", None, "cus_nobody")


@pytest.mark.asyncio
async def test_org_creation_keeps_admin_provisioned_provenance(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A user with an admin-provisioned entitlement and a Stripe pointer
    (a 075 'review' case): the entitlement moves to the org as it is — it
    is never converted to Stripe without Stripe's confirmation."""
    user = await _user(db_session, tier="team", stripe_customer_id="cus_ap", stripe_subscription_id="sub_ap")
    db_session.add(Entitlement(owner_type="user", owner_id=user.id, source="admin_provisioned",
                               tier="team", status="active"))
    await db_session.commit()
    headers = await _headers(db_session, user)
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "AP", "slug": "ap-owner"})
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]
    rows = (await db_session.execute(
        select(Entitlement.owner_type, Entitlement.owner_id, Entitlement.source, Entitlement.source_ref)
    )).all()
    assert [tuple(r) for r in rows] == [("org", org_id, "admin_provisioned", None)]


@pytest.mark.asyncio
async def test_org_creation_refuses_a_different_stripe_binding_and_records_it(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team", stripe_customer_id="cus_cf", stripe_subscription_id="sub_y")
    user_id = user.id
    await _ent(db_session, "user", user_id, "sub_x", tier="team")  # bound to a different subscription
    headers = await _headers(db_session, user)
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "CF", "slug": "cf-owner"})
    assert resp.status_code == 409, resp.text
    assert "subscription_binding_conflict" in resp.text
    assert await _ents(db_session) == [("user", user_id, "sub_x", "active", "team")]
    assert await _recon(db_session) == [("subscription_binding_conflict", "sub_y", "user", user_id)]
    orgs = (await db_session.execute(select(Organization.id).where(Organization.slug == "cf-owner"))).all()
    assert orgs == []


@pytest.mark.asyncio
async def test_org_creation_is_blocked_by_an_open_personal_plan_checkout(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team")
    headers = await _headers(db_session, user)
    db_session.add(BillingCheckoutIntent(
        id="bci_personal", owner_type="user", owner_id=user.id, scope="personal", tier="pro",
        seats=1, idempotency_key="p", status="pending", checkout_url="https://p",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    ))
    await db_session.commit()
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "PP", "slug": "pp-owner"})
    assert resp.status_code == 409 and "personal_checkout_open" in resp.text
    owner_type = (await db_session.execute(
        select(BillingCheckoutIntent.owner_type).where(BillingCheckoutIntent.id == "bci_personal")
    )).scalar_one()
    assert owner_type == "user"


@pytest.mark.asyncio
async def test_personal_plan_on_an_orgs_exact_binding_downgrades_it_to_match_stripe(
    db_session: AsyncSession, stripe_fake
):
    """An org's exact Stripe binding switched to a personal plan: the org
    entitlement is downgraded to match what Stripe actually sells it —
    keeping the binding (and the subscription pointer) so a move back to a
    team plan re-grants — and the mismatch is recorded."""
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, tier="team", stripe_customer_id="cus_pp", stripe_subscription_id="sub_pp")
    org_id = org.id
    await _ent(db_session, "org", org_id, "sub_pp", tier="team")
    stripe_fake.subscription("sub_pp", tier="pro", customer="cus_pp")  # a personal plan
    await _process_stripe_event(_sub_event("sub_pp", "cus_pp", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("free", 0, "sub_pp")
    assert await _ents(db_session) == [("org", org_id, "sub_pp", "active", "free")]
    assert await _recon(db_session) == [("plan_owner_mismatch", "sub_pp", "org", org_id)]

    # The binding was kept: moving back to a team plan re-grants.
    stripe_fake.subscription("sub_pp", tier="team", quantity=7, customer="cus_pp")
    await _process_stripe_event(_sub_event("sub_pp", "cus_pp", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("team", 7, "sub_pp")
    assert await _ents(db_session) == [("org", org_id, "sub_pp", "active", "team")]


@pytest.mark.asyncio
async def test_personal_plan_routed_to_an_org_without_a_binding_changes_nothing(
    db_session: AsyncSession, stripe_fake
):
    """No exact Stripe binding (a manual/admin entitlement next to a legacy
    pointer): the entitlement is unrelated to Stripe and stays untouched —
    only the mismatch is recorded."""
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, tier="team", stripe_customer_id="cus_np", stripe_subscription_id="sub_np")
    org_id = org.id
    await _ent(db_session, "org", org_id, None, tier="team")  # manual, no Stripe binding
    stripe_fake.subscription("sub_np", tier="pro", customer="cus_np")
    await _process_stripe_event(_sub_event("sub_np", "cus_np", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("team", 5, "sub_np")
    assert await _ents(db_session) == [("org", org_id, None, "active", "team")]
    assert await _recon(db_session) == [("plan_owner_mismatch", "sub_np", "org", org_id)]


# ── round 8: ambiguous claimants, checkout rechecks, the customer gap, ──
# ── the pre-intent sweep, pointer-only adoption, fetch generations ──


@pytest.mark.asyncio
async def test_a_subscription_two_users_claim_is_ambiguous_and_changes_nothing(
    db_session: AsyncSession, stripe_fake
):
    """Migration 075 leaves a doubly-claimed pointer unbound; a later event
    for it must never hand the subscription to whichever claimant sorts
    first — every claimant is collected, locked, and nothing is applied."""
    first = await _user(db_session, tier="pro", stripe_customer_id="cus_amb1",
                        stripe_subscription_id="sub_amb")
    second = await _user(db_session, tier="pro", stripe_customer_id="cus_amb2",
                         stripe_subscription_id="sub_amb")
    first_id, second_id = first.id, second.id
    stripe_fake.subscription("sub_amb", tier="pro", customer="cus_amb1")

    result = await _process_stripe_event(
        _sub_event("sub_amb", "cus_amb1", "customer.subscription.updated"), db_session
    )
    assert result == {"status": "ok"}
    assert await _ents(db_session) == []
    db_session.expire_all()
    rows = (await db_session.execute(
        select(User.id, User.tier, User.stripe_subscription_id)
        .where(User.id.in_([first_id, second_id]))
    )).all()
    assert sorted(tuple(r) for r in rows) == sorted([
        (first_id, "pro", "sub_amb"), (second_id, "pro", "sub_amb"),
    ])
    recon = await _recon(db_session)
    assert [(r[0], r[1]) for r in recon] == [("ambiguous_subscription_pointer", "sub_amb")]
    details = (await db_session.execute(
        select(BillingReconciliation.details)
        .where(BillingReconciliation.kind == "ambiguous_subscription_pointer")
    )).scalar_one()
    assert first_id in details and second_id in details


@pytest.mark.asyncio
async def test_an_org_and_an_unrelated_user_claiming_one_subscription_is_ambiguous(
    db_session: AsyncSession, stripe_fake
):
    """An org claimant plus a user who is NOT a member carrying the org's
    customer is not the legacy duplicate: ambiguous, nothing applied."""
    outsider = await _user(db_session, tier="team", stripe_customer_id="cus_out",
                           stripe_subscription_id="sub_amb_o")
    outsider_id = outsider.id
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, stripe_customer_id="cus_orgx",
                     stripe_subscription_id="sub_amb_o")
    org_id = org.id
    await _ent(db_session, "org", org_id, None, tier="team")  # manual, no Stripe binding
    stripe_fake.subscription("sub_amb_o", tier="team", quantity=3, customer="cus_orgx")

    await _process_stripe_event(
        _sub_event("sub_amb_o", "cus_orgx", "customer.subscription.updated"), db_session
    )
    assert await _org_state(db_session, org_id) == ("team", 5, "sub_amb_o")
    assert await _ents(db_session) == [("org", org_id, None, "active", "team")]
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.stripe_subscription_id).where(User.id == outsider_id)
    )).scalar_one() == "sub_amb_o"
    assert [r[0] for r in await _recon(db_session)] == ["ambiguous_subscription_pointer"]


@pytest.mark.asyncio
async def test_the_legacy_org_plus_member_duplicate_resolves_to_the_org(
    db_session: AsyncSession, stripe_fake
):
    """The one provable duplicate shape — the same one migration 075's
    backfill recognizes: exactly one org claimant, and every other claimant
    is a member of that org carrying the org's Stripe customer. The org
    wins and the member's stale pointer is cleared."""
    member = await _user(db_session, tier="team", stripe_customer_id="cus_lg",
                         stripe_subscription_id="sub_lg")
    member_id = member.id
    org = await _org(db_session, member_id, stripe_customer_id="cus_lg",
                     stripe_subscription_id="sub_lg")
    org_id = org.id
    stripe_fake.subscription("sub_lg", tier="team", quantity=9, customer="cus_lg")

    await _process_stripe_event(_sub_event("sub_lg", "cus_lg", "customer.subscription.updated"), db_session)
    assert await _org_state(db_session, org_id) == ("team", 9, "sub_lg")
    assert await _ents(db_session) == [("org", org_id, "sub_lg", "active", "team")]
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.stripe_subscription_id, User.stripe_customer_id).where(User.id == member_id)
    )).one() == (None, None)
    assert await _recon(db_session) == []


# ── round 8: checkout re-resolves the caller's membership under the locks ──


def _checkout_ctx(user, *, org=None, role=None, effective=None):
    from sessionfs.server.tier_gate import UserContext
    from sessionfs.server.tiers import Tier

    return UserContext(
        user=user, effective_tier=effective or Tier.FREE, org=org, role=role,
        is_org_user=org is not None,
    )


@pytest.mark.asyncio
async def test_a_demoted_admin_cannot_checkout_with_a_stale_admin_snapshot(
    db_session: AsyncSession, stripe_fake
):
    """The auth snapshot said owner; the membership was then changed to a
    plain member. The locked recheck inside the checkout decision must see
    the demotion, not the snapshot."""
    user = await _user(db_session)
    org = await _org(db_session, user.id, tier="free", seats_limit=0)
    member = (await db_session.execute(
        select(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == user.id)
    )).scalar_one()
    member.role = "member"  # demoted after the snapshot
    await db_session.commit()
    ctx = _checkout_ctx(user, org=org, role="owner")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        with pytest.raises(HTTPException) as exc:
            await billing.create_checkout(
                billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db_session
            )
    assert exc.value.status_code == 403
    assert "admin_required" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_removed_admin_cannot_checkout_with_a_stale_admin_snapshot(
    db_session: AsyncSession, stripe_fake
):
    from sqlalchemy import delete as sa_delete

    user = await _user(db_session)
    org = await _org(db_session, user.id, tier="free", seats_limit=0)
    await db_session.execute(sa_delete(OrgMember).where(OrgMember.org_id == org.id))
    await db_session.commit()
    ctx = _checkout_ctx(user, org=org, role="admin")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        with pytest.raises(HTTPException) as exc:
            await billing.create_checkout(
                billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db_session
            )
    assert exc.value.status_code == 403
    assert "admin_required" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_new_org_member_cannot_buy_a_personal_plan_with_a_stale_solo_snapshot(
    db_session: AsyncSession, stripe_fake
):
    """The snapshot was taken while the user had no org; they joined one
    before the checkout decision ran. The locked recheck must see it."""
    user = await _user(db_session)
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, tier="team")
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    await db_session.commit()
    ctx = _checkout_ctx(user)  # solo snapshot
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        with pytest.raises(HTTPException) as exc:
            await billing.create_checkout(billing.CheckoutRequest(tier="pro"), ctx=ctx, db=db_session)
    assert exc.value.status_code == 400
    assert "org_member_restriction" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_solo_team_checkout_after_joining_an_org_is_asked_to_retry(
    db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session)
    owner = await _user(db_session)
    org = await _org(db_session, owner.id, tier="team")
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="admin"))
    await db_session.commit()
    ctx = _checkout_ctx(user)  # solo snapshot, team plan, no org context
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        with pytest.raises(HTTPException) as exc:
            await billing.create_checkout(
                billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db_session
            )
    assert exc.value.status_code == 409
    assert "membership_changed" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_pending_personal_sub_on_a_distinct_customer_does_not_block_org_checkout(
    db_session: AsyncSession, stripe_fake
):
    """An incomplete personal subscription (first payment never made) leaves
    only ``stripe_pending_subscription_id`` on the user row. When that user
    later becomes an org admin and checks out for the org, the pending sub
    sits on their own Stripe customer, so it must not block the org checkout
    — the same different-customer exemption the active pointer already gets."""
    user = await _user(
        db_session,
        stripe_customer_id="cus_personal",
        stripe_pending_subscription_id="sub_pending",
    )
    org = await _org(
        db_session, user.id, tier="free", seats_limit=0, stripe_customer_id="cus_org"
    )
    ctx = _checkout_ctx(user, org=org, role="owner")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await billing.create_checkout(
            billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db_session
        )
    assert resp.checkout_url.startswith("https://checkout.example/")
    assert len(stripe_fake.checkout_calls) == 1


@pytest.mark.asyncio
async def test_a_pending_personal_sub_on_the_org_customer_still_blocks_org_checkout(
    db_session: AsyncSession, stripe_fake
):
    """A pending sub sharing the org's Stripe customer still blocks an org
    checkout, so a duplicate on that customer can't be created."""
    user = await _user(
        db_session,
        stripe_customer_id="cus_shared",
        stripe_pending_subscription_id="sub_pending",
    )
    org = await _org(
        db_session, user.id, tier="free", seats_limit=0, stripe_customer_id="cus_shared"
    )
    ctx = _checkout_ctx(user, org=org, role="owner")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        with pytest.raises(HTTPException) as exc:
            await billing.create_checkout(
                billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db_session
            )
    assert exc.value.status_code == 409
    assert "already_subscribed" in str(exc.value.detail)


# ── round 8: the customer-creation gap ──


@pytest.mark.asyncio
async def test_a_checkout_reservation_moves_to_a_new_org_and_so_does_the_customer(
    db_session: AsyncSession, stripe_fake
):
    """A user reserves a checkout (a customer-less intent) and creates an
    org before the customer attach finishes: org creation sees the
    reservation and takes it over, and the Stripe customer is attached to
    the reservation's CURRENT owner — the org — not the stale user."""
    from sessionfs.server.routes.org import CreateOrgRequest, create_organization

    user = await _user(db_session, tier="team")
    user_id = user.id
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        step = await billing._checkout_step(
            db_session, billing.CheckoutRequest(tier="team", seats=4), _PRICES["team"],
            user_id, None, False, 4,
        )
    assert step[0] == "customer"
    intent_id = step[1]
    intent = (await db_session.execute(
        select(BillingCheckoutIntent).where(BillingCheckoutIntent.id == intent_id)
    )).scalar_one()
    assert intent.stripe_customer_id is None and intent.owner_type == "user"

    # The org is created while Customer.create would be running.
    locked_user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    user_email = locked_user.email
    org = await create_organization(
        CreateOrgRequest(name="Move", slug=f"mv-{uuid.uuid4().hex[:6]}"),
        user=locked_user, db=db_session,
    )
    db_session.expire_all()
    intent = (await db_session.execute(
        select(BillingCheckoutIntent).where(BillingCheckoutIntent.id == intent_id)
    )).scalar_one()
    assert (intent.owner_type, intent.owner_id) == ("org", org.org_id)

    # The customer is attached to the reservation's current owner — the org.
    await billing._ensure_stripe_customer(
        db_session, stripe_fake, intent_id, "user", user_id, user_email, user_id, None
    )
    db_session.expire_all()
    assert (await db_session.execute(
        select(Organization.stripe_customer_id).where(Organization.id == org.org_id)
    )).scalar_one() == stripe_fake.customers[0]
    assert (await db_session.execute(
        select(User.stripe_customer_id).where(User.id == user_id)
    )).scalar_one() is None
    assert (await db_session.execute(
        select(BillingCheckoutIntent.stripe_customer_id)
        .where(BillingCheckoutIntent.id == intent_id)
    )).scalar_one() == stripe_fake.customers[0]


@pytest.mark.asyncio
async def test_org_creation_is_blocked_by_a_customer_less_personal_reservation(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A reservation made before the Stripe customer exists (no session id,
    no customer, no URL) still blocks org creation for a personal plan."""
    user = await _user(db_session, tier="team")
    headers = await _headers(db_session, user)
    db_session.add(BillingCheckoutIntent(
        id="bci_reserved", owner_type="user", owner_id=user.id, scope="personal", tier="pro",
        seats=1, idempotency_key="r", status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    ))
    await db_session.commit()
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "RP", "slug": "rp-owner"})
    assert resp.status_code == 409 and "personal_checkout_open" in resp.text


@pytest.mark.asyncio
async def test_a_lapsed_customer_less_reservation_expires_instead_of_blocking(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """An interrupted reservation (no Stripe session id was ever recorded
    for it) past its window simply expires — Stripe is not asked about a
    session id that was never stored."""
    user = await _user(db_session, stripe_customer_id="cus_lapsed")
    headers = await _headers(db_session, user)
    db_session.add(BillingCheckoutIntent(
        id="bci_interrupted", owner_type="user", owner_id=user.id, scope="personal", tier="pro",
        seats=1, idempotency_key="i", status="pending", stripe_customer_id="cus_lapsed",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert (await db_session.execute(
        select(BillingCheckoutIntent.status).where(BillingCheckoutIntent.id == "bci_interrupted")
    )).scalar_one() == "expired"
    assert len(stripe_fake.checkout_calls) == 1


@pytest.mark.asyncio
async def test_a_retry_adopts_the_crashed_requests_reservation(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A request that crashed between reserving and opening the session
    left a customer-less intent; the retry (same plan, same idempotency
    window) adopts it instead of failing on a conflicting open checkout."""
    user = await _user(db_session)
    user_id = user.id
    headers = await _headers(db_session, user)
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        step = await billing._checkout_step(
            db_session, billing.CheckoutRequest(tier="pro"), _PRICES["pro"],
            user_id, None, False, 1,
        )
        assert step[0] == "customer"
        intent_id = step[1]
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 200, resp.text
    intents = (await db_session.execute(
        select(BillingCheckoutIntent.id, BillingCheckoutIntent.status,
               BillingCheckoutIntent.stripe_session_id, BillingCheckoutIntent.stripe_customer_id)
        .where(BillingCheckoutIntent.owner_id == user_id)
    )).all()
    assert [(i[0], i[1]) for i in intents] == [(intent_id, "pending")]
    assert intents[0][2] is not None  # the session was opened on the same intent
    assert intents[0][3] == stripe_fake.customers[0]
    assert len(stripe_fake.customer_calls) == 1
    assert len(stripe_fake.checkout_calls) == 1


# ── round 8: the first-checkout sweep for pre-intent Stripe state ──


@pytest.mark.asyncio
async def test_an_untracked_open_stripe_checkout_session_blocks_a_new_checkout(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A Checkout session opened before intents existed (or out of band):
    the sweep finds it, records it once, and refuses."""
    user = await _user(db_session, stripe_customer_id="cus_sweep")
    headers = await _headers(db_session, user)
    stripe_fake.session("cs_legacy", "open", customer="cus_sweep")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        first = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
        second = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert first.status_code == 409 and "checkout_pending" in first.text
    assert second.status_code == 409 and "checkout_pending" in second.text
    assert stripe_fake.checkout_calls == []
    recon = (await db_session.execute(
        select(BillingReconciliation.kind, BillingReconciliation.stripe_customer_id)
    )).all()
    assert [tuple(r) for r in recon] == [("untracked_checkout_session", "cus_sweep")]


@pytest.mark.asyncio
async def test_an_untracked_live_subscription_blocks_a_new_checkout(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, stripe_customer_id="cus_sweep2")
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_legacy", tier="pro", customer="cus_sweep2")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 409 and "already_subscribed" in resp.text
    assert stripe_fake.checkout_calls == []
    recon = (await db_session.execute(
        select(BillingReconciliation.kind, BillingReconciliation.stripe_subscription_id)
    )).all()
    assert [tuple(r) for r in recon] == [("untracked_subscription", "sub_legacy")]


@pytest.mark.asyncio
async def test_the_sweep_passes_when_stripe_state_is_already_tracked(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """The open session Stripe reports is one an intent already records:
    the sweep passes and the new checkout proceeds."""
    user = await _user(db_session, stripe_customer_id="cus_known")
    headers = await _headers(db_session, user)
    stripe_fake.session("cs_known", "open", customer="cus_known")
    db_session.add(BillingCheckoutIntent(
        id="bci_done", owner_type="user", owner_id=user.id, scope="personal", tier="pro",
        seats=1, idempotency_key="done", status="completed", stripe_customer_id="cus_known",
        stripe_session_id="cs_known",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    ))
    await db_session.commit()
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 200, resp.text
    assert len(stripe_fake.checkout_calls) == 1
    assert await _recon(db_session) == []


@pytest.mark.asyncio
async def test_the_sweep_fails_closed_when_stripe_cannot_list(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, stripe_customer_id="cus_down")
    headers = await _headers(db_session, user)
    stripe_fake.list_error = APIConnectionError("stripe unreachable")
    with patch.dict(billing.TIER_PRICE_MAP, _PRICES):
        resp = await client.post("/api/v1/billing/checkout", headers=headers, json={"tier": "pro"})
    assert resp.status_code == 503 and "billing_unavailable" in resp.text
    assert stripe_fake.checkout_calls == []


# ── round 8: pointer-only org adoption needs Stripe's confirmation ──


@pytest.mark.asyncio
async def test_pointer_only_org_creation_is_refused_when_stripe_shows_canceled(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """No entitlement row anywhere, only the creator's cached pointer and
    tier cache: Stripe's canonical state (canceled) governs, not the cache."""
    user = await _user(db_session, tier="team", stripe_customer_id="cus_cx",
                       stripe_subscription_id="sub_cx")
    user_id = user.id
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_cx", status="canceled", tier="team", customer="cus_cx")
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "CX", "slug": "cx-owner"})
    assert resp.status_code == 409, resp.text
    assert "unconfirmed_subscription_adoption" in resp.text
    assert await _recon(db_session) == [("unconfirmed_subscription_adoption", "sub_cx", "user", user_id)]
    assert (await db_session.execute(
        select(Organization.id).where(Organization.slug == "cx-owner"))).all() == []


@pytest.mark.asyncio
async def test_pointer_only_org_creation_is_refused_for_a_personal_plan(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team", stripe_customer_id="cus_pp2",
                       stripe_subscription_id="sub_pp2")
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_pp2", tier="pro", customer="cus_pp2")  # active, but personal
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "PP2", "slug": "pp2-owner"})
    assert resp.status_code == 409 and "unconfirmed_subscription_adoption" in resp.text


@pytest.mark.asyncio
async def test_pointer_only_org_creation_is_refused_when_the_customer_differs(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team", stripe_customer_id="cus_mine",
                       stripe_subscription_id="sub_elsewhere")
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_elsewhere", tier="team", quantity=3, customer="cus_someone_else")
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "SE", "slug": "se-owner"})
    assert resp.status_code == 409 and "unconfirmed_subscription_adoption" in resp.text


@pytest.mark.asyncio
async def test_pointer_only_org_creation_fails_closed_when_stripe_is_unreachable(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    user = await _user(db_session, tier="team", stripe_customer_id="cus_dn",
                       stripe_subscription_id="sub_dn")
    headers = await _headers(db_session, user)
    stripe_fake.retrieve_error = APIConnectionError("stripe unreachable")
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "DN", "slug": "dn-owner"})
    assert resp.status_code == 503 and "billing_unavailable" in resp.text
    assert (await db_session.execute(
        select(Organization.id).where(Organization.slug == "dn-owner"))).all() == []


@pytest.mark.asyncio
async def test_pointer_only_adoption_derives_tier_and_seats_from_stripe(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """A pointer-only adoption must fund the org from Stripe's canonical tier
    and quantity, not the cached User.tier (team) or the plan defaults."""
    user = await _user(db_session, tier="team", stripe_customer_id="cus_ent",
                       stripe_subscription_id="sub_ent")
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_ent", tier="enterprise", quantity=9, customer="cus_ent")
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "Ent", "slug": "ent-owner"})
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]
    row = (await db_session.execute(
        select(Organization.tier, Organization.seats_limit, Organization.storage_limit_bytes)
        .where(Organization.id == org_id)
    )).one()
    assert tuple(row) == ("enterprise", 9, 0)


@pytest.mark.asyncio
async def test_pointer_only_adoption_records_the_synced_generation(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """The entitlement created by a pointer-only adoption records the fetch
    generation of the Stripe state it applied, and an observation row is
    written so later events can order against it."""
    from sessionfs.server.db.models import StripeSubscriptionObservation

    user = await _user(db_session, tier="team", stripe_customer_id="cus_syn",
                       stripe_subscription_id="sub_syn")
    headers = await _headers(db_session, user)
    stripe_fake.subscription("sub_syn", tier="team", quantity=4, customer="cus_syn")
    resp = await client.post("/api/v1/org", headers=headers, json={"name": "Syn", "slug": "syn-owner"})
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["org_id"]

    db_session.expire_all()
    ent_gen = (await db_session.execute(
        select(Entitlement.stripe_synced_generation).where(
            Entitlement.owner_type == "org", Entitlement.owner_id == org_id)
    )).scalar_one()
    obs_gen = (await db_session.execute(
        select(StripeSubscriptionObservation.generation).where(
            StripeSubscriptionObservation.subscription_id == "sub_syn")
    )).scalar_one()
    assert ent_gen is not None
    assert ent_gen == obs_gen


@pytest.mark.asyncio
async def test_pointer_only_adoption_is_refused_when_a_terminal_observation_supersedes_it(
    client: AsyncClient, db_session: AsyncSession, stripe_fake
):
    """Stripe still shows a granting Team subscription, but a cancellation was
    observed at a higher fetch generation. The pointer-only adoption must not
    synthesize a grant that terminal state supersedes."""
    from sessionfs.server.db.models import StripeSubscriptionObservation

    user = await _user(db_session, tier="team", stripe_customer_id="cus_term",
                       stripe_subscription_id="sub_term")
    user_id = user.id
    headers = await _headers(db_session, user)
    now = datetime.now(timezone.utc)
    db_session.add(StripeSubscriptionObservation(
        subscription_id="sub_term", status="canceled", customer_id="cus_term",
        observed_at=now, terminal_at=now, generation=1000, terminal_generation=1000,
    ))
    await db_session.commit()
    stripe_fake.subscription("sub_term", tier="team", quantity=4, customer="cus_term")

    resp = await client.post("/api/v1/org", headers=headers, json={"name": "Term", "slug": "term-owner"})
    assert resp.status_code == 409, resp.text
    assert "unconfirmed_subscription_adoption" in resp.text
    assert await _recon(db_session) == [("unconfirmed_subscription_adoption", "sub_term", "user", user_id)]
    assert (await db_session.execute(
        select(Organization.id).where(Organization.slug == "term-owner"))).all() == []


@pytest.mark.asyncio
async def test_pointer_only_org_creation_retries_when_the_pointer_changed(
    db_session: AsyncSession, stripe_fake
):
    """The pointer read at auth time (for the prefetch) can be cleared by a
    cancellation webhook before the row lock. Org creation must retry instead
    of skipping Stripe's confirmation and keeping a stale paid tier."""
    from sqlalchemy import update

    from sessionfs.server.routes.org import CreateOrgRequest, create_organization
    from sessionfs.server.services.entitlements import BillingLockRetry

    user = await _user(db_session, tier="team", stripe_customer_id="cus_chg",
                       stripe_subscription_id="sub_chg")
    stripe_fake.subscription("sub_chg", tier="team", quantity=4, customer="cus_chg")

    # A cancellation clears the pointer behind the identity map's back, so the
    # auth-time snapshot the function holds is now stale.
    await db_session.execute(
        update(User).where(User.id == user.id).values(stripe_subscription_id=None)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    with pytest.raises(BillingLockRetry):
        await create_organization(
            CreateOrgRequest(name="Changed", slug="changed-owner"), user=user, db=db_session
        )
    await db_session.rollback()
    assert (await db_session.execute(
        select(Organization.id).where(Organization.slug == "changed-owner"))).all() == []


# ── round 8: generations, not wall clocks, order fetches ──


@pytest.mark.asyncio
async def test_a_terminal_fetch_older_than_the_applied_grant_is_refused(
    db_session: AsyncSession, stripe_fake
):
    """A checkout granted generation 5; a delete fetched at generation 3
    (started before the grant's fetch) must not revoke it — even with a
    newer wall clock on the delete's fetch."""
    from sessionfs.server.routes.billing import (
        _CanonicalSubscription,
        _Fetched,
        _handle_checkout_completed,
        _handle_subscription_deleted,
    )

    user = await _user(db_session, stripe_customer_id="cus_gen")
    user_id = user.id
    grant = _Fetched(
        "sub_gen", datetime.now(timezone.utc), 5,
        canonical=_CanonicalSubscription("sub_gen", "active", "cus_gen", 1), tier="pro",
    )
    await _handle_checkout_completed(
        _checkout_event({"user_id": user_id, "tier": "pro", "seats": "1"}, "sub_gen", "cus_gen"),
        db_session, grant,
    )
    await db_session.commit()
    assert await _ents(db_session) == [("user", user_id, "sub_gen", "active", "pro")]

    older_terminal = _Fetched(
        "sub_gen", datetime.now(timezone.utc) + timedelta(minutes=5), 3,
        canonical=_CanonicalSubscription("sub_gen", "canceled", "cus_gen", 1),
    )
    await _handle_subscription_deleted(
        _sub_event("sub_gen", "cus_gen", "customer.subscription.deleted", status="canceled"),
        db_session, older_terminal,
    )
    await db_session.commit()
    assert await _ents(db_session) == [("user", user_id, "sub_gen", "active", "pro")]
    db_session.expire_all()
    assert (await db_session.execute(
        select(User.tier).where(User.id == user_id)
    )).scalar_one() == "pro"


# ── PostgreSQL: the universal lock order ──


pg_only = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
)


async def _pg_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.db.models import Base

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _pg_paid_org(factory, suffix: str):
    async with factory() as db:
        admin = User(id=str(uuid.uuid4()), email=f"adm-{suffix}@sessionfs.dev", tier="admin",
                     email_verified=True, created_at=datetime.now(timezone.utc))
        owner = User(id=str(uuid.uuid4()), email=f"own-{suffix}@example.com", tier="free",
                     email_verified=True, created_at=datetime.now(timezone.utc))
        db.add_all([admin, owner])
        await db.flush()
        org = Organization(id=f"org_{suffix}", name="Race", slug=f"race-{suffix}", tier="team",
                           seats_limit=5, storage_limit_bytes=5 * 1024**3,
                           stripe_customer_id=f"cus_{suffix}", stripe_subscription_id=f"sub_{suffix}")
        db.add(org)
        await db.flush()
        db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
        db.add(Entitlement(owner_type="org", owner_id=org.id, source="stripe",
                           source_ref=f"sub_{suffix}", tier="team", status="active", seats_limit=5))
        await db.commit()
        return admin, owner, org.id


async def _org_seats(factory, org_id: str) -> tuple[int, int]:
    async with factory() as db:
        org_seats = (await db.execute(
            select(Organization.seats_limit).where(Organization.id == org_id)
        )).scalar_one()
        ent_seats = (await db.execute(
            select(Entitlement.seats_limit).where(
                Entitlement.owner_type == "org", Entitlement.owner_id == org_id,
                Entitlement.status == "active")
        )).scalar_one()
    return org_seats, ent_seats


@pg_only
@pytest.mark.asyncio
async def test_pg_org_create_and_checkout_serialize_on_the_customer_lock():
    from sessionfs.server.routes.org import CreateOrgRequest, create_organization
    from sessionfs.server.services.entitlements import lock_stripe_customer

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        cus, sub = f"cus_pg_{suffix}", f"sub_pg_{suffix}"
        fake.subscription(sub, tier="team", quantity=4, customer=cus)
        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"pg-{suffix}@example.com", tier="team",
                        email_verified=True, created_at=datetime.now(timezone.utc),
                        stripe_customer_id=cus)
            db.add(user)
            await db.commit()

        with patch.object(billing, "_get_stripe", return_value=fake):
            async with factory() as tx_a, factory() as tx_b:
                # Transaction A (org creation) holds the customer lock first.
                await lock_stripe_customer(tx_a, cus)
                event = _checkout_event({"user_id": user.id, "tier": "team", "seats": "4"}, sub, cus)
                webhook = asyncio.create_task(_process_stripe_event(event, tx_b))
                await asyncio.sleep(0.5)
                assert not webhook.done(), "checkout must wait for the customer lock"

                user_a = (await tx_a.execute(select(User).where(User.id == user.id))).scalar_one()
                org = await create_organization(
                    CreateOrgRequest(name="PG Race", slug=f"pg-race-{suffix}"), user=user_a, db=tx_a
                )
                assert await asyncio.wait_for(webhook, timeout=10) == {"status": "ok"}

        async with factory() as db:
            rows = (await db.execute(
                select(Entitlement.owner_type, Entitlement.owner_id, Entitlement.status)
                .where(Entitlement.source == "stripe", Entitlement.source_ref == sub)
            )).all()
            assert [tuple(r) for r in rows] == [("org", org.org_id, "active")]
            assert (await db.execute(
                select(Entitlement.id).where(Entitlement.owner_type == "user",
                                             Entitlement.owner_id == user.id,
                                             Entitlement.status == "active")
            )).first() is None
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_admin_org_tier_change_waits_for_an_in_flight_subscription_update():
    """The webhook holds its locks (customer -> org row -> entitlement) and is
    paused just before writing; the admin change queues behind it instead of
    deadlocking, then applies on top."""
    from sessionfs.server.routes.admin import admin_change_org_tier

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        admin, _owner, org_id = await _pg_paid_org(factory, suffix)
        fake.subscription(f"sub_{suffix}", tier="team", quantity=12, customer=f"cus_{suffix}")
        locked, release = asyncio.Event(), asyncio.Event()
        real_apply = billing.apply_entitlement

        async def paused_apply(*args, **kwargs):
            locked.set()
            await release.wait()
            return await real_apply(*args, **kwargs)

        async def webhook():
            async with factory() as db:
                return await _process_stripe_event(
                    _sub_event(f"sub_{suffix}", f"cus_{suffix}", "customer.subscription.updated"), db
                )

        async def admin_change():
            async with factory() as db:
                return await admin_change_org_tier(org_id, {"seats_limit": 30}, admin=admin, db=db)

        with patch.object(billing, "_get_stripe", return_value=fake), \
                patch.object(billing, "apply_entitlement", paused_apply):
            hook = asyncio.create_task(webhook())
            await asyncio.wait_for(locked.wait(), timeout=10)
            admin_task = asyncio.create_task(admin_change())
            await asyncio.sleep(0.5)
            assert not admin_task.done(), "admin change must queue behind the webhook's locks"
            release.set()
            assert await asyncio.wait_for(hook, timeout=10) == {"status": "ok"}
            result = await asyncio.wait_for(admin_task, timeout=10)
        assert result["updated"] == {"seats_limit": 30}
        assert await _org_seats(factory, org_id) == (30, 30)
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_subscription_update_waits_for_an_in_flight_admin_change():
    """The reverse: an admin change holds the owner's billing locks; the
    webhook for the same owner queues behind it and then applies."""
    import sessionfs.server.routes.admin as admin_routes
    from sessionfs.server.routes.admin import admin_change_org_tier

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        admin, _owner, org_id = await _pg_paid_org(factory, suffix)
        fake.subscription(f"sub_{suffix}", tier="team", quantity=7, customer=f"cus_{suffix}")
        locked, release = asyncio.Event(), asyncio.Event()
        real_apply = admin_routes.apply_entitlement

        async def paused_apply(*args, **kwargs):
            locked.set()
            await release.wait()
            return await real_apply(*args, **kwargs)

        async def admin_change():
            async with factory() as db:
                return await admin_change_org_tier(org_id, {"seats_limit": 20}, admin=admin, db=db)

        async def webhook():
            async with factory() as db:
                return await _process_stripe_event(
                    _sub_event(f"sub_{suffix}", f"cus_{suffix}", "customer.subscription.updated"), db
                )

        with patch.object(admin_routes, "apply_entitlement", paused_apply), \
                patch.object(billing, "_get_stripe", return_value=fake):
            admin_task = asyncio.create_task(admin_change())
            await asyncio.wait_for(locked.wait(), timeout=10)
            hook = asyncio.create_task(webhook())
            await asyncio.sleep(0.5)
            assert not hook.done(), "webhook must queue behind the admin change's locks"
            release.set()
            assert (await asyncio.wait_for(admin_task, timeout=10))["updated"] == {"seats_limit": 20}
            assert await asyncio.wait_for(hook, timeout=10) == {"status": "ok"}
        assert await _org_seats(factory, org_id) == (7, 7)  # webhook applied last
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_concurrent_billing_paths_never_deadlock():
    """Admin org/user changes, subscription webhooks (org + user), checkout
    completion webhooks, org and personal checkout creation, and an org
    creation adopting a customer whose checkout is completing — all
    concurrent on the same owners and customers."""
    from sessionfs.server.routes.admin import admin_change_org_tier, change_user_tier
    from sessionfs.server.routes.org import CreateOrgRequest, create_organization
    from sessionfs.server.tier_gate import UserContext
    from sessionfs.server.tiers import Tier

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        admin, owner, org_id = await _pg_paid_org(factory, suffix)
        fake.subscription(f"sub_{suffix}", tier="team", quantity=6, customer=f"cus_{suffix}")
        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"pu-{suffix}@example.com", tier="pro",
                        email_verified=True, created_at=datetime.now(timezone.utc),
                        stripe_customer_id=f"cus_u_{suffix}", stripe_subscription_id=f"sub_u_{suffix}")
            db.add(user)
            await db.flush()
            db.add(Entitlement(owner_type="user", owner_id=user.id, source="stripe",
                               source_ref=f"sub_u_{suffix}", tier="pro", status="active"))
            # A second, unsubscribed org owned by the same owner user, so org
            # checkout creation runs its full path (customer, user, org,
            # intent) rather than stopping at "already subscribed".
            org2 = Organization(id=f"org2_{suffix}", name="Race2", slug=f"race2-{suffix}",
                                tier="free", seats_limit=0, storage_limit_bytes=0,
                                stripe_customer_id=f"cus_{suffix}")
            db.add(org2)
            await db.flush()
            # Checkout re-resolves the caller's membership under the locks:
            # the owner must be an admin member of org2 for its checkout to
            # run at all.
            db.add(OrgMember(org_id=org2.id, user_id=owner.id, role="owner"))
            # A user with no subscription (personal checkout full path) and a
            # buyer whose checkout completes while they create an org.
            shopper = User(id=str(uuid.uuid4()), email=f"shop-{suffix}@example.com", tier="free",
                           email_verified=True, created_at=datetime.now(timezone.utc))
            buyer = User(id=str(uuid.uuid4()), email=f"buy-{suffix}@example.com", tier="team",
                         email_verified=True, created_at=datetime.now(timezone.utc),
                         stripe_customer_id=f"cus_b_{suffix}")
            db.add_all([shopper, buyer])
            await db.commit()
        fake.subscription(f"sub_u_{suffix}", tier="pro", customer=f"cus_u_{suffix}")
        fake.subscription(f"sub_b_{suffix}", tier="team", quantity=4, customer=f"cus_b_{suffix}")

        async def org_admin(i):
            async with factory() as db:
                return await admin_change_org_tier(org_id, {"seats_limit": 10 + i}, admin=admin, db=db)

        async def user_admin(i):
            async with factory() as db:
                return await change_user_tier(user.id, {"tier": "pro" if i % 2 else "starter"},
                                              admin=admin, db=db)

        async def hook(sub, cus):
            async with factory() as db:
                return await _process_stripe_event(
                    _sub_event(sub, cus, "customer.subscription.updated"), db
                )

        async def org_checkout(target_org_id):
            async with factory() as db:
                owner_row = (await db.execute(select(User).where(User.id == owner.id))).scalar_one()
                org_row = (await db.execute(
                    select(Organization).where(Organization.id == target_org_id)
                )).scalar_one()
                ctx = UserContext(user=owner_row, effective_tier=Tier.TEAM, org=org_row,
                                  role="owner", is_org_user=True)
                try:
                    return await billing.create_checkout(
                        billing.CheckoutRequest(tier="team", seats=3), ctx=ctx, db=db
                    )
                except HTTPException as exc:  # 409 already_subscribed / checkout_pending
                    assert exc.status_code == 409, exc.detail
                    return exc

        async def personal_checkout():
            async with factory() as db:
                row = (await db.execute(select(User).where(User.id == shopper.id))).scalar_one()
                ctx = UserContext(user=row, effective_tier=Tier.FREE, org=None, role=None,
                                  is_org_user=False)
                try:
                    return await billing.create_checkout(billing.CheckoutRequest(tier="pro"), ctx=ctx, db=db)
                except HTTPException as exc:
                    assert exc.status_code == 409, exc.detail
                    return exc

        async def buyer_checkout_completed():
            async with factory() as db:
                return await _process_stripe_event(
                    _checkout_event({"user_id": buyer.id, "tier": "team", "seats": "4"},
                                    f"sub_b_{suffix}", f"cus_b_{suffix}"),
                    db,
                )

        async def buyer_creates_org():
            async with factory() as db:
                row = (await db.execute(select(User).where(User.id == buyer.id))).scalar_one()
                return await create_organization(
                    CreateOrgRequest(name="Buyer Co", slug=f"buyer-{suffix}"), user=row, db=db
                )

        with patch.object(billing, "_get_stripe", return_value=fake), \
                patch.dict(billing.TIER_PRICE_MAP, _PRICES):
            jobs = [buyer_creates_org()]
            for i in range(8):
                jobs += [
                    org_admin(i),
                    hook(f"sub_{suffix}", f"cus_{suffix}"),
                    user_admin(i),
                    hook(f"sub_u_{suffix}", f"cus_u_{suffix}"),
                    org_checkout(org_id),
                    org_checkout(f"org2_{suffix}"),
                    personal_checkout(),
                    buyer_checkout_completed(),
                ]
            results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException) and not isinstance(r, HTTPException)]
        assert errors == [], errors
        async with factory() as db:
            # Exactly one binding for the buyer's subscription, wherever it landed.
            bound = (await db.execute(
                select(Entitlement.owner_type, Entitlement.status).where(
                    Entitlement.source == "stripe", Entitlement.source_ref == f"sub_b_{suffix}")
            )).all()
            assert len(bound) == 1 and bound[0][1] == "active", bound
            shop_pending = (await db.execute(
                select(BillingCheckoutIntent.id).where(
                    BillingCheckoutIntent.owner_id == shopper.id,
                    BillingCheckoutIntent.status == "pending")
            )).all()
            assert len(shop_pending) == 1
        async with factory() as db:
            pending = (await db.execute(
                select(BillingCheckoutIntent.id).where(
                    BillingCheckoutIntent.owner_id == f"org2_{suffix}",
                    BillingCheckoutIntent.status == "pending")
            )).all()
        assert len(pending) == 1
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_concurrent_events_for_a_doubly_claimed_subscription_grant_nothing():
    """Two users carry the same subscription pointer (migration 075 leaves
    the shape unbound). Concurrent events for it lock EVERY claimant in the
    universal order — never deadlocking — and grant to no one; each delivery
    records only an ambiguous_subscription_pointer row."""
    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        cus, sub = f"cus_pg_a_{suffix}", f"sub_pg_a_{suffix}"
        fake.subscription(sub, tier="pro", customer=cus)
        async with factory() as db:
            first = User(id=str(uuid.uuid4()), email=f"a1-{suffix}@example.com", tier="pro",
                         email_verified=True, created_at=datetime.now(timezone.utc),
                         stripe_customer_id=cus, stripe_subscription_id=sub)
            second = User(id=str(uuid.uuid4()), email=f"a2-{suffix}@example.com", tier="pro",
                          email_verified=True, created_at=datetime.now(timezone.utc),
                          stripe_customer_id=f"cus2_{suffix}", stripe_subscription_id=sub)
            db.add_all([first, second])
            await db.commit()

        async def event():
            async with factory() as db:
                return await _process_stripe_event(
                    _sub_event(sub, cus, "customer.subscription.updated"), db
                )

        with patch.object(billing, "_get_stripe", return_value=fake):
            results = await asyncio.gather(*[event() for _ in range(6)])
        assert results == [{"status": "ok"}] * 6
        async with factory() as db:
            assert (await db.execute(
                select(Entitlement.id).where(Entitlement.source_ref == sub)
            )).all() == []
            recon = (await db.execute(
                select(BillingReconciliation.kind).where(
                    BillingReconciliation.stripe_subscription_id == sub)
            )).all()
            assert len(recon) == 6
            assert {r[0] for r in recon} == {"ambiguous_subscription_pointer"}
            tiers = (await db.execute(
                select(User.tier).where(User.stripe_subscription_id == sub)
            )).scalars().all()
            assert sorted(tiers) == ["pro", "pro"]  # nobody was granted or revoked
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_concurrent_fetches_apply_in_generation_order():
    """Two fetches of the same subscription START concurrently on the real
    database; the one with the higher generation (started later) must win
    even when it lands first and the older fetch arrives afterwards."""
    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        cus, sub = f"cus_pg_g_{suffix}", f"sub_pg_g_{suffix}"
        fake.subscription(sub, tier="team", quantity=6, customer=cus)
        async with factory() as db:
            org = Organization(id=f"orgg_{suffix}", name="Gen", slug=f"gen-{suffix}",
                               tier="team", seats_limit=5, storage_limit_bytes=5 * 1024**3,
                               stripe_customer_id=cus, stripe_subscription_id=sub)
            db.add(org)
            await db.flush()
            db.add(Entitlement(owner_type="org", owner_id=org.id, source="stripe",
                               source_ref=sub, tier="team", status="active", seats_limit=5))
            await db.commit()
            org_id = org.id

        # Fetch A starts (generation allocated + committed), then fetch B
        # starts — B's generation is higher by construction.
        with patch.object(billing, "_get_stripe", return_value=fake):
            async with factory() as db:
                fetch_a = await billing._fetch_subscription_state(db, sub)
            fake.subscription(sub, tier="team", quantity=11, customer=cus)
            async with factory() as db:
                fetch_b = await billing._fetch_subscription_state(db, sub)
        assert 0 < fetch_a.generation < fetch_b.generation

        # B lands first and applies; A arrives afterwards and is refused.
        async with factory() as db:
            await billing._handle_subscription_updated(
                _sub_event(sub, cus, "customer.subscription.updated"), db, fetch_b
            )
            await db.commit()
        assert await _org_seats(factory, org_id) == (11, 11)
        async with factory() as db:
            await billing._handle_subscription_updated(
                _sub_event(sub, cus, "customer.subscription.updated"), db, fetch_a
            )
            await db.commit()
        assert await _org_seats(factory, org_id) == (11, 11)
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_org_creation_retries_when_the_pointer_is_cleared_after_prefetch():
    """The pointer is prefetched (generation committed) and then cleared by a
    concurrent cancellation before the user row is locked. Org creation must
    retry via BillingLockRetry, never skip Stripe's confirmation and keep a
    stale paid tier."""
    from sqlalchemy import update

    from sessionfs.server.routes.org import CreateOrgRequest, create_organization
    from sessionfs.server.services.entitlements import BillingLockRetry

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        cus, sub = f"cus_pg_c_{suffix}", f"sub_pg_c_{suffix}"
        fake.subscription(sub, tier="team", quantity=4, customer=cus)
        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"chg-{suffix}@example.com", tier="team",
                        email_verified=True, created_at=datetime.now(timezone.utc),
                        stripe_customer_id=cus, stripe_subscription_id=sub)
            db.add(user)
            await db.commit()

        locked, release = asyncio.Event(), asyncio.Event()
        real_fetch = billing._fetch_subscription_state

        async def paused_fetch(db, sub_id):
            fetched = await real_fetch(db, sub_id)
            locked.set()
            await release.wait()
            return fetched

        async def clear_pointer():
            async with factory() as db:
                await db.execute(
                    update(User).where(User.id == user.id).values(stripe_subscription_id=None)
                )
                await db.commit()

        async def org_create():
            async with factory() as db:
                row = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
                return await create_organization(
                    CreateOrgRequest(name="PG Changed", slug=f"pg-chg-{suffix}"), user=row, db=db
                )

        with patch.object(billing, "_get_stripe", return_value=fake), \
                patch.object(billing, "_fetch_subscription_state", paused_fetch):
            task = asyncio.create_task(org_create())
            await asyncio.wait_for(locked.wait(), timeout=10)
            await clear_pointer()
            release.set()
            with pytest.raises(BillingLockRetry):
                await asyncio.wait_for(task, timeout=10)
        async with factory() as db:
            assert (await db.execute(
                select(Organization.id).where(Organization.slug == f"pg-chg-{suffix}")
            )).all() == []
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_pg_org_adoption_is_refused_when_a_higher_generation_terminal_fetch_lands_first():
    """A's prefetch starts first (lower generation) and pauses before returning
    its granting state; a cancellation observed at a higher generation commits
    a terminal observation. A's grant is superseded and the org creation is
    refused, not synthesized on top of a dead subscription."""
    from sessionfs.server.routes.org import CreateOrgRequest, create_organization

    engine, factory = await _pg_factory()
    fake = FakeStripe()
    try:
        suffix = uuid.uuid4().hex[:8]
        cus, sub = f"cus_pg_t_{suffix}", f"sub_pg_t_{suffix}"
        fake.subscription(sub, tier="team", quantity=4, customer=cus)
        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"term-{suffix}@example.com", tier="team",
                        email_verified=True, created_at=datetime.now(timezone.utc),
                        stripe_customer_id=cus, stripe_subscription_id=sub)
            db.add(user)
            await db.commit()

        locked, release = asyncio.Event(), asyncio.Event()
        real_fetch = billing._fetch_subscription_state
        calls = 0

        async def paused_fetch(db, sub_id):
            nonlocal calls
            calls += 1
            fetched = await real_fetch(db, sub_id)
            if calls == 1:
                # A: the granting prefetch is captured (generation N) and paused.
                locked.set()
                await release.wait()
            return fetched

        async def terminal_observation():
            # B: observes a cancellation at generation N+1 without touching the
            # user pointer (that path is the #3 guard's job).
            fake.subscription(sub, status="canceled", tier="team", quantity=4, customer=cus)
            async with factory() as db:
                fetched = await billing._fetch_subscription_state(db, sub)
                await billing._observe(db, fetched)
                await db.commit()

        async def org_create():
            async with factory() as db:
                row = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
                try:
                    return await create_organization(
                        CreateOrgRequest(name="PG Term", slug=f"pg-term-{suffix}"), user=row, db=db
                    )
                except HTTPException as exc:
                    return exc

        with patch.object(billing, "_get_stripe", return_value=fake), \
                patch.object(billing, "_fetch_subscription_state", paused_fetch):
            org_task = asyncio.create_task(org_create())
            await asyncio.wait_for(locked.wait(), timeout=10)
            await asyncio.wait_for(terminal_observation(), timeout=10)
            release.set()
            exc = await asyncio.wait_for(org_task, timeout=10)
        assert isinstance(exc, HTTPException) and exc.status_code == 409, exc
        async with factory() as db:
            assert (await db.execute(
                select(Organization.id).where(Organization.slug == f"pg-term-{suffix}")
            )).all() == []
            recon = (await db.execute(
                select(BillingReconciliation.kind, BillingReconciliation.stripe_subscription_id)
                .where(BillingReconciliation.stripe_subscription_id == sub)
            )).all()
            assert ("unconfirmed_subscription_adoption", sub) in [tuple(r) for r in recon]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deadlock_and_serialization_failures_answer_409_retry():
    """A transaction aborted by a lost race is a 409 with a retry hint, not a
    500; other database errors keep the default 500 path."""
    from fastapi import FastAPI
    from httpx import ASGITransport
    from sqlalchemy.exc import DBAPIError, OperationalError

    from sessionfs.server.errors import register_exception_handlers

    class _Orig(Exception):
        def __init__(self, sqlstate):
            super().__init__("db")
            self.sqlstate = sqlstate

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/deadlock")
    async def deadlock():
        raise DBAPIError("UPDATE ...", {}, _Orig("40P01"))

    @app.get("/serialization")
    async def serialization():
        raise OperationalError("UPDATE ...", {}, _Orig("40001"))

    @app.get("/other")
    async def other():
        raise DBAPIError("UPDATE ...", {}, _Orig("53300"))

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        for path in ("/deadlock", "/serialization"):
            resp = await c.get(path)
            assert resp.status_code == 409, resp.text
            assert resp.headers["retry-after"] == "1"
            body = resp.json()["error"]
            assert body["code"] == "concurrent_update"
            assert body["details"]["retryable"] is True
        assert (await c.get("/other")).status_code == 500
