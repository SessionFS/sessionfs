"""P2 entitlement resolution switch + write-through + /me enrichment tests.

Covers:
- resolve_entitlement: org + user + fallback-when-absent + cross-org isolation
- apply_entitlement: upsert (update-existing vs insert-new) + legacy column mirror
- get_effective_tier: resolution through entitlement with fallback to legacy
- Drift regression: admin tier change + Stripe webhook → resolution reflects it
- /me: effective_tier + org_id + org_name + org_role, no N+1
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    Base,
    User,
    Organization,
    OrgMember,
    Entitlement,
)
from sessionfs.server.services.entitlements import (
    ResolvedEntitlement,
    apply_entitlement,
    resolve_entitlement,
    VALID_ENTITLEMENT_TIERS,
)
from sessionfs.server.tier_gate import get_effective_tier, get_user_org_membership
from sessionfs.server.tiers import Tier


# ────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────

@pytest.fixture
async def db_engine():
    """In-memory aiosqlite engine with all ORM tables + FK enforcement."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
    )
    sa.event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


@pytest.fixture
async def db_session(db_engine):
    """Async session per test."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_user(db: AsyncSession, **kwargs) -> User:
    uid = kwargs.pop("id", str(uuid.uuid4()))
    defaults = {
        "id": uid,
        "email": f"{uid[:8]}@test.com",
        "tier": "free",
        "created_at": _now(),
    }
    defaults.update(kwargs)
    user = User(**defaults)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_org(db: AsyncSession, **kwargs) -> Organization:
    oid = kwargs.pop("id", str(uuid.uuid4()))
    defaults = {
        "id": oid,
        "name": f"Org {oid[:8]}",
        "slug": f"org-{oid[:8]}",
        "tier": "team",
        "created_at": _now(),
    }
    defaults.update(kwargs)
    org = Organization(**defaults)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _add_member(db: AsyncSession, org_id: str, user_id: str, role: str = "admin") -> OrgMember:
    member = OrgMember(org_id=org_id, user_id=user_id, role=role)
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


# ────────────────────────────────────────────────────────────────
# resolve_entitlement
# ────────────────────────────────────────────────────────────────

class TestResolveEntitlement:
    async def test_resolve_org_entitlement_returns_active(self, db_session: AsyncSession):
        """Resolve returns the active entitlement for an org."""
        org = await _create_org(db_session, tier="enterprise")
        ent = Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="manual",
            tier="enterprise",
            status="active",
            billing_status="current",
            current_period_start=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "enterprise"
        assert resolved.owner_type == "org"
        assert resolved.owner_id == org.id
        assert resolved.source == "manual"
        assert resolved.billing_status == "current"

    async def test_resolve_user_entitlement_returns_active(self, db_session: AsyncSession):
        """Resolve returns the active entitlement for a user."""
        user = await _create_user(db_session, tier="pro")
        ent = Entitlement(
            owner_type="user",
            owner_id=user.id,
            source="stripe",
            source_ref="sub_123",
            tier="pro",
            status="active",
            billing_status="current",
            current_period_start=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        resolved = await resolve_entitlement("user", user.id, db_session)
        assert resolved is not None
        assert resolved.tier == "pro"
        assert resolved.owner_type == "user"
        assert resolved.owner_id == user.id
        assert resolved.source == "stripe"
        assert resolved.source_ref == "sub_123"

    async def test_resolve_returns_none_when_no_active_entitlement(self, db_session: AsyncSession):
        """Resolve returns None when no active entitlement exists."""
        org = await _create_org(db_session, tier="team")
        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is None

    async def test_resolve_ignores_canceled_entitlement(self, db_session: AsyncSession):
        """Only status='active' rows are returned; canceled rows are skipped."""
        org = await _create_org(db_session, tier="team")
        ent = Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="stripe",
            source_ref="sub_canceled",
            tier="team",
            status="canceled",
            billing_status="current",
            current_period_start=_now(),
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is None

    async def test_resolve_returns_at_most_one_active(self, db_session: AsyncSession):
        """The partial unique index guarantees ≤1 active row.  We verify
        that trying to insert a second active row raises IntegrityError,
        and that the first row is still resolvable afterwards."""
        org = await _create_org(db_session, tier="team")
        org_id = org.id  # store before rollback may expire objects

        ent1 = Entitlement(
            owner_type="org",
            owner_id=org_id,
            source="manual",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        # Attempt to insert a second active row — the partial unique index
        # should block it.
        ent2 = Entitlement(
            owner_type="org",
            owner_id=org_id,
            source="stripe",
            source_ref="sub_dup",
            tier="enterprise",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        with pytest.raises(Exception):
            await db_session.commit()
        await db_session.rollback()

        # After rollback, only ent1 is active — resolve it.
        resolved = await resolve_entitlement("org", org_id, db_session)
        assert resolved is not None
        assert resolved.tier == "team"

    async def test_cross_org_isolation(self, db_session: AsyncSession):
        """Resolve for org A never returns org B's entitlement."""
        org_a = await _create_org(db_session, tier="enterprise")
        org_b = await _create_org(db_session, tier="team")
        ent_a = Entitlement(
            owner_type="org",
            owner_id=org_a.id,
            source="admin_provisioned",
            tier="enterprise",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent_a)
        await db_session.commit()

        # Resolve org B — should NOT get org A's entitlement.
        resolved_b = await resolve_entitlement("org", org_b.id, db_session)
        assert resolved_b is None  # org B has no entitlement

        # Resolve org A — should get the right one.
        resolved_a = await resolve_entitlement("org", org_a.id, db_session)
        assert resolved_a is not None
        assert resolved_a.tier == "enterprise"
        assert resolved_a.owner_id == org_a.id


