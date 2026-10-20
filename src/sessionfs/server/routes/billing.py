"""Billing routes — Stripe Checkout, Customer Portal, subscription status."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import StripeEvent, User
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.errors import _PG_RETRYABLE_SQLSTATES, db_sqlstate
from sessionfs.server.services.entitlements import (
    BillingLockRetry,
    apply_entitlement,
    lock_billing_rows,
)
from sessionfs.server.tier_gate import UserContext, get_user_context
from sessionfs.server.tiers import get_storage_limit

logger = logging.getLogger("sessionfs.api")
router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

# Stripe price IDs — configured via env vars
TIER_PRICE_MAP = {
    "starter": os.environ.get("SFS_STRIPE_PRICE_STARTER", ""),
    "pro": os.environ.get("SFS_STRIPE_PRICE_PRO", ""),
    "team": os.environ.get("SFS_STRIPE_PRICE_TEAM", ""),
}


# Checkout idempotency window; the Stripe session expires two windows after
# the window it was opened in (always 30-60 minutes from creation).
_CHECKOUT_WINDOW_SECONDS = 1800
_CHECKOUT_EXPIRY_MARGIN_SECONDS = 600


def _get_stripe():
    """Lazy-import stripe to avoid hard dependency."""
    try:
        import stripe
        stripe.api_key = os.environ.get("SFS_STRIPE_SECRET_KEY", "")
        return stripe
    except ImportError:
        raise HTTPException(500, "Stripe not configured")


# --- Request/Response schemas ---


class CheckoutRequest(BaseModel):
    tier: str
    seats: int = 1


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalRequest(BaseModel):
    scope: str = "auto"  # "auto", "org", or "personal"


class PortalResponse(BaseModel):
    portal_url: str


class BillingStatusResponse(BaseModel):
    tier: str
    storage_used_bytes: int
    storage_limit_bytes: int
    stripe_customer_id: str | None
    has_subscription: bool
    has_personal_subscription: bool = False
    is_org_member: bool = False
    org_role: str | None = None
    is_beta: bool = True  # True when Stripe billing is not yet active for this deployment


# --- Routes ---
#
# Stripe I/O never happens while a database transaction (and so a lock or a
# pooled connection) is open. Billing routes and webhooks alternate between
# short transactions that take the billing locks (universal order: Stripe
# customer -> user -> org -> entitlement, see lock_billing_rows) and Stripe
# calls made with the async client between them. Every Stripe write is
# idempotent (idempotency keys), so a step can be retried safely.


_OPEN_INTENT_STATUSES = ("pending", "unresolved")
_CHECKOUT_STEP_ATTEMPTS = 4


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _field(obj, key, default=None):
    """Read a key from a Stripe object, a dict, or a plain object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
            if value is not default:
                return value
        except Exception:  # noqa: BLE001 — fall through to attribute access
            pass
    return getattr(obj, key, default)


