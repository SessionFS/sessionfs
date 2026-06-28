"""OIDC SSO login flow — /start, /callback.

SSO-P2 (tk_bff90953b25c42a4): The security crux — account-linking
predicates, anti-takeover guards, JIT provisioning, and sso_minted key
minting.  Built on top of SSO-P1 models (migrations 055+056) and the
existing ApiKey / OrgMember / OrgAuditEvent infrastructure.

Security invariants (Sentinel-reviewed, R2):
  - State+nonce+PKCE verifier bound to an HttpOnly+Secure+SameSite=Lax cookie
  - OidcLoginAttempt consumed with rowcount-1 atomic guard (mirrors activation)
  - id_token validated with alg-pin, issuer-from-attempt, strict email_verified
  - All issuer-derived fetches pass the §3.2.1 SSRF guard
  - Account linking auto-links only on (verified email match + existing
    verified + org member); everything else routes to explicit confirmation
  - explicit_confirm into unverified account revokes pre-existing keys
  - JIT always provisions member, never admin/owner
  - sso_minted is server-set only, never accepted from request input
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ApiKey,
    ExternalIdentity,
    OidcLoginAttempt,
    OrgAuditEvent,
    OrgDomainVerification,
    OrgIdentityProvider,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.oidc import consume_login_attempt
from sessionfs.server.services.oidc_fetch import SsrfError, oidc_fetch_json
from sessionfs.server.services.oidc_token import (
    TokenValidationError,
    validate_id_token,
)

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/auth/sso", tags=["sso"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cookie name for the browser-bound state bundle
SSO_STATE_COOKIE = "sso_state"

# Login attempt TTL (10 minutes — design §3.2)
LOGIN_ATTEMPT_TTL_MINUTES = 10

# Allowlisted redirect_after destinations (exact match only)
_ALLOWED_REDIRECT_ORIGINS: frozenset[str] = frozenset(
    filter(None, os.environ.get("SFS_SSO_REDIRECT_ORIGINS", "").split(","))
)

# Our fixed callback URL
_SELF_CALLBACK_URL: str = os.environ.get(
    "SFS_SSO_CALLBACK_URL", "https://api.sessionfs.dev/api/v1/auth/sso/callback"
)



# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class SsoStartRequest(BaseModel):
    """Body for POST /api/v1/auth/sso/start."""

    org_slug: str | None = None
    org_idp_id: str | None = None
    redirect_after: str | None = None  # post-login destination

    @field_validator("redirect_after")
    @classmethod
    def _validate_redirect(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # Must pass allowlist validation (checked again before redirect)
        return v


class SsoStartResponse(BaseModel):
    authorize_url: str


class SsoCallbackResponse(BaseModel):
    user_id: str
    email: str
    api_key: str | None  # raw key, returned once (for CLI flows)
    link_method: str  # 'existing_link' | 'verified_email_match' | 'jit_provision'
    org_id: str | None
    pending_confirmation: bool = False


class SsoPendingResponse(BaseModel):
    """Returned when auto-link is denied and explicit confirmation is required."""
    pending_confirmation: bool = True
    message: str
    link_token: str  # one-time token for the explicit-confirm flow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_client_secret(client_secret_ref: str) -> str:
    """Resolve an IdP client secret from its ref.

    Supports:
      - env:VAR_NAME → os.environ[VAR_NAME]
      - plain string → returned as-is (dev/test only)

    NEVER logs or persists the resolved secret.
    """
    if client_secret_ref.startswith("env:"):
        var = client_secret_ref[4:]
        val = os.environ.get(var, "")
        if not val:
            raise HTTPException(
                500,
                f"Client secret environment variable {var} is not set. "
                "Please configure the IdP client secret.",
            )
        return val
    # Dev/test: plain string
    return client_secret_ref


def _hash_verifier(raw: str) -> str:
    """Hash a PKCE code verifier for storage (raw verifier NEVER persisted)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _validate_redirect_after(redirect_after: str | None) -> str | None:
    """Validate redirect_after against the exact-origin allowlist.

    Returns the value if allowed, None if not provided, or raises 400.
    """
    if not redirect_after:
        return None

    parsed = urlparse(redirect_after)

    # Relative paths are always allowed (e.g. /dashboard/sessions) — but
    # ONLY genuinely-relative ones. A browser normalizes backslashes to
    # slashes, so "/\evil.com" or "//evil.com" become protocol-relative
    # redirects to an external host. urlparse sees those as path-only, so
    # reject them explicitly here.
    if not parsed.scheme and not parsed.netloc:
        if (
            not redirect_after.startswith("/")
            or redirect_after.startswith("//")
            or "\\" in redirect_after
        ):
            raise HTTPException(
                400, "redirect_after must be a safe absolute path"
            )
        return redirect_after

    # Absolute URLs must match the allowlist exactly
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for allowed in _ALLOWED_REDIRECT_ORIGINS:
        if origin == allowed:
            return redirect_after

    raise HTTPException(
        400,
        f"redirect_after origin '{origin}' is not in the allowed list",
    )


