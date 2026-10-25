"""Admin API routes for user/session management and system stats."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    require_admin,
    require_platform_sweeper,
    sweep_actor_details,
)
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ActivationAttempt,
    AdminAction,
    ApiKey,
    Handoff,
    HelmLicense,
    HelperConversation,
    HelperMessage,
    OidcLoginAttempt,
    Organization,
    OrgAuditEvent,
    OrgDigestRun,
    OrgMember,
    Project,
    ProjectTransfer,
    Session,
    SsoExchangeCode,
    Ticket,
    TicketNotification,
    TicketWatcher,
    User,
    UserNotificationSettings,
)
from sessionfs.server.services.auto_join import maybe_auto_join
from sessionfs.server.roles import OrgRole
from sessionfs.server.services.entitlements import apply_entitlement, lock_billing_owner
from sessionfs.server.services.personas import clear_represented_user
from sessionfs.server.services.publishers import advisory_xact_lock
from sessionfs.server.services.tracker_scope import drop_project_tracker_mapping

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

logger = logging.getLogger("sessionfs.admin")

VALID_TIERS = {"free", "starter", "pro", "team", "enterprise", "admin"}


# GH #84 — admin_actions.source values (migration 081).
SOURCE_API = "api"  # the server performed the action and wrote the row
SOURCE_OPERATOR_RECORD = "operator_record"  # operator-recorded, not verified

REASON_MAX_CHARS = 500
STATE_MAX_BYTES = 64 * 1024  # serialized before_state / after_state cap


def _dump_state(state: dict | None) -> str | None:
    if state is None:
        return None
    return json.dumps(state, sort_keys=True, default=str)


def _load_json(raw: str | None) -> Any:
    """Parse a stored JSON column for display. Rows can be written by hand
    (raw SQL), so a malformed value is returned as the raw string rather
    than failing the whole listing."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _clean_reason(value: Any, *, required: bool) -> str | None:
    """Validate an operator-supplied reason: a string of 1..REASON_MAX_CHARS
    characters after trimming. An optional reason may be omitted, null or
    blank (all stored as NULL)."""
    if value is None:
        if required:
            raise HTTPException(422, "reason is required")
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "reason must be a string")
    value = value.strip()
    if not value:
        if required:
            raise HTTPException(422, "reason must not be blank")
        return None
    if len(value) > REASON_MAX_CHARS:
        raise HTTPException(422, f"reason must be at most {REASON_MAX_CHARS} characters")
    return value


async def _log_action(
    db: AsyncSession,
    admin_id: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
    *,
    reason: str | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    source: str = SOURCE_API,
) -> AdminAction:
    """Record an admin action in the audit log.

    Only adds the row to the session: it commits (or rolls back) together
    with the change it describes, in the caller's transaction."""
    return _log_action_sync(
        db, admin_id, action, target_type, target_id, details,
        reason=reason, before_state=before_state, after_state=after_state, source=source,
    )


def _log_action_sync(
    db: AsyncSession,
    admin_id: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
    *,
    reason: str | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    source: str = SOURCE_API,
) -> AdminAction:
    """`_log_action` for synchronous callers (a merge engine pre-commit
    hook). Adds the row to ``db`` without awaiting anything."""
    entry = AdminAction(
        id=str(uuid.uuid4()),
        admin_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details) if details else None,
        reason=reason,
        before_state=_dump_state(before_state),
        after_state=_dump_state(after_state),
        source=source,
    )
    db.add(entry)
    return entry