@dataclass
class _CheckoutPlan:
    """A checkout to open: the intent row is already committed (pending)."""
    intent_id: str
    stripe_params: dict


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    data: CheckoutRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout session for subscription.

    At most one open checkout (pending, or unresolved) exists per billing
    owner and per Stripe customer (billing_checkout_intents). A second
    request while one is live reuses its session for the same plan or is
    refused. An intent past its expiry is only released after Stripe
    confirms the session expired — a session that completed (webhook not
    yet applied) turns the intent 'unresolved', which keeps blocking new
    checkouts until ops resolves it, and a Stripe outage fails closed.
    The Stripe session is created with an idempotency key scoped to the
    owner, scope, plan and a 30-minute window.

    No Stripe call is made inside a database transaction: each decision is
    taken in a short locked transaction, Stripe is called with the async
    client between them, and the next step re-validates under the locks.
    """
    stripe = _get_stripe()
    user_id = ctx.user.id
    org_id = ctx.org.id if ctx.is_org_user and ctx.org else None

    if data.tier not in TIER_PRICE_MAP:
        raise HTTPException(400, f"Invalid tier: {data.tier}")

    price_id = TIER_PRICE_MAP[data.tier]
    if not price_id:
        raise HTTPException(400, f"Stripe price not configured for tier: {data.tier}")

    # Org billing restrictions
    if org_id:
        # Org members can't buy personal plans
        if data.tier in ("starter", "pro"):
            raise HTTPException(
                400,
                {"error": "org_member_restriction", "message": "Organization members cannot purchase individual plans. Your tier is managed by the organization admin."},
            )
        # Only org admins can change org subscription (team/enterprise)
        # Owners rank above admins, so compare by level, not literally.
        if data.tier in ("team",) and not has_minimum_role(ctx.role or "", "admin"):
            raise HTTPException(
                403,
                {"error": "admin_required", "message": "Only organization admins can change the subscription."},
            )

    is_org_checkout = bool(org_id and data.tier in ("team", "enterprise"))
    seats = data.seats if data.tier == "team" else 1
    # End the read transaction the auth dependencies opened.
    await db.rollback()

    reserved_id = None  # this request's own reserved intent, if any
    swept = False  # this request already asked Stripe about outstanding state
    for _ in range(_CHECKOUT_STEP_ATTEMPTS):
        try:
            step = await _checkout_step(
                db, data, price_id, user_id, org_id, is_org_checkout, seats,
                reserved_id=reserved_id, swept=swept,
            )
            kind = step[0]
            if kind == "url":
                return CheckoutResponse(checkout_url=step[1])
            if kind == "verify":
                await _verify_lapsed_intents(db, stripe, step[1])
                reserved_id = None  # the reservation, if lapsed, is now closed
                continue
            if kind == "customer":
                reserved_id = step[1]
                await _ensure_stripe_customer(db, stripe, *step[1:])
                continue
            if kind == "sweep":
                await _sweep_for_outstanding_stripe_state(db, stripe, step[1])
                swept = True
                continue
        except BillingLockRetry:
            # A customer id changed while locks were being taken (e.g. a
            # concurrent request created it); the step rolled back — redo it.
            continue
        return await _open_checkout_session(db, stripe, step[1])
    raise HTTPException(
        409,
        {"error": "checkout_pending", "message": "Checkout state is changing; try again shortly."},
    )


async def _checkout_step(db, data, price_id, user_id, org_id, is_org_checkout, seats,
                         *, reserved_id=None, swept=False):
    """One short locked transaction deciding the next checkout step.

    Returns ("url", url) | ("verify", [(intent_id, session_id)]) |
    ("customer", intent_id, owner_type, owner_id, email, user_id, org_id) |
    ("sweep", customer_id) | ("create", _CheckoutPlan). Always ends its
    transaction.

    ``reserved_id`` is the intent this request already reserved (so its own
    reservation is adopted, not treated as a conflicting open checkout);
    ``swept`` notes this request already asked Stripe about outstanding
    sessions/subscriptions for the customer.
    """
    from sqlalchemy import and_, or_
    from sqlalchemy.exc import IntegrityError

    from sessionfs.server.db.models import BillingCheckoutIntent, OrgMember

    try:
        locked = await lock_billing_rows(
            db, user_ids=[user_id], org_ids=[org_id] if org_id else []
        )
        user = locked.get(("user", user_id))
        org = locked.get(("org", org_id)) if org_id else None
        if user is None:
            raise HTTPException(404, "User not found")

        # Re-resolve the caller's membership under the locks (FOR SHARE —
        # read, not mutate): an admin removed or demoted, or a user who
        # joined an org, since the auth snapshot was taken must not drive
        # this decision.
        memberships = (
            await db.execute(
                select(OrgMember)
                .where(OrgMember.user_id == user_id)
                .with_for_update(read=True)
            )
        ).scalars().all()
        membership = (
            next((m for m in memberships if m.org_id == org_id), None) if org_id else None
        )
        if data.tier in ("starter", "pro"):
            if memberships:
                raise HTTPException(
                    400,
                    {"error": "org_member_restriction", "message": "Organization members cannot purchase individual plans. Your tier is managed by the organization admin."},
                )
        elif org_id:
            if membership is None or not has_minimum_role(membership.role, "admin"):
                raise HTTPException(
                    403,
                    {"error": "admin_required", "message": "Only organization admins can change the subscription."},
                )
        elif memberships:
            # A solo team/enterprise checkout whose caller joined an org
            # since the snapshot: the org context must be re-resolved.
            raise HTTPException(
                409,
                {"error": "membership_changed", "message": "Your organization membership changed. Try again."},
            )

        # Prevent duplicate subscriptions — check both user and org,
        # including subscriptions still awaiting their first payment.
        # For org admins, a personal sub doesn't block org checkout
        # (different Stripe customer).
        org_cust = org.stripe_customer_id if org is not None else None
        user_has_personal_sub = bool(
            (user.stripe_subscription_id or user.stripe_pending_subscription_id)
            and user.stripe_customer_id
            and user.stripe_customer_id != org_cust
        )
        user_sub = user.stripe_subscription_id or user.stripe_pending_subscription_id
        if user_sub and not (org is not None and user_has_personal_sub):
            raise HTTPException(
                409,
                {"error": "already_subscribed", "message": "You already have an active subscription. Use the customer portal to manage it."},
            )
        if org is not None and (org.stripe_subscription_id or org.stripe_pending_subscription_id):
            raise HTTPException(
                409,
                {"error": "already_subscribed", "message": "Your organization already has an active subscription. Use the customer portal to manage it."},
            )

        if is_org_checkout and org is not None:
            owner_type, owner_id, scope, owner_row = "org", org.id, "org", org
        else:
            owner_type, owner_id, scope, owner_row = "user", user.id, "personal", user
        customer_id = owner_row.stripe_customer_id

        # Idempotency window: the same owner/scope/plan inside one 30-minute
        # window maps to the same Stripe request (and the same intent id),
        # and the Stripe session expires with the intent (30 min-24 h). The
        # margin keeps expires_at past Stripe's 30-minute minimum even for a
        # request at the very end of a window whose session is created after
        # several Stripe round-trips (or retried later in the same window).
        now = datetime.now(timezone.utc)
        window_start = int(now.timestamp()) // _CHECKOUT_WINDOW_SECONDS * _CHECKOUT_WINDOW_SECONDS
        expires_ts = window_start + 2 * _CHECKOUT_WINDOW_SECONDS + _CHECKOUT_EXPIRY_MARGIN_SECONDS
        idempotency_key = (
            f"sfs-checkout:{owner_type}:{owner_id}:{scope}:{data.tier}:{seats}:{window_start}"
        )

        # Open checkouts for this owner or this Stripe customer.
        conds = [
            and_(
                BillingCheckoutIntent.owner_type == owner_type,
                BillingCheckoutIntent.owner_id == owner_id,
            )
        ]
        if customer_id:
            conds.append(BillingCheckoutIntent.stripe_customer_id == customer_id)
        open_intents = (
            await db.execute(
                select(BillingCheckoutIntent)
                .where(
                    BillingCheckoutIntent.status.in_(_OPEN_INTENT_STATUSES),
                    or_(*conds),
                )
                .with_for_update()
            )
        ).scalars().all()

        lapsed = []
        own = None
        for intent in open_intents:
            if intent.status == "unresolved":
                raise HTTPException(
                    409,
                    {
                        "error": "checkout_unresolved",
                        "message": "A previous checkout completed but has not been applied yet. "
                        "Contact support before starting another.",
                    },
                )
            if _aware(intent.expires_at) > now:
                if (
                    not intent.checkout_url
                    and (
                        (reserved_id is not None and intent.id == reserved_id)
                        or intent.idempotency_key == idempotency_key
                    )
                    and intent.owner_type == owner_type
                    and intent.owner_id == owner_id
                    and intent.tier == data.tier
                    and intent.seats == seats
                ):
                    # This request's own reservation — or a retry's, after a
                    # crash between reserving and opening the session (the
                    # idempotency key then still returns the same session).
                    own = intent
                    continue
                if (
                    intent.owner_type == owner_type
                    and intent.owner_id == owner_id
                    and intent.tier == data.tier
                    and intent.seats == seats
                    and intent.checkout_url
                ):
                    url = intent.checkout_url
                    await db.rollback()
                    return ("url", url)
                raise HTTPException(
                    409,
                    {
                        "error": "checkout_pending",
                        "message": "A checkout is already in progress. "
                        "Finish it or wait for it to expire before starting another.",
                    },
                )
            lapsed.append((intent.id, intent.stripe_session_id))
        if lapsed:
            await db.rollback()
            return ("verify", lapsed)

        if own is not None:
            customer_id = own.stripe_customer_id or customer_id

        if not customer_id:
            # Reserve the checkout BEFORE calling Stripe: an org created
            # while Customer.create runs must see it (it blocks org creation
            # for personal plans and moves with the customer otherwise).
            if own is None:
                own = BillingCheckoutIntent(
                    id="bci_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32],
                    owner_type=owner_type,
                    owner_id=owner_id,
                    scope=scope,
                    tier=data.tier,
                    seats=seats,
                    created_by_user_id=user.id,
                    stripe_customer_id=None,
                    idempotency_key=idempotency_key,
                    status="pending",
                    expires_at=datetime.fromtimestamp(expires_ts, tz=timezone.utc),
                )
                db.add(own)
                try:
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    raise HTTPException(
                        409,
                        {
                            "error": "checkout_pending",
                            "message": "A checkout is already in progress. Finish it or try again shortly.",
                        },
                    )
            await db.commit()
            return ("customer", own.id, owner_type, owner_id, user.email, user_id, org_id)

        if not swept:
            # State created before checkout intents existed (or out of band)
            # is invisible to the intent scan: ask Stripe first.
            await db.rollback()
            return ("sweep", customer_id)

        if own is not None:
            intent_id = own.id
            intent_key = own.idempotency_key
            expires_ts = int(_aware(own.expires_at).timestamp())
        else:
            intent_id = "bci_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
            intent_key = idempotency_key
            db.add(
                BillingCheckoutIntent(
                    id=intent_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    scope=scope,
                    tier=data.tier,
                    seats=seats,
                    created_by_user_id=user.id,
                    stripe_customer_id=customer_id,
                    idempotency_key=intent_key,
                    status="pending",
                    expires_at=datetime.fromtimestamp(expires_ts, tz=timezone.utc),
                )
            )
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(
                    409,
                    {
                        "error": "checkout_pending",
                        "message": "A checkout is already in progress. Finish it or try again shortly.",
                    },
                )
        app_url = os.environ.get("SFS_APP_URL", "https://app.sessionfs.dev")
        plan = _CheckoutPlan(
            intent_id=intent_id,
            stripe_params=dict(
                customer=customer_id,
                payment_method_types=["card"],
                line_items=[{"price": price_id, "quantity": seats}],
                mode="subscription",
                expires_at=expires_ts,
                success_url=f"{app_url}/settings/billing?success=true",
                cancel_url=f"{app_url}/settings/billing?cancelled=true",
                metadata={
                    "user_id": user.id,
                    "tier": data.tier,
                    "seats": str(data.seats),
                    "checkout_intent_id": intent_id,
                    **({"org_id": owner_id} if owner_type == "org" else {}),
                },
                idempotency_key=intent_key,
            ),
        )
        await db.commit()
        return ("create", plan)
    except BaseException:
        await db.rollback()
        raise


async def _verify_lapsed_intents(db: AsyncSession, stripe, lapsed) -> None:
    """Release checkouts whose window passed — only once Stripe confirms the
    session expired. A completed session (webhook not applied yet) or one we
    can't identify becomes 'unresolved'; a Stripe outage fails closed.

    An intent with no recorded session is an interrupted reservation: no
    session id was ever stored for it, and any session Stripe actually
    opened for the customer is found (and blocks) by the pre-create sweep —
    so the reservation itself simply expires."""
    from sessionfs.server.db.models import BillingCheckoutIntent

    outcomes: dict[str, str] = {}
    for intent_id, session_id in lapsed:
        if not session_id:
            outcomes[intent_id] = "expired"
            continue
        try:
            session = await stripe.checkout.Session.retrieve_async(session_id)
        except Exception:
            logger.warning("Could not confirm checkout session %s with Stripe", session_id, exc_info=True)
            raise HTTPException(
                503,
                {
                    "error": "billing_unavailable",
                    "message": "Could not confirm the previous checkout with Stripe. Try again shortly.",
                },
            )
        status = _field(session, "status")
        outcomes[intent_id] = (
            "expired" if status == "expired" else "unresolved" if status == "complete" else "pending"
        )

    try:
        for intent_id, new_status in outcomes.items():
            if new_status == "pending":
                continue
            await db.execute(
                update(BillingCheckoutIntent)
                .where(
                    BillingCheckoutIntent.id == intent_id,
                    BillingCheckoutIntent.status == "pending",
                )
                .values(status=new_status)
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    if "pending" in outcomes.values():
        raise HTTPException(
            409,
            {"error": "checkout_pending", "message": "A checkout is still open. Finish it or try again shortly."},
        )


async def _ensure_stripe_customer(db, stripe, intent_id, owner_type, owner_id, email, user_id, org_id) -> None:
    """Create the owner's Stripe customer (idempotent per owner) outside any
    transaction, then attach it under the CURRENT owner's locks.

    The checkout was reserved (a billing_checkout_intents row) before this
    call, so an org created while Customer.create runs takes over that
    reservation; the customer is then attached to the reservation's CURRENT
    owner — re-resolved after the Stripe call — never to a stale snapshot.
    If the reservation moved again between the re-resolve and the lock, the
    lock order forbids taking the new owner's locks here, so BillingLockRetry
    restarts the checkout loop."""
    from sessionfs.server.db.models import BillingCheckoutIntent

    customer = await stripe.Customer.create_async(
        email=email,
        metadata={"org_id": owner_id, "user_id": user_id} if owner_type == "org" else {"user_id": user_id},
        idempotency_key=f"sfs-customer:{owner_type}:{owner_id}",
    )
    try:
        intent = (
            await db.execute(
                select(BillingCheckoutIntent).where(BillingCheckoutIntent.id == intent_id)
            )
        ).scalar_one_or_none()
        if intent is not None:
            owner_type, owner_id = intent.owner_type, intent.owner_id
        locked = await lock_billing_rows(
            db,
            user_ids=[owner_id] if owner_type == "user" else [],
            org_ids=[owner_id] if owner_type == "org" else [],
        )
        row = locked.get((owner_type, owner_id))
        if intent is not None:
            intent = (
                await db.execute(
                    select(BillingCheckoutIntent)
                    .where(BillingCheckoutIntent.id == intent_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if intent is not None and intent.status == "pending":
            if (intent.owner_type, intent.owner_id) != (owner_type, owner_id):
                raise BillingLockRetry(
                    f"checkout intent {intent_id} moved to {intent.owner_type}:{intent.owner_id}"
                )
            if row is not None and not row.stripe_customer_id:
                row.stripe_customer_id = customer.id
            intent.stripe_customer_id = (
                row.stripe_customer_id if row is not None else None
            ) or customer.id
        elif row is not None and not row.stripe_customer_id:
            row.stripe_customer_id = customer.id
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


_SWEEP_LIVE_SUBSCRIPTION_STATUSES = frozenset(
    {"active", "trialing", "past_due", "unpaid", "paused", "incomplete"}
)


def _list_data(result) -> list:
    data = _field(result, "data")
    return list(data) if data else []


async def _sweep_for_outstanding_stripe_state(db: AsyncSession, stripe, customer_id) -> None:
    """First-checkout guard for state created before checkout intents
    existed (or out of band): an open Checkout session or a live
    subscription on this customer that server state does not know blocks
    the new checkout and is recorded for reconciliation.

    Called between steps — no transaction, lock or pooled connection is
    held across the Stripe calls. A Stripe failure fails closed (503).
    """
    from sessionfs.server.db.models import (
        BillingCheckoutIntent,
        BillingReconciliation,
        Entitlement,
        Organization,
    )

    try:
        sessions = _list_data(
            await stripe.checkout.Session.list_async(customer=customer_id, status="open")
        )
        subs = _list_data(await stripe.Subscription.list_async(customer=customer_id))
    except Exception:
        logger.warning("Stripe listing failed during the checkout sweep", exc_info=True)
        raise HTTPException(
            503,
            {"error": "billing_unavailable", "message": "Could not confirm outstanding checkouts with Stripe. Try again shortly."},
        )
    open_sessions = [s for s in sessions if _field(s, "status") == "open"]
    live_subs = [s for s in subs if _field(s, "status") in _SWEEP_LIVE_SUBSCRIPTION_STATUSES]
    if not open_sessions and not live_subs:
        return

    sub_ids = [sid for sid in (_field(s, "id") for s in live_subs) if sid]
    session_ids = [sid for sid in (_field(s, "id") for s in open_sessions) if sid]
    try:
        known_subs: set = set()
        if sub_ids:
            known_subs |= set((
                await db.execute(
                    select(Entitlement.source_ref).where(
                        Entitlement.source == "stripe", Entitlement.source_ref.in_(sub_ids)
                    )
                )
            ).scalars())
            for model in (User, Organization):
                for column in ("stripe_subscription_id", "stripe_pending_subscription_id"):
                    known_subs |= set((
                        await db.execute(
                            select(getattr(model, column)).where(getattr(model, column).in_(sub_ids))
                        )
                    ).scalars())
        known_subs.discard(None)
        known_sessions: set = set()
        if session_ids:
            known_sessions |= set((
                await db.execute(
                    select(BillingCheckoutIntent.stripe_session_id).where(
                        BillingCheckoutIntent.stripe_session_id.in_(session_ids)
                    )
                )
            ).scalars())
        known_sessions.discard(None)

        unknown_sessions = [s for s in open_sessions if _field(s, "id") not in known_sessions]
        unknown_subs = [s for s in live_subs if _field(s, "id") not in known_subs]

        recorded = {
            (kind, sub)
            for kind, sub in (
                await db.execute(
                    select(
                        BillingReconciliation.kind,
                        BillingReconciliation.stripe_subscription_id,
                    ).where(
                        BillingReconciliation.status == "open",
                        BillingReconciliation.stripe_customer_id == customer_id,
                        BillingReconciliation.kind.in_(
                            ("untracked_checkout_session", "untracked_subscription")
                        ),
                    )
                )
            ).all()
        }
        if unknown_sessions and ("untracked_checkout_session", None) not in recorded:
            await _record_reconciliation(
                db, None, "untracked_checkout_session",
                customer_id=customer_id,
                details={
                    "session_ids": [_field(s, "id") for s in unknown_sessions],
                    "action": "checkout refused; finish or expire the open Stripe checkout session",
                },
            )
        for sub in unknown_subs:
            sub_id = _field(sub, "id")
            if ("untracked_subscription", sub_id) in recorded:
                continue
            await _record_reconciliation(
                db, None, "untracked_subscription",
                customer_id=customer_id, subscription_id=sub_id,
                details={
                    "status": _field(sub, "status"),
                    "action": "checkout refused; manage the existing subscription instead",
                },
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    if unknown_subs:
        raise HTTPException(
            409,
            {"error": "already_subscribed", "message": "Stripe already has a subscription for this account. Use the customer portal to manage it."},
        )
    if unknown_sessions:
        raise HTTPException(
            409,
            {"error": "checkout_pending", "message": "Stripe already has an open checkout session for this account. Finish it or wait for it to expire."},
        )


async def _open_checkout_session(db: AsyncSession, stripe, plan: _CheckoutPlan) -> CheckoutResponse:
    """Create the Stripe session for a committed pending intent, then record
    it. On failure the unopened intent is dropped (the idempotency key makes
    a retry return the same session if Stripe did create one)."""
    from sessionfs.server.db.models import BillingCheckoutIntent

    async def _drop() -> None:
        try:
            await db.execute(
                delete(BillingCheckoutIntent).where(
                    BillingCheckoutIntent.id == plan.intent_id,
                    BillingCheckoutIntent.stripe_session_id.is_(None),
                    BillingCheckoutIntent.status == "pending",
                )
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    try:
        session = await stripe.checkout.Session.create_async(**plan.stripe_params)
    except Exception:
        logger.warning("Stripe checkout session creation failed", exc_info=True)
        await _drop()
        raise HTTPException(
            502,
            {"error": "billing_unavailable", "message": "Stripe checkout is unavailable. Try again shortly."},
        )
    if not session.url:
        await _drop()
        raise HTTPException(500, "Stripe did not return a checkout URL")

    try:
        await db.execute(
            update(BillingCheckoutIntent)
            .where(BillingCheckoutIntent.id == plan.intent_id)
            .values(stripe_session_id=getattr(session, "id", None), checkout_url=session.url)
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return CheckoutResponse(checkout_url=session.url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    body: PortalRequest | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Customer Portal session for self-service management."""
    stripe = _get_stripe()
    scope = (body.scope if body else None) or "auto"

    org_cust = ctx.org.stripe_customer_id if ctx.is_org_user and ctx.org else None
    has_personal = bool(
        user.stripe_customer_id
        and user.stripe_subscription_id
        and user.stripe_customer_id != org_cust
    )

    customer_id = None
    if scope == "personal":
        # Explicit personal portal request
        if has_personal:
            customer_id = user.stripe_customer_id
        else:
            raise HTTPException(400, "No personal subscription found.")
    elif ctx.is_org_user and has_minimum_role(ctx.role or "", "admin"):
        if scope == "org" or not has_personal:
            # Admin: default to org portal
            customer_id = org_cust
        else:
            # Admin with personal sub, auto scope: route to org portal
            # (they can use scope=personal for personal)
            customer_id = org_cust
    elif ctx.is_org_user:
        # Non-admin org member
        if has_personal:
            customer_id = user.stripe_customer_id
        else:
            raise HTTPException(
                403,
                {"error": "admin_required", "message": "Only organization admins can manage the org subscription."},
            )
    elif user.stripe_customer_id:
        customer_id = user.stripe_customer_id

    if not customer_id:
        raise HTTPException(400, "No subscription found.")

    # No database transaction (or pooled connection) held across the call.
    await db.rollback()
    session = await stripe.billing_portal.Session.create_async(
        customer=customer_id,
        return_url=f"{os.environ.get('SFS_APP_URL', 'https://app.sessionfs.dev')}/settings/billing",
    )

    return PortalResponse(portal_url=session.url)


