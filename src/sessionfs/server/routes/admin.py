"""Admin API routes for user/session management and system stats."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import require_admin
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    Handoff,
    Organization,
    OrgMember,
    Session,
    User,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

VALID_TIERS = {"free", "starter", "pro", "team", "enterprise", "admin"}


async def _log_action(
    db: AsyncSession,
    admin_id: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
) -> AdminAction:
    """Record an admin action in the audit log."""
    entry = AdminAction(
        id=str(uuid.uuid4()),
        admin_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details) if details else None,
    )
    db.add(entry)
    return entry


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    tier_filter: str | None = Query(None),
    search: str | None = Query(None),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all users with summary info."""
    query = select(User).where(User.is_active == True)  # noqa: E712

    if tier_filter:
        query = query.where(User.tier == tier_filter)
    if search:
        query = query.where(User.email.contains(search))

    # Total count (before pagination)
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    query = query.order_by(User.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()

    # Gather session counts per user in batch
    user_ids = [u.id for u in users]
    session_counts: dict[str, int] = {}
    if user_ids:
        sc_q = (
            select(Session.user_id, func.count())
            .where(Session.user_id.in_(user_ids), Session.is_deleted == False)  # noqa: E712
            .group_by(Session.user_id)
        )
        for row in (await db.execute(sc_q)).all():
            session_counts[row[0]] = row[1]

    items = []
    for u in users:
        items.append({
            "id": u.id,
            "email": u.email,
            "tier": u.tier,
            "email_verified": u.email_verified,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "session_count": session_counts.get(u.id, 0),
        })

    return {"total": total, "page": page, "page_size": page_size, "users": items}


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Full user detail with session/storage/key stats."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Session count
    sc = (
        await db.execute(
            select(func.count())
            .select_from(Session)
            .where(Session.user_id == user_id, Session.is_deleted == False)  # noqa: E712
        )
    ).scalar() or 0

    # Storage used
    storage = (
        await db.execute(
            select(func.coalesce(func.sum(Session.blob_size_bytes), 0))
            .where(Session.user_id == user_id, Session.is_deleted == False)  # noqa: E712
        )
    ).scalar() or 0

    # API key count
    key_count = (
        await db.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == user_id, ApiKey.is_active == True)  # noqa: E712
        )
    ).scalar() or 0

    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "tier": user.tier,
        "email_verified": user.email_verified,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "session_count": sc,
        "storage_used_bytes": storage,
        "api_key_count": key_count,
    }


