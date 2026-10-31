"""v0.10.10 — API key CRUD routes (tk_2e030a85253143df).

Two route groups:

  /api/v1/orgs/{org_id}/service-keys/...
      Org admin-only. Mints scoped service keys for cloud agents / CI /
      Bedrock / Vertex. Codex R1 MEDIUM 2 made these org-scoped (vs
      a global /admin/api-keys surface).

  /api/v1/auth/me/api-keys/...
      Any user can list/create/revoke their own personal user keys.
      Personal keys inherit user-key semantics (scopes='["*"]') —
      this is the existing key kind, just with a UI for self-mint
      replacing the dashboard-only flow.

Raw keys are returned ONCE (POST /create + POST /rotate). List/detail
responses only expose key_prefix (Codex R1 MEDIUM 3).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ApiKey, Organization, TicketWatchCursor, User
from sessionfs.server.schemas.api_keys import (
    PersonalKeyCreateRequest,
    PersonalKeyCreateResponse,
    PersonalKeyResponse,
    RevokeKeyRequest,
    ServiceKeyCreateRequest,
    ServiceKeyCreateResponse,
    ServiceKeyResponse,
    ServiceKeyWarning,
)
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    check_role,
    get_user_context,
)

logger = logging.getLogger("sessionfs.api")

# Two distinct routers so /admin/* style decoration can stay focused.
service_key_router = APIRouter(prefix="/api/v1/orgs", tags=["service-keys"])
personal_key_router = APIRouter(prefix="/api/v1/auth/me", tags=["api-keys"])


def _key_prefix(raw_key: str) -> str:
    """Safe-to-display prefix (e.g. 'sk_sfs_abcdef'). 12 chars.
    Codex R3 MEDIUM 2 — this prefix is persisted on the row at create
    time so list/get responses show what the deployed secret looks like."""
    return raw_key[:12]


def _service_key_to_response(
    row: ApiKey,
    project_ids_list: list[str] | None,
    coupling_ok: bool | None = None,
    coupling_reason: str | None = None,
) -> ServiceKeyResponse:
    return ServiceKeyResponse(
        id=row.id,
        name=row.name or "",
        org_id=row.org_id or "",
        service_key_name=row.service_key_name or row.name or "",
        scopes=json.loads(row.scopes) if row.scopes else [],
        project_ids=project_ids_list,
        # Codex R3 MEDIUM 2 — return the persisted real prefix. Legacy
        # rows that pre-date key_prefix have NULL, so fall back to a
        # safe placeholder so the response is still valid.
        key_prefix=row.key_prefix or "sk_sfs_legacy",
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoke_reason=row.revoke_reason,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        is_active=bool(row.is_active),
        coupling_ok=coupling_ok,
        coupling_reason=coupling_reason,
    )


async def _require_org_admin(
    org_id: str, ctx: UserContext, db: AsyncSession
) -> Organization:
    """Membership + role + tier gate for org-scoped service-key admin."""
    check_feature(ctx, "team_management")
    check_role(ctx, "admin")
    if ctx.org is None or ctx.org.id != org_id:
        # Existence-hiding: caller doesn't even belong to this org.
        raise HTTPException(status_code=404, detail="Organization not found")
    return ctx.org


# Scopes whose routes act across every org (the works-in harness, the
# registry poller, the scheduled platform sweeps): only a platform admin may
# mint or rotate a key that carries them.
PLATFORM_ADMIN_SCOPES = frozenset(
    {
        "skills_verify:run",
        "skills_registry:poll",
        "platform_sweeps:run",
        "admin_metrics:read",
    }
)


def _check_platform_scopes(scopes: list[str], user: User) -> None:
    if PLATFORM_ADMIN_SCOPES.intersection(scopes) and user.tier != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "platform_admin_scope",
                "message": "Only a platform admin can mint or rotate a key with "
                f"{sorted(PLATFORM_ADMIN_SCOPES.intersection(scopes))}.",
            },
        )


# Non-fatal warning code for keys that carry a tier-coupled (platform-admin
# mint only) scope. The set is PLATFORM_ADMIN_SCOPES above — one source of
# truth shared by the mint-time check and this warning.
TIER_COUPLING_WARNING_CODE = "minter_tier_coupling"


def _tier_coupling_warnings(scopes: list[str]) -> list[ServiceKeyWarning]:
    """Warn when a key carries a scope whose lifetime is coupled to the
    minter's (and owner's) platform-admin tier. The security rule is kept;
    this just makes the coupling visible at mint/rotate time."""
    coupled = sorted(PLATFORM_ADMIN_SCOPES.intersection(scopes))
    if not coupled:
        return []
    joined = ", ".join(coupled)
    return [
        ServiceKeyWarning(
            code=TIER_COUPLING_WARNING_CODE,
            message=(
                f"{joined} is coupled to platform-admin tier: this key stops "
                "working the moment the user who minted it or the key's owner "
                "(the user it runs as) is no longer an active platform admin. "
                "Elevating to admin, minting, then reverting the elevation "
                "will not yield a working key."
            ),
        )
    ]


async def _coupling_status(
    db: AsyncSession,
    owner_id: str,
    minter_id: str | None,
    scopes: list[str],
) -> tuple[bool | None, str | None]:
    """Whether a key's tier coupling currently holds (both owner and minter
    are active platform admins). ``(None, None)`` when the key carries no
    tier-coupled scope."""
    if not PLATFORM_ADMIN_SCOPES.intersection(scopes):
        return None, None
    if minter_id is None:
        return False, "minter record missing"
    owner = await db.get(User, owner_id)
    minter = owner if minter_id == owner_id else await db.get(User, minter_id)
    if minter is None or not minter.is_active or minter.tier != "admin":
        return False, "minter no longer admin"
    if owner is None or not owner.is_active or owner.tier != "admin":
        return False, "owner no longer admin"
    return True, None


@service_key_router.post(
    "/{org_id}/service-keys",
    status_code=201,
    response_model=ServiceKeyCreateResponse,
)
async def create_service_key(
    org_id: str,
    body: ServiceKeyCreateRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Mint a scoped service key for cloud agents / CI / Bedrock / Vertex.

    Returns the raw key ONCE — the caller MUST persist it. Subsequent
    list/get responses only include the key_prefix (Codex R1 MEDIUM 3).
    """
    await _require_org_admin(org_id, ctx, db)
    _check_platform_scopes(body.scopes, user)

    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    now = datetime.now(timezone.utc)
    expires_at = (
        now + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user.id,  # service key "runs as" the minter
        key_hash=key_hash,
        name=body.name,
        is_active=True,
        key_kind="service",
        org_id=org_id,
        scopes=json.dumps(body.scopes),
        expires_at=expires_at,
        created_by_user_id=user.id,
        service_key_name=body.name,
        project_ids=json.dumps(body.project_ids) if body.project_ids else None,
        key_prefix=_key_prefix(raw_key),  # R3 MEDIUM 2
        created_at=now,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    # Codex R1 MEDIUM 3 — never log the raw key.
    logger.info(
        "Service key created: id=%s org=%s by=%s scopes=%s",
        api_key.id, org_id, user.id, body.scopes,
    )

    coupling_ok, coupling_reason = await _coupling_status(
        db, api_key.user_id, api_key.created_by_user_id, body.scopes
    )
    base = _service_key_to_response(
        api_key, body.project_ids, coupling_ok=coupling_ok, coupling_reason=coupling_reason
    )
    return ServiceKeyCreateResponse(
        **base.model_dump(), key=raw_key, warnings=_tier_coupling_warnings(body.scopes)
    )


@service_key_router.get(
    "/{org_id}/service-keys",
    response_model=list[ServiceKeyResponse],
)
async def list_service_keys(
    org_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """List service keys in the org. Org admins only."""
    await _require_org_admin(org_id, ctx, db)
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.org_id == org_id, ApiKey.key_kind == "service")
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    out: list[ServiceKeyResponse] = []
    for r in rows:
        project_ids = json.loads(r.project_ids) if r.project_ids else None
        scopes = json.loads(r.scopes) if r.scopes else []
        coupling_ok, coupling_reason = await _coupling_status(
            db, r.user_id, r.created_by_user_id, scopes
        )
        out.append(
            _service_key_to_response(
                r, project_ids, coupling_ok=coupling_ok, coupling_reason=coupling_reason
            )
        )
    return out


@service_key_router.delete(
    "/{org_id}/service-keys/{key_id}",
    status_code=204,
)
async def revoke_service_key(
    org_id: str,
    key_id: str,
    body: RevokeKeyRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a service key. Soft delete — stamps revoked_at + reason."""
    await _require_org_admin(org_id, ctx, db)
    row = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.org_id == org_id,
                ApiKey.key_kind == "service",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Service key not found")
    if row.revoked_at is not None:
        # Idempotent — already revoked.
        return
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            is_active=False,
            revoked_at=datetime.now(timezone.utc),
            revoke_reason=body.reason,
        )
    )
    await db.commit()
    logger.info(
        "Service key revoked: id=%s org=%s by=%s reason=%r",
        key_id, org_id, user.id, body.reason,
    )


@service_key_router.post(
    "/{org_id}/service-keys/{key_id}/rotate",
    response_model=ServiceKeyCreateResponse,
)
async def rotate_service_key(
    org_id: str,
    key_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Revoke the old key and mint a new one with the same scopes,
    expiry policy, and project allowlist. Returns the new raw key
    ONCE — same secret-handling rules as create."""
    await _require_org_admin(org_id, ctx, db)
    old = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.org_id == org_id,
                ApiKey.key_kind == "service",
            )
        )
    ).scalar_one_or_none()
    if old is None:
        raise HTTPException(status_code=404, detail="Service key not found")
    if old.revoked_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "key_already_revoked",
                "message": "Cannot rotate a revoked key — create a new one",
            },
        )

    _check_platform_scopes(json.loads(old.scopes) if old.scopes else [], user)

    now = datetime.now(timezone.utc)
    # Compute new expiry preserving the same total lifetime if the old
    # key had one — otherwise none. Codex R3 Q6 — normalize both
    # operands to timezone-aware UTC before subtracting so SQLite naive
    # values don't TypeError against our aware `now`.
    if old.expires_at is not None:
        exp = old.expires_at
        created = old.created_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        original_lifetime = exp - created
        new_expires = now + original_lifetime
    else:
        new_expires = None

    raw_key = generate_api_key()
    new_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=old.user_id,
        key_hash=hash_api_key(raw_key),
        name=old.name,
        is_active=True,
        key_kind="service",
        org_id=org_id,
        scopes=old.scopes,
        expires_at=new_expires,
        created_by_user_id=user.id,
        rotated_from_key_id=old.id,
        service_key_name=old.service_key_name or old.name,
        project_ids=old.project_ids,
        key_prefix=_key_prefix(raw_key),  # R3 MEDIUM 2
        created_at=now,
    )
    db.add(new_key)
    # Revoke old in the same transaction.
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == old.id)
        .values(
            is_active=False,
            revoked_at=now,
            revoke_reason=f"rotated by {user.email or user.id}",
        )
    )

    # Carry the old key's watch cursor(s) into the new key's namespace so a
    # rotated resident keeps its read position instead of replaying history.
    # The service-key cursor namespace is "svc:<key id>"; the new key id is
    # freshly minted, so on_conflict_do_nothing only guards a retry.
    old_persona = f"svc:{old.id}"
    new_persona = f"svc:{new_key.id}"
    copied_cursors = (
        await db.execute(
            select(TicketWatchCursor).where(
                TicketWatchCursor.persona_name == old_persona
            )
        )
    ).scalars().all()
    if copied_cursors:
        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            insert_impl = pg_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            insert_impl = sqlite_insert
        await db.execute(
            insert_impl(TicketWatchCursor)
            .values(
                [
                    {
                        "user_id": row.user_id,
                        "persona_name": new_persona,
                        "last_seen_notification_id": row.last_seen_notification_id,
                        "updated_at": now,
                    }
                    for row in copied_cursors
                ]
            )
            .on_conflict_do_nothing(index_elements=["user_id", "persona_name"])
        )

    await db.commit()
    await db.refresh(new_key)

    logger.info(
        "Service key rotated: old=%s new=%s org=%s by=%s",
        old.id, new_key.id, org_id, user.id,
    )

    project_ids = json.loads(new_key.project_ids) if new_key.project_ids else None
    new_scopes = json.loads(new_key.scopes) if new_key.scopes else []
    coupling_ok, coupling_reason = await _coupling_status(
        db, new_key.user_id, new_key.created_by_user_id, new_scopes
    )
    base = _service_key_to_response(
        new_key, project_ids, coupling_ok=coupling_ok, coupling_reason=coupling_reason
    )
    return ServiceKeyCreateResponse(
        **base.model_dump(), key=raw_key, warnings=_tier_coupling_warnings(new_scopes)
    )


