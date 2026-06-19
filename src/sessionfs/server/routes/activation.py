"""License activation — self-service HelmLicense→Organization binding.

Three-phase activation with durable token storage (design §2.2):
  GET  /api/v1/org/activate/info?key=    — unauthenticated, non-oracular preview
  POST /api/v1/org/activate              — Phase A: validate + create token + email
  POST /api/v1/org/activate/verify       — Phase B: consume token + bind + commit

Security invariants (Sentinel-reviewed):
  - Required email verification (no soft warning).
  - Non-oracular info + verification responses.
  - Full license key never logged/echoed. Raw token never stored (sha256 hash only).
  - Activation grants OrgMember.role='owner' ONLY — never User.tier='admin'.
  - Rate-limit all three endpoints (app-layer; Cloud Armor for prod — Forge tk_c279911c20264333).
  - Bind-first-rollback = zero orphans on race (org row created inside the Phase B txn;
    rowcount-1 guard on license bind + activation_attempt consume).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ActivationAttempt,
    HelmLicense,
    OrgAuditEvent,
    OrgMember,
    Organization,
    PendingLicenseClaim,
    User,
)
from sessionfs.server.services.entitlements import apply_entitlement

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/org/activate", tags=["activation"])

# ---------------------------------------------------------------------------
# Rate limiters (app-layer only — Cloud Armor is the durable multi-replica
# layer, per Forge tk_c279911c20264333)
# ---------------------------------------------------------------------------
# Rate limiters (app-layer only — Cloud Armor is the durable multi-replica
# layer, per Forge tk_c279911c20264333)
# ---------------------------------------------------------------------------
# Lax enough to not break normal usage; Cloud Armor provides the real
# rate-limiting perimeter.  These are defense-in-depth against application-level
# abuse without a configured cloud perimeter.
_info_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=3600)
_activate_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=3600)
_verify_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=3600)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _slugify(name: str) -> str:
    """Derive a URL-safe slug from an org name."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-")
    if not s:
        s = "org"
    return s[:100]


class ActivateRequest(BaseModel):
    key: str
    org_name: str | None = None  # optional override of license.org_name
    slug: str | None = None  # optional override of derived slug

    @field_validator("key")
    @classmethod
    def validate_key_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("License key must not be blank")
        return v


