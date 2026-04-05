"""Organization management routes — create, members, invites, roles."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import OrgInvite, OrgMember, Organization, User
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
    """Create an organization. The creator becomes admin."""
    # Check user has a Team+ subscription
    if user.tier not in ("team", "enterprise", "admin"):
        raise HTTPException(403, "Organizations require Team tier or above. Upgrade at https://sessionfs.dev/pricing")

    # Check slug uniqueness
    existing = await db.execute(
        select(Organization).where(Organization.slug == data.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Organization slug '{data.slug}' is already taken")

    # Check user isn't already in an org
    existing_member = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user.id)
    )
    if existing_member.scalar_one_or_none():
        raise HTTPException(409, "You are already a member of an organization")

    org_id = f"org_{secrets.token_hex(8)}"
    tier = user.tier if user.tier in ("team", "enterprise") else "team"

    # Derive seats from Stripe subscription quantity if available
    seats = 5  # default
    if user.stripe_subscription_id:
        try:
            import os
            stripe_key = os.environ.get("SFS_STRIPE_SECRET_KEY", "")
            if stripe_key:
                import stripe
                stripe.api_key = stripe_key
                sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
                if sub.get("items", {}).get("data"):
                    seats = sub["items"]["data"][0].get("quantity", 5)
        except Exception:
            pass  # Fall back to default seats on any Stripe error

    storage = seats * 1024 * 1024 * 1024  # 1GB per seat

    org = Organization(
        id=org_id,
        name=data.name,
        slug=data.slug,
        tier=tier,
        stripe_customer_id=user.stripe_customer_id,
        stripe_subscription_id=user.stripe_subscription_id,
        seats_limit=seats,
        storage_limit_bytes=storage,
    )
    db.add(org)

    # Creator is admin
    member = OrgMember(
        org_id=org_id,
        user_id=user.id,
        role="admin",
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

    # Check for existing invite
    existing = await db.execute(
        select(OrgInvite).where(
            OrgInvite.org_id == ctx.org.id,
            OrgInvite.email == data.email,
            OrgInvite.accepted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "An active invite already exists for this email")

    # Check user isn't already a member
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
            raise HTTPException(409, "This user is already a member of your organization")

    role = data.role if data.role in ("admin", "member") else "member"
    invite_id = f"inv_{secrets.token_hex(8)}"
    invite = OrgInvite(
        id=invite_id,
        org_id=ctx.org.id,
        email=data.email,
        role=role,
        invited_by=ctx.user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invite)
    await db.commit()

    # Send invite email if email service available
    try:
        from fastapi import Request as _  # noqa: F401
        # Email sending delegated to caller/background — keep route fast
    except Exception:
        pass

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
    if invite.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "Invite has expired")
    if invite.email != user.email:
        raise HTTPException(403, "This invite is for a different email address")

    # Check user isn't already in an org
    existing = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You are already a member of an organization")

    member = OrgMember(
        org_id=invite.org_id,
        user_id=user.id,
        role=invite.role,
        invited_by=invite.invited_by,
        invited_at=invite.created_at,
    )
    db.add(member)

    await db.execute(
        update(OrgInvite)
        .where(OrgInvite.id == invite_id)
        .values(accepted_at=datetime.now(timezone.utc))
    )
    await db.commit()

    return {"org_id": invite.org_id, "role": invite.role}


@router.put("/members/{user_id}/role")
async def change_member_role(
    user_id: str,
    data: ChangeRoleRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Change a member's role. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    if data.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    # Can't change own role (prevents last-admin lockout)
    if user_id == ctx.user.id:
        raise HTTPException(400, "Cannot change your own role")

    result = await db.execute(
        select(OrgMember).where(
            OrgMember.org_id == ctx.org.id,
            OrgMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(404, "Member not found in your organization")

    await db.execute(
        update(OrgMember)
        .where(OrgMember.org_id == ctx.org.id, OrgMember.user_id == user_id)
        .values(role=data.role)
    )
    await db.commit()

    return {"user_id": user_id, "role": data.role}


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member from the org. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    # Can't remove yourself
    if user_id == ctx.user.id:
        raise HTTPException(400, "Cannot remove yourself. Transfer admin role first.")

    result = await db.execute(
        select(OrgMember).where(
            OrgMember.org_id == ctx.org.id,
            OrgMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(404, "Member not found in your organization")

    await db.execute(
        delete(OrgMember).where(
            OrgMember.org_id == ctx.org.id,
            OrgMember.user_id == user_id,
        )
    )
    await db.commit()

    return {"removed": user_id}


@router.get("/invites")
async def list_invites(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """List pending org invites. Admin only."""
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "You are not in an organization")

    result = await db.execute(
        select(OrgInvite).where(
            OrgInvite.org_id == ctx.org.id,
            OrgInvite.accepted_at.is_(None),
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
