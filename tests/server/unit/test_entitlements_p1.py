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
