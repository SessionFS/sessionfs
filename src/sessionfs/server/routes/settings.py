"""Settings routes — judge LLM configuration, GitHub and GitLab integrations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import and_, delete, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    GitHubClaimAttempt,
    GitHubInstallation,
    GitLabSettings,
    User,
    UserJudgeSettings,
)
from sessionfs.server.llm_keys import decrypt_llm_key, encrypt_llm_key, encrypt_secret
from sessionfs.server.routes.llm_providers import validate_provider_or_422
from sessionfs.server.services import github_oauth

logger = logging.getLogger("sessionfs.server.routes.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class JudgeSettingsRequest(BaseModel):
    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None


class JudgeSettingsResponse(BaseModel):
    provider: str
    model: str
    key_set: bool
    base_url: str | None = None


class SaveWarning(BaseModel):
    code: str
    message: str


class JudgeSettingsSaveResponse(JudgeSettingsResponse):
    # Transient connection-check failures (the settings were saved anyway).
    warnings: list[SaveWarning] = Field(default_factory=list)


class LlmConnectionTestRequest(BaseModel):
    """Body of ``POST .../test``. Every field is optional.

    An omitted field falls back to the saved value; an explicit empty or
    null ``base_url`` means "no custom endpoint" (the provider's own API).
    Without ``api_key`` the saved key is used, and only for exactly the
    provider/endpoint it was saved with.
    """

    provider: str | None = Field(None, max_length=32)
    model: str | None = Field(None, max_length=128)
    base_url: str | None = Field(None, max_length=500)
    api_key: str | None = Field(None, max_length=4096)


class LlmConnectionTestResponse(BaseModel):
    ok: bool
    code: str
    message: str
    provider: str
    model: str
    endpoint_host: str
    # Omitted (null) on failures against private addresses.
    latency_ms: int | None = None


def resolve_test_target(
    body: LlmConnectionTestRequest,
    *,
    saved_provider: str | None,
    saved_model: str | None,
    saved_base_url: str | None,
) -> tuple[str, str, str | None]:
    """The (provider, model, base_url) a connection test runs against.

    Omitted fields fall back to the saved row; 422 when there is nothing to
    test. The provider id must be in the registry unless a custom endpoint
    routes the request.
    """
    from sessionfs.judge.provider_registry import get_provider

    fields = body.model_fields_set
    provider = (body.provider or "").strip().lower() if body.provider else (saved_provider or "")
    if "base_url" in fields:
        base_url = (body.base_url or "").strip() or None
    else:
        base_url = saved_base_url or None
    if not provider and not base_url:
        raise HTTPException(
            status_code=422,
            detail={"code": "provider_required", "message": "Choose a provider to test."},
        )
    validate_provider_or_422(provider, base_url)
    model = (body.model or "").strip() or (saved_model or "")
    if not model:
        profile = get_provider(provider)
        if profile is not None and profile.suggested_models:
            model = profile.suggested_models[0]
    if not model:
        raise HTTPException(
            status_code=422,
            detail={"code": "model_required", "message": "Enter a model to test."},
        )
    return provider, model, base_url


async def release_transaction(db: AsyncSession) -> None:
    """End the request's read transaction before a slow outbound call.

    A connection test can take up to 20 s; holding a pooled DB connection
    (and any row snapshot) open for that long would starve the pool. The
    session factory uses ``expire_on_commit=False``, so loaded objects stay
    readable; anything written afterwards is re-read first.
    """
    await db.commit()


def _rate_limited_or_continue(user: User) -> None:
    from sessionfs.server.services import llm_connection

    if not llm_connection.take_test_slot(user.id):
        raise HTTPException(status_code=429, detail=llm_connection.RATE_LIMITED)


@router.post("/judge/test", response_model=LlmConnectionTestResponse)
async def test_judge_connection(
    body: LlmConnectionTestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Make one minimal chat request with the given (or saved) settings.

    Returns ``{ok, code, message, provider, model, endpoint_host,
    latency_ms}``; a failed test is a 200 with ``ok: false`` and a readable
    ``code``. 400 ``key_required`` when no key was supplied and the saved key
    belongs to a different provider/endpoint; 429 ``test_rate_limited``.
    """
    from sessionfs.server.services import llm_connection

    settings = (
        await db.execute(select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id))
    ).scalar_one_or_none()
    provider, model, base_url = resolve_test_target(
        body,
        saved_provider=settings.provider if settings else None,
        saved_model=settings.model if settings else None,
        saved_base_url=settings.base_url if settings else None,
    )

    api_key = (body.api_key or "").strip()
    if not api_key:
        saved_key = ""
        if settings is not None and settings.encrypted_api_key:
            try:
                saved_key = decrypt_llm_key(settings.encrypted_api_key)
            except Exception:
                saved_key = ""
        if settings is not None and saved_key:
            bound = llm_connection.saved_key_bound_to(
                settings.provider,
                settings.base_url,
                provider=provider,
                base_url=base_url,
            ) and saved_judge_key_matches(settings, provider=provider, base_url=base_url)
            if not bound:
                raise HTTPException(status_code=400, detail=llm_connection.KEY_REQUIRED)
            api_key = saved_key
        elif not base_url:
            # A registry provider always needs a key; a custom endpoint may
            # be keyless (a self-hosted Ollama), so it is tested without one.
            raise HTTPException(status_code=400, detail=llm_connection.KEY_REQUIRED)

    _rate_limited_or_continue(user)
    await release_transaction(db)
    result = await llm_connection.run_test(
        provider=provider, model=model, api_key=api_key, base_url=base_url
    )
    return result.to_dict()