class VerifyRequest(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def validate_token_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Token must not be blank")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(raw: str) -> str:
    """Hash a raw token for storage. Raw token is NEVER persisted or logged."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def _active_unbound_license(key: str, db) -> HelmLicense | None:
    """Return the license if it is active, unexpired, and unbound.

    SINGLE helper — every endpoint that needs to evaluate a license uses
    this to avoid inconsistent checks.  Returns None for invalid / expired /
    revoked / already-bound / missing.
    """
    from sqlalchemy import or_

    now = datetime.now(timezone.utc)
    stmt = select(HelmLicense).where(
        HelmLicense.id == key,
        HelmLicense.status == "active",
        HelmLicense.org_id.is_(None),
        or_(
            HelmLicense.expires_at.is_(None),
            HelmLicense.expires_at > now,
        ),
    )
    result = await db.execute(stmt)
    lic = result.scalar_one_or_none()
    # Check in Python in case the DB returned a row with tz-naive datetime
    if lic is not None and lic.expires_at is not None:
        expire = lic.expires_at
        if expire.tzinfo is None:
            expire = expire.replace(tzinfo=timezone.utc)
        if expire <= now:
            return None
    return lic


def _key_prefix(key: str) -> str:
    """Return a safe prefix for logging. Full key is NEVER logged."""
    if len(key) <= 8:
        return key[:2] + "…"
    return key[:7] + "…"


async def _phase_b_commit(
    *,
    lic: HelmLicense,
    org_name: str,
    slug: str,
    user: User,
    db: AsyncSession,
    verification_method: str,
    attempt_id: int | None = None,
) -> dict:
    """Phase B single-atomic-transaction: create org, bind license,
    apply entitlement, add owner, consume pending claim, emit audit.

    Runs entirely inside the caller's transaction — caller commits.
    On any failure the caller MUST roll back (zero orphans).

    verification_method is 'matched_contact_email' (exact-match shortcut)
    or 'email_token' (standard Phase B via /verify).
    """
    now = datetime.now(timezone.utc)

    # 0. Ensure the slug is unique. A derived slug (from org_name) can collide
    #    with an existing org — e.g. two licenses whose names slugify the same.
    #    Without this, db.flush() below raises IntegrityError → rollback → 500,
    #    and because the derived slug is deterministic the customer is
    #    permanently wedged (every retry hits the same collision). Auto-suffix
    #    on collision. The UNIQUE constraint remains the backstop for the rare
    #    concurrent race (which self-heals on retry once the other org exists).
    base_slug = slug
    for _ in range(6):
        slug_taken = await db.execute(
            select(Organization.id).where(Organization.slug == slug)
        )
        if slug_taken.scalar_one_or_none() is None:
            break
        slug = f"{base_slug}-{secrets.token_hex(3)}"

    # 1. Create the Organization row FIRST (satisfies HelmLicense.org_id FK)
    org_id = f"org_{secrets.token_hex(8)}"
    org = Organization(
        id=org_id,
        name=org_name,
        slug=slug,
        tier=lic.tier,
        seats_limit=lic.seats_limit,
    )
    db.add(org)
    await db.flush()

    # 2. Rowcount-1 bind the license FK
    bind_result = await db.execute(
        update(HelmLicense)
        .where(
            HelmLicense.id == lic.id,
            HelmLicense.org_id.is_(None),
            HelmLicense.status == "active",
        )
        .values(org_id=org_id)
    )
    if bind_result.rowcount != 1:
        raise HTTPException(
            409,
            "This license was already bound to another organization. "
            "Please contact support if you believe this is an error.",
        )

    # 3. Apply entitlement (source='helm_license', source_ref=license key)
    await apply_entitlement(
        "org",
        org_id,
        tier=lic.tier,
        seats=lic.seats_limit,
        source="helm_license",
        source_ref=lic.id,
        db=db,
        current_period_end=lic.expires_at,
    )

    # 4. Add activating user as OrgMember with role='owner'
    #    Sentinel HIGH-1: role='owner' ONLY — NEVER User.tier='admin'.
    member = OrgMember(
        org_id=org_id,
        user_id=user.id,
        role="owner",
    )
    db.add(member)

    # 5. Consume any PendingLicenseClaim for this license
    claim_result = await db.execute(
        select(PendingLicenseClaim).where(
            PendingLicenseClaim.helm_license_id == lic.id,
        )
    )
    claim = claim_result.scalar_one_or_none()
    if claim is not None:
        await db.delete(claim)

    # 6. Emit OrgAuditEvent
    audit = OrgAuditEvent(
        id=f"oae_{secrets.token_hex(12)}",
        org_id=org_id,
        org_name_snapshot=org_name,
        event_type="license_activated",
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time="owner",
        target_type="license",
        target_id=_key_prefix(lic.id),
        after=_org_audit_after_json(org_id, org_name, lic, verification_method),
    )
    db.add(audit)

    # If we consumed an ActivationAttempt, mark it
    if attempt_id is not None:
        await db.execute(
            update(ActivationAttempt)
            .where(
                ActivationAttempt.id == attempt_id,
                ActivationAttempt.status == "pending",
            )
            .values(status="consumed", consumed_at=now)
        )

    return {
        "org_id": org_id,
        "name": org_name,
        "slug": slug,
        "tier": lic.tier,
        "seats_limit": lic.seats_limit,
        "verification_method": verification_method,
    }


def _org_audit_after_json(
    org_id: str, org_name: str, lic: HelmLicense, verification_method: str
) -> str:
    """Build the 'after' JSON payload for the org_audit_events row."""
    import json as _json

    return _json.dumps(
        {
            "org_id": org_id,
            "org_name": org_name,
            "license_key_prefix": _key_prefix(lic.id),
            "tier": lic.tier,
            "seats_limit": lic.seats_limit,
            "verification_method": verification_method,
        }
    )


# ---------------------------------------------------------------------------
# Endpoint 1 — GET /api/v1/org/activate/info?key=...
# ---------------------------------------------------------------------------


@router.get("/info")
async def activation_info(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Non-oracular license preview (unauthenticated).

    Returns ONLY {valid, org_name, tier} for a valid+active+unbound+unexpired
    license.  For anything else returns {valid: false} — never distinguish
    revoked/expired/bound/unknown.  NEVER returns contact_email.

    App-layer IP rate-limit (Cloud Armor is the durable multi-replica layer).
    """
    # IP rate-limit
    client_ip = request.client.host if request.client else "unknown"
    if not _info_limiter.is_allowed(client_ip):
        raise HTTPException(
            429,
            "Too many activation info requests. Please try again later.",
        )

    key = key.strip()
    if not key:
        return {"valid": False}

    # Use a synthetic async generator to fetch
    lic = await _active_unbound_license(key, db)

    if lic is None:
        return {"valid": False}

    return {
        "valid": True,
        "org_name": lic.org_name,
        "tier": lic.tier,
    }


# ---------------------------------------------------------------------------
# Endpoint 2 — POST /api/v1/org/activate (Phase A)
# ---------------------------------------------------------------------------


@router.post("")
async def activate_phase_a(
    body: ActivateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Phase A: validate license, create durable activation token, send email.

    If the caller's verified account email matches the license contact_email
    (case-insensitive), skip the email round-trip and go straight to Phase B
    (verification_method='matched_contact_email').

    Otherwise, create a single-use ActivationAttempt with a hashed token,
    commit it, then best-effort email the raw token to the license contact.
    """
    # Rate-limit by user.id + IP
    client_ip = request.client.host if request.client else "unknown"
    if not _activate_limiter.is_allowed(f"user:{user.id}"):
        raise HTTPException(
            429, "Too many activation attempts. Please try again later."
        )
    if not _activate_limiter.is_allowed(f"ip:{client_ip}"):
        raise HTTPException(
            429, "Too many activation attempts. Please try again later."
        )

    key = body.key.strip()

    # 1. Validate the license (read-only)
    lic = await _active_unbound_license(key, db)
    if lic is None:
        raise HTTPException(
            409,
            "License key is invalid, expired, or already bound to an organization.",
        )

    # 2. Check caller is not already in an org
    existing_member = await db.execute(
        select(OrgMember).where(OrgMember.user_id == user.id)
    )
    if existing_member.scalar_one_or_none():
        raise HTTPException(
            409,
            "You are already a member of an organization. "
            "Leave your current org before activating a new license.",
        )

    # Derive org_name and slug
    org_name = (body.org_name or lic.org_name).strip()
    slug = (body.slug or _slugify(org_name)).strip().lower()

    if len(slug) < 3:
        slug = _slugify(org_name)
        if len(slug) < 3:
            slug = f"org-{secrets.token_hex(3)}"

    # Check slug uniqueness (if org_name/slug was customized)
    if body.slug:
        slug_existing = await db.execute(
            select(Organization).where(Organization.slug == slug)
        )
        if slug_existing.scalar_one_or_none():
            raise HTTPException(409, f"Organization slug '{slug}' is already taken")

    # 3. Exact-match email shortcut?
    caller_email = (user.email or "").strip().lower()
    contact_email = (lic.contact_email or "").strip().lower()

    if caller_email and contact_email and caller_email == contact_email:
        # The user already proved email control via account verification.
        # Skip the email round-trip — go straight to Phase B.
        logger.info(
            "activation_exact_match_shortcut key=%s user=%s",
            _key_prefix(key),
            user.id,
        )
        try:
            result = await _phase_b_commit(
                lic=lic,
                org_name=org_name,
                slug=slug,
                user=user,
                db=db,
                verification_method="matched_contact_email",
            )
            await db.commit()
            return result
        except HTTPException:
            await db.rollback()
            raise
        except Exception:
            await db.rollback()
            logger.exception("activation_shortcut_failed key=%s", _key_prefix(key))
            raise HTTPException(500, "Activation failed. Please try again.")

    # 4. Standard path: create durable ActivationAttempt in OWN committed txn.
    # The token IS the verification code the user types — it must be exactly
    # what we hash and what we email (no truncation, or /verify can never
    # match). token_urlsafe(12) → a 16-char, ~96-bit code: short enough to
    # type, strong enough that brute force is infeasible under the 30-min TTL
    # + single-use + rate limiting.
    raw_token = secrets.token_urlsafe(12)
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=30)

    attempt = ActivationAttempt(
        helm_license_id=lic.id,
        token_hash=token_hash,
        contact_email_snapshot=contact_email,
        requested_by_user_id=user.id,
        status="pending",
        expires_at=expires_at,
    )
    db.add(attempt)
    await db.flush()
    attempt_id = attempt.id  # capture before commit clears it
    await db.commit()

    # 5. Best-effort email AFTER commit (durable-before-send)
    email_service = getattr(request.app.state, "email_service", None)
    if email_service and contact_email:
        try:
            html = (
                "<div style='font-family: system-ui, sans-serif; max-width: 480px; "
                "margin: 0 auto; background: #0a0c10; color: #e6edf3; padding: 32px; "
                "border-radius: 8px;'>"
                "<h2 style='margin-bottom: 16px;'>Activate your SessionFS Organization</h2>"
                "<p>Use the verification code below to activate your organization "
                f"<strong>{_html_escape(org_name)}</strong>:</p>"
                "<div style='background: #1c2128; border: 1px solid #30363d; "
                "border-radius: 6px; padding: 16px; margin: 16px 0; text-align: center;'>"
                "<code style='font-size: 24px; letter-spacing: 4px; color: #58a6ff;'>"
                f"{_html_escape(raw_token)}</code>"
                "</div>"
                "<p style='color: #8b949e; font-size: 13px;'>"
                "This code expires in 30 minutes. "
                "If you didn't request this activation, ignore this email.</p>"
                "<p style='color: #8b949e; font-size: 13px;'>"
                "Run <code>sfs org activate --verify &lt;code&gt;</code> to complete activation.</p>"
                "</div>"
            )
            await email_service.send(
                contact_email,
                f"Activate your SessionFS organization — {org_name}",
                html,
            )
            logger.info(
                "activation_email_sent attempt=%s license=%s",
                attempt_id,
                _key_prefix(key),
            )
        except Exception:
            logger.warning(
                "activation_email_failed attempt=%s license=%s — token still valid",
                attempt_id,
                _key_prefix(key),
            )

    logger.info(
        "activation_attempt_created attempt=%s license=%s user=%s",
        attempt_id,
        _key_prefix(key),
        user.id,
    )

    return {
        "status": "verification_sent",
        "message": (
            "A verification code has been sent to the license contact email. "
            "Use it to complete activation."
        ),
    }


# ---------------------------------------------------------------------------
# Endpoint 3 — POST /api/v1/org/activate/verify (Phase B)
# ---------------------------------------------------------------------------


@router.post("/verify")
async def activate_phase_b(
    body: VerifyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Phase B: consume the single-use token and atomically create the org.

    ONE transaction:
      1. Hash token, find pending, non-expired ActivationAttempt.
      2. Consume with rowcount-1 guard (idempotent — used tokens fail).
      3. Verify requestor owns the attempt (requested_by_user_id == caller).
      4. Re-validate license is still active + unbound.
      5. Create org, bind license, apply entitlement, add owner, emit audit.
      6. COMMIT — all writes land together or none do (zero orphans).
    """
    # Rate-limit by user.id + IP
    client_ip = request.client.host if request.client else "unknown"
    if not _verify_limiter.is_allowed(f"user:{user.id}"):
        raise HTTPException(
            429, "Too many verification attempts. Please try again later."
        )
    if not _verify_limiter.is_allowed(f"ip:{client_ip}"):
        raise HTTPException(
            429, "Too many verification attempts. Please try again later."
        )

    raw_token = body.token.strip()
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)

    # 1. Find the pending ActivationAttempt
    attempt_result = await db.execute(
        select(ActivationAttempt).where(
            ActivationAttempt.token_hash == token_hash,
            ActivationAttempt.status == "pending",
            ActivationAttempt.expires_at > now,
        )
    )
    attempt = attempt_result.scalar_one_or_none()

    if attempt is None:
        # Non-oracular — don't distinguish expired vs used vs invalid
        raise HTTPException(
            410,
            "The verification code is invalid, expired, or has already been used. "
            "Please start activation again.",
        )

    # 2. Token must belong to the calling user (anti-theft)
    if attempt.requested_by_user_id != user.id:
        logger.warning(
            "activation_token_wrong_user attempt=%s token_user=%s caller=%s",
            attempt.id,
            attempt.requested_by_user_id,
            user.id,
        )
        raise HTTPException(
            403,
            "The verification code is invalid, expired, or has already been used. "
            "Please start activation again.",
        )

    # 3. Consume the attempt with rowcount-1 guard
    consume_result = await db.execute(
        update(ActivationAttempt)
        .where(
            ActivationAttempt.id == attempt.id,
            ActivationAttempt.status == "pending",
        )
        .values(status="consumed", consumed_at=now)
    )
    if consume_result.rowcount != 1:
        raise HTTPException(
            410,
            "The verification code has already been used. "
            "Please start activation again.",
        )

    # 4. Re-validate license is still active + unbound
    lic = await _active_unbound_license(attempt.helm_license_id, db)
    if lic is None:
        await db.rollback()
        raise HTTPException(
            409,
            "This license is no longer available for activation. "
            "It may have been bound to another organization or expired.",
        )

    # Derive org_name / slug from the license (Phase A payload was not persisted)
    org_name = lic.org_name
    slug = _slugify(org_name)

    # 5. Execute Phase B commit (single txn)
    try:
        result = await _phase_b_commit(
            lic=lic,
            org_name=org_name,
            slug=slug,
            user=user,
            db=db,
            verification_method="email_token",
            attempt_id=attempt.id,
        )
        await db.commit()
        logger.info(
            "activation_complete key=%s org=%s user=%s",
            _key_prefix(lic.id),
            result["org_id"],
            user.id,
        )
        return result
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception(
            "activation_phase_b_failed key=%s user=%s",
            _key_prefix(attempt.helm_license_id),
            user.id,
        )
        raise HTTPException(500, "Activation failed. Please try again.")


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for email templates."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