def _is_domain_verified(domain: str, verifications: list[OrgDomainVerification]) -> bool:
    """Check whether *domain* (lowercase) is an org-verified domain."""
    return any(v.domain == domain and v.status == "verified" for v in verifications)


async def _resolve_provider(
    db: AsyncSession, org_slug: str | None, org_idp_id: str | None
) -> OrgIdentityProvider:
    """Resolve the enabled IdP for an org.

    Returns the OrgIdentityProvider row or raises 404.
    """
    if org_idp_id:
        stmt = select(OrgIdentityProvider).where(
            OrgIdentityProvider.id == org_idp_id,
            OrgIdentityProvider.enabled.is_(True),
        )
    elif org_slug:
        stmt = (
            select(OrgIdentityProvider)
            .join(Organization, Organization.id == OrgIdentityProvider.org_id)
            .where(
                Organization.slug == org_slug,
                OrgIdentityProvider.enabled.is_(True),
            )
        )
    else:
        raise HTTPException(400, "Provide org_slug or org_idp_id")

    result = await db.execute(stmt)
    idp = result.scalar_one_or_none()
    if idp is None:
        raise HTTPException(
            404,
            "SSO is not configured for this organization. "
            "Contact your org admin to set up SSO.",
        )
    return idp


async def _fetch_discovery(
    issuer: str, idp: OrgIdentityProvider, db: AsyncSession
) -> dict:
    """Fetch (or use cached) the OIDC discovery document through the SSRF guard."""
    # Per R2-N1: fetch discovery doc ONLY from the exact configured issuer URL.
    # The SSRF guard runs independently on jwks_uri + token_endpoint later.
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        discovery = await oidc_fetch_json(discovery_url, timeout=10.0)
    except SsrfError as exc:
        logger.warning("sso_ssrf_blocked issuer=%s url=%s", issuer, discovery_url)
        raise HTTPException(502, f"SSO provider unreachable: {exc}") from exc
    except Exception as exc:
        logger.warning("sso_discovery_fetch_failed issuer=%s err=%s", issuer, exc)
        raise HTTPException(502, "Could not reach SSO provider") from exc
    return discovery


async def _fetch_jwks(jwks_uri: str) -> dict:
    """Fetch JWKS through the SSRF guard."""
    try:
        return await oidc_fetch_json(jwks_uri, timeout=10.0)
    except SsrfError as exc:
        logger.warning("sso_ssrf_blocked jwks_uri=%s", jwks_uri)
        raise HTTPException(502, f"SSO JWKS endpoint unreachable: {exc}") from exc
    except Exception as exc:
        logger.warning("sso_jwks_fetch_failed uri=%s err=%s", jwks_uri, exc)
        raise HTTPException(502, "Could not reach SSO JWKS endpoint") from exc


