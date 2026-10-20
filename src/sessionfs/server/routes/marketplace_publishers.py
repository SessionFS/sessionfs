"""Marketplace publishers, verification and trusted publishing (§4).

Owner surface (user keys only; publisher identity is not automation's to
change):

- ``POST /api/v1/publishers`` claim a namespace (personal: Starter+
  ``marketplace_access``; org: owner/admin + Team+ ``team_skill_library``).
- ``GET /api/v1/publishers`` publishers you manage; ``GET /publishers/{id}``
  public view (any ``marketplace:read`` key) with the owner detail for
  managers; ``DELETE /publishers/{id}`` tombstones (the namespace is never
  released).
- ``POST /publishers/{id}/verifications`` (``dns_txt`` with a domain, or
  ``github_org`` / ``github_user`` with one of YOUR GitHub App
  installations), ``.../{vid}/check``, ``DELETE .../{vid}``.
- ``GET/POST/DELETE /publishers/{id}/trusted-publishers``.
- ``GET /publishers/{id}/listings`` including listings awaiting review.

Trusted publishing (no API key):

- ``POST /api/v1/marketplace/trusted-publishing/token`` exchanges a GitHub
  Actions OIDC token for a 15-minute, single-use, package-scoped publish
  token (Cloud Armor sensitive tier + app-layer limits).
- ``POST /api/v1/marketplace/trusted-publishing/publish`` uploads with that
  token; the version records the verified build provenance.

Platform admin:

- publishers list / create-with-override / suspend / restore / public
  publishing cohort, reserved names, the verification re-check sweeper, and
  the public-listing review queue (approve / reject).
"""

from __future__ import annotations

import functools
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs import __version__ as sessionfs_version
from sessionfs.server import github_app
from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    GitHubInstallation,
    OrgDomainVerification,
    OrgMember,
    Organization,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageVersion,
    SkillPublisher,
    SkillPublisherVerification,
    SkillReservedName,
    SkillTrustedPublisher,
    User,
)
from sessionfs.server.routes import marketplace as mp
from sessionfs.server.services import domain_verification
from sessionfs.server.services import github_oidc
from sessionfs.server.services import publishers as svc
from sessionfs.server.services.oidc_token import TokenValidationError
from sessionfs.server.tier_gate import get_effective_tier
from sessionfs.server.tiers import get_features_for_tier
from sessionfs.skills.confusables import skeleton

router = APIRouter(prefix="/api/v1", tags=["skill-publishers"], route_class=mp._MarketplaceRoute)

MAX_PUBLISHERS_PER_OWNER = svc.MAX_PUBLISHERS_PER_OWNER
MAX_PUBLISHER_CLAIMS_LIFETIME = svc.MAX_PUBLISHER_CLAIMS_LIFETIME
_claim_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=3600)
MAX_TRUSTED_PUBLISHERS = 20
_exchange_ip_limiter = SlidingWindowRateLimiter(max_requests=60, window_seconds=60)
_exchange_repo_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=600)
_PACKAGE_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PublisherClaim(BaseModel):
    namespace: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = Field(None, max_length=200)
    org_id: str | None = Field(None, max_length=64)


class VerificationCreate(BaseModel):
    method: Literal["dns_txt", "github_org", "github_user"]
    domain: str | None = Field(None, max_length=255)
    installation_id: int | None = Field(None, ge=1)


class TrustedPublisherCreate(BaseModel):
    repository: str = Field(..., max_length=200)
    workflow: str = Field(..., max_length=255)
    environment: str | None = Field(None, max_length=255)
    ref_pattern: str | None = Field(None, max_length=255)
    package_name: str | None = Field(None, max_length=100)
    repository_id: int | None = Field(None, ge=1)
    repository_owner_id: int | None = Field(None, ge=1)
    reusable_workflow: str | None = Field(None, max_length=300)
    allow_workflow_run: bool = False


class TokenExchange(BaseModel):
    token: str = Field(..., min_length=1, max_length=16384)
    namespace: str | None = Field(None, max_length=64)
    package: str | None = Field(None, max_length=100)


class ReasonBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=2000)


class CohortBody(BaseModel):
    enabled: bool


class AdminPublisherCreate(BaseModel):
    namespace: str = Field(..., min_length=1, max_length=64)
    owner_type: Literal["org", "user"]
    owner_id: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = Field(None, max_length=200)
    reason: str = Field(..., min_length=3, max_length=2000)


class ReservedNameCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=39)
    reason: Literal["brand", "popular", "tombstone"] = "brand"
    required_subject: str | None = Field(None, max_length=255)


