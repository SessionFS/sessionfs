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


async def lock_active_entitlement(
    db: AsyncSession, owner_type: str, owner_id: str
) -> str | None:
    """Lock the owner's active entitlement row FOR UPDATE and return its tier.

    This is the serialization point against a concurrent ``apply_entitlement``
    downgrade: apply_entitlement UPDATEs the entitlement row in place (then
    mirrors the legacy tier column on Organization/User), so holding this row
    FOR UPDATE until the fenced operation commits makes a downgrade wait for it
    (or vice versa) and keeps ``resolve_entitlement``'s read stable inside the
    tier re-check. Returns None when there is no active entitlement (the legacy
    tier columns still apply)."""
    return (
        await db.execute(
            select(Entitlement.tier)
            .where(
                Entitlement.owner_type == owner_type,
                Entitlement.owner_id == owner_id,
                Entitlement.status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


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
    # Sentinel MEDIUM-3 guard — explicit raise (not `assert`, which is
    # stripped under `python -O`). The DB CHECK constraint is the final
    # backstop, but fail loudly here regardless of optimization flags.
    if tier not in VALID_ENTITLEMENT_TIERS:
        raise ValueError(
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
        # Only mirror the fields actually supplied. Codex R1 HIGH:
        # a tier-only update (seats/storage omitted) must NOT zero the
        # existing Organization.seats_limit / storage_limit_bytes — that
        # silently corrupts the org's quota. Preserve omitted columns.
        org_values: dict = {"tier": tier, "entitlement_id": ent.id}
        if seats is not None:
            org_values["seats_limit"] = seats
        if storage is not None:
            org_values["storage_limit_bytes"] = storage
        await db.execute(
            update(Organization)
            .where(Organization.id == owner_id)
            .values(**org_values)
        )
    elif owner_type == "user":
        # User.tier is the denormalized cache.  Sentinel MEDIUM-3:
        # NEVER write 'admin' to User.tier from an entitlement.
        if tier == "admin":
            raise ValueError(
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


async def lock_stripe_customer(db: AsyncSession, customer_id: str | None) -> None:
    """Serialize every billing mutation for one Stripe customer.

    Lock order for anything that changes tier, seats or entitlements (org
    creation adopting a subscription, admin tier/seat changes, and the
    checkout / subscription.updated / subscription.deleted webhooks):

        1. this customer lock
        2. the user row, then the org row (SELECT ... FOR UPDATE)
        3. the entitlement row(s)

    lock_billing_rows() / lock_billing_owner() take 1 and 2 (and the org row).

    Taking the customer lock first means a webhook can never interleave
    with an org creation that is moving the same customer, and both sides
    re-resolve ownership after acquiring it.

    PostgreSQL: a transaction-scoped advisory lock (released at commit or
    rollback). Other dialects (SQLite in tests and single-node dev) already
    serialize writers, so this is a no-op there.
    """
    if not customer_id:
        return
    if db.get_bind().dialect.name != "postgresql":
        return
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"sessionfs:stripe_customer:{customer_id}"},
    )


class BillingLockRetry(Exception):
    """A Stripe customer id changed between the unlocked read and the row
    lock. Taking that customer's lock now would break the lock order, so
    the transaction must roll back and start over (the webhook processor
    restarts it; HTTP callers get 409 concurrent_update)."""


async def lock_billing_rows(
    db: AsyncSession,
    *,
    user_ids=(),
    org_ids=(),
    extra_customers=(),
) -> dict:
    """Take billing locks in the universal order and return the locked,
    refreshed rows as {("user"|"org", id): row}.

    Universal billing lock order — every path that changes tier, seats,
    Stripe ids or entitlements (admin tier/seat changes, org creation,
    checkout creation, and the Stripe checkout / subscription webhooks):

        1. Stripe customer advisory locks (lock_stripe_customer), ALL of
           them — the customers stored on every row about to be locked plus
           any extra (e.g. an event's customer) — in sorted order
        2. user rows (SELECT ... FOR UPDATE), sorted by id
        3. organization rows, sorted by id
        4. entitlement rows (apply_entitlement / the binding lookup)

    No lock from an earlier step is ever taken after a later one. Customer
    ids are read before locking; if a locked row's customer is not among
    the locks taken (it changed in between), BillingLockRetry is raised
    instead of locking it late.
    """
    user_ids = sorted({u for u in user_ids if u})
    org_ids = sorted({o for o in org_ids if o})
    customers = {c for c in extra_customers if c}
    if user_ids:
        customers.update(
            c for c in (
                await db.execute(select(User.stripe_customer_id).where(User.id.in_(user_ids)))
            ).scalars() if c
        )
    if org_ids:
        customers.update(
            c for c in (
                await db.execute(
                    select(Organization.stripe_customer_id).where(Organization.id.in_(org_ids))
                )
            ).scalars() if c
        )
    for customer_id in sorted(customers):
        await lock_stripe_customer(db, customer_id)

    rows: dict = {}
    for model, kind, ids in ((User, "user", user_ids), (Organization, "org", org_ids)):
        for owner_id in ids:
            row = (
                await db.execute(
                    select(model)
                    .where(model.id == owner_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is not None and row.stripe_customer_id and row.stripe_customer_id not in customers:
                raise BillingLockRetry(f"{kind} {owner_id} customer changed during locking")
            rows[(kind, owner_id)] = row
    return rows


async def lock_billing_owner(db: AsyncSession, owner_type: str, owner_id: str, *, extra_customers=()):
    """lock_billing_rows for one owner; returns the locked row or None."""
    rows = await lock_billing_rows(
        db,
        user_ids=[owner_id] if owner_type == "user" else [],
        org_ids=[owner_id] if owner_type == "org" else [],
        extra_customers=extra_customers,
    )
    return rows.get((owner_type, owner_id))


class EntitlementBindingConflict(Exception):
    """The external binding (source, source_ref) belongs to another owner."""


async def adopt_user_entitlement_for_org(
    *,
    user_id: str,
    org_id: str,
    stripe_subscription_id: str | None,
    db: AsyncSession,
) -> Entitlement | None:
    """Move the creator's paid entitlement onto the org they are creating.

    Whatever funded the creator's personal paid tier now funds the org, so
    the user must not keep an active entitlement of their own (they would
    keep paid access after leaving the org), and the org must not get a
    second row for the same external binding (``uq_entitlements_source_ref``
    allows one row per binding, and the clash rolls back the org create).

    Resolution, all under ``SELECT ... FOR UPDATE``:

    1. If ``stripe_subscription_id`` is set and a row carries
       ``(stripe, stripe_subscription_id)``, that row is moved. It must be
       this user's active entitlement; anything else raises
       EntitlementBindingConflict.
    2. Otherwise the user's active entitlement, if any, is moved:
       * a Stripe binding to a DIFFERENT subscription is a conflict
         (EntitlementBindingConflict) — never silently rebound;
       * the known migration-050 shape (source='manual', no source_ref) is
         rebound to ``(stripe, stripe_subscription_id)`` when a
         subscription id is known;
       * any other provenance (admin_provisioned, helm_license, ...) moves
         as it is — it is never converted to Stripe here; a later canonical
         Stripe event for the org's subscription does that.

    The row is re-owned in place (``created_at``/history preserved); the
    caller then runs ``apply_entitlement('org', ...)`` with the moved row's
    provenance, which finds it as the org's active entitlement and mirrors
    the org cache. In every case the user's ``entitlement_id`` pointer is
    cleared and the ``User.tier`` cache drops to 'free' (never touching the
    platform 'admin' tier).

    Returns the moved row, or None when the user had nothing to move.
    """
    row: Entitlement | None = None
    if stripe_subscription_id:
        row = (
            await db.execute(
                select(Entitlement)
                .where(
                    Entitlement.source == "stripe",
                    Entitlement.source_ref == stripe_subscription_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None and not (
            row.owner_type == "user"
            and row.owner_id == user_id
            and row.status == "active"
        ):
            raise EntitlementBindingConflict(
                f"stripe binding {stripe_subscription_id} is held by {row.owner_type} "
                f"entitlement {row.id} (status={row.status})"
            )

    if row is None:
        row = (
            await db.execute(
                select(Entitlement)
                .where(
                    Entitlement.owner_type == "user",
                    Entitlement.owner_id == user_id,
                    Entitlement.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None and row.source == "stripe" and row.source_ref and (
            row.source_ref != stripe_subscription_id
        ):
            raise EntitlementBindingConflict(
                f"user's active entitlement {row.id} is bound to Stripe subscription "
                f"{row.source_ref}, not {stripe_subscription_id}"
            )
        if (
            row is not None
            and stripe_subscription_id
            and row.source == "manual"
            and row.source_ref is None
        ):
            row.source = "stripe"
            row.source_ref = stripe_subscription_id

    if row is not None:
        row.owner_type = "org"
        row.owner_id = org_id
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()

    await db.execute(
        update(User).where(User.id == user_id).values(entitlement_id=None)
    )
    await db.execute(
        update(User)
        .where(User.id == user_id, User.tier != "admin")
        .values(tier="free")
    )
    return row


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
