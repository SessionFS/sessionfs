"""FastAPI authentication dependencies.

v0.10.10 (tk_2e030a85253143df) — scoped service API keys.

Two dependency tracks (Codex R2 HIGH — pre-route enforcement, not
middleware):

  Track 1: `get_current_user` — legacy. Authenticates and returns User.
           Service keys are REJECTED here with 403 `service_key_not_allowed`
           so undecorated routes are fail-closed for cloud agents.

  Track 2: `require_scope(*scopes)` / `require_any_scope(...)` — the
           ONLY dependency that admits service keys. Enforces:
             - key not revoked
             - key not expired
             - at least one matching scope (or '*' wildcard for user keys)
             - returns AuthContext for the route to inspect

Routes that want to support service keys must opt in by depending on
`require_scope(...)` instead of `get_current_user`. Plain
`get_current_user` routes continue to work for user keys (zero behavior
change) but emit 403 for service-key requests before any side effect.

Project-scoped routes additionally call
`assert_service_key_can_access_project(...)` after fetching the project
to enforce the org/project boundary for service keys (Codex R1 MEDIUM 1).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import hash_api_key
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ApiKey, Project, User
from sessionfs.server.metrics import AUTH_FAILURES_TOTAL

logger = logging.getLogger("sessionfs.api")

_rate_limiter: SlidingWindowRateLimiter | None = None
_rate_limit_disabled: bool | None = None

# Special wildcard reserved for user/admin keys. Service keys must
# enumerate their scopes explicitly (rejected at create otherwise).
WILDCARD_SCOPE = "*"


@dataclass
class AuthContext:
    """Authentication context attached to request.state.

    Built by `get_current_user` and `require_scope(...)`. Routes that
    write audit rows use `actor_type` / `service_key_id` / `service_key_name`
    to record service-key provenance (Codex R1 HIGH 2).
    """

    user: User
    api_key_id: str
    key_kind: str  # 'user' | 'service'
    sso_minted: bool = False  # server-set only — SSO-P4 enforcement
    scopes: list[str] = field(default_factory=list)
    org_id: str | None = None
    service_key_id: str | None = None  # populated when key_kind='service'
    service_key_name: str | None = None
    project_ids: list[str] | None = None  # service-key project allowlist

    @property
    def actor_type(self) -> str:
        """Audit-row actor_type — 'user' or 'service_key'."""
        return "service_key" if self.key_kind == "service" else "user"


def _get_rate_limit_per_minute() -> int:
    """Read rate limit from env, defaulting to 120."""
    return int(os.environ.get("SFS_RATE_LIMIT_PER_MINUTE", "120"))


def get_rate_limiter() -> SlidingWindowRateLimiter:
    """Return the global rate limiter instance."""
    global _rate_limiter, _rate_limit_disabled
    if _rate_limiter is None:
        limit = _get_rate_limit_per_minute()
        _rate_limit_disabled = limit == 0
        _rate_limiter = SlidingWindowRateLimiter(max_requests=max(limit, 1))
    return _rate_limiter


def set_rate_limiter(limiter: SlidingWindowRateLimiter) -> None:
    """Override the rate limiter (for testing)."""
    global _rate_limiter, _rate_limit_disabled
    _rate_limiter = limiter
    _rate_limit_disabled = False


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP. Strips port; honors X-Forwarded-For first hop."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # First hop only — anything after is appended by intermediaries.
        return xff.split(",")[0].strip()[:45]
    if request.client and request.client.host:
        return request.client.host[:45]
    return None


def _parse_scope_list(raw: str | None) -> list[str]:
    """Parse the JSON scopes column. Invalid → empty list (deny)."""
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        return [s for s in loaded if isinstance(s, str)] if isinstance(loaded, list) else []
    except (TypeError, ValueError):
        return []


def _parse_project_ids(raw: str | None) -> list[str] | None:
    """Parse project_ids JSON column. None/empty = all-in-org."""
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
        if not isinstance(loaded, list) or not loaded:
            return None
        return [p for p in loaded if isinstance(p, str)]
    except (TypeError, ValueError):
        return None


async def _authenticate_and_build_context(
    request: Request,
    db: AsyncSession,
    limiter: SlidingWindowRateLimiter,
) -> AuthContext:
    """Shared auth path used by both legacy `get_current_user` and the
    scoped `require_scope` dependencies.

    Enforces (in order): bearer header → rate limit → key lookup →
    is_active → revoked_at → expires_at → user exists → user active.
    Updates last_used_at + last_used_ip + builds AuthContext on
    request.state. Does NOT check scopes — that's the caller's job.

    R2 HIGH — this happens in the dependency layer so service keys are
    rejected BEFORE any route side effect when callers wire scopes
    incorrectly. Plain `get_current_user` routes get fail-closed
    behavior for service keys via the deny-by-default check in
    `get_current_user` itself.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        AUTH_FAILURES_TOTAL.inc(reason="missing_bearer")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    raw_key = auth_header[7:]
    key_hash = hash_api_key(raw_key)

    # Rate limit by key hash (skip if disabled via SFS_RATE_LIMIT_PER_MINUTE=0).
    # A request is charged once even when authenticated twice (the marketplace
    # publish pre-check authenticates before the route dependency runs).
    already_charged = getattr(request.state, "rate_limit_charged_key", None) == key_hash
    request.state.rate_limit_charged_key = key_hash
    if not already_charged and not _rate_limit_disabled and not limiter.is_allowed(key_hash):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Rate limited: client=%s limit=%d/min",
            client_ip,
            limiter.max_requests,
        )
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Look up key
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        AUTH_FAILURES_TOTAL.inc(reason="invalid_key")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Codex R3 MEDIUM 1 — revoked_at check BEFORE is_active so real
    # revoked keys (which also set is_active=False) surface the
    # structured api_key_revoked code instead of the generic 401.
    if api_key.revoked_at is not None:
        AUTH_FAILURES_TOTAL.inc(reason="revoked")
        raise HTTPException(
            status_code=401,
            detail={
                "error": "api_key_revoked",
                "message": "API key has been revoked",
            },
        )
    if not api_key.is_active:
        AUTH_FAILURES_TOTAL.inc(reason="inactive_key")
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key.expires_at is not None:
        exp = api_key.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            AUTH_FAILURES_TOTAL.inc(reason="expired")
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "api_key_expired",
                    "message": "API key has expired; please rotate",
                },
            )

    # Look up user
    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        AUTH_FAILURES_TOTAL.inc(reason="inactive_user")
        raise HTTPException(status_code=403, detail="User account is inactive")

    # Update last_used_at + last_used_ip
    client_ip = _client_ip(request)
    await db.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(
            last_used_at=datetime.now(timezone.utc),
            last_used_ip=client_ip,
        )
    )
    await db.commit()

    ctx = AuthContext(
        user=user,
        api_key_id=api_key.id,
        key_kind=api_key.key_kind or "user",
        sso_minted=bool(api_key.sso_minted),
        scopes=_parse_scope_list(api_key.scopes),
        org_id=api_key.org_id,
        service_key_id=api_key.id if api_key.key_kind == "service" else None,
        service_key_name=api_key.service_key_name,
        project_ids=_parse_project_ids(api_key.project_ids),
    )
    # Stash on request.state so audit-row writers can pull it without
    # re-injecting the dependency.
    request.state.auth_context = ctx
    return ctx