@router.get("/status", response_model=BillingStatusResponse)
async def billing_status(
    ctx: UserContext = Depends(get_user_context),
):
    """Get current subscription status."""
    user = ctx.user

    # Beta = Stripe not fully configured (need secret key + all three price IDs: starter, pro, team)
    stripe_key = os.environ.get("SFS_STRIPE_SECRET_KEY", "")
    stripe_configured = bool(
        stripe_key
        and TIER_PRICE_MAP.get("starter")
        and TIER_PRICE_MAP.get("pro")
        and TIER_PRICE_MAP.get("team")
    )

    # For org members, use org billing state but surface personal sub conflict
    if ctx.is_org_user and ctx.org:
        # A personal sub is one whose Stripe customer differs from the org's
        org_cust = ctx.org.stripe_customer_id
        has_personal = bool(
            user.stripe_subscription_id
            and user.stripe_customer_id
            and user.stripe_customer_id != org_cust
        )
        return BillingStatusResponse(
            tier=ctx.effective_tier.value,
            storage_used_bytes=ctx.org.storage_used_bytes or 0,
            storage_limit_bytes=ctx.org.storage_limit_bytes or get_storage_limit(ctx.effective_tier),
            stripe_customer_id=ctx.org.stripe_customer_id,
            has_subscription=ctx.org.stripe_subscription_id is not None,
            has_personal_subscription=has_personal,
            is_org_member=True,
            org_role=ctx.role,
            is_beta=not stripe_configured,
        )

    return BillingStatusResponse(
        tier=ctx.effective_tier.value,
        storage_used_bytes=user.storage_used_bytes or 0,
        storage_limit_bytes=get_storage_limit(ctx.effective_tier),
        stripe_customer_id=user.stripe_customer_id,
        has_subscription=user.stripe_subscription_id is not None,
        is_org_member=False,
        org_role=None,
        is_beta=not stripe_configured,
    )


