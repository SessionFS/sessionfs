"""Organization management routes — create, members, invites, roles."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    BillingCheckoutIntent,
    BillingReconciliation,
    Entitlement,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.roles import OrgRole
from sessionfs.server.services.entitlements import (
    BillingLockRetry,
    EntitlementBindingConflict,
    adopt_user_entitlement_for_org,
    apply_entitlement,
    lock_billing_rows,
)
from sessionfs.server.tier_gate import UserContext, check_role, get_user_context

logger = logging.getLogger("sessionfs.api")
router = APIRouter(prefix="/api/v1/org", tags=["organization"])


# --- Request/Response schemas ---


class CreateOrgRequest(BaseModel):
    name: str
    slug: str

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if len(v) < 3:
            raise ValueError("Slug must be at least 3 characters")
        if len(v) > 100:
            raise ValueError("Slug must be 100 characters or fewer")
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", v):
            raise ValueError("Slug must be lowercase alphanumeric and hyphens, starting with alphanumeric")
        return v


class InviteRequest(BaseModel):
    email: str
    role: str = "member"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class ChangeRoleRequest(BaseModel):
    role: str


class OrgResponse(BaseModel):
    org_id: str
    name: str
    slug: str


class OrgInfoResponse(BaseModel):
    org: dict | None
    members: list[dict]
    current_user_role: str | None


class InviteResponse(BaseModel):
    invite_id: str
    email: str
    role: str


# --- Routes ---


@router.post("", response_model=OrgResponse)
async def create_organization(
    data: CreateOrgRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an organization. The creator becomes its owner."""
    from sqlalchemy import and_, or_

    from sessionfs.server.routes import billing as _billing

    # A pointer-only adoption (no entitlement row anywhere for the creator
    # or the subscription) must not trust the cached pointer + tier cache:
    # confirm the subscription's canonical state with Stripe BEFORE opening
    # the locked transaction (no Stripe I/O inside one). When any
    # entitlement evidence exists, adoption handles provenance instead and
    # no fetch is needed. Snapshot the creator id first: the rollback below
    # expires the user object, and reading it afterwards would be lazy IO.
    creator_id = user.id
    prefetched = None
    auth_sub_pointer = user.stripe_subscription_id
    if auth_sub_pointer:
        has_evidence = (
            await db.execute(
                select(Entitlement.id)
                .where(
                    or_(
                        and_(
                            Entitlement.source == "stripe",
                            Entitlement.source_ref == auth_sub_pointer,
                        ),
                        and_(
                            Entitlement.owner_type == "user",
                            Entitlement.owner_id == creator_id,
                            Entitlement.status == "active",
                        ),
                    )
                )
                .limit(1)
            )
        ).first()
        if has_evidence is None:
            await db.rollback()  # no transaction across the Stripe call
            try:
                prefetched = await _billing._fetch_subscription_state(db, auth_sub_pointer)
            except HTTPException:
                raise
            except Exception:
                logger.warning(
                    "Stripe unreachable confirming subscription %s during org creation",
                    auth_sub_pointer, exc_info=True,
                )
                raise HTTPException(
                    503,
                    {"error": "billing_unavailable",
                     "message": "Could not confirm your subscription with Stripe. Try again shortly."},
                )

    # Universal billing lock order (services/entitlements.lock_billing_rows):
    # Stripe customer -> user row -> org row -> entitlement, shared with the
    # checkout and subscription webhooks, so a webhook for this customer
    # can't interleave with the adoption below. The user row lock also
    # serializes with invite acceptance and domain auto-join (user -> org).
    # The locked row is refreshed (populate_existing), so the tier, Stripe
    # ids and active flag below come from it, not the auth-time snapshot.
    # If the customer changes between read and lock, BillingLockRetry
    # answers 409 so the client retries (never a late customer lock).
    # creator_id, not user.id: the prefetch branch's rollback expired the
    # user object, and reading it here would be lazy IO outside a greenlet.
    # Lock every current/pending claimant of the creator's subscription
    # pointer too, not just the creator: an ambiguous pointer (two owners
    # carrying the same sub id — migration 075 leaves those unbound) must be
    # caught under the same lock set the webhooks take, never granted to
    # whichever claimant happens to sort first.
    prelock_claimants = (
        await _billing._subscription_claimants(db, auth_sub_pointer)
        if auth_sub_pointer
        else []
    )
    # GH #70 — membership add: the user's SCOPE lock comes before every
    # billing lock and row lock (scope_locks.py global order), so creating
    # an org serializes with user delete, member removal and other joins.
    # Retaken on every attempt (a rollback releases it).
    from sessionfs.server.services.scope_locks import lock_scopes

    await lock_scopes(db, user_ids=[creator_id], org_ids=[])
    locked = await lock_billing_rows(
        db,
        user_ids=[creator_id] + [cid for k, cid in prelock_claimants if k == "user"],
        org_ids=[cid for k, cid in prelock_claimants if k == "org"],
    )
    locked_user = locked.get(("user", creator_id))
    if locked_user is None or not locked_user.is_active:
        raise HTTPException(403, "Account is inactive")
    user_id = locked_user.id
    user_tier = locked_user.tier
    stripe_sub_snapshot = locked_user.stripe_subscription_id
    stripe_customer_snapshot = locked_user.stripe_customer_id
    pending_sub_snapshot = locked_user.stripe_pending_subscription_id

    # The pointer must still match the state prefetched above (if any): a
    # cancellation webhook (or a competing adoption) can clear or move it
    # between the prefetch and this lock. Retry rather than carry on — the
    # pointer-only branch below would otherwise skip its Stripe confirmation
    # and keep a stale paid tier as a permanent manual grant.
    if prefetched is not None and stripe_sub_snapshot != prefetched.subscription_id:
        await db.rollback()
        raise BillingLockRetry("subscription pointer changed during org creation")

    # Refuse an ambiguous subscription pointer before adoption can rebind the
    # creator's manual entitlement to Stripe and steal a subscription two
    # owners share — the exact "first claimant wins" outcome the webhook path
    # prevents. Mirror _handle_subscription_event: an existing (stripe,
    # sub_id) binding is authoritative, otherwise more than one claimant is
    # ambiguous unless the legacy org-plus-member duplicate provably resolves
    # to a single org.
    if stripe_sub_snapshot:
        binding = await _billing._subscription_binding(stripe_sub_snapshot, db)
        if binding is None:
            claimants = await _billing._subscription_claimants(db, stripe_sub_snapshot)
            if len(claimants) > 1 and await _billing._legacy_org_claimant(db, claimants) is None:
                await db.rollback()
                db.add(
                    BillingReconciliation(
                        id=f"brc_{secrets.token_hex(12)}",
                        kind="ambiguous_subscription_pointer",
                        status="open",
                        stripe_customer_id=stripe_customer_snapshot,
                        stripe_subscription_id=stripe_sub_snapshot,
                        owner_type="user",
                        owner_id=user_id,
                        details=json.dumps({
                            "reason": "more than one owner claims this subscription at org creation",
                            "claimants": [f"{k}:{i}" for k, i in claimants],
                            "action": "nothing adopted; assign the subscription manually",
                        }),
                    )
                )
                await db.commit()
                logger.error(
                    "Org creation refused for user %s: ambiguous subscription pointer %s "
                    "claimed by %s",
                    user_id, stripe_sub_snapshot,
                    [f"{k}:{i}" for k, i in claimants],
                )
                raise HTTPException(
                    409,
                    {
                        "error": "ambiguous_subscription_pointer",
                        "message": "More than one billing record claims your subscription. "
                        "Contact support to assign it before creating an organization.",
                    },
                )

    # A pointer-only adoption is confirmed only when Stripe's canonical state
    # (prefetched before the locks) is an org-capable, granting subscription
    # on this user's customer — the cached tier/pointer are never trusted on
    # their own. Used for the tier gate, the tier/seat derivation and the
    # entitlement below.
    pointer_confirmed = (
        prefetched is not None
        and prefetched.subscription_id == stripe_sub_snapshot
        and not prefetched.problem
        and prefetched.canonical is not None
        and prefetched.canonical.status in _billing._GRANTING_STATUSES
        and prefetched.tier in ("team", "enterprise")
        and prefetched.canonical.customer_id in (None, stripe_customer_snapshot)
    )

    # Check user isn't already in an org. This runs before the tier check:
    # creating an org moves the creator's paid entitlement to the org and
    # drops their personal tier cache to 'free', so an existing member must
    # get the accurate 409 rather than an "upgrade to Team" 403.
    existing_member = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user_id).limit(1)
    )
    if existing_member.scalar_one_or_none():
        raise HTTPException(409, "You are already a member of an organization")

    # Check user has a Team+ subscription. A confirmed pointer-only adoption
    # gates on Stripe's own tier (not the cache): the cache may lag a
    # just-completed Team checkout, or carry a stale paid tier a cancellation
    # webhook already cleared.
    if pointer_confirmed:
        assert prefetched is not None
        effective_tier = prefetched.tier or user_tier
    else:
        effective_tier = user_tier
    if effective_tier not in ("team", "enterprise", "admin"):
        raise HTTPException(403, "Organizations require Team tier or above. Upgrade at https://sessionfs.dev/pricing")

    # Check slug uniqueness
    existing = await db.execute(
        select(Organization).where(Organization.slug == data.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Organization slug '{data.slug}' is already taken")

    # An open personal-plan checkout (Starter/Pro) can't move to an org:
    # it would complete into a subscription that can't fund one. Make the
    # user finish or abandon it first.
    personal_open = (
        await db.execute(
            select(BillingCheckoutIntent.id).where(
                BillingCheckoutIntent.owner_type == "user",
                BillingCheckoutIntent.owner_id == user_id,
                BillingCheckoutIntent.status.in_(("pending", "unresolved")),
                BillingCheckoutIntent.tier.notin_(("team", "enterprise")),
            ).limit(1)
        )
    ).first()
    if personal_open is not None:
        raise HTTPException(
            409,
            {
                "error": "personal_checkout_open",
                "message": "You have an open checkout for a personal plan. Finish it or let it "
                "expire before creating an organization.",
            },
        )

    org_id = f"org_{secrets.token_hex(8)}"
    if pointer_confirmed:
        # Tier and seat quantity come from Stripe's canonical state — never
        # the cached User.tier or the plan defaults — so a pointer-only
        # adoption matches what Stripe actually sells.
        assert prefetched is not None and prefetched.canonical is not None
        assert prefetched.tier is not None
        tier = prefetched.tier
        quantity = prefetched.canonical.quantity
        seats = quantity if quantity and quantity > 0 else (25 if tier == "enterprise" else 5)
        storage = 0 if tier == "enterprise" else seats * 1024 * 1024 * 1024
    else:
        tier = user_tier if user_tier in ("team", "enterprise") else "team"

        # Seats: carried over from the creator's current paid entitlement when
        # it records them, else the plan default. No Stripe call here — this
        # runs under the billing locks; the next subscription webhook applies
        # Stripe's canonical quantity anyway.
        seats = 5  # default
        carried_seats = (
            await db.execute(
                select(Entitlement.seats_limit).where(
                    Entitlement.owner_type == "user",
                    Entitlement.owner_id == user_id,
                    Entitlement.status == "active",
                )
            )
        ).scalar_one_or_none()
        if carried_seats and carried_seats > 0:
            seats = carried_seats

        # Enterprise gets unlimited storage (0 = unlimited in check_storage)
        if tier == "enterprise":
            storage = 0  # unlimited
            if seats == 5:
                seats = 25  # enterprise default
        else:
            storage = seats * 1024 * 1024 * 1024  # 1GB per seat for team

    org = Organization(
        id=org_id,
        name=data.name,
        slug=data.slug,
        tier=tier,
        stripe_customer_id=stripe_customer_snapshot,
        stripe_subscription_id=stripe_sub_snapshot,
        stripe_pending_subscription_id=pending_sub_snapshot,
        seats_limit=seats,
        storage_limit_bytes=storage,
    )
    db.add(org)
    # v0.10.24 tk_17b39010f9a64cba — force the Organization INSERT to
    # land BEFORE OrgMember is queued, otherwise SQLAlchemy's unit-of-
    # work flush at commit() doesn't reliably topologically sort the
    # two pending INSERTs by FK dependency and OrgMember runs first
    # → FK violation → 500. This used to be masked for Stripe-paying
    # users because the `update(User)` below triggered an implicit
    # autoflush; users with no stripe_customer_id or
    # stripe_subscription_id (every manual-license enterprise customer)
    # skipped that branch and hit the FK error. najitestech (GH #51)
    # was the first such customer to surface it.
    await db.flush()

    # The Stripe ids were snapshotted from the locked row above, before the
    # bulk update(User) below nulls them on the in-memory object (Codex R1
    # MEDIUM: deriving source/source_ref afterward would lose provenance).

    # Transfer subscription ownership: clear user-level Stripe fields
    # so they can't be confused with a personal subscription later.
    if stripe_customer_snapshot or stripe_sub_snapshot or pending_sub_snapshot:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                stripe_customer_id=None,
                stripe_subscription_id=None,
                stripe_pending_subscription_id=None,
            )
        )

    # The creator's open checkout (pending or unresolved) moves with the
    # customer: otherwise the org would see no open checkout and start a
    # second session that could become a second subscription.
    await db.execute(
        update(BillingCheckoutIntent)
        .where(
            BillingCheckoutIntent.owner_type == "user",
            BillingCheckoutIntent.owner_id == user_id,
            BillingCheckoutIntent.status.in_(("pending", "unresolved")),
        )
        .values(owner_type="org", owner_id=org_id)
    )

    # Move whatever funded the creator's personal paid tier onto the org:
    # the (stripe, sub_id) row a checkout created, or the legacy active
    # user entitlement migration 050 backfilled as manual. Inserting a
    # second (stripe, sub_id) row would violate uq_entitlements_source_ref,
    # and leaving the user's row active would keep paid access after they
    # leave the org.
    try:
        adopted = await adopt_user_entitlement_for_org(
            user_id=user_id,
            org_id=org_id,
            stripe_subscription_id=stripe_sub_snapshot,
            db=db,
        )
    except EntitlementBindingConflict as exc:
        await db.rollback()
        # Durable record for support (its own transaction; the org create
        # was rolled back).
        db.add(
            BillingReconciliation(
                id=f"brc_{secrets.token_hex(12)}",
                kind="subscription_binding_conflict",
                status="open",
                stripe_customer_id=stripe_customer_snapshot,
                stripe_subscription_id=stripe_sub_snapshot,
                owner_type="user",
                owner_id=user_id,
                details=json.dumps({"reason": str(exc), "action": "org creation refused"}),
            )
        )
        await db.commit()
        logger.error("Org creation refused for user %s: %s", user_id, exc)
        raise HTTPException(
            409,
            {
                "error": "subscription_binding_conflict",
                "message": "Your subscription is attached to a different billing record. "
                "Contact support to move it before creating an organization.",
            },
        )

    if adopted is not None:
        # Keep the moved row's provenance: a Stripe binding stays Stripe
        # (including the migration-050 manual shape just rebound), and
        # admin_provisioned / helm_license / manual stay as they are — they
        # are never converted to Stripe without Stripe's own confirmation.
        ent_source, ent_source_ref = adopted.source, adopted.source_ref
        # A webhook can grant between the prefetch above and these billing
        # locks (the prefetch only runs while there is no entitlement
        # evidence). When the adopted row already carries NEWER Stripe state
        # than the prefetch, keep its plan/seats instead of overwriting them
        # with the stale prefetched state below — and leave its generation
        # watermark intact.
        if (
            prefetched is not None
            and adopted.stripe_synced_generation is not None
            and adopted.stripe_synced_generation > prefetched.generation
        ):
            tier = adopted.tier
            if adopted.seats_limit is not None and adopted.seats_limit > 0:
                seats = adopted.seats_limit
            else:
                seats = 25 if tier == "enterprise" else 5
            storage = adopted.storage_limit_bytes
            if storage is None:
                storage = 0 if tier == "enterprise" else seats * 1024 * 1024 * 1024
    elif stripe_sub_snapshot:
        # No entitlement row at all, only the creator's Stripe subscription
        # pointer. Adopt ONLY on Stripe's own confirmation — prefetched
        # above, before the locks: canonical state must be a granting,
        # org-capable (Team/Enterprise) subscription on the same customer.
        # Anything else is refused with a durable reconciliation row instead
        # of synthesizing an active Stripe entitlement from a bare pointer
        # and a cached tier.
        if not pointer_confirmed:
            await db.rollback()
            db.add(
                BillingReconciliation(
                    id=f"brc_{secrets.token_hex(12)}",
                    kind="unconfirmed_subscription_adoption",
                    status="open",
                    stripe_customer_id=stripe_customer_snapshot,
                    stripe_subscription_id=stripe_sub_snapshot,
                    owner_type="user",
                    owner_id=user_id,
                    details=json.dumps({
                        "reason": "Stripe did not confirm a granting Team/Enterprise "
                        "subscription for the pointer-only adoption",
                        "problem": prefetched.problem if prefetched else "no prefetch",
                        "status": (
                            prefetched.canonical.status
                            if prefetched and prefetched.canonical else None
                        ),
                        "tier": prefetched.tier if prefetched else None,
                        "action": "org creation refused; confirm the subscription and retry",
                    }, default=str),
                )
            )
            await db.commit()
            logger.error(
                "Org creation refused for user %s: unconfirmed subscription %s",
                user_id, stripe_sub_snapshot,
            )
            raise HTTPException(
                409,
                {
                    "error": "unconfirmed_subscription_adoption",
                    "message": "Your subscription could not be confirmed with Stripe as an "
                    "active Team/Enterprise subscription. Contact support.",
                },
            )

        # Reconcile the prefetched state against the observation row (under
        # the billing locks). A terminal state observed at an equal-or-higher
        # fetch generation supersedes this granting fetch, so the adoption is
        # refused rather than synthesizing a grant a cancellation already
        # superseded.
        assert prefetched is not None and prefetched.canonical is not None
        prev = await _billing._observe(db, prefetched)
        prev_terminal_gen = prev[1] if prev else None
        if prev_terminal_gen is not None and prefetched.generation <= prev_terminal_gen:
            await db.rollback()
            db.add(
                BillingReconciliation(
                    id=f"brc_{secrets.token_hex(12)}",
                    kind="unconfirmed_subscription_adoption",
                    status="open",
                    stripe_customer_id=stripe_customer_snapshot,
                    stripe_subscription_id=stripe_sub_snapshot,
                    owner_type="user",
                    owner_id=user_id,
                    details=json.dumps({
                        "reason": "a terminal observation supersedes this granting fetch",
                        "generation": prefetched.generation,
                        "terminal_generation": prev_terminal_gen,
                        "action": "org creation refused; the subscription is over",
                    }, default=str),
                )
            )
            await db.commit()
            logger.error(
                "Org creation refused for user %s: subscription %s superseded by a terminal "
                "observation (generation %s <= %s)",
                user_id, stripe_sub_snapshot, prefetched.generation, prev_terminal_gen,
            )
            raise HTTPException(
                409,
                {
                    "error": "unconfirmed_subscription_adoption",
                    "message": "Your subscription could not be confirmed with Stripe as an "
                    "active Team/Enterprise subscription. Contact support.",
                },
            )
        ent_source, ent_source_ref = "stripe", stripe_sub_snapshot
    else:
        ent_source, ent_source_ref = "manual", None

    # P2: write-through — create the entitlement so resolution is authoritative.
    ent = await apply_entitlement(
        "org",
        org_id,
        tier=tier,
        seats=seats,
        storage=storage,
        source=ent_source,
        source_ref=ent_source_ref,
        db=db,
    )
    if pointer_confirmed and prefetched is not None:
        # Record the fetch generation of the Stripe state this adoption
        # applied, so a later webhook carrying an older state can't roll it
        # back (the same guard _apply_canonical uses). Never lower a newer
        # generation a webhook already recorded on the adopted row.
        if ent.stripe_synced_generation is None or prefetched.generation > ent.stripe_synced_generation:
            ent.stripe_synced_generation = prefetched.generation

    # Creator is the owner. Every org has exactly one owner (v0.11.0
    # entitlements model; uq_org_members_one_owner_per_org), matching
    # license activation. Inserting the creator as "admin" left these orgs
    # ownerless (tk_d311dcf71be74a97; repaired by migration 074).
    member = OrgMember(
        org_id=org_id,
        user_id=user_id,
        role=OrgRole.OWNER.value,
    )
    db.add(member)
    await db.commit()

    return OrgResponse(org_id=org_id, name=data.name, slug=data.slug)