async def _sso_enforcement_check(ctx: AuthContext, db: AsyncSession) -> None:
    """SSO-P4 enforcement check — runs ONLY for user keys.

    HARD INVARIANTS (do not violate):
      1. Service keys are CATEGORICALLY EXEMPT — this function is never
         called for key_kind=='service'.
      2. The org OWNER is always exempt (never locked out).
      3. A valid active SsoBreakGlassGrant exempts one admin until expiry.
      4. Enforcement only REJECTS a non-sso_minted user key for a
         verified-domain user whose org has an enabled + enforced OIDC IdP.
      5. FAIL-OPEN on unexpected errors: log a warning and ALLOW.
    """
    # Step 0 — only for user keys (caller guarantees this).
    if ctx.key_kind != "user":
        return

    # Step 1 — sso_minted keys are always allowed.
    if ctx.sso_minted:
        return

    # Step 2 — derive the user's email domain.
    email = (ctx.user.email or "").strip().lower()
    if "@" not in email:
        return  # no domain to enforce against
    domain = email.rsplit("@", 1)[1]

    try:
        # Step 3 — single query: find an org with enabled+enforced OIDC
        # IdP + verified domain matching the user's domain, where the
        # user is a member.
        from sqlalchemy import and_

        from sessionfs.server.db.models import (
            OrgDomainVerification,
            OrgIdentityProvider,
            OrgMember,
            SsoBreakGlassGrant,
        )

        result = await db.execute(
            select(OrgIdentityProvider)
            .join(
                OrgDomainVerification,
                and_(
                    OrgDomainVerification.org_id == OrgIdentityProvider.org_id,
                    OrgDomainVerification.domain == domain,
                    OrgDomainVerification.status == "verified",
                ),
            )
            .where(
                OrgIdentityProvider.enabled.is_(True),
                OrgIdentityProvider.enforced.is_(True),
                OrgIdentityProvider.protocol == "oidc",
            )
            .limit(1)
        )
        enforcing_idp = result.scalar_one_or_none()
        if enforcing_idp is None:
            return  # not under enforcement for this domain

        org_id = enforcing_idp.org_id

        # Step 4 — check membership.
        member_result = await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == ctx.user.id,
            )
        )
        membership = member_result.scalar_one_or_none()
        if membership is None:
            return  # not a member of the enforcing org

        # Step 5 — exempt OWNER (never locked out).
        if membership.role == "owner":
            return

        # Step 6 — exempt active break-glass grant.
        now_dt = datetime.now(timezone.utc)
        grant_result = await db.execute(
            select(SsoBreakGlassGrant).where(
                SsoBreakGlassGrant.org_id == org_id,
                SsoBreakGlassGrant.admin_user_id == ctx.user.id,
                SsoBreakGlassGrant.revoked_at.is_(None),
                SsoBreakGlassGrant.expires_at > now_dt,
            ).limit(1)
        )
        if grant_result.scalar_one_or_none() is not None:
            return

        # Step 7 — enforcement required. Reject with structured detail.
        # Resolve org slug for the SSO start URL.
        from sessionfs.server.db.models import Organization

        org_result = await db.execute(
            select(Organization.slug).where(Organization.id == org_id)
        )
        org_slug = org_result.scalar_one_or_none() or org_id

        raise HTTPException(
            status_code=403,
            detail={
                "error": "sso_enforcement_required",
                "message": (
                    "This account requires SSO authentication. "
                    "Re-authenticate via your organization's SSO provider."
                ),
                "org_slug": org_slug,
                # P2's start endpoint is POST /api/v1/auth/sso/start with
                # {"org_slug": ...} in the body (not a path param).
                "sso_start_url": "/api/v1/auth/sso/start",
            },
        )

    except HTTPException:
        raise  # re-raise our own enforcement rejection
    except Exception:
        # FAIL-OPEN: an enforcement-eval bug must never take down auth
        # for an org. Log a warning and allow the request.
        logger.warning(
            "sso_enforcement_check_error user=%s key=%s — FAIL-OPEN: "
            "allowing request despite enforcement eval error",
            ctx.user.id,
            ctx.api_key_id,
            exc_info=True,
        )
        return


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> User:
    """Authenticate via Bearer token and return the current user.

    v0.10.10 (Codex R2 HIGH) — service keys are REJECTED here. The
    legacy dependency is the deny-by-default gate for cloud agents:
    any route that uses plain `Depends(get_current_user)` rejects
    service-key requests before any side effect can happen.

    Routes that want to support service keys must depend on
    `require_scope('...')` instead, which validates the scope at
    auth time (pre-route).
    """
    ctx = await _authenticate_and_build_context(request, db, limiter)
    if ctx.key_kind == "service":
        raise HTTPException(
            status_code=403,
            detail={
                # `code` becomes the envelope's error.code; `error` stays in
                # error.details for clients that matched on the older shape.
                "code": "service_key_not_allowed",
                "error": "service_key_not_allowed",
                "message": (
                    "Service keys must call routes that opt in via "
                    "require_scope(...); this endpoint accepts user "
                    "keys only."
                ),
                "service_key_name": ctx.service_key_name,
            },
        )
    # SSO-P4: enforcement check for user keys only (service keys
    # already rejected above).
    await _sso_enforcement_check(ctx, db)
    return ctx.user


