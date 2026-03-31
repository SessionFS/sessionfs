"""Tier gating and RBAC middleware for API routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import OrgMember, Organization, User
from sessionfs.server.roles import has_minimum_role
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


async def get_effective_tier(user: User, db: AsyncSession) -> Tier:
    """Resolve the user's effective tier.

    Solo users: tier lives on user record.
    Org users: tier inherited from their org.
    """
    membership = await get_user_org_membership(user.id, db)

    if membership:
        result = await db.execute(
            select(Organization).where(Organization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            try:
                return Tier(org.tier)
            except ValueError:
                pass

    try:
        return Tier(user.tier)
    except ValueError:
        # Legacy admin tier
        if user.tier == "admin":
            return Tier.ENTERPRISE
        return Tier.FREE


async def get_user_context(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """Full auth context for a request — tier + role + org."""
    membership = await get_user_org_membership(user.id, db)

    if membership:
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