@router.get("", response_model=OrgInfoResponse)
async def get_organization_info(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Get the user's organization info and member list."""
    if not ctx.is_org_user or not ctx.org:
        return OrgInfoResponse(org=None, members=[], current_user_role=None)

    result = await db.execute(
        select(OrgMember, User)
        .join(User, OrgMember.user_id == User.id)
        .where(OrgMember.org_id == ctx.org.id)
    )
    rows = result.all()

    members = []
    for member, member_user in rows:
        members.append({
            "user_id": member.user_id,
            "email": member_user.email,
            "display_name": member_user.display_name,
            "role": member.role,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        })

    return OrgInfoResponse(
        org={
            "id": ctx.org.id,
            "name": ctx.org.name,
            "slug": ctx.org.slug,
            "tier": ctx.org.tier,
            "seats_limit": ctx.org.seats_limit,
            "seats_used": len(members),
            "storage_limit_bytes": ctx.org.storage_limit_bytes,
            "storage_used_bytes": ctx.org.storage_used_bytes,
        },
        members=members,
        current_user_role=ctx.role,
    )


@router.post("/invite", response_model=InviteResponse)
async def invite_member(
    data: InviteRequest,
    request: Request,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Invite a user to the org via email. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    # Check seat limit
    result = await db.execute(
        select(OrgMember).where(OrgMember.org_id == ctx.org.id)
    )
    members = result.scalars().all()
    if len(members) >= ctx.org.seats_limit:
        raise HTTPException(403, {
            "error": "seat_limit",
            "seats_used": len(members),
            "seats_limit": ctx.org.seats_limit,
            "message": "All seats are in use. Upgrade for more seats.",
        })

    # Lookup ANY existing row for this (org_id, email). Migration 016
    # enforces uq_org_invites_org_email so at most one row can exist.
    # We classify by state below — if no row exists we INSERT; if a
    # stale (declined / expired / orphan-accepted) row exists we
    # UPDATE in place. An active row (no terminal state) is the only
    # case that returns 409 here.
    #
    # tk_d88678e6fe384de6 — the v0.10.22 R1 MED fix made the active-
    # invite predicate more permissive at the route layer, but the
    # DB UniqueConstraint still blocked the INSERT with IntegrityError
    # (= 409 duplicate_resource via the v0.10.24 envelope). The
    # UPSERT pattern below resolves that mismatch.
    #
    # `.with_for_update()` serializes concurrent re-invites against
    # the same stale row on PG. Without the lock, two requests can
    # both load the old primary key; the first commits the new id,
    # and the second flushes its UPDATE against the now-missing old
    # id → SQLAlchemy 0-row-update / StaleDataError → 500 instead of
    # the AC's atomic behavior. SQLite is single-writer at the engine
    # level, so it ignores FOR UPDATE harmlessly. Codex R1 MED on
    # this ticket.
    stale = (
        await db.execute(
            select(OrgInvite)
            .where(
                OrgInvite.org_id == ctx.org.id,
                OrgInvite.email == data.email,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if stale is not None:
        stale_expires = stale.expires_at
        # SQLite roundtrips DateTime(timezone=True) as naive; coerce
        # to tz-aware before comparing so Helm-on-SQLite tests don't
        # raise offset-naive/offset-aware errors.
        if stale_expires.tzinfo is None:
            stale_expires = stale_expires.replace(tzinfo=timezone.utc)
        is_active = (
            stale.accepted_at is None
            and stale.declined_at is None
            and stale_expires > now
        )
        if is_active:
            raise HTTPException(
                409, "An active invite already exists for this email"
            )

    # Check user isn't already a member (covers both: existing member
    # added via a different path AND orphan-accepted stale invite with
    # a live OrgMember row).
    existing_user = await db.execute(
        select(User).where(User.email == data.email)
    )
    target_user = existing_user.scalar_one_or_none()
    if target_user:
        existing_membership = await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == ctx.org.id,
                OrgMember.user_id == target_user.id,
            )
        )
        if existing_membership.scalar_one_or_none():
            raise HTTPException(
                409, "This user is already a member of your organization"
            )

    role = data.role if data.role in ("admin", "member") else "member"
    new_invite_id = f"inv_{secrets.token_hex(8)}"
    new_expires_at = now + timedelta(days=7)

    if stale is not None:
        # UPDATE-in-place: regenerate `id` so stale email-acceptance
        # links from the prior attempt are invalidated; refresh
        # `expires_at`; clear all terminal-state fields; update
        # role/invited_by. Preserve `created_at` as the audit signal
        # of when the email was first onboarded.
        stale.id = new_invite_id
        stale.role = role
        stale.invited_by = ctx.user.id
        stale.expires_at = new_expires_at
        stale.accepted_at = None
        stale.declined_at = None
        stale.decline_reason = None
        stale.last_emailed_at = None
        invite = stale
    else:
        invite = OrgInvite(
            id=new_invite_id,
            org_id=ctx.org.id,
            email=data.email,
            role=role,
            invited_by=ctx.user.id,
            expires_at=new_expires_at,
        )
        db.add(invite)

    await db.commit()
    await db.refresh(invite)
    invite_id = invite.id

    # v0.10.22 (tk_6afbcfefe5804c1d) — best-effort email send. The
    # invite row is already durable; a transient email failure logs
    # but does not 500 the route (admin can resend later).
    from sessionfs.server.services.invite_helpers import dispatch_invite_email

    await dispatch_invite_email(
        request=request,
        db=db,
        invite=invite,
        org=ctx.org,
        inviter=ctx.user,
    )

    return InviteResponse(invite_id=invite_id, email=data.email, role=role)


@router.post("/invite/{invite_id}/accept")
async def accept_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept an org invite."""
    result = await db.execute(
        select(OrgInvite).where(OrgInvite.id == invite_id)
    )
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(404, "Invite not found")
    if invite.accepted_at:
        raise HTTPException(400, "Invite already accepted")
    # v0.10.22 — explicit declined check (column added in migration 046).
    # Without this an accepted invite that was then re-declined elsewhere
    # would slip through; defense in depth even though the flow normally
    # writes one or the other.
    if invite.declined_at:
        raise HTTPException(400, "Invite was declined")
    # SQLite naive-aware coercion — see decline_invite for context.
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Invite has expired")
    if (invite.email or "").strip().lower() != (user.email or "").strip().lower():
        raise HTTPException(403, "This invite is for a different email address")

    # GH #70 — membership add/remove: the user's scope lock (then the
    # org's), before any row lock, so it serializes with user delete,
    # member removal and other joins (scope_locks.py global order).
    from sessionfs.server.services.scope_locks import lock_scopes

    await lock_scopes(db, user_ids=[user.id], org_ids=[invite.org_id])

    # GH #70 — re-read the user under the user lock: a concurrent delete may
    # have deactivated them while we waited. Never add an inactive user.
    _locked_user = (
        await db.execute(
            select(User)
            .where(User.id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if _locked_user is None or not _locked_user.is_active:
        raise HTTPException(403, "Account is inactive")

    # Lock the user row first (same user -> org order as domain auto-join),
    # then check membership, so a concurrent auto-join into another org and
    # this acceptance can't both see "no membership" and both insert.
    await db.execute(select(User).where(User.id == user.id).with_for_update())

    # Check user isn't already in an org
    existing = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user.id).limit(1)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You are already a member of an organization")

    # Re-check seat capacity at acceptance time (not just invite creation).
    # Lock the Organization row so this serializes with SSO JIT provisioning,
    # domain auto-join and other acceptances, which lock the same row before
    # counting seats; otherwise two can both see one free seat.
    org_result = await db.execute(
        select(Organization).where(Organization.id == invite.org_id).with_for_update()
    )
    org = org_result.scalar_one_or_none()
    if org:
        member_count_result = await db.execute(
            select(OrgMember).where(OrgMember.org_id == invite.org_id)
        )
        current_members = len(member_count_result.scalars().all())
        if current_members >= org.seats_limit:
            raise HTTPException(403, {
                "error": "seat_limit",
                "seats_used": current_members,
                "seats_limit": org.seats_limit,
                "message": "All seats are in use. Ask an admin to upgrade for more seats.",
            })

    member = OrgMember(
        org_id=invite.org_id,
        user_id=user.id,
        role=invite.role,
        invited_by=invite.invited_by,
        invited_at=invite.created_at,
    )
    db.add(member)

    # Atomic gate — must be the last write before commit so a
    # concurrent /decline (or another /accept) loses the race
    # cleanly. Codex v0.10.22 R1 MEDIUM (tk_6afbcfefe5804c1d). If
    # the rowcount is zero, another transition won the race; roll
    # back the pending OrgMember add and return 409.
    now_ts = datetime.now(timezone.utc)
    # synchronize_session=False so the ORM doesn't evaluate the WHERE
    # clause client-side — that path hits the SQLite naive vs PG
    # tz-aware datetime mismatch. Server-side evaluation is correct
    # on both backends.
    result = await db.execute(
        update(OrgInvite)
        .where(
            OrgInvite.id == invite_id,
            OrgInvite.accepted_at.is_(None),
            OrgInvite.declined_at.is_(None),
            OrgInvite.expires_at > now_ts,
        )
        .values(accepted_at=now_ts)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Invite state changed concurrently; reload and retry")
    await db.commit()

    return {"org_id": invite.org_id, "role": invite.role}


class DeclineInviteRequest(BaseModel):
    reason: str | None = None


@router.post("/invite/{invite_id}/decline")
async def decline_invite(
    invite_id: str,
    body: DeclineInviteRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recipient declines an org invite.

    Atomic — refuses if the invite was already accepted/declined/expired.
    Pairs with the dashboard `/invites` Decline button. v0.10.22 —
    tk_6afbcfefe5804c1d.
    """
    invite = (
        await db.execute(select(OrgInvite).where(OrgInvite.id == invite_id))
    ).scalar_one_or_none()
    if not invite:
        raise HTTPException(404, "Invite not found")
    if invite.accepted_at:
        raise HTTPException(400, "Invite already accepted")
    if invite.declined_at:
        raise HTTPException(400, "Invite already declined")
    # SQLite naive-aware coercion — see resend_invite for context.
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(400, "Invite has expired")
    if (invite.email or "").strip().lower() != (user.email or "").strip().lower():
        raise HTTPException(403, "This invite is for a different email address")

    reason = (body.reason.strip() if body and body.reason else None) or None
    if reason and len(reason) > 1000:
        reason = reason[:1000]

    # Atomic gate — same pattern as /accept. Rowcount-1 guard so a
    # concurrent /accept (or another /decline) loses the race
    # cleanly. Codex v0.10.22 R1 MEDIUM (tk_6afbcfefe5804c1d).
    now_ts = datetime.now(timezone.utc)
    result = await db.execute(
        update(OrgInvite)
        .where(
            OrgInvite.id == invite_id,
            OrgInvite.accepted_at.is_(None),
            OrgInvite.declined_at.is_(None),
            OrgInvite.expires_at > now_ts,
        )
        .values(declined_at=now_ts, decline_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Invite state changed concurrently; reload and retry")
    await db.commit()

    return {"invite_id": invite_id, "declined_at": now_ts.isoformat()}


class MyInviteEntry(BaseModel):
    invite_id: str
    org_id: str
    org_name: str
    role: str
    invited_by_email: str
    created_at: datetime
    expires_at: datetime


class MyInvitesResponse(BaseModel):
    invites: list[MyInviteEntry]


@router.get("/invites/me", response_model=MyInvitesResponse)
async def list_my_invites(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyInvitesResponse:
    """Pending org invites for the logged-in user (matched on email).

    Drives the dashboard `/invites` page and the post-login banner.
    Filters out accepted, declined, and expired rows so the dashboard
    can render directly without re-filtering. v0.10.22 —
    tk_6afbcfefe5804c1d.
    """
    user_email = (user.email or "").strip().lower()
    if not user_email:
        return MyInvitesResponse(invites=[])

    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(OrgInvite, Organization, User)
            .join(Organization, OrgInvite.org_id == Organization.id)
            .join(User, OrgInvite.invited_by == User.id)
            .where(
                OrgInvite.email == user_email,
                OrgInvite.accepted_at.is_(None),
                OrgInvite.declined_at.is_(None),
                # Server-side comparison: PG honors timezone, SQLite
                # stores naive but comparing two columns/values written
                # in the same dialect roundtrips correctly. The naive-
                # aware coercion only matters when comparing a DB
                # value to a Python datetime in route code (see
                # accept/decline/resend handlers).
                OrgInvite.expires_at > now,
            )
            .order_by(OrgInvite.created_at.desc())
        )
    ).all()

    return MyInvitesResponse(
        invites=[
            MyInviteEntry(
                invite_id=invite.id,
                org_id=org.id,
                org_name=org.name,
                role=invite.role,
                invited_by_email=inviter.email,
                created_at=invite.created_at,
                expires_at=invite.expires_at,
            )
            for invite, org, inviter in rows
        ]
    )


@router.put("/members/{user_id}/role")
async def change_member_role(
    user_id: str,
    data: ChangeRoleRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Change a member's role. Admin only.

    Delegates to `org_members.perform_role_change()` so the same
    guards (last-admin, self-role, target-not-member) AND the
    admin→member source-authority cleanup fire from BOTH this legacy
    `/api/v1/org/*` surface and the new `/api/v1/orgs/{org_id}/*`
    surface — Codex Phase-3a round-5 MEDIUM (KB entry 262).
    """
    from sessionfs.server.routes.org_members import perform_role_change

    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    return await perform_role_change(db, ctx.user, ctx.org.id, user_id, data.role)


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member from the org. Admin only.

    Delegates to `org_members.perform_member_removal()` so the
    same CEO data-stays invariants fire from BOTH this legacy
    `/api/v1/org/*` surface and the new `/api/v1/orgs/{org_id}/*`
    surface — Codex Phase-3a round-1 HIGH (KB entry 254).

    The legacy route adds nothing here beyond:
      - the `ctx.role == admin` precondition
      - resolving the single-org via `ctx.org.id` (legacy callers
        don't pass org_id in the URL)
    """
    from sessionfs.server.routes.org_members import perform_member_removal

    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    return await perform_member_removal(db, ctx.user, ctx.org.id, user_id)


@router.get("/invites")
async def list_invites(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """List pending org invites. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    # Codex v0.10.22 R2 LOW (tk_6afbcfefe5804c1d) — declined + expired
    # rows must not show up under "Pending Invites" in the dashboard.
    # Stays consistent with the duplicate-active-invite check on the
    # creation paths and with the new GET /api/v1/org/invites/me filter.
    result = await db.execute(
        select(OrgInvite).where(
            OrgInvite.org_id == ctx.org.id,
            OrgInvite.accepted_at.is_(None),
            OrgInvite.declined_at.is_(None),
            OrgInvite.expires_at > datetime.now(timezone.utc),
        )
    )
    invites = result.scalars().all()

    return {
        "invites": [
            {
                "id": inv.id,
                "email": inv.email,
                "role": inv.role,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            }
            for inv in invites
        ]
    }


@router.delete("/invites/{invite_id}")
async def revoke_invite(
    invite_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a pending invite. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    result = await db.execute(
        select(OrgInvite).where(
            OrgInvite.id == invite_id,
            OrgInvite.org_id == ctx.org.id,
        )
    )
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(404, "Invite not found")

    await db.execute(delete(OrgInvite).where(OrgInvite.id == invite_id))
    await db.commit()

    return {"revoked": invite_id}
