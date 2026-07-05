"""Resident R0 — resident registration + durable memory endpoints.

Migration 057 foundation for the resident reviewer/implementer runner
(docs/design/resident-reviewer.md R3). Endpoints:

  Org-admin (registration + lifecycle):
    POST   /api/v1/orgs/{org_id}/residents              — register
    GET    /api/v1/orgs/{org_id}/residents              — list
    GET    /api/v1/orgs/{org_id}/residents/{id}         — get
    POST   /api/v1/orgs/{org_id}/residents/{id}/status  — pause/retire
    POST   /api/v1/orgs/{org_id}/residents/{id}/rotate-key  — F7 rebind

  Resident's own service key (memory, C5 isolation):
    POST   .../residents/{id}/memory                    — write (resident_memory:write)
    GET    .../residents/{id}/memory                    — read own (resident_memory:read)
    POST   .../residents/{id}/memory/hydrate            — hydrate (resident_memory:read)
    POST   .../residents/{id}/memory/compact            — compact (resident_memory:write)
    POST   .../residents/{id}/memory/{entry_id}/quarantine  — quarantine entry

C5 ISOLATION (hard, server-enforced): every memory endpoint requires
  resident.org_id == ctx.org_id AND
  resident.service_key_id == ctx.service_key_id.
A resident touches ONLY its own mind — not another resident's, even in
the same org. org_id on entries is SERVER-SET, never trusted from the body.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    Resident,
    ResidentMemoryEntry,
    TrustedReviewer,
    WorkQueue,
    WorkQueueItem,
)
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    check_role,
    get_user_context,
)

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/orgs", tags=["residents"])

# ── Per-request memory write cap (F6) ────────────────────────────────
_MAX_MEMORY_WRITES_PER_REQUEST = 50


# ── Pydantic schemas ──────────────────────────────────────────────────


class ResidentRegisterRequest(BaseModel):
    service_key_id: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    persona_name: str = Field(..., min_length=1, max_length=50)
    work_queue_id: str | None = Field(None)
    project_id: str = Field(..., min_length=1)
    mind_token_budget: int = Field(8000, ge=1000, le=32000)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in ("reviewer", "implementer"):
            raise ValueError("kind must be 'reviewer' or 'implementer'")
        return v


class ResidentStatusRequest(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in ("active", "paused", "retired"):
            raise ValueError("status must be 'active', 'paused', or 'retired'")
        return v


class RotateKeyRequest(BaseModel):
    new_service_key_id: str = Field(..., min_length=1)


class MemoryWriteRequest(BaseModel):
    kind: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, max_length=65536)
    token_estimate: int = Field(0, ge=0)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in ("reasoning", "digest", "observation"):
            raise ValueError("kind must be 'reasoning', 'digest', or 'observation'")
        return v


class MemoryCompactRequest(BaseModel):
    digest_content: str = Field(..., min_length=1, max_length=65536)
    digest_token_estimate: int = Field(0, ge=0)
    superseded_entry_ids: list[str] = Field(default_factory=list, max_length=200)


class MemoryHydrateResponse(BaseModel):
    digest: dict[str, Any] | None = None
    recent_reasoning: list[dict[str, Any]] = []


class ResidentResponse(BaseModel):
    id: str
    org_id: str
    project_id: str
    kind: str
    persona_name: str
    service_key_id: str
    work_queue_id: str | None
    status: str
    mind_token_budget: int
    max_uncompacted_entries: int
    created_by_user_id: str
    created_at: str
    updated_at: str


class MemoryEntryResponse(BaseModel):
    id: str
    resident_id: str
    org_id: str
    kind: str
    seq: int
    content: str
    token_estimate: int
    superseded_by: str | None
    compacted_at: str | None
    quarantined: bool
    created_at: str


# ── Helpers ────────────────────────────────────────────────────────────


def _resident_to_response(r: Resident) -> ResidentResponse:
    return ResidentResponse(
        id=r.id,
        org_id=r.org_id,
        project_id=r.project_id,
        kind=r.kind,
        persona_name=r.persona_name,
        service_key_id=r.service_key_id,
        work_queue_id=r.work_queue_id,
        status=r.status,
        mind_token_budget=r.mind_token_budget,
        max_uncompacted_entries=r.max_uncompacted_entries,
        created_by_user_id=r.created_by_user_id,
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


def _entry_to_response(e: ResidentMemoryEntry) -> MemoryEntryResponse:
    return MemoryEntryResponse(
        id=e.id,
        resident_id=e.resident_id,
        org_id=e.org_id,
        kind=e.kind,
        seq=e.seq,
        content=e.content,
        token_estimate=e.token_estimate,
        superseded_by=e.superseded_by,
        compacted_at=e.compacted_at.isoformat() if e.compacted_at else None,
        quarantined=bool(e.quarantined),
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


async def _require_org_admin(
    org_id: str, ctx: UserContext, db: AsyncSession
) -> Organization:
    """Membership + role + tier gate for resident registration/lifecycle."""
    check_feature(ctx, "team_management")
    check_role(ctx, "admin")
    if ctx.org is None or ctx.org.id != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return ctx.org


async def _fetch_resident(
    db: AsyncSession, resident_id: str, org_id: str
) -> Resident:
    """Fetch a resident by id, scoped to org. 404 if not found."""
    resident = (
        await db.execute(
            select(Resident).where(
                Resident.id == resident_id,
                Resident.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if resident is None:
        raise HTTPException(status_code=404, detail="Resident not found")
    return resident


async def _enforce_resident_isolation(
    db: AsyncSession,
    resident: Resident,
    auth: AuthContext,
    *,
    allow_org_admin: bool = False,
) -> None:
    """C5 isolation. The resident's OWN service key has full access to its own
    mind (org must match). For read/quarantine (allow_org_admin=True), F5
    owner-visibility ALSO admits an org owner/admin USER key of the resident's
    org — the audited recovery path for a poisoned implementer mind. Writes and
    hydration stay own-key-only, so no cross-identity path can mutate the mind."""
    # Own service key → full access to its own mind (org must still match).
    if (
        auth.service_key_id is not None
        and auth.service_key_id == resident.service_key_id
    ):
        if auth.org_id != resident.org_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "resident_org_mismatch",
                    "message": "Resident org does not match authenticated org.",
                },
            )
        return

    # F5 owner-visibility (read/quarantine only): an org owner/admin may inspect
    # an IMPLEMENTER resident's mind to recover from poisoned memory. The
    # recovery exception is scoped to implementer minds — reviewer resident
    # memory stays resident-private (no admin carve-out).
    if (
        allow_org_admin
        and resident.kind == "implementer"
        and auth.key_kind == "user"
        and auth.user is not None
    ):
        member = await db.scalar(
            select(OrgMember).where(
                OrgMember.org_id == resident.org_id,
                OrgMember.user_id == auth.user.id,
            )
        )
        if member is not None and member.role in ("owner", "admin"):
            return

    raise HTTPException(
        status_code=403,
        detail={
            "error": "resident_key_mismatch",
            "message": (
                "This resident is bound to a different service key. "
                "A resident may only access its own mind; org owners/admins "
                "may inspect it read-only."
            ),
        },
    )


async def _check_f4_mutual_exclusion(
    db: AsyncSession,
    service_key_id: str,
    org_id: str,
    kind: str,
    project_id: str,
) -> None:
    """F4 SoD mutual-exclusion: a service_key_id cannot be both an
    implementer resident and a trusted reviewer (either direction).
    Called at resident registration AND trusted-reviewer registration.
    """
    if kind == "implementer":
        # Check if this key is already a trusted reviewer for this project/org.
        tr_exists = (
            await db.execute(
                select(TrustedReviewer.id).where(
                    TrustedReviewer.service_key_id == service_key_id,
                    TrustedReviewer.is_active.is_(True),
                    TrustedReviewer.revoked_at.is_(None),
                    or_(
                        TrustedReviewer.project_id == project_id,
                        TrustedReviewer.org_id == org_id,
                    ),
                )
            )
        ).scalar_one_or_none()
        if tr_exists is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "sod_mutual_exclusion",
                    "message": (
                        "This service key is already registered as a trusted "
                        "reviewer for this project/org. A key cannot hold both "
                        "implementer and reviewer roles."
                    ),
                },
            )
    else:
        # reviewer kind — check if already an implementer resident.
        impl_exists = (
            await db.execute(
                select(Resident.id).where(
                    Resident.service_key_id == service_key_id,
                    Resident.org_id == org_id,
                    Resident.kind == "implementer",
                    Resident.status.in_(("active", "paused")),
                )
            )
        ).scalar_one_or_none()
        if impl_exists is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "sod_mutual_exclusion",
                    "message": (
                        "This service key is already bound to an implementer "
                        "resident in this org. A key cannot hold both "
                        "implementer and reviewer roles."
                    ),
                },
            )


# ── Registration + lifecycle (org-admin gated) ─────────────────────────


@router.post("/{org_id}/residents", status_code=201, response_model=ResidentResponse)
async def register_resident(
    org_id: str,
    body: ResidentRegisterRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ResidentResponse:
    """Register a resident (org-admin). F4 mutual-exclusion enforced."""
    await _require_org_admin(org_id, ctx, db)

    # Validate the service key belongs to this org.
    key = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == body.service_key_id,
                ApiKey.org_id == org_id,
                ApiKey.key_kind == "service",
            )
        )
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "service_key_not_in_org",
                "message": (
                    "service_key_id must be a service key belonging to "
                    "this organization."
                ),
            },
        )

    # Validate project belongs to org.
    project = (
        await db.execute(
            select(Project).where(
                Project.id == body.project_id,
                Project.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "project_not_in_org",
                "message": "project_id must be a project in this organization.",
            },
        )

    # Enforce the service key's project allowlist (defense-in-depth): a key
    # scoped to specific projects must NOT drive a resident for another
    # project — the memory endpoints authorize by service_key_id + org, so an
    # unenforced allowlist would let a project-B key reach a project-A mind.
    key_allowlist = json.loads(key.project_ids) if key.project_ids else None
    if key_allowlist and body.project_id not in key_allowlist:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "service_key_project_not_allowed",
                "message": (
                    "This service key's project allowlist does not include "
                    "the requested project."
                ),
            },
        )

    # Validate work_queue_id if provided.
    if body.work_queue_id is not None:
        wq = (
            await db.execute(
                select(WorkQueue.id).where(
                    WorkQueue.id == body.work_queue_id,
                    WorkQueue.project_id == body.project_id,
                )
            )
        ).scalar_one_or_none()
        if wq is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "work_queue_not_found",
                    "message": "work_queue_id not found in this project.",
                },
            )

    # F4 SoD mutual-exclusion check.
    await _check_f4_mutual_exclusion(
        db, body.service_key_id, org_id, body.kind, body.project_id
    )

    # Check uniqueness: one service key per resident (uq_resident_service_key).
    existing = (
        await db.execute(
            select(Resident.id).where(
                Resident.service_key_id == body.service_key_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "service_key_already_bound",
                "message": "This service key is already bound to a resident.",
            },
        )

    now = datetime.now(timezone.utc)
    resident = Resident(
        id=f"res_{secrets.token_hex(12)}",
        org_id=org_id,
        project_id=body.project_id,
        kind=body.kind,
        persona_name=body.persona_name,
        service_key_id=body.service_key_id,
        work_queue_id=body.work_queue_id,
        status="active",
        mind_token_budget=body.mind_token_budget,
        max_uncompacted_entries=500,
        created_by_user_id=ctx.user.id if ctx.user else "",
        actor_type="user",
        service_key_name=key.service_key_name,
        created_at=now,
        updated_at=now,
    )
    db.add(resident)
    await db.commit()
    await db.refresh(resident)

    logger.info(
        "Resident registered: id=%s org=%s kind=%s persona=%s by=%s",
        resident.id,
        org_id,
        body.kind,
        body.persona_name,
        ctx.user.id if ctx.user else "",
    )
    return _resident_to_response(resident)


@router.get("/{org_id}/residents", response_model=list[ResidentResponse])
async def list_residents(
    org_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[ResidentResponse]:
    """List this org's residents. Org admins/owners only."""
    await _require_org_admin(org_id, ctx, db)
    rows = (
        await db.execute(
            select(Resident)
            .where(Resident.org_id == org_id)
            .order_by(Resident.created_at.desc())
        )
    ).scalars().all()
    return [_resident_to_response(r) for r in rows]