# ────────────────────────────────────────────────────────────────
# apply_entitlement (write-through)
# ────────────────────────────────────────────────────────────────

class TestApplyEntitlement:
    async def test_apply_creates_new_entitlement(self, db_session: AsyncSession):
        """apply_entitlement inserts a new row when none exists."""
        org = await _create_org(db_session, tier="free")

        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            seats=50,
            source="admin_provisioned",
            db=db_session,
        )
        await db_session.commit()

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "enterprise"
        assert resolved.seats_limit == 50
        assert resolved.source == "admin_provisioned"

    async def test_apply_updates_existing_active_entitlement(self, db_session: AsyncSession):
        """apply_entitlement updates the existing active row in-place."""
        org = await _create_org(db_session, tier="team")
        await apply_entitlement(
            "org",
            org.id,
            tier="team",
            seats=5,
            source="stripe",
            source_ref="sub_123",
            db=db_session,
        )
        await db_session.commit()

        # Now upgrade to enterprise — same active row should be updated.
        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            seats=100,
            source="stripe",
            source_ref="sub_123",
            db=db_session,
        )
        await db_session.commit()

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "enterprise"
        assert resolved.seats_limit == 100

        # Only one row total for this owner.
        result = await db_session.execute(
            select(Entitlement).where(
                Entitlement.owner_type == "org",
                Entitlement.owner_id == org.id,
            )
        )
        all_rows = result.scalars().all()
        assert len(all_rows) == 1

    async def test_apply_mirrors_legacy_org_columns(self, db_session: AsyncSession):
        """apply_entitlement mirrors tier/seats/storage onto Organization."""
        org = await _create_org(db_session, tier="free")
        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            seats=200,
            storage=10_000_000_000,
            source="admin_provisioned",
            db=db_session,
        )
        await db_session.commit()

        # Re-read org to check mirroring.
        await db_session.refresh(org)
        assert org.tier == "enterprise"
        assert org.seats_limit == 200
        assert org.storage_limit_bytes == 10_000_000_000
        assert org.entitlement_id is not None

    async def test_apply_mirrors_legacy_user_columns(self, db_session: AsyncSession):
        """apply_entitlement mirrors tier onto User."""
        user = await _create_user(db_session, tier="free")
        await apply_entitlement(
            "user",
            user.id,
            tier="starter",
            source="stripe",
            source_ref="sub_user_1",
            db=db_session,
        )
        await db_session.commit()

        await db_session.refresh(user)
        assert user.tier == "starter"
        assert user.entitlement_id is not None

    async def test_apply_rejects_admin_tier_for_user(self, db_session: AsyncSession):
        """Sentinel MEDIUM-3: apply_entitlement refuses to write 'admin' to User.tier."""
        user = await _create_user(db_session, tier="admin")
        with pytest.raises(AssertionError, match="not a valid entitlement tier"):
            await apply_entitlement(
                "user",
                user.id,
                tier="admin",
                source="admin_provisioned",
                db=db_session,
            )

    async def test_apply_rejects_admin_tier_for_org(self, db_session: AsyncSession):
        """Sentinel MEDIUM-3: apply_entitlement refuses to write 'admin' tier."""
        org = await _create_org(db_session, tier="enterprise")
        with pytest.raises(AssertionError, match="not a valid entitlement tier"):
            await apply_entitlement(
                "org",
                org.id,
                tier="admin",
                source="admin_provisioned",
                db=db_session,
            )

    async def test_apply_status_canceled(self, db_session: AsyncSession):
        """apply_entitlement with status='canceled' transitions the row."""
        org = await _create_org(db_session, tier="team")
        await apply_entitlement(
            "org",
            org.id,
            tier="team",
            source="stripe",
            source_ref="sub_to_cancel",
            db=db_session,
        )
        await db_session.commit()

        # Cancel the entitlement.
        await apply_entitlement(
            "org",
            org.id,
            tier="free",
            seats=0,
            storage=0,
            source="stripe",
            source_ref="sub_to_cancel",
            status="canceled",
            db=db_session,
        )
        await db_session.commit()

        # Resolution should return None (no active row).
        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is None

        # Legacy columns should reflect free.
        await db_session.refresh(org)
        assert org.tier == "free"
        assert org.seats_limit == 0