@router.put("/judge", response_model=JudgeSettingsSaveResponse)
async def put_judge_settings(
    body: JudgeSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JudgeSettingsSaveResponse:
    """Store judge LLM settings with encrypted API key.

    With a custom ``base_url`` that is new or saved with a new key, one test
    request runs first: an address that isn't a chat API, a rejected key or
    a blocked endpoint is refused (422 ``{code, message}``); a transient
    failure saves anyway and is reported in ``warnings``.
    """
    from sessionfs.server.services import llm_connection

    validate_provider_or_422(body.provider, body.base_url)
    body.base_url = await _check_llm_base_url_or_400(body.base_url)
    encrypted_key = encrypt_llm_key(body.api_key)

    stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    warnings: list[dict] = []
    if body.base_url:
        base_changed = existing is None or llm_connection.normalise_base_url(
            existing.base_url
        ) != llm_connection.normalise_base_url(body.base_url)
        if base_changed or body.api_key:
            user_id = user.id
            await release_transaction(db)
            warnings = await llm_connection.save_time_check(
                user_id=user_id,
                provider=body.provider,
                model=body.model,
                api_key=body.api_key,
                base_url=body.base_url,
                base_changed=base_changed,
            )
            # Re-read: the row may have changed during the outbound call.
            existing = (
                await db.execute(stmt.execution_options(populate_existing=True))
            ).scalar_one_or_none()

    if existing:
        existing.provider = body.provider
        existing.model = body.model
        existing.encrypted_api_key = encrypted_key
        existing.base_url = body.base_url
    else:
        settings = UserJudgeSettings(
            user_id=user.id,
            provider=body.provider,
            model=body.model,
            encrypted_api_key=encrypted_key,
            base_url=body.base_url,
        )
        db.add(settings)

    await db.commit()

    return JudgeSettingsSaveResponse(
        provider=body.provider,
        model=body.model,
        key_set=True,
        base_url=body.base_url,
        warnings=[SaveWarning(**w) for w in warnings],
    )


@router.get("/judge", response_model=JudgeSettingsResponse)
async def get_judge_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JudgeSettingsResponse:
    """Return judge settings (never returns the key itself)."""
    stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    if settings is None:
        return JudgeSettingsResponse(provider="", model="", key_set=False)

    return JudgeSettingsResponse(
        provider=settings.provider,
        model=settings.model,
        key_set=True,
        base_url=settings.base_url,
    )


@router.delete("/judge")
async def delete_judge_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete judge settings."""
    stmt = delete(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    await db.execute(stmt)
    await db.commit()
    return {"deleted": True}


# ---- Audit Trigger Settings ----


class AuditTriggerRequest(BaseModel):
    trigger: str  # "manual", "on_sync", "on_pr"


@router.get("/audit-trigger")
async def get_audit_trigger(
    user: User = Depends(get_current_user),
) -> dict:
    """Get user's auto-audit trigger setting."""
    return {"trigger": user.audit_trigger or "manual"}


@router.put("/audit-trigger")
async def update_audit_trigger(
    body: AuditTriggerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update auto-audit trigger."""
    if body.trigger not in ("manual", "on_sync", "on_pr"):
        from fastapi import HTTPException
        raise HTTPException(400, "Trigger must be 'manual', 'on_sync', or 'on_pr'")
    user.audit_trigger = body.trigger
    await db.commit()
    return {"trigger": body.trigger}


# ---- Summarize Trigger Settings ----


@router.get("/summarize-trigger")
async def get_summarize_trigger(
    user: User = Depends(get_current_user),
) -> dict:
    """Get user's auto-summarize trigger setting."""
    return {"trigger": getattr(user, "summarize_trigger", "manual") or "manual"}


@router.put("/summarize-trigger")
async def update_summarize_trigger(
    body: AuditTriggerRequest,  # Same schema: {trigger: str}
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update auto-summarize trigger."""
    if body.trigger not in ("manual", "on_sync", "on_pr"):
        from fastapi import HTTPException
        raise HTTPException(400, "Trigger must be 'manual', 'on_sync', or 'on_pr'")
    user.summarize_trigger = body.trigger
    await db.commit()
    return {"trigger": body.trigger}


class DiscoverModelsRequest(BaseModel):
    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=4096)
    provider: str | None = Field(None, max_length=32)


_DISCOVERY_MAX_BYTES = 2 * 1024 * 1024
_ENDPOINT_REFUSED = (
    "This endpoint URL is not allowed. It must be https and resolve to public "
    "addresses (self-hosted servers can allow private hosts with "
    "SFS_LLM_ALLOW_PRIVATE_HOSTS)."
)


async def _check_llm_base_url_or_400(base_url: str | None) -> str | None:
    """Validate a user-supplied LLM endpoint; 400 with a generic message (the
    resolved addresses are logged, never returned)."""
    if base_url is None or not base_url.strip():
        return None
    import asyncio

    from sessionfs.server.llm_url_guard import SsrfError, validate_llm_base_url

    try:
        return await asyncio.to_thread(validate_llm_base_url, base_url)
    except SsrfError as exc:
        logger.warning("Refused LLM endpoint %s: %s", base_url[:200], exc)
        raise HTTPException(400, _ENDPOINT_REFUSED) from None


def saved_judge_key_matches(
    settings: UserJudgeSettings,
    *,
    provider: str | None,
    base_url: str | None,
) -> bool:
    """Whether a request may use the saved judge key it did not supply.

    NOTE: an empty/None ``base_url`` here means "fall back to the saved
    endpoint" and always matches. That suits routes where omitting the
    override keeps the saved endpoint (audit, narrative), but a route where
    an empty base_url means "use the provider's own API" (the connection
    tests) must ALSO require ``services.llm_connection.saved_key_bound_to``,
    the strict binding, or a saved custom-endpoint key would be sent to the
    registry provider.

    The saved key is write-only and bound to the provider/endpoint it was
    saved with. A request that overrides the endpoint (``base_url``, already
    checked) or, for a key saved without a custom endpoint, the provider would
    otherwise send the stored key to a host the caller picks. ``None`` /
    empty overrides fall back to the saved values and always match.
    """
    saved_base = (settings.base_url or "").rstrip("/")
    if base_url:
        return bool(saved_base) and saved_base == base_url.rstrip("/")
    if provider and not saved_base:
        return provider.strip().lower() == (settings.provider or "").strip().lower()
    return True


async def _saved_judge_api_key(
    user: User,
    db: AsyncSession,
    *,
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """The caller's saved judge key, but only for the endpoint it was saved for.

    A stored key is write-only: discovery must never send it to a different
    provider or URL than the one it was saved with (it would leak the key to
    that host). ``provider`` is a registry id (fixed endpoint, saved row must
    have no custom base_url); ``base_url`` is an already-checked custom URL
    that must equal the saved one.
    """
    stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()
    if settings is None or not settings.encrypted_api_key:
        return ""
    saved_base = (settings.base_url or "").rstrip("/")
    if base_url is not None:
        if not saved_base or saved_base != base_url.rstrip("/"):
            return ""
    elif provider is not None:
        if saved_base or (settings.provider or "").strip().lower() != provider:
            return ""
    else:
        return ""
    if settings.encrypted_api_key:
        try:
            return decrypt_llm_key(settings.encrypted_api_key)
        except Exception:
            return ""
    return ""


async def _fetch_models(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code >= 300:
            return resp.status_code, b""
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > _DISCOVERY_MAX_BYTES:
                raise ValueError("response too large")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)


def _parse_model_list(raw: bytes) -> list[dict]:
    """Parse a OpenAI-compatible /models body into ``{"id", "owned_by"}`` items."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    models_list = data.get("data", data.get("models", [])) if isinstance(data, dict) else []
    if not isinstance(models_list, list):
        return []
    models: list[dict] = []
    for m in models_list[:1000]:
        if isinstance(m, dict):
            model_id = m.get("id", m.get("model", ""))
            if model_id and isinstance(model_id, str):
                owned_by = m.get("owned_by", "")
                models.append({
                    "id": model_id[:200],
                    "owned_by": owned_by[:200] if isinstance(owned_by, str) else "",
                })
        elif isinstance(m, str) and m:
            models.append({"id": m[:200], "owned_by": ""})
    return models


async def _discover_models(base_url: str, api_key: str, user: User, db: AsyncSession) -> dict:
    from sessionfs.server.llm_url_guard import guard_llm_base_url

    checked = await _check_llm_base_url_or_400(base_url)
    if checked is None:
        raise HTTPException(400, "base_url is required")

    if not api_key:
        api_key = await _saved_judge_api_key(user, db, base_url=checked)

    url = checked
    if not url.endswith("/models"):
        if url.endswith("/v1"):
            url = f"{url}/models"
        else:
            url = f"{url}/v1/models"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            # Re-checked right before each request (DNS rebinding).
            await guard_llm_base_url(checked)
            status, raw = await _fetch_models(client, url, headers)
            if status == 404:
                # Some endpoints use /models without the /v1 prefix.
                alt_url = checked + "/models"
                if alt_url != url:
                    await guard_llm_base_url(checked)
                    status, raw = await _fetch_models(client, alt_url, headers)

        if status >= 300:
            return {"models": [], "error": f"Endpoint returned {status}"}

        return {"models": _parse_model_list(raw), "base_url": checked}

    except httpx.TimeoutException:
        return {"models": [], "error": "Connection timed out"}
    except Exception as e:
        # Never echo the exception text: it would turn this into a network
        # probe (connection refused vs. TLS error vs. refused address).
        logger.warning("Model discovery failed for %s: %s", base_url[:200], e)
        return {"models": [], "error": "Could not list models from this endpoint"}


async def _discover_models_from_provider(
    provider: str, api_key: str, user: User, db: AsyncSession
) -> dict:
    """List models for a registry provider using its fixed /models endpoint.

    Falls back to the provider's suggested models when the provider exposes no
    list-models endpoint or the fetch fails, so the picker always has usable
    ids. The endpoint is fixed by the registry (never user-supplied), but the
    response is still capped and redirects are not followed.
    """
    from sessionfs.judge.provider_registry import get_provider

    profile = get_provider(provider)
    if profile is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_provider",
                "message": f"Unknown LLM provider: {provider}",
            },
        )

    suggested = [{"id": m, "owned_by": ""} for m in profile.suggested_models]
    if not profile.discovery or not profile.models_path:
        return {"models": suggested, "provider": profile.id, "base_url": None}

    url = profile.base_url.rstrip("/") + profile.models_path
    if not api_key:
        api_key = await _saved_judge_api_key(user, db, provider=profile.id)

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            status, raw = await _fetch_models(client, url, headers)
        if status >= 300:
            return {"models": suggested, "provider": profile.id, "base_url": url, "error": f"Endpoint returned {status}"}
        fetched = _parse_model_list(raw)
        # Prefer the live list; keep the curated suggestions when it is empty.
        return {"models": fetched or suggested, "provider": profile.id, "base_url": url}

    except httpx.TimeoutException:
        return {"models": suggested, "provider": profile.id, "error": "Connection timed out"}
    except Exception as e:
        logger.warning("Provider model discovery failed for %s: %s", provider, e)
        return {"models": suggested, "provider": profile.id, "error": "Could not list models from this endpoint"}


@router.get("/judge/models")
async def discover_models(
    base_url: str = Query(..., max_length=500, description="OpenAI-compatible endpoint URL"),
    api_key: str | None = Query(None, include_in_schema=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Discover available models from an OpenAI-compatible endpoint using the
    caller's saved judge API key (if any).

    To test a key that is not saved yet, use POST /judge/models: an API key in
    a query string lands in access logs, so it is refused here.
    """
    if api_key:
        raise HTTPException(
            400,
            "Send the API key in the body of POST /api/v1/settings/judge/models, not in the query string.",
        )
    return await _discover_models(base_url, "", user, db)


@router.post("/judge/models")
async def discover_models_post(
    body: DiscoverModelsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Discover available models; the optional API key travels in the body.
    Without one, the caller's saved judge key is used.

    A registry ``provider`` id routes discovery to that provider's fixed
    /models endpoint; otherwise a custom ``base_url`` is queried as before.
    """
    if body.provider:
        return await _discover_models_from_provider(body.provider, body.api_key, user, db)
    return await _discover_models(body.base_url, body.api_key, user, db)


# --- GitHub installation settings ---
#
# Claiming an installation binds a GitHub App installation (and with it PR
# comments, github_app repo verification and publisher verification) to a
# SessionFS user. A claim is accepted ONLY from a PKCE-bound GitHub OAuth
# authorization started by this server for the signed-in user, after
# GitHub's GET /user/installations (called with that user's token) lists the
# installation.
#
# Flows:
#   install    start -> github.com/apps/<slug>/installations/new?state=S1
#              -> callback?state=S1&installation_id=N (Setup URL, or callback
#              URL when "request user authorization during installation" is
#              on). Any code on this hop is NOT PKCE-bound and is ignored;
#              the callback starts the authorize flow below targeting N.
#   authorize  start -> github.com/login/oauth/authorize (state + PKCE)
#              -> callback?code&state -> exchange with the verifier -> proof.
#              Exactly one installation is linked per flow: the targeted one,
#              the single eligible one, or one the user picks from a
#              short-lived server-side chooser.
#
# The old path (PUT with a bare installation_id inside a 15-minute window)
# proved nothing and doubled as an enumeration oracle. It now returns one
# constant response.

GITHUB_CLAIM_COOKIE = "gh_claim"
GITHUB_CLAIM_COOKIE_PATH = "/api/v1/settings/github/oauth"
GITHUB_CLAIM_TTL_MINUTES = 10
# A claim attempt parked in 'processing' (consumed, callback still working)
# is never cleaned up while it is younger than this — a slow GitHub round
# must not lose its single-use row mid-flight.
GITHUB_CLAIM_PROCESSING_GRACE_MINUTES = 10
# Installations evaluated (each may need a live membership call) when no
# installation is targeted.
_MAX_TARGETLESS_CANDIDATES = 25

# App-layer limits (per process). Keys are authenticated users or validated
# attempts, never a client address (behind a proxy every client shares one).
# The durable multi-replica limit belongs at the edge (Cloud Armor rule on
# /api/v1/settings/github/oauth/*).
_claim_start_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=3600)
_claim_callback_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=3600)
_claim_choose_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=3600)
_legacy_claim_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=3600)