def require_scope(*scopes: str):
    """Dependency factory — admits a request whose key has ANY of the
    listed scopes (OR semantics — same as require_any_scope).

    Use on routes that should be callable by service keys with that
    specific capability. User keys with scopes=['*'] always pass.

    Example:
        @router.post("/handoffs")
        async def create_handoff(
            body: CreateHandoffRequest,
            ctx: AuthContext = Depends(require_scope("handoffs:write")),
            ...
        ):
    """
    if not scopes:
        raise ValueError("require_scope() needs at least one scope")
    required = set(scopes)

    async def _dep(
        request: Request,
        db: AsyncSession = Depends(get_db),
        limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    ) -> AuthContext:
        ctx = await _authenticate_and_build_context(request, db, limiter)
        # Wildcard satisfies anything (user/admin keys only — service keys
        # reject '*' at create time, so a service key here can never have it).
        if WILDCARD_SCOPE in ctx.scopes:
            # SSO-P4: enforcement check for user keys on scoped routes.
            await _sso_enforcement_check(ctx, db)
            return ctx
        if not required.intersection(ctx.scopes):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "required": sorted(required),
                    "current": ctx.scopes,
                    "message": (
                        f"Scope required: one of {sorted(required)}. "
                        f"Key has: {ctx.scopes}."
                    ),
                },
            )
        # SSO-P4: enforcement check for user keys on scoped routes.
        await _sso_enforcement_check(ctx, db)
        return ctx

    return _dep


