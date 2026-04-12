"""Integration tests for Stripe webhook handlers.

Covers the handlers flagged in the Codex pre-release review as untested:
- checkout.session.completed (personal + org path)
- customer.subscription.updated (active, past_due, org routing)
- customer.subscription.deleted (personal + org path)
- _find_user_or_org_by_customer disambiguation via subscription_id

Tests call the private `_handle_*` functions directly with hand-crafted
event objects so we don't have to mock `stripe.Webhook.construct_event`
or sign real payloads. The handlers only use .data.object and a few
nested .get() calls, so a SimpleNamespace/dict hybrid works fine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Organization, OrgMember, User
from sessionfs.server.routes.billing import (
    _find_user_or_org_by_customer,
    _handle_checkout_completed,
    _handle_subscription_deleted,
    _handle_subscription_updated,
)


# ---------------------------------------------------------------------------
# Helpers — fake Stripe event objects + users/orgs
# ---------------------------------------------------------------------------


class FakeMetadata(dict):
    """Metadata dict that supports both .get() and attribute access."""


def _event(data_obj):
    """Wrap a dict/SimpleNamespace as a Stripe-event-shaped object."""
    return SimpleNamespace(data=SimpleNamespace(object=data_obj), id=f"evt_{uuid.uuid4().hex[:16]}")


def _checkout_session(user_id, tier, subscription_id, customer_id, org_id=None, seats=1):
    """Build a fake checkout.session.completed event payload."""
    metadata = FakeMetadata(user_id=user_id, tier=tier, seats=str(seats))
    if org_id:
        metadata["org_id"] = org_id

    obj = SimpleNamespace(
        metadata=metadata,
        subscription=subscription_id,
    )
    # The handler uses .get("customer", "") — SimpleNamespace needs this
    def _get(key, default=None):
        return {"customer": customer_id}.get(key, default)
    obj.get = _get
    return _event(obj)


def _subscription(subscription_id, customer_id, status="active", tier=None, seats=1):
    """Build a fake customer.subscription.* event payload.

    The handler looks up product metadata via stripe.Product.retrieve to get
    the tier, so we also patch that call inline in the tests that use this.
    """
    items_data = [{
        "price": {"product": f"prod_{tier}" if tier else "prod_test"},
        "quantity": seats,
    }]
    obj = SimpleNamespace(
        id=subscription_id,
        customer=customer_id,
        status=status,
    )
    def _get(key, default=None):
        return {"items": {"data": items_data}}.get(key, default)
    obj.get = _get
    return _event(obj)


async def _mk_user(db, *, email=None, tier="free", stripe_customer_id=None, stripe_subscription_id=None):
    u = User(
        id=str(uuid.uuid4()),
        email=email or f"{uuid.uuid4().hex[:8]}@example.com",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _mk_org(db, *, owner_user_id, tier="team", stripe_customer_id=None, stripe_subscription_id=None, seats_limit=5):
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier=tier,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
        seats_limit=seats_limit,
        storage_limit_bytes=seats_limit * 1024 * 1024 * 1024 if tier == "team" else 0,
    )
    db.add(org)
    member = OrgMember(org_id=org.id, user_id=owner_user_id, role="admin")
    db.add(member)
    await db.commit()
    await db.refresh(org)
    return org


class _FakeProduct:
    """Mock for stripe.Product.retrieve responses."""

    def __init__(self, tier: str):
        self.metadata = {"tier": tier}


def _patch_stripe_product(tier: str):
    """Context manager: patch _get_stripe() so Product.retrieve returns tier."""
    fake_stripe = SimpleNamespace(
        Product=SimpleNamespace(retrieve=lambda _pid: _FakeProduct(tier)),
    )
    return patch("sessionfs.server.routes.billing._get_stripe", return_value=fake_stripe)


# ---------------------------------------------------------------------------
# _handle_checkout_completed — personal path
# ---------------------------------------------------------------------------


class TestCheckoutCompleted:
    @pytest.mark.asyncio
    async def test_personal_checkout_sets_user_tier(self, db_session: AsyncSession):
        user = await _mk_user(db_session, tier="free")
        event = _checkout_session(
            user_id=user.id,
            tier="pro",
            subscription_id="sub_personal_001",
            customer_id="cus_personal_001",
        )

        await _handle_checkout_completed(event, db_session)

        refreshed = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.tier == "pro"
        assert refreshed.stripe_subscription_id == "sub_personal_001"

    @pytest.mark.asyncio
    async def test_org_checkout_updates_org_not_user(self, db_session: AsyncSession):
        user = await _mk_user(db_session, tier="free")
        org = await _mk_org(
            db_session,
            owner_user_id=user.id,
            tier="free",
            stripe_customer_id=None,
        )

        event = _checkout_session(
            user_id=user.id,
            tier="team",
            subscription_id="sub_org_001",
            customer_id="cus_org_001",
            org_id=org.id,
            seats=10,
        )

        await _handle_checkout_completed(event, db_session)

        refreshed_org = (await db_session.execute(select(Organization).where(Organization.id == org.id))).scalar_one()
        assert refreshed_org.tier == "team"
        assert refreshed_org.stripe_subscription_id == "sub_org_001"
        assert refreshed_org.seats_limit == 10
        # Org checkout must NOT write Stripe fields to the user
        refreshed_user = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed_user.stripe_subscription_id is None
        assert refreshed_user.tier == "free"  # user.tier unchanged

    @pytest.mark.asyncio
    async def test_missing_user_id_is_noop(self, db_session: AsyncSession):
        """Handler must not crash when metadata is incomplete."""
        event = _checkout_session(
            user_id=None,
            tier="pro",
            subscription_id="sub_x",
            customer_id="cus_x",
        )
        # Should return early without error
        await _handle_checkout_completed(event, db_session)


# ---------------------------------------------------------------------------
# _handle_subscription_updated — active path + past_due downgrade
# ---------------------------------------------------------------------------


class TestSubscriptionUpdated:
    @pytest.mark.asyncio
    async def test_active_personal_subscription_sets_user_tier(self, db_session: AsyncSession):
        user = await _mk_user(
            db_session, tier="free",
            stripe_customer_id="cus_u_001",
            stripe_subscription_id="sub_u_001",
        )
        event = _subscription("sub_u_001", "cus_u_001", status="active", tier="pro")

        with _patch_stripe_product("pro"):
            await _handle_subscription_updated(event, db_session)

        refreshed = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.tier == "pro"

    @pytest.mark.asyncio
    async def test_active_org_subscription_updates_org_not_user(self, db_session: AsyncSession):
        user = await _mk_user(db_session, tier="free")
        org = await _mk_org(
            db_session,
            owner_user_id=user.id,
            tier="free",
            stripe_customer_id="cus_org_002",
            stripe_subscription_id="sub_org_002",
        )

        event = _subscription("sub_org_002", "cus_org_002", status="active", tier="team", seats=8)

        with _patch_stripe_product("team"):
            await _handle_subscription_updated(event, db_session)

        refreshed_org = (await db_session.execute(select(Organization).where(Organization.id == org.id))).scalar_one()
        assert refreshed_org.tier == "team"
        assert refreshed_org.seats_limit == 8
        # User must NOT have its tier touched
        refreshed_user = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed_user.tier == "free"

    @pytest.mark.asyncio
    async def test_past_due_downgrades_user_to_free(self, db_session: AsyncSession):
        user = await _mk_user(
            db_session, tier="pro",
            stripe_customer_id="cus_u_002",
            stripe_subscription_id="sub_u_002",
        )
        event = _subscription("sub_u_002", "cus_u_002", status="past_due")

        await _handle_subscription_updated(event, db_session)

        refreshed = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.tier == "free"
        # stripe_subscription_id should be cleared on downgrade
        assert refreshed.stripe_subscription_id is None

    @pytest.mark.asyncio
    async def test_past_due_downgrades_org_to_free(self, db_session: AsyncSession):
        user = await _mk_user(db_session, tier="free")
        org = await _mk_org(
            db_session,
            owner_user_id=user.id,
            tier="team",
            stripe_customer_id="cus_org_003",
            stripe_subscription_id="sub_org_003",
            seats_limit=10,
        )
        event = _subscription("sub_org_003", "cus_org_003", status="unpaid")

        await _handle_subscription_updated(event, db_session)

        refreshed_org = (await db_session.execute(select(Organization).where(Organization.id == org.id))).scalar_one()
        assert refreshed_org.tier == "free"
        assert refreshed_org.stripe_subscription_id is None
        assert refreshed_org.seats_limit == 0
        assert refreshed_org.storage_limit_bytes == 0


# ---------------------------------------------------------------------------
# _handle_subscription_deleted — personal + org
# ---------------------------------------------------------------------------


class TestSubscriptionDeleted:
    @pytest.mark.asyncio
    async def test_personal_cancel_downgrades_user(self, db_session: AsyncSession):
        user = await _mk_user(
            db_session, tier="pro",
            stripe_customer_id="cus_u_003",
            stripe_subscription_id="sub_u_003",
        )
        event = _subscription("sub_u_003", "cus_u_003", status="canceled")

        await _handle_subscription_deleted(event, db_session)

        refreshed = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.tier == "free"
        assert refreshed.stripe_subscription_id is None

    @pytest.mark.asyncio
    async def test_org_cancel_downgrades_org_not_user(self, db_session: AsyncSession):
        user = await _mk_user(db_session, tier="free")
        org = await _mk_org(
            db_session,
            owner_user_id=user.id,
            tier="enterprise",
            stripe_customer_id="cus_org_004",
            stripe_subscription_id="sub_org_004",
            seats_limit=50,
        )

        event = _subscription("sub_org_004", "cus_org_004", status="canceled")

        await _handle_subscription_deleted(event, db_session)

        refreshed_org = (await db_session.execute(select(Organization).where(Organization.id == org.id))).scalar_one()
        assert refreshed_org.tier == "free"
        assert refreshed_org.stripe_subscription_id is None
        assert refreshed_org.seats_limit == 0
        # User must stay unchanged (Stripe fields were never on user)
        refreshed_user = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed_user.tier == "free"


# ---------------------------------------------------------------------------
# _find_user_or_org_by_customer — shared-customer disambiguation
# ---------------------------------------------------------------------------


class TestFindUserOrOrg:
    @pytest.mark.asyncio
    async def test_org_only_returns_org(self, db_session: AsyncSession):
        user = await _mk_user(db_session)
        org = await _mk_org(
            db_session,
            owner_user_id=user.id,
            stripe_customer_id="cus_only_org",
            stripe_subscription_id="sub_only_org",
        )

        u, o = await _find_user_or_org_by_customer("cus_only_org", db_session)
        assert u is None
        assert o is not None
        assert o.id == org.id

    @pytest.mark.asyncio
    async def test_user_only_returns_user(self, db_session: AsyncSession):
        user = await _mk_user(
            db_session,
            stripe_customer_id="cus_only_user",
            stripe_subscription_id="sub_only_user",
        )

        u, o = await _find_user_or_org_by_customer("cus_only_user", db_session)
        assert u is not None
        assert u.id == user.id
        assert o is None

    @pytest.mark.asyncio
    async def test_same_customer_different_subs_disambiguates_by_subscription(self, db_session: AsyncSession):
        """The legacy race where an org and a user share stripe_customer_id
        with DIFFERENT subscription IDs. The subscription_id parameter must
        route correctly — personal sub → user, org sub → org.
        """
        user = await _mk_user(
            db_session,
            stripe_customer_id="cus_shared",
            stripe_subscription_id="sub_personal_shared",
        )
        org = await _mk_org(
            db_session,
            owner_user_id=str(uuid.uuid4()),  # unrelated owner
            stripe_customer_id="cus_shared",
            stripe_subscription_id="sub_org_shared",
        )
        # Org owner doesn't exist as a User, which is fine for this lookup test

        # Personal subscription ID → should return user
        u, o = await _find_user_or_org_by_customer(
            "cus_shared", db_session, subscription_id="sub_personal_shared"
        )
        assert u is not None and u.id == user.id
        assert o is None

        # Org subscription ID → should return org
        u2, o2 = await _find_user_or_org_by_customer(
            "cus_shared", db_session, subscription_id="sub_org_shared"
        )
        assert u2 is None
        assert o2 is not None and o2.id == org.id