_CLAIM_DISABLED = {
    "code": "github_claim_disabled",
    "message": (
        "Linking a GitHub App installation is disabled on this server: GitHub "
        "OAuth is not configured (SFS_GITHUB_APP_CLIENT_ID, "
        "SFS_GITHUB_APP_CLIENT_SECRET, an https SFS_DASHBOARD_URL and an https "
        "SFS_GITHUB_OAUTH_CALLBACK_URL ending in /api/v1/settings/github/oauth/callback "
        "are required)."
    ),
}
_CLAIM_REQUIRES_OAUTH = {
    "code": "github_claim_requires_oauth",
    "message": (
        "Installations can no longer be linked by id. Use Connect GitHub in "
        "the dashboard, which confirms with GitHub that you installed the App."
    ),
}
_INSTALL_FLOW_DISABLED = {
    "code": "github_install_flow_disabled",
    "message": "The install flow is not configured on this server (SFS_GITHUB_APP_SLUG).",
}
_CLAIM_RATE_LIMITED = {
    "code": "github_claim_rate_limited",
    "message": "Too many GitHub link attempts. Try again later.",
}
_CHOICE_FAILED = {
    "code": "github_claim_failed",
    "message": "That installation could not be linked. Start again from Connect GitHub.",
}


class GitHubInstallationResponse(BaseModel):
    installation_id: int | None = None
    account_login: str | None
    account_type: str | None
    auto_comment: bool
    include_trust_score: bool
    include_session_links: bool
    # Ownership proof state. A linked installation with ownership_verified
    # False is a legacy claim: it still drives PR comments, but it is not
    # trusted for repo or publisher verification until re-verified.
    ownership_verified: bool = False
    needs_reverification: bool = False
    claim_verified_github_login: str | None = None
    claim_role: str | None = None
    # True when the claim currently grants installation-wide authority
    # (repo verification): a live account-owner / org-admin proof younger
    # than CLAIM_PROOF_MAX_AGE.
    repo_verification_active: bool = False
    # A privileged proof that has aged out; re-verify to restore it.
    proof_stale: bool = False
    # Which OAuth flows this server can run.
    claim_available: bool = False
    install_available: bool = False