class RecheckBody(BaseModel):
    limit: int = Field(200, ge=1, le=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_user_key(auth: AuthContext) -> None:
    if auth.key_kind != "user":
        raise HTTPException(
            403,
            detail={
                "code": "service_key_not_allowed",
                "message": "Publisher management requires a user key.",
            },
        )


def _is_platform_admin(auth: AuthContext) -> bool:
    return auth.key_kind == "user" and auth.user.tier == "admin"


def _require_platform_admin(auth: AuthContext) -> None:
    _require_user_key(auth)
    if not _is_platform_admin(auth):
        raise HTTPException(403, "Admin access required")


async def _require_personal_tier(db: AsyncSession, user: User) -> None:
    if user.tier == "admin":
        return
    tier = await get_effective_tier(user, db)
    if "marketplace_access" not in get_features_for_tier(tier):
        raise HTTPException(
            403,
            detail={
                "code": "upgrade_required",
                "message": "Publishers and trusted publishing require the Starter tier or above.",
                "feature": "marketplace_access",
                "current_tier": tier.value,
            },
        )


async def _fence_personal_claim(db: AsyncSession, user_id: str) -> None:
    """The personal claim's final authorization, run BEFORE the quota locks so
    the lock order stays membership -> entitlement -> user (matching the
    publish fence): the primary membership FOR SHARE, the owning entitlement
    FOR UPDATE, then the user row reloaded — still active and still on the
    personal publishing tier. A deactivation or downgrade committed after
    authentication must not acquire a permanent namespace."""
    await mp._fence_personal_tier(db, user_id=user_id)


async def _publisher_or_404(db: AsyncSession, publisher_id: str) -> SkillPublisher:
    publisher = await db.get(SkillPublisher, publisher_id)
    if publisher is None:
        raise HTTPException(404, "Publisher not found")
    return publisher


async def _org_role(db: AsyncSession, org_id: str, user_id: str) -> str | None:
    return (
        await db.execute(
            select(OrgMember.role).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
        )
    ).scalar_one_or_none()


async def _manage(
    db: AsyncSession, auth: AuthContext, publisher_id: str, *, write: bool = True
) -> tuple[SkillPublisher, str, Organization | None]:
    """(publisher, role, org) for someone allowed to manage the publisher.
    Personal: the owner. Org: owner/admin (members get 403, outsiders a 404
    so publisher management never reveals org membership). Platform admins
    may manage any. Writes also re-check the owner's plan."""
    _require_user_key(auth)
    if write:
        # Every lifecycle mutation serializes on the publisher row (the same
        # lock every publish under this publisher takes).
        locked = await svc.lock_publisher(db, publisher_id)
        if locked is None:
            raise HTTPException(404, "Publisher not found")
        publisher = locked
    else:
        publisher = await _publisher_or_404(db, publisher_id)
    if publisher.owner_type == "system":
        if not _is_platform_admin(auth):
            raise HTTPException(404, "Publisher not found")
        return publisher, "platform_admin", None
    org: Organization | None = None
    if publisher.owner_type == "user":
        if publisher.user_id == auth.user.id:
            role = "owner"
        elif _is_platform_admin(auth):
            role = "platform_admin"
        else:
            raise HTTPException(404, "Publisher not found")
        if write and role == "owner":
            await _require_personal_tier(db, auth.user)
    else:
        org = await db.get(Organization, publisher.org_id)
        # Writes fence the caller's membership FOR SHARE (lock order:
        # publisher -> membership): a concurrent removal or demotion either
        # committed first and is seen here, or waits for this commit.
        member_role = (
            await fenced_org_role(db, publisher.org_id or "", auth.user.id)
            if write
            else await _org_role(db, publisher.org_id or "", auth.user.id)
        )
        if _is_platform_admin(auth):
            role = "platform_admin"
        elif member_role is None or org is None:
            raise HTTPException(404, "Publisher not found")
        elif member_role not in ("owner", "admin"):
            raise HTTPException(403, "Organization admin or owner role required")
        else:
            role = member_role
        if write and org is not None and role != "platform_admin":
            await mp._check_org_feature(db, org, "team_skill_library")
    if write and publisher.status == "tombstoned":
        raise HTTPException(409, detail={"code": "publisher_tombstoned", "message": "This publisher is retired."})
    return publisher, role, org


def _org_audit(
    db: AsyncSession,
    *,
    org: Organization | None,
    auth: AuthContext,
    role: str,
    event_type: str,
    target_type: str,
    target_id: str,
    after: dict,
) -> None:
    if org is not None:
        mp._audit(
            db,
            org=org,
            auth=auth,
            role=role,
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            after=after,
        )


def _admin_audit(db: AsyncSession, auth: AuthContext, action: str, target_type: str, target_id: str, details: dict) -> None:
    db.add(
        AdminAction(
            id=str(uuid.uuid4()),
            admin_id=auth.user.id,
            action=action[:50],
            target_type=target_type[:20],
            target_id=target_id[:64],
            details=json.dumps(details, sort_keys=True),
        )
    )


def _iso(value) -> str | None:
    value = svc.utc(value)
    return value.isoformat() if value is not None else None


def _verification_response(row: SkillPublisherVerification) -> dict:
    data = {
        "id": row.id,
        "method": row.method,
        "subject": row.subject,
        "subject_display": row.subject_display,
        "status": row.status,
        "status_reason": row.status_reason,
        "badge": svc.badge_text(row) if row.status == "verified" else None,
        "verified_at": _iso(row.verified_at),
        "last_checked_at": _iso(row.last_checked_at),
        "last_success_at": _iso(row.last_success_at),
        "consecutive_failures": row.consecutive_failures,
        "github_installation_id": row.github_installation_id,
        "reused_org_domain_verification": row.org_domain_verification_id is not None,
    }
    if row.method == "dns_txt" and row.verification_token:
        data["txt_record"] = domain_verification.txt_record(row.verification_token)
        data["txt_host"] = row.subject
    return data


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _trusted_response(row: SkillTrustedPublisher) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "repository": row.repository,
        "repository_id": row.repository_id,
        "repository_owner_id": row.repository_owner_id,
        "workflow": row.workflow_path,
        "reusable_workflow": row.reusable_workflow,
        "allow_workflow_run": row.allow_workflow_run,
        "environment": row.environment,
        "ref_pattern": row.ref_pattern,
        "package_name": row.package_name,
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
    }


async def _publisher_response(db: AsyncSession, publisher: SkillPublisher, *, manager: bool) -> dict:
    block = (await svc.publisher_blocks(db, [publisher.id])).get(publisher.id, {})
    data: dict[str, Any] = {
        **block,
        "org_id": publisher.org_id,
        "created_at": _iso(publisher.created_at),
    }
    if not manager:
        # The public view names the namespace and its badges, never the
        # owning org or user ids.
        data.pop("org_id", None)
        return data
    verifications = (
        await db.execute(
            select(SkillPublisherVerification)
            .where(SkillPublisherVerification.publisher_id == publisher.id)
            .order_by(SkillPublisherVerification.created_at.desc(), SkillPublisherVerification.id)
        )
    ).scalars().all()
    trusted = (
        await db.execute(
            select(SkillTrustedPublisher)
            .where(
                SkillTrustedPublisher.publisher_id == publisher.id,
                SkillTrustedPublisher.revoked_at.is_(None),
            )
            .order_by(SkillTrustedPublisher.created_at.desc(), SkillTrustedPublisher.id)
        )
    ).scalars().all()
    data.update(
        {
            "user_id": publisher.user_id,
            "public_publishing": publisher.public_publishing,
            "suspended_reason": publisher.suspended_reason,
            "review_flags": _json_list(publisher.review_flags),
            "verifications": [_verification_response(row) for row in verifications],
            "trusted_publishers": [_trusted_response(row) for row in trusted],
        }
    )
    return data


# ---------------------------------------------------------------------------
# Claim / view / tombstone
# ---------------------------------------------------------------------------


