"""P2 entitlement resolution + write-through.

resolve_entitlement: single-query resolution of the active entitlement for
an owner (org or user).  owner_id is ALWAYS derived server-side — never
client-supplied.  The denormalized entitlement_id pointer on users/
organizations is a HINT only; the authoritative row is always fetched via
the scoped status='active' query.

apply_entitlement: UPSERT the owner's active entitlement AND mirror
tier/seats/storage onto the legacy column cache (Organization.tier/
seats_limit/storage_limit_bytes or User.tier).  Route EVERY existing
tier-write site through this helper so entitlements stay current.

SAFETY INVARIANT: P1 backfilled entitlements to match current tiers, but
nothing keeps them in sync.  apply_entitlement IS that keep-in-sync
mechanism.  Every existing path that writes User.tier / Organization.tier
MUST also call this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Entitlement, Organization, User

logger = logging.getLogger("sessionfs.entitlements")

VALID_ENTITLEMENT_TIERS = frozenset({"free", "starter", "pro", "team", "enterprise"})


@dataclass
class ResolvedEntitlement:
    """The resolved active entitlement for an owner.

    None of these fields are client-supplied — they're all read from the
    entitlements table via a scoped query.
    """

    id: int
    owner_type: str
    owner_id: str
    tier: str
    seats_limit: int | None
    storage_limit_bytes: int | None
    billing_status: str
    source: str
    source_ref: str | None
    current_period_end: datetime | None


async def resolve_entitlement(
    owner_type: str,
    owner_id: str,
    db: AsyncSession,
) -> ResolvedEntitlement | None:
    """Resolve the active entitlement for an owner.

    Single query: ``SELECT ... FROM entitlements WHERE owner_type = :t
    AND owner_id = :id AND status = 'active'``.  Guaranteed ≤1 row by the
    P1 partial unique index ``uq_entitlements_one_active_per_owner``.

    owner_id is DERIVED SERVER-SIDE — from the caller's OrgMember row for
    orgs, or user.id.  Never from client input.
    """
    result = await db.execute(
        select(Entitlement).where(
            Entitlement.owner_type == owner_type,
            Entitlement.owner_id == owner_id,
            Entitlement.status == "active",
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None

    return ResolvedEntitlement(
        id=row.id,
        owner_type=row.owner_type,
        owner_id=row.owner_id,
        tier=row.tier,
        seats_limit=row.seats_limit,
        storage_limit_bytes=row.storage_limit_bytes,
        billing_status=row.billing_status,
        source=row.source,
        source_ref=row.source_ref,
        current_period_end=row.current_period_end,
    )


async def apply_entitlement(
    owner_type: str,
    owner_id: str,
    *,
    tier: str,
    seats: int | None = None,
    storage: int | None = None,
    source: str,
    source_ref: str | None = None,
    db: AsyncSession,
    current_period_end: datetime | None = None,
    billing_status: str = "current",
    status: str = "active",
) -> Entitlement:
    """UPSERT the active entitlement for an owner AND mirror legacy columns.

    Strategy:
    1. Find the existing active entitlement for this owner (if any).
    2. If found, UPDATE it in-place.  If not, INSERT a new row.
    3. Mirror tier/seats/storage onto the legacy column cache
       (Organization.tier/seats_limit/storage_limit_bytes or User.tier).
       For user entitlements, also update the user's entitlement_id FK.
       For org entitlements, also update the org's entitlement_id FK.

    Sentinel MEDIUM-3 guard: tier MUST be in VALID_ENTITLEMENT_TIERS
    (free/starter/pro/team/enterprise).  'admin' is NOT an entitlement
    tier and is rejected at this layer.
    """
    assert tier in VALID_ENTITLEMENT_TIERS, (
        f"apply_entitlement: tier '{tier}' is not a valid entitlement tier. "
        f"'admin' is a platform role, not an entitlement tier."
    )

    now = datetime.now(timezone.utc)

    # ── 1. Find existing active entitlement ──────────────────────
    existing_result = await db.execute(
        select(Entitlement).where(
            Entitlement.owner_type == owner_type,
            Entitlement.owner_id == owner_id,
            Entitlement.status == "active",
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        # UPDATE in-place — preserve created_at, bump updated_at.
        existing.tier = tier
        existing.source = source
        existing.source_ref = source_ref
        existing.billing_status = billing_status
        existing.updated_at = now
        if seats is not None:
            existing.seats_limit = seats
        if storage is not None:
            existing.storage_limit_bytes = storage
        if current_period_end is not None:
            existing.current_period_end = current_period_end
        if status != "active":
            existing.status = status
        ent = existing
    else:
        # INSERT new entitlement row.
        ent = Entitlement(
            owner_type=owner_type,
            owner_id=owner_id,
            source=source,
            source_ref=source_ref,
            tier=tier,
            seats_limit=seats,
            storage_limit_bytes=storage,
            status=status,
            billing_status=billing_status,
            current_period_start=now,
            current_period_end=current_period_end,
        )
        db.add(ent)
        # Flush so ent.id is populated for the FK mirror below.
        await db.flush()

    # ── 2. Mirror legacy column cache ────────────────────────────
    if owner_type == "org":
        await db.execute(
            update(Organization)
            .where(Organization.id == owner_id)
            .values(
                tier=tier,
                seats_limit=seats if seats is not None else 0,
                storage_limit_bytes=storage if storage is not None else 0,
                entitlement_id=ent.id,
            )
        )
    elif owner_type == "user":
        # User.tier is the denormalized cache.  Sentinel MEDIUM-3:
        # NEVER write 'admin' to User.tier from an entitlement.
        assert tier != "admin", (
            "apply_entitlement: refusing to write tier='admin' to User.tier "
            "from an entitlement. Platform-admin is grantable ONLY via the "
            "existing admin path."
        )
        await db.execute(
            update(User)
            .where(User.id == owner_id)
            .values(
                tier=tier,
                entitlement_id=ent.id,
            )
        )

    return ent


async def _mirror_org_cache_only(
    org_id: str,
    tier: str,
    seats: int | None,
    storage: int | None,
    db: AsyncSession,
) -> None:
    """Mirror tier/seats/storage onto Organization columns ONLY.

    Used by Stripe handlers that already write directly to org fields
    (backward-compatible caching).  Prefer apply_entitlement for new
    code paths.
    """
    values: dict = {"tier": tier}
    if seats is not None:
        values["seats_limit"] = seats
    if storage is not None:
        values["storage_limit_bytes"] = storage
    await db.execute(
        update(Organization).where(Organization.id == org_id).values(**values)
    )
