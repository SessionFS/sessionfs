"""Billing routes — Stripe Checkout, Customer Portal, subscription status."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import StripeEvent, User
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


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    data: CheckoutRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout session for subscription."""
    stripe = _get_stripe()
    user = ctx.user

    if data.tier not in TIER_PRICE_MAP:
        raise HTTPException(400, f"Invalid tier: {data.tier}")

    price_id = TIER_PRICE_MAP[data.tier]
    if not price_id:
        raise HTTPException(400, f"Stripe price not configured for tier: {data.tier}")

    # Org billing restrictions
    if ctx.is_org_user and ctx.org:
        # Org members can't buy personal plans
        if data.tier in ("starter", "pro"):
            raise HTTPException(
                400,
                {"error": "org_member_restriction", "message": "Organization members cannot purchase individual plans. Your tier is managed by the organization admin."},
            )
        # Only org admins can change org subscription (team/enterprise)
        if data.tier in ("team",) and ctx.role != "admin":
            raise HTTPException(
                403,
                {"error": "admin_required", "message": "Only organization admins can change the subscription."},
            )

    # Prevent duplicate subscriptions — check both user and org.
    # For org admins, a personal sub doesn't block org checkout (different Stripe customer).
    org_cust = ctx.org.stripe_customer_id if ctx.is_org_user and ctx.org else None
    user_has_personal_sub = bool(
        user.stripe_subscription_id
        and user.stripe_customer_id
        and user.stripe_customer_id != org_cust
    )
    if user.stripe_subscription_id and not (ctx.is_org_user and user_has_personal_sub):
        raise HTTPException(
            409,
            {"error": "already_subscribed", "message": "You already have an active subscription. Use the customer portal to manage it."},
        )
    if ctx.is_org_user and ctx.org and ctx.org.stripe_subscription_id:
        raise HTTPException(
            409,
            {"error": "already_subscribed", "message": "Your organization already has an active subscription. Use the customer portal to manage it."},
        )

    # Get or create Stripe customer.
    # For org Team/Enterprise checkout, use the org's own Stripe customer
    # (separate from any personal customer) to maintain billing isolation.
    is_org_checkout = ctx.is_org_user and ctx.org and data.tier in ("team", "enterprise")

    if is_org_checkout:
        if ctx.org.stripe_customer_id:
            customer_id = ctx.org.stripe_customer_id
        else:
            customer = stripe.Customer.create(
                email=user.email,
                metadata={"org_id": ctx.org.id, "user_id": user.id},
            )
            ctx.org.stripe_customer_id = customer.id
            await db.commit()
            customer_id = customer.id
    elif user.stripe_customer_id:
        customer_id = user.stripe_customer_id
    else:
        customer = stripe.Customer.create(
            email=user.email,
            metadata={"user_id": user.id},
        )
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(stripe_customer_id=customer.id)
        )
        await db.commit()
        customer_id = customer.id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price": price_id,
            "quantity": data.seats if data.tier == "team" else 1,
        }],
        mode="subscription",
        success_url=f"{os.environ.get('SFS_APP_URL', 'https://app.sessionfs.dev')}/settings/billing?success=true",
        cancel_url=f"{os.environ.get('SFS_APP_URL', 'https://app.sessionfs.dev')}/settings/billing?cancelled=true",
        metadata={
            "user_id": user.id,
            "tier": data.tier,
            "seats": str(data.seats),
            **({"org_id": ctx.org.id} if is_org_checkout else {}),
        },
    )

    if not session.url:
        raise HTTPException(500, "Stripe did not return a checkout URL")

    return CheckoutResponse(checkout_url=session.url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    body: PortalRequest | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
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
    elif ctx.is_org_user and ctx.role == "admin":
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

    session = stripe.billing_portal.Session.create(
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

    # Idempotency check
    existing = await db.execute(
        select(StripeEvent).where(StripeEvent.event_id == event.id)
    )
    if existing.scalar_one_or_none():
        return {"status": "already_processed"}

    handler = _WEBHOOK_HANDLERS.get(event.type)
    if handler:
        await handler(event, db)

    # Record as processed
    db.add(StripeEvent(event_id=event.id, event_type=event.type))
    await db.commit()

    return {"status": "ok"}


async def _sync_billing_to_org(
    user_id: str, tier: str, subscription_id: str | None, db: AsyncSession,
    seats: int | None = None, customer_id: str | None = None,
) -> None:
    """Sync billing state to the user's organization if they're in one."""
    from sessionfs.server.db.models import OrgMember, Organization

    result = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user_id)
    )
    membership = result.scalar_one_or_none()
    if not membership:
        return

    org_result = await db.execute(
        select(Organization).where(Organization.id == membership.org_id)
    )
    org = org_result.scalar_one_or_none()
    if not org:
        return

    # Orgs only support team/enterprise/free — if Stripe sends starter/pro, treat as downgrade
    effective_org_tier = tier if tier in ("team", "enterprise", "free") else "free"
    org.tier = effective_org_tier
    org.stripe_subscription_id = subscription_id
    if customer_id:
        org.stripe_customer_id = customer_id

    # Always sync seats and storage — including on downgrade/cancel
    if effective_org_tier == "free":
        org.seats_limit = 0
        org.storage_limit_bytes = 0
    elif effective_org_tier == "enterprise":
        if seats and seats > 0:
            org.seats_limit = seats
        elif not org.seats_limit:
            org.seats_limit = 25
        org.storage_limit_bytes = 0  # unlimited
    elif effective_org_tier == "team":
        if seats and seats > 0:
            org.seats_limit = seats
            org.storage_limit_bytes = seats * 1024 * 1024 * 1024
        elif not org.seats_limit:
            org.seats_limit = 5
            org.storage_limit_bytes = 5 * 1024 * 1024 * 1024


