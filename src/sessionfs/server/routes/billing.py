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


class PortalResponse(BaseModel):
    portal_url: str


class BillingStatusResponse(BaseModel):
    tier: str
    storage_used_bytes: int
    storage_limit_bytes: int
    stripe_customer_id: str | None
    has_subscription: bool


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

    # Prevent duplicate subscriptions — check both user and org
    if user.stripe_subscription_id:
        raise HTTPException(
            409,
            {"error": "already_subscribed", "message": "You already have an active subscription. Use the customer portal to manage it."},
        )
    if ctx.is_org_user and ctx.org and ctx.org.stripe_subscription_id:
        raise HTTPException(
            409,
            {"error": "already_subscribed", "message": "Your organization already has an active subscription. Use the customer portal to manage it."},
        )

    # Get or create Stripe customer
    if not user.stripe_customer_id:
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
    else:
        customer_id = user.stripe_customer_id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price": price_id,
            "quantity": data.seats if data.tier == "team" else 1,
        }],
        mode="subscription",
        success_url="https://app.sessionfs.dev/settings/billing?success=true",
        cancel_url="https://app.sessionfs.dev/settings/billing?cancelled=true",
        metadata={"user_id": user.id, "tier": data.tier, "seats": str(data.seats)},
    )

    if not session.url:
        raise HTTPException(500, "Stripe did not return a checkout URL")

    return CheckoutResponse(checkout_url=session.url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
):
    """Create a Stripe Customer Portal session for self-service management."""
    stripe = _get_stripe()

    # Use org customer_id for org members, user's for solo
    customer_id = None
    if ctx.is_org_user and ctx.org and ctx.org.stripe_customer_id:
        customer_id = ctx.org.stripe_customer_id
    elif user.stripe_customer_id:
        customer_id = user.stripe_customer_id

    if not customer_id:
        raise HTTPException(400, "No subscription found.")

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url="https://app.sessionfs.dev/settings/billing",
    )

    return PortalResponse(portal_url=session.url)


@router.get("/status", response_model=BillingStatusResponse)
async def billing_status(
    ctx: UserContext = Depends(get_user_context),
):
    """Get current subscription status."""
    user = ctx.user

    # For org members, use org billing state
    if ctx.is_org_user and ctx.org:
        return BillingStatusResponse(
            tier=ctx.effective_tier.value,
            storage_used_bytes=ctx.org.storage_used_bytes or 0,
            storage_limit_bytes=ctx.org.storage_limit_bytes or get_storage_limit(ctx.effective_tier),
            stripe_customer_id=ctx.org.stripe_customer_id,
            has_subscription=ctx.org.stripe_subscription_id is not None,
        )

    return BillingStatusResponse(
        tier=ctx.effective_tier.value,
        storage_used_bytes=user.storage_used_bytes or 0,
        storage_limit_bytes=get_storage_limit(ctx.effective_tier),
        stripe_customer_id=user.stripe_customer_id,
        has_subscription=user.stripe_subscription_id is not None,
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

    org.tier = tier
    org.stripe_subscription_id = subscription_id
    if customer_id:
        org.stripe_customer_id = customer_id
    if seats and seats > 0:
        org.seats_limit = seats
    if tier in ("team", "enterprise"):
        org.storage_limit_bytes = seats * 1024 * 1024 * 1024 if seats else org.storage_limit_bytes


async def _handle_checkout_completed(event, db: AsyncSession) -> None:
    """New subscription created via Checkout."""
    session = event.data.object
    user_id = session.metadata.get("user_id")
    tier = session.metadata.get("tier")
    subscription_id = session.subscription

    if not user_id or not tier:
        return

    from datetime import datetime, timezone
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            tier=tier,
            stripe_subscription_id=subscription_id,
            tier_updated_at=datetime.now(timezone.utc),
        )
    )

    # Seats: store in metadata since line_items aren't expanded in webhook
    # The checkout metadata includes tier; for team, seats come from the form
    seats = int(session.metadata.get("seats", "1")) if session.metadata else 1

    # Sync to org if user is in one
    customer_id = session.get("customer", "")
    await _sync_billing_to_org(user_id, tier, subscription_id, db, seats=seats, customer_id=customer_id)
    await db.commit()


async def _handle_subscription_updated(event, db: AsyncSession) -> None:
    """Subscription changed (upgrade, downgrade, renewal)."""
    subscription = event.data.object
    customer_id = subscription.customer

    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()
    if not user:
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
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                tier=new_tier,
                stripe_subscription_id=subscription.id,
                tier_updated_at=datetime.now(timezone.utc),
            )
        )

        # Extract seats from subscription quantity
        seats = data_list[0].get("quantity", 1) if data_list else None

        # Sync to org
        await _sync_billing_to_org(user.id, new_tier, subscription.id, db, seats=seats, customer_id=customer_id)
        await db.commit()


async def _handle_subscription_deleted(event, db: AsyncSession) -> None:
    """Subscription cancelled — downgrade to free."""
    subscription = event.data.object
    customer_id = subscription.customer

    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return

    from datetime import datetime, timezone
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            tier="free",
            stripe_subscription_id=None,
            tier_updated_at=datetime.now(timezone.utc),
        )
    )

    # Sync downgrade to org
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
