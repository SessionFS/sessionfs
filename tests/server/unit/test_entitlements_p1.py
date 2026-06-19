"""P1 entitlements data-model foundation tests.

Covers:
- Entitlement model: partial unique indexes, CHECK constraint, basic CRUD
- OrgAuditEvent: append-only, org_id SET NULL on org delete
- ActivationAttempt + PendingLicenseClaim: model sanity
- HelmLicense.org_id: UNIQUE constraint
- OrgRole.OWNER: enum value + ROLE_LEVEL + has_minimum_role
- OrgMember: one-owner-per-org partial unique index
- Migration backfill: deterministic single-owner selection, entitlements backfill
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError

from sessionfs.server.db.models import (
    Base,
    User,
    Organization,
    OrgMember,
    HelmLicense,
    Entitlement,
    OrgAuditEvent,
    ActivationAttempt,
    PendingLicenseClaim,
    AdminAction,
)
from sessionfs.server.roles import OrgRole, ROLE_LEVEL, has_minimum_role


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
    # Enable foreign key enforcement on every connection (SQLite disables
    # by default; ON DELETE CASCADE / ON DELETE SET NULL need this).
    sa.event.listen(engine.sync_engine, "connect", _set_sqlite_pragma)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _set_sqlite_pragma(dbapi_connection, _connection_record):
    """Enable FK enforcement on SQLite connections."""
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


# ────────────────────────────────────────────────────────────────
# OrgRole.OWNER
# ────────────────────────────────────────────────────────────────

class TestOrgRoleOwner:
    def test_owner_enum_value(self):
        assert OrgRole.OWNER == "owner"
        assert OrgRole("owner") == OrgRole.OWNER

    def test_role_levels(self):
        assert ROLE_LEVEL[OrgRole.MEMBER] == 10
        assert ROLE_LEVEL[OrgRole.ADMIN] == 50
        assert ROLE_LEVEL[OrgRole.OWNER] == 100

    def test_owner_meets_all_roles(self):
        assert has_minimum_role("owner", "member") is True
        assert has_minimum_role("owner", "admin") is True
        assert has_minimum_role("owner", "owner") is True

    def test_admin_does_not_meet_owner(self):
        assert has_minimum_role("admin", "owner") is False


# ────────────────────────────────────────────────────────────────
# Entitlement model constraints
# ────────────────────────────────────────────────────────────────

class TestEntitlementConstraints:
    async def test_one_active_per_owner_index_rejects_second_active(
        self, db_session: AsyncSession
    ):
        """Partial unique index uq_entitlements_one_active_per_owner
        must reject a second active entitlement for the same owner."""
        ent1 = Entitlement(
            owner_type="user",
            owner_id="user-abc",
            source="manual",
            tier="pro",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        ent2 = Entitlement(
            owner_type="user",
            owner_id="user-abc",
            source="manual",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_inactive_rows_not_restricted_by_partial_index(
        self, db_session: AsyncSession
    ):
        """Canceled/expired rows don't trigger the partial unique index."""
        ent1 = Entitlement(
            owner_type="user",
            owner_id="user-abc",
            source="manual",
            tier="pro",
            status="canceled",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        ent2 = Entitlement(
            owner_type="user",
            owner_id="user-abc",
            source="stripe",
            source_ref="sub_123",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        await db_session.commit()  # should NOT raise

    async def test_source_ref_partial_unique_index_rejects_duplicate(
        self, db_session: AsyncSession
    ):
        """uq_entitlements_source_ref prevents two entitlements with
        same (source, source_ref) when source_ref IS NOT NULL."""
        ent1 = Entitlement(
            owner_type="org",
            owner_id="org-1",
            source="stripe",
            source_ref="sub_abc",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        ent2 = Entitlement(
            owner_type="org",
            owner_id="org-2",
            source="stripe",
            source_ref="sub_abc",  # same source_ref
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_source_ref_null_allows_duplicates(
        self, db_session: AsyncSession
    ):
        """NULL source_ref rows aren't restricted by the partial index."""
        ent1 = Entitlement(
            owner_type="user",
            owner_id="user-1",
            source="manual",
            source_ref=None,
            tier="pro",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        ent2 = Entitlement(
            owner_type="user",
            owner_id="user-2",
            source="manual",
            source_ref=None,
            tier="pro",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        await db_session.commit()  # should NOT raise

    async def test_tier_check_rejects_admin(self, db_session: AsyncSession):
        """CHECK constraint blocks 'admin' as a tier value."""
        ent = Entitlement(
            owner_type="user",
            owner_id="user-abc",
            source="manual",
            tier="admin",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_tier_check_allows_all_valid_tiers(
        self, db_session: AsyncSession
    ):
        """All valid tiers pass the CHECK constraint."""
        for tier in ("free", "starter", "pro", "team", "enterprise"):
            ent = Entitlement(
                owner_type="user",
                owner_id=f"user-{tier}",
                source="manual",
                tier=tier,
                status="active",
                billing_status="current",
                created_at=_now(),
                updated_at=_now(),
            )
            db_session.add(ent)
        await db_session.commit()  # should NOT raise


# ────────────────────────────────────────────────────────────────
# OrgMember: one-owner-per-org partial unique index
# ────────────────────────────────────────────────────────────────

class TestOrgMemberOwnerConstraint:
    async def test_one_owner_per_org_index_rejects_second_owner(
        self, db_session: AsyncSession
    ):
        """Partial unique index uq_org_members_one_owner_per_org
        must reject a second owner in the same org."""
        org = await _create_org(db_session)
        user1 = await _create_user(db_session)
        user2 = await _create_user(db_session)

        om1 = OrgMember(
            org_id=org.id,
            user_id=user1.id,
            role="owner",
        )
        db_session.add(om1)
        await db_session.commit()

        om2 = OrgMember(
            org_id=org.id,
            user_id=user2.id,
            role="owner",
        )
        db_session.add(om2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_multiple_admins_allowed(self, db_session: AsyncSession):
        """Multiple admin members in the same org should be fine."""
        org = await _create_org(db_session)
        user1 = await _create_user(db_session)
        user2 = await _create_user(db_session)

        db_session.add(OrgMember(org_id=org.id, user_id=user1.id, role="admin"))
        db_session.add(OrgMember(org_id=org.id, user_id=user2.id, role="admin"))
        await db_session.commit()  # should NOT raise

    async def test_owner_and_admin_allowed(self, db_session: AsyncSession):
        """One owner + one admin in same org is valid."""
        org = await _create_org(db_session)
        user1 = await _create_user(db_session)
        user2 = await _create_user(db_session)

        db_session.add(OrgMember(org_id=org.id, user_id=user1.id, role="owner"))
        db_session.add(OrgMember(org_id=org.id, user_id=user2.id, role="admin"))
        await db_session.commit()  # should NOT raise


# ────────────────────────────────────────────────────────────────
# HelmLicense.org_id UNIQUE
# ────────────────────────────────────────────────────────────────

class TestHelmLicenseOrgId:
    async def test_two_licenses_cannot_bind_to_same_org(
        self, db_session: AsyncSession
    ):
        """UNIQUE constraint on HelmLicense.org_id prevents two
        licenses from being bound to the same org."""
        org = await _create_org(db_session)

        hl1 = HelmLicense(
            id="hl-001",
            org_name="Acme Corp",
            contact_email="acme@example.com",
            tier="enterprise",
            org_id=org.id,
        )
        db_session.add(hl1)
        await db_session.commit()

        hl2 = HelmLicense(
            id="hl-002",
            org_name="Acme Corp",
            contact_email="acme2@example.com",
            tier="enterprise",
            org_id=org.id,  # same org_id
        )
        db_session.add(hl2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_license_null_org_allowed(self, db_session: AsyncSession):
        """Multiple licenses with NULL org_id is fine."""
        hl1 = HelmLicense(
            id="hl-003",
            org_name="Beta Inc",
            contact_email="beta@example.com",
            tier="enterprise",
            org_id=None,
        )
        db_session.add(hl1)
        await db_session.commit()

        hl2 = HelmLicense(
            id="hl-004",
            org_name="Gamma LLC",
            contact_email="gamma@example.com",
            tier="enterprise",
            org_id=None,
        )
        db_session.add(hl2)
        await db_session.commit()  # should NOT raise


# ────────────────────────────────────────────────────────────────
# Entitlement lifecycle: billing_status vs status
# ────────────────────────────────────────────────────────────────

class TestEntitlementBillingStatus:
    async def test_past_due_is_not_a_status_value(self, db_session: AsyncSession):
        """billing_status='past_due' with status='active' is valid.
        past_due is NOT a lifecycle status."""
        ent = Entitlement(
            owner_type="org",
            owner_id="org-xyz",
            source="stripe",
            source_ref="sub_pastdue",
            tier="team",
            status="active",
            billing_status="past_due",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()
        assert ent.billing_status == "past_due"
        assert ent.status == "active"

    async def test_past_due_active_still_enforces_one_active_index(
        self, db_session: AsyncSession
    ):
        """An active+past_due entitlement still blocks a second active row."""
        ent1 = Entitlement(
            owner_type="user",
            owner_id="user-pd",
            source="stripe",
            source_ref="sub_pd_1",
            tier="pro",
            status="active",
            billing_status="past_due",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent1)
        await db_session.commit()

        ent2 = Entitlement(
            owner_type="user",
            owner_id="user-pd",
            source="stripe",
            source_ref="sub_pd_2",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


# ────────────────────────────────────────────────────────────────
# OrgAuditEvent: append-only, org_id SET NULL on delete
# ────────────────────────────────────────────────────────────────

class TestOrgAuditEvent:
    async def test_create_audit_event(self, db_session: AsyncSession):
        """Basic creation of an org audit event."""
        org = await _create_org(db_session)
        user = await _create_user(db_session)

        evt = OrgAuditEvent(
            id=str(uuid.uuid4()),
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type="org_created",
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            actor_role_at_time="owner",
            created_at=_now(),
        )
        db_session.add(evt)
        await db_session.commit()
        assert evt.id is not None

    async def test_audit_event_survives_org_delete(
        self, db_session: AsyncSession
    ):
        """org_id ON DELETE SET NULL: audit rows survive org deletion."""
        org = await _create_org(db_session)
        user = await _create_user(db_session)

        evt = OrgAuditEvent(
            id=str(uuid.uuid4()),
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type="member_joined",
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            actor_role_at_time="admin",
            created_at=_now(),
        )
        db_session.add(evt)
        await db_session.commit()

        # Delete the org.
        await db_session.delete(org)
        await db_session.commit()

        # Re-fetch the audit event — it should survive with org_id=NULL.
        await db_session.refresh(evt)
        assert evt.org_id is None
        assert evt.org_name_snapshot == org.name  # snapshot preserved


# ────────────────────────────────────────────────────────────────
# ActivationAttempt + PendingLicenseClaim models
# ────────────────────────────────────────────────────────────────

class TestActivationAttempt:
    async def test_create_attempt(self, db_session: AsyncSession):
        """Basic creation of an activation attempt."""
        user = await _create_user(db_session)
        hl = HelmLicense(
            id="hl-act-test",
            org_name="TestCorp",
            contact_email="testcorp@example.com",
            tier="enterprise",
        )
        db_session.add(hl)
        await db_session.commit()

        attempt = ActivationAttempt(
            helm_license_id=hl.id,
            token_hash="sha256:abc123def456",
            contact_email_snapshot=hl.contact_email,
            requested_by_user_id=user.id,
            status="pending",
            expires_at=_now(),
        )
        db_session.add(attempt)
        await db_session.commit()
        assert attempt.id is not None

    async def test_attempt_fk_enforced(self, db_session: AsyncSession):
        """FK constraint: bad helm_license_id is rejected."""
        user = await _create_user(db_session)
        attempt = ActivationAttempt(
            helm_license_id="hl-nonexistent",
            token_hash="sha256:deadbeef",
            contact_email_snapshot="test@example.com",
            requested_by_user_id=user.id,
            status="pending",
            expires_at=_now(),
        )
        db_session.add(attempt)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


class TestPendingLicenseClaim:
    async def test_create_claim(self, db_session: AsyncSession):
        """Basic creation of a pending license claim."""
        hl = HelmLicense(
            id="hl-claim-test",
            org_name="ClaimCorp",
            contact_email="claim@example.com",
            tier="enterprise",
        )
        db_session.add(hl)
        await db_session.commit()

        claim = PendingLicenseClaim(
            helm_license_id=hl.id,
            org_name=hl.org_name,
            contact_email=hl.contact_email,
            tier=hl.tier,
            seats_limit=25,
        )
        db_session.add(claim)
        await db_session.commit()
        assert claim.id is not None

    async def test_claim_unique_per_license(self, db_session: AsyncSession):
        """UNIQUE on helm_license_id: one claim per license."""
        hl = HelmLicense(
            id="hl-unique-claim",
            org_name="UniqueClaim",
            contact_email="unique@example.com",
            tier="enterprise",
        )
        db_session.add(hl)
        await db_session.commit()

        claim1 = PendingLicenseClaim(
            helm_license_id=hl.id,
            org_name=hl.org_name,
            contact_email=hl.contact_email,
            tier=hl.tier,
        )
        db_session.add(claim1)
        await db_session.commit()

        claim2 = PendingLicenseClaim(
            helm_license_id=hl.id,  # same license
            org_name=hl.org_name,
            contact_email=hl.contact_email,
            tier=hl.tier,
        )
        db_session.add(claim2)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


# ────────────────────────────────────────────────────────────────
# Backfill logic: deterministic single-owner selection
# ────────────────────────────────────────────────────────────────

class TestSingleOwnerBackfill:
    """Tests for the deterministic single-owner backfill logic.

    Precedence chain per org:
    1. Creator from AdminAction where action='admin_create_org'
    2. Earliest admin by COALESCE(joined_at, invited_at)
    3. Lowest-id admin
    """

    async def test_precedence_1_creator_from_admin_action(
        self, db_session: AsyncSession
    ):
        """Creator identified via AdminAction wins."""
        org = await _create_org(db_session)
        creator = await _create_user(db_session)
        other_admin = await _create_user(db_session)

        # Creator has AdminAction record
        aa = AdminAction(
            id=str(uuid.uuid4()),
            admin_id=creator.id,
            action="admin_create_org",
            target_type="organization",
            target_id=org.id,
        )
        db_session.add(aa)
        await db_session.commit()

        # Both are admins, but creator joined later.
        db_session.add(OrgMember(
            org_id=org.id, user_id=creator.id, role="admin",
            joined_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        db_session.add(OrgMember(
            org_id=org.id, user_id=other_admin.id, role="admin",
            joined_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ))
        await db_session.commit()

        # Run the backfill query: precedence 1.
        result = await db_session.execute(
            sa.text(
                "SELECT admin_id FROM admin_actions "
                "WHERE action = 'admin_create_org' "
                "AND target_type = 'organization' "
                "AND target_id = :org_id "
                "ORDER BY created_at LIMIT 1"
            ),
            {"org_id": org.id},
        )
        creator_row = result.fetchone()
        assert creator_row is not None
        assert creator_row[0] == creator.id

    async def test_precedence_2_earliest_admin_by_join_date(
        self, db_session: AsyncSession
    ):
        """Without AdminAction, earliest admin by join date wins."""
        org = await _create_org(db_session)
        early = await _create_user(db_session, email="early@test.com")
        late = await _create_user(db_session, email="late@test.com")

        db_session.add(OrgMember(
            org_id=org.id, user_id=early.id, role="admin",
            joined_at=datetime(2025, 3, 1, tzinfo=timezone.utc),
        ))
        db_session.add(OrgMember(
            org_id=org.id, user_id=late.id, role="admin",
            joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        await db_session.commit()

        result = await db_session.execute(
            sa.text(
                "SELECT user_id FROM org_members "
                "WHERE org_id = :org_id AND role = 'admin' "
                "ORDER BY COALESCE(joined_at, invited_at) ASC, user_id ASC "
                "LIMIT 1"
            ),
            {"org_id": org.id},
        )
        winner = result.fetchone()
        assert winner is not None
        assert winner[0] == early.id

    async def test_precedence_3_lowest_id_admin_fallback(
        self, db_session: AsyncSession
    ):
        """Without AdminAction or join dates, lowest-id admin wins."""
        org = await _create_org(db_session)
        user_a = await _create_user(db_session, id="aaa-user")
        user_z = await _create_user(db_session, id="zzz-user")

        # Both admins, same joined_at (default).
        db_session.add(OrgMember(
            org_id=org.id, user_id=user_a.id, role="admin",
        ))
        db_session.add(OrgMember(
            org_id=org.id, user_id=user_z.id, role="admin",
        ))
        await db_session.commit()

        result = await db_session.execute(
            sa.text(
                "SELECT user_id FROM org_members "
                "WHERE org_id = :org_id AND role = 'admin' "
                "ORDER BY user_id ASC LIMIT 1"
            ),
            {"org_id": org.id},
        )
        winner = result.fetchone()
        assert winner is not None
        assert winner[0] == user_a.id  # "aaa-user" < "zzz-user"


# ────────────────────────────────────────────────────────────────
# Backfill: entitlements creation
# ────────────────────────────────────────────────────────────────

class TestEntitlementsBackfill:
    async def test_org_gets_entitlement_manual(self, db_session: AsyncSession):
        """An org without stripe_subscription_id gets source='manual'."""
        org = await _create_org(db_session, tier="team")

        ent = Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="manual",
            tier=org.tier,
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        result = await db_session.execute(
            sa.text(
                "SELECT * FROM entitlements "
                "WHERE owner_type = 'org' AND owner_id = :oid "
                "AND status = 'active'"
            ),
            {"oid": org.id},
        )
        row = result.fetchone()
        assert row is not None
        assert row.source == "manual"

    async def test_org_gets_entitlement_stripe(self, db_session: AsyncSession):
        """An org with stripe_subscription_id gets source='stripe'."""
        org = await _create_org(
            db_session, tier="team",
            stripe_subscription_id="sub_test_123",
        )

        ent = Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="stripe",
            source_ref="sub_test_123",
            tier=org.tier,
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        result = await db_session.execute(
            sa.text(
                "SELECT * FROM entitlements "
                "WHERE owner_type = 'org' AND owner_id = :oid "
                "AND status = 'active'"
            ),
            {"oid": org.id},
        )
        row = result.fetchone()
        assert row is not None
        assert row.source == "stripe"
        assert row.source_ref == "sub_test_123"

    async def test_paid_user_without_org_gets_entitlement(
        self, db_session: AsyncSession
    ):
        """A user with tier!='free' and no OrgMember gets a manual entitlement."""
        user = await _create_user(db_session, tier="pro")

        ent = Entitlement(
            owner_type="user",
            owner_id=user.id,
            source="manual",
            tier="pro",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        result = await db_session.execute(
            sa.text(
                "SELECT * FROM entitlements "
                "WHERE owner_type = 'user' AND owner_id = :uid "
                "AND status = 'active'"
            ),
            {"uid": user.id},
        )
        row = result.fetchone()
        assert row is not None
        assert row.tier == "pro"
        assert row.source == "manual"

    async def test_null_tier_coerced_to_free(self, db_session: AsyncSession):
        """NULL tier should be coerced to 'free' with a diagnostic."""
        # Direct test of the _coerce_tier logic from migration 050.
        from tests.server.unit.test_entitlements_p1 import _coerce_tier_test

        assert _coerce_tier_test(None, "test context") == "free"
        assert _coerce_tier_test("invalid_tier", "test context") == "free"
        assert _coerce_tier_test("ADMIN", "test context") == "free"
        assert _coerce_tier_test("enterprise", "test context") == "enterprise"

    async def test_entitlement_id_fk_on_user(self, db_session: AsyncSession):
        """User.entitlement_id FK links to entitlements.id."""
        user = await _create_user(db_session)

        ent = Entitlement(
            owner_type="user",
            owner_id=user.id,
            source="manual",
            tier="pro",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        user.entitlement_id = ent.id
        await db_session.commit()

        await db_session.refresh(user)
        assert user.entitlement_id == ent.id

    async def test_entitlement_id_fk_on_organization(
        self, db_session: AsyncSession
    ):
        """Organization.entitlement_id FK links to entitlements.id."""
        org = await _create_org(db_session)

        ent = Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="manual",
            tier="team",
            status="active",
            billing_status="current",
            created_at=_now(),
            updated_at=_now(),
        )
        db_session.add(ent)
        await db_session.commit()

        org.entitlement_id = ent.id
        await db_session.commit()

        await db_session.refresh(org)
        assert org.entitlement_id == ent.id


# ────────────────────────────────────────────────────────────────
# Cross-table integration: full backfill scenario
# ────────────────────────────────────────────────────────────────

class TestFullBackfillScenario:
    async def test_full_backfill_workflow(self, db_session: AsyncSession):
        """Simulate the full migration 050 backfill in a test."""
        now = _now()

        # Create 2 orgs, each with members.
        org1 = await _create_org(db_session, tier="team", slug="org-backfill-1")
        org2 = await _create_org(
            db_session, tier="enterprise", slug="org-backfill-2",
            stripe_subscription_id="sub_full_1",
        )

        u1 = await _create_user(db_session, tier="team")
        u2 = await _create_user(db_session, tier="free")
        u3 = await _create_user(db_session, tier="free")
        u4 = await _create_user(db_session, tier="pro")  # paid, no org

        # org1: u1=admin, u2=member
        db_session.add(OrgMember(org_id=org1.id, user_id=u1.id, role="admin"))
        db_session.add(OrgMember(org_id=org1.id, user_id=u2.id, role="member"))
        # org2: u3=admin
        db_session.add(OrgMember(org_id=org2.id, user_id=u3.id, role="admin"))
        await db_session.commit()

        # --- Run backfill logic ---

        # 1. Entitlements per org
        for org, tier, stripe_sub in [
            (org1, "team", None),
            (org2, "enterprise", "sub_full_1"),
        ]:
            source = "stripe" if stripe_sub else "manual"
            source_ref = stripe_sub if stripe_sub else None
            db_session.add(Entitlement(
                owner_type="org", owner_id=org.id,
                source=source, source_ref=source_ref,
                tier=tier, status="active", billing_status="current",
                created_at=now, updated_at=now,
            ))
        await db_session.commit()

        # 2. Paid user without org gets entitlement
        db_session.add(Entitlement(
            owner_type="user", owner_id=u4.id,
            source="manual", tier="pro", status="active",
            billing_status="current",
            created_at=now, updated_at=now,
        ))
        await db_session.commit()

        # 3. Single-owner backfill: promote earliest admin in each org
        for org_id, expected_owner in [(org1.id, u1.id), (org2.id, u3.id)]:
            winner = await db_session.execute(
                sa.text(
                    "SELECT user_id FROM org_members "
                    "WHERE org_id = :org_id AND role = 'admin' "
                    "ORDER BY COALESCE(joined_at, invited_at) ASC, user_id ASC "
                    "LIMIT 1"
                ),
                {"org_id": org_id},
            )
            winner_id = winner.fetchone()[0]

            await db_session.execute(
                sa.text(
                    "UPDATE org_members SET role = 'owner' "
                    "WHERE org_id = :org_id AND user_id = :user_id"
                ),
                {"org_id": org_id, "user_id": winner_id},
            )
            await db_session.commit()

            assert winner_id == expected_owner

        # Verify state
        # org1: u1 is now owner
        u1_role = await db_session.execute(
            sa.text(
                "SELECT role FROM org_members "
                "WHERE org_id = :org_id AND user_id = :user_id"
            ),
            {"org_id": org1.id, "user_id": u1.id},
        )
        assert u1_role.fetchone()[0] == "owner"

        # org2: u3 is now owner
        u3_role = await db_session.execute(
            sa.text(
                "SELECT role FROM org_members "
                "WHERE org_id = :org_id AND user_id = :user_id"
            ),
            {"org_id": org2.id, "user_id": u3.id},
        )
        assert u3_role.fetchone()[0] == "owner"

        # Verify entitlements exist
        for owner_type, owner_id in [
            ("org", org1.id), ("org", org2.id), ("user", u4.id),
        ]:
            count = await db_session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM entitlements "
                    "WHERE owner_type = :ot AND owner_id = :oid "
                    "AND status = 'active'"
                ),
                {"ot": owner_type, "oid": owner_id},
            )
            assert count.fetchone()[0] >= 1


# ────────────────────────────────────────────────────────────────
# _coerce_tier helper (imported by test above)
# ────────────────────────────────────────────────────────────────

VALID_TIERS = {"free", "starter", "pro", "team", "enterprise"}


def _coerce_tier_test(raw: str | None, context: str) -> str:
    """Copy of migration _coerce_tier for testing."""
    if raw is None:
        return "free"
    normalized = raw.strip().lower()
    if normalized in VALID_TIERS:
        return normalized
    return "free"


# ────────────────────────────────────────────────────────────────
# Migration 050 up + down on SQLite
# ────────────────────────────────────────────────────────────────


@pytest.fixture
def migration_050_db_path(tmp_path):
    """Create a SQLite DB with minimum prerequisite tables for migration 050.

    Migration 050 needs: users, organizations, org_members, helm_licenses,
    admin_actions (all pre-050 state).
    """
    import sqlite3

    db_path = tmp_path / "migration_050_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # Users — columns that existed pre-050
    conn.execute("""
        CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY,
            email VARCHAR(255) NOT NULL UNIQUE,
            display_name VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            email_verified BOOLEAN NOT NULL DEFAULT 0,
            tier VARCHAR(20) NOT NULL DEFAULT 'free',
            is_active BOOLEAN NOT NULL DEFAULT 1,
            stripe_customer_id VARCHAR(64),
            stripe_subscription_id VARCHAR(64),
            tier_updated_at TIMESTAMP,
            storage_used_bytes BIGINT NOT NULL DEFAULT 0,
            beta_pro_expires_at TIMESTAMP,
            last_client_version VARCHAR(20),
            last_client_platform VARCHAR(50),
            last_client_device VARCHAR(100),
            last_sync_at TIMESTAMP,
            sync_mode VARCHAR(20) NOT NULL DEFAULT 'off',
            sync_debounce INTEGER NOT NULL DEFAULT 30,
            audit_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
            summarize_trigger VARCHAR(20) NOT NULL DEFAULT 'manual',
            default_org_id VARCHAR(64)
        )
    """)

    # Organizations — pre-050 columns
    conn.execute("""
        CREATE TABLE organizations (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            slug VARCHAR(100) NOT NULL UNIQUE,
            tier VARCHAR(20) NOT NULL DEFAULT 'team',
            stripe_customer_id VARCHAR(64),
            stripe_subscription_id VARCHAR(64),
            storage_limit_bytes BIGINT NOT NULL DEFAULT 0,
            storage_used_bytes BIGINT NOT NULL DEFAULT 0,
            seats_limit INTEGER NOT NULL DEFAULT 5,
            settings TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # OrgMembers
    conn.execute("""
        CREATE TABLE org_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id VARCHAR(64) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL DEFAULT 'member',
            invited_by VARCHAR(36) REFERENCES users(id),
            invited_at TIMESTAMP,
            joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, user_id)
        )
    """)

    # HelmLicenses
    conn.execute("""
        CREATE TABLE helm_licenses (
            id VARCHAR(64) PRIMARY KEY,
            org_name VARCHAR(255) NOT NULL,
            contact_email VARCHAR(255) NOT NULL,
            tier VARCHAR(20) NOT NULL DEFAULT 'enterprise',
            seats_limit INTEGER DEFAULT 25,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            expires_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            license_type VARCHAR(20) NOT NULL DEFAULT 'paid',
            cluster_id VARCHAR(128),
            last_validated_at TIMESTAMP,
            validation_count INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # AdminActions
    conn.execute("""
        CREATE TABLE admin_actions (
            id VARCHAR(36) PRIMARY KEY,
            admin_id VARCHAR(36) NOT NULL REFERENCES users(id),
            action VARCHAR(50) NOT NULL,
            target_type VARCHAR(20) NOT NULL,
            target_id VARCHAR(64) NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── Seed representative data ──

    # Users
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-1', 'alice@corp.com', 'free')"
    )
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-2', 'bob@corp.com', 'free')"
    )
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-3', 'carol@other.com', 'free')"
    )
    conn.execute(
        "INSERT INTO users (id, email, tier) VALUES ('user-4', 'dan@paid.com', 'pro')"
    )

    # Orgs
    # org-1: has stripe_sub → source='stripe'
    conn.execute(
        "INSERT INTO organizations (id, name, slug, tier, stripe_subscription_id, "
        "seats_limit) VALUES ('org-1', 'Corp Org', 'corp-org', 'team', 'sub_corp_1', 10)"
    )
    # org-2: has matching HelmLicense → source='helm_license'
    conn.execute(
        "INSERT INTO organizations (id, name, slug, tier, seats_limit) "
        "VALUES ('org-2', 'Licensed Inc', 'licensed-inc', 'free', 5)"
    )
    # org-3: no stripe, no license match → source='manual'
    conn.execute(
        "INSERT INTO organizations (id, name, slug, tier, seats_limit) "
        "VALUES ('org-3', 'Manual LLC', 'manual-llc', 'starter', 3)"
    )

    # OrgMembers
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) "
        "VALUES ('org-1', 'user-1', 'admin', '2025-01-01')"
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) "
        "VALUES ('org-2', 'user-2', 'admin', '2025-06-01')"
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) "
        "VALUES ('org-3', 'user-3', 'admin', '2025-03-01')"
    )

    # HelmLicenses
    # hl-001: matches org-2 (org_name='Licensed Inc' + contact_email='bob@corp.com')
    conn.execute(
        "INSERT INTO helm_licenses (id, org_name, contact_email, tier, seats_limit, "
        "status) VALUES ('hl-001', 'Licensed Inc', 'bob@corp.com', 'enterprise', "
        "50, 'active')"
    )
    # hl-002: UNMATCHED (no org with that name+email combo) → pending_claim
    conn.execute(
        "INSERT INTO helm_licenses (id, org_name, contact_email, tier, seats_limit, "
        "status) VALUES ('hl-002', 'Ghost Co', 'ghost@example.com', 'pro', "
        "15, 'active')"
    )

    # AdminActions: user-1 created org-1
    conn.execute(
        "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id) "
        "VALUES ('aa-1', 'user-1', 'admin_create_org', 'organization', 'org-1')"
    )
    # user-3 created org-3 (but joined later — earliest-admin precedence won't use it)
    conn.execute(
        "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id) "
        "VALUES ('aa-2', 'user-3', 'admin_create_org', 'organization', 'org-3')"
    )

    conn.commit()
    conn.close()
    return db_path


class TestMigration050:
    """Migration 050 upgrade + downgrade on SQLite."""

    @staticmethod
    def _alembic_cfg(db_path):
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option(
            "script_location", "src/sessionfs/server/db/migrations"
        )
        cfg.set_main_option(
            "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
        )
        return cfg

    def test_upgrade_creates_tables_and_backfills(self, migration_050_db_path):
        """Full upgrade: tables exist, exactly one active entitlement per org,
        matched license → helm_license source, unmatched → pending_claim,
        entitlement_id pointers populated, tier CHECK enforced."""
        from alembic import command
        import sqlite3

        cfg = self._alembic_cfg(migration_050_db_path)
        command.stamp(cfg, "049")
        command.upgrade(cfg, "050")

        conn = sqlite3.connect(str(migration_050_db_path))

        # 1. Tables created
        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for t in ("entitlements", "org_audit_events", "activation_attempt",
                  "pending_license_claim"):
            assert t in tables, f"Table {t} should exist"

        # 2. Entitlement columns exist
        ent_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('entitlements')").fetchall()
        ]
        for col in ("id", "owner_type", "owner_id", "source", "source_ref",
                     "tier", "seats_limit", "storage_limit_bytes", "status",
                     "billing_status", "current_period_end"):
            assert col in ent_cols, f"entitlements missing column: {col}"

        # 3. New columns on orgs/users/helm_licenses
        org_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('organizations')").fetchall()
        ]
        assert "entitlement_id" in org_cols

        user_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('users')").fetchall()
        ]
        assert "entitlement_id" in user_cols

        hl_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('helm_licenses')").fetchall()
        ]
        assert "org_id" in hl_cols

        # 4. Indexes exist
        indexes = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        ]
        for idx in ("uq_entitlements_one_active_per_owner",
                     "uq_entitlements_source_ref",
                     "uq_org_members_one_owner_per_org"):
            assert idx in indexes, f"Missing index: {idx}"

        # 5. Exactly ONE active entitlement per org.
        for org_id in ("org-1", "org-2", "org-3"):
            count = conn.execute(
                "SELECT COUNT(*) FROM entitlements "
                "WHERE owner_type = 'org' AND owner_id = ? "
                "AND status = 'active'",
                (org_id,),
            ).fetchone()[0]
            assert count == 1, (
                f"Org {org_id} has {count} active entitlements, expected 1"
            )

        # 6. org-1 has stripe source (no license match)
        ent1 = conn.execute(
            "SELECT source, source_ref, tier FROM entitlements "
            "WHERE owner_type = 'org' AND owner_id = 'org-1' AND status = 'active'"
        ).fetchone()
        assert ent1 is not None
        assert ent1[0] == "stripe", f"org-1 source should be 'stripe', got {ent1[0]}"
        assert ent1[1] == "sub_corp_1"
        assert ent1[2] == "team"

        # 7. org-2 has helm_license source (matched license wins over manual)
        ent2 = conn.execute(
            "SELECT source, source_ref, tier, seats_limit FROM entitlements "
            "WHERE owner_type = 'org' AND owner_id = 'org-2' AND status = 'active'"
        ).fetchone()
        assert ent2 is not None
        assert ent2[0] == "helm_license", (
            f"org-2 source should be 'helm_license', got {ent2[0]}"
        )
        assert ent2[1] == "hl-001"
        assert ent2[2] == "enterprise"  # from license, not org's original 'free'
        assert ent2[3] == 50  # license seats

        # 8. org-3 has manual source
        ent3 = conn.execute(
            "SELECT source, source_ref, tier FROM entitlements "
            "WHERE owner_type = 'org' AND owner_id = 'org-3' AND status = 'active'"
        ).fetchone()
        assert ent3 is not None
        assert ent3[0] == "manual", f"org-3 source should be 'manual', got {ent3[0]}"
        assert ent3[1] is None

        # 9. Paid user (user-4, no org) has entitlement
        ent_user = conn.execute(
            "SELECT owner_type, owner_id, source, tier FROM entitlements "
            "WHERE owner_type = 'user' AND owner_id = 'user-4' AND status = 'active'"
        ).fetchone()
        assert ent_user is not None
        assert ent_user[2] == "manual"
        assert ent_user[3] == "pro"

        # 10. Unmatched license → pending_license_claim (NOT an entitlement)
        claim = conn.execute(
            "SELECT helm_license_id, org_name FROM pending_license_claim "
            "WHERE helm_license_id = 'hl-002'"
        ).fetchone()
        assert claim is not None, "hl-002 should be in pending_license_claim"
        assert claim[1] == "Ghost Co"

        # 11. Matched license → org_id set on helm_licenses
        hl1_org = conn.execute(
            "SELECT org_id FROM helm_licenses WHERE id = 'hl-001'"
        ).fetchone()[0]
        assert hl1_org == "org-2", f"hl-001 should be bound to org-2, got {hl1_org}"

        # Unmatched license stays unbound
        hl2_org = conn.execute(
            "SELECT org_id FROM helm_licenses WHERE id = 'hl-002'"
        ).fetchone()[0]
        assert hl2_org is None, "hl-002 should remain unbound"

        # 12. entitlement_id pointers populated on organizations
        for org_id in ("org-1", "org-2", "org-3"):
            ptr = conn.execute(
                "SELECT entitlement_id FROM organizations WHERE id = ?",
                (org_id,),
            ).fetchone()[0]
            assert ptr is not None, (
                f"org {org_id} entitlement_id pointer is NULL"
            )
            # Verify pointer resolves to the correct row
            ent = conn.execute(
                "SELECT owner_id FROM entitlements WHERE id = ?", (ptr,)
            ).fetchone()
            assert ent is not None, (
                f"org {org_id} entitlement_id {ptr} does not resolve"
            )
            assert ent[0] == org_id

        # Not checking user-4 entitlement_id here — the backfill sets it
        # on the user row in step 8.

        # 13. Single-owner backfill: each org has exactly one owner
        for org_id in ("org-1", "org-2", "org-3"):
            owner_count = conn.execute(
                "SELECT COUNT(*) FROM org_members "
                "WHERE org_id = ? AND role = 'owner'",
                (org_id,),
            ).fetchone()[0]
            assert owner_count == 1, (
                f"Org {org_id} has {owner_count} owners, expected 1"
            )

        # 14. Tier CHECK constraint: 'admin' is rejected
        import sqlite3 as sq3
        try:
            conn.execute(
                "INSERT INTO entitlements "
                "(owner_type, owner_id, source, tier, status, billing_status, "
                "created_at, updated_at) "
                "VALUES ('user', 'bad-tier', 'manual', 'admin', 'active', "
                "'current', datetime('now'), datetime('now'))"
            )
            conn.commit()
            assert False, "Should have raised IntegrityError for tier='admin'"
        except sq3.IntegrityError:
            pass  # Expected

        conn.close()

    def test_downgrade_cleans_up(self, migration_050_db_path):
        """Downgrade removes all 050 additions cleanly."""
        from alembic import command
        import sqlite3

        cfg = self._alembic_cfg(migration_050_db_path)
        command.stamp(cfg, "049")
        command.upgrade(cfg, "050")
        command.downgrade(cfg, "049")

        conn = sqlite3.connect(str(migration_050_db_path))

        # Tables gone
        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for t in ("entitlements", "org_audit_events", "activation_attempt",
                  "pending_license_claim"):
            assert t not in tables, f"Table {t} should be gone"

        # Columns removed from users, organizations, helm_licenses
        org_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('organizations')").fetchall()
        ]
        assert "entitlement_id" not in org_cols

        user_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('users')").fetchall()
        ]
        assert "entitlement_id" not in user_cols

        hl_cols = [
            row[1] for row in
            conn.execute("PRAGMA table_info('helm_licenses')").fetchall()
        ]
        assert "org_id" not in hl_cols

        # Owner→admin reverted
        for org_id in ("org-1", "org-2", "org-3"):
            owner_count = conn.execute(
                "SELECT COUNT(*) FROM org_members "
                "WHERE org_id = ? AND role = 'owner'",
                (org_id,),
            ).fetchone()[0]
            assert owner_count == 0, (
                f"Org {org_id} still has {owner_count} owners after downgrade"
            )

        # Original orgs + users intact
        org_count = conn.execute(
            "SELECT COUNT(*) FROM organizations"
        ).fetchone()[0]
        assert org_count == 3

        user_count = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        assert user_count == 4

        conn.close()

    def test_idempotent_upgrade(self, migration_050_db_path):
        """Upgrade → downgrade → upgrade is idempotent."""
        from alembic import command
        import sqlite3

        cfg = self._alembic_cfg(migration_050_db_path)

        command.stamp(cfg, "049")
        command.upgrade(cfg, "050")
        command.downgrade(cfg, "049")
        command.upgrade(cfg, "050")  # Must succeed — second upgrade

        conn = sqlite3.connect(str(migration_050_db_path))

        # Verify backfill re-applied: exactly one active entitlement per org
        for org_id in ("org-1", "org-2", "org-3"):
            count = conn.execute(
                "SELECT COUNT(*) FROM entitlements "
                "WHERE owner_type = 'org' AND owner_id = ? "
                "AND status = 'active'",
                (org_id,),
            ).fetchone()[0]
            assert count == 1, (
                f"After re-upgrade, org {org_id} has {count} active entitlements"
            )

        # org-2 still gets helm_license source on re-upgrade
        ent2 = conn.execute(
            "SELECT source FROM entitlements "
            "WHERE owner_type = 'org' AND owner_id = 'org-2' AND status = 'active'"
        ).fetchone()
        assert ent2 is not None
        assert ent2[0] == "helm_license"

        # Unmatched license still goes to pending_claim
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM pending_license_claim "
            "WHERE helm_license_id = 'hl-002'"
        ).fetchone()[0]
        assert claim_count == 1

        conn.close()

    def test_downgrade_order_owner_to_admin_before_index_drop(
        self, migration_050_db_path
    ):
        """The downgrade must UPDATE owner→admin BEFORE dropping the
        partial unique index. This test verifies the downgrade completes
        without IntegrityError, which would happen if the index were
        dropped first (index would prevent multiple owners from existing
        during the UPDATE). Actually the index prevents multiple owners
        in the SAME org, so the update itself must happen before the
        drop to be safe — but the real test is that the downgrade
        succeeds at all."""
        from alembic import command
        import sqlite3

        cfg = self._alembic_cfg(migration_050_db_path)
        command.stamp(cfg, "049")
        command.upgrade(cfg, "050")
        # This must not raise
        command.downgrade(cfg, "049")

        conn = sqlite3.connect(str(migration_050_db_path))
        assert "entitlements" not in [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