def require_any_scope(*scopes: str):
    """Alias for `require_scope` — same OR semantics, clearer at call site
    for routes covered by multiple scopes (e.g. read OR write)."""
    return require_scope(*scopes)


# Scheduled platform sweeps (catalog regenerate, verify sweep, freshness,
# rescan, publisher recheck, GitHub webhook-delivery sweep). The scope is
# platform-admin mint only (routes/api_keys.py PLATFORM_ADMIN_SCOPES).
PLATFORM_SWEEPS_SCOPE = "platform_sweeps:run"


def require_platform_sweeper():
    """Dependency for the platform sweep endpoints.

    Admits EITHER a platform-admin (tier ``admin``) user key, exactly as
    ``require_admin`` did, OR a service key that carries
    ``platform_sweeps:run`` and whose owner (the user it runs as) and minter
    (``created_by_user_id``; the rotator after a rotation) are both CURRENTLY
    active platform admins. Demoting or deactivating either stops the key on
    the next request. Any other service key is refused before the route runs.
    """
    required = {PLATFORM_SWEEPS_SCOPE}

    async def _dep(
        request: Request,
        db: AsyncSession = Depends(get_db),
        limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    ) -> AuthContext:
        ctx = await _authenticate_and_build_context(request, db, limiter)
        if ctx.key_kind != "service":
            await _sso_enforcement_check(ctx, db)
            if ctx.user.tier != "admin":
                raise HTTPException(status_code=403, detail="Admin access required")
            return ctx
        if not required.intersection(ctx.scopes):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "required": sorted(required),
                    "current": ctx.scopes,
                    "message": (
                        f"Scope required: one of {sorted(required)}. "
                        f"Key has: {ctx.scopes}."
                    ),
                },
            )
        await assert_platform_key_minter_active(
            ctx, db, error="platform_sweeper_denied", scope=PLATFORM_SWEEPS_SCOPE
        )
        return ctx

    return _dep


# Operator metrics scrape (GitHub #90). Same shape as the platform sweeps:
# a platform-admin user key, or a narrow service key minted and run by active
# platform admins.
ADMIN_METRICS_SCOPE = "admin_metrics:read"


def require_admin_metrics():
    """Dependency for the operator metrics endpoint.

    Admits EITHER a platform-admin (tier ``admin``) user key, exactly as
    ``require_admin`` did, OR a service key that carries
    ``admin_metrics:read`` and whose owner (the user it runs as) and minter
    (``created_by_user_id``; the rotator after a rotation) are both CURRENTLY
    active platform admins. Demoting or deactivating either stops the key on
    the next request. Any other service key is refused before the route runs.
    """
    required = {ADMIN_METRICS_SCOPE}

    async def _dep(
        request: Request,
        db: AsyncSession = Depends(get_db),
        limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    ) -> AuthContext:
        ctx = await _authenticate_and_build_context(request, db, limiter)
        if ctx.key_kind != "service":
            await _sso_enforcement_check(ctx, db)
            if ctx.user.tier != "admin":
                raise HTTPException(status_code=403, detail="Admin access required")
            return ctx
        if not required.intersection(ctx.scopes):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "required": sorted(required),
                    "current": ctx.scopes,
                    "message": (
                        f"Scope required: one of {sorted(required)}. "
                        f"Key has: {ctx.scopes}."
                    ),
                },
            )
        await assert_platform_key_minter_active(
            ctx, db, error="admin_metrics_denied", scope=ADMIN_METRICS_SCOPE
        )
        return ctx

    return _dep