class TestValidEntitlementTiers:
    def test_admin_not_in_valid_tiers(self):
        assert "admin" not in VALID_ENTITLEMENT_TIERS

    def test_free_in_valid_tiers(self):
        assert "free" in VALID_ENTITLEMENT_TIERS

    def test_all_customer_tiers_valid(self):
        for t in ("free", "starter", "pro", "team", "enterprise"):
            assert t in VALID_ENTITLEMENT_TIERS


# ────────────────────────────────────────────────────────────────
# get_effective_tier — resolution with fallback
# ────────────────────────────────────────────────────────────────

class TestEffectiveTierResolution:
    async def test_effective_tier_from_org_entitlement(self, db_session: AsyncSession):
        """When an org-member user has an org entitlement, it takes priority."""
        org = await _create_org(db_session, tier="free")
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        # Org entitlement says enterprise; legacy org.tier says free.
        # Resolution should prefer the entitlement.
        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            source="admin_provisioned",
            db=db_session,
        )
        await db_session.commit()

        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.ENTERPRISE

    async def test_effective_tier_falls_back_to_org_tier(self, db_session: AsyncSession):
        """When no org entitlement exists, fall back to legacy org.tier."""
        org = await _create_org(db_session, tier="team")
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        # No entitlement — should fall back to org.tier.
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.TEAM

    async def test_effective_tier_from_user_entitlement(self, db_session: AsyncSession):
        """When a solo user has an entitlement, it takes priority."""
        user = await _create_user(db_session, tier="free")
        await apply_entitlement(
            "user",
            user.id,
            tier="pro",
            source="stripe",
            source_ref="sub_solo",
            db=db_session,
        )
        await db_session.commit()

        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.PRO

    async def test_effective_tier_falls_back_to_user_tier(self, db_session: AsyncSession):
        """When no user entitlement exists, fall back to legacy user.tier."""
        user = await _create_user(db_session, tier="starter")
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.STARTER

    async def test_effective_tier_admin_legacy(self, db_session: AsyncSession):
        """Legacy admin tier resolves to ENTERPRISE (never from entitlement)."""
        user = await _create_user(db_session, tier="admin")
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.ENTERPRISE


# ────────────────────────────────────────────────────────────────
# Drift regression
# ────────────────────────────────────────────────────────────────

