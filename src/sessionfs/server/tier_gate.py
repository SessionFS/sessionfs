"""Tier gating and RBAC middleware for API routes.

P2 (entitlement resolution switch): get_effective_tier and get_user_context
now resolve through the entitlements table FIRST (via resolve_entitlement),
falling back to the legacy org.tier / user.tier columns when no active
entitlement exists.  This ensures behavior is IDENTICAL for backfilled data
(the P1 backfill created entitlements matching current tiers), while new
entitlement mutations (via apply_entitlement) become authoritative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import OrgMember, Organization, User
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services.entitlements import resolve_entitlement
from sessionfs.server.tiers import Tier, format_bytes, get_features_for_tier, get_minimum_tier_for_feature, get_storage_limit

logger = logging.getLogger("sessionfs.api")


@dataclass
class UserContext:
    """Full auth context for a request — tier + role + org."""

    user: User
    effective_tier: Tier
    org: Organization | None
    role: str | None  # OrgRole value or None for solo users
    is_org_user: bool


async def get_user_org_membership(
    user_id: str, db: AsyncSession
) -> OrgMember | None:
    """Get user's organization membership, if any."""
    result = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def _get_org(org_id: str, db: AsyncSession) -> Organization | None:
    """Fetch an Organization by id (cached helper for get_user_context)."""
    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    return result.scalar_one_or_none()


async def get_effective_tier(user: User, db: AsyncSession) -> Tier:
    """Resolve the user's effective tier.

    P2 (entitlement resolution switch):
    1. If the user is an org member → resolve org entitlement FIRST,
       fall back to org.tier if no active entitlement exists.
    2. Else → resolve user entitlement FIRST, fall back to user.tier
       if no active entitlement exists.
    3. Legacy admin tier: 'admin' is never an entitlement tier; always
       resolves to ENTERPRISE.
    """
    membership = await get_user_org_membership(user.id, db)

    if membership:
        # Try entitlement resolution first for the org.
        ent = await resolve_entitlement("org", membership.org_id, db)
        if ent is not None:
            try:
                return Tier(ent.tier)
            except ValueError:
                pass

        # Fallback: legacy org.tier column.
        result = await db.execute(
            select(Organization).where(Organization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            try:
                return Tier(org.tier)
            except ValueError:
                pass

    # Try entitlement resolution for the user (no org, or org fallback missed).
    ent = await resolve_entitlement("user", user.id, db)
    if ent is not None:
        try:
            return Tier(ent.tier)
        except ValueError:
            pass

    # Fallback: legacy user.tier column.
    try:
        return Tier(user.tier)
    except ValueError:
        # Legacy admin tier — not an entitlement tier, always ENTERPRISE.
        if user.tier == "admin":
            return Tier.ENTERPRISE
        return Tier.FREE


async def _resolve_user_for_context(
    request,
    db: AsyncSession,
) -> User:
    """v0.10.10 — pull the authenticated User from request.state.auth_context
    if a scoped dependency (require_scope) has already run; otherwise fall
    back to the legacy get_current_user path. This avoids double-auth when
    a route depends on BOTH require_scope and get_user_context, and crucially
    avoids the get_current_user service-key rejection on routes that have
    already admitted the service key via require_scope.
    """
    auth_ctx = getattr(request.state, "auth_context", None)
    if auth_ctx is not None:
        return auth_ctx.user
    # Legacy path — fall through to get_current_user, which authenticates
    # AND rejects service keys with 403 service_key_not_allowed. This
    # preserves the deny-by-default behavior for routes that don't use
    # require_scope.
    from sessionfs.server.auth.dependencies import get_rate_limiter
    return await get_current_user(
        request=request, db=db, limiter=get_rate_limiter()
    )


async def get_user_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """Full auth context for a request — tier + role + org.

    v0.10.10 — sources the User from request.state.auth_context when a
    scoped dependency (require_scope) has already authenticated. Falls
    back to legacy get_current_user for routes that don't use scoped
    auth.

    P2 (entitlement resolution switch): resolves effective tier through
    the entitlements table FIRST, falling back to legacy org.tier /
    user.tier when no active entitlement exists.
    """
    user = await _resolve_user_for_context(request, db)
    membership = await get_user_org_membership(user.id, db)

    if membership:
        # Resolve org entitlement FIRST, fall back to legacy org.tier.
        ent = await resolve_entitlement("org", membership.org_id, db)
        if ent is not None:
            try:
                tier = Tier(ent.tier)
            except ValueError:
                tier = Tier.TEAM
            return UserContext(
                user=user,
                effective_tier=tier,
                org=await _get_org(membership.org_id, db),
                role=membership.role,
                is_org_user=True,
            )

        # Fallback: legacy org.tier column.
        result = await db.execute(
            select(Organization).where(Organization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            try:
                tier = Tier(org.tier)
            except ValueError:
                tier = Tier.TEAM
            return UserContext(
                user=user,
                effective_tier=tier,
                org=org,
                role=membership.role,
                is_org_user=True,
            )

    # Resolve user entitlement FIRST, fall back to legacy user.tier.
    ent = await resolve_entitlement("user", user.id, db)
    if ent is not None:
        try:
            tier = Tier(ent.tier)
        except ValueError:
            tier = Tier.FREE
        return UserContext(
            user=user,
            effective_tier=tier,
            org=None,
            role=None,
            is_org_user=False,
        )

    # Fallback: legacy user.tier column.
    try:
        tier = Tier(user.tier)
    except ValueError:
        tier = Tier.ENTERPRISE if user.tier == "admin" else Tier.FREE

    return UserContext(
        user=user,
        effective_tier=tier,
        org=None,
        role=None,
        is_org_user=False,
    )


def check_feature(ctx: UserContext, feature: str) -> None:
    """Raise 403 if the user's tier doesn't include the feature."""
    user_features = get_features_for_tier(ctx.effective_tier)
    if feature not in user_features:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": feature,
                "current_tier": ctx.effective_tier.value,
                "required_tier": get_minimum_tier_for_feature(feature),
                "upgrade_url": "https://sessionfs.dev/pricing",
                "message": f"This feature requires {get_minimum_tier_for_feature(feature)} or above.",
            },
        )


def check_role(ctx: UserContext, min_role: str) -> None:
    """Raise 403 if the user's role is insufficient.

    Non-org users are always rejected — org role checks only apply to org members.
    """
    if not ctx.is_org_user:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_role",
                "current_role": "none",
                "required_role": min_role,
                "message": "This action requires an organization membership.",
            },
        )
    if not ctx.role or not has_minimum_role(ctx.role, min_role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_role",
                "current_role": ctx.role or "none",
                "required_role": min_role,
                "message": f"This action requires the {min_role} role.",
            },
        )


def check_storage(ctx: UserContext, size_bytes: int) -> None:
    """Raise 403 if storage limit would be exceeded."""
    limit = get_storage_limit(ctx.effective_tier)
    current = ctx.user.storage_used_bytes or 0
    if ctx.is_org_user and ctx.org:
        current = ctx.org.storage_used_bytes or 0
        limit = ctx.org.storage_limit_bytes or limit

    if limit > 0 and (current + size_bytes) > limit:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "storage_limit",
                "current_usage": current,
                "limit": limit,
                "message": f"Storage limit ({format_bytes(limit)}) reached. Upgrade for more.",
                "upgrade_url": "https://sessionfs.dev/pricing",
            },
        )