async def assert_platform_key_minter_active(
    ctx: AuthContext, db: AsyncSession, *, error: str, scope: str
) -> None:
    """For a service key carrying a platform-only scope: the user it runs as
    (``user_id``) and its minter (``created_by_user_id``; the rotator after a
    rotation) must BOTH be active platform admins right now. Demoting or
    deactivating either stops the key on its next request. No-op for user
    keys."""
    if ctx.key_kind != "service":
        return
    api_key = await db.get(ApiKey, ctx.api_key_id)
    minter_id = api_key.created_by_user_id if api_key is not None else None

    if minter_id is None:
        minter_tier = "unknown"
    else:
        minter = ctx.user if minter_id == ctx.user.id else await db.get(User, minter_id)
        owner_ok = ctx.user.is_active and ctx.user.tier == "admin"
        minter_ok = minter is not None and minter.is_active and minter.tier == "admin"
        if owner_ok and minter_ok:
            return
        if minter is None or not minter.is_active:
            minter_tier = "deactivated"
        else:
            minter_tier = minter.tier

    message = (
        f"A {scope} key works only while its owner (the user it runs as) and "
        "the user who minted it are both active platform admins."
    )
    detail = {
        "error": error,
        "message": message,
        "minter_user_id": minter_id,
        "minter_current_tier": minter_tier,
        "required_minter_tier": "admin",
        "resolution": (
            "Restore the minter's admin tier, or mint a new key as a user "
            "who will stay a platform admin."
        ),
    }
    # The caller presented the key to get here, so these details only reach
    # the key's own holder. Log the key id (never the key itself).
    logger.warning(
        "platform key denied: error=%s key_id=%s minter_user_id=%s minter_current_tier=%s",
        error, ctx.api_key_id, minter_id, minter_tier,
    )
    raise HTTPException(status_code=403, detail=detail)


def sweep_actor_details(ctx: AuthContext) -> dict:
    """Actor provenance for the AdminAction rows the sweeps write
    (``admin_id`` is the key's owner; these fields say which key acted)."""
    return {
        "actor_type": ctx.actor_type,
        "service_key_id": ctx.service_key_id,
        "service_key_name": ctx.service_key_name,
    }


async def assert_service_key_can_access_project(
    db: AsyncSession,
    ctx: AuthContext,
    project: Project,
    *,
    opaque_404_detail: str | None = None,
) -> None:
    """Cross-org enforcement for service-key requests (Codex R1 MEDIUM 1).

    Routes that take a project_id and use `require_scope(...)` call this
    after fetching the project to verify:
      1. The service key's `org_id` matches the project's `org_id`
      2. If the key has a `project_ids` allowlist, the project is in it

    User keys are unaffected (no org boundary on user keys — they keep
    legacy any-project-they-can-see semantics).

    ``opaque_404_detail``: when set, every deny raises 404 with this detail
    instead of 403. Read routes use it so a cross-org or unallowlisted
    project is indistinguishable from one that does not exist — no
    ``cross_org_denied``/``project_not_in_allowlist`` code and no
    ``project_org_id``/``allowed_project_ids`` leak. The detail is whatever
    the route returns for an unknown project, so the deny is byte-identical
    to a miss. Write routes omit it and keep the 403 oracle.
    """
    if ctx.key_kind != "service":
        return

    def _deny(detail: dict) -> None:
        if opaque_404_detail is not None:
            raise HTTPException(status_code=404, detail=opaque_404_detail)
        raise HTTPException(status_code=403, detail=detail)

    if ctx.org_id is None:
        # Schema-level: service keys are required to have org_id, but
        # be defensive if a malformed row sneaks through.
        _deny({
            "error": "service_key_missing_org",
            "message": "Service key lacks org_id binding",
        })
    if project.org_id != ctx.org_id:
        _deny({
            "error": "cross_org_denied",
            "key_org_id": ctx.org_id,
            "project_org_id": project.org_id,
            "message": "Service key cannot access projects outside its org",
        })
    # Optional per-key project allowlist within org.
    if ctx.project_ids is not None and project.id not in ctx.project_ids:
        _deny({
            "error": "project_not_in_allowlist",
            "allowed_project_ids": ctx.project_ids,
            "requested_project_id": project.id,
            "message": "Service key project allowlist does not include this project",
        })


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require that the authenticated user has admin tier."""
    if user.tier != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_verified_user(
    user: User = Depends(get_current_user),
) -> User:
    """Require that the authenticated user has verified their email."""
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Check your inbox for the verification link.",
        )
    return user