@router.get("/{org_id}/residents/{resident_id}", response_model=ResidentResponse)
async def get_resident(
    org_id: str,
    resident_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ResidentResponse:
    """Get a single resident. Org admins/owners only."""
    await _require_org_admin(org_id, ctx, db)
    resident = await _fetch_resident(db, resident_id, org_id)
    return _resident_to_response(resident)


@router.post("/{org_id}/residents/{resident_id}/status", response_model=ResidentResponse)
async def set_resident_status(
    org_id: str,
    resident_id: str,
    body: ResidentStatusRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ResidentResponse:
    """Pause or retire a resident (the lifecycle kill switch)."""
    await _require_org_admin(org_id, ctx, db)
    resident = await _fetch_resident(db, resident_id, org_id)
    resident.status = body.status
    resident.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(resident)
    logger.info(
        "Resident status set: id=%s status=%s by=%s",
        resident_id,
        body.status,
        ctx.user.id if ctx.user else "",
    )
    return _resident_to_response(resident)


@router.post(
    "/{org_id}/residents/{resident_id}/rotate-key", response_model=ResidentResponse
)
async def rotate_resident_key(
    org_id: str,
    resident_id: str,
    body: RotateKeyRequest,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ResidentResponse:
    """F7: rebind a resident to a new service key.

    The rotated-out key is denied at require_scope immediately on revoke
    (checked on every request via api_keys.revoked_at). The resident's
    durable mind is unaffected (keyed by resident_id, not service key).
    """
    await _require_org_admin(org_id, ctx, db)
    resident = await _fetch_resident(db, resident_id, org_id)

    # Validate the new key belongs to this org.
    new_key = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.id == body.new_service_key_id,
                ApiKey.org_id == org_id,
                ApiKey.key_kind == "service",
            )
        )
    ).scalar_one_or_none()
    if new_key is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "service_key_not_in_org",
                "message": "new_service_key_id must be a service key belonging to this org.",
            },
        )

    # Enforce the new key's project allowlist against the resident's project
    # (mirror registration): rotation must not move a resident onto a key
    # scoped to a different project, or that key could reach this mind.
    new_key_allowlist = json.loads(new_key.project_ids) if new_key.project_ids else None
    if new_key_allowlist and resident.project_id not in new_key_allowlist:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "service_key_project_not_allowed",
                "message": (
                    "The new service key's project allowlist does not include "
                    "this resident's project."
                ),
            },
        )

    # F4 SoD check for the new key.
    await _check_f4_mutual_exclusion(
        db, body.new_service_key_id, org_id, resident.kind, resident.project_id
    )

    # Check the new key isn't already bound to a different resident.
    existing = (
        await db.execute(
            select(Resident.id).where(
                Resident.service_key_id == body.new_service_key_id,
                Resident.id != resident_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "service_key_already_bound",
                "message": "The new service key is already bound to a different resident.",
            },
        )

    old_service_key_id = resident.service_key_id
    resident.service_key_id = body.new_service_key_id
    resident.service_key_name = new_key.service_key_name
    resident.updated_at = datetime.now(timezone.utc)

    # F7: revoke the rotated-out key so it can no longer authenticate. The
    # endpoint's contract is immediate denial; require_scope checks
    # revoked_at / is_active on every request, so rebinding alone is not
    # enough — the old key would otherwise keep any scopes it held.
    if old_service_key_id and old_service_key_id != body.new_service_key_id:
        old_key = (
            await db.execute(
                select(ApiKey).where(ApiKey.id == old_service_key_id)
            )
        ).scalar_one_or_none()
        if old_key is not None and old_key.revoked_at is None:
            old_key.revoked_at = datetime.now(timezone.utc)
            old_key.is_active = False

        # Keep IN-FLIGHT work-queue items' recorded implementer identity in
        # sync with the rotation. Otherwise F1's self-review + stale-verdict
        # guards keep comparing against the OLD key, so post-rotation work by
        # the new key would be checked against the pre-rotation writeback and
        # could auto-close without reviewing the new work. Terminal items
        # (done/failed) are NOT rewritten — their implementer_service_key_id is
        # historical provenance for who actually implemented that work.
        await db.execute(
            update(WorkQueueItem)
            .where(
                WorkQueueItem.implementer_service_key_id == old_service_key_id,
                WorkQueueItem.item_status.notin_(("done", "failed")),
            )
            .values(implementer_service_key_id=body.new_service_key_id)
        )

    await db.commit()
    await db.refresh(resident)

    logger.info(
        "Resident key rotated: id=%s new_key_prefix=%s by=%s",
        resident_id,
        new_key.key_prefix or "",
        ctx.user.id if ctx.user else "",
    )
    return _resident_to_response(resident)


# ── Memory endpoints (resident's own service key, C5 isolation) ────────


@router.post(
    "/{org_id}/residents/{resident_id}/memory",
    status_code=201,
    response_model=MemoryEntryResponse,
)
async def write_memory(
    org_id: str,
    resident_id: str,
    body: MemoryWriteRequest,
    auth: AuthContext = Depends(require_scope("resident_memory:write")),
    db: AsyncSession = Depends(get_db),
) -> MemoryEntryResponse:
    """Write a memory entry (reasoning|observation). Server assigns seq.

    F6: rejects when un-compacted (non-superseded) count >= max_uncompacted_entries.
    """
    resident = await _fetch_resident(db, resident_id, org_id)
    await _enforce_resident_isolation(db, resident, auth)

    if resident.status != "active":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "resident_not_active",
                "message": f"Resident is {resident.status}; cannot write memory.",
            },
        )

    # F6 cap: count live (non-superseded, non-quarantined) entries. Excluding
    # quarantined is load-bearing for recovery — quarantining poisoned memory
    # must free cap space so the resident can write again.
    live_count = (
        await db.scalar(
            select(func.count(ResidentMemoryEntry.id)).where(
                ResidentMemoryEntry.resident_id == resident_id,
                ResidentMemoryEntry.superseded_by.is_(None),
                ResidentMemoryEntry.quarantined.is_(False),
            )
        )
    ) or 0
    if live_count >= resident.max_uncompacted_entries:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "memory_cap_exceeded",
                "message": (
                    f"Un-compacted memory entry count ({live_count}) has reached "
                    f"the cap ({resident.max_uncompacted_entries}). "
                    "Compact before writing more."
                ),
            },
        )

    # Get next seq.
    max_seq = (
        await db.scalar(
            select(func.max(ResidentMemoryEntry.seq)).where(
                ResidentMemoryEntry.resident_id == resident_id,
            )
        )
    ) or 0
    next_seq = max_seq + 1

    now = datetime.now(timezone.utc)
    entry = ResidentMemoryEntry(
        id=f"rme_{secrets.token_hex(12)}",
        resident_id=resident_id,
        org_id=resident.org_id,  # SERVER-SET, never from body
        kind=body.kind,
        seq=next_seq,
        content=body.content,
        token_estimate=body.token_estimate,
        created_at=now,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    return _entry_to_response(entry)


@router.get(
    "/{org_id}/residents/{resident_id}/memory",
    response_model=list[MemoryEntryResponse],
)
async def read_memory(
    org_id: str,
    resident_id: str,
    limit: int = Query(100, ge=1, le=500),
    before_seq: int | None = Query(None, ge=0),
    auth: AuthContext = Depends(require_scope("resident_memory:read")),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryEntryResponse]:
    """Read this resident's memory entries (newest first).

    C5: resident's own service key, OR an org owner/admin of the resident's
    org (F5 owner-visibility — read-only recovery path).
    """
    resident = await _fetch_resident(db, resident_id, org_id)
    await _enforce_resident_isolation(db, resident, auth, allow_org_admin=True)

    stmt = (
        select(ResidentMemoryEntry)
        .where(ResidentMemoryEntry.resident_id == resident_id)
        .order_by(ResidentMemoryEntry.seq.desc())
        .limit(limit)
    )
    if before_seq is not None:
        stmt = stmt.where(ResidentMemoryEntry.seq < before_seq)

    rows = (await db.execute(stmt)).scalars().all()
    return [_entry_to_response(r) for r in rows]


@router.post(
    "/{org_id}/residents/{resident_id}/memory/hydrate",
    response_model=MemoryHydrateResponse,
)
async def hydrate_memory(
    org_id: str,
    resident_id: str,
    auth: AuthContext = Depends(require_scope("resident_memory:read")),
    db: AsyncSession = Depends(get_db),
) -> MemoryHydrateResponse:
    """Hydrate: latest digest + recent reasoning bounded by mind_token_budget.
    Excludes quarantined entries.
    """
    resident = await _fetch_resident(db, resident_id, org_id)
    await _enforce_resident_isolation(db, resident, auth)

    # Latest digest (non-quarantined, non-superseded). Quarantine filter is
    # load-bearing: a poisoned digest is the highest-value entry to exclude.
    digest_entry = (
        await db.execute(
            select(ResidentMemoryEntry)
            .where(
                ResidentMemoryEntry.resident_id == resident_id,
                ResidentMemoryEntry.kind == "digest",
                ResidentMemoryEntry.superseded_by.is_(None),
                ResidentMemoryEntry.quarantined.is_(False),
            )
            .order_by(ResidentMemoryEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Recent reasoning entries, non-quarantined, non-superseded,
    # bounded by mind_token_budget.
    budget_remaining = resident.mind_token_budget
    reasoning_entries: list[dict[str, Any]] = []

    if budget_remaining > 0:
        rows = (
            await db.execute(
                select(ResidentMemoryEntry)
                .where(
                    ResidentMemoryEntry.resident_id == resident_id,
                    ResidentMemoryEntry.kind == "reasoning",
                    ResidentMemoryEntry.superseded_by.is_(None),
                    ResidentMemoryEntry.quarantined.is_(False),
                )
                .order_by(ResidentMemoryEntry.seq.desc())
                .limit(50)  # reasonable upper bound per wake
            )
        ).scalars().all()

        for row in rows:
            # Check the budget BEFORE appending — never return an entry that
            # exceeds the remaining budget (that would defeat the advertised
            # bound). Entries are newest-first; stop at the first that doesn't
            # fit so the recent-reasoning window stays contiguous and the
            # digest remains the always-available compacted fallback.
            if row.token_estimate > budget_remaining:
                break
            entry_dict = _entry_to_response(row).model_dump()
            reasoning_entries.append(entry_dict)
            budget_remaining -= row.token_estimate
            if budget_remaining <= 0:
                break

    return MemoryHydrateResponse(
        digest=_entry_to_response(digest_entry).model_dump()
        if digest_entry is not None
        else None,
        recent_reasoning=reasoning_entries,
    )


@router.post(
    "/{org_id}/residents/{resident_id}/memory/compact",
    status_code=201,
    response_model=MemoryEntryResponse,
)
async def compact_memory(
    org_id: str,
    resident_id: str,
    body: MemoryCompactRequest,
    auth: AuthContext = Depends(require_scope("resident_memory:write")),
    db: AsyncSession = Depends(get_db),
) -> MemoryEntryResponse:
    """Compact: store a digest and mark prior entries superseded.

    Client-side produces the digest; server only stores + supersedes
    (never summarizes — no server-side LLM, C1).
    """
    resident = await _fetch_resident(db, resident_id, org_id)
    await _enforce_resident_isolation(db, resident, auth)

    if resident.status != "active":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "resident_not_active",
                "message": f"Resident is {resident.status}; cannot compact.",
            },
        )

    # Get next seq.
    max_seq = (
        await db.scalar(
            select(func.max(ResidentMemoryEntry.seq)).where(
                ResidentMemoryEntry.resident_id == resident_id,
            )
        )
    ) or 0
    next_seq = max_seq + 1

    now = datetime.now(timezone.utc)
    digest_entry = ResidentMemoryEntry(
        id=f"rme_{secrets.token_hex(12)}",
        resident_id=resident_id,
        org_id=resident.org_id,  # SERVER-SET
        kind="digest",
        seq=next_seq,
        content=body.digest_content,
        token_estimate=body.digest_token_estimate,
        created_at=now,
    )
    db.add(digest_entry)
    await db.flush()  # get the id for superseded_by references

    # Mark the listed entries as superseded by this digest.
    if body.superseded_entry_ids:
        for eid in body.superseded_entry_ids:
            await db.execute(
                update(ResidentMemoryEntry)
                .where(
                    ResidentMemoryEntry.id == eid,
                    ResidentMemoryEntry.resident_id == resident_id,
                    ResidentMemoryEntry.superseded_by.is_(None),
                )
                .values(
                    superseded_by=digest_entry.id,
                    compacted_at=now,
                )
            )

    # F6: a digest is itself a live entry, so compact MUST net-reduce (or at
    # least not grow) the live set — otherwise repeated empty-supersession
    # compacts would create unlimited digests and bypass write_memory's cap.
    # Enforce the cap after the operation and roll back (no commit) if over.
    await db.flush()
    live_count = (
        await db.scalar(
            select(func.count(ResidentMemoryEntry.id)).where(
                ResidentMemoryEntry.resident_id == resident_id,
                ResidentMemoryEntry.superseded_by.is_(None),
                ResidentMemoryEntry.quarantined.is_(False),
            )
        )
    ) or 0
    if live_count > resident.max_uncompacted_entries:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "memory_cap_exceeded",
                "message": (
                    f"Compaction would leave {live_count} live entries, over "
                    f"the cap ({resident.max_uncompacted_entries}). Supersede "
                    "more entries in this compaction."
                ),
            },
        )

    await db.commit()
    await db.refresh(digest_entry)

    return _entry_to_response(digest_entry)