# --- Stripe Webhook ---


STRIPE_WEBHOOK_SECRET = os.environ.get("SFS_STRIPE_WEBHOOK_SECRET", "")

webhook_router = APIRouter(tags=["webhooks"])


@webhook_router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Stripe webhook events."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(500, "Stripe webhook not configured")

    stripe = _get_stripe()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not sig:
        raise HTTPException(400, "Missing stripe-signature header")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Invalid signature")

    return await _process_stripe_event(event, db)


_APPLY_ATTEMPTS = 3


def _is_lock_race(exc: BaseException) -> bool:
    if isinstance(exc, (BillingLockRetry, _OwnershipChanged)):
        return True
    return isinstance(exc, DBAPIError) and db_sqlstate(exc) in _PG_RETRYABLE_SQLSTATES


async def _process_stripe_event(event, db: AsyncSession) -> dict:
    """Apply one verified Stripe event exactly once, in three phases.

    1. A cheap read of the StripeEvent marker; the read transaction is
       closed before any network I/O.
    2. The canonical Stripe state the event refers to (subscription and its
       product) is fetched with the async client while NO transaction, lock
       or pooled connection is held. A transient failure propagates (non-2xx,
       nothing committed) and Stripe retries.
    3. A short transaction claims the marker, takes the billing locks,
       re-validates the owner and applies the fetched state; marker and
       effects commit together. A lost lock race (customer changed, owner
       moved, deadlock) rolls back and the transaction restarts.

    Applying canonical state is idempotent; a fetch whose DB-issued
    generation is not newer than the state already applied to the binding
    (entitlements.stripe_synced_generation) is refused, so a slow delivery
    can't roll a newer state back — generations, not wall clocks, order
    fetches. A permanently
    unusable event is committed with outcome='dead_letter' plus a
    billing_reconciliations row instead of being reported as processed.
    """
    from sqlalchemy.exc import IntegrityError

    seen = (
        await db.execute(select(StripeEvent.event_id).where(StripeEvent.event_id == event.id))
    ).first()
    await db.rollback()
    if seen is not None:
        return {"status": "already_processed"}

    prefetch = _WEBHOOK_PREFETCH.get(event.type)
    fetched = await prefetch(db, event) if prefetch is not None else None

    for attempt in range(_APPLY_ATTEMPTS):
        try:
            db.add(StripeEvent(event_id=event.id, event_type=event.type))
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                return {"status": "already_processed"}
            handler = _WEBHOOK_HANDLERS.get(event.type)
            if handler:
                await handler(event, db, fetched)
            outcome = (
                await db.execute(
                    select(StripeEvent.outcome).where(StripeEvent.event_id == event.id)
                )
            ).scalar_one()
            await db.commit()
            return {"status": "ok" if outcome == "processed" else outcome}
        except BaseException as exc:
            await db.rollback()
            if _is_lock_race(exc) and attempt + 1 < _APPLY_ATTEMPTS:
                logger.info("Stripe event %s: lock race, restarting (%s)", event.id, exc)
                continue
            raise
    raise RuntimeError("unreachable")


# --- Reconciliation / dead letters ---


_RECONCILIATION_LOG_LEVEL = {
    "duplicate_subscription": logging.ERROR,
    "stale_subscription_event": logging.WARNING,
    "unroutable_subscription_event": logging.WARNING,
    "malformed_event": logging.ERROR,
    "plan_owner_mismatch": logging.ERROR,
    "subscription_binding_conflict": logging.ERROR,
    "ambiguous_subscription_pointer": logging.ERROR,
    "unconfirmed_subscription_adoption": logging.ERROR,
    "untracked_checkout_session": logging.WARNING,
    "untracked_subscription": logging.ERROR,
}


