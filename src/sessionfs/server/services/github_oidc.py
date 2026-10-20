"""GitHub Actions OIDC verification for marketplace trusted publishing.

The JWKS is fetched from the fixed GitHub endpoint through the SSRF-guarded
``oidc_fetch`` path (https only, public IPs only, no redirects, 2 MiB cap)
and cached in-process. A token whose ``kid`` is not in the cached set forces
at most one refresh per ``_REFRESH_COOLDOWN_SECONDS``, so a flood of tokens
with random ``kid`` values cannot turn this endpoint into a fetch amplifier.

Tests replace :func:`fetch_jwks` (or call :func:`set_jwks_for_tests`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import jwt

from sessionfs.server.services.oidc_fetch import oidc_fetch_json
from sessionfs.server.services.oidc_token import (
    GITHUB_ACTIONS_ISSUER,
    TokenValidationError,
    validate_github_actions_token,
)

GITHUB_JWKS_URL = f"{GITHUB_ACTIONS_ISSUER}/.well-known/jwks"
_CACHE_TTL_SECONDS = 600
_REFRESH_COOLDOWN_SECONDS = 60
# Stale-if-error: a cached JWKS may outlive its TTL by at most this much while
# GitHub is unreachable; after that, fail closed (a key GitHub removed must
# not stay trusted indefinitely during an outage).
_STALE_IF_ERROR_SECONDS = 3600

# last_failure is None until a fetch has actually failed: 0.0 would be
# indistinguishable from "failed at process start" and would negative-cache a
# cold JWKS for the whole cooldown window of the first minute of uptime.
_cache: dict[str, Any] = {"jwks": None, "fetched_at": 0.0, "last_attempt": 0.0, "last_failure": None}
# Single flight: concurrent exchanges queue on this lock and re-check the
# cache under it, so a cold start (or a GitHub outage with an expired cache)
# produces ONE outbound fetch per cooldown window, never one per exchange.
_refresh_lock = asyncio.Lock()


# Audiences that name no deployment: a token minted for one SessionFS server
# with one of these could be replayed against any other.
GENERIC_AUDIENCES = frozenset({"sessionfs", "sfs", "sessionfs.dev", "api", "default"})
DEVELOPMENT_AUDIENCE = "sessionfs-development"


_DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "test", "testing", "local"})


@dataclass(frozen=True)
class TrustedPublishingSettings:
    """The ONE resolved trusted-publishing configuration of a running server,
    used for the startup check, the public config response and token
    verification alike. ``audience`` None means trusted publishing is off."""

    audience: str | None
    development: bool
    source: str  # 'override' | 'public_api_url' | 'development' | 'unset'


def resolve_trusted_publishing(config: Any) -> TrustedPublishingSettings:
    """Resolve environment and audience from a ServerConfig (fields
    ``environment``, ``public_api_url``, ``trusted_publishing_audience``,
    ``database_url``; SFS_-prefixed environment variables).

    Development: SFS_ENVIRONMENT in development/dev/test/local, or (unset) a
    SQLite database. Audience: the explicit override, else the public API
    origin, else (development only) a development audience. Outside
    development the final audience is validated whatever its source: a
    generic value or the development audience raises RuntimeError (refuse to
    start), and a public API URL must be an https origin."""
    environment = str(getattr(config, "environment", "") or "").strip().lower()
    database_url = str(getattr(config, "database_url", "") or "")
    development = (
        environment in _DEVELOPMENT_ENVIRONMENTS if environment else database_url.startswith("sqlite")
    )
    explicit = str(getattr(config, "trusted_publishing_audience", "") or "").strip()
    origin = str(getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    if explicit:
        audience: str | None = explicit
        source = "override"
    elif origin:
        audience = origin
        source = "public_api_url"
    elif development:
        audience = DEVELOPMENT_AUDIENCE
        source = "development"
    else:
        audience = None
        source = "unset"
    if not development and audience is not None:
        if audience.lower() in GENERIC_AUDIENCES or audience == DEVELOPMENT_AUDIENCE:
            raise RuntimeError(
                f"The trusted publishing audience ({source}) is a generic value; set "
                "SFS_PUBLIC_API_URL to this deployment's public API origin (e.g. "
                "https://api.example.com) or SFS_TRUSTED_PUBLISHING_AUDIENCE to a unique value."
            )
        if source == "public_api_url" and not audience.lower().startswith("https://"):
            raise RuntimeError("SFS_PUBLIC_API_URL must be this deployment's https:// API origin.")
    return TrustedPublishingSettings(audience=audience, development=development, source=source)


async def fetch_jwks() -> dict:
    return await oidc_fetch_json(GITHUB_JWKS_URL, timeout=10.0)


def set_jwks_for_tests(jwks: dict | None) -> None:
    global _refresh_lock
    _cache["jwks"] = jwks
    _cache["fetched_at"] = time.monotonic() if jwks is not None else 0.0
    _cache["last_attempt"] = 0.0
    _cache["last_failure"] = None
    # A contended asyncio.Lock binds to the running loop; each test runs on
    # its own loop, so hand every test a fresh lock.
    _refresh_lock = asyncio.Lock()


def _serveable(now: float, force: bool) -> dict | None:
    """The cached JWKS when it may be served without a fetch: fresh (unless a
    refresh is forced), or stale-but-within the bounded stale-if-error window
    while a refresh is cooling down."""
    cached = _cache["jwks"]
    if cached is None:
        return None
    age = now - _cache["fetched_at"]
    if age < _CACHE_TTL_SECONDS and not force:
        return cached
    if (
        age < _CACHE_TTL_SECONDS + _STALE_IF_ERROR_SECONDS
        and now - _cache["last_attempt"] < _REFRESH_COOLDOWN_SECONDS
    ):
        return cached
    return None


def _stale_fallback(now: float) -> dict | None:
    """The expired cache, usable as an answer to a FAILED fetch only within
    the bounded stale-if-error window (a key GitHub removed must not stay
    trusted indefinitely during an outage)."""
    cached = _cache["jwks"]
    if cached is not None and now - _cache["fetched_at"] < _CACHE_TTL_SECONDS + _STALE_IF_ERROR_SECONDS:
        return cached
    return None


async def _jwks(force: bool = False) -> dict:
    now = time.monotonic()
    serveable = _serveable(now, force)
    if serveable is not None:
        return serveable
    if (
        _cache["last_failure"] is not None
        and now - _cache["last_failure"] < _REFRESH_COOLDOWN_SECONDS
    ):
        # Negative cache: the last fetch just failed and there is nothing
        # serveable — fail fast instead of piling another outbound request
        # onto an unreachable GitHub.
        raise TokenValidationError("jwks_unavailable", "could not fetch the GitHub JWKS")
    async with _refresh_lock:
        # Re-check under the lock: the exchange that held it may have just
        # refreshed the cache (or failed into the negative cache).
        now = time.monotonic()
        serveable = _serveable(now, force)
        if serveable is not None:
            return serveable
        if (
            _cache["last_failure"] is not None
            and now - _cache["last_failure"] < _REFRESH_COOLDOWN_SECONDS
        ):
            raise TokenValidationError("jwks_unavailable", "could not fetch the GitHub JWKS")
        _cache["last_attempt"] = now
        try:
            jwks = await fetch_jwks()
        except Exception as exc:
            _cache["last_failure"] = now
            fallback = _stale_fallback(now)
            if fallback is not None:
                return fallback
            raise TokenValidationError("jwks_unavailable", "could not fetch the GitHub JWKS") from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            _cache["last_failure"] = now
            fallback = _stale_fallback(now)
            if fallback is not None:
                return fallback
            raise TokenValidationError("jwks_unavailable", "GitHub JWKS response is malformed")
        _cache["jwks"] = jwks
        _cache["fetched_at"] = now
        _cache["last_failure"] = None
        return jwks


def _kid_known(jwks: dict, kid: str | None) -> bool:
    return any(isinstance(key, dict) and key.get("kid") == kid for key in jwks.get("keys", []))


async def verify_github_actions_token(token: str, audience: str | None) -> dict:
    """Verify signature and claims against the server's resolved audience;
    returns the claims or raises TokenValidationError."""
    if audience is None:
        raise TokenValidationError("not_configured", "trusted publishing audience is not configured")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise TokenValidationError("token_parse_error", "token missing or too large")
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise TokenValidationError("token_parse_error", "malformed token") from exc
    kid = header.get("kid")
    jwks = await _jwks()
    if not _kid_known(jwks, kid):
        jwks = await _jwks(force=True)
    return validate_github_actions_token(token, jwks, audience=audience)