@router.put("/users/{user_id}/tier")
async def change_user_tier(
    user_id: str,
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change a user's tier."""
    new_tier = body.get("tier")
    if new_tier not in VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {', '.join(sorted(VALID_TIERS))}")

    if user_id == admin.id and new_tier != "admin":
        raise HTTPException(status_code=400, detail="Cannot demote your own admin account")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    old_tier = user.tier
    user.tier = new_tier

    await _log_action(db, admin.id, "tier_change", "user", user_id, {
        "old_tier": old_tier, "new_tier": new_tier,
    })
    await db.commit()

    return {"user_id": user_id, "old_tier": old_tier, "new_tier": new_tier}


@router.put("/users/{user_id}/verify")
async def force_verify_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Force-verify a user's email."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.email_verified = True

    await _log_action(db, admin.id, "verify", "user", user_id)
    await db.commit()

    return {"user_id": user_id, "email_verified": True}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a user: deactivate account and revoke all API keys."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    user.is_active = False

    # Revoke all API keys
    await db.execute(
        update(ApiKey).where(ApiKey.user_id == user_id).values(is_active=False)
    )

    # Remove org memberships to free seats
    await db.execute(
        delete(OrgMember).where(OrgMember.user_id == user_id)
    )

    # Expire pending handoffs sent to this user
    await db.execute(
        update(Handoff)
        .where(Handoff.recipient_email == user.email, Handoff.status == "pending")
        .values(status="expired")
    )

    await _log_action(db, admin.id, "delete_user", "user", user_id, {
        "email": user.email,
    })
    await db.commit()


# ---------------------------------------------------------------------------
# Organization management (admin back-office)
# ---------------------------------------------------------------------------


@router.get("/orgs")
async def list_orgs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all organizations with member counts."""
    offset = (page - 1) * page_size
    total = (await db.execute(select(func.count(Organization.id)))).scalar() or 0

    result = await db.execute(
        select(Organization)
        .order_by(Organization.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    orgs = result.scalars().all()

    items = []
    for org in orgs:
        member_count = (
            await db.execute(
                select(func.count(OrgMember.user_id)).where(OrgMember.org_id == org.id)
            )
        ).scalar() or 0
        items.append({
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "tier": org.tier,
            "seats_limit": org.seats_limit,
            "storage_limit_bytes": org.storage_limit_bytes,
            "member_count": member_count,
            "created_at": org.created_at.isoformat() if org.created_at else None,
        })

    return {
        "orgs": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/orgs", status_code=201)
async def admin_create_org(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create an organization with arbitrary tier/seats/storage, bypassing the
    normal Team+ subscription gate. Designed for back-office setup of the
    SessionFS company org and for enterprise pre-sales provisioning.

    Required body fields:
      - name: human-readable org name
      - slug: unique URL slug
      - owner_user_id: user ID that becomes the org admin
    Optional:
      - tier: one of free/starter/pro/team/enterprise (default: enterprise)
      - seats_limit: int (default: 100)
      - storage_limit_bytes: int (default: 0 = unlimited)
    """
    import secrets as _secrets

    name = (body.get("name") or "").strip()
    slug = (body.get("slug") or "").strip()
    owner_user_id = body.get("owner_user_id")
    tier = body.get("tier", "enterprise")
    seats_limit = int(body.get("seats_limit", 100))
    storage_limit_bytes = int(body.get("storage_limit_bytes", 0))

    if not name or not slug:
        raise HTTPException(400, "name and slug are required")
    if tier not in VALID_TIERS or tier == "admin":
        raise HTTPException(
            400, f"tier must be one of: {', '.join(sorted(VALID_TIERS - {'admin'}))}"
        )
    if not owner_user_id:
        raise HTTPException(400, "owner_user_id is required")

    # Verify owner exists
    owner = (
        await db.execute(select(User).where(User.id == owner_user_id))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(404, f"User {owner_user_id} not found")

    # Slug uniqueness
    existing = (
        await db.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"Organization slug '{slug}' is already taken")

    # Owner can't already be in an org
    existing_member = (
        await db.execute(select(OrgMember).where(OrgMember.user_id == owner_user_id))
    ).scalar_one_or_none()
    if existing_member is not None:
        raise HTTPException(
            409, f"User {owner_user_id} is already a member of an organization"
        )

    org_id = f"org_{_secrets.token_hex(8)}"
    org = Organization(
        id=org_id,
        name=name,
        slug=slug,
        tier=tier,
        seats_limit=seats_limit,
        storage_limit_bytes=storage_limit_bytes,
    )
    db.add(org)

    member = OrgMember(org_id=org_id, user_id=owner_user_id, role="admin")
    db.add(member)

    await _log_action(
        db,
        admin.id,
        "admin_create_org",
        "org",
        org_id,
        {
            "name": name,
            "slug": slug,
            "tier": tier,
            "seats_limit": seats_limit,
            "storage_limit_bytes": storage_limit_bytes,
            "owner_user_id": owner_user_id,
        },
    )
    await db.commit()

    return {
        "id": org_id,
        "name": name,
        "slug": slug,
        "tier": tier,
        "seats_limit": seats_limit,
        "storage_limit_bytes": storage_limit_bytes,
        "owner_user_id": owner_user_id,
    }


@router.put("/orgs/{org_id}/tier")
async def admin_change_org_tier(
    org_id: str,
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change an organization's tier + seat/storage limits.

    Body fields (all optional; only provided ones are updated):
      - tier: one of free/starter/pro/team/enterprise
      - seats_limit: int
      - storage_limit_bytes: int (0 = unlimited)
    """
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(404, "Organization not found")

    changes: dict[str, object] = {}
    if "tier" in body:
        new_tier = body["tier"]
        if new_tier not in VALID_TIERS or new_tier == "admin":
            raise HTTPException(400, f"Invalid tier: {new_tier}")
        changes["tier"] = new_tier
    if "seats_limit" in body:
        changes["seats_limit"] = int(body["seats_limit"])
    if "storage_limit_bytes" in body:
        changes["storage_limit_bytes"] = int(body["storage_limit_bytes"])

    if not changes:
        raise HTTPException(400, "No changes provided")

    await db.execute(update(Organization).where(Organization.id == org_id).values(**changes))

    await _log_action(db, admin.id, "admin_update_org", "org", org_id, changes)
    await db.commit()

    return {"org_id": org_id, "updated": changes}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_all_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: str | None = Query(None),
    source_tool: str | None = Query(None),
    sort: str = Query("created_at"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all sessions across all users."""
    query = select(Session).where(Session.is_deleted == False)  # noqa: E712

    if user_id:
        query = query.where(Session.user_id == user_id)
    if source_tool:
        query = query.where(Session.source_tool == source_tool)

    # Sort
    sort_col = {
        "created_at": Session.created_at,
        "message_count": Session.message_count,
        "blob_size": Session.blob_size_bytes,
    }.get(sort, Session.created_at)
    query = query.order_by(sort_col.desc())

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)
    result = await db.execute(query)
    sessions = result.scalars().all()

    items = []
    for s in sessions:
        items.append({
            "id": s.id,
            "user_id": s.user_id,
            "title": s.title,
            "source_tool": s.source_tool,
            "message_count": s.message_count,
            "blob_size_bytes": s.blob_size_bytes,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    return {"total": total, "page": page, "page_size": page_size, "sessions": items}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Force soft-delete a session."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    session.is_deleted = True
    session.deleted_at = datetime.now(timezone.utc)

    await _log_action(db, admin.id, "delete_session", "session", session_id, {
        "user_id": session.user_id, "title": session.title,
    })
    await db.commit()


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------


@router.get("/stats")
async def get_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """System-wide statistics."""
    # Users
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    verified_users = (
        await db.execute(
            select(func.count()).select_from(User).where(User.email_verified == True)  # noqa: E712
        )
    ).scalar() or 0

    tier_rows = (
        await db.execute(
            select(User.tier, func.count()).group_by(User.tier)
        )
    ).all()
    by_tier = {row[0]: row[1] for row in tier_rows}

    # Sessions
    total_sessions = (
        await db.execute(
            select(func.count()).select_from(Session).where(Session.is_deleted == False)  # noqa: E712
        )
    ).scalar() or 0
    total_size = (
        await db.execute(
            select(func.coalesce(func.sum(Session.blob_size_bytes), 0))
            .where(Session.is_deleted == False)  # noqa: E712
        )
    ).scalar() or 0

    tool_rows = (
        await db.execute(
            select(Session.source_tool, func.count())
            .where(Session.is_deleted == False)  # noqa: E712
            .group_by(Session.source_tool)
        )
    ).all()
    by_tool = {row[0]: row[1] for row in tool_rows}

    # Handoffs
    total_handoffs = (await db.execute(select(func.count()).select_from(Handoff))).scalar() or 0
    pending_handoffs = (
        await db.execute(
            select(func.count()).select_from(Handoff).where(Handoff.status == "pending")
        )
    ).scalar() or 0
    claimed_handoffs = (
        await db.execute(
            select(func.count()).select_from(Handoff).where(Handoff.status == "claimed")
        )
    ).scalar() or 0

    return {
        "users": {
            "total": total_users,
            "verified": verified_users,
            "by_tier": by_tier,
        },
        "sessions": {
            "total": total_sessions,
            "total_size_bytes": total_size,
            "by_tool": by_tool,
        },
        "handoffs": {
            "total": total_handoffs,
            "pending": pending_handoffs,
            "claimed": claimed_handoffs,
        },
        "storage": {
            "total_bytes": total_size,
            "blob_count": total_sessions,
        },
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit-log")
async def get_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List recent admin actions."""
    count_q = select(func.count()).select_from(AdminAction)
    total = (await db.execute(count_q)).scalar() or 0

    offset = (page - 1) * page_size
    query = (
        select(AdminAction)
        .order_by(AdminAction.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    actions = result.scalars().all()

    items = []
    for a in actions:
        items.append({
            "id": a.id,
            "admin_id": a.admin_id,
            "action": a.action,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "details": json.loads(a.details) if a.details else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    return {"total": total, "page": page, "page_size": page_size, "actions": items}