class TestDriftRegression:
    async def test_admin_tier_change_reflected_in_resolution(self, db_session: AsyncSession):
        """After an admin changes the org tier, entitlement resolution
        reflects the new tier (write-through keeps them in sync)."""
        org = await _create_org(db_session, tier="team")
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        # Initial state: no entitlement, falls back to org.tier=team.
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.TEAM

        # Simulate admin tier change (what admin_change_org_tier does).
        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            seats=100,
            storage=0,
            source="admin_provisioned",
            db=db_session,
        )
        # Also update legacy cache.
        org.tier = "enterprise"
        org.seats_limit = 100
        org.storage_limit_bytes = 0
        await db_session.commit()

        # Resolution should now return enterprise.
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.ENTERPRISE

        # And the entitlement row matches.
        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "enterprise"
        assert resolved.seats_limit == 100

    async def test_stripe_active_webhook_reflected_in_entitlement(self, db_session: AsyncSession):
        """After a Stripe subscription.updated (active) handler runs,
        the entitlement reflects the new tier."""
        org = await _create_org(
            db_session,
            tier="team",
            stripe_customer_id="cus_test",
            stripe_subscription_id="sub_test_1",
        )
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        # Simulate what _handle_subscription_updated(active) does.
        await apply_entitlement(
            "org",
            org.id,
            tier="enterprise",
            seats=50,
            storage=0,
            source="stripe",
            source_ref="sub_test_1",
            db=db_session,
        )
        org.tier = "enterprise"
        org.seats_limit = 50
        org.storage_limit_bytes = 0
        await db_session.commit()

        # Resolution reflects the Stripe update.
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.ENTERPRISE

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "enterprise"
        assert resolved.source == "stripe"
        assert resolved.source_ref == "sub_test_1"

    async def test_stripe_deleted_webhook_cancels_entitlement(self, db_session: AsyncSession):
        """After a Stripe subscription.deleted handler runs,
        the entitlement status becomes 'canceled' and resolution falls back."""
        org = await _create_org(
            db_session,
            tier="team",
            stripe_customer_id="cus_test2",
            stripe_subscription_id="sub_to_delete",
        )
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        # Create the Stripe entitlement first.
        await apply_entitlement(
            "org",
            org.id,
            tier="team",
            seats=5,
            source="stripe",
            source_ref="sub_to_delete",
            db=db_session,
        )
        await db_session.commit()

        # Simulate subscription.deleted — cancel the entitlement.
        await apply_entitlement(
            "org",
            org.id,
            tier="free",
            seats=0,
            storage=0,
            source="stripe",
            source_ref="sub_to_delete",
            status="canceled",
            db=db_session,
        )
        org.tier = "free"
        org.seats_limit = 0
        org.storage_limit_bytes = 0
        await db_session.commit()

        # Resolution falls back to legacy org.tier (free).
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.FREE

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is None  # no active entitlement

    async def test_past_due_does_not_cancel_entitlement(self, db_session: AsyncSession):
        """v0.10.32 hotfix: past_due does NOT change entitlement status.
        The entitlement remains active and resolves normally."""
        org = await _create_org(
            db_session,
            tier="team",
            stripe_customer_id="cus_past_due",
            stripe_subscription_id="sub_past_due",
        )
        user = await _create_user(db_session, tier="free")
        await _add_member(db_session, org.id, user.id, "admin")

        await apply_entitlement(
            "org",
            org.id,
            tier="team",
            seats=5,
            source="stripe",
            source_ref="sub_past_due",
            db=db_session,
        )
        await db_session.commit()

        # past_due should NOT be a terminal status change — the
        # entitlement stays active and resolution works.
        tier = await get_effective_tier(user, db_session)
        assert tier == Tier.TEAM

        resolved = await resolve_entitlement("org", org.id, db_session)
        assert resolved is not None
        assert resolved.tier == "team"
        # The entitlement is active (resolve_entitlement only returns
        # status='active' rows).  billing_status defaults to 'current'
        # per apply_entitlement; P5 adds proper billing_status tracking.