# ── Personal user-key surface ────────────────────────────────────────


@personal_key_router.get(
    "/api-keys",
    response_model=list[PersonalKeyResponse],
)
async def list_my_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List my own personal user keys. No service keys here."""
    rows = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.key_kind == "user")
            .order_by(ApiKey.created_at.desc())
        )
    ).scalars().all()
    return [
        PersonalKeyResponse(
            id=r.id,
            name=r.name,
            key_prefix=r.key_prefix or "sk_sfs_legacy",
            created_at=r.created_at,
            expires_at=r.expires_at,
            last_used_at=r.last_used_at,
            last_used_ip=r.last_used_ip,
            is_active=bool(r.is_active),
        )
        for r in rows
    ]


@personal_key_router.post(
    "/api-keys",
    status_code=201,
    response_model=PersonalKeyCreateResponse,
)
async def create_my_api_key(
    body: PersonalKeyCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a personal user key for CI use, etc. Inherits user-key
    semantics (scopes='["*"]'). Raw key returned ONCE."""
    raw_key = generate_api_key()
    now = datetime.now(timezone.utc)
    expires_at = (
        now + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        name=body.name,
        is_active=True,
        key_kind="user",
        scopes=json.dumps(["*"]),
        expires_at=expires_at,
        created_by_user_id=user.id,
        key_prefix=_key_prefix(raw_key),  # R3 MEDIUM 2
        created_at=now,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    logger.info(
        "Personal user key created: id=%s user=%s",
        api_key.id, user.id,
    )
    return PersonalKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix or _key_prefix(raw_key),
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=None,
        last_used_ip=None,
        is_active=True,
        key=raw_key,
    )


@personal_key_router.delete(
    "/api-keys/{key_id}",
    status_code=204,
)
async def revoke_my_api_key(
    key_id: str,
    body: RevokeKeyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one of my personal keys. Soft delete + reason."""
    row = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.user_id == user.id,
                ApiKey.key_kind == "user",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Key not found")
    if row.revoked_at is not None:
        return  # idempotent
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            is_active=False,
            revoked_at=datetime.now(timezone.utc),
            revoke_reason=body.reason,
        )
    )
    await db.commit()
