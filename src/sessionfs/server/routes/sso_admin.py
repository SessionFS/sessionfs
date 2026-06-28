"""SSO admin surface: OIDC provider config CRUD + domain verification.

SSO-P3 (tk_0de967a55afe4896): Org-admin surface to configure an OIDC
provider and verify email domains — the data plane that P2's login flow
reads. Owner-or-admin gated, cross-org isolation returns 404.

Secret handling: client_secret_ref is a reference (GCP Secret Manager /
K8s secret / env var), NEVER the plaintext secret. The server never
accepts, stores, or returns a plaintext client secret — only the ref.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgAuditEvent,
    OrgDomainVerification,
    OrgIdentityProvider,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.oidc_fetch import SsrfError, oidc_fetch_json
from sessionfs.server.services.sso_domains import FREE_EMAIL_DENYLIST

logger = logging.getLogger("sessionfs.api")
router = APIRouter(prefix="/api/v1/orgs", tags=["sso-admin"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed scheme prefixes for client_secret_ref.  Anything that doesn't
# match one of these is rejected as plaintext-looking (§5.2 / §7 risk).
_ALLOWED_SECRET_REF_PREFIXES: tuple[str, ...] = ("projects/", "k8s:", "env:")

# TXT record prefix for domain verification.
_DOMAIN_VERIFICATION_PREFIX = "sessionfs-verification="


# ---------------------------------------------------------------------------
# DNS resolver (mockable — tests patch this)
# ---------------------------------------------------------------------------


async def _resolve_txt(domain: str) -> list[str]:
    """Resolve DNS TXT records for *domain* (async, via dnspython).

    Returns the list of decoded TXT strings, or [] on any resolution
    failure (NXDOMAIN, no TXT, timeout, or dnspython missing) — verification
    then fails closed. Tests inject a mock by patching this module-level
    callable.
    """
    try:
        import dns.asyncresolver
    except ImportError:  # pragma: no cover - dnspython is a declared dep
        logger.warning(
            "dnspython not installed — DNS TXT resolution unavailable for %s",
            domain,
        )
        return []

    try:
        answers = await dns.asyncresolver.resolve(domain, "TXT")
    except Exception as exc:
        # NXDOMAIN / NoAnswer / Timeout / etc. → fail closed (not verified).
        logger.info("DNS TXT resolution failed for %s: %s", domain, exc)
        return []

    records: list[str] = []
    for rdata in answers:
        # A TXT rdata is a sequence of byte chunks; concatenate + decode.
        try:
            records.append(
                "".join(chunk.decode("utf-8", "ignore") for chunk in rdata.strings)
            )
        except Exception:  # pragma: no cover - defensive
            continue
    return records


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateProviderRequest(BaseModel):
    """POST /{org_id}/sso/provider — create the org's OIDC provider."""

    display_name: str
    issuer: str  # https URL
    client_id: str
    client_secret_ref: str  # REF only, never plaintext
    allowed_scopes: list[str] = ["openid", "email", "profile"]

    @field_validator("display_name")
    @classmethod
    def _trim_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("display_name must be 1–100 characters")
        return v

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("issuer must be an HTTPS URL")
        if len(v) > 500:
            raise ValueError("issuer must be ≤ 500 characters")
        return v.rstrip("/")

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("client_id must be 1–255 characters")
        return v

    @field_validator("client_secret_ref")
    @classmethod
    def _validate_secret_ref(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("client_secret_ref must be 1–255 characters")
        if not any(v.startswith(p) for p in _ALLOWED_SECRET_REF_PREFIXES):
            raise ValueError(
                f"client_secret_ref must start with one of: "
                f"{', '.join(_ALLOWED_SECRET_REF_PREFIXES)}. "
                "Never send a plaintext secret."
            )
        return v


class UpdateProviderRequest(BaseModel):
    """PATCH /{org_id}/sso/provider — update fields (all optional)."""

    display_name: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    client_secret_ref: str | None = None
    allowed_scopes: list[str] | None = None
    enabled: bool | None = None

    @field_validator("display_name")
    @classmethod
    def _trim_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("display_name must be 1–100 characters")
        return v

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("issuer must be an HTTPS URL")
        if len(v) > 500:
            raise ValueError("issuer must be ≤ 500 characters")
        return v.rstrip("/")

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("client_id must be 1–255 characters")
        return v

    @field_validator("client_secret_ref")
    @classmethod
    def _validate_secret_ref(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("client_secret_ref must be 1–255 characters")
        if not any(v.startswith(p) for p in _ALLOWED_SECRET_REF_PREFIXES):
            raise ValueError(
                f"client_secret_ref must start with one of: "
                f"{', '.join(_ALLOWED_SECRET_REF_PREFIXES)}. "
                "Never send a plaintext secret."
            )
        return v


class ProviderResponse(BaseModel):
    """Serialized OrgIdentityProvider — NEVER contains a plaintext secret."""

    id: str
    org_id: str
    protocol: str
    display_name: str
    issuer: str
    client_id: str
    client_secret_ref: str  # ref only — NEVER a plaintext secret
    allowed_scopes: list[str]
    enabled: bool
    enforced: bool
    created_by_user_id: str | None
    created_at: datetime | None
    updated_at: datetime | None


class CreateDomainRequest(BaseModel):
    """POST /{org_id}/sso/domains — request domain verification."""

    domain: str

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or len(v) > 255:
            raise ValueError("domain must be 1–255 characters")
        if "@" in v:
            raise ValueError("domain must not contain '@' — provide just the domain part")
        return v


class DomainResponse(BaseModel):
    """Serialized OrgDomainVerification row."""

    id: str
    org_id: str
    domain: str
    method: str
    verification_token: str
    status: str
    verified_at: datetime | None
    txt_record: str  # the DNS TXT record the admin must publish
    created_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _org_or_404(db: AsyncSession, org_id: str) -> Organization:
    """Fetch an org row or raise 404."""
    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(404, "Org not found")
    return org


async def _user_role_in_org(
    db: AsyncSession, user_id: str, org_id: str
) -> str | None:
    """Return the user's role in this org, or None."""
    result = await db.execute(
        select(OrgMember).where(
            OrgMember.org_id == org_id,
            OrgMember.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    return row.role if row else None


async def _require_owner_or_admin(
    db: AsyncSession, user: User, org_id: str
) -> str:
    """Require owner or admin role in *org_id*.

    Cross-org isolation: non-members get 404 (not 403) — the org does not
    exist from their perspective.  Members without admin/owner get 403.
    Returns the role on success.
    """
    role = await _user_role_in_org(db, user.id, org_id)
    if role is None:
        raise HTTPException(404, "Org not found")
    if role not in ("admin", "owner"):
        raise HTTPException(403, "Admin or owner role required")
    return role


async def _emit_audit(
    db: AsyncSession,
    *,
    org_id: str,
    org_name_snapshot: str = "",
    event_type: str,
    actor_user_id: str | None = None,
    actor_email_snapshot: str | None = None,
    actor_role_at_time: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """Emit a single OrgAuditEvent row (append-only, inside txn)."""
    audit = OrgAuditEvent(
        id=f"oae_{secrets.token_hex(12)}",
        org_id=org_id,
        org_name_snapshot=org_name_snapshot,
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email_snapshot=actor_email_snapshot,
        actor_role_at_time=actor_role_at_time,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
    )
    db.add(audit)


async def _validate_oidc_discovery(issuer: str) -> dict:
    """Fetch + validate the OIDC discovery doc through the SSRF guard.

    Returns the discovery dict on success.  Raises HTTPException(400) on
    SSRF-rejected or unreachable issuers, and HTTPException(502) on
    incomplete discovery docs.
    """
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    try:
        discovery = await oidc_fetch_json(discovery_url, timeout=10.0)
    except SsrfError as exc:
        logger.warning("sso_admin_ssrf_blocked issuer=%s err=%s", issuer, exc)
        raise HTTPException(400, f"SSO provider URL rejected: {exc}") from exc
    except Exception as exc:
        logger.warning("sso_admin_discovery_fetch_failed issuer=%s err=%s", issuer, exc)
        raise HTTPException(502, "Could not reach the SSO provider's discovery document") from exc

    required = ["authorization_endpoint", "token_endpoint", "jwks_uri"]
    missing = [k for k in required if not discovery.get(k)]
    if missing:
        raise HTTPException(
            502,
            f"SSO provider discovery document is missing required fields: "
            f"{', '.join(missing)}",
        )
    return discovery


def _provider_to_response(idp: OrgIdentityProvider) -> ProviderResponse:
    """Serialize an OrgIdentityProvider row → ProviderResponse."""
    try:
        scopes: list[str] = json.loads(idp.allowed_scopes)
    except (json.JSONDecodeError, TypeError):
        scopes = []
    return ProviderResponse(
        id=idp.id,
        org_id=idp.org_id,
        protocol=idp.protocol,
        display_name=idp.display_name,
        issuer=idp.issuer,
        client_id=idp.client_id,
        client_secret_ref=idp.client_secret_ref,
        allowed_scopes=scopes,
        enabled=idp.enabled,
        enforced=idp.enforced,
        created_by_user_id=idp.created_by_user_id,
        created_at=idp.created_at,
        updated_at=idp.updated_at,
    )


# ---------------------------------------------------------------------------
# A. Provider config CRUD
# ---------------------------------------------------------------------------


@router.post("/{org_id}/sso/provider", response_model=ProviderResponse)
async def create_provider(
    org_id: str,
    body: CreateProviderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderResponse:
    """Create the org's OIDC provider config. Owner or admin only.

    Validates the OIDC discovery doc on save (SSRF-guarded).  The provider
    is created with ``enabled=false`` — the admin must explicitly enable it.
    ``client_secret_ref`` is stored as-is; the raw secret is never accepted.
    """
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    # One OIDC provider per org — the API is singular (no provider id in
    # GET/PATCH/DELETE paths).  Creating a second is rejected.
    existing = (
        await db.execute(
            select(OrgIdentityProvider).where(
                OrgIdentityProvider.org_id == org_id,
                OrgIdentityProvider.protocol == "oidc",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            409,
            "An OIDC provider is already configured for this org. "
            "Use PATCH to update it, or DELETE it first.",
        )

    # Validate the discovery doc (SSRF guard runs inside oidc_fetch_json)
    discovery = await _validate_oidc_discovery(body.issuer)
    discovery_json = json.dumps(discovery)

    now = _now()
    idp_id = f"oidp_{secrets.token_hex(12)}"
    scopes_json = json.dumps(body.allowed_scopes)

    idp = OrgIdentityProvider(
        id=idp_id,
        org_id=org_id,
        protocol="oidc",
        display_name=body.display_name,
        issuer=body.issuer,
        client_id=body.client_id,
        client_secret_ref=body.client_secret_ref,
        allowed_scopes=scopes_json,
        discovery_cache=discovery_json,
        discovery_fetched_at=now,
        enabled=False,
        enforced=False,
        created_by_user_id=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(idp)

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type="sso_idp_created",
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="idp",
        target_id=idp_id,
        after=json.dumps({"issuer": body.issuer, "display_name": body.display_name}),
    )

    await db.commit()
    await db.refresh(idp)
    return _provider_to_response(idp)


@router.get("/{org_id}/sso/provider", response_model=ProviderResponse)
async def get_provider(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderResponse:
    """Get the org's OIDC provider config. Owner or admin only.

    ``client_secret_ref`` is returned — it is a reference, NEVER a plaintext
    secret.  No plaintext secret field exists on this model.
    """
    await _org_or_404(db, org_id)
    await _require_owner_or_admin(db, user, org_id)

    result = await db.execute(
        select(OrgIdentityProvider).where(
            OrgIdentityProvider.org_id == org_id,
            OrgIdentityProvider.protocol == "oidc",
        )
    )
    idp = result.scalars().first()  # at most one per org (enforced by partial-unique)
    if idp is None:
        raise HTTPException(404, "No OIDC provider configured for this org")
    return _provider_to_response(idp)


@router.patch("/{org_id}/sso/provider", response_model=ProviderResponse)
async def update_provider(
    org_id: str,
    body: UpdateProviderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderResponse:
    """Update the org's OIDC provider. Owner or admin only.

    Partial update — only the fields present in the body are changed.
    Re-validates the OIDC discovery doc if the issuer changes.  Enabling
    a second IdP while one is already enabled returns 409.
    """
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    result = await db.execute(
        select(OrgIdentityProvider).where(
            OrgIdentityProvider.org_id == org_id,
            OrgIdentityProvider.protocol == "oidc",
        )
    )
    idp = result.scalars().first()
    if idp is None:
        raise HTTPException(404, "No OIDC provider configured for this org")

    before_snapshot = json.dumps({
        "issuer": idp.issuer,
        "display_name": idp.display_name,
        "enabled": idp.enabled,
    })

    now = _now()
    issuer_changed = False

    if body.display_name is not None:
        idp.display_name = body.display_name
    if body.issuer is not None:
        if body.issuer != idp.issuer:
            issuer_changed = True
            idp.issuer = body.issuer
    if body.client_id is not None:
        idp.client_id = body.client_id
    if body.client_secret_ref is not None:
        idp.client_secret_ref = body.client_secret_ref
        await _emit_audit(
            db,
            org_id=org_id,
            org_name_snapshot=org.name,
            event_type="sso_client_secret_rotated",
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            actor_role_at_time=role,
            target_type="idp",
            target_id=idp.id,
        )
    if body.allowed_scopes is not None:
        idp.allowed_scopes = json.dumps(body.allowed_scopes)

    # Enable/disable toggle
    enabling = False
    if body.enabled is not None and body.enabled != idp.enabled:
        enabling = body.enabled
        idp.enabled = body.enabled
        idp.updated_at = now
        if body.enabled:
            # If disabling enforcement alongside enabling, that's fine —
            # but enabling when another is already enabled → 409 partial-unique.
            pass
        else:
            # Disabling auto-clears enforced (§4.4 rule 4)
            idp.enforced = False

    # Re-validate discovery if issuer changed
    if issuer_changed:
        discovery = await _validate_oidc_discovery(idp.issuer)
        idp.discovery_cache = json.dumps(discovery)
        idp.discovery_fetched_at = now

    idp.updated_at = now

    # If enabling, the partial-unique index may fire.  We flush now so the
    # IntegrityError surfaces before we emit the audit event.
    try:
        await db.flush()
    except Exception:
        # On IntegrityError from the partial-unique, translate to 409.
        # SQLite raises IntegrityError; PG raises IntegrityError.
        from sqlalchemy.exc import IntegrityError
        import sys as _sys

        # Re-raise anything that isn't an IntegrityError
        _exc_info = _sys.exc_info()
        if _exc_info[0] is not IntegrityError:
            raise
        raise HTTPException(
            409,
            "Another OIDC provider is already enabled for this org. "
            "Disable it before enabling this one.",
        )

    event_type = "sso_idp_updated"
    if body.enabled is True and enabling:
        event_type = "sso_idp_enabled"
    elif body.enabled is False and not enabling:
        event_type = "sso_idp_disabled"

    after_snapshot = json.dumps({
        "issuer": idp.issuer,
        "display_name": idp.display_name,
        "enabled": idp.enabled,
    })

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type=event_type,
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="idp",
        target_id=idp.id,
        before=before_snapshot,
        after=after_snapshot,
    )

    await db.commit()
    await db.refresh(idp)
    return _provider_to_response(idp)


@router.delete("/{org_id}/sso/provider")
async def delete_provider(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disable (remove) the org's OIDC provider. Owner or admin only.

    Disabling auto-clears ``enforced`` (§4.4 rule 4).  The row itself is
    deleted.  ExternalIdentity rows cascade (ON DELETE CASCADE).
    """
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    result = await db.execute(
        select(OrgIdentityProvider).where(
            OrgIdentityProvider.org_id == org_id,
            OrgIdentityProvider.protocol == "oidc",
        )
    )
    idp = result.scalars().first()
    if idp is None:
        raise HTTPException(404, "No OIDC provider configured for this org")

    before_snapshot = json.dumps({
        "issuer": idp.issuer,
        "display_name": idp.display_name,
        "enabled": idp.enabled,
        "enforced": idp.enforced,
    })

    idp_id = idp.id
    await db.execute(
        delete(OrgIdentityProvider).where(OrgIdentityProvider.id == idp_id)
    )

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type="sso_idp_disabled",
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="idp",
        target_id=idp_id,
        before=before_snapshot,
    )

    await db.commit()
    return {"deleted": True, "id": idp_id}


# ---------------------------------------------------------------------------
# B. Domain verification
# ---------------------------------------------------------------------------


@router.post("/{org_id}/sso/domains", response_model=DomainResponse)
async def request_domain_verification(
    org_id: str,
    body: CreateDomainRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DomainResponse:
    """Request domain verification. Owner or admin only.

    Normalizes to lowercase, enforces the consumer-domain denylist
    (gmail.com etc. → 400), generates a DNS-TXT token, and persists a
    ``status='pending'`` row.  Returns the TXT record the admin must
    publish at their domain.
    """
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    domain = body.domain

    # Consumer-domain denylist (hard gate)
    if domain in FREE_EMAIL_DENYLIST:
        raise HTTPException(
            400,
            f"'{domain}' is a consumer email provider and cannot be verified "
            "as an organizational domain.",
        )

    # Check if this domain is already verified by ANY org
    existing = (
        await db.execute(
            select(OrgDomainVerification).where(
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "verified",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # If it's THIS org, it's a duplicate request — return conflict
        if existing.org_id == org_id:
            raise HTTPException(409, "This domain is already verified by your org")
        # Another org owns it
        raise HTTPException(
            409,
            f"Domain '{domain}' is already verified by another organization.",
        )

    # Check for an existing pending request from THIS org (idempotent)
    existing_pending = (
        await db.execute(
            select(OrgDomainVerification).where(
                OrgDomainVerification.org_id == org_id,
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        # Re-return the existing pending row
        return DomainResponse(
            id=existing_pending.id,
            org_id=existing_pending.org_id,
            domain=existing_pending.domain,
            method=existing_pending.method,
            verification_token=existing_pending.verification_token,
            status=existing_pending.status,
            verified_at=existing_pending.verified_at,
            txt_record=f"{_DOMAIN_VERIFICATION_PREFIX}{existing_pending.verification_token}",
            created_at=existing_pending.created_at,
        )

    # Generate token + create row
    token = secrets.token_hex(20)
    now = _now()
    dv_id = f"odv_{secrets.token_hex(12)}"

    dv = OrgDomainVerification(
        id=dv_id,
        org_id=org_id,
        domain=domain,
        method="dns_txt",
        verification_token=token,
        status="pending",
        created_at=now,
    )
    db.add(dv)

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type="sso_domain_verification_started",
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="domain",
        target_id=dv_id,
        after=json.dumps({"domain": domain}),
    )

    await db.commit()
    await db.refresh(dv)

    return DomainResponse(
        id=dv.id,
        org_id=dv.org_id,
        domain=dv.domain,
        method=dv.method,
        verification_token=dv.verification_token,
        status=dv.status,
        verified_at=dv.verified_at,
        txt_record=f"{_DOMAIN_VERIFICATION_PREFIX}{dv.verification_token}",
        created_at=dv.created_at,
    )


@router.post("/{org_id}/sso/domains/{domain_id}/verify", response_model=DomainResponse)
async def verify_domain(
    org_id: str,
    domain_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DomainResponse:
    """Verify a pending domain. Owner or admin only.

    Resolves DNS TXT records for the domain (mockable) and checks for the
    ``sessionfs-verification=<token>`` value.  On match flips status to
    ``verified``.  The ``uq_org_domain_global_verified`` partial-unique
    index is the backstop against a race with another org.
    """
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    dv = (
        await db.execute(
            select(OrgDomainVerification).where(
                OrgDomainVerification.id == domain_id,
                OrgDomainVerification.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if dv is None:
        raise HTTPException(404, "Domain verification request not found")
    if dv.status == "verified":
        raise HTTPException(409, "Domain is already verified")
    if dv.status == "failed":
        # Allow re-verification of a failed domain
        pass

    # Resolve TXT records
    txt_records = await _resolve_txt(dv.domain)
    expected = f"{_DOMAIN_VERIFICATION_PREFIX}{dv.verification_token}"
    found = any(expected in r for r in txt_records)

    now = _now()
    dv.last_checked_at = now

    if found:
        dv.status = "verified"
        dv.verified_at = now
        dv.verified_by_user_id = user.id
    else:
        dv.status = "failed"
        dv.verified_at = None

    # The partial-unique index on (domain WHERE status='verified') is the
    # race backstop.  If another org verified the same domain between our
    # SELECT and UPDATE, the commit raises IntegrityError.
    try:
        await db.flush()
    except Exception:
        from sqlalchemy.exc import IntegrityError
        import sys as _sys

        _exc_info = _sys.exc_info()
        if _exc_info[0] is not IntegrityError:
            raise
        raise HTTPException(
            409,
            f"Domain '{dv.domain}' was verified by another organization "
            "while your verification was in progress.",
        )

    event_type = "sso_domain_verified" if found else "sso_domain_verification_failed"
    after_payload = json.dumps({
        "domain": dv.domain,
        "status": dv.status,
        "found": found,
    })

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type=event_type,
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="domain",
        target_id=dv.id,
        after=after_payload,
    )

    await db.commit()
    await db.refresh(dv)

    return DomainResponse(
        id=dv.id,
        org_id=dv.org_id,
        domain=dv.domain,
        method=dv.method,
        verification_token=dv.verification_token,
        status=dv.status,
        verified_at=dv.verified_at,
        txt_record=f"{_DOMAIN_VERIFICATION_PREFIX}{dv.verification_token}",
        created_at=dv.created_at,
    )


@router.get("/{org_id}/sso/domains", response_model=list[DomainResponse])
async def list_domains(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DomainResponse]:
    """List the org's domain verification requests. Owner or admin only."""
    await _org_or_404(db, org_id)
    await _require_owner_or_admin(db, user, org_id)

    result = await db.execute(
        select(OrgDomainVerification).where(
            OrgDomainVerification.org_id == org_id,
        ).order_by(OrgDomainVerification.created_at.desc())
    )
    rows = result.scalars().all()

    return [
        DomainResponse(
            id=dv.id,
            org_id=dv.org_id,
            domain=dv.domain,
            method=dv.method,
            verification_token=dv.verification_token,
            status=dv.status,
            verified_at=dv.verified_at,
            txt_record=f"{_DOMAIN_VERIFICATION_PREFIX}{dv.verification_token}",
            created_at=dv.created_at,
        )
        for dv in rows
    ]


@router.delete("/{org_id}/sso/domains/{domain_id}")
async def delete_domain(
    org_id: str,
    domain_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a domain verification request. Owner or admin only."""
    org = await _org_or_404(db, org_id)
    role = await _require_owner_or_admin(db, user, org_id)

    dv = (
        await db.execute(
            select(OrgDomainVerification).where(
                OrgDomainVerification.id == domain_id,
                OrgDomainVerification.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if dv is None:
        raise HTTPException(404, "Domain verification request not found")

    domain_name = dv.domain
    dv_id = dv.id

    await db.execute(
        delete(OrgDomainVerification).where(OrgDomainVerification.id == dv_id)
    )

    await _emit_audit(
        db,
        org_id=org_id,
        org_name_snapshot=org.name,
        event_type="sso_domain_revoked",
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="domain",
        target_id=dv_id,
        before=json.dumps({"domain": domain_name}),
    )

    await db.commit()
    return {"deleted": True, "id": dv_id, "domain": domain_name}