async def _handle_checkout_completed(event, db: AsyncSession) -> None:
    """New subscription created via Checkout."""
    session = event.data.object
    user_id = session.metadata.get("user_id")
    tier = session.metadata.get("tier")
    subscription_id = session.subscription

    if not user_id or not tier:
        return

    from datetime import datetime, timezone
    org_id = session.metadata.get("org_id") if session.metadata else None
    seats = int(session.metadata.get("seats", "1")) if session.metadata else 1
    customer_id = session.get("customer", "")

    if org_id:
        # Org checkout — update org directly, don't write Stripe state to user
        from sessionfs.server.db.models import Organization
        org_result = await db.execute(select(Organization).where(Organization.id == org_id))
        org = org_result.scalar_one_or_none()
        if org:
            org.tier = tier if tier in ("team", "enterprise") else "free"
            org.stripe_subscription_id = subscription_id
            org.stripe_customer_id = customer_id or org.stripe_customer_id
            if tier == "team" and seats:
                org.seats_limit = seats
                org.storage_limit_bytes = seats * 1024 * 1024 * 1024
            elif tier == "enterprise":
                org.seats_limit = seats or 25
                org.storage_limit_bytes = 0
    else:
        # Personal checkout — update user
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                tier=tier,
                stripe_subscription_id=subscription_id,
                tier_updated_at=datetime.now(timezone.utc),
            )
        )
        await _sync_billing_to_org(user_id, tier, subscription_id, db, seats=seats, customer_id=customer_id)

    await db.commit()


async def _find_user_or_org_by_customer(customer_id: str, db: AsyncSession):
    """Find User and/or Org by stripe_customer_id.

    Orgs are checked first — if an org owns the customer, the org path wins
    even if a user still has the same customer_id (legacy data before the
    ownership-transfer fix). This prevents org subscription state from
    leaking back onto user rows.
    """
    from sessionfs.server.db.models import Organization

    # Check org first — org ownership takes precedence
    org_result = await db.execute(
        select(Organization).where(Organization.stripe_customer_id == customer_id)
    )
    org = org_result.scalar_one_or_none()
    if org:
        # Clean up legacy user rows that carry the org's subscription ID
        # (from before the ownership-transfer fix). Only clear fields when
        # the user's subscription matches the org's — preserve genuinely
        # personal subscriptions even if they share the same customer.
        if org.stripe_subscription_id:
            await db.execute(
                update(User)
                .where(
                    User.stripe_customer_id == customer_id,
                    User.stripe_subscription_id == org.stripe_subscription_id,
                )
                .values(stripe_customer_id=None, stripe_subscription_id=None)
            )
        return None, org

    # No org — try user
    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()
    return user, None