async def _record_reconciliation(
    db: AsyncSession,
    event,
    kind: str,
    *,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    owner_type: str | None = None,
    owner_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Write a durable billing_reconciliations row (once per event + kind)."""
    from sessionfs.server.db.models import BillingReconciliation

    event_id = getattr(event, "id", None)
    if event_id:
        seen = (
            await db.execute(
                select(BillingReconciliation.id).where(
                    BillingReconciliation.stripe_event_id == event_id,
                    BillingReconciliation.kind == kind,
                )
            )
        ).first()
        if seen is not None:
            return
    db.add(
        BillingReconciliation(
            id=f"brc_{secrets.token_hex(12)}",
            kind=kind,
            status="open",
            stripe_event_id=event_id,
            stripe_event_type=getattr(event, "type", None),
            stripe_customer_id=customer_id or None,
            stripe_subscription_id=subscription_id or None,
            owner_type=owner_type,
            owner_id=owner_id,
            details=json.dumps(details or {}, default=str),
        )
    )
    await db.flush()
    logger.log(
        _RECONCILIATION_LOG_LEVEL.get(kind, logging.ERROR),
        "Billing reconciliation needed (%s): event=%s subscription=%s customer=%s "
        "owner=%s:%s details=%s",
        kind, event_id, subscription_id, customer_id, owner_type, owner_id, details,
    )


async def _dead_letter(db: AsyncSession, event, reason: str, **fields) -> None:
    """Record a permanently unusable event: reconciliation row + marker
    outcome 'dead_letter' (when the event went through _process_stripe_event)."""
    extra = fields.pop("details", None) or {}
    await _record_reconciliation(
        db, event, "malformed_event", details={"reason": reason, **extra}, **fields
    )
    event_id = getattr(event, "id", None)
    if event_id:
        await db.execute(
            update(StripeEvent)
            .where(StripeEvent.event_id == event_id)
            .values(outcome="dead_letter")
        )


_PERMANENT_STRIPE_ERRORS = {"InvalidRequestError"}


def _is_permanent_stripe_error(exc: BaseException) -> bool:
    """True only for Stripe errors that a retry can never fix (e.g. a
    product that doesn't exist). Connection/rate-limit/API/auth errors and
    anything unrecognized are treated as transient and propagate."""
    return any(cls.__name__ in _PERMANENT_STRIPE_ERRORS for cls in type(exc).__mro__)


class _OwnershipChanged(Exception):
    """Ownership moved between the unlocked read and the locked re-read;
    the apply transaction rolls back and restarts."""


# --- Canonical Stripe state (fetched outside any transaction) ---
#
# Stripe does not deliver events in order: a deletion can arrive before the
# checkout that created the subscription, and an older update after a newer
# one. Events are therefore only triggers: the subscription's CURRENT state
# is fetched from Stripe (before any transaction opens) and applied. No
# per-subscription tombstone is kept — a deletion that arrived too early to
# route is harmless because the later checkout fetches the subscription,
# sees it canceled, and grants nothing.

# Statuses whose entitlement is (still) granted. past_due/unpaid/paused keep
# access during Stripe's retry window; only a terminal status revokes it.
_GRANTING_STATUSES = frozenset({"active", "trialing", "past_due", "unpaid", "paused"})
# Terminal: the subscription is over; nothing is (or stays) granted.
_TERMINAL_STATUSES = frozenset({"canceled", "incomplete_expired"})
# 'incomplete': created, first payment not yet made — recorded as the
# owner's pending claim (stripe_pending_subscription_id): it routes later
# events and blocks a second checkout but grants and revokes nothing.
_VALID_TIERS = ("free", "starter", "pro", "team", "enterprise")


@dataclass
class _CanonicalSubscription:
    id: str
    status: str
    customer_id: str | None
    quantity: int | None


@dataclass
class _Fetched:
    """Stripe's state for one subscription, fetched at ``fetched_at``.
    ``generation`` is a DB-issued monotonic id (stripe_fetch_generations)
    allocated BEFORE the fetch started — it, not the wall clock, orders
    fetches (webhooks fetch outside any transaction, so no two machines'
    clocks can be compared). Handcrafted fetches default to 0, older than
    every real generation, so they are never applied over recorded state.
    ``problem`` is set when the event is permanently unusable."""
    subscription_id: str
    fetched_at: datetime
    generation: int = 0
    canonical: _CanonicalSubscription | None = None
    tier: str | None = None
    problem: str | None = None
    details: dict | None = None


async def _next_fetch_generation(db: AsyncSession) -> int:
    """Allocate the next Stripe-fetch generation and return it.

    The row is committed immediately when no transaction is open (the normal
    webhook path: the generation must exist before the Stripe call starts
    without holding a pooled connection across it). Inside an already-open
    transaction (direct handler calls in tests) the insert flushes with the
    caller's transaction — still monotonic; a rollback just leaves a gap.
    """
    from sessionfs.server.db.models import StripeFetchGeneration

    fresh = not db.in_transaction()
    row = StripeFetchGeneration()
    db.add(row)
    if fresh:
        await db.commit()
    else:
        await db.flush()
    return row.id


async def _fetch_subscription_state(db: AsyncSession, subscription_id: str) -> _Fetched:
    """Fetch a subscription (and its product's tier) with the async Stripe
    client. Transient failures propagate; permanent ones are returned as
    ``problem`` so the apply phase can dead-letter the event.

    The fetch generation is allocated (and committed) BEFORE the fetch
    starts, so a slow fetch that started before a newer one can never
    overwrite the newer one's applied state.
    """
    generation = await _next_fetch_generation(db)
    fetched_at = datetime.now(timezone.utc)
    stripe = _get_stripe()
    try:
        sub = await stripe.Subscription.retrieve_async(subscription_id)
    except Exception as exc:
        if not _is_permanent_stripe_error(exc):
            raise
        return _Fetched(subscription_id, fetched_at, generation,
                        problem=f"subscription lookup failed permanently: {type(exc).__name__}")
    status = _field(sub, "status")
    items = _field(_field(sub, "items"), "data") or []
    first = items[0] if items else None
    customer = _field(sub, "customer")
    if customer is not None and not isinstance(customer, str):
        customer = _field(customer, "id")
    quantity = _field(first, "quantity")
    canonical = _CanonicalSubscription(
        id=subscription_id,
        status=status if isinstance(status, str) else "",
        customer_id=customer,
        quantity=quantity if isinstance(quantity, int) else None,
    )
    fetched = _Fetched(subscription_id, fetched_at, generation, canonical=canonical)
    if canonical.status not in _GRANTING_STATUSES:
        return fetched

    product = _field(_field(first, "price"), "product")
    if not product:
        fetched.problem = "subscription has no product"
        return fetched
    if isinstance(product, str):
        try:
            product = await stripe.Product.retrieve_async(product)
        except Exception as exc:
            if not _is_permanent_stripe_error(exc):
                raise
            fetched.problem = f"product lookup failed permanently: {type(exc).__name__}"
            return fetched
    tier = _field(_field(product, "metadata") or {}, "tier")
    if tier not in _VALID_TIERS:
        fetched.problem = "product has no valid tier metadata"
        fetched.details = {"tier": tier}
        return fetched
    fetched.tier = tier
    return fetched


async def _prefetch_checkout(db: AsyncSession, event) -> _Fetched | None:
    subscription_id = getattr(event.data.object, "subscription", None)
    return await _fetch_subscription_state(db, subscription_id) if subscription_id else None


async def _prefetch_subscription_event(db: AsyncSession, event) -> _Fetched | None:
    return await _fetch_subscription_state(db, event.data.object.id)


# --- Ownership resolution ---


async def _load_owner(db: AsyncSession, owner_type: str, owner_id: str):
    from sessionfs.server.db.models import Organization

    if owner_type == "org":
        return (
            await db.execute(select(Organization).where(Organization.id == owner_id))
        ).scalar_one_or_none()
    return (
        await db.execute(select(User).where(User.id == owner_id))
    ).scalar_one_or_none()


async def _active_entitlement(db: AsyncSession, owner_type: str, owner_id: str):
    from sessionfs.server.db.models import Entitlement

    return (
        await db.execute(
            select(Entitlement).where(
                Entitlement.owner_type == owner_type,
                Entitlement.owner_id == owner_id,
                Entitlement.status == "active",
            )
        )
    ).scalar_one_or_none()


async def _subscription_binding(
    subscription_id: str | None, db: AsyncSession, *, lock: bool = False
):
    """The entitlement row bound to (stripe, subscription_id), if any.

    uq_entitlements_source_ref allows one row per binding, so this row —
    whatever its status — is the authoritative owner of the subscription.
    """
    if not subscription_id:
        return None
    from sessionfs.server.db.models import Entitlement

    stmt = select(Entitlement).where(
        Entitlement.source == "stripe",
        Entitlement.source_ref == subscription_id,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _bound_elsewhere(db: AsyncSession, owner_type: str, owner_id: str, subscription_id: str):
    """The owner's active entitlement if it is bound to a DIFFERENT Stripe
    subscription (so this one must not rebind it)."""
    active = await _active_entitlement(db, owner_type, owner_id)
    if (
        active is not None
        and active.source == "stripe"
        and active.source_ref
        and active.source_ref != subscription_id
    ):
        return active
    return None


async def _subscription_claimants(db: AsyncSession, subscription_id: str) -> list[tuple[str, str]]:
    """Every current AND pending claimant of a subscription id, from both
    owner tables — never a LIMIT-1 'first' owner, so an ambiguous pointer
    (migration 075 deliberately leaves those unbound) can never be granted
    to whichever claimant happens to sort first."""
    from sqlalchemy import or_

    from sessionfs.server.db.models import Organization

    org_ids = (
        await db.execute(
            select(Organization.id).where(
                or_(
                    Organization.stripe_subscription_id == subscription_id,
                    Organization.stripe_pending_subscription_id == subscription_id,
                )
            )
        )
    ).scalars().all()
    user_ids = (
        await db.execute(
            select(User.id).where(
                or_(
                    User.stripe_subscription_id == subscription_id,
                    User.stripe_pending_subscription_id == subscription_id,
                )
            )
        )
    ).scalars().all()
    return sorted({("org", i) for i in org_ids} | {("user", i) for i in user_ids})


async def _legacy_org_claimant(db: AsyncSession, claimants: list[tuple[str, str]]):
    """The one provable multi-claim shape — the same one migration 075's
    backfill recognizes: exactly one org claimant, and every other claimant
    is a user who is a member of that org AND carries the org's Stripe
    customer. Returns the org claim, else None."""
    from sessionfs.server.db.models import Organization, OrgMember

    orgs = [c for c in claimants if c[0] == "org"]
    if len(orgs) != 1:
        return None
    org = (
        await db.execute(select(Organization).where(Organization.id == orgs[0][1]))
    ).scalar_one_or_none()
    if org is None or not org.stripe_customer_id:
        return None
    others = [c for c in claimants if c != orgs[0]]
    if not others:
        return orgs[0]
    if any(kind != "user" for kind, _ in others):
        return None
    other_ids = [c[1] for c in others]
    customers = dict(
        (
            await db.execute(select(User.id, User.stripe_customer_id).where(User.id.in_(other_ids)))
        ).all()
    )
    members = set(
        (
            await db.execute(
                select(OrgMember.user_id).where(
                    OrgMember.org_id == org.id,
                    OrgMember.user_id.in_(other_ids),
                )
            )
        ).scalars().all()
    )
    for claimant_id in other_ids:
        if customers.get(claimant_id) != org.stripe_customer_id:
            return None
        if claimant_id not in members:
            return None
    return orgs[0]


async def _resolve_subscription_event_owner(db: AsyncSession, subscription_id: str, *, lock: bool):
    """Owner of a customer.subscription.* event:
    (kind, owner, binding, problem, claimants).

    Only an exact match counts: the (stripe, subscription_id) entitlement
    binding, whose owner's current subscription pointer (if set) must equal
    the event's id; otherwise the owners whose subscription pointer or
    pending (incomplete) claim equals it. Customer ids never route these
    events. Every claimant is collected (current and pending, both tables)
    and returned so the caller can lock them all; more than one claimant is
    'ambiguous' — nothing is applied — except the provable legacy
    org-plus-member duplicate, where the org wins.
    problem: None | 'stale' | 'unroutable' | 'ambiguous'.
    """
    binding = await _subscription_binding(subscription_id, db, lock=lock)
    claimants = await _subscription_claimants(db, subscription_id)
    if binding is not None:
        owner = await _load_owner(db, binding.owner_type, binding.owner_id)
        if owner is None:
            return binding.owner_type, None, binding, "unroutable", claimants
        pointer = owner.stripe_subscription_id
        if pointer and pointer != subscription_id:
            return binding.owner_type, owner, binding, "stale", claimants
        return binding.owner_type, owner, binding, None, claimants

    if not claimants:
        return None, None, None, "unroutable", claimants
    if len(claimants) > 1:
        winner = await _legacy_org_claimant(db, claimants)
        if winner is None:
            return None, None, None, "ambiguous", claimants
    else:
        winner = claimants[0]
    kind, owner_id = winner
    owner = await _load_owner(db, kind, owner_id)
    if owner is None:
        return None, None, None, "unroutable", claimants
    if await _bound_elsewhere(db, kind, owner.id, subscription_id) is not None:
        return kind, owner, None, "stale", claimants
    return kind, owner, None, None, claimants


async def _locked_subscription_event_owner(db: AsyncSession, subscription_id: str, customers):
    """Resolve, lock EVERY claimant in the universal order, then re-resolve
    and require the same answer — same owner, same problem, same claimant
    set (else _OwnershipChanged restarts the transaction)."""
    kind, owner, _b, problem, claimants = await _resolve_subscription_event_owner(
        db, subscription_id, lock=False
    )
    user_ids = {cid for k, cid in claimants if k == "user"}
    org_ids = {cid for k, cid in claimants if k == "org"}
    if kind is not None and owner is not None:
        (user_ids if kind == "user" else org_ids).add(owner.id)
    # Even with no owner (unroutable/ambiguous), take the locks: the
    # observation (or reconciliation row) recorded here must serialize with
    # checkout and org creation for the same owners and customers.
    await lock_billing_rows(
        db, user_ids=sorted(user_ids), org_ids=sorted(org_ids), extra_customers=customers
    )
    result = await _resolve_subscription_event_owner(db, subscription_id, lock=True)
    if (result[0], getattr(result[1], "id", None), result[3], result[4]) != (
        kind,
        getattr(owner, "id", None),
        problem,
        claimants,
    ):
        raise _OwnershipChanged(subscription_id)
    return result


# --- Applying canonical state ---


async def _clear_legacy_user_pointers(db: AsyncSession, subscription_id: str) -> None:
    """Users still carrying an org's subscription id (legacy duplication).
    Their rows were locked before the org row (universal lock order)."""
    await db.execute(
        update(User)
        .where(User.stripe_subscription_id == subscription_id)
        .values(stripe_subscription_id=None, stripe_customer_id=None)
    )


async def _grant(db: AsyncSession, kind: str, owner, fetched: _Fetched, customer_id: str | None) -> None:
    canonical = fetched.canonical
    assert canonical is not None and fetched.tier is not None
    subscription_id, tier, quantity = canonical.id, fetched.tier, canonical.quantity
    if owner.stripe_pending_subscription_id == subscription_id:
        owner.stripe_pending_subscription_id = None
    if kind == "org":
        org = owner
        effective_tier = tier if tier in ("team", "enterprise", "free") else "free"
        org.tier = effective_tier
        org.stripe_subscription_id = subscription_id
        org.stripe_customer_id = org.stripe_customer_id or customer_id
        if effective_tier == "team":
            seats = quantity if quantity and quantity > 0 else (org.seats_limit or 5)
            org.seats_limit = seats
            org.storage_limit_bytes = seats * 1024 * 1024 * 1024
        elif effective_tier == "enterprise":
            org.storage_limit_bytes = 0  # unlimited
            if quantity and quantity > 0:
                org.seats_limit = quantity
            elif not org.seats_limit:
                org.seats_limit = 25
        await _clear_legacy_user_pointers(db, subscription_id)
        ent = await apply_entitlement(
            "org",
            org.id,
            tier=effective_tier,
            seats=org.seats_limit,
            storage=org.storage_limit_bytes,
            source="stripe",
            source_ref=subscription_id,
            db=db,
        )
    else:
        owner.tier = tier
        owner.stripe_subscription_id = subscription_id
        owner.tier_updated_at = datetime.now(timezone.utc)
        ent = await apply_entitlement(
            "user",
            owner.id,
            tier=tier,
            source="stripe",
            source_ref=subscription_id,
            db=db,
        )
    ent.stripe_synced_generation = fetched.generation


async def _revoke(db: AsyncSession, kind: str, owner, binding, fetched: _Fetched) -> None:
    """The subscription ended. Access is revoked ONLY through an active,
    exact Stripe binding owned by this owner. A pending claim or a bare
    subscription pointer for it is cleared without touching access (it may
    sit next to an unrelated manual or admin-provisioned entitlement)."""
    subscription_id = fetched.subscription_id
    if owner.stripe_pending_subscription_id == subscription_id:
        owner.stripe_pending_subscription_id = None
    holds = (
        binding is not None
        and binding.status == "active"
        and binding.owner_type == kind
        and binding.owner_id == owner.id
    )
    if not holds:
        if owner.stripe_subscription_id == subscription_id:
            owner.stripe_subscription_id = None
        return
    if kind == "org":
        org = owner
        org.tier = "free"
        if org.stripe_subscription_id == subscription_id:
            org.stripe_subscription_id = None
        org.seats_limit = 0
        org.storage_limit_bytes = 0
        await _clear_legacy_user_pointers(db, subscription_id)
        ent = await apply_entitlement(
            "org", org.id, tier="free", seats=0, storage=0,
            source="stripe", source_ref=subscription_id, status="canceled", db=db,
        )
    else:
        owner.tier = "free"
        if owner.stripe_subscription_id == subscription_id:
            owner.stripe_subscription_id = None
        owner.tier_updated_at = datetime.now(timezone.utc)
        ent = await apply_entitlement(
            "user", owner.id, tier="free",
            source="stripe", source_ref=subscription_id, status="canceled", db=db,
        )
    ent.stripe_synced_generation = fetched.generation


async def _observe(db: AsyncSession, fetched: _Fetched | None):
    """Record a fetched canonical state for its subscription, independent of
    ownership, and return the previous (generation, terminal_generation).

    Called under the Stripe customer lock(s) for every fetched state a
    webhook handles — applied, stale or unroutable — so a terminal state
    seen before anyone owned the subscription still governs later events.
    Ordering is by DB-issued fetch generation, not wall clock: a fetch that
    STARTED later always wins, wherever it ran. A concurrent first insert
    for the same id restarts the transaction.
    """
    from sqlalchemy.exc import IntegrityError

    from sessionfs.server.db.models import StripeSubscriptionObservation as Obs

    if fetched is None or fetched.canonical is None:
        return None, None
    canonical = fetched.canonical
    terminal = canonical.status in _TERMINAL_STATUSES
    row = (
        await db.execute(
            select(Obs)
            .where(Obs.subscription_id == canonical.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            Obs(
                subscription_id=canonical.id,
                status=canonical.status or "unknown",
                customer_id=canonical.customer_id,
                observed_at=fetched.fetched_at,
                terminal_at=fetched.fetched_at if terminal else None,
                generation=fetched.generation,
                terminal_generation=fetched.generation if terminal else None,
            )
        )
        try:
            await db.flush()
        except IntegrityError:
            raise _OwnershipChanged(canonical.id)
        return None, None
    prev = (row.generation, row.terminal_generation)
    if prev[0] is None or fetched.generation > prev[0]:
        row.status = canonical.status or row.status
        row.observed_at = fetched.fetched_at
        row.customer_id = canonical.customer_id or row.customer_id
        row.generation = fetched.generation
    if terminal and (prev[1] is None or fetched.generation > prev[1]):
        row.terminal_at = fetched.fetched_at
        row.terminal_generation = fetched.generation
    row.updated_at = datetime.now(timezone.utc)
    return prev


async def _apply_canonical(db: AsyncSession, event, kind: str, owner, binding,
                           fetched: _Fetched, customer_id: str | None = None,
                           prev=(None, None)) -> str:
    """Make the owner's state match the fetched Stripe state.

    ``prev`` is the subscription's previous observation (_observe), as
    (generation, terminal_generation). A fetch that started before the state
    already applied to the binding, or before the latest terminal
    observation, is refused — whoever owns the subscription, and regardless
    of any wall clock. Returns 'applied' | 'stale' | 'dead_letter' |
    'reconciled'."""
    if fetched.problem:
        await _dead_letter(db, event, fetched.problem,
                           subscription_id=fetched.subscription_id, customer_id=customer_id,
                           details=fetched.details or {})
        return "dead_letter"
    canonical = fetched.canonical
    assert canonical is not None
    synced_gen = binding.stripe_synced_generation if binding is not None else None
    if synced_gen is not None and fetched.generation <= synced_gen:
        logger.info(
            "Subscription %s: fetch generation %s is not newer than the applied generation %s; "
            "skipped", canonical.id, fetched.generation, synced_gen,
        )
        return "stale"

    status = canonical.status
    prev_terminal_gen = prev[1] if prev else None
    if (
        status not in _TERMINAL_STATUSES
        and prev_terminal_gen is not None
        and fetched.generation <= prev_terminal_gen
    ):
        logger.warning(
            "Subscription %s: %s state (fetch generation %s) predates its terminal "
            "observation (generation %s); nothing granted",
            canonical.id, status, fetched.generation, prev_terminal_gen,
        )
        return "stale"
    if status in _TERMINAL_STATUSES:
        await _revoke(db, kind, owner, binding, fetched)
    elif status == "incomplete":
        if owner.stripe_subscription_id != canonical.id and (
            owner.stripe_pending_subscription_id in (None, canonical.id)
        ):
            owner.stripe_pending_subscription_id = canonical.id
            if kind == "org" and not owner.stripe_customer_id:
                owner.stripe_customer_id = canonical.customer_id or customer_id
        logger.info("Subscription %s is incomplete — pending claim recorded, nothing granted",
                    canonical.id)
    elif status in _GRANTING_STATUSES:
        if kind == "org" and fetched.tier not in ("team", "enterprise"):
            # A personal plan (Starter/Pro) can't fund an org. When this org
            # holds the exact active Stripe binding, match the entitlement
            # to what Stripe actually sells it — keeping the binding (and
            # the subscription pointer) so a later move back to a team plan
            # re-grants — and record the mismatch. Without an exact binding
            # the entitlement is unrelated to Stripe: leave it alone and
            # only record the mismatch.
            exact = (
                binding is not None
                and binding.status == "active"
                and binding.owner_type == "org"
                and binding.owner_id == owner.id
            )
            if exact:
                ent = await apply_entitlement(
                    "org", owner.id, tier="free", seats=0, storage=0,
                    source="stripe", source_ref=canonical.id, db=db,
                )
                ent.stripe_synced_generation = fetched.generation
            await _record_reconciliation(
                db, event, "plan_owner_mismatch",
                customer_id=canonical.customer_id or customer_id, subscription_id=canonical.id,
                owner_type=kind, owner_id=owner.id,
                details={"tier": fetched.tier, "status": status,
                         "action": "org entitlement downgraded to match Stripe"
                         if exact else "not applied to the org; review the subscription"},
            )
            return "reconciled"
        await _grant(db, kind, owner, fetched, canonical.customer_id or customer_id)
    else:
        await _dead_letter(db, event, f"subscription has unknown status {status!r}",
                           subscription_id=canonical.id, customer_id=customer_id)
        return "dead_letter"
    await db.flush()
    return "applied"


# --- checkout.session.completed ---


async def _resolve_checkout_target(
    db: AsyncSession, subscription_id: str, customer_id: str | None,
    metadata_user_id: str, metadata_org_id: str | None, *, lock: bool,
):
    """Owner a completed checkout belongs to:
    (kind, owner_id, binding, problem, claimants).

    Server state first, metadata last: the (stripe, subscription_id)
    binding; then EVERY current AND pending claimant (both owner tables) —
    a single claimant wins, more than one is 'ambiguous' except the provable
    legacy org-plus-member duplicate where the org wins; then an org
    carrying an adopted customer (no user still holds it — e.g. a personal
    checkout still pending when its user created an org); then the session
    metadata. problem is None or 'ambiguous' (nothing is granted, the intent
    is left unresolved) so a duplicated pointer can never be handed to
    whichever claimant sorts first, and a later grant can't clear an
    unrelated user's pointer.
    """
    from sessionfs.server.db.models import Organization

    binding = await _subscription_binding(subscription_id, db, lock=lock)
    claimants = await _subscription_claimants(db, subscription_id)
    if binding is not None:
        return binding.owner_type, binding.owner_id, binding, None, claimants

    if claimants:
        if len(claimants) > 1:
            winner = await _legacy_org_claimant(db, claimants)
            if winner is None:
                return None, None, None, "ambiguous", claimants
        else:
            winner = claimants[0]
        return winner[0], winner[1], None, None, claimants

    if customer_id:
        org_id = (
            await db.execute(
                select(Organization.id).where(Organization.stripe_customer_id == customer_id)
            )
        ).scalar_one_or_none()
        user_holder = (
            await db.execute(
                select(User.id).where(User.stripe_customer_id == customer_id).limit(1)
            )
        ).scalar_one_or_none()
        if org_id is not None and user_holder is None:
            return "org", org_id, None, None, claimants
    if metadata_org_id:
        return "org", metadata_org_id, None, None, claimants
    return "user", metadata_user_id, None, None, claimants


async def _locked_checkout_target(
    db: AsyncSession, subscription_id: str, customer_id: str | None,
    metadata_user_id: str, metadata_org_id: str | None, customers,
):
    """Resolve the checkout's owner, lock EVERY claimant in the universal
    order, then re-resolve and require the same answer (kind, owner, problem
    and claimant set) — else _OwnershipChanged restarts the transaction."""
    kind, owner_id, _b, problem, claimants = await _resolve_checkout_target(
        db, subscription_id, customer_id, metadata_user_id, metadata_org_id, lock=False
    )
    user_ids = {cid for k, cid in claimants if k == "user"}
    org_ids = {cid for k, cid in claimants if k == "org"}
    if kind is not None and owner_id is not None:
        (user_ids if kind == "user" else org_ids).add(owner_id)
    await lock_billing_rows(
        db, user_ids=sorted(user_ids), org_ids=sorted(org_ids), extra_customers=customers
    )
    result = await _resolve_checkout_target(
        db, subscription_id, customer_id, metadata_user_id, metadata_org_id, lock=True
    )
    if (result[0], result[1], result[3], result[4]) != (kind, owner_id, problem, claimants):
        raise _OwnershipChanged(subscription_id)
    return result


async def _set_checkout_intent(db: AsyncSession, session, subscription_id, status: str) -> None:
    """Close the checkout's intent: 'completed' when its outcome was applied,
    'unresolved' when it needs ops (dead letter, duplicate) — which keeps
    blocking new checkouts for that owner and customer."""
    from sqlalchemy import or_

    from sessionfs.server.db.models import BillingCheckoutIntent

    metadata = session.metadata or {}
    session_id = getattr(session, "id", None)
    intent_id = metadata.get("checkout_intent_id")
    conds = []
    if isinstance(session_id, str) and session_id:
        conds.append(BillingCheckoutIntent.stripe_session_id == session_id)
    if intent_id:
        conds.append(BillingCheckoutIntent.id == intent_id)
    if not conds:
        return
    await db.execute(
        update(BillingCheckoutIntent)
        .where(or_(*conds), BillingCheckoutIntent.status.in_(("pending", "expired", "unresolved")))
        .values(
            status=status,
            stripe_subscription_id=subscription_id,
            completed_at=datetime.now(timezone.utc) if status == "completed" else None,
        )
    )


async def _handle_checkout_completed(event, db: AsyncSession, fetched: _Fetched | None = None) -> None:
    """New subscription created via Checkout.

    The session metadata (user_id / org_id) records who STARTED the
    checkout, which can be stale by the time the event lands: the user may
    have created an org meanwhile (moving their customer, entitlement and
    open checkout to it), or the event may be a replay. So the current
    owner is resolved from server state first, under the universal billing
    lock order, and re-resolved after the locks are held. The event is only
    a trigger: the subscription's CURRENT state (fetched before this
    transaction) is applied, so a subscription canceled or expired before
    this event arrived grants nothing.

    A (stripe, subscription_id) binding is never created twice and an
    owner's existing (or pending) subscription is never rebound: a second
    subscription for an owner that already has one is recorded for
    reconciliation (not canceled in Stripe) and its checkout intent is left
    'unresolved'. Effects are flushed, not committed: the webhook commits
    them together with the StripeEvent idempotency marker.
    """
    session = event.data.object
    metadata = session.metadata or {}
    user_id = metadata.get("user_id")
    tier = metadata.get("tier")
    subscription_id = session.subscription
    customer_id = session.get("customer", "") or None
    metadata_org_id = metadata.get("org_id")

    if not user_id or not tier or not subscription_id:
        await _dead_letter(
            db, event, "checkout session missing user_id, tier or subscription",
            customer_id=customer_id, subscription_id=subscription_id,
        )
        await _set_checkout_intent(db, session, subscription_id, "unresolved")
        return
    if fetched is None:  # called directly (tests); the webhook prefetches
        fetched = await _fetch_subscription_state(db, subscription_id)

    customers = [customer_id, fetched.canonical.customer_id if fetched.canonical else None]
    kind, owner_id, binding, problem, claimants = await _locked_checkout_target(
        db, subscription_id, customer_id, user_id, metadata_org_id, customers
    )
    prev = await _observe(db, fetched)

    if problem == "ambiguous":
        await _record_reconciliation(
            db, event, "ambiguous_subscription_pointer",
            customer_id=customer_id, subscription_id=subscription_id,
            details={
                "reason": "more than one owner claims this subscription at checkout",
                "claimants": [f"{k}:{i}" for k, i in claimants],
                "metadata_user_id": user_id,
                "metadata_org_id": metadata_org_id,
                "action": "nothing applied; assign the subscription manually",
            },
        )
        await _set_checkout_intent(db, session, subscription_id, "unresolved")
        return

    if binding is not None and binding.status != "active":
        # The subscription already ended; a late or replayed checkout must
        # not resurrect it (or insert a second binding).
        logger.warning(
            "Checkout for subscription %s ignored: its entitlement is %s",
            subscription_id, binding.status,
        )
        await _set_checkout_intent(db, session, subscription_id, "completed")
        return

    owner = await _load_owner(db, kind, owner_id)
    if owner is None:
        await _record_reconciliation(
            db, event, "unroutable_subscription_event",
            customer_id=customer_id, subscription_id=subscription_id,
            owner_type=kind, owner_id=owner_id,
            details={"reason": "checkout owner not found"},
        )
        await _set_checkout_intent(db, session, subscription_id, "unresolved")
        return

    terminal = fetched.canonical is not None and fetched.canonical.status in _TERMINAL_STATUSES
    if binding is None and not terminal and not fetched.problem:
        # A new subscription for this owner: never replace the one it has.
        current = owner.stripe_subscription_id
        pending = owner.stripe_pending_subscription_id
        other = await _bound_elsewhere(db, kind, owner_id, subscription_id)
        existing = (
            (current if current and current != subscription_id else None)
            or (pending if pending and pending != subscription_id else None)
            or (other.source_ref if other is not None else None)
        )
        if existing:
            await _record_reconciliation(
                db, event, "duplicate_subscription",
                customer_id=customer_id, subscription_id=subscription_id,
                owner_type=kind, owner_id=owner_id,
                details={
                    "existing_subscription_id": existing,
                    "metadata_user_id": user_id,
                    "metadata_org_id": metadata_org_id,
                    "action": "not applied; the duplicate subscription was NOT canceled in Stripe",
                },
            )
            await _set_checkout_intent(db, session, subscription_id, "unresolved")
            return

    if kind == "org" and metadata_org_id != owner_id:
        logger.warning(
            "Checkout for subscription %s (metadata user=%s org=%s) routed to "
            "its current owner org %s",
            subscription_id, user_id, metadata_org_id, owner_id,
        )
    result = await _apply_canonical(db, event, kind, owner, binding, fetched, customer_id, prev)
    await _set_checkout_intent(
        db, session, subscription_id,
        "unresolved" if result in ("dead_letter", "reconciled") else "completed",
    )


async def _find_user_or_org_by_customer(
    customer_id: str, db: AsyncSession, subscription_id: str | None = None,
):
    """Find User and/or Org by stripe_customer_id.

    Uses subscription_id to disambiguate when the same customer has both
    a personal and an org subscription (legacy same-customer data). An
    unknown subscription on a shared customer resolves to nobody — it is
    never defaulted to the org. Webhooks no longer route by customer (see
    _resolve_subscription_event_owner); this remains for lookups.
    """
    from sessionfs.server.db.models import Organization

    # Check org
    org_result = await db.execute(
        select(Organization).where(Organization.stripe_customer_id == customer_id)
    )
    org = org_result.scalar_one_or_none()

    # Check user
    user_result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = user_result.scalar_one_or_none()

    if org and user and subscription_id:
        # Same customer on both — use subscription_id to disambiguate.
        if org.stripe_subscription_id == subscription_id:
            return None, org
        if user.stripe_subscription_id == subscription_id:
            return user, None
        return None, None

    if org:
        return None, org

    return user, None


# --- customer.subscription.updated / .deleted ---


async def _handle_subscription_event(event, db: AsyncSession, label: str,
                                     fetched: _Fetched | None) -> None:
    """The event is only a trigger. The owner is resolved through the exact
    binding (or pointer / pending claim) under the universal lock order and
    the subscription's CURRENT state — fetched before this transaction — is
    applied, so an older event arriving after a newer one can't roll the
    owner back and a deletion is applied whenever Stripe says the
    subscription is over. Stale and unroutable rows are recorded for ops
    visibility only; correctness doesn't depend on them."""
    subscription = event.data.object
    customer_id = getattr(subscription, "customer", None)
    if customer_id is not None and not isinstance(customer_id, str):
        customer_id = _field(customer_id, "id")
    if fetched is None:  # called directly (tests); the webhook prefetches
        fetched = await _fetch_subscription_state(db, subscription.id)

    customers = [customer_id, fetched.canonical.customer_id if fetched.canonical else None]
    kind, owner, binding, problem, claimants = await _locked_subscription_event_owner(
        db, subscription.id, customers
    )
    prev = await _observe(db, fetched)
    if problem == "ambiguous":
        await _record_reconciliation(
            db, event, "ambiguous_subscription_pointer",
            customer_id=customer_id, subscription_id=subscription.id,
            details={
                "reason": f"more than one owner claims this subscription ({label})",
                "claimants": [f"{k}:{i}" for k, i in claimants],
                "action": "nothing applied; assign the subscription manually",
            },
        )
        return
    if problem == "unroutable":
        await _record_reconciliation(
            db, event, "unroutable_subscription_event",
            customer_id=customer_id, subscription_id=subscription.id,
            owner_type=kind, owner_id=getattr(owner, "id", None) or (binding.owner_id if binding else None),
            details={"reason": f"no owner holds this subscription ({label})"},
        )
        return
    if problem == "stale":
        await _record_reconciliation(
            db, event, "stale_subscription_event",
            customer_id=customer_id, subscription_id=subscription.id,
            owner_type=kind, owner_id=owner.id,
            details={
                "reason": f"{label} for a subscription that is not the owner's current one",
                "current_subscription_id": owner.stripe_subscription_id,
            },
        )
        return
    if binding is not None and binding.status != "active":
        # A finished subscription can't come back, and a repeat delete has
        # nothing to do; never write a second binding for it.
        logger.info(
            "Subscription %s %s ignored: its entitlement is %s",
            subscription.id, label, binding.status,
        )
        return
    await _apply_canonical(db, event, kind, owner, binding, fetched, customer_id, prev)


async def _handle_subscription_updated(event, db: AsyncSession, fetched: _Fetched | None = None) -> None:
    """Subscription changed (upgrade, downgrade, renewal, dunning)."""
    await _handle_subscription_event(event, db, "update", fetched)


async def _handle_subscription_deleted(event, db: AsyncSession, fetched: _Fetched | None = None) -> None:
    """Subscription ended — applied through its exact binding."""
    await _handle_subscription_event(event, db, "delete", fetched)


async def _handle_payment_failed(event, db: AsyncSession, fetched=None) -> None:
    """Payment failed — log for now (grace period handled by Stripe retry)."""
    invoice = event.data.object
    customer_id = invoice.customer
    logger.warning("Payment failed for Stripe customer: %s", customer_id)


_WEBHOOK_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_payment_failed,
}

# Stripe reads done BEFORE the apply transaction (no locks, no connection).
_WEBHOOK_PREFETCH = {
    "checkout.session.completed": _prefetch_checkout,
    "customer.subscription.updated": _prefetch_subscription_event,
    "customer.subscription.deleted": _prefetch_subscription_event,
}