class GitHubInstallationUpdate(BaseModel):
    auto_comment: bool | None = None
    include_trust_score: bool | None = None
    include_session_links: bool | None = None
    # Accepted for backwards compatibility with older dashboards only. A
    # bare id is never enough to link an installation; see the OAuth flow.
    installation_id: int | None = None


class GitHubClaimStartRequest(BaseModel):
    flow: Literal["install", "authorize"] = "authorize"
    installation_id: StrictInt | None = Field(default=None, gt=0, lt=2**63)


class GitHubClaimStartResponse(BaseModel):
    redirect_url: str


class GitHubClaimChoice(BaseModel):
    installation_id: int
    account_login: str | None
    account_type: str | None


class GitHubClaimChoicesResponse(BaseModel):
    choices: list[GitHubClaimChoice]


class GitHubClaimChooseRequest(BaseModel):
    installation_id: StrictInt = Field(gt=0, lt=2**63)


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_proof_timestamp(raw: object) -> datetime | None:
    """Parse a parked chooser proof timestamp; None on missing/malformed.

    Missing (a chooser parked by an older server) falls back to bind time
    in ``_bind_installation``, preserving the previous behaviour.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _installation_response(
    inst: GitHubInstallation | None,
    config: github_oauth.GitHubOAuthConfig | None,
    *,
    defaults: GitHubInstallationUpdate | None = None,
) -> GitHubInstallationResponse:
    claim_available = config is not None
    install_available = config is not None and bool(config.app_slug)
    if inst is None:
        d = defaults or GitHubInstallationUpdate()
        return GitHubInstallationResponse(
            account_login=None,
            account_type=None,
            auto_comment=d.auto_comment if d.auto_comment is not None else True,
            include_trust_score=d.include_trust_score if d.include_trust_score is not None else True,
            include_session_links=d.include_session_links if d.include_session_links is not None else True,
            claim_available=claim_available,
            install_available=install_available,
        )
    verified = inst.claim_verified_at is not None
    privileged = verified and inst.claim_role in github_oauth.PRIVILEGED_ROLES
    fresh = verified and (
        _as_aware(inst.claim_verified_at)  # type: ignore[arg-type]
        >= datetime.now(timezone.utc) - github_oauth.CLAIM_PROOF_MAX_AGE
    )
    return GitHubInstallationResponse(
        installation_id=inst.id,
        account_login=inst.account_login,
        account_type=inst.account_type,
        auto_comment=inst.auto_comment,
        include_trust_score=inst.include_trust_score,
        include_session_links=inst.include_session_links,
        ownership_verified=verified,
        needs_reverification=not verified,
        claim_verified_github_login=inst.claim_verified_github_login if verified else None,
        claim_role=inst.claim_role if verified else None,
        repo_verification_active=privileged and fresh,
        proof_stale=privileged and not fresh,
        claim_available=claim_available,
        install_available=install_available,
    )


async def _current_installation(db: AsyncSession, user_id: str) -> GitHubInstallation | None:
    """The user's installation shown in settings: most recently verified first."""
    result = await db.execute(
        select(GitHubInstallation)
        .where(GitHubInstallation.user_id == user_id)
        .order_by(
            GitHubInstallation.claim_verified_at.is_(None),
            GitHubInstallation.claim_verified_at.desc(),
            GitHubInstallation.created_at.desc(),
            GitHubInstallation.id,
        )
        .limit(1)
    )
    return result.scalars().first()