def _serialize_action(a: AdminAction) -> dict:
    return {
        "id": a.id,
        "admin_id": a.admin_id,
        "action": a.action,
        "target_type": a.target_type,
        "target_id": a.target_id,
        "details": _load_json(a.details),
        "reason": a.reason,
        "before_state": _load_json(a.before_state),
        "after_state": _load_json(a.after_state),
        "source": a.source or SOURCE_API,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


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
    reason = _clean_reason(body.get("reason"), required=False)

    if user_id == admin.id and new_tier != "admin":
        raise HTTPException(status_code=400, detail="Cannot demote your own admin account")

    # Billing lock order shared with the Stripe webhooks (Stripe customer ->
    # user row -> entitlement): see services/entitlements.lock_billing_owner.
    user = await lock_billing_owner(db, "user", user_id)
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

    await _log_action(
        db, admin.id, "tier_change", "user", user_id,
        {"old_tier": old_tier, "new_tier": new_tier},
        reason=reason,
        before_state={"tier": old_tier},
        after_state={"tier": new_tier},
    )
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

    # GitHub #59 — auto-join verified-email users by domain.
    await maybe_auto_join(user, db)

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


async def _lock_offboarding_memberships(db: AsyncSession, user_id: str) -> None:
    """Lock every org_members row of a user being deactivated, FOR UPDATE in
    stable org_id order, BEFORE the user row is written.

    The marketplace publisher fences (claims, org/trusted publishes, SSO
    domain changes) hold a membership row FOR SHARE and then wait for the
    user row — membership before user. Taking the same order here means
    offboarding waits for an in-flight fence (or vice versa) instead of
    deadlocking with it."""
    await db.execute(
        select(OrgMember.id)
        .where(OrgMember.user_id == user_id)
        .order_by(OrgMember.org_id)
        .with_for_update()
    )


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a user: deactivate account and revoke all API keys.

    P4 (§2.5): before deactivation, check whether the user is the sole
    owner of any org. If another admin exists, auto-promote the
    longest-tenured admin to owner. If NO other admin exists, BLOCK
    with 409 — never silently orphan an org.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    # Serialize offboarding transactions against each other: two concurrent
    # deletions of users whose successor memberships interleave (each locks
    # the other's member row while scanning successors) can deadlock. A
    # transaction-scoped advisory lock makes offboarding single-threaded per
    # database so the successor scan below cannot interleave with another.
    # Only offboarding takes it, and it is taken before any scope lock.
    await advisory_xact_lock(db, "offboarding", "global")

    # GH #70 — membership changes follow the scope-lock protocol (user lock,
    # then the org locks of every org the user belongs to), before any row
    # lock or mutation; state is re-read under the locks.
    from sessionfs.server.services.scope_locks import lock_scopes

    # 1. The USER scope lock: every membership add/remove for this user
    #    takes it, so after this no membership of theirs can appear or
    #    disappear until we commit.
    await lock_scopes(db, user_ids=[user_id])
    # 2. Snapshot the full org set ONCE (stable under the user lock) and
    #    take all those org locks in one sorted call — never one at a time.
    member_orgs = (
        await db.execute(
            select(OrgMember.org_id).where(OrgMember.user_id == user_id)
        )
    ).scalars().all()
    await lock_scopes(db, org_ids=member_orgs)

    # 3. Row locks (after every scope lock). Shared with the marketplace
    #    publishers: every affected org_members row is locked FOR UPDATE, in
    #    stable org_id order, BEFORE the user row is written. Marketplace
    #    claim/publish fences hold a membership row FOR SHARE and then wait
    #    for the user row; writing the user row first here (is_active) and
    #    locking the memberships after (the delete below) would invert that
    #    order and deadlock PostgreSQL.
    await _lock_offboarding_memberships(db, user_id)
    await db.refresh(user)

    # P4: deactivation-time last-owner check (Sentinel MEDIUM-4).
    # Runs BEFORE is_active=False in the same transaction.
    owner_orgs = (
        await db.execute(
            select(OrgMember, Organization)
            .join(Organization, OrgMember.org_id == Organization.id)
            .where(
                OrgMember.user_id == user_id,
                OrgMember.role == "owner",
            )
        )
    ).all()

    for member, org in owner_orgs:
        # Find the longest-tenured admin who is NOT the deactivated user.
        # FOR UPDATE locks the chosen successor row so a concurrent role
        # change can't demote it between selection and promotion (Sentinel
        # L1 — consistent with the accept/force-transfer paths; the one-owner
        # index is the structural backstop).
        #
        # The literal role == "admin" is deliberate: the only owner of this
        # org is the user being deactivated (uq_org_members_one_owner_per_org,
        # and they are excluded below), so widening to admin-level roles
        # could never match another owner. The successor must be an admin
        # because it is promoted to owner — promoting an owner is meaningless.
        successor = (
            await db.execute(
                select(OrgMember)
                .where(
                    OrgMember.org_id == org.id,
                    OrgMember.role == "admin",
                    OrgMember.user_id != user_id,
                )
                .order_by(OrgMember.joined_at.asc(), OrgMember.user_id.asc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()

        if successor is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "sole_owner",
                    "org_id": org.id,
                    "org_name": org.name,
                    "message": (
                        f"Cannot deactivate — you are the sole owner-or-admin "
                        f"of org '{org.name}'. Transfer ownership or promote "
                        f"another member to admin first, or use the admin "
                        f"force-transfer endpoint."
                    ),
                },
            )

        # Auto-promote: demote old owner to admin first (so the
        # partial unique index uq_org_members_one_owner_per_org
        # never sees two owners), then promote the successor.
        await db.execute(
            update(OrgMember)
            .where(
                OrgMember.org_id == org.id,
                OrgMember.user_id == user_id,
            )
            .values(role="admin")
        )
        await db.execute(
            update(OrgMember)
            .where(
                OrgMember.org_id == org.id,
                OrgMember.user_id == successor.user_id,
            )
            .values(role="owner")
        )

        # Emit OrgAuditEvent for the auto-promotion.
        successor_user = (
            await db.execute(
                select(User).where(User.id == successor.user_id)
            )
        ).scalar_one()
        db.add(
            OrgAuditEvent(
                id=f"oae_{uuid.uuid4().hex[:16]}",
                org_id=org.id,
                org_name_snapshot=org.name,
                event_type="owner_auto_promoted_on_deactivation",
                actor_user_id=admin.id,
                actor_email_snapshot=admin.email,
                actor_role_at_time="platform_admin",
                target_type="user",
                target_id=successor.user_id,
                target_email_snapshot=successor_user.email,
                before=json.dumps({"role": "admin"}),
                after=json.dumps({"role": "owner"}),
            )
        )

    user.is_active = False

    # Revoke all API keys
    await db.execute(
        update(ApiKey).where(ApiKey.user_id == user_id).values(is_active=False)
    )

    # Remove org memberships to free seats
    await db.execute(
        delete(OrgMember).where(OrgMember.user_id == user_id)
    )
    # Remove ticket-watcher subscriptions so a deactivated user no longer
    # accumulates notification rows (unbounded growth).
    await db.execute(
        delete(TicketWatcher).where(TicketWatcher.user_id == user_id)
    )
    # GitHub #81 — and with them any org persona they represented.
    await clear_represented_user(
        db,
        user_id=user_id,
        actor_user_id=admin.id,
        actor_email=admin.email,
        actor_role="platform_admin",
        reason="user_deactivated",
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
      - owner_user_id: active user ID that becomes the org owner
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

    # Slug uniqueness
    existing = (
        await db.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"Organization slug '{slug}' is already taken")

    # GH #70 — membership add/remove: the user's scope lock (then the
    # org's), before any row lock, so it serializes with user delete,
    # member removal and other joins (scope_locks.py global order).
    from sessionfs.server.services.scope_locks import lock_scopes

    await lock_scopes(db, user_ids=[owner_user_id], org_ids=[])

    # The owner row below is re-read under the scope lock (populate_existing),
    # so a deactivation committed while we waited is seen (404 / 422).

    # Lock the user row first (the user -> org order shared with invite
    # acceptance and domain auto-join) so a concurrent membership insert for
    # this user elsewhere can't slip in between the check and the insert.
    # The owner is read only here, locked, with populate_existing so a User
    # already in the identity map is refreshed from the locked row: a
    # deactivation committed earlier must not produce an org whose only
    # owner is an inactive account.
    locked_owner = (
        await db.execute(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_owner is None:
        raise HTTPException(404, f"User {owner_user_id} not found")
    if not locked_owner.is_active:
        raise HTTPException(
            422, f"User {owner_user_id} is inactive and cannot own an organization"
        )

    # Owner can't already be in an org
    existing_member = (
        await db.execute(
            select(OrgMember).where(OrgMember.user_id == owner_user_id).limit(1)
        )
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

    # The named owner_user_id becomes the org's single owner (not admin) —
    # every org has exactly one owner (tk_d311dcf71be74a97).
    member = OrgMember(org_id=org_id, user_id=owner_user_id, role=OrgRole.OWNER.value)
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
    # Billing lock order shared with the Stripe webhooks (Stripe customer ->
    # org row -> entitlement): see services/entitlements.lock_billing_owner.
    reason = _clean_reason(body.get("reason"), required=False)
    org = await lock_billing_owner(db, "org", org_id)
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

    before = {key: getattr(org, key) for key in changes}
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

    await _log_action(
        db, admin.id, "admin_update_org", "org", org_id, changes,
        reason=reason, before_state=before, after_state=dict(changes),
    )
    await db.commit()

    return {"org_id": org_id, "updated": changes}


# P4 — Admin force-transfer: platform-admin recovery for
# deactivated/compromised/departed owners (Sentinel MEDIUM-4).


class ForceTransferRequest(BaseModel):
    to_user_id: str
    # GH #84 — optional (existing clients omit it); recorded on the
    # AdminAction row.
    reason: str | None = Field(None, max_length=REASON_MAX_CHARS)


@router.post("/orgs/{org_id}/force-transfer-owner")
async def force_transfer_owner(
    org_id: str,
    body: ForceTransferRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Force-transfer org ownership. Platform admin only.

    Recovery path when the owner is deactivated, compromised, or
    departed and no other admin can transfer ownership. The platform
    admin is the trust anchor.

    Atomic single-transaction swap: re-validate current state →
    demote old owner → promote target. AdminAction + OrgAuditEvent
    logged.
    """
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(404, "Org not found")

    # GH #70 — org scope lock before any membership read, row lock or
    # mutation; every check below re-reads state under it.
    from sessionfs.server.services.scope_locks import lock_scopes

    await lock_scopes(db, org_ids=[org_id])

    # Target must be an existing org member.
    target_member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == body.to_user_id,
            )
        )
    ).scalar_one_or_none()
    if target_member is None:
        raise HTTPException(404, "Target user is not a member of this org")

    # Find the current owner.
    current_owner = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.role == "owner",
            )
        )
    ).scalar_one_or_none()

    if current_owner is None:
        raise HTTPException(
            409, "Org has no owner — contact support to repair"
        )
    if current_owner.user_id == body.to_user_id:
        raise HTTPException(
            400, "Target user is already the owner"
        )

    # Atomic swap under FOR UPDATE.
    await db.execute(
        select(OrgMember)
        .where(
            OrgMember.org_id == org_id,
            OrgMember.role.in_(["owner", "admin"]),
        )
        .with_for_update()
    )

    # Re-validate after lock.
    current_owner = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.role == "owner",
            )
        )
    ).scalar_one_or_none()
    if current_owner is None:
        raise HTTPException(409, "Org owner changed — retry")

    old_owner_id = current_owner.user_id
    new_owner_id = body.to_user_id
    # The displaced owner is demoted to the target's pre-transfer role
    # (capture it for a self-describing audit — Shield LOW-3).
    displaced_owner_resulting_role = target_member.role

    # Demote old owner → the target's current role (usually admin or member).
    await db.execute(
        update(OrgMember)
        .where(OrgMember.org_id == org_id, OrgMember.user_id == old_owner_id)
        .values(role=target_member.role)
    )
    # Promote target → owner.
    await db.execute(
        update(OrgMember)
        .where(OrgMember.org_id == org_id, OrgMember.user_id == new_owner_id)
        .values(role="owner")
    )

    # AdminAction audit.
    await _log_action(
        db, admin.id, "force_transfer_owner", "organization", org_id,
        {"from_user_id": old_owner_id, "to_user_id": new_owner_id},
        reason=_clean_reason(body.reason, required=False),
        before_state={
            "owner_user_id": old_owner_id,
            "target_user_role": displaced_owner_resulting_role,
        },
        after_state={
            "owner_user_id": new_owner_id,
            "displaced_owner_role": displaced_owner_resulting_role,
        },
    )

    # OrgAuditEvent.
    new_owner_user = (
        await db.execute(select(User).where(User.id == new_owner_id))
    ).scalar_one_or_none()
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type="owner_force_transferred",
            actor_user_id=admin.id,
            actor_email_snapshot=admin.email,
            actor_role_at_time="platform_admin",
            target_type="user",
            target_id=new_owner_id,
            target_email_snapshot=new_owner_user.email if new_owner_user else None,
            before=json.dumps(
                {"from_user_id": old_owner_id, "to_user_id": new_owner_id}
            ),
            after=json.dumps(
                {
                    "new_owner_user_id": new_owner_id,
                    "displaced_owner_user_id": old_owner_id,
                    "displaced_owner_resulting_role": displaced_owner_resulting_role,
                }
            ),
        )
    )

    await db.commit()

    return {
        "status": "force_transferred",
        "org_id": org_id,
        "from_user_id": old_owner_id,
        "to_user_id": new_owner_id,
    }


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
    target_type: str | None = Query(None, max_length=20),
    target_id: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=50),
    source: str | None = Query(None, max_length=20),
    admin_id: str | None = Query(None, max_length=36),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List recent admin actions, newest first.

    Optional exact-match filters: target_type, target_id, action, source
    ('api' | 'operator_record') and admin_id."""
    filters = []
    if target_type:
        filters.append(AdminAction.target_type == target_type)
    if target_id:
        filters.append(AdminAction.target_id == target_id)
    if action:
        filters.append(AdminAction.action == action)
    if source:
        filters.append(AdminAction.source == source)
    if admin_id:
        filters.append(AdminAction.admin_id == admin_id)

    count_q = select(func.count()).select_from(AdminAction).where(*filters)
    total = (await db.execute(count_q)).scalar() or 0

    offset = (page - 1) * page_size
    query = (
        select(AdminAction)
        .where(*filters)
        .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    items = [_serialize_action(a) for a in result.scalars().all()]

    return {"total": total, "page": page, "page_size": page_size, "actions": items}


# GH #84 — operator-recorded actions. Operators with database access will
# sometimes change data by hand; this endpoint lets them leave the same
# audit row the paved paths write, marked source='operator_record' because
# the server did not perform (or verify) the change.

_ACTION_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"
_TARGET_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,19}$"
# No control characters, no leading/trailing whitespace.
_TARGET_ID_PATTERN = r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]{0,62}[^\s\x00-\x1f\x7f])?$"


def _check_state_size(name: str, state: dict | None) -> None:
    if state is None:
        return
    size = len(json.dumps(state, sort_keys=True, default=str).encode("utf-8"))
    if size > STATE_MAX_BYTES:
        raise HTTPException(
            422,
            f"{name} is {size} bytes serialized; the limit is {STATE_MAX_BYTES} bytes",
        )


class RecordAdminActionRequest(BaseModel):
    action: str = Field(..., pattern=_ACTION_PATTERN)
    target_type: str = Field(..., pattern=_TARGET_TYPE_PATTERN)
    target_id: str = Field(..., pattern=_TARGET_ID_PATTERN)
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_CHARS)
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


@router.post("/audit-log", status_code=201)
async def record_admin_action(
    body: RecordAdminActionRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record an action an operator performed outside the API.

    The row is stored with source='operator_record': the server attests
    only WHO recorded it and WHEN, not that the described change happened.
    """
    _check_state_size("before_state", body.before_state)
    _check_state_size("after_state", body.after_state)
    entry = await _log_action(
        db,
        admin.id,
        body.action,
        body.target_type,
        body.target_id,
        reason=body.reason,
        before_state=body.before_state,
        after_state=body.after_state,
        source=SOURCE_OPERATOR_RECORD,
    )
    await db.commit()
    await db.refresh(entry)
    return _serialize_action(entry)


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


# Default retention window for terminal/expired activation attempts.
_ACTIVATION_ATTEMPT_RETENTION_DAYS = 30


@router.post("/activation-attempts/purge")
async def purge_activation_attempts(
    body: dict | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reap stale license-activation attempts (data minimization).

    Two steps:
      1. Lazily flip any expired-but-still-'pending' attempts to 'expired'
         (the declared status is otherwise never set — the verify path only
         filters on expires_at). Keying off expires_at makes expired rows
         distinguishable + reapable.
      2. Delete terminal/expired attempts older than the retention window,
         which removes the `contact_email_snapshot` PII + token_hash with the
         row. Default window 30 days; override with body {"retention_days": N}.
    """
    now = datetime.now(timezone.utc)
    retention_days = _ACTIVATION_ATTEMPT_RETENTION_DAYS
    if body and isinstance(body.get("retention_days"), int):
        retention_days = max(0, body["retention_days"])
    cutoff = now - timedelta(days=retention_days)

    # 1. Mark expired pending attempts as 'expired'.
    flipped = await db.execute(
        update(ActivationAttempt)
        .where(
            ActivationAttempt.status == "pending",
            ActivationAttempt.expires_at < now,
        )
        .values(status="expired")
    )

    # 2. Delete terminal (consumed|expired) attempts older than the window.
    del_result = await db.execute(
        delete(ActivationAttempt).where(
            ActivationAttempt.status.in_(("consumed", "expired")),
            ActivationAttempt.created_at < cutoff,
        )
    )
    purged = del_result.rowcount or 0

    await _log_action(
        db,
        admin.id,
        "purge_activation_attempts",
        "activation_attempt",
        "bulk",
        {"purged": purged, "expired_flipped": flipped.rowcount or 0,
         "retention_days": retention_days},
    )
    await db.commit()

    return {"purged": purged, "expired_flipped": flipped.rowcount or 0}


# Default retention window for accepted GitHub webhook delivery ids.
_GITHUB_WEBHOOK_DELIVERY_RETENTION_DAYS = 30  # stored payloads
_GITHUB_WEBHOOK_TOMBSTONE_RETENTION_DAYS = 400  # replay fingerprints


@router.post("/github-webhook-deliveries/sweep")
@router.post("/github-webhook-deliveries/purge")
async def sweep_github_webhook_deliveries(
    body: dict | None = None,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Process and purge GitHub webhook deliveries; reap dead claim attempts.

    Accepts a platform-admin user key or a ``platform_sweeps:run`` service
    key (the scheduled job). The service key may only set ``retry_limit``:
    the retention overrides stay with an admin user key.

    0. Retry due deliveries (failed with backoff elapsed, or stale
       processing) from their stored payloads, up to body {"retry_limit": N}
       (default 50). The server has no scheduler; webhook deliveries also
       retry a couple opportunistically.
    0b. Republish created PR comments that are behind their newest desired
       version and have no live publishing lease (same limit).
    1. Delete github_webhook_deliveries older than the retention window
       (default 30 days; override with body {"retention_days": N}); stored
       payloads go with them.
    2. Delete github_claim_attempts that are expired or consumed (their
       state/nonce/verifier hashes and any parked chooser go with them);
       'processing' attempts survive a 10-minute grace so an in-flight
       callback never loses its row mid-flight.
    3. Abort expired pending session uploads and delete their staged parts.
    """
    from sessionfs.server.db.models import GitHubClaimAttempt
    from sessionfs.server.routes.webhooks import (
        prune_deliveries,
        republish_dirty_comments,
        retry_due_deliveries,
    )

    if auth.key_kind == "service" and body and (
        "retention_days" in body or "tombstone_retention_days" in body
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "sweep_override_not_allowed",
                "message": "Retention overrides need a platform-admin user key.",
            },
        )
    retry_limit = 50
    if body and isinstance(body.get("retry_limit"), int) and not isinstance(
        body.get("retry_limit"), bool
    ):
        retry_limit = max(0, min(500, body["retry_limit"]))
    # End the request session's transaction (opened by authentication)
    # before any GitHub work: retries and republishing use their own short
    # sessions, and pruning + audit below run in a fresh short transaction.
    admin_id = auth.user.id
    actor = sweep_actor_details(auth)
    await db.rollback()
    retry_stats = await retry_due_deliveries(retry_limit)
    comment_stats = await republish_dirty_comments(retry_limit)

    now = datetime.now(timezone.utc)
    retention_days = _GITHUB_WEBHOOK_DELIVERY_RETENTION_DAYS
    if body and isinstance(body.get("retention_days"), int) and not isinstance(
        body.get("retention_days"), bool
    ):
        retention_days = max(0, body["retention_days"])
    # Tombstones (replay fingerprints) are kept far longer than payloads and
    # can never be configured below the payload window.
    tombstone_days = _GITHUB_WEBHOOK_TOMBSTONE_RETENTION_DAYS
    if body and isinstance(body.get("tombstone_retention_days"), int) and not isinstance(
        body.get("tombstone_retention_days"), bool
    ):
        tombstone_days = body["tombstone_retention_days"]
    # Always: a tombstone never expires before the payload it guards.
    tombstone_days = max(tombstone_days, retention_days)

    payloads_purged, tombstones_purged = await prune_deliveries(
        db,
        now=now,
        payload_retention=timedelta(days=retention_days),
        tombstone_retention=timedelta(days=tombstone_days),
    )
    # 'processing' attempts (consumed, callback still working) survive
    # until their grace has passed — mirrors _create_attempt housekeeping
    # in routes/settings.py.
    from sessionfs.server.routes.settings import (
        GITHUB_CLAIM_PROCESSING_GRACE_MINUTES,
    )

    processing_grace = now - timedelta(minutes=GITHUB_CLAIM_PROCESSING_GRACE_MINUTES)
    attempts = await db.execute(
        delete(GitHubClaimAttempt)
        .where(
            # A 'processing' attempt is single-use work in flight: its
            # expiry alone must not delete it while it is inside its
            # grace (the callback may still be finishing).
            (
                (GitHubClaimAttempt.status != "processing")
                & (GitHubClaimAttempt.expires_at < now)
            )
            | (GitHubClaimAttempt.status == "consumed")
            | (
                (GitHubClaimAttempt.status == "processing")
                & (
                    func.coalesce(
                        GitHubClaimAttempt.consumed_at,
                        GitHubClaimAttempt.created_at,
                    ) < processing_grace
                )
            )
        )
        .execution_options(synchronize_session=False)
    )
    # Abort expired pending session uploads and delete their staged parts.
    from sessionfs.server.routes.uploads import abort_expired_pending_uploads

    uploads_aborted = await abort_expired_pending_uploads(
        db, request.app.state.blob_store, now
    )

    result = {
        "payloads_purged": payloads_purged,
        "deliveries_purged": tombstones_purged,
        "claim_attempts_purged": attempts.rowcount or 0,
        "uploads_aborted": uploads_aborted,
        "deliveries_retried": retry_stats["retried"],
        "retries_succeeded": retry_stats["succeeded"],
        "retries_failed": retry_stats["failed"],
        **comment_stats,
    }
    await _log_action(
        db,
        admin_id,
        "purge_github_webhook_deliveries",
        "gh_webhook_delivery",  # admin_actions.target_type is VARCHAR(20)
        "bulk",
        {**result, "retention_days": retention_days, "tombstone_retention_days": tombstone_days,
         **actor},
    )
    await db.commit()
    return result


def _digest_dashboard_url(config) -> str:
    """Dashboard base URL for the digest's filtered-view links.

    Resolution order: ``SFS_DASHBOARD_URL`` (``config.dashboard_url``), then
    ``config.app_url`` (what invite emails already use), then ``""`` which
    means "skip links". There is no hosted-dashboard fallback so self-hosted
    installs never link recipients to the hosted app.
    """
    explicit_app_url = "app_url" in getattr(config, "model_fields_set", set()) or bool(
        os.environ.get("SFS_APP_URL")
    )
    return str(
        getattr(config, "dashboard_url", None)
        or (getattr(config, "app_url", None) if explicit_app_url else None)
        or ""
    ).strip().rstrip("/")


@router.post("/sweeps/org-digest")
async def sweep_org_digest(
    request: Request,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
):
    """Send the weekly org activity digest (GitHub #97).

    Accepts a platform-admin user key or a ``platform_sweeps:run`` service
    key (the scheduled job). For every Team/Enterprise org with activity in
    the most recently completed Monday-to-Monday week, builds one digest and
    emails each non-opted-out owner/admin. Idempotent per org per week via
    the ``org_digest_runs`` primary key: the run row is reserved with an
    ON CONFLICT guard BEFORE any email is sent, so re-running the same week
    sends nothing. The email provider "none" is a no-op that still records
    the run (air-gapped/self-hosted). Recipients are masked in logs.
    """
    from sessionfs.server.email import create_email_provider, mask_address
    from sessionfs.server.services.org_digest import build_org_digest, digest_week
    from sessionfs.server.tier_gate import resolve_org_plan_tier
    from sessionfs.server.tiers import Tier

    config = getattr(request.app.state, "config", None)
    dashboard_url = _digest_dashboard_url(config)
    provider = create_email_provider(config)

    admin_id = auth.user.id
    actor = sweep_actor_details(auth)
    now = datetime.now(timezone.utc)
    week_start, _, _ = digest_week(now)
    week_end = week_start + timedelta(days=6)
    week_label = f"{week_start.isoformat()} – {week_end.isoformat()}"

    # The request session opened a transaction during authentication; end it
    # before per-org commits below so the reservation commits are isolated.
    await db.rollback()

    # Materialize (id, name) as plain strings up front: the per-org error
    # handler rolls the session back, which expires ORM objects, so the loop
    # must not rely on lazy attribute loads from the org rows.
    orgs = (await db.execute(select(Organization.id, Organization.name))).all()
    sent_orgs: list[dict] = []
    skipped_orgs: list[dict] = []
    recipient_total = 0

    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    already_reserved = set(
        (
            await db.execute(
                select(OrgDigestRun.org_id).where(OrgDigestRun.week_start == week_start)
            )
        ).scalars()
    )

    for org_id, org_name in orgs:
        reserved = False
        sent = 0
        try:
            if org_id in already_reserved:
                skipped_orgs.append({"org_id": org_id, "reason": "already_sent"})
                continue
            tier = await resolve_org_plan_tier(org_id, db)
            if tier not in (Tier.TEAM, Tier.ENTERPRISE):
                continue

            digest = await build_org_digest(db, org_id, week_start)
            if not _digest_has_activity(digest):
                continue

            recipients = await _digest_recipients(db, org_id)
            if not recipients:
                continue

            # Reserve the (org, week) slot before sending. rowcount == 0 means
            # another run already sent this week, so skip to avoid a double send.
            stmt = dialect_insert(OrgDigestRun).values(
                org_id=org_id,
                week_start=week_start,
                sent_at=now,
                recipient_count=len(recipients),
                sent_count=0,
            ).on_conflict_do_nothing(index_elements=["org_id", "week_start"])
            result = await db.execute(stmt)
            if result.rowcount == 0:
                skipped_orgs.append({"org_id": org_id, "reason": "already_sent"})
                continue
            await db.commit()  # durable reservation before any send
            reserved = True

            for email, member_id in recipients:
                outcome = await provider.send_org_digest(
                    to_email=email,
                    org_name=org_name,
                    week_label=week_label,
                    digest=digest,
                    dashboard_url=dashboard_url,
                )
                if outcome.get("status") == "sent":
                    sent += 1
                else:
                    logger.warning(
                        "org digest send failed to member %s", mask_address(email)
                    )
            recipient_total += sent

            if sent == 0:
                # Every recipient failed: release the reservation so a later
                # run this week retries instead of silently losing the week.
                await db.execute(
                    delete(OrgDigestRun).where(
                        OrgDigestRun.org_id == org_id,
                        OrgDigestRun.week_start == week_start,
                    )
                )
                await db.commit()
                skipped_orgs.append({"org_id": org_id, "reason": "send_failed"})
                continue

            await db.execute(
                update(OrgDigestRun)
                .where(
                    OrgDigestRun.org_id == org_id,
                    OrgDigestRun.week_start == week_start,
                )
                .values(sent_count=sent)
            )
            await db.commit()
            sent_orgs.append(
                {"org_id": org_id, "recipients": len(recipients), "sent": sent}
            )
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "org digest failed for org %s: %s", org_id, type(exc).__name__
            )
            if reserved:
                # The reservation is already committed: release it when nobody
                # got the email so a later run retries, else record what went out.
                try:
                    where = (
                        OrgDigestRun.org_id == org_id,
                        OrgDigestRun.week_start == week_start,
                    )
                    if sent == 0:
                        await db.execute(delete(OrgDigestRun).where(*where))
                    else:
                        await db.execute(
                            update(OrgDigestRun).where(*where).values(sent_count=sent)
                        )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.warning(
                        "org digest could not settle the reservation for org %s",
                        org_id,
                    )
            skipped_orgs.append({"org_id": org_id, "reason": "error"})
            continue

    result_payload = {
        "week_start": week_start.isoformat(),
        "orgs_processed": len(sent_orgs),
        "orgs_skipped": len(skipped_orgs),
        "recipients": recipient_total,
        "orgs": sent_orgs,
        "skipped": skipped_orgs,
    }
    await _log_action(
        db,
        admin_id,
        "run_org_digest",
        "org_digest",
        "bulk",
        {**result_payload, **actor},
    )
    await db.commit()
    return result_payload


# Ticket-notification email sweep: only notify rows older than this so a burst
# of coalesced events settles into one email instead of one-per-keystroke.
_TICKET_EMAIL_MIN_AGE = timedelta(minutes=10)
# Bounded work per sweep run, and the retention window for stale notifications.
_TICKET_SWEEP_LIMIT = 5000
# Candidate USERS per sweep run: selecting users (not rows) means one user with
# a huge backlog can't fill the row limit and starve everyone behind them.
_TICKET_SWEEP_USER_LIMIT = 500
_TICKET_RETENTION_DAYS = 90


async def _mark_notifications_emailed(
    db: AsyncSession, user_id: str, ids: list[int], now: datetime
) -> int:
    """Claim rows for delivery. The ``emailed_at IS NULL`` guard keeps this
    idempotent under a concurrent sweep: the second run's rowcount is zero."""
    if not ids:
        return 0
    result = await db.execute(
        update(TicketNotification)
        .where(
            TicketNotification.id.in_(ids),
            TicketNotification.user_id == user_id,
            TicketNotification.emailed_at.is_(None),
        )
        .values(emailed_at=now)
    )
    return result.rowcount


async def _claim_notifications(
    db: AsyncSession, user_id: str, ids: list[int], now: datetime
) -> list[int]:
    """Claim rows for delivery, returning the ids this run actually won.

    The digest must be built from the claimed ids (not the pre-claim
    selection), otherwise two overlapping sweeps both send the same rows. On
    PostgreSQL a single ``UPDATE ... RETURNING id`` does this atomically; on
    SQLite (no ``UPDATE ... RETURNING``) rows are claimed one at a time and
    kept only when ``rowcount == 1``.
    """
    if not ids:
        return []
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        result = await db.execute(
            update(TicketNotification)
            .where(
                TicketNotification.id.in_(ids),
                TicketNotification.user_id == user_id,
                TicketNotification.emailed_at.is_(None),
            )
            .values(emailed_at=now)
            .returning(TicketNotification.id)
        )
        return [row_id for (row_id,) in result.fetchall()]
    claimed: list[int] = []
    for nid in ids:
        result = await db.execute(
            update(TicketNotification)
            .where(
                TicketNotification.id == nid,
                TicketNotification.user_id == user_id,
                TicketNotification.emailed_at.is_(None),
            )
            .values(emailed_at=now)
        )
        if result.rowcount == 1:
            claimed.append(nid)
    return claimed


@router.post("/sweeps/ticket-notifications")
async def sweep_ticket_notifications(
    request: Request,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
):
    """Email users about watched tickets that changed (ticket watchers).

    Accepts a platform-admin user key or a ``platform_sweeps:run`` service
    key (the scheduled job). Selects unread, un-emailed notifications older
    than 10 minutes and sends each user ONE digest email. Access is re-checked
    per row against the ticket's current project, so a user who lost access
    since the notification was written is never emailed its content. Per-user
    ``ticket_email`` (off | your_turn | all, default your_turn) gates delivery:
    ``off`` never emails, ``your_turn`` only emails when at least one pending
    notification is a your_turn, ``all`` emails every batch. Recipients are
    masked in logs. Idempotent via ``emailed_at IS NULL`` rowcount guards.
    """
    from sessionfs.server.auth.project_access import user_can_access_project
    from sessionfs.server.email import create_email_provider, mask_address

    config = getattr(request.app.state, "config", None)
    provider = create_email_provider(config)
    dashboard_url = _digest_dashboard_url(config)

    admin_id = auth.user.id
    actor = sweep_actor_details(auth)
    now = datetime.now(timezone.utc)
    cutoff = now - _TICKET_EMAIL_MIN_AGE

    # End the auth transaction; each per-user delivery commits independently.
    await db.rollback()

    # Select candidate USERS (not rows) so a few users with large backlogs
    # can't fill the row limit and starve everyone behind them. Ordering by
    # each user's oldest deliverable row preserves global age order at user
    # granularity; each user's rows are then fetched and claimed per user.
    user_ids = (
        await db.execute(
            select(TicketNotification.user_id)
            .join(Ticket, Ticket.id == TicketNotification.ticket_id)
            .where(
                TicketNotification.read_at.is_(None),
                TicketNotification.emailed_at.is_(None),
                TicketNotification.updated_at <= cutoff,
            )
            .group_by(TicketNotification.user_id)
            .order_by(func.min(TicketNotification.id).asc())
            .limit(_TICKET_SWEEP_USER_LIMIT)
        )
    ).scalars().all()

    users = {
        u.id: u
        for u in (
            await db.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars()
    }
    settings = {
        s.user_id: s
        for s in (
            await db.execute(
                select(UserNotificationSettings).where(
                    UserNotificationSettings.user_id.in_(user_ids)
                )
            )
        ).scalars()
    }

    sent = 0
    skipped_pref = 0
    skipped_access = 0
    errors: list[dict] = []

    for user_id in user_ids:
        items = (
            await db.execute(
                select(
                    TicketNotification,
                    Ticket.title,
                    Ticket.project_id,
                )
                .join(Ticket, Ticket.id == TicketNotification.ticket_id)
                .where(
                    TicketNotification.user_id == user_id,
                    TicketNotification.read_at.is_(None),
                    TicketNotification.emailed_at.is_(None),
                    TicketNotification.updated_at <= cutoff,
                )
                .order_by(TicketNotification.id.asc())
                .limit(_TICKET_SWEEP_LIMIT)
            )
        ).all()
        ids = [n.id for n, _, _ in items]
        try:
            user = users.get(user_id)
            if user is None or not user.is_active or not user.email:
                await _mark_notifications_emailed(db, user_id, ids, now)
                await db.commit()
                skipped_pref += len(items)
                continue

            # Re-check tenant isolation once per (user, project), not per row.
            visible: list[tuple[TicketNotification, str | None, str]] = []
            invisible_ids: list[int] = []
            project_access: dict[str, bool] = {}
            for notification, title, project_id in items:
                if project_id not in project_access:
                    project = await db.get(Project, project_id)
                    project_access[project_id] = (
                        project is not None
                        and await user_can_access_project(db, user_id, project)
                    )
                if project_access[project_id]:
                    visible.append((notification, title, project_id))
                else:
                    invisible_ids.append(notification.id)
            # Inaccessible rows are consumed (never delivered) so they do not
            # resurface on later sweeps.
            if invisible_ids:
                await _mark_notifications_emailed(db, user_id, invisible_ids, now)
            if not visible:
                await db.commit()
                skipped_access += len(items)
                continue

            mode = settings.get(user_id)
            ticket_email = mode.ticket_email if mode is not None else "your_turn"
            if ticket_email == "off":
                await _mark_notifications_emailed(
                    db, user_id, [n.id for n, _, _ in visible], now
                )
                await db.commit()
                skipped_pref += len(visible)
                continue
            if ticket_email == "your_turn" and not any(
                n.kind == "your_turn" for n, _, _ in visible
            ):
                await _mark_notifications_emailed(
                    db, user_id, [n.id for n, _, _ in visible], now
                )
                await db.commit()
                skipped_pref += len(visible)
                continue

            # Claim the visible rows for delivery; ``claimed_ids`` is exactly
            # what this run won, so two overlapping sweeps never both email the
            # same row.
            claimed_ids = await _claim_notifications(
                db, user_id, [n.id for n, _, _ in visible], now
            )
            await db.commit()
            if not claimed_ids:
                skipped_pref += len(visible)
                continue

            claimed_lookup = set(claimed_ids)
            digest = [
                {
                    "ticket_title": title,
                    "ticket_id": n.ticket_id,
                    "project_id": project_id,
                    "kind": n.kind,
                    "summary": n.summary,
                }
                for n, title, project_id in visible
                if n.id in claimed_lookup
            ]
            outcome = await provider.send_ticket_notification(
                to_email=user.email,
                notifications=digest,
                dashboard_url=dashboard_url,
            )
            if outcome.get("status") == "sent":
                sent += 1
            else:
                # Release only the rows THIS run claimed, guarded by
                # ``emailed_at == now`` so a concurrent run's reservation is
                # never released.
                await db.execute(
                    update(TicketNotification)
                    .where(
                        TicketNotification.id.in_(claimed_ids),
                        TicketNotification.emailed_at == now,
                    )
                    .values(emailed_at=None)
                )
                await db.commit()
                logger.warning(
                    "ticket notification email failed to %s",
                    mask_address(user.email),
                )
                errors.append({"user_id": user_id, "reason": "send_failed"})
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "ticket notification sweep failed for user %s: %s",
                user_id,
                type(exc).__name__,
            )
            errors.append({"user_id": user_id, "reason": "error"})

    # Retention: purge notifications delivered through at least one channel
    # (read or emailed) that are older than the retention window. Split into
    # two bounded deletes (read-and-old, emailed-and-old) so each can use a
    # dedicated (column, updated_at) index instead of one OR'd full scan.
    retention_cutoff = now - timedelta(days=_TICKET_RETENTION_DAYS)
    read_stale_ids = (
        await db.execute(
            select(TicketNotification.id)
            .where(
                TicketNotification.updated_at < retention_cutoff,
                TicketNotification.read_at.is_not(None),
            )
            .order_by(TicketNotification.id.asc())
            .limit(_TICKET_SWEEP_LIMIT)
        )
    ).scalars().all()
    if read_stale_ids:
        await db.execute(
            delete(TicketNotification).where(
                TicketNotification.id.in_(list(read_stale_ids))
            )
        )
    emailed_stale_ids = (
        await db.execute(
            select(TicketNotification.id)
            .where(
                TicketNotification.updated_at < retention_cutoff,
                TicketNotification.emailed_at.is_not(None),
                # Read rows were handled above; partition so a row flagged both
                # read and emailed is counted (and deleted) exactly once.
                TicketNotification.read_at.is_(None),
            )
            .order_by(TicketNotification.id.asc())
            .limit(_TICKET_SWEEP_LIMIT)
        )
    ).scalars().all()
    if emailed_stale_ids:
        await db.execute(
            delete(TicketNotification).where(
                TicketNotification.id.in_(list(emailed_stale_ids))
            )
        )
    retention_deleted = len(read_stale_ids) + len(emailed_stale_ids)

    result_payload = {
        "users_emailed": sent,
        "notifications_skipped_preference": skipped_pref,
        "notifications_skipped_access": skipped_access,
        "notifications_deleted_retention": retention_deleted,
        "errors": errors,
    }
    await _log_action(
        db,
        admin_id,
        "run_ticket_notifications_sweep",
        "ticket_notification",
        "bulk",
        {**result_payload, **actor},
    )
    await db.commit()
    return result_payload


def _digest_has_activity(digest: dict) -> bool:
    """A digest is worth sending when at least one org-scoped metric moved."""
    return bool(
        digest["sessions"]["total"]
        or digest["tickets"]["opened"]
        or digest["tickets"]["completed"]
        or digest["agent_runs"]["total"]
        or digest["knowledge"]["claims_added"]
        or digest["knowledge"]["claims_promoted"]
        or digest["knowledge"]["claims_dismissed"]
        or digest["handoffs"]["created"]
        or digest["handoffs"]["claimed"]
        or digest["admin_actions"]["total"]
    )


async def _digest_recipients(
    db: AsyncSession, org_id: str
) -> list[tuple[str, str]]:
    """Owner/admins who have not opted out, with a deliverable email."""
    rows = (
        await db.execute(
            select(OrgMember, User)
            .join(User, OrgMember.user_id == User.id)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.role.in_(("owner", "admin")),
                OrgMember.digest_opt_out.is_(False),
                User.email.is_not(None),
                User.is_active.is_(True),
            )
        )
    ).all()
    return [(u.email, m.user_id) for m, u in rows]


@router.post("/sweeps/kb-compile")
async def sweep_kb_compile(
    body: dict | None = None,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
):
    """Run the scheduled knowledge-base compile sweep (GitHub #93).

    Compiles every project whose schedule is due — or whose pending-claim
    threshold is reached — with the deterministic compiler (never the LLM).
    Accepts a platform-admin user key or a ``platform_sweeps:run`` service
    key. Optional body {"limit": N} caps how many projects are compiled
    (default 100, max 1000).

    Each project compiles in its own transaction; a failure is reported in
    ``errors`` and does not stop the batch. Safe to run repeatedly: a
    compiled project advances ``last_scheduled_compile_at`` and is not
    selected again until its schedule lapses.
    """
    from sessionfs.server.db.engine import _session_factory
    from sessionfs.server.services.scheduled_compile import run_kb_compile_sweep

    limit = 100
    if body and isinstance(body.get("limit"), int) and not isinstance(
        body.get("limit"), bool
    ):
        limit = max(1, min(1000, body["limit"]))
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    admin_id = auth.user.id
    actor = sweep_actor_details(auth)
    # End the request session's auth transaction; the sweep selects in a
    # fresh short transaction and compiles each project in its own session.
    await db.rollback()
    result = await run_kb_compile_sweep(
        db,
        limit=limit,
        session_factory=_session_factory,
    )
    await _log_action(
        db,
        admin_id,
        "kb_compile_sweep",
        "project",
        "bulk",
        {**result, **actor},
    )
    await db.commit()
    return result


# Default retention window (days) for session-helper messages when an org has
# not overridden it via ``organizations.settings`` → ``general.helper_retention_days``.
_HELPER_RETENTION_DAYS = 90

# Max rows hard-deleted per transaction during the retention sweep, so a large
# backlog is drained in bounded batches instead of one unbounded transaction.
_HELPER_RETENTION_BATCH = 1000


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime to aware UTC (SQLite returns naive datetimes)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _helper_retention_days_from_settings(settings: str | dict | None) -> int:
    """Read ``general.helper_retention_days`` from raw settings JSON (default 90, min 1)."""
    if isinstance(settings, str):
        try:
            parsed = json.loads(settings)
        except (TypeError, ValueError):
            return _HELPER_RETENTION_DAYS
        settings = parsed
    if not isinstance(settings, dict):
        return _HELPER_RETENTION_DAYS
    general = settings.get("general")
    days = (
        general.get("helper_retention_days", _HELPER_RETENTION_DAYS)
        if isinstance(general, dict)
        else _HELPER_RETENTION_DAYS
    )
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        return _HELPER_RETENTION_DAYS
    return days


def _helper_retention_days(org: Organization) -> int:
    """Read an org's ``general.helper_retention_days`` setting (default 90, min 1)."""
    return _helper_retention_days_from_settings(org.settings)


@router.post("/sweeps/helper-retention")
async def sweep_helper_retention(
    body: dict | None = None,
    batch_size: int = Query(default=_HELPER_RETENTION_BATCH, ge=1, le=10000),
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete helper messages past their retention window.

    Retention is per message (not per conversation): each message expires once
    its ``created_at`` is older than its conversation org's
    ``general.helper_retention_days`` (default 90); personal conversations use
    the same default. Conversations left with no messages are then purged, but
    only once their ``updated_at`` is at least an hour old, so a thread that is
    still being written to (or was just created) is never purged mid-flight.
    Deletes run in bounded batches and never load every expired id into memory
    (a keyset pages through candidates one batch at a time). Runs as the
    scheduled retention sweep — platform-admin user key or the
    ``platform_sweeps:run`` service key.
    """
    now = datetime.now(timezone.utc)
    batch = batch_size

    # Retention window per org (settings["general"]["helper_retention_days"]).
    org_days: dict[str, int] = {}
    for org_id, settings in (
        await db.execute(select(Organization.id, Organization.settings))
    ).all():
        org_days[org_id] = _helper_retention_days_from_settings(settings)

    # Expired messages are filtered in Python (per-org windows can't be
    # expressed portably across SQLite/Postgres inside the settings JSON) and
    # hard-deleted one batch at a time: page through candidates with a keyset
    # on id, delete the page's expired ids, repeat until a short page (N5d).
    messages_deleted = 0
    cursor: str | None = None
    while True:
        stmt = (
            select(
                HelperMessage.id,
                HelperMessage.created_at,
                HelperConversation.org_id,
            )
            .join(
                HelperConversation,
                HelperMessage.conversation_id == HelperConversation.id,
            )
            .order_by(HelperMessage.id.asc())
            .limit(batch)
        )
        if cursor is not None:
            stmt = stmt.where(HelperMessage.id > cursor)
        page = (await db.execute(stmt)).all()
        if not page:
            break
        expired_ids = [
            message_id
            for message_id, created_at, org_id in page
            if created_at is not None
            and _as_utc(created_at)
            < now - timedelta(days=org_days.get(org_id, _HELPER_RETENTION_DAYS))
        ]
        if expired_ids:
            result = await db.execute(
                delete(HelperMessage).where(HelperMessage.id.in_(expired_ids))
            )
            messages_deleted += result.rowcount or 0
            await db.commit()
        cursor = page[-1][0]
        if len(page) < batch:
            break

    # Purge conversations the per-message sweep emptied (explicit delete: the
    # FK cascade only fires when a conversation row is removed, not its
    # children), but leave any conversation touched within the last hour.
    conversations_purged = 0
    cursor = None
    while True:
        stmt = (
            select(HelperConversation.id)
            .outerjoin(
                HelperMessage,
                HelperMessage.conversation_id == HelperConversation.id,
            )
            .where(HelperMessage.id.is_(None))
            .where(HelperConversation.updated_at < now - timedelta(hours=1))
            .order_by(HelperConversation.id.asc())
            .limit(batch)
        )
        if cursor is not None:
            stmt = stmt.where(HelperConversation.id > cursor)
        page = (await db.execute(stmt)).scalars().all()
        if not page:
            break
        result = await db.execute(
            delete(HelperConversation).where(HelperConversation.id.in_(page))
        )
        conversations_purged += result.rowcount or 0
        await db.commit()
        cursor = page[-1]
        if len(page) < batch:
            break

    await _log_action(
        db,
        auth.user.id,
        "helper_retention_sweep",
        "helper_conversation",
        "bulk",
        {
            **sweep_actor_details(auth),
            "purged": conversations_purged,
            "messages_deleted": messages_deleted,
        },
    )
    await db.commit()
    return {"purged": conversations_purged, "messages_deleted": messages_deleted}


@router.post("/github-webhook-deliveries/{fingerprint}/retry")
async def force_retry_github_webhook_delivery(
    fingerprint: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin override: retry one delivery now, even past its attempt limit
    or backoff (a plain GitHub redelivery cannot bypass either)."""
    import re as _re

    from sessionfs.server.routes.webhooks import force_retry_delivery

    if not _re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise HTTPException(400, "Invalid delivery fingerprint")
    admin_id = admin.id
    await db.rollback()  # no request transaction across the retry's GitHub work
    outcome = await force_retry_delivery(fingerprint)
    await _log_action(
        db,
        admin_id,
        "force_retry_github_webhook_delivery",
        "gh_webhook_delivery",
        fingerprint[:64],
        {"outcome": outcome},
    )
    await db.commit()
    if outcome == "not_found":
        raise HTTPException(404, "Delivery not found")
    return {"fingerprint": fingerprint, "outcome": outcome}


# Default retention window for terminal/expired OIDC SSO login attempts.
_OIDC_LOGIN_ATTEMPT_RETENTION_DAYS = 30


@router.post("/oidc-login-attempts/purge")
async def purge_oidc_login_attempts(
    body: dict | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reap stale OIDC SSO login attempts (data minimization).

    Mirrors the activation-attempt sweep. Two steps:
      1. Lazily flip any expired-but-still-'pending' attempts to 'expired'
         (the callback path only filters on expires_at via the atomic
         consume, so the declared status is otherwise never set for
         abandoned attempts — keying off expires_at makes them reapable).
      2. Delete terminal (consumed|expired) attempts older than the
         retention window, removing the state/nonce/pkce_verifier_hash with
         the row. Default window 30 days; override with body {"retention_days": N}.
    """
    now = datetime.now(timezone.utc)
    retention_days = _OIDC_LOGIN_ATTEMPT_RETENTION_DAYS
    if body and isinstance(body.get("retention_days"), int):
        retention_days = max(0, body["retention_days"])
    cutoff = now - timedelta(days=retention_days)

    # 1. Mark expired pending attempts as 'expired'.
    flipped = await db.execute(
        update(OidcLoginAttempt)
        .where(
            OidcLoginAttempt.status == "pending",
            OidcLoginAttempt.expires_at < now,
        )
        .values(status="expired")
    )

    # 2. Delete terminal (consumed|expired) attempts older than the window.
    del_result = await db.execute(
        delete(OidcLoginAttempt).where(
            OidcLoginAttempt.status.in_(("consumed", "expired")),
            OidcLoginAttempt.created_at < cutoff,
        )
    )
    purged = del_result.rowcount or 0

    # 3. Delete EXPIRED browser-login one-time codes regardless of status
    #    (C14) — a consumed-but-expired code is dead either way; the 120s TTL
    #    makes expiry the reap key. Removes the code_hash/binding_hash rows.
    codes_result = await db.execute(
        delete(SsoExchangeCode).where(SsoExchangeCode.expires_at < now)
    )
    codes_purged = codes_result.rowcount or 0

    await _log_action(
        db,
        admin.id,
        "purge_oidc_login_attempts",
        "oidc_login_attempt",
        "bulk",
        {"purged": purged, "expired_flipped": flipped.rowcount or 0,
         "exchange_codes_purged": codes_purged,
         "retention_days": retention_days},
    )
    await db.commit()

    return {
        "purged": purged,
        "expired_flipped": flipped.rowcount or 0,
        "exchange_codes_purged": codes_purged,
    }


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


# ---------------------------------------------------------------------------
# Project transfer override (GH #84)
# ---------------------------------------------------------------------------


class ProjectForceTransferRequest(BaseModel):
    to_org_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_CHARS)
    # Same meaning as the transfer accept route's `force` (GH #70): move
    # even when non-terminal tickets / runs / handoffs use org personas the
    # destination org does not have.
    force: bool = False
    # Org-project owners must be members of the project's org (member
    # removal reassigns their projects; owners otherwise keep access
    # forever). Required when the current owner is not a member of the
    # destination org; may also be given to reassign ownership explicitly.
    new_owner_id: str | None = Field(None, min_length=1, max_length=36)
    # Precondition against delayed retries: when present, the project must
    # currently be in this org (null = personal) or the request is refused
    # with 409 unexpected_source_scope. Omit to skip the check.
    expected_org_id: str | None = Field(None, min_length=1, max_length=64)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


async def _is_org_member(
    db: AsyncSession, org_id: str, user_id: str, *, require_active: bool = False
) -> bool:
    stmt = (
        select(OrgMember.user_id)
        .join(User, User.id == OrgMember.user_id)
        .where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
        .limit(1)
    )
    if require_active:
        stmt = stmt.where(User.is_active.is_not(False))
    return (await db.execute(stmt)).first() is not None


def _project_override_org_event(
    org: Organization,
    admin: User,
    project: Project,
    before: dict,
    after: dict,
) -> OrgAuditEvent:
    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        org_name_snapshot=org.name,
        event_type="project_force_transferred",
        actor_user_id=admin.id,
        actor_email_snapshot=admin.email,
        actor_role_at_time="platform_admin",
        target_type="project",
        target_id=project.id,
        before=json.dumps(before, sort_keys=True),
        after=json.dumps(after, sort_keys=True),
    )


@router.post("/projects/{project_id}/force-transfer")
async def force_transfer_project(
    project_id: str,
    body: ProjectForceTransferRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Move a project into an org without the owner's consent. Platform
    admin only.

    The operator override for "the owner is unavailable and the team needs
    the project in the org". It keeps the normal transfer path's
    invariants: the GH #70 scope locks (project, then the source and
    destination orgs) are taken before any check, a merged project is
    refused (409 project_merged), and the stranded-persona guard applies
    unless `force`. The owner must be a member of the destination org
    (409 owner_not_member) unless `new_owner_id` names an active member,
    who then becomes the owner. `expected_org_id` (null = personal) refuses
    the move (409 unexpected_source_scope) if the project is no longer
    where the operator saw it. Any pending transfer for the project is cancelled. An
    accepted ProjectTransfer row (initiated and accepted by the admin), an
    AdminAction with before/after state and the reason, and OrgAuditEvent
    rows for the orgs involved are written in the same transaction as the
    move: either all of it commits or none of it does.
    """
    from sessionfs.server.routes.project_transfers import _assert_no_stranded_personas
    from sessionfs.server.services.project_resolver import get_primary_remote
    from sessionfs.server.services.scope_locks import lock_project_scope

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    to_org_id = body.to_org_id
    # Re-reads the project under its lock (404 if deleted meanwhile, 409 if
    # merged), then locks its current org and the destination org.
    await lock_project_scope(db, project, extra_org_ids=[to_org_id])

    dest_org = (
        await db.execute(select(Organization).where(Organization.id == to_org_id))
    ).scalar_one_or_none()
    if dest_org is None:
        raise HTTPException(404, "Destination org not found")
    if "expected_org_id" in body.model_fields_set and project.org_id != body.expected_org_id:
        raise HTTPException(
            409,
            {
                "code": "unexpected_source_scope",
                "message": (
                    f"The project is in {project.org_id or 'personal'!r}, not "
                    f"{body.expected_org_id or 'personal'!r}; nothing was changed."
                ),
                "current_org_id": project.org_id,
            },
        )
    if project.org_id == dest_org.id:
        raise HTTPException(
            409,
            {
                "code": "already_in_org",
                "message": "The project is already in that org; nothing to transfer.",
            },
        )

    # Org-project owners must be members of the org: member removal hands
    # their projects to the removing admin, and owners keep full access
    # regardless of membership. Checked under the destination org lock, which
    # member removal also takes.
    old_owner_id = project.owner_id
    new_owner_id = old_owner_id
    if body.new_owner_id is not None:
        if not await _is_org_member(db, dest_org.id, body.new_owner_id, require_active=True):
            raise HTTPException(
                409,
                {
                    "code": "new_owner_not_member",
                    "message": (
                        "new_owner_id must be an active member of the destination org."
                    ),
                },
            )
        new_owner_id = body.new_owner_id
    elif not await _is_org_member(db, dest_org.id, old_owner_id):
        raise HTTPException(
            409,
            {
                "code": "owner_not_member",
                "message": (
                    "The project owner is not a member of the destination org. "
                    "Pass new_owner_id (an active member of that org) to "
                    "reassign ownership with the move."
                ),
                "owner_id": old_owner_id,
            },
        )

    source_org = None
    if project.org_id is not None:
        source_org = (
            await db.execute(
                select(Organization).where(Organization.id == project.org_id)
            )
        ).scalar_one_or_none()
    from_scope = project.org_id if project.org_id is not None else "personal"

    await _assert_no_stranded_personas(db, project, dest_org.id, body.force)

    now = datetime.now(timezone.utc)
    pending_ids = list(
        (
            await db.execute(
                select(ProjectTransfer.id).where(
                    ProjectTransfer.project_id == project.id,
                    ProjectTransfer.state == "pending",
                )
            )
        ).scalars().all()
    )
    if pending_ids:
        await db.execute(
            update(ProjectTransfer)
            .where(
                ProjectTransfer.id.in_(pending_ids),
                ProjectTransfer.state == "pending",
            )
            .values(state="cancelled", updated_at=now)
        )

    primary_remote = await get_primary_remote(db, project.id)
    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        project_git_remote_snapshot=primary_remote or project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=admin.id,
        target_user_id=admin.id,
        from_scope=from_scope,
        to_scope=dest_org.id,
        state="accepted",
        accepted_by=admin.id,
        accepted_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(transfer)

    before = {
        "org_id": project.org_id,
        "scope": from_scope,
        "owner_id": project.owner_id,
        "pending_transfer_ids": pending_ids,
    }
    project.org_id = dest_org.id
    project.owner_id = new_owner_id
    project.updated_at = now
    # Drop the old org's tracker mapping; existing links go stale.
    await drop_project_tracker_mapping(db, project.id)
    after = {
        "org_id": dest_org.id,
        "scope": dest_org.id,
        "owner_id": new_owner_id,
        "transfer_id": transfer.id,
        "cancelled_transfer_ids": pending_ids,
    }

    entry = await _log_action(
        db,
        admin.id,
        "project_transfer_override",
        "project",
        project.id,
        {
            "from_scope": from_scope,
            "to_org_id": dest_org.id,
            "transfer_id": transfer.id,
            "force": body.force,
            "owner_changed": new_owner_id != old_owner_id,
        },
        reason=body.reason,
        before_state=before,
        after_state=after,
    )
    for org in (source_org, dest_org):
        if org is not None:
            db.add(_project_override_org_event(org, admin, project, before, after))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "The project's transfers changed concurrently; retry"
        ) from None

    return {
        "status": "transferred",
        "project_id": project_id,
        "from_scope": from_scope,
        "to_org_id": dest_org.id,
        "transfer_id": transfer.id,
        "cancelled_transfer_ids": pending_ids,
        "previous_owner_id": old_owner_id,
        "owner_id": new_owner_id,
        "admin_action_id": entry.id,
    }


# ---------------------------------------------------------------------------
# Project merge override (GH #89)
# ---------------------------------------------------------------------------


class ProjectMergeOverrideRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_CHARS)
    dry_run: StrictBool = True
    persona_policy: str = Field("rename", pattern="^(rename|skip|merge_content)$")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


@router.post("/projects/{target_id}/merge-from/{source_id}")
async def merge_project_override(
    target_id: str,
    source_id: str,
    body: ProjectMergeOverrideRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Merge SOURCE into TARGET whoever owns them. Platform admin only.

    The operator path for duplicate projects whose owners can't run
    `sfs project merge` themselves (the owner-of-both / org-admin path).
    It runs the same merge engine (services/merge.py): `dry_run` (default
    true) returns the plan and writes nothing; `dry_run=false` merges in
    one transaction, SOURCE becomes a tombstone redirecting to TARGET, and
    the AdminAction (`project_merge`, target = the source project, reason,
    before/after) commits in that same transaction. The engine's own
    project_merge_audit row is written too.

    Owners need not match (GH #89 asks for the cross-owner path): two
    users' personal projects can be merged, and the target's owner then
    holds everything from the source. The AdminAction's before_state names
    both owners.

    Refused: source == target (400); either project missing (404); the
    projects are in different scopes (400 cross_scope: move one with
    force-transfer first); everything the engine refuses (already merged,
    pending transfer, persona shadowing under a non-rename policy).
    """
    from sessionfs.server.db.engine import _session_factory
    from sessionfs.server.services.merge import merge_projects
    from sessionfs.server.services.project_resolver import ProjectResolutionLoopError

    if target_id == source_id:
        raise HTTPException(400, "Cannot merge a project into itself")
    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if source is None:
        raise HTTPException(404, "Source project not found")
    if target is None:
        raise HTTPException(404, "Target project not found")
    if source.org_id != target.org_id:
        raise HTTPException(
            400,
            {
                "code": "cross_scope",
                "message": (
                    "The projects are in different scopes. Move one into the "
                    "other's org with force-transfer first."
                ),
            },
        )
    if _session_factory is None:
        raise HTTPException(500, "Database not initialized")

    before = {
        "source": {"id": source.id, "name": source.name, "owner_id": source.owner_id,
                   "org_id": source.org_id},
        "target": {"id": target.id, "name": target.name, "owner_id": target.owner_id,
                   "org_id": target.org_id},
    }
    actions: list[AdminAction] = []

    def record(stats: dict) -> None:
        actions.append(
            _log_action_sync(
                db,
                admin.id,
                "project_merge",
                "project",
                source_id,
                details={"merged_into": target_id, "persona_policy": body.persona_policy},
                reason=body.reason,
                before_state=before,
                after_state={"merged_into": target_id, "stats": stats},
            )
        )

    try:
        result = await merge_projects(
            db=db,
            source_id=source_id,
            target_id=target_id,
            user_id=admin.id,
            dry_run=body.dry_run,
            persona_policy=body.persona_policy,
            session_factory=_session_factory,
            platform_admin=True,
            before_commit=record,
        )
    except ProjectResolutionLoopError:
        raise HTTPException(
            409,
            {
                "error": "resolution_loop",
                "message": "Project resolution exceeded hop limit — possible data corruption.",
            },
        )

    if body.dry_run:
        return {"status": "planned", **result}
    return {"status": "merged", "admin_action_id": actions[0].id, **result}


# ---------------------------------------------------------------------------
# Duplicate-account merge (platform admin)
# ---------------------------------------------------------------------------


class UserMergeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=REASON_MAX_CHARS)
    # StrictBool: 1, 0, "true" and null are 422, not silently coerced into
    # a destructive apply.
    dry_run: StrictBool = True

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


@router.post("/users/{source_id}/merge-into/{target_id}")
async def merge_user_into(
    source_id: str,
    target_id: str,
    body: UserMergeRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fold a duplicate account (SOURCE) into another (TARGET). Platform
    admin only. `dry_run` (default true) writes nothing and returns the
    plan; `dry_run=false` applies it in ONE transaction together with the
    deactivation of SOURCE and exactly one AdminAction (`user_merge`, target
    = the source user, reason, before/after = per-table counts and row ids,
    source `api`; `details.merged_into` names the target).

    Refused: source == target (400); either user missing (404); target
    inactive (409 target_inactive); source is a platform admin (409
    source_is_platform_admin); source holds Stripe pointers (409
    source_has_billing); both hold an active entitlement (409
    entitlement_conflict, the merge never guesses which plan to keep);
    source owns skill-marketplace packages/publishers/listings/registries
    (409 marketplace_assets_owned); both have a GitHub installation (409
    github_installation_conflict); source owns org projects in an org
    neither user belongs to (409 project_owner_not_member).

    Per-table decisions (see services/user_merge.py):

    * REASSIGNED to the target: sessions, projects.owner_id, share_links,
      bookmark_folders, bookmarks, audit_reports,
      project_rules.created_by, rules_versions.compiled_by, handoffs
      (sender_id, recipient_id, recipient_user_id), handoff_comments
      author, knowledge_entries.user_id, context_compilations.user_id,
      agent_personas (created_by, represented_user_id, with an org audit
      row each), tickets (created_by_user_id, resolver_user_id),
      ticket_comments author, ticket_edits editor, wiki_page_revisions,
      work_queues creator, work_queue_items implementer, org_invites
      inviter, pr_comments owner, github_installations, and every
      entitlements row owned by the source (the target also inherits the
      source's entitlement pointer and tier when the source held the only
      active one). Source storage_used_bytes is added to the target's.
    * DEDUPED: org_members (per org: moved when the target is not a
      member; otherwise the higher role wins, the target on ties, the
      source row is deleted before any promotion so the one-owner-per-org
      index never sees two owners, each change audited in the org's
      audit trail); team_members, sync_watchlist, skill_devices,
      gitlab_settings and user_judge_settings (the target's row wins, the
      source's is deleted, the rest move); sessions.alias (unique per
      user: a source alias the target already uses is cleared on the
      moved session).
    * REVOKED / CANCELLED (never moved): the source's API keys (including
      service keys), trusted_reviewers bindings, SSO break-glass grants,
      SSO external identities, and pending org owner transfers and project
      transfers involving the source. The source user is deactivated.
    * LEFT ALONE so history keeps the original actor: admin_actions,
      org_audit_events, activation_attempt, agent_runs, agent_feedback,
      billing intents/reconciliations, handoff_events, retrieval audit
      tables, project_merge_audit, project_repos, transfer history, org
      skill policies/assignments, skill_* provenance and moderation
      tables, residents, trusted_reviewers.created_by_user_id,
      teams.created_by, org_members.invited_by, sso_exchange_codes,
      github_claim_attempts, sessions.deleted_by,
      knowledge_entries.dismissed_by, work_queue_items.closed_by_user_id,
      api_keys.created_by_user_id.

    Locks follow the GH #70 order: project scope locks (the source's
    projects), then both user scope locks, then the org scope locks of
    every org either user belongs to or whose project is moved, then the
    billing row locks (Stripe customer, user rows). The set of the
    source's projects is re-read under the locks; if it changed the
    request is refused (409 changed_concurrently) and can be retried.
    """
    from sessionfs.server.services.entitlements import lock_billing_rows
    from sessionfs.server.services.scope_locks import lock_scopes
    from sessionfs.server.services.user_merge import merge_users, trim_state

    if source_id == target_id:
        raise HTTPException(400, "Source and target must be different users")
    users = {
        u.id: u
        for u in (
            await db.execute(select(User).where(User.id.in_([source_id, target_id])))
        ).scalars().all()
    }
    if source_id not in users:
        raise HTTPException(404, "Source user not found")
    if target_id not in users:
        raise HTTPException(404, "Target user not found")
    apply = not body.dry_run

    if apply:
        owned_before = set(
            (await db.execute(select(Project.id).where(Project.owner_id == source_id))).scalars()
        )
        await lock_scopes(db, project_ids=owned_before, user_ids=[source_id, target_id])
        org_ids = set(
            (
                await db.execute(
                    select(OrgMember.org_id).where(OrgMember.user_id.in_([source_id, target_id]))
                )
            ).scalars()
        ) | set(
            (
                await db.execute(
                    select(Project.org_id).where(
                        Project.owner_id == source_id, Project.org_id.is_not(None)
                    )
                )
            ).scalars()
        )
        await lock_scopes(db, org_ids=org_ids)
        owned_now = set(
            (await db.execute(select(Project.id).where(Project.owner_id == source_id))).scalars()
        )
        if owned_now != owned_before:
            await db.rollback()
            raise HTTPException(
                409,
                {
                    "code": "changed_concurrently",
                    "message": "The source user's projects changed while locking; retry.",
                },
            )
        locked = await lock_billing_rows(db, user_ids=[source_id, target_id])
        source, target = locked[("user", source_id)], locked[("user", target_id)]
        if source is None or target is None:
            await db.rollback()
            raise HTTPException(404, "User not found")
    else:
        source, target = users[source_id], users[target_id]

    def _snapshot(u: User) -> dict:
        return {
            "id": u.id, "email": u.email, "tier": u.tier, "is_active": u.is_active,
            "storage_used_bytes": u.storage_used_bytes or 0,
            "entitlement_id": u.entitlement_id,
        }

    before_users = {"source": _snapshot(source), "target": _snapshot(target)}
    plan = await merge_users(db, admin=admin, source=source, target=target, apply=apply)

    if not apply:
        return {
            "status": "dry_run", "dry_run": True,
            "source_id": source_id, "target_id": target_id,
            "reason": body.reason, "plan": plan,
        }

    rows = {k: plan[k] for k in ("reassigned", "deduped", "revoked", "cancelled", "org_members")}
    before = trim_state(
        {"users": before_users, "target_id": target_id, "rows": rows}, STATE_MAX_BYTES // 2
    )
    after = trim_state(
        {
            "users": {"source": _snapshot(source), "target": _snapshot(target)},
            "merged_into": target_id,
            "rows": rows,
            "user": plan["user"],
        },
        STATE_MAX_BYTES // 2,
    )
    entry = await _log_action(
        db, admin.id, "user_merge", "user", source_id,
        {"merged_into": target_id, "source_email": source.email, "target_email": target.email},
        reason=body.reason, before_state=before, after_state=after,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {"code": "changed_concurrently", "message": "Rows changed concurrently; nothing was merged, retry."},
        ) from None
    return {
        "status": "merged", "dry_run": False,
        "source_id": source_id, "target_id": target_id,
        "reason": body.reason, "plan": plan, "admin_action_id": entry.id,
    }
