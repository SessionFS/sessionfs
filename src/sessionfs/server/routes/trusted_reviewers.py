"""tk_f503ce5c24c54040 — admin CRUD surface for the trusted_reviewers registry.

Routes at /api/v1/orgs/{org_id}/trusted-reviewers (org-admin gated).

This endpoint GRANTS VERDICT-TRUST AUTHORITY: a row here decides whose review
verdicts the work-queue stop oracle (`is_registered_trusted_reviewer` in
routes/tickets.py) will count as authoritative. It is therefore gated exactly
like the org service-key surface (org-admin or owner; cross-org access →
404 existence-hiding) and every register/revoke writes an append-only
OrgAuditEvent.

The registry's two scope shapes (mirroring TrustedReviewer + migration 053
and how `is_registered_trusted_reviewer` reads them):
  - project-scoped: project_id set, org_id NULL → that one project.
  - org-wide:       org_id set, project_id NULL → every project in the org.

Revocation is a SOFT delete (is_active=false + revoked_at) — settled verdicts
are never rewritten and the audit trail is preserved (revoke-not-delete,
matching the service-key surface).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ApiKey,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    TrustedReviewer,
    User,
)
from sessionfs.server.schemas.trusted_reviewers import (
    TrustedReviewerCreateRequest,
    TrustedReviewerResponse,
    TrustedReviewerRevokeRequest,
)
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    check_role,
    get_user_context,
)

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/orgs", tags=["trusted-reviewers"])


async def _require_org_admin(
    org_id: str, ctx: UserContext, db: AsyncSession
) -> Organization:
    """Membership + role + tier gate, mirroring api_keys._require_org_admin.

    Cross-org access is existence-hiding: a caller that does not belong to
    {org_id} gets 404, never 403, so the endpoint never confirms an org's
    existence to a non-member. Owners satisfy check_role(ctx, 'admin')
    because owner (100) >= admin (50) in the role hierarchy.
    """
    check_feature(ctx, "team_management")
    check_role(ctx, "admin")
    if ctx.org is None or ctx.org.id != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return ctx.org


def _to_response(row: TrustedReviewer) -> TrustedReviewerResponse:
    return TrustedReviewerResponse(
        id=row.id,
        org_id=row.org_id,
        project_id=row.project_id,
        user_id=row.user_id,
        service_key_id=row.service_key_id,
        reviewer_persona=row.reviewer_persona,
        is_active=bool(row.is_active),
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _write_audit(
    db: AsyncSession,
    *,
    org: Organization,
    event_type: str,
    actor: User,
    actor_role: str | None,
    target_id: str,
    before: dict | None,
    after: dict | None,
) -> None:
    """Append-only OrgAuditEvent, mirroring the v0.11.0 org-audit pattern
    (org_members._emit_owner_transfer_audit). before/after are JSON-encoded
    snapshots of the registry row state around the mutation."""
    db.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_role_at_time=actor_role,
            target_type="trusted_reviewer",
            target_id=target_id,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


@router.post(
    "/{org_id}/trusted-reviewers",
    status_code=201,
    response_model=TrustedReviewerResponse,
)
async def register_trusted_reviewer(
    org_id: str,
    body: TrustedReviewerCreateRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TrustedReviewerResponse:
    """Register an identity as a trusted reviewer.

    Identity-AND-scope are pre-validated in-app (clean 422, never a DB-CHECK
    500). The bound identity is validated to belong to this org: a
    service_key_id must be an org service key; a user_id must be an org
    member; a project_id (when scoped) must be in the org.
    """
    org = await _require_org_admin(org_id, ctx, db)

    # ── Identity-AND-scope invariant (defense-in-depth over the schema
    # validator + the DB CHECK). Surface a clean 422, never a 500.
    if not body.user_id and not body.service_key_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "identity_required",
                "message": "Provide user_id and/or service_key_id.",
            },
        )

    # ── Bound-identity validation: the service key must belong to THIS org.
    if body.service_key_id is not None:
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

    # ── Bound-identity validation: the user must be a member of THIS org.
    if body.user_id is not None:
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.org_id == org_id,
                    OrgMember.user_id == body.user_id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "user_not_org_member",
                    "message": "user_id must be a member of this organization.",
                },
            )

    # ── Scope resolution. project-scoped → project_id set, org_id NULL;
    # org-wide → org_id set, project_id NULL (matches how
    # is_registered_trusted_reviewer reads the registry).
    row_org_id: str | None
    row_project_id: str | None
    if body.project_id is not None:
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
        row_org_id = None
        row_project_id = body.project_id
    else:
        # org-wide
        row_org_id = org_id
        row_project_id = None

    now = datetime.now(timezone.utc)
    row = TrustedReviewer(
        id=f"tr_{secrets.token_hex(12)}",
        org_id=row_org_id,
        project_id=row_project_id,
        user_id=body.user_id,
        service_key_id=body.service_key_id,
        reviewer_persona=body.reviewer_persona,
        is_active=body.is_active,
        created_by_user_id=user.id,
        created_at=now,
    )
    db.add(row)

    _write_audit(
        db,
        org=org,
        event_type="trusted_reviewer_registered",
        actor=user,
        actor_role=ctx.role,
        target_id=row.id,
        before=None,
        after={
            "org_id": row_org_id,
            "project_id": row_project_id,
            "user_id": body.user_id,
            "service_key_id": body.service_key_id,
            "reviewer_persona": body.reviewer_persona,
            "is_active": body.is_active,
        },
    )

    await db.commit()
    await db.refresh(row)

    logger.info(
        "Trusted reviewer registered: id=%s org=%s scope=%s persona=%s by=%s",
        row.id,
        org_id,
        "org-wide" if row_project_id is None else f"project:{row_project_id}",
        body.reviewer_persona,
        user.id,
    )
    return _to_response(row)


@router.get(
    "/{org_id}/trusted-reviewers",
    response_model=list[TrustedReviewerResponse],
)
async def list_trusted_reviewers(
    org_id: str,
    include_revoked: bool = Query(
        False, description="Include revoked (inactive) reviewers."
    ),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[TrustedReviewerResponse]:
    """List this org's trusted reviewers. Org admins/owners only.

    Returns both project-scoped rows (project belongs to the org) and
    org-wide rows (org_id == org). Active-only by default; pass
    include_revoked=true to also surface revoked rows.
    """
    await _require_org_admin(org_id, ctx, db)

    # Org-wide rows match on org_id; project-scoped rows have org_id NULL,
    # so resolve them via their project's org_id.
    project_ids_in_org = (
        select(Project.id).where(Project.org_id == org_id).scalar_subquery()
    )
    stmt = select(TrustedReviewer).where(
        (TrustedReviewer.org_id == org_id)
        | (TrustedReviewer.project_id.in_(project_ids_in_org))
    )
    if not include_revoked:
        stmt = stmt.where(TrustedReviewer.is_active.is_(True))
    stmt = stmt.order_by(TrustedReviewer.created_at.desc())

    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(r) for r in rows]


@router.delete(
    "/{org_id}/trusted-reviewers/{reviewer_id}",
    status_code=204,
)
async def revoke_trusted_reviewer(
    org_id: str,
    reviewer_id: str,
    body: TrustedReviewerRevokeRequest | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke (deactivate) a trusted reviewer — SOFT delete.

    Sets is_active=false + revoked_at. Settled TicketComment.verdict_trusted
    rows are never rewritten; only FUTURE verdicts from this identity stop
    being counted. 404 if the row is not in this org (existence-hiding).
    """
    org = await _require_org_admin(org_id, ctx, db)

    # Belongs-to-org check: org-wide row (org_id==org) OR project-scoped row
    # whose project is in the org.
    project_ids_in_org = (
        select(Project.id).where(Project.org_id == org_id).scalar_subquery()
    )
    row = (
        await db.execute(
            select(TrustedReviewer).where(
                TrustedReviewer.id == reviewer_id,
                (TrustedReviewer.org_id == org_id)
                | (TrustedReviewer.project_id.in_(project_ids_in_org)),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Trusted reviewer not found")

    if not row.is_active and row.revoked_at is not None:
        # Idempotent — already revoked.
        return

    now = datetime.now(timezone.utc)
    before = {
        "is_active": bool(row.is_active),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }
    await db.execute(
        update(TrustedReviewer)
        .where(TrustedReviewer.id == reviewer_id)
        .values(is_active=False, revoked_at=now)
    )

    _write_audit(
        db,
        org=org,
        event_type="trusted_reviewer_revoked",
        actor=user,
        actor_role=ctx.role,
        target_id=reviewer_id,
        before=before,
        after={
            "is_active": False,
            "revoked_at": now.isoformat(),
            "reason": body.reason if body else None,
        },
    )

    await db.commit()
    logger.info(
        "Trusted reviewer revoked: id=%s org=%s by=%s reason=%r",
        reviewer_id,
        org_id,
        user.id,
        (body.reason if body else None),
    )