@router.get("/github", response_model=GitHubInstallationResponse)
async def get_github_installation(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationResponse:
    """Return GitHub installation settings for the current user."""
    inst = await _current_installation(db, user.id)
    return _installation_response(inst, github_oauth.load_config())


@router.put("/github", response_model=GitHubInstallationResponse)
async def update_github_installation(
    body: GitHubInstallationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationResponse:
    """Update preferences on the user's linked installation.

    Never links an installation. A request carrying installation_id from a
    user with nothing linked gets ONE constant response whatever the id is
    (unknown, claimed by someone else, expired, or unclaimed), so the route
    cannot be used to probe installation ids.
    """
    config = github_oauth.load_config()
    inst = await _current_installation(db, user.id)

    if inst is None:
        if body.installation_id is not None:
            if not _legacy_claim_limiter.is_allowed(f"user:{user.id}"):
                raise HTTPException(429, detail=_CLAIM_RATE_LIMITED)
            raise HTTPException(
                409, detail=_CLAIM_REQUIRES_OAUTH if config is not None else _CLAIM_DISABLED
            )
        return _installation_response(None, config, defaults=body)

    values: dict = {}
    if body.auto_comment is not None:
        values["auto_comment"] = body.auto_comment
    if body.include_trust_score is not None:
        values["include_trust_score"] = body.include_trust_score
    if body.include_session_links is not None:
        values["include_session_links"] = body.include_session_links

    if values:
        # Conditional UPDATE: mutate the row only while it is still linked
        # to this user. A concurrent detach/rebind (or the OAuth claim flow
        # re-linking to another user) changes user_id, so the rowcount drops
        # to 0 and we 409 instead of mutating someone else's installation.
        result = await db.execute(
            update(GitHubInstallation)
            .where(
                GitHubInstallation.id == inst.id,
                GitHubInstallation.user_id == user.id,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "installation_changed",
                    "message": (
                        "Your GitHub installation changed while the update "
                        "was in flight. Reload and try again."
                    ),
                },
            )
        # Opting out fences pending comment work in the same transaction
        # (tk_b6c5e184ba8d4aa1): behind rows are marked published and their
        # leases dropped, so the sweeper and any in-flight publisher stop
        # instead of writing after the flip. Only fence after the update
        # won — fencing a row we no longer own would silence another
        # user's comments.
        if values.get("auto_comment") is False:
            from sessionfs.server.routes.webhooks import (
                cancel_pending_comment_work,
            )

            await cancel_pending_comment_work(db, inst.id)

    await db.commit()
    await db.refresh(inst)
    return _installation_response(inst, config)


def _pkce_challenge(code_verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )


async def _create_attempt(
    db: AsyncSession, *, user_id: str, flow: str, target: int | None
) -> tuple[str, str, str]:
    """Commit a pending attempt; return (state, nonce, code_verifier)."""
    now = datetime.now(timezone.utc)
    # Housekeeping: drop this user's expired or finished attempts. A
    # 'processing' attempt (consumed, callback still working) survives
    # until its grace has passed — deleting it mid-callback would strand
    # the flow it is single-use for.
    processing_grace = now - timedelta(minutes=GITHUB_CLAIM_PROCESSING_GRACE_MINUTES)
    await db.execute(
        delete(GitHubClaimAttempt).where(
            GitHubClaimAttempt.user_id == user_id,
            or_(
                # A 'processing' attempt is single-use work in flight: its
                # expiry alone must not delete it while it is inside its
                # grace (the callback may still be finishing).
                and_(
                    GitHubClaimAttempt.status != "processing",
                    GitHubClaimAttempt.expires_at < now,
                ),
                GitHubClaimAttempt.status == "consumed",
                and_(
                    GitHubClaimAttempt.status == "processing",
                    func.coalesce(
                        GitHubClaimAttempt.consumed_at,
                        GitHubClaimAttempt.created_at,
                    ) < processing_grace,
                ),
            ),
        )
        .execution_options(synchronize_session=False)
    )
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    db.add(
        GitHubClaimAttempt(
            id=f"gca_{secrets.token_hex(12)}",
            state_hash=_sha256_hex(state),
            user_id=user_id,
            nonce_hash=_sha256_hex(nonce),
            pkce_verifier_hash=_sha256_hex(code_verifier),
            flow=flow,
            target_installation_id=target,
            status="pending",
            created_at=now,
            expires_at=now + timedelta(minutes=GITHUB_CLAIM_TTL_MINUTES),
        )
    )
    await db.commit()
    return state, nonce, code_verifier


def _set_claim_cookie(
    response: Response,
    config: github_oauth.GitHubOAuthConfig,
    *,
    state: str,
    nonce: str,
    code_verifier: str,
) -> None:
    response.set_cookie(
        key=GITHUB_CLAIM_COOKIE,
        value=json.dumps({"state": state, "nonce": nonce, "code_verifier": code_verifier}),
        httponly=True,
        secure=config.secure_cookie,
        samesite="lax",
        max_age=GITHUB_CLAIM_TTL_MINUTES * 60,
        path=GITHUB_CLAIM_COOKIE_PATH,
    )


@router.post("/github/oauth/start", response_model=GitHubClaimStartResponse)
async def start_github_claim(
    body: GitHubClaimStartRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubClaimStartResponse:
    """Begin linking a GitHub App installation via GitHub OAuth.

    Commits a single-use attempt bound to the authenticated SessionFS user,
    sets an HttpOnly+SameSite=Lax cookie (Secure unless in the explicit
    localhost development mode) holding the browser nonce and PKCE verifier,
    and returns the GitHub URL to send the browser to. The installation id
    (if given) is only remembered, never looked up, so this route reveals
    nothing about installations.
    """
    config = github_oauth.load_config()
    if config is None:
        raise HTTPException(503, detail=_CLAIM_DISABLED)
    if body.flow == "install" and not config.app_slug:
        raise HTTPException(503, detail=_INSTALL_FLOW_DISABLED)
    if not _claim_start_limiter.is_allowed(f"user:{user.id}"):
        raise HTTPException(429, detail=_CLAIM_RATE_LIMITED)

    state, nonce, code_verifier = await _create_attempt(
        db, user_id=user.id, flow=body.flow, target=body.installation_id
    )
    _set_claim_cookie(response, config, state=state, nonce=nonce, code_verifier=code_verifier)

    if body.flow == "install":
        url = github_oauth.build_install_url(config, state=state)
    else:
        url = github_oauth.build_authorize_url(
            config, state=state, code_challenge=_pkce_challenge(code_verifier)
        )
    logger.info("github_claim_start user=%s flow=%s", user.id, body.flow)
    return GitHubClaimStartResponse(redirect_url=url)


def _claim_redirect(config: github_oauth.GitHubOAuthConfig, outcome: str) -> RedirectResponse:
    redirect = RedirectResponse(
        f"{config.dashboard_url}/settings?github={outcome}", status_code=302
    )
    redirect.delete_cookie(
        GITHUB_CLAIM_COOKIE,
        path=GITHUB_CLAIM_COOKIE_PATH,
        secure=config.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    return redirect


def _parse_installation_id(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise ValueError("bad installation_id")
    value = int(raw)
    if value <= 0:
        raise ValueError("bad installation_id")
    return value


async def _consume_claim_attempt(
    db: AsyncSession, state: str, *, flow: str
) -> GitHubClaimAttempt | None:
    """Atomically consume a pending attempt of ``flow`` (rowcount-1 guard).

    The row moves to 'processing', not 'consumed': the callback still has
    work to do (code exchange, installation listing, chooser), and cleanup
    must be able to tell an in-flight attempt apart from a finished one.
    'consumed_at' records when the work started."""
    now = datetime.now(timezone.utc)
    state_hash = _sha256_hex(state)
    result = await db.execute(
        update(GitHubClaimAttempt)
        .where(
            GitHubClaimAttempt.state_hash == state_hash,
            GitHubClaimAttempt.status == "pending",
            GitHubClaimAttempt.flow == flow,
            GitHubClaimAttempt.expires_at > now,
        )
        .values(status="processing", consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        return None
    row = await db.execute(
        select(GitHubClaimAttempt).where(GitHubClaimAttempt.state_hash == state_hash)
    )
    attempt = row.scalar_one_or_none()
    # Commit the consumption now so the attempt stays single-use even if a
    # later step fails and rolls back.
    await db.commit()
    return attempt


async def _ensure_installation_row(
    db: AsyncSession, info: github_oauth.UserInstallation
) -> None:
    """Create the row if the installation webhook has not arrived yet.

    The data comes from GitHub itself (user-token API response). A later
    signed installation webhook fills in its own metadata.
    """
    existing = await db.get(GitHubInstallation, info.id)
    if existing is not None:
        return
    db.add(
        GitHubInstallation(
            id=info.id,
            user_id=None,
            account_login=info.account_login,
            account_type=info.account_type,
            account_id=info.account_id,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()


def _takeover_allowed_clause(role: str):
    """Rows another user holds that a PRIVILEGED claimant may take over:
    legacy (never proven), member/installer-level, or a privileged proof
    older than CLAIM_PROOF_MAX_AGE (the holder may have been demoted)."""
    stale_before = datetime.now(timezone.utc) - github_oauth.CLAIM_PROOF_MAX_AGE
    if role not in github_oauth.PRIVILEGED_ROLES:
        return false()
    return or_(
        GitHubInstallation.claim_verified_at.is_(None),
        GitHubInstallation.claim_role.is_(None),
        GitHubInstallation.claim_role.notin_(sorted(github_oauth.PRIVILEGED_ROLES)),
        GitHubInstallation.claim_verified_at < stale_before,
    )


def _eligible(row: GitHubInstallation | None, user_id: str, role: str) -> bool:
    """Python mirror of the conditional UPDATE in _bind_installation."""
    if row is None or row.user_id is None or row.user_id == user_id:
        return True
    if role not in github_oauth.PRIVILEGED_ROLES:
        return False
    if row.claim_verified_at is None or row.claim_role not in github_oauth.PRIVILEGED_ROLES:
        return True
    return _as_aware(row.claim_verified_at) < (
        datetime.now(timezone.utc) - github_oauth.CLAIM_PROOF_MAX_AGE
    )


async def _bind_installation(
    db: AsyncSession,
    *,
    user_id: str,
    gh_user: github_oauth.GitHubUser,
    info: github_oauth.UserInstallation,
    role: str,
    proof_verified_at: datetime | None = None,
) -> bool:
    """Bind (or re-verify) one installation to ``user``. Returns success.

    Conditional UPDATE so concurrent claims cannot both win. Allowed when the
    row is unclaimed or already this user's; a privileged claimant may also
    take over a legacy, member-level or stale claim (see
    _takeover_allowed_clause).

    ``proof_verified_at`` is when GitHub last confirmed the claimant's role.
    It defaults to bind time (the direct callback path binds seconds after
    ``determine_role``); the chooser path passes the callback-time proof
    timestamp it parked, so a privileged role that sat up to the chooser TTL
    is not stamped as a fresh proof at choose time.
    """
    await _ensure_installation_row(db, info)
    now = datetime.now(timezone.utc)
    allowed = or_(
        GitHubInstallation.user_id.is_(None),
        GitHubInstallation.user_id == user_id,
        _takeover_allowed_clause(role),
    )
    # Serialize this user's link changes: lock the user row (PostgreSQL
    # FOR UPDATE; SQLite serializes writers anyway) before touching any
    # installation, so two concurrent links cannot both keep a row. The
    # partial unique index uq_github_installations_user is the backstop.
    # The user is reloaded and re-checked under the lock: the GitHub calls
    # that produced this proof ran with no transaction open.
    locked_user = (
        await db.execute(
            select(User.id, User.is_active).where(User.id == user_id).with_for_update()
        )
    ).first()
    if locked_user is None or not locked_user.is_active:
        await db.rollback()
        return False
    # Lock the installation row before reading the current holder. The
    # conditional UPDATE below is evaluated by the database against the
    # row's live state, but the takeover side-effects (auto_comment flip
    # and the pending-comment fence) are gated on this read — an unlocked
    # read could observe a stale holder and skip the fence on a takeover
    # that the UPDATE then wins (TOCTOU). FOR UPDATE serializes concurrent
    # claims on the same installation: the second claimant blocks until the
    # first commits and then reads the committed holder, so the fence always
    # runs exactly when ownership actually moves.
    previous = await db.execute(
        select(GitHubInstallation.user_id)
        .where(GitHubInstallation.id == info.id)
        .with_for_update()
    )
    previous_user_id = previous.scalar_one_or_none()

    # One linked installation per user: detach any other installation this
    # user held FIRST (the unique index forbids two), and stop its PR
    # comments. The dashboard manages exactly one installation, so a second
    # link must not leave the first active but unmanageable. Rolled back
    # together with the bind if the bind does not win. Pending comment work
    # for each detached installation is fenced in the same transaction.
    detach_ids = list((await db.execute(
        select(GitHubInstallation.id).where(
            GitHubInstallation.user_id == user_id,
            GitHubInstallation.id != info.id,
        )
    )).scalars().all())
    detached = await db.execute(
        update(GitHubInstallation)
        .where(GitHubInstallation.user_id == user_id, GitHubInstallation.id != info.id)
        .values(
            user_id=None,
            auto_comment=False,
            claim_verified_at=None,
            claim_verified_github_user_id=None,
            claim_verified_github_login=None,
            claim_role=None,
        )
        .execution_options(synchronize_session=False)
    )
    if detach_ids:
        from sessionfs.server.routes.webhooks import (
            cancel_pending_comment_work,
        )

        for old_installation_id in detach_ids:
            await cancel_pending_comment_work(db, old_installation_id)

    values: dict = {
        "user_id": user_id,
        "claim_verified_at": proof_verified_at if proof_verified_at is not None else now,
        "claim_verified_github_user_id": gh_user.id,
        "claim_verified_github_login": gh_user.login,
        "claim_role": role,
    }
    if info.account_login:
        values["account_login"] = info.account_login
    if info.account_type:
        values["account_type"] = info.account_type
    if info.account_id is not None:
        values["account_id"] = info.account_id
    if previous_user_id is not None and previous_user_id != user_id:
        # Takeover: ownership is moving to a different user. The previous
        # holder's sessions are no longer what the installation scopes, so
        # its PR comments must not keep publishing on the new owner's
        # behalf. Flip auto_comment off here (mirroring the detach branch)
        # instead of relying on cancel_pending_comment_work alone, which
        # only fences work already in flight.
        values["auto_comment"] = False

    try:
        result = await db.execute(
            update(GitHubInstallation)
            .where(GitHubInstallation.id == info.id, allowed)
            .values(**values)
            # The WHERE is evaluated by the database only (SQLite hands back
            # naive datetimes the in-Python evaluator cannot compare).
            .execution_options(synchronize_session=False)
        )
    except IntegrityError:
        await db.rollback()
        return False
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        return False
    if previous_user_id is not None and previous_user_id != user_id:
        # Takeover: the previous holder's pending comment work for THIS
        # installation must not publish after ownership moved to a new user
        # (its sessions are no longer what the installation scopes).
        from sessionfs.server.routes.webhooks import (
            cancel_pending_comment_work,
        )

        await cancel_pending_comment_work(db, info.id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    if detached.rowcount:  # type: ignore[attr-defined]
        logger.info(
            "github_claim_detached_previous user=%s count=%s new_installation=%s",
            user_id, detached.rowcount, info.id,  # type: ignore[attr-defined]
        )
    if previous_user_id is not None and previous_user_id != user_id:
        logger.warning(
            "github_claim_displaced installation=%s previous_user=%s new_user=%s role=%s",
            info.id, previous_user_id, user_id, role,
        )
    logger.info(
        "github_claim_verified installation=%s user=%s github_user=%s role=%s",
        info.id, user_id, gh_user.id, role,
    )
    return True


async def _role_for(
    db: AsyncSession, token: str, gh_user: github_oauth.GitHubUser, info: github_oauth.UserInstallation
) -> tuple[str, GitHubInstallation | None]:
    # Short read, committed BEFORE the membership call to GitHub.
    row = await db.get(GitHubInstallation, info.id, populate_existing=True)
    await db.commit()
    role = await github_oauth.determine_role(
        token, gh_user, info,
        installer_github_user_id=row.installer_github_user_id if row else None,
    )
    return role, row


@dataclass
class _ValidatedAttempt:
    attempt: GitHubClaimAttempt
    user: User
    code_verifier: str
    callback_installation_id: int | None


async def _validate_return(
    db: AsyncSession,
    *,
    flow: str,
    state: str | None,
    installation_id: str | None,
    gh_claim: str | None,
) -> _ValidatedAttempt | str:
    """Shared browser-binding checks for both GitHub return endpoints.

    Returns the validated attempt, or a failure reason (for the log only).
    Cheap checks come first: nothing touches GitHub until a valid,
    browser-bound attempt OF THE EXPECTED FLOW has been consumed.
    """
    if not state:
        return "missing_state"
    if not gh_claim:
        return "missing_cookie"
    try:
        cookie = json.loads(gh_claim)
    except (TypeError, ValueError):
        return "bad_cookie"
    if not isinstance(cookie, dict):
        return "bad_cookie"
    cookie_state = cookie.get("state")
    cookie_nonce = cookie.get("nonce")
    code_verifier = cookie.get("code_verifier")
    if not (
        isinstance(cookie_state, str) and cookie_state
        and isinstance(cookie_nonce, str) and cookie_nonce
        and isinstance(code_verifier, str) and code_verifier
    ):
        return "bad_cookie"
    # The state in the URL must be the one THIS browser started with
    # (login-CSRF / session-fixation defense).
    if not hmac.compare_digest(cookie_state, state):
        return "state_browser_mismatch"
    try:
        callback_installation_id = _parse_installation_id(installation_id)
    except ValueError:
        return "bad_installation_id"

    attempt = await _consume_claim_attempt(db, state, flow=flow)
    if attempt is None:
        return "attempt_missing_used_or_wrong_flow"
    if not hmac.compare_digest(attempt.nonce_hash, _sha256_hex(cookie_nonce)):
        return "nonce_mismatch"
    if not hmac.compare_digest(attempt.pkce_verifier_hash, _sha256_hex(code_verifier)):
        return "pkce_mismatch"

    user = await db.get(User, attempt.user_id)
    if user is None or not user.is_active:
        return "user_inactive"
    if not _claim_callback_limiter.is_allowed(f"user:{user.id}"):
        return "rate_limited"
    return _ValidatedAttempt(attempt, user, code_verifier, callback_installation_id)


@router.get("/github/oauth/install-return", include_in_schema=False)
async def github_install_return(
    state: str | None = Query(default=None, max_length=256),
    # Declared only to detect (and refuse) it; never read or exchanged.
    code: str | None = Query(default=None, max_length=512),
    installation_id: str | None = Query(default=None, max_length=32),
    gh_claim: str | None = Cookie(default=None, alias=GITHUB_CLAIM_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    """Where GitHub returns the browser after an installation (Setup URL).

    This endpoint NEVER reads or exchanges an OAuth code: one issued on the
    install hop carries no PKCE challenge. It only learns which installation
    was just made, then starts a separate PKCE authorization whose
    redirect_uri is the dedicated /oauth/callback, so GitHub's redirect_uri
    binding also refuses any install-hop code replayed there.
    """
    config = github_oauth.load_config()
    if config is None:
        raise HTTPException(503, detail=_CLAIM_DISABLED)
    if code is not None:
        # GitHub only adds a code here when "Request user authorization
        # (OAuth) during installation" is ON. That code has no PKCE
        # challenge; refuse the flow outright so the misconfiguration is
        # noticed (the code is never exchanged either way).
        logger.error(
            "github_claim_failed reason=oauth_during_install_enabled: turn OFF "
            "'Request user authorization (OAuth) during installation' on the GitHub App"
        )
        return _claim_redirect(config, "failed")
    checked = await _validate_return(
        db, flow="install", state=state, installation_id=installation_id, gh_claim=gh_claim
    )
    if isinstance(checked, str):
        logger.info("github_claim_failed reason=%s", checked)
        return _claim_redirect(config, "failed")

    target = checked.callback_installation_id or checked.attempt.target_installation_id
    new_state, new_nonce, new_verifier = await _create_attempt(
        db, user_id=checked.user.id, flow="authorize", target=target
    )
    redirect = RedirectResponse(
        github_oauth.build_authorize_url(
            config, state=new_state, code_challenge=_pkce_challenge(new_verifier)
        ),
        status_code=302,
    )
    _set_claim_cookie(redirect, config, state=new_state, nonce=new_nonce, code_verifier=new_verifier)
    logger.info("github_claim_install_continue user=%s target=%s", checked.user.id, target)
    return redirect


@router.get("/github/oauth/callback", include_in_schema=False)
async def github_claim_callback(
    code: str | None = Query(default=None, max_length=512),
    state: str | None = Query(default=None, max_length=256),
    installation_id: str | None = Query(default=None, max_length=32),
    gh_claim: str | None = Cookie(default=None, alias=GITHUB_CLAIM_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    """PKCE authorization callback (the redirect_uri of every authorize).

    Only attempts started as 'authorize' are accepted here; install-flow
    attempts are consumed solely by /oauth/install-return. Every failure
    lands the browser on the same ``?github=failed`` page; the specific
    reason goes to the server log only.
    """
    config = github_oauth.load_config()
    if config is None:
        raise HTTPException(503, detail=_CLAIM_DISABLED)

    def fail(reason: str) -> RedirectResponse:
        logger.info("github_claim_failed reason=%s", reason)
        return _claim_redirect(config, "failed")

    checked = await _validate_return(
        db, flow="authorize", state=state, installation_id=installation_id, gh_claim=gh_claim
    )
    if isinstance(checked, str):
        return fail(checked)
    # Copy what we need and end the transaction: no database connection is
    # held while GitHub is called below (code exchange, /user, installation
    # pages, membership checks).
    attempt_id = checked.attempt.id
    target = checked.attempt.target_installation_id
    user_id = checked.user.id
    code_verifier = checked.code_verifier
    callback_installation_id = checked.callback_installation_id
    await db.commit()

    # flow == "authorize": only this PKCE-bound code proves anything.
    if not code:
        return fail("missing_code")
    if callback_installation_id is not None and callback_installation_id != target:
        return fail("installation_target_mismatch")

    try:
        token = await github_oauth.exchange_code(config, code=code, code_verifier=code_verifier)
        gh_user = await github_oauth.get_user(token)
        listed = await github_oauth.list_user_installations(token)
    except github_oauth.GitHubOAuthError as exc:
        return fail(f"github_error:{exc}")

    try:
        if target is not None:
            info = next((i for i in listed if i.id == target), None)
            if info is None:
                return fail("installation_not_listed_for_user")
            role, _row = await _role_for(db, token, gh_user, info)
            if await _bind_installation(db, user_id=user_id, gh_user=gh_user, info=info, role=role):
                return _claim_redirect(config, "connected")
            return fail("installation_held_by_another_user")

        # No target: find the installations this proof lets the user link.
        eligible: list[tuple[github_oauth.UserInstallation, str]] = []
        for info in listed[:_MAX_TARGETLESS_CANDIDATES]:
            role, row = await _role_for(db, token, gh_user, info)
            if _eligible(row, user_id, role):
                eligible.append((info, role))
    finally:
        del token

    if not eligible:
        return fail("nothing_linkable")
    if len(eligible) == 1:
        info, role = eligible[0]
        if await _bind_installation(db, user_id=user_id, gh_user=gh_user, info=info, role=role):
            return _claim_redirect(config, "connected")
        return fail("installation_held_by_another_user")

    # Several: link none yet. Park the proof (never the token) on the
    # attempt for a short time and let the user pick exactly one. The
    # role proofs were just obtained by calling GitHub, so ``now`` is the
    # proof timestamp and is carried through the chooser so choose-time
    # binding does not stamp a privileged role as fresher than it is.
    now = datetime.now(timezone.utc)
    choices = {
        "github_user": {"id": gh_user.id, "login": gh_user.login},
        "proof_verified_at": now.isoformat(),
        "items": [
            {
                "id": i.id,
                "account_id": i.account_id,
                "account_login": i.account_login,
                "account_type": i.account_type,
                "role": r,
            }
            for i, r in eligible
        ],
    }
    parked = await db.execute(
        update(GitHubClaimAttempt)
        .where(GitHubClaimAttempt.id == attempt_id, GitHubClaimAttempt.status == "processing")
        .values(
            status="choosing",
            choices_json=json.dumps(choices),
            expires_at=now + timedelta(minutes=GITHUB_CLAIM_TTL_MINUTES),
        )
        .execution_options(synchronize_session=False)
    )
    if parked.rowcount != 1:  # type: ignore[attr-defined]
        # The attempt was cleaned up or otherwise moved on while GitHub
        # was being called — never redirect into a chooser that does not
        # exist.
        await db.rollback()
        return fail("attempt_no_longer_processing")
    await db.commit()
    return _claim_redirect(config, "choose")


async def _open_chooser(db: AsyncSession, user_id: str) -> GitHubClaimAttempt | None:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(GitHubClaimAttempt)
        .where(
            GitHubClaimAttempt.user_id == user_id,
            GitHubClaimAttempt.status == "choosing",
            GitHubClaimAttempt.expires_at > now,
        )
        .order_by(GitHubClaimAttempt.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


@router.get("/github/oauth/choices", response_model=GitHubClaimChoicesResponse)
async def list_github_claim_choices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubClaimChoicesResponse:
    """Installations the user's latest GitHub proof lets them link."""
    attempt = await _open_chooser(db, user.id)
    if attempt is None or not attempt.choices_json:
        return GitHubClaimChoicesResponse(choices=[])
    items = json.loads(attempt.choices_json).get("items", [])
    return GitHubClaimChoicesResponse(
        choices=[
            GitHubClaimChoice(
                installation_id=item["id"],
                account_login=item.get("account_login"),
                account_type=item.get("account_type"),
            )
            for item in items
        ]
    )


@router.post("/github/oauth/choose", response_model=GitHubInstallationResponse)
async def choose_github_claim(
    body: GitHubClaimChooseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationResponse:
    """Link the one installation the user picked from the chooser."""
    config = github_oauth.load_config()
    if config is None:
        raise HTTPException(503, detail=_CLAIM_DISABLED)
    if not _claim_choose_limiter.is_allowed(f"user:{user.id}"):
        raise HTTPException(429, detail=_CLAIM_RATE_LIMITED)
    attempt = await _open_chooser(db, user.id)
    if attempt is None or not attempt.choices_json:
        raise HTTPException(409, detail=_CHOICE_FAILED)
    # Single use: exactly one choose call can close the chooser.
    now = datetime.now(timezone.utc)
    closed = await db.execute(
        update(GitHubClaimAttempt)
        .where(
            GitHubClaimAttempt.id == attempt.id,
            GitHubClaimAttempt.status == "choosing",
            GitHubClaimAttempt.expires_at > now,
        )
        .values(status="consumed", consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(409, detail=_CHOICE_FAILED)
    await db.commit()

    data = json.loads(attempt.choices_json)
    item = next((i for i in data.get("items", []) if i.get("id") == body.installation_id), None)
    if item is None:
        raise HTTPException(409, detail=_CHOICE_FAILED)
    gh = data["github_user"]
    gh_user = github_oauth.GitHubUser(id=int(gh["id"]), login=str(gh["login"]))
    info = github_oauth.UserInstallation(
        id=int(item["id"]),
        account_id=item.get("account_id"),
        account_login=item.get("account_login"),
        account_type=item.get("account_type"),
    )
    proof_verified_at = _parse_proof_timestamp(data.get("proof_verified_at"))
    if not await _bind_installation(
        db,
        user_id=user.id,
        gh_user=gh_user,
        info=info,
        role=item["role"],
        proof_verified_at=proof_verified_at,
    ):
        raise HTTPException(409, detail=_CHOICE_FAILED)
    inst = await db.get(GitHubInstallation, info.id, populate_existing=True)
    return _installation_response(inst, config)


# --- GitLab integration settings ---


class GitLabSettingsResponse(BaseModel):
    instance_url: str
    has_token: bool
    has_webhook_secret: bool = False
    # Only ever set in a PUT response, when the server generated the secret,
    # so the user can copy it into GitLab once. Never read back afterwards.
    webhook_secret: str | None = None


class GitLabSettingsRequest(BaseModel):
    instance_url: str = "https://gitlab.com"
    access_token: str
    webhook_secret: str | None = None


@router.get("/gitlab", response_model=GitLabSettingsResponse)
async def get_gitlab_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitLabSettingsResponse:
    """Return GitLab integration settings for the current user."""
    result = await db.execute(
        select(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        return GitLabSettingsResponse(
            instance_url="https://gitlab.com",
            has_token=False,
        )
    return GitLabSettingsResponse(
        instance_url=settings.instance_url,
        has_token=bool(settings.encrypted_access_token),
        has_webhook_secret=bool(settings.webhook_secret),
    )


@router.put("/gitlab", response_model=GitLabSettingsResponse)
async def update_gitlab_settings(
    body: GitLabSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitLabSettingsResponse:
    """Create or update GitLab integration settings."""
    import asyncio

    from sessionfs.server.gitlab_client import SsrfError, validate_gitlab_instance_url

    try:
        instance_url = await asyncio.to_thread(
            validate_gitlab_instance_url, body.instance_url
        )
    except SsrfError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_gitlab_instance_url",
                "message": f"GitLab instance URL rejected: {exc}",
            },
        ) from exc
    body.instance_url = instance_url
    import secrets as _secrets

    encrypted = encrypt_secret(body.access_token)

    result = await db.execute(
        select(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    settings = result.scalar_one_or_none()
    generated = None if body.webhook_secret else _secrets.token_hex(16)
    webhook_secret = body.webhook_secret or generated

    if settings is None:
        settings = GitLabSettings(
            user_id=user.id,
            instance_url=body.instance_url,
            encrypted_access_token=encrypted,
            webhook_secret=webhook_secret,
        )
        db.add(settings)
    else:
        settings.instance_url = body.instance_url
        settings.encrypted_access_token = encrypted
        if body.webhook_secret:
            settings.webhook_secret = body.webhook_secret
        else:
            generated = None  # kept the existing secret; nothing new to show

    await db.commit()
    await db.refresh(settings)

    return GitLabSettingsResponse(
        instance_url=settings.instance_url,
        has_token=True,
        has_webhook_secret=bool(settings.webhook_secret),
        webhook_secret=generated,
    )


@router.delete("/gitlab", status_code=204)
async def delete_gitlab_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove GitLab integration settings."""
    await db.execute(
        delete(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    await db.commit()
