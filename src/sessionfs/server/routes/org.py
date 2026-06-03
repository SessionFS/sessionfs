"""Organization management routes — create, members, invites, roles."""

from __future__ import annotations

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
        stripe_customer_id=user.stripe_customer_id,
        stripe_subscription_id=user.stripe_subscription_id,
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

    # Transfer subscription ownership: clear user-level Stripe fields
    # so they can't be confused with a personal subscription later.
    if user.stripe_customer_id or user.stripe_subscription_id:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(stripe_customer_id=None, stripe_subscription_id=None)
        )

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

    # Check user isn't already in an org
    existing = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You are already a member of an organization")

    # Re-check seat capacity at acceptance time (not just invite creation)
    org_result = await db.execute(
        select(Organization).where(Organization.id == invite.org_id)
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
