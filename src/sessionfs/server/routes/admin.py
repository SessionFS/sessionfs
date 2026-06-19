"""Admin API routes for user/session management and system stats."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import require_admin
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    Handoff,
    HelmLicense,
    Organization,
    OrgAuditEvent,
    OrgMember,
    Session,
    User,
)
from sessionfs.server.services.entitlements import apply_entitlement

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

    # P2: write-through — upsert the user's active entitlement so
    # resolution stays authoritative.  'admin' tier is NOT written
    # as an entitlement (platform role, not a customer tier).
    if new_tier in ("free", "starter", "pro", "team", "enterprise"):
        await apply_entitlement(
            "user",
            user_id,
            tier=new_tier,
            source="admin_provisioned",
            db=db,
        )

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


# tk_4afbae8ed3a442e9 — Admin operational tool to mint a fresh API
# key on behalf of any user. SessionFS has no magic-link / OAuth /
# password-reset / signin flow, so a user who loses their key has no
# self-service recovery. This endpoint is the legitimate
# admin-impersonation-free recovery path (no session token issued —
# the admin gets a raw key once and hands it to the user out-of-band).


class MintApiKeyOnBehalfRequest(BaseModel):
    name: str | None = None


class MintApiKeyOnBehalfResponse(BaseModel):
    key_id: str
    raw_key: str
    name: str | None
    created_at: datetime
    user_id: str
    user_email: str


@router.post(
    "/users/{user_id}/api-keys",
    response_model=MintApiKeyOnBehalfResponse,
    status_code=201,
)
async def mint_api_key_on_behalf(
    user_id: str,
    body: MintApiKeyOnBehalfRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MintApiKeyOnBehalfResponse:
    """Mint a user-kind API key on behalf of the target user.

    Admin-only. Mirrors `POST /api/v1/auth/me/api-keys` response shape
    plus `user_id` + `user_email` so the caller knows which account
    received the key. Raw key is returned exactly once.

    Mints a `key_kind='user'` row with `scopes='["*"]'` — set
    explicitly at the call site below (not relying on column
    defaults) so the security contract stays visible during future
    refactors. Full user-equivalent capability, matching what the
    user would have minted for themselves via the self-service
    endpoint. NOT a scoped service key (those live at
    `/api/v1/orgs/{org_id}/service-keys` per v0.10.10).

    Refuses to mint for inactive users (no backdoor for disabled
    accounts) and unknown user_ids. All successful mints write an
    audit log entry via `_log_action`.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.is_active:
        raise HTTPException(
            status_code=403, detail="Cannot mint key for inactive user"
        )

    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    name = body.name or "admin-minted"

    # Mirror the explicit-field shape from
    # `routes/api_keys.py:create_personal_key` (Codex R3 MEDIUM 2 +
    # this ticket R1 MEDIUM) — populate `key_prefix` so the row shows
    # up in list responses as `sk_sfs_xxxxxx` instead of the
    # `sk_sfs_legacy` placeholder. Also set `key_kind` + `scopes`
    # explicitly rather than relying on column defaults, so this
    # endpoint's security contract is visible at the call site rather
    # than in model defaults that future migrations could shift.
    # `created_by_user_id=admin.id` captures the admin issuer for
    # audit; `target.id` already owns the row via `user_id`.
    from sessionfs.server.routes.api_keys import _key_prefix

    api_key = ApiKey(
        id=key_id,
        user_id=target.id,
        key_hash=hash_api_key(raw_key),
        name=name,
        key_kind="user",
        scopes=json.dumps(["*"]),
        key_prefix=_key_prefix(raw_key),
        created_by_user_id=admin.id,
        is_active=True,
    )
    db.add(api_key)

    await _log_action(
        db,
        admin.id,
        "mint_api_key_on_behalf",
        "user",
        user_id,
        {"key_id": key_id, "name": name},
    )
    await db.commit()
    await db.refresh(api_key)

    return MintApiKeyOnBehalfResponse(
        key_id=api_key.id,
        raw_key=raw_key,
        name=api_key.name,
        created_at=api_key.created_at,
        user_id=target.id,
        user_email=target.email,
    )


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

    # Expire pending handoffs sent to this user. Mirror the inbox lookup
    # path: prefer the indexed normalized column, fall back to a
    # case+whitespace-tolerant match on the raw column for legacy rows
    # that pre-date migration 032's backfill. Without this, mixed-case
    # legacy handoffs stay pending forever after user deletion.
    from sqlalchemy import func as sa_func, or_
    from sessionfs.server.routes.handoffs import normalize_email

    user_email_norm = normalize_email(user.email)
    if user_email_norm:
        await db.execute(
            update(Handoff)
            .where(
                or_(
                    Handoff.recipient_email_normalized == user_email_norm,
                    (Handoff.recipient_email_normalized.is_(None))
                    & (
                        sa_func.lower(sa_func.trim(Handoff.recipient_email))
                        == user_email_norm
                    ),
                ),
                Handoff.status == "pending",
            )
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

    # Batch-load member counts for the page's orgs in ONE query instead
    # of N+1. Previously the loop below ran `SELECT count(*) FROM
    # org_members WHERE org_id = ?` once per org — on a 50-org page
    # that's 50 round-trips for what's a single GROUP BY.
    member_counts: dict[str, int] = {}
    if orgs:
        org_ids = [o.id for o in orgs]
        member_rows = (await db.execute(
            select(OrgMember.org_id, func.count(OrgMember.user_id))
            .where(OrgMember.org_id.in_(org_ids))
            .group_by(OrgMember.org_id)
        )).all()
        member_counts = {oid: cnt for oid, cnt in member_rows}

    items = []
    for org in orgs:
        items.append({
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "tier": org.tier,
            "seats_limit": org.seats_limit,
            "storage_limit_bytes": org.storage_limit_bytes,
            "member_count": member_counts.get(org.id, 0),
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
    # Explicit flush so the organizations row exists before the org_members FK
    # is checked. Without this, db.commit() may flush org_members before
    # organizations, triggering ForeignKeyViolationError on Postgres.
    await db.flush()

    member = OrgMember(org_id=org_id, user_id=owner_user_id, role="admin")
    db.add(member)

    # P2: write-through — create entitlement for the admin-provisioned org.
    # If a license_key is provided, source from the HelmLicense (admin-assisted
    # pre-provision — staff is the trust anchor, no email token needed).
    license_key = (body.get("license_key") or "").strip()
    lic = None
    if license_key:
        lic = (
            await db.execute(
                select(HelmLicense).where(
                    HelmLicense.id == license_key,
                    HelmLicense.status == "active",
                    HelmLicense.org_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if lic is not None:
            # Bind the license to this org
            await db.execute(
                update(HelmLicense)
                .where(HelmLicense.id == license_key, HelmLicense.org_id.is_(None))
                .values(org_id=org_id)
            )
            tier = lic.tier
            seats_limit = lic.seats_limit
            source = "helm_license"
            source_ref = license_key
            current_period_end = lic.expires_at
        else:
            raise HTTPException(
                400,
                f"License key '{license_key}' is invalid, expired, or already bound.",
            )
    else:
        source = "admin_provisioned"
        source_ref = None
        current_period_end = None

    await apply_entitlement(
        "org",
        org_id,
        tier=tier,
        seats=seats_limit,
        storage=storage_limit_bytes,
        source=source,
        source_ref=source_ref,
        db=db,
        current_period_end=current_period_end,
    )

    # Emit OrgAuditEvent for license-activated admin orgs
    if lic is not None:
        audit = OrgAuditEvent(
            id=f"oae_{_secrets.token_hex(12)}",
            org_id=org_id,
            org_name_snapshot=name,
            event_type="license_activated",
            actor_user_id=admin.id,
            actor_email_snapshot=admin.email,
            actor_role_at_time="platform_admin",
            target_type="license",
            target_id=license_key[:7] + "…",
            after=json.dumps(
                {
                    "org_id": org_id,
                    "org_name": name,
                    "license_key_prefix": license_key[:7] + "…",
                    "tier": lic.tier,
                    "seats_limit": lic.seats_limit,
                    "verification_method": "admin_assisted",
                }
            ),
        )
        db.add(audit)

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

    # P2: write-through — keep the entitlement in sync with admin tier change.
    new_tier = changes.get("tier", org.tier)
    if new_tier in ("free", "starter", "pro", "team", "enterprise"):
        await apply_entitlement(
            "org",
            org_id,
            tier=new_tier,
            seats=changes.get("seats_limit"),
            storage=changes.get("storage_limit_bytes"),
            source="admin_provisioned",
            db=db,
        )

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
        from sessionfs.server.routes.sessions import _source_tool_filter_values

        tool_values = _source_tool_filter_values(source_tool)
        if len(tool_values) == 1:
            query = query.where(Session.source_tool == source_tool)
        else:
            query = query.where(Session.source_tool.in_(tool_values))

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


@router.post("/purge-deleted")
async def purge_deleted(
    body: dict | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Purge soft-deleted sessions past their retention window.

    Optional body: {"session_id": "ses_abc"} for single-session purge.
    No body: purge all expired sessions (purge_after < now()).
    """
    now = datetime.now(timezone.utc)
    purged = 0
    bytes_reclaimed = 0

    # Get blob store from app state
    blob_store = request.app.state.blob_store

    if body and body.get("session_id"):
        # Single session purge — validate session_id format
        session_id = body["session_id"]
        if not isinstance(session_id, str) or len(session_id) > 50:
            raise HTTPException(status_code=400, detail="Invalid session_id format")
        result = await db.execute(
            select(Session).where(
                Session.id == session_id,
                Session.is_deleted == True,  # noqa: E712
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="Deleted session not found")
        # Delete blob
        try:
            await blob_store.delete(session.blob_key)
        except Exception:
            pass  # Blob may already be gone
        bytes_reclaimed = session.blob_size_bytes or 0
        await db.execute(delete(Session).where(Session.id == session_id))
        purged = 1
    else:
        # Bulk purge: all expired
        result = await db.execute(
            select(Session).where(
                Session.is_deleted == True,  # noqa: E712
                Session.purge_after != None,  # noqa: E711
                Session.purge_after < now,
            )
        )
        sessions = result.scalars().all()
        for session in sessions:
            try:
                await blob_store.delete(session.blob_key)
            except Exception:
                pass
            bytes_reclaimed += session.blob_size_bytes or 0
            await db.execute(delete(Session).where(Session.id == session.id))
            purged += 1

    # Log audit action before commit so both purge and audit are atomic
    await _log_action(
        db,
        admin.id,
        "purge_deleted",
        "sessions",
        body.get("session_id", "bulk") if body else "bulk",
        {"purged": purged, "bytes_reclaimed": bytes_reclaimed},
    )
    await db.commit()

    return {"purged": purged, "bytes_reclaimed": bytes_reclaimed}


# ---------------------------------------------------------------------------
# Project context repair — restore from a ContextCompilation snapshot
# tk_dd3ba7082ef0432e (v0.10.13)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/restore-from-compilation")
async def restore_project_from_compilation(
    project_id: str,
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Restore a project's context document AND the compiled_at metadata
    on participating entries from a specific ContextCompilation snapshot.

    Background: when /rebuild crashed before tk_bc3c02a63e994717 was
    fixed, it left projects with empty `context_document` and NULL
    `compiled_at` on every active claim. The public `PUT
    /projects/{git_remote}/context` endpoint can restore the document
    text alone (R1), but not the metadata. This endpoint closes that
    gap by parsing `source_manifest` (the per-compilation provenance
    JSON) and restoring `compiled_at` on the exact entries that
    participated in the chosen compilation.

    Body:
        compilation_id (int, required) — id of the row in
            context_compilations to restore from. Must belong to this
            project.
        dry_run (bool, optional, default true) — when true, reports
            counts only and writes nothing.

    Returns:
        {
            "project_id": str,
            "compilation_id": int,
            "compiled_at": ISO ts of the snapshot,
            "context_words_restored": int,
            "entries_compiled_at_restored": int,
            "dry_run": bool,
        }

    All writes commit in a single transaction. Failure rolls back.
    """
    from sessionfs.server.db.models import (
        ContextCompilation,
        KnowledgeEntry,
        Project,
    )

    # Codex R1 MEDIUM on tk_879dbd5a5a034d0e — Python's bool is an int
    # subclass, so a malformed body like {"compilation_id": true} would
    # otherwise coerce to compilation_id=1 and target the wrong row.
    # Reject bool explicitly. Same defensive shape on dry_run so a body
    # like {"dry_run": "false"} doesn't surprise via Python truthiness.
    compilation_id = body.get("compilation_id")
    if (
        isinstance(compilation_id, bool)
        or not isinstance(compilation_id, int)
        or compilation_id <= 0
    ):
        raise HTTPException(422, "compilation_id must be a positive integer")

    dry_run_raw = body.get("dry_run", True)
    if not isinstance(dry_run_raw, bool):
        raise HTTPException(422, "dry_run must be a boolean")
    dry_run = dry_run_raw

    # Fetch project + compilation. Cross-project safety: the compilation
    # row's project_id must match the path parameter.
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")

    compilation = (
        await db.execute(
            select(ContextCompilation).where(
                ContextCompilation.id == compilation_id,
                ContextCompilation.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if compilation is None:
        raise HTTPException(
            404,
            f"Compilation {compilation_id} not found in project {project_id}",
        )

    if not compilation.context_after:
        raise HTTPException(
            422,
            f"Compilation {compilation_id} has empty context_after — "
            f"nothing to restore from this row",
        )

    # Parse source_manifest. Schema:
    #   {section_slug: [{kb_entry_id, created_by_user_id, ...}, ...], ...}
    # We just need the kb_entry_id set for compiled_at restoration.
    entry_ids: set[int] = set()
    if compilation.source_manifest:
        try:
            manifest = json.loads(compilation.source_manifest)
        except json.JSONDecodeError:
            manifest = {}
        if isinstance(manifest, dict):
            for section_entries in manifest.values():
                if isinstance(section_entries, list):
                    for entry in section_entries:
                        if isinstance(entry, dict):
                            eid = entry.get("kb_entry_id")
                            if isinstance(eid, int):
                                entry_ids.add(eid)

    context_words_restored = len(compilation.context_after.split())

    if dry_run:
        return {
            "project_id": project_id,
            "compilation_id": compilation_id,
            "compiled_at": (
                compilation.compiled_at.isoformat()
                if compilation.compiled_at
                else None
            ),
            "context_words_restored": context_words_restored,
            "entries_compiled_at_restored": len(entry_ids),
            "dry_run": True,
        }

    # Apply: restore context_document + compiled_at, then commit once.
    # tk_bc3c02a63e994717 pattern: single atomic transaction, all-or-nothing.
    project.context_document = compilation.context_after
    project.updated_at = datetime.now(timezone.utc)

    if entry_ids:
        await db.execute(
            update(KnowledgeEntry)
            .where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.id.in_(entry_ids),
            )
            .values(compiled_at=compilation.compiled_at)
            .execution_options(synchronize_session=False)
        )

    await _log_action(
        db,
        admin.id,
        "restore_from_compilation",
        "project",
        project_id,
        {
            "compilation_id": compilation_id,
            "context_words_restored": context_words_restored,
            "entries_compiled_at_restored": len(entry_ids),
        },
    )
    await db.commit()

    return {
        "project_id": project_id,
        "compilation_id": compilation_id,
        "compiled_at": (
            compilation.compiled_at.isoformat()
            if compilation.compiled_at
            else None
        ),
        "context_words_restored": context_words_restored,
        "entries_compiled_at_restored": len(entry_ids),
        "dry_run": False,
    }