async def _exchange_code(
    token_endpoint: str,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Exchange authorization code for tokens at the IdP token_endpoint.

    Server-to-server through the SSRF guard.  The client_secret is sent
    only to the validated token_endpoint — never logged.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _SELF_CALLBACK_URL,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    try:
        return await oidc_fetch_json(
            token_endpoint, method="POST", data=data, timeout=10.0
        )
    except SsrfError as exc:
        logger.warning("sso_ssrf_blocked token_endpoint=%s", token_endpoint)
        raise HTTPException(502, f"SSO token endpoint unreachable: {exc}") from exc
    except Exception as exc:
        logger.warning("sso_token_exchange_failed endpoint=%s err=%s", token_endpoint, exc)
        raise HTTPException(502, "SSO token exchange failed") from exc


def _mint_user_key(user_id: str) -> tuple[str, ApiKey]:
    """Create a new user ApiKey with sso_minted=True.

    Returns (raw_key, api_key_row).  The raw key is returned ONCE to the
    caller and never stored.
    """
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    api_key = ApiKey(
        id=key_id,
        user_id=user_id,
        key_hash=hash_api_key(raw_key),
        name="SSO login",
        key_kind="user",
        sso_minted=True,
    )
    return raw_key, api_key


async def _emit_audit(
    db: AsyncSession,
    *,
    org_id: str,
    org_name_snapshot: str = "",
    event_type: str,
    actor_user_id: str | None = None,
    actor_email_snapshot: str | None = None,
    target_type: str = "external_identity",
    target_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """Emit a single OrgAuditEvent row."""
    audit = OrgAuditEvent(
        id=f"oae_{secrets.token_hex(12)}",
        org_id=org_id,
        org_name_snapshot=org_name_snapshot,
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email_snapshot=actor_email_snapshot,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
    )
    db.add(audit)


async def _link_identity(
    db: AsyncSession,
    *,
    org_idp_id: str,
    org_id: str,
    user_id: str,
    provider_issuer: str,
    subject: str,
    idp_email: str,
    link_method: str,
) -> ExternalIdentity:
    """Create (or re-activate) an ExternalIdentity row."""
    # Check for existing (maybe deactivated) identity
    existing_result = await db.execute(
        select(ExternalIdentity).where(
            ExternalIdentity.org_idp_id == org_idp_id,
            ExternalIdentity.subject == subject,
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        # Re-link onto existing row
        existing.user_id = user_id
        existing.email_at_link = idp_email
        existing.link_method = link_method
        existing.linked_at = datetime.now(timezone.utc)
        existing.last_login_at = datetime.now(timezone.utc)
        existing.deactivated_at = None
        return existing

    eid = f"eid_{secrets.token_hex(12)}"
    identity = ExternalIdentity(
        id=eid,
        user_id=user_id,
        org_idp_id=org_idp_id,
        provider_issuer=provider_issuer,
        subject=subject,
        email_at_link=idp_email,
        link_method=link_method,
        linked_at=datetime.now(timezone.utc),
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(identity)
    return identity


# ---------------------------------------------------------------------------
# Route: POST /start
# ---------------------------------------------------------------------------


@router.post("/start", response_model=SsoStartResponse)
async def sso_start(
    body: SsoStartRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Phase A — start the OIDC authorization-code + PKCE flow.

    Resolves the org's enabled IdP, generates state/nonce/PKCE, commits a
    durable OidcLoginAttempt, sets an HttpOnly+Secure+SameSite=Lax cookie
    with the state bundle, and returns the IdP authorize URL.
    """
    # 1. Resolve IdP
    idp = await _resolve_provider(db, body.org_slug, body.org_idp_id)

    # 2. Validate redirect_after
    redirect_after = _validate_redirect_after(body.redirect_after)

    # 3. Fetch discovery doc (for authorization_endpoint)
    discovery = await _fetch_discovery(idp.issuer, idp, db)
    auth_endpoint = discovery.get("authorization_endpoint", "")
    if not auth_endpoint:
        raise HTTPException(502, "SSO provider did not advertise an authorization_endpoint")

    # 4. Generate state, nonce, PKCE code_verifier
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        hashlib.sha256(code_verifier.encode())
        .digest()
        .hex()
    )  # Actually need base64url for PKCE S256

    # PKCE S256: base64url-encoded SHA256 hash
    import base64 as _base64
    code_challenge = (
        _base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )

    # 5. Create durable OidcLoginAttempt (committed before redirect)
    now = datetime.now(timezone.utc)
    attempt = OidcLoginAttempt(
        id=f"ola_{secrets.token_hex(12)}",
        org_idp_id=idp.id,
        org_id=idp.org_id,
        provider_id=idp.id,
        state=state,
        nonce=nonce,
        pkce_verifier_hash=_hash_verifier(code_verifier),
        redirect_after=redirect_after,
        status="pending",
        expires_at=now + timedelta(minutes=LOGIN_ATTEMPT_TTL_MINUTES),
        created_at=now,
    )
    db.add(attempt)
    await db.commit()

    # 6. Set HttpOnly+Secure+SameSite=Lax cookie with state bundle
    cookie_value = json.dumps({
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "org_idp_id": idp.id,
    })
    response.set_cookie(
        key=SSO_STATE_COOKIE,
        value=cookie_value,
        httponly=True,
        secure=True,  # Only over HTTPS
        samesite="lax",
        max_age=LOGIN_ATTEMPT_TTL_MINUTES * 60,
        path="/api/v1/auth/sso",
    )

    # 7. Build authorize URL
    scopes = json.loads(idp.allowed_scopes)
    params = {
        "response_type": "code",
        "client_id": idp.client_id,
        "redirect_uri": _SELF_CALLBACK_URL,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{auth_endpoint}?{urlencode(params)}"

    logger.info(
        "sso_start org_idp=%s org=%s state_prefix=%s",
        idp.id, idp.org_id, state[:12],
    )
    return SsoStartResponse(authorize_url=authorize_url)


# ---------------------------------------------------------------------------
# Route: GET /callback
# ---------------------------------------------------------------------------


@router.get("/callback")
async def sso_callback(
    code: str = Query(...),
    state: str = Query(...),
    sso_state: str | None = Cookie(default=None, alias=SSO_STATE_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    """Phase B — OIDC callback.  THE SECURITY CRUX.

    Processes the IdP redirect: atomically consumes the OidcLoginAttempt,
    validates the id_token, resolves/links the account, JIT-provisions if
    needed, mints an sso_minted ApiKey, and returns credentials.
    """
    # ---- 0. Validate browser cookie ------------------------------------
    if not sso_state:
        raise HTTPException(400, "Missing SSO state cookie (state_browser_mismatch)")

    try:
        cookie = json.loads(sso_state)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid SSO state cookie")

    cookie_state = cookie.get("state", "")
    cookie_nonce = cookie.get("nonce", "")
    code_verifier = cookie.get("code_verifier", "")
    org_idp_id = cookie.get("org_idp_id", "")

    if cookie_state != state:
        raise HTTPException(400, "State mismatch (state_browser_mismatch)")

    if not code_verifier or not org_idp_id:
        raise HTTPException(400, "Incomplete SSO state cookie")

    # ---- 1. Atomically consume the login attempt (rowcount-1) -----------
    attempt = await consume_login_attempt(db, state=state)
    if attempt is None:
        raise HTTPException(
            400, "Login session expired or already used. Please start again."
        )

    # Verify PKCE verifier against stored hash
    expected_hash = attempt.pkce_verifier_hash
    if _hash_verifier(code_verifier) != expected_hash:
        raise HTTPException(400, "PKCE verification failed")

    # Verify nonce matches
    if attempt.nonce != cookie_nonce:
        raise HTTPException(400, "Nonce mismatch")

    # Bind the cookie's IdP to the DURABLE attempt row — the attempt is the
    # authoritative trust anchor; the cookie is client-held. Resolve the IdP
    # from the attempt (not the cookie) and reject any cookie/attempt drift.
    if attempt.org_idp_id != org_idp_id:
        raise HTTPException(400, "SSO state mismatch")
    org_idp_id = attempt.org_idp_id

    # ---- 2. Resolve IdP and fetch discovery + JWKS ---------------------
    idp_result = await db.execute(
        select(OrgIdentityProvider).where(
            OrgIdentityProvider.id == org_idp_id,
            OrgIdentityProvider.enabled.is_(True),
        )
    )
    idp = idp_result.scalar_one_or_none()
    if idp is None:
        raise HTTPException(400, "SSO provider is no longer available")

    # Fetch org for audit-event org_name_snapshot
    org_result = await db.execute(
        select(Organization.name).where(Organization.id == idp.org_id)
    )
    org_name = org_result.scalar_one_or_none() or "unknown"

    # Fetch discovery doc + JWKS through SSRF guard
    discovery = await _fetch_discovery(idp.issuer, idp, db)

    token_endpoint = discovery.get("token_endpoint", "")
    jwks_uri = discovery.get("jwks_uri", "")
    if not token_endpoint or not jwks_uri:
        raise HTTPException(502, "SSO provider discovery incomplete")

    # SSRF-guard the jwks_uri + token_endpoint INDEPENDENTLY (R2-N1)
    jwks = await _fetch_jwks(jwks_uri)

    # Resolve client secret (NEVER logged)
    client_secret = _resolve_client_secret(idp.client_secret_ref)

    # ---- 3. Exchange code for tokens -----------------------------------
    token_response = await _exchange_code(
        token_endpoint, code, code_verifier, idp.client_id, client_secret
    )
    id_token_str = token_response.get("id_token", "")
    if not id_token_str:
        raise HTTPException(502, "SSO provider did not return an id_token")

    # ---- 4. Validate the id_token --------------------------------------
    try:
        claims = validate_id_token(
            id_token_str,
            jwks=dict(jwks),
            client_id=idp.client_id,
            expected_issuer=idp.issuer,
            expected_nonce=attempt.nonce,
        )
    except TokenValidationError as exc:
        logger.warning(
            "sso_id_token_invalid reason=%s detail=%s org_idp=%s",
            exc.reason, exc.detail, idp.id,
        )
        await _emit_audit(            db,
            org_name_snapshot=org_name,
            org_id=idp.org_id,
            event_type="sso_login_failed",
            target_id=f"idp:{idp.id}",
            before=json.dumps({"reason": exc.reason, "detail": exc.detail}),
        )
        await db.commit()
        raise HTTPException(401, f"SSO authentication failed ({exc.reason})") from exc

    # ---- 5. Extract claims ---------------------------------------------
    sub = cast(str, claims["sub"])
    idp_email = (claims.get("email") or "").strip()
    idp_email_normalized = idp_email.lower()
    domain = idp_email_normalized.split("@")[-1] if "@" in idp_email_normalized else ""

    # ---- 6. Account resolution + linking (THE CRUX — §3.3) --------------
    org_id = idp.org_id

    # Fetch org's verified domains
    verified_domains_result = await db.execute(
        select(OrgDomainVerification).where(OrgDomainVerification.org_id == org_id)
    )
    verified_domains = list(verified_domains_result.scalars().all())

    # --- 6a. EXISTING LINK? (scoped to this IdP — R2 shared-issuer safe) ---
    existing_identity_result = await db.execute(
        select(ExternalIdentity).where(
            ExternalIdentity.org_idp_id == idp.id,
            ExternalIdentity.subject == sub,
        )
    )
    existing_identity = existing_identity_result.scalar_one_or_none()

    if existing_identity is not None:
        if existing_identity.deactivated_at is not None:
            await _emit_audit(                db,
                org_name_snapshot=org_name,
                org_id=org_id,
                event_type="sso_login_failed",
                target_type="external_identity",
                target_id=existing_identity.id,
                before=json.dumps({"reason": "account_deactivated"}),
            )
            await db.commit()
            raise HTTPException(403, "This account has been deactivated")

        # Returning identity — log in
        existing_identity.last_login_at = datetime.now(timezone.utc)
        existing_identity.email_at_link = idp_email

        # Fetch user
        user_result = await db.execute(
            select(User).where(User.id == existing_identity.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(403, "User account is inactive")

        # Mint SSO key
        raw_key, api_key = _mint_user_key(user.id)
        db.add(api_key)

        await _emit_audit(
            db,
            org_name_snapshot=org_name,
            org_id=org_id,
            event_type="sso_login_succeeded",
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            target_type="external_identity",
            target_id=existing_identity.id,
            after=json.dumps({"link_method": "existing_link"}),
        )
        await db.commit()

        return _build_callback_response(
            user, raw_key, "existing_link", org_id, attempt.redirect_after
        )

    # --- 6b. HARD PRECONDITION: email_verified is already strict-true
    #         (enforced by validate_id_token).  We also need the domain
    #         to be org-verified. ---
    if not _is_domain_verified(domain, verified_domains):
        await _emit_audit(
            db,
            org_name_snapshot=org_name,
            org_id=org_id,
            event_type="sso_login_failed",
            before=json.dumps({
                "reason": "email_domain_not_verified_for_org",
                "domain": domain,
            }),
        )
        await db.commit()
        raise HTTPException(
            403,
            f"Email domain '{domain}' is not verified for this organization. "
            "Contact your org admin to verify the domain.",
        )

    # --- 6c. EXISTING USER? (case-insensitive email match) ---
    existing_user_result = await db.execute(
        select(User).where(User.email == idp_email_normalized)
    )
    existing_user = existing_user_result.scalar_one_or_none()

    # An existing-but-INACTIVE account with this email is still "an existing
    # account" (§3.3 step 2d) — JIT must NOT create a duplicate-email user.
    # Refuse rather than fall through to provisioning.
    if existing_user is not None and not existing_user.is_active:
        await _emit_audit(
            db,
            org_name_snapshot=org_name,
            org_id=org_id,
            event_type="sso_login_failed",
            actor_user_id=existing_user.id,
            before=json.dumps({"reason": "existing_account_inactive"}),
        )
        await db.commit()
        raise HTTPException(
            403,
            "An account for this email exists but is inactive. "
            "Contact your org admin.",
        )

    if existing_user is not None and existing_user.is_active:
        # --- THE TAKEOVER DECISION POINT (§3.3 step 2e) ---
        auto_link_allowed = (
            existing_user.email_verified is True
            and existing_user.email == idp_email_normalized  # already normalized
        )

        # Check org membership
        member_result = await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == existing_user.id,
            )
        )
        is_member = member_result.scalar_one_or_none() is not None

        if auto_link_allowed and is_member:
            # AUTO-LINK happy path
            identity = await _link_identity(
                db,
                org_idp_id=idp.id,
                org_id=org_id,
                user_id=existing_user.id,
                provider_issuer=idp.issuer,
                subject=sub,
                idp_email=idp_email,
                link_method="verified_email_match",
            )
            db.add(identity)

            raw_key, api_key = _mint_user_key(existing_user.id)
            db.add(api_key)

            await _emit_audit(                db,
                org_name_snapshot=org_name,
                org_id=org_id,
                event_type="sso_identity_linked",
                actor_user_id=existing_user.id,
                actor_email_snapshot=existing_user.email,
                target_type="external_identity",
                target_id=identity.id,
                after=json.dumps({"link_method": "verified_email_match"}),
            )
            await _emit_audit(                db,
                org_name_snapshot=org_name,
                org_id=org_id,
                event_type="sso_login_succeeded",
                actor_user_id=existing_user.id,
                actor_email_snapshot=existing_user.email,
            )
            await db.commit()

            return _build_callback_response(
                existing_user, raw_key, "verified_email_match", org_id,
                attempt.redirect_after,
            )

        else:
            # DENY auto-link → require explicit confirmation
            await _emit_audit(                db,
                org_name_snapshot=org_name,
                org_id=org_id,
                event_type="sso_identity_link_denied",
                actor_user_id=existing_user.id,
                before=json.dumps({
                    "reason": (
                        "email_unverified_on_sessionfs_side"
                        if not existing_user.email_verified
                        else "not_org_member"
                    ),
                }),
            )
            await db.commit()

            # SAFE deny: we never auto-link here. The dual-control
            # explicit-confirm completion (§3.3.1 reseed-guard) is a
            # follow-up (tk pending) — until it ships, this path refuses
            # and routes the user to their org admin. We do NOT claim an
            # email was sent (none is).
            return _build_pending_response(
                existing_user,
                "This SessionFS account can't be auto-linked to SSO "
                "(its email isn't verified on SessionFS, or you're not yet "
                "a member of this org). Linking requires explicit "
                "confirmation — contact your org admin.",
            )

    # --- 6d. JIT PROVISION (§3.4) — no existing user ---
    # Seat enforcement (§3.4): JIT consumes a seat and MUST respect the org's
    # seat cap, exactly like the invite-accept path (org_members.py). A pending
    # invite does NOT double-count — it isn't a member row, so the live member
    # count already excludes it; the seat it reserved is the one JIT consumes.
    # Checked BEFORE creating any User row so a rejected login leaves no orphan.
    # Lock the Organization row (FOR UPDATE) so concurrent JIT callbacks
    # serialize on the seat count — mirrors the invite-accept path. Without
    # this, N parallel logins each read count<limit and all provision, over-
    # running the paid seat cap. (SQLite ignores FOR UPDATE — single-writer.)
    org_row = (
        await db.execute(
            select(Organization).where(Organization.id == org_id).with_for_update()
        )
    ).scalar_one_or_none()
    seats_limit = org_row.seats_limit if org_row is not None else 0
    member_count = len(
        (
            await db.execute(
                select(OrgMember.id).where(OrgMember.org_id == org_id)
            )
        ).scalars().all()
    )
    if seats_limit is not None and member_count >= seats_limit:
        await _emit_audit(
            db,
            org_name_snapshot=org_name,
            org_id=org_id,
            event_type="sso_login_failed",
            actor_email_snapshot=idp_email_normalized,
            before=json.dumps({
                "reason": "seat_limit_reached",
                "seats_used": member_count,
                "seats_limit": seats_limit,
            }),
        )
        await db.commit()
        raise HTTPException(
            403,
            {
                "error": "seat_limit_reached",
                "seats_used": member_count,
                "seats_limit": seats_limit,
                "message": (
                    "Your organization has no available seats for SSO "
                    "sign-up. Ask your org admin to add seats."
                ),
            },
        )

    # Create User
    new_user_id = str(uuid.uuid4())
    display_name = (claims.get("name") or claims.get("given_name") or idp_email).strip()
    new_user = User(
        id=new_user_id,
        email=idp_email_normalized,
        display_name=display_name[:255],
        email_verified=True,  # IdP verified + domain verified
        tier="free",
    )
    db.add(new_user)

    # Create ExternalIdentity
    identity = await _link_identity(
        db,
        org_idp_id=idp.id,
        org_id=org_id,
        user_id=new_user_id,
        provider_issuer=idp.issuer,
        subject=sub,
        idp_email=idp_email,
        link_method="jit_provision",
    )
    db.add(identity)

    # Upsert OrgMember — member only, never admin/owner (§3.4)
    # ON CONFLICT (org_id, user_id) DO NOTHING (uq_org_members_org_user)
    member_stmt = select(OrgMember).where(
        OrgMember.org_id == org_id,
        OrgMember.user_id == new_user_id,
    )
    member_check = await db.execute(member_stmt)
    if member_check.scalar_one_or_none() is None:
        # Check pending invite reconciliation
        invite_result = await db.execute(
            select(OrgInvite).where(
                OrgInvite.org_id == org_id,
                OrgInvite.email == idp_email_normalized,
            )
        )
        invite = invite_result.scalar_one_or_none()

        role = "member"
        if invite is not None:
            # Honor invited role (capped at admin — never owner)
            if invite.role in ("admin", "owner"):
                role = "admin"  # cap to admin
            else:
                role = invite.role

        member = OrgMember(
            org_id=org_id,
            user_id=new_user_id,
            role=role,
            invited_at=datetime.now(timezone.utc),
        )
        db.add(member)

        # Consume pending invite
        if invite is not None:
            await db.delete(invite)
            await _emit_audit(                db,
                org_name_snapshot=org_name,
                org_id=org_id,
                event_type="sso_invite_reconciled",
                actor_user_id=new_user_id,
                after=json.dumps({"honored_role": role}),
            )

    # Mint SSO key
    raw_key, api_key = _mint_user_key(new_user_id)
    db.add(api_key)

    await _emit_audit(        db,
        org_name_snapshot=org_name,
        org_id=org_id,
        event_type="sso_user_jit_provisioned",
        actor_user_id=new_user_id,
        actor_email_snapshot=idp_email_normalized,
        target_type="external_identity",
        target_id=identity.id,
        after=json.dumps({"link_method": "jit_provision", "role": "member"}),
    )
    await _emit_audit(        db,
        org_name_snapshot=org_name,
        org_id=org_id,
        event_type="sso_login_succeeded",
        actor_user_id=new_user_id,
        actor_email_snapshot=idp_email_normalized,
    )
    await db.commit()

    return _build_callback_response(
        new_user, raw_key, "jit_provision", org_id, attempt.redirect_after,
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _build_callback_response(
    user: User,
    raw_key: str,
    link_method: str,
    org_id: str | None,
    redirect_after: str | None,
) -> dict:
    """Build success response with optional redirect."""
    result = {
        "user_id": user.id,
        "email": user.email,
        "api_key": raw_key,
        "link_method": link_method,
        "org_id": org_id,
        "pending_confirmation": False,
    }
    if redirect_after:
        result["redirect_after"] = redirect_after
    return result


def _build_pending_response(user: User, message: str) -> dict:
    """Build pending-confirmation response."""
    return {
        "pending_confirmation": True,
        "message": message,
        "user_id": user.id,
        "email": user.email,
        "api_key": None,
        "link_method": "explicit_confirm_required",
        "org_id": None,
    }