@router.post(
    "/{org_id}/residents/{resident_id}/memory/{entry_id}/quarantine",
    response_model=MemoryEntryResponse,
)
async def quarantine_memory_entry(
    org_id: str,
    resident_id: str,
    entry_id: str,
    auth: AuthContext = Depends(require_scope("resident_memory:write")),
    db: AsyncSession = Depends(get_db),
) -> MemoryEntryResponse:
    """F5: quarantine a specific memory entry (flag as poisoned).

    The resident's own key can quarantine its own entries, OR an org-admin
    can quarantine entries on implementer residents (owner-visibility, F5).
    """
    resident = await _fetch_resident(db, resident_id, org_id)
    # C5 + F5: the resident's own key OR an org owner/admin (recovery path).
    await _enforce_resident_isolation(db, resident, auth, allow_org_admin=True)

    entry = (
        await db.execute(
            select(ResidentMemoryEntry).where(
                ResidentMemoryEntry.id == entry_id,
                ResidentMemoryEntry.resident_id == resident_id,
                ResidentMemoryEntry.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory entry not found")

    entry.quarantined = True
    await db.commit()
    await db.refresh(entry)

    logger.info(
        "Memory entry quarantined: id=%s resident=%s",
        entry_id,
        resident_id,
    )
    return _entry_to_response(entry)