async def _handle_subscription_updated(event, db: AsyncSession) -> None:
    """Subscription changed (upgrade, downgrade, renewal)."""
    subscription = event.data.object
    customer_id = subscription.customer

    user, org = await _find_user_or_org_by_customer(customer_id, db)
    if not user and not org:
        logger.warning("Webhook: no user or org found for customer %s", customer_id)
        return

    status = subscription.status
    if status == "active":
        # Determine tier from price metadata
        try:
            items = subscription.get("items") or {}
            data_list = items.get("data") or []
            if not data_list:
                logger.warning("Subscription %s has no line items", subscription.id)
                return
            price = data_list[0].get("price") or {}
            product_id = price.get("product", "")
            if not product_id:
                logger.warning("Subscription %s has no product ID", subscription.id)
                return
            stripe = _get_stripe()
            product = stripe.Product.retrieve(product_id)
            new_tier = product.metadata.get("tier", "free")
        except Exception:
            logger.warning(
                "Failed to resolve tier for subscription %s — skipping",
                subscription.id,
                exc_info=True,
            )
            return

        from datetime import datetime, timezone
        seats = data_list[0].get("quantity", 1) if data_list else None

        if org:
            # Org-owned subscription — update org directly, never write
            # Stripe fields back to the admin user (they were cleared on
            # org creation and must stay clear).
            effective_tier = new_tier if new_tier in ("team", "enterprise", "free") else "free"
            org.tier = effective_tier
            org.stripe_subscription_id = subscription.id
            if effective_tier == "team" and seats:
                org.seats_limit = seats
                org.storage_limit_bytes = seats * 1024 * 1024 * 1024
            elif effective_tier == "enterprise":
                org.storage_limit_bytes = 0  # unlimited
                if seats and seats > 0:
                    org.seats_limit = seats
        elif user:
            # Personal subscription — update user directly
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    tier=new_tier,
                    stripe_subscription_id=subscription.id,
                    tier_updated_at=datetime.now(timezone.utc),
                )
            )
            await _sync_billing_to_org(user.id, new_tier, subscription.id, db, seats=seats, customer_id=customer_id)
        await db.commit()

    elif status in ("past_due", "unpaid", "paused", "incomplete_expired"):
        logger.warning(
            "Subscription %s moved to %s for customer %s — downgrading to free",
            subscription.id, status, customer_id,
        )
        from datetime import datetime, timezone
        if org:
            org.tier = "free"
            org.seats_limit = 0
            org.storage_limit_bytes = 0
        elif user:
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    tier="free",
                    tier_updated_at=datetime.now(timezone.utc),
                )
            )
            await _sync_billing_to_org(user.id, "free", subscription.id, db)
        await db.commit()


async def _handle_subscription_deleted(event, db: AsyncSession) -> None:
    """Subscription cancelled — downgrade to free."""
    subscription = event.data.object
    customer_id = subscription.customer

    user, org = await _find_user_or_org_by_customer(customer_id, db)
    if not user and not org:
        logger.warning("Webhook: no user or org found for customer %s (subscription deleted)", customer_id)
        return

    from datetime import datetime, timezone
    if org:
        # Org-owned subscription — update org directly, don't touch user rows
        org.tier = "free"
        org.stripe_subscription_id = None
        org.seats_limit = 0
        org.storage_limit_bytes = 0
    elif user:
        # Personal subscription
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                tier="free",
                stripe_subscription_id=None,
                tier_updated_at=datetime.now(timezone.utc),
            )
        )
        await _sync_billing_to_org(user.id, "free", None, db)

    await db.commit()


async def _handle_payment_failed(event, db: AsyncSession) -> None:
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