@router.post("/publishers", status_code=201)
async def claim_publisher(
    body: PublisherClaim,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_user_key(auth)
    org: Organization | None = None
    role = "owner"
    if body.org_id:
        org, role = await mp._org_access(db, auth, body.org_id, admin=True)
        if role != "platform_admin":
            await mp._check_org_feature(db, org, "team_skill_library")
        owner_type, owner_id = "org", org.id
    else:
        await _require_personal_tier(db, auth.user)
        owner_type, owner_id = "user", auth.user.id
    svc.validate_namespace_shape(body.namespace)
    # Per-process limiter for refused attempts (probing); the durable,
    # cross-process limits are enforced under row locks below.
    if not _claim_limiter.is_allowed(f"user:{auth.user.id}"):
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Too many namespace claims; retry later."})
    # Lock order: namespace -> owner rows. Serializes this claim with any
    # first publish into the same (or a lookalike) namespace.
    await svc.lock_confusable_promotion(db)
    await svc.lock_namespace(db, body.namespace)
    if org is not None and role != "platform_admin":
        # Final fenced re-authorization, held through commit (lock order:
        # global -> namespace -> membership -> org entitlement -> owner rows).
        fenced = await fenced_org_role(db, org.id, auth.user.id)
        if fenced not in ("owner", "admin"):
            raise HTTPException(403, "Organization admin or owner role required")
        role = fenced
        await mp._fence_org_feature(db, org_id=org.id)
    elif owner_type == "user":
        # Final fenced re-authorization BEFORE the quota locks so the lock
        # order stays membership -> entitlement -> user (matching the publish
        # fence): re-check active + personal tier under the entitlement lock.
        await _fence_personal_claim(db, auth.user.id)
    await svc.enforce_claim_quota(
        db, owner_type=owner_type, owner_id=owner_id, claimant_user_id=auth.user.id
    )
    publisher, flags = await svc.claim_namespace(
        db,
        namespace=body.namespace,
        owner_type=owner_type,
        org_id=owner_id if owner_type == "org" else None,
        user_id=owner_id if owner_type == "user" else None,
        display_name=body.display_name,
        created_by_user_id=auth.user.id,
    )
    _org_audit(
        db,
        org=org,
        auth=auth,
        role=role,
        event_type="skill_publisher_claimed",
        target_type="skill_publisher",
        target_id=publisher.id,
        after={"namespace": publisher.namespace, "review_flags": flags},
    )
    await db.commit()
    data = await _publisher_response(db, publisher, manager=True)
    return data


@router.get("/publishers")
async def list_my_publishers(
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_user_key(auth)
    org_ids = list(
        (
            await db.execute(
                select(OrgMember.org_id).where(
                    OrgMember.user_id == auth.user.id, OrgMember.role.in_(("owner", "admin"))
                )
            )
        ).scalars().all()
    )
    conditions = [(SkillPublisher.owner_type == "user") & (SkillPublisher.user_id == auth.user.id)]
    if org_ids:
        conditions.append((SkillPublisher.owner_type == "org") & (SkillPublisher.org_id.in_(org_ids)))
    rows = (
        await db.execute(
            select(SkillPublisher)
            .where(or_(*conditions), SkillPublisher.status != "tombstoned")
            .order_by(SkillPublisher.created_at, SkillPublisher.id)
        )
    ).scalars().all()
    items = [await _publisher_response(db, row, manager=True) for row in rows]
    return {"items": items, "count": len(items)}


@router.get("/publishers/{publisher_id}")
async def get_publisher(
    publisher_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    publisher = await _publisher_or_404(db, publisher_id)
    manager = False
    if auth.key_kind == "user":
        try:
            await _manage(db, auth, publisher_id, write=False)
            manager = True
        except HTTPException:
            manager = False
    if publisher.status == "tombstoned" and not manager:
        raise HTTPException(404, "Publisher not found")
    return await _publisher_response(db, publisher, manager=manager)


@router.delete("/publishers/{publisher_id}", status_code=200)
async def tombstone_publisher(
    publisher_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Retire a publisher. The namespace (and its lookalikes) can never be
    claimed again; verifications are revoked and trusted publishers removed.
    Existing packages and versions stay as they are."""
    # Retiring changes the protected-name set: global lock first.
    await svc.lock_confusable_promotion(db)
    publisher, role, org = await _manage(db, auth, publisher_id)
    if publisher.owner_type == "system":
        raise HTTPException(409, detail={"code": "system_publisher", "message": "The platform publisher cannot be retired."})
    now = svc.now_utc()
    publisher.status = "tombstoned"
    publisher.public_publishing = False
    svc.bump_auth_generation(publisher)
    await db.execute(
        update(SkillPublisherVerification)
        .where(SkillPublisherVerification.publisher_id == publisher.id)
        .values(status="revoked", status_reason="publisher_tombstoned")
    )
    await db.execute(
        update(SkillTrustedPublisher)
        .where(
            SkillTrustedPublisher.publisher_id == publisher.id,
            SkillTrustedPublisher.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by_user_id=auth.user.id)
    )
    _org_audit(
        db, org=org, auth=auth, role=role, event_type="skill_publisher_tombstoned",
        target_type="skill_publisher", target_id=publisher.id, after={"namespace": publisher.namespace},
    )
    await db.commit()
    return {"id": publisher.id, "status": publisher.status}


@router.get("/publishers/{publisher_id}/listings")
async def publisher_listings(
    publisher_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every listing of the publisher's packages, including those awaiting
    review or rejected (managers only)."""
    publisher, _, _ = await _manage(db, auth, publisher_id, write=False)
    rows = (
        await db.execute(
            select(SkillMarketplaceListing, SkillPackage, SkillPackageVersion)
            .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
            .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
            .where(SkillPackage.publisher_id == publisher.id)
            .order_by(SkillMarketplaceListing.published_at.desc(), SkillMarketplaceListing.id.desc())
            .limit(200)
        )
    ).all()
    items = []
    for listing, package, version in rows:
        item = mp._package_response(listing, package, version)
        item["moderation_reason"] = listing.moderation_reason
        items.append(item)
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Verifications
# ---------------------------------------------------------------------------


async def _create_dns_verification(
    db: AsyncSession, auth: AuthContext, publisher: SkillPublisher, domain_raw: str | None
) -> SkillPublisherVerification:
    try:
        domain = domain_verification.normalize_domain(domain_raw or "")
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "invalid_domain", "message": str(exc)}) from None
    held = (
        await db.execute(
            select(SkillPublisherVerification).where(
                SkillPublisherVerification.method == "dns_txt",
                SkillPublisherVerification.subject == domain,
                SkillPublisherVerification.status == "verified",
            )
        )
    ).scalar_one_or_none()
    if held is not None and held.publisher_id != publisher.id:
        raise HTTPException(409, detail={"code": "subject_verified_elsewhere", "message": "This domain is already verified by another publisher."})
    if held is not None:
        raise HTTPException(409, detail={"code": "already_verified", "message": "This publisher already verified this domain."})
    other_org = (
        await db.execute(
            select(OrgDomainVerification.org_id).where(
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "verified",
            )
        )
    ).scalar_one_or_none()
    if other_org is not None and other_org != publisher.org_id:
        raise HTTPException(409, detail={"code": "subject_verified_elsewhere", "message": "This domain is verified by another organization."})
    pending = (
        await db.execute(
            select(SkillPublisherVerification).where(
                SkillPublisherVerification.publisher_id == publisher.id,
                SkillPublisherVerification.method == "dns_txt",
                SkillPublisherVerification.subject == domain,
                SkillPublisherVerification.status.in_(("pending", "lapsed")),
            )
        )
    ).scalars().first()
    if pending is not None:
        return pending
    # SSO reuse: the org already proved this domain. Reusing it is an explicit
    # owner/admin action (this request), audit-logged, and re-checked live now.
    reused: OrgDomainVerification | None = None
    if publisher.owner_type == "org" and other_org == publisher.org_id:
        reused = (
            await db.execute(
                select(OrgDomainVerification).where(
                    OrgDomainVerification.org_id == publisher.org_id,
                    OrgDomainVerification.domain == domain,
                    OrgDomainVerification.status == "verified",
                )
            )
        ).scalar_one_or_none()
    row = SkillPublisherVerification(
        id=f"spv_{secrets.token_hex(12)}",
        publisher_id=publisher.id,
        method="dns_txt",
        subject=domain,
        subject_display=domain,
        verification_token=reused.verification_token if reused else secrets.token_hex(20),
        org_domain_verification_id=reused.id if reused else None,
        status="pending",
        created_by_user_id=auth.user.id,
    )
    db.add(row)
    await db.flush()
    return row


async def _owned_installation_or_error(db: AsyncSession, auth: AuthContext, installation_id: int) -> None:
    # Sentinel N1: only the CALLER's own GitHub App installations count.
    owned = (
        await db.execute(
            select(GitHubInstallation.id)
            .where(
                GitHubInstallation.id == installation_id,
                GitHubInstallation.user_id == auth.user.id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(404, detail={"code": "installation_not_found", "message": "That GitHub App installation is not linked to your account."})
    # ...and only through a VERIFIED claim binding (tk_b6c5e184ba8d4aa1). A
    # legacy installation-id claim can be hijacked, so it never earns a badge.
    if not await svc.installation_owned_by(db, auth.user.id, installation_id):
        raise HTTPException(
            409,
            detail={
                "code": "installation_claim_unverified",
                "message": "Re-link the GitHub App from Settings to confirm you own this installation, "
                "then verify again.",
            },
        )


def _installation_unusable() -> HTTPException:
    return HTTPException(409, detail={"code": "installation_suspended", "message": "That GitHub App installation is suspended or removed."})


async def _github_account_preflight(
    db: AsyncSession, auth: AuthContext, method: str, installation_id: int | None
) -> tuple[int, dict]:
    """Ownership checks and the live GitHub lookup, with NO lock held."""
    if installation_id is None:
        raise HTTPException(422, detail={"code": "installation_required", "message": "installation_id is required"})
    await _owned_installation_or_error(db, auth, installation_id)
    try:
        account = await github_app.get_installation_account(installation_id)
    except Exception:
        raise HTTPException(503, detail={"code": "github_unavailable", "message": "Could not reach GitHub to confirm the installation; try again."}) from None
    if account is None:
        raise HTTPException(404, detail={"code": "installation_not_found", "message": "That GitHub App installation no longer exists."})
    if account.get("suspended"):
        raise _installation_unusable()
    expected = "Organization" if method == "github_org" else "User"
    if account.get("type") != expected:
        raise HTTPException(422, detail={"code": "installation_account_type", "message": f"{method} needs an installation on a GitHub {expected.lower()} account."})
    return installation_id, account


async def _create_github_verification(
    db: AsyncSession, auth: AuthContext, publisher: SkillPublisher, method: str, installation_id: int, account: dict
) -> SkillPublisherVerification:
    """Caller holds the installation lock and then the publisher lock:
    re-validate the installation under them (a signed suspend / delete
    webhook either committed first and is seen here, or waits and then
    revokes this row)."""
    await _owned_installation_or_error(db, auth, installation_id)
    if not await svc.installation_usable(db, installation_id):
        raise _installation_unusable()
    now = svc.now_utc()
    row = SkillPublisherVerification(
        id=f"spv_{secrets.token_hex(12)}",
        publisher_id=publisher.id,
        method=method,
        subject=str(account["id"]),
        subject_display=f"github.com/{account.get('login')}"[:255],
        github_installation_id=installation_id,
        status="verified",
        verified_at=now,
        last_checked_at=now,
        last_success_at=now,
        created_by_user_id=auth.user.id,
    )
    if await svc.subject_held_elsewhere(db, row):
        raise _subject_conflict("This GitHub account is already verified by another publisher.")
    db.add(row)
    return row


def _subject_conflict(message: str) -> HTTPException:
    return HTTPException(409, detail={"code": "subject_verified_elsewhere", "message": message})


async def _commit_verification(db: AsyncSession) -> None:
    """Commit; the partial unique index on verified (method, subject) is the
    race backstop, surfaced as a 409 rather than a 500."""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _subject_conflict("This subject was verified by another publisher meanwhile.") from None


@router.post("/publishers/{publisher_id}/verifications", status_code=201)
async def create_verification(
    publisher_id: str,
    body: VerificationCreate,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Authorize first (no lock), then lock in the global order: installation
    # (GitHub) -> domain (DNS) -> publisher.
    publisher, _, _ = await _manage(db, auth, publisher_id, write=False)
    if publisher.owner_type == "system":
        raise HTTPException(409, detail={"code": "system_publisher", "message": "The platform publisher needs no verification."})
    reuse_check = False
    if body.method == "dns_txt":
        publisher, role, org = await _manage(db, auth, publisher_id)
        row = await _create_dns_verification(db, auth, publisher, body.domain)
        reuse_check = row.status == "pending" and row.org_domain_verification_id is not None
    else:
        # The method names the account kind; it must match the publisher's
        # owner kind both ways.
        expected_owner = "org" if body.method == "github_org" else "user"
        if publisher.owner_type != expected_owner:
            raise HTTPException(
                422,
                detail={
                    "code": "method_owner_mismatch",
                    "message": f"{body.method} verification is for {expected_owner} publishers.",
                },
            )
        installation_id, account = await _github_account_preflight(db, auth, body.method, body.installation_id)
        await svc.lock_confusable_promotion(db)
        await svc.lock_installation(db, installation_id)
        publisher, role, org = await _manage(db, auth, publisher_id)
        conflict = await svc.promotion_conflict(db, publisher)
        if conflict is not None:
            raise svc.confusable_error(conflict)
        row = await _create_github_verification(db, auth, publisher, body.method, installation_id, account)
    _org_audit(
        db, org=org, auth=auth, role=role, event_type="skill_publisher_verification_started",
        target_type="skill_publisher", target_id=publisher.id,
        after={"method": row.method, "subject": row.subject, "status": row.status,
               "reused_sso_domain": row.org_domain_verification_id is not None},
    )
    await _commit_verification(db)
    if reuse_check:
        # Reused SSO proof: re-check it live now. The DNS lookup runs with no
        # lock held; the transition is applied under the publisher lock.
        row_id, domain = row.id, row.subject
        probe = await svc.probe_verification(db, row)
        await svc.lock_confusable_promotion(db)
        await svc.lock_domain(db, domain)
        await _manage(db, auth, publisher_id)
        current = await svc.reload_verification(db, row_id)
        if current is not None:
            await svc.apply_probe(db, current, probe)
            row = current
        await _commit_verification(db)
    return _verification_response(row)


async def _verification_or_404(db: AsyncSession, publisher: SkillPublisher, verification_id: str) -> SkillPublisherVerification:
    row = await db.get(SkillPublisherVerification, verification_id)
    if row is None or row.publisher_id != publisher.id:
        raise HTTPException(404, "Verification not found")
    return row


@router.post("/publishers/{publisher_id}/verifications/{verification_id}/check")
async def check_verification(
    publisher_id: str,
    verification_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Authorize, then probe the source of truth (DNS / GitHub) with NO lock
    # held; then take the publisher lock, re-read the row and apply.
    publisher, _, _ = await _manage(db, auth, publisher_id, write=False)
    row = await _verification_or_404(db, publisher, verification_id)
    if row.status == "revoked":
        raise HTTPException(409, detail={"code": "verification_revoked", "message": "This verification was revoked; start a new one."})
    probe = await svc.probe_verification(db, row)
    await svc.lock_confusable_promotion(db)
    if row.method == "dns_txt":
        await svc.lock_domain(db, row.subject)
    publisher, role, org = await _manage(db, auth, publisher_id)
    current = await svc.reload_verification(db, verification_id)
    if current is None or current.publisher_id != publisher.id:
        raise HTTPException(404, "Verification not found")
    row = current
    if row.status == "revoked":
        raise HTTPException(409, detail={"code": "verification_revoked", "message": "This verification was revoked; start a new one."})
    before = row.status
    after = await svc.apply_probe(db, row, probe)
    if after != before:
        _org_audit(
            db, org=org, auth=auth, role=role, event_type="skill_publisher_verification_checked",
            target_type="skill_publisher", target_id=publisher.id,
            after={"verification_id": row.id, "from": before, "to": after},
        )
    await _commit_verification(db)
    return _verification_response(row)


@router.delete("/publishers/{publisher_id}/verifications/{verification_id}")
async def revoke_verification(
    publisher_id: str,
    verification_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    publisher, role, org = await _manage(db, auth, publisher_id)
    row = await _verification_or_404(db, publisher, verification_id)
    row.status = "revoked"
    row.status_reason = "removed_by_owner"
    _org_audit(
        db, org=org, auth=auth, role=role, event_type="skill_publisher_verification_revoked",
        target_type="skill_publisher", target_id=publisher.id, after={"verification_id": row.id},
    )
    await db.commit()
    return _verification_response(row)


# ---------------------------------------------------------------------------
# Trusted publishers
# ---------------------------------------------------------------------------


@router.get("/publishers/{publisher_id}/trusted-publishers")
async def list_trusted_publishers(
    publisher_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    publisher, _, _ = await _manage(db, auth, publisher_id, write=False)
    rows = (
        await db.execute(
            select(SkillTrustedPublisher)
            .where(SkillTrustedPublisher.publisher_id == publisher.id)
            .order_by(SkillTrustedPublisher.created_at.desc(), SkillTrustedPublisher.id)
        )
    ).scalars().all()
    return {"items": [_trusted_response(row) for row in rows], "count": len(rows)}


@router.post("/publishers/{publisher_id}/trusted-publishers", status_code=201)
async def add_trusted_publisher(
    publisher_id: str,
    body: TrustedPublisherCreate,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Authorize, validate and resolve the repository on GitHub with NO lock
    # held; then take the publisher lock and fence the caller's membership
    # and re-check everything before inserting.
    publisher, _, _ = await _manage(db, auth, publisher_id, write=False)
    if publisher.owner_type == "system":
        raise HTTPException(409, detail={"code": "system_publisher", "message": "The platform publisher does not use trusted publishing."})
    repository, workflow, environment, ref_pattern = svc.validate_trusted_publisher_input(
        repository=body.repository,
        workflow=body.workflow,
        environment=body.environment,
        ref_pattern=body.ref_pattern,
    )
    reusable_workflow = svc.validate_reusable_workflow(body.reusable_workflow)
    package_name = (body.package_name or "").strip() or None
    if package_name is not None and (
        set(package_name) - _PACKAGE_NAME_CHARS
        or package_name[0] in ".-"
        or package_name[-1] in ".-"
    ):
        raise HTTPException(422, detail={"code": "invalid_package_name", "message": "package_name must be a lowercase package name"})
    if body.repository_id is not None and body.repository_owner_id is not None:
        repo_id, owner_id, display = body.repository_id, body.repository_owner_id, repository
    else:
        owner, name = repository.split("/", 1)
        try:
            resolved = await github_app.resolve_repository(db, auth.user.id, owner, name)
        except Exception:
            resolved = None
        if resolved is None:
            raise HTTPException(
                422,
                detail={
                    "code": "repository_not_found",
                    "message": "Could not resolve the repository on GitHub. Install the SessionFS "
                    "GitHub App on it, or pass repository_id and repository_owner_id.",
                },
            )
        repo_id, owner_id, display = int(resolved["id"]), int(resolved["owner_id"]), str(resolved["full_name"])
    # Fenced section: publisher lock, then the caller's membership FOR SHARE
    # (a concurrent removal / demotion either committed first and is seen,
    # or waits for this insert), then status, tier and trust count again.
    publisher, role, org = await _manage(db, auth, publisher_id)
    # No delegation (platform administrators included): a trust publishes AS
    # its creator, so only the owning user (personal) or a fenced owner /
    # admin of the org may create one.
    if publisher.owner_type == "user" and publisher.user_id != auth.user.id:
        raise HTTPException(403, "Only the publisher's owner can add trusted publishers.")
    if org is not None:
        fenced = await fenced_org_role(db, org.id, auth.user.id)
        if fenced not in ("owner", "admin"):
            raise HTTPException(403, "Organization admin or owner role required")
        role = fenced
    if publisher.status != "active":
        raise HTTPException(409, detail={"code": "publisher_not_active", "message": "Only an active publisher can add trusted publishers."})
    active = (
        await db.execute(
            select(SkillTrustedPublisher.id).where(
                SkillTrustedPublisher.publisher_id == publisher.id,
                SkillTrustedPublisher.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    if len(active) >= MAX_TRUSTED_PUBLISHERS:
        raise HTTPException(409, detail={"code": "trusted_publisher_limit", "message": f"At most {MAX_TRUSTED_PUBLISHERS} trusted publishers per publisher."})
    row = SkillTrustedPublisher(
        id=f"stp_{secrets.token_hex(12)}",
        publisher_id=publisher.id,
        package_name=package_name,
        provider="github",
        repository_owner_id=owner_id,
        repository_id=repo_id,
        repository=display[:200],
        workflow_path=workflow,
        reusable_workflow=reusable_workflow,
        allow_workflow_run=body.allow_workflow_run,
        environment=environment,
        ref_pattern=ref_pattern,
        created_by_user_id=auth.user.id,
    )
    db.add(row)
    _org_audit(
        db, org=org, auth=auth, role=role, event_type="skill_trusted_publisher_added",
        target_type="skill_publisher", target_id=publisher.id,
        after={"trusted_publisher_id": row.id, "repository_id": repo_id, "workflow": workflow,
               "reusable_workflow": reusable_workflow, "allow_workflow_run": body.allow_workflow_run,
               "environment": environment, "ref_pattern": ref_pattern, "package_name": package_name},
    )
    await db.commit()
    return _trusted_response(row)


@router.delete("/publishers/{publisher_id}/trusted-publishers/{trusted_id}")
async def remove_trusted_publisher(
    publisher_id: str,
    trusted_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    publisher, role, org = await _manage(db, auth, publisher_id)
    row = await db.get(SkillTrustedPublisher, trusted_id)
    if row is None or row.publisher_id != publisher.id:
        raise HTTPException(404, "Trusted publisher not found")
    if row.revoked_at is None:
        row.revoked_at = svc.now_utc()
        row.revoked_by_user_id = auth.user.id
        # Tokens minted under ANY trust of this publisher before the removal
        # stop working (the revoked trust's own tokens already do).
        svc.bump_auth_generation(publisher)
        _org_audit(
            db, org=org, auth=auth, role=role, event_type="skill_trusted_publisher_removed",
            target_type="skill_publisher", target_id=publisher.id, after={"trusted_publisher_id": row.id},
        )
    await db.commit()
    return _trusted_response(row)


# ---------------------------------------------------------------------------
# Trusted publishing: OIDC exchange and publish
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _trusted_publishing_settings(request: Request) -> github_oidc.TrustedPublishingSettings:
    """The settings resolved once at app creation (create_app)."""
    settings = getattr(request.app.state, "trusted_publishing", None)
    if not isinstance(settings, github_oidc.TrustedPublishingSettings):
        # Not created through create_app: trusted publishing stays off.
        return github_oidc.TrustedPublishingSettings(audience=None, development=False, source="unset")
    return settings


@router.get("/marketplace/trusted-publishing/config")
async def trusted_publishing_config(request: Request) -> dict:
    """Public: the OIDC audience and issuer this server accepts, so CI (the
    CLI, the example workflow) requests a token for exactly this deployment."""
    audience = _trusted_publishing_settings(request).audience
    return {
        "enabled": audience is not None,
        "audience": audience,
        "issuer": github_oidc.GITHUB_ACTIONS_ISSUER,
        "allowed_events": sorted(svc.PUBLISH_EVENTS),
    }


@router.post("/marketplace/trusted-publishing/token")
async def exchange_token(
    request: Request,
    body: TokenExchange,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Exchange a GitHub Actions OIDC token (``aud`` = this server's trusted
    publishing audience) for a short-lived publish token. Unauthenticated
    except for the OIDC token itself; every failure is a 401/403 with a
    stable code and no detail about which binding failed."""
    if not _exchange_ip_limiter.is_allowed(_client_ip(request)):
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Too many token exchanges; retry shortly."})
    try:
        claims = await github_oidc.verify_github_actions_token(
            body.token, _trusted_publishing_settings(request).audience
        )
    except TokenValidationError as exc:
        if exc.reason == "not_configured":
            raise HTTPException(
                503,
                detail={
                    "code": "trusted_publishing_not_configured",
                    "message": "Trusted publishing is not configured on this server.",
                },
            ) from None
        raise HTTPException(
            401,
            detail={"code": "invalid_oidc_token", "message": "The OIDC token was rejected.", "reason": exc.reason},
        ) from None
    if not _exchange_repo_limiter.is_allowed(f"repo:{claims['repository_id']}"):
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Too many token exchanges; retry shortly."})
    namespace = (body.namespace or "").strip().lower() or None
    package = (body.package or "").strip().lower() or None
    raw, token, trusted, publisher = await svc.exchange_oidc_claims(
        db, claims, requested_package=package, namespace=namespace
    )
    await db.commit()
    return {
        "token": raw,
        "expires_at": _iso(token.expires_at),
        "publisher_id": publisher.id,
        "namespace": publisher.namespace,
        "package": token.package_name,
        "trusted_publisher_id": trusted.id,
        # What an upload without an explicit visibility becomes, so the CLI
        # validates for the right target (personal packages need no license).
        "owner_type": publisher.owner_type,
        "default_visibility": "user" if publisher.owner_type == "user" else "org",
    }


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


async def _trusted_preauth(request: Request) -> str:
    """Pre-body check for the trusted publish route: a live publish token
    (unexpired, unused, active publisher). Returns the admission principal."""
    overrides = getattr(request.app, "dependency_overrides", {}) or {}
    provider = overrides.get(get_db, get_db)
    sessions = provider()
    db = await sessions.__anext__()
    try:
        token, _, _ = await svc.load_publish_token(db, _bearer(request))
        return f"trusted:{token.publisher_id}"
    finally:
        await sessions.aclose()


@dataclass
class TrustedPublisherAuth(AuthContext):
    """AuthContext for a trusted-publishing upload: attributed to the
    publisher's owning user, audited as ``trusted_publisher``."""

    trusted_publisher_id: str | None = None

    @property
    def actor_type(self) -> str:  # type: ignore[override]
        return "trusted_publisher"


async def _attribution_user(db: AsyncSession, publisher: SkillPublisher, trusted: SkillTrustedPublisher) -> tuple[User, Organization | None]:
    if publisher.owner_type == "user":
        user = await db.get(User, publisher.user_id)
        if user is None or not getattr(user, "is_active", True):
            raise svc._forbidden("publisher_owner_inactive", "The publisher's owner account is not active.")
        await _require_personal_tier(db, user)
        return user, None
    org = await db.get(Organization, publisher.org_id)
    if org is None:
        raise svc._forbidden("publisher_owner_inactive", "The publisher's organization no longer exists.")
    await mp._check_org_feature(db, org, "team_skill_library")
    # Attributed ONLY to the trust's creator, while still an active owner or
    # admin (membership fenced FOR SHARE). Never to an unrelated current owner:
    # a trust created by someone who has since left stops publishing.
    creator_id = trusted.created_by_user_id
    if creator_id:
        role = await fenced_org_role(db, org.id, creator_id)
        user = await db.get(User, creator_id)
        if role in ("owner", "admin") and user is not None and getattr(user, "is_active", True):
            return user, org
    raise svc._forbidden(
        "trust_creator_not_authorized",
        "The trusted publisher's creator is no longer an active admin of this organization; "
        "an admin must re-create the trust.",
    )


async def fenced_org_role(db: AsyncSession, org_id: str, user_id: str) -> str | None:
    """The member's role, read with the org_members row locked FOR SHARE so
    a concurrent removal or demotion cannot commit until this transaction
    ends (and one that committed first is seen)."""
    return (
        await db.execute(
            select(OrgMember.role)
            .where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@router.post("/marketplace/trusted-publishing/publish", status_code=201)
@mp._capped_upload(preauth=_trusted_preauth)
async def trusted_publish(
    request: Request,
    package: UploadFile = File(...),
    visibility: Literal["user", "org", "public"] | None = Form(None),
    collection: str | None = Form(None),
    source_locator: str | None = Form(None),
    source_commit: str | None = Form(None),
    source_subdir: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token, trusted, publisher = await svc.load_publish_token(db, _bearer(request))
    default_visibility = "user" if publisher.owner_type == "user" else "org"
    target = visibility or default_visibility
    if target not in (default_visibility, "public"):
        raise HTTPException(422, detail={"code": "invalid_visibility", "message": f"This publisher can publish '{default_visibility}' or 'public' listings."})
    user, org = await _attribution_user(db, publisher, trusted)
    provenance = svc.provenance_metadata(token, trusted, f"sessionfs-{sessionfs_version}")
    # Single use: burn (and commit) before any publish work.
    token_id, trusted_id, generation = token.id, trusted.id, token.auth_generation or 0
    expected_package = token.package_name
    await svc.burn_publish_token(db, token_id)
    # A suspension or revocation that committed between the token check and
    # the burn is refused right away. Plain reads, no locks: nothing is held
    # while the shared path reads, validates, DLP-scans and stores the upload.
    # The authoritative check is _reauthorize_trusted, which the shared path
    # runs under the publisher lock after validation and holds through commit.
    publisher = (
        await db.execute(
            select(SkillPublisher)
            .where(SkillPublisher.id == publisher.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()  # type: ignore[assignment]
    trusted = (
        await db.execute(
            select(SkillTrustedPublisher)
            .where(SkillTrustedPublisher.id == trusted_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()  # type: ignore[assignment]
    if not _trust_still_valid(publisher, trusted, generation):
        await db.rollback()
        raise svc._invalid_publish_token()
    auth = TrustedPublisherAuth(
        user=user,
        api_key_id=f"trusted:{token.id}",
        key_kind="trusted_publisher",
        scopes=[],
        org_id=org.id if org else None,
        trusted_publisher_id=trusted.id,
    )
    if source_locator is None and provenance.get("published_from_repo"):
        source_locator = f"https://{provenance['published_from_repo']}"
    result = await mp._publish_package(
        request=request,
        db=db,
        auth=auth,
        owner_type=publisher.owner_type,
        owner_id=publisher.org_id if publisher.owner_type == "org" else (publisher.user_id or ""),
        org=org,
        role="trusted_publisher",
        upload=package,
        namespace=publisher.namespace,
        collection=(collection or publisher.namespace),
        visibility=target,
        source_locator=source_locator,
        source_commit=source_commit,
        source_subdir=source_subdir,
        publisher=publisher,
        provenance=provenance,
        expected_name=expected_package,
        publish_token_id=token_id,
        reauthorize=functools.partial(
            _reauthorize_trusted, publisher_id=publisher.id, trusted_id=trusted_id,
            token_generation=generation, publisher_owner_type=publisher.owner_type,
            publisher_org_id=publisher.org_id, publisher_user_id=publisher.user_id,
            trusted_creator_id=trusted.created_by_user_id, attributed_user_id=user.id,
        ),
    )
    return result


def _trust_still_valid(
    publisher: SkillPublisher | None, trusted: SkillTrustedPublisher | None, generation: int
) -> bool:
    """The token's publisher is active, its trust is not revoked, and the
    publisher's authorization generation is still the one the token was
    minted at (trust removal and suspension move it on)."""
    return (
        publisher is not None
        and trusted is not None
        and publisher.status == "active"
        and trusted.revoked_at is None
        and trusted.publisher_id == publisher.id
        and (publisher.auth_generation or 0) == generation
    )


async def _reauthorize_trusted(
    db: AsyncSession,
    *,
    publisher_id: str,
    trusted_id: str,
    token_generation: int,
    publisher_owner_type: str,
    publisher_org_id: str | None,
    publisher_user_id: str | None,
    trusted_creator_id: str | None,
    attributed_user_id: str,
) -> None:
    """Attribution + paid-feature fence held by the FINAL publish transaction
    (re-run after any collision-recovery rollback).

    For a user publisher the full personal fence re-runs (membership, then the
    entitlement, then the user row, then the marketplace_access tier check).
    For an org publisher the trust creator must still be an owner/admin with
    the membership row FOR SHARE, and the org's active entitlement is locked
    FOR UPDATE and re-checked for team_skill_library (entitlement before the
    user row, matching apply_entitlement and the other fences).

    First of all, the token's own authority: the shared path calls this
    after taking the publisher row lock (and again after re-taking it on a
    collision-recovery rollback), where suspension, tombstoning and trust
    removal are serialized. The publisher and the trust are re-read there:
    a revocation or generation change that committed at any point after the
    token burn refuses the publish."""
    current_publisher = (
        await db.execute(
            select(SkillPublisher)
            .where(SkillPublisher.id == publisher_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current_trust = (
        await db.execute(
            select(SkillTrustedPublisher)
            .where(SkillTrustedPublisher.id == trusted_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not _trust_still_valid(current_publisher, current_trust, token_generation):
        raise svc._invalid_publish_token()
    user_id = publisher_user_id if publisher_owner_type == "user" else trusted_creator_id
    if not user_id or user_id != attributed_user_id:
        raise svc._forbidden("trust_creator_not_authorized", "The trusted publisher's attribution changed.")
    if publisher_owner_type == "user":
        await mp._fence_personal_tier(db, user_id=user_id)
        return
    role = await fenced_org_role(db, publisher_org_id or "", user_id)
    if role not in ("owner", "admin"):
        raise svc._forbidden(
            "trust_creator_not_authorized",
            "The trusted publisher's creator is no longer an active admin of this organization.",
        )
    await mp._fence_org_feature(db, org_id=publisher_org_id or "")
    active = (
        await db.execute(
            select(User.is_active)
            .where(User.id == user_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if active is False or active is None:
        raise svc._forbidden("publisher_owner_inactive", "The attributed account is not active.")


# ---------------------------------------------------------------------------
# Platform admin
# ---------------------------------------------------------------------------


@router.get("/admin/marketplace/publishers")
async def admin_list_publishers(
    status: Literal["active", "suspended", "tombstoned"] | None = Query(None),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_platform_admin(auth)
    statement = select(SkillPublisher).order_by(SkillPublisher.created_at.desc(), SkillPublisher.id).limit(500)
    if status:
        statement = statement.where(SkillPublisher.status == status)
    rows = (await db.execute(statement)).scalars().all()
    items = [await _publisher_response(db, row, manager=True) for row in rows]
    return {"items": items, "count": len(items)}


@router.post("/admin/marketplace/publishers", status_code=201)
async def admin_create_publisher(
    body: AdminPublisherCreate,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Assign a namespace with a lookalike / reserved-name override (appeal
    outcome). The global uniqueness of the skeleton still applies."""
    _require_platform_admin(auth)
    if body.owner_type == "org":
        if await db.get(Organization, body.owner_id) is None:
            raise HTTPException(404, "Organization not found")
    elif await db.get(User, body.owner_id) is None:
        raise HTTPException(404, "User not found")
    await svc.lock_confusable_promotion(db)
    await svc.lock_namespace(db, body.namespace)
    publisher, _ = await svc.claim_namespace(
        db,
        namespace=body.namespace,
        owner_type=body.owner_type,
        org_id=body.owner_id if body.owner_type == "org" else None,
        user_id=body.owner_id if body.owner_type == "user" else None,
        display_name=body.display_name,
        created_by_user_id=auth.user.id,
        admin_override=True,
    )
    _admin_audit(db, auth, "skill_publisher_override_claim", "skill_publisher", publisher.id,
                 {"namespace": publisher.namespace, "owner_type": body.owner_type,
                  "owner_id": body.owner_id, "reason": body.reason})
    await db.commit()
    return await _publisher_response(db, publisher, manager=True)


@router.post("/admin/marketplace/publishers/{publisher_id}/suspend")
async def admin_suspend_publisher(
    publisher_id: str,
    body: ReasonBody,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Suspend: no new versions, no trusted-publishing tokens, and the
    publisher's approved public listings go back into review."""
    _require_platform_admin(auth)
    publisher, listing_ids = await svc.suspend_publisher(db, publisher_id, body.reason)
    if listing_ids:
        version_ids = (
            await db.execute(
                select(SkillMarketplaceListing.version_id).where(SkillMarketplaceListing.id.in_(listing_ids))
            )
        ).scalars().all()
        await mp.bump_grant_generations_for_versions(db, list(version_ids))
    _admin_audit(db, auth, "skill_publisher_suspend", "skill_publisher", publisher.id,
                 {"reason": body.reason, "listings_to_review": sorted(listing_ids)})
    await db.commit()
    return await _publisher_response(db, publisher, manager=True)


@router.post("/admin/marketplace/publishers/{publisher_id}/restore")
async def admin_restore_publisher(
    publisher_id: str,
    body: ReasonBody,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_platform_admin(auth)
    locked = await svc.lock_publisher(db, publisher_id)
    if locked is None:
        raise HTTPException(404, "Publisher not found")
    publisher = locked
    if publisher.status != "suspended":
        raise HTTPException(409, detail={"code": "publisher_not_suspended", "message": "Publisher is not suspended."})
    listing_ids = [str(item) for item in _json_list(publisher.suspended_listing_ids)]
    if listing_ids:
        await db.execute(
            update(SkillMarketplaceListing)
            .where(
                SkillMarketplaceListing.id.in_(listing_ids),
                SkillMarketplaceListing.moderation_state == "pending_review",
                SkillMarketplaceListing.moderation_reason == "publisher_suspended",
            )
            .values(moderation_state="approved", moderation_reason=None)
        )
        version_ids = (
            await db.execute(
                select(SkillMarketplaceListing.version_id).where(SkillMarketplaceListing.id.in_(listing_ids))
            )
        ).scalars().all()
        await mp.bump_grant_generations_for_versions(db, list(version_ids))
    publisher.status = "active"
    publisher.suspended_reason = None
    publisher.suspended_listing_ids = "[]"
    _admin_audit(db, auth, "skill_publisher_restore", "skill_publisher", publisher.id,
                 {"reason": body.reason, "listings_restored": listing_ids})
    await db.commit()
    return await _publisher_response(db, publisher, manager=True)


@router.post("/admin/marketplace/publishers/{publisher_id}/public-publishing")
async def admin_set_public_publishing(
    publisher_id: str,
    body: CohortBody,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """D1 invited cohort: allow (or stop) a verified publisher's public
    publishing. Their first public version and widening updates still need
    review."""
    _require_platform_admin(auth)
    locked = await svc.lock_publisher(db, publisher_id)
    if locked is None:
        raise HTTPException(404, "Publisher not found")
    publisher = locked
    if body.enabled and not await svc.is_verified(db, publisher):
        raise HTTPException(409, detail={"code": "publisher_not_verified", "message": "Only an active, verified publisher can join the public cohort."})
    publisher.public_publishing = body.enabled
    _admin_audit(db, auth, "skill_publisher_public_publishing", "skill_publisher", publisher.id, {"enabled": body.enabled})
    await db.commit()
    return await _publisher_response(db, publisher, manager=True)


@router.get("/admin/marketplace/reserved-names")
async def admin_list_reserved(
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_platform_admin(auth)
    from sessionfs.skills.confusables import BRAND_RESERVED

    rows = (await db.execute(select(SkillReservedName).order_by(SkillReservedName.normalized))).scalars().all()
    return {
        "builtin": [{"name": name, "required_subject": domain} for name, domain in sorted(BRAND_RESERVED.items())],
        "items": [
            {"name": row.name, "normalized": row.normalized, "reason": row.reason, "required_subject": row.required_subject}
            for row in rows
        ],
    }


@router.post("/admin/marketplace/reserved-names", status_code=201)
async def admin_add_reserved(
    body: ReservedNameCreate,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_platform_admin(auth)
    name = svc.validate_namespace_shape(body.name)
    await svc.lock_confusable_promotion(db)  # protected names change under the global lock
    required = None
    if body.required_subject:
        try:
            required = domain_verification.normalize_domain(body.required_subject)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "invalid_domain", "message": str(exc)}) from None
    normalized = skeleton(name)
    if await db.get(SkillReservedName, normalized) is not None:
        raise HTTPException(409, detail={"code": "already_reserved", "message": "Already reserved."})
    db.add(SkillReservedName(normalized=normalized, name=name, reason=body.reason,
                             required_subject=required, created_by_user_id=auth.user.id))
    _admin_audit(db, auth, "skill_reserved_name_add", "reserved_name", normalized,
                 {"name": name, "reason": body.reason, "required_subject": required})
    await db.commit()
    return {"name": name, "normalized": normalized, "reason": body.reason, "required_subject": required}


@router.post("/admin/marketplace/publishers/recheck")
async def admin_recheck_verifications(
    body: RecheckBody | None = None,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verification sweeper (Cloud Scheduler, daily): live DNS re-check (a
    record missing for 7 consecutive days lapses the badge) and GitHub
    installation liveness (gone or suspended revokes). Also deletes expired
    publish tokens."""
    _require_platform_admin(auth)
    body = body or RecheckBody(limit=200)
    summary = await svc.recheck_verifications(db, limit=body.limit)
    summary["expired_tokens_deleted"] = await svc.sweep_expired_tokens(db)
    _admin_audit(db, auth, "skill_publisher_recheck", "skill_publisher", "bulk", summary)
    await db.commit()
    return summary


@router.get("/admin/marketplace/review-queue")
async def admin_review_queue(
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_platform_admin(auth)
    rows = (
        await db.execute(
            select(SkillMarketplaceListing, SkillPackage, SkillPackageVersion)
            .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
            .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
            .where(SkillMarketplaceListing.moderation_state == "pending_review")
            .order_by(SkillMarketplaceListing.published_at, SkillMarketplaceListing.id)
            .limit(200)
        )
    ).all()
    items = []
    for listing, package, version in rows:
        item = mp._package_response(listing, package, version)
        item["moderation_reason"] = listing.moderation_reason
        items.append(item)
    await mp.attach_publishers(db, items)
    return {"items": items, "count": len(items)}


async def _review(db: AsyncSession, auth: AuthContext, listing_id: str, state: str, reason: str | None) -> dict:
    _require_platform_admin(auth)
    listing = await db.get(SkillMarketplaceListing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found")
    if listing.moderation_state != "pending_review":
        raise HTTPException(409, detail={"code": "not_pending_review", "message": f"Listing is {listing.moderation_state}."})
    if state == "approved":
        package = await db.get(SkillPackage, listing.package_id)
        publisher = (
            await svc.lock_publisher(db, package.publisher_id) if package and package.publisher_id else None
        )
        if publisher is not None and publisher.status != "active":
            raise HTTPException(409, detail={"code": "publisher_not_active", "message": "Restore the publisher before approving its listings."})
    result = await db.execute(
        update(SkillMarketplaceListing)
        .where(SkillMarketplaceListing.id == listing_id, SkillMarketplaceListing.moderation_state == "pending_review")
        .values(moderation_state=state, moderation_reason=reason, reviewed_by_user_id=auth.user.id,
                reviewed_at=svc.now_utc())
    )
    if (result.rowcount or 0) != 1:
        await db.rollback()
        raise HTTPException(409, detail={"code": "not_pending_review", "message": "Listing changed; reload."})
    await mp.bump_grant_generations_for_versions(db, [listing.version_id])
    _admin_audit(db, auth, f"skill_listing_review_{'approve' if state == 'approved' else 'reject'}",
                 "skill_listing", listing_id, {"reason": reason, "version_id": listing.version_id})
    await db.commit()
    return {"listing_id": listing_id, "moderation_state": state, "moderation_reason": reason}


@router.post("/admin/marketplace/listings/{listing_id}/approve")
async def admin_approve_listing(
    listing_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _review(db, auth, listing_id, "approved", None)


@router.post("/admin/marketplace/listings/{listing_id}/reject")
async def admin_reject_listing(
    listing_id: str,
    body: ReasonBody,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _review(db, auth, listing_id, "rejected", body.reason)
