"""GitLab CI ID token verification for marketplace trusted publishing.

GitLab CI issues an ID token per job through the ``id_tokens:`` keyword
(``aud`` chosen in .gitlab-ci.yml). The deprecated ``CI_JOB_JWT`` /
``CI_JOB_JWT_V2`` variables were removed in GitLab 17.0 and are not
supported.

Issuers come ONLY from the operator allowlist ``SFS_GITLAB_OIDC_ISSUERS``
(comma-separated https origins; default ``https://gitlab.com``; ``none``
disables GitLab). A token's ``iss`` is read unverified only to pick one of
those allowlisted values: a token naming any other issuer is refused before
any network request, so neither a token nor a trust row can make the server
fetch from an arbitrary host.

The JWKS URL is derived from the allowlisted issuer:
``{issuer}/oauth/discovery/keys`` (the ``jwks_uri`` GitLab advertises in
``/.well-known/openid-configuration``; it signs CI ID tokens with a key
listed there). It is fetched through the SSRF-guarded ``oidc_fetch`` path
(https only, public IPs only, no redirects, 2 MiB cap) and cached per issuer
with the same TTL, refresh cooldown, single flight, negative cache and
bounded stale-if-error as the GitHub JWKS.

Tests replace :func:`fetch_jwks` (or call :func:`set_jwks_for_tests`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import jwt

from sessionfs.server.services.oidc_fetch import oidc_fetch_json
from sessionfs.server.services.oidc_token import (
    GITHUB_ACTIONS_ISSUER,
    TokenValidationError,
    validate_gitlab_ci_token,
)

GITLAB_COM_ISSUER = "https://gitlab.com"
DEFAULT_ISSUERS: tuple[str, ...] = (GITLAB_COM_ISSUER,)
JWKS_PATH = "/oauth/discovery/keys"
MAX_ISSUERS = 20
_DISABLED = frozenset({"none", "off", "disabled"})

_CACHE_TTL_SECONDS = 600
_REFRESH_COOLDOWN_SECONDS = 60
_STALE_IF_ERROR_SECONDS = 3600


# ---------------------------------------------------------------------------
# Issuer allowlist
# ---------------------------------------------------------------------------


def normalize_issuer(value: str) -> str:
    """An issuer origin in the form GitLab puts in ``iss``: ``https://host``
    (port and a relative-URL-root path allowed), lowercase scheme and host,
    no trailing slash, no credentials, query or fragment. Raises ValueError."""
    text = (value or "").strip()
    if not text or len(text) > 255 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in text):
        raise ValueError(f"invalid GitLab issuer {value!r}")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError(f"GitLab issuer must be an https:// origin: {value!r}")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError(f"invalid GitLab issuer host: {value!r}")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError(f"GitLab issuer must not carry a query or fragment: {value!r}")
    path = parts.path.rstrip("/")
    if path and (
        "//" in path or any(seg in (".", "..") for seg in path.split("/")[1:]) or "\\" in path
    ):
        raise ValueError(f"invalid GitLab issuer path: {value!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid GitLab issuer port: {value!r}") from exc
    host = parts.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    issuer = f"https://{netloc}{path}"
    if issuer == GITHUB_ACTIONS_ISSUER:
        raise ValueError("the GitHub Actions issuer cannot be a GitLab issuer")
    return issuer


def parse_issuers(raw: str | None) -> tuple[str, ...]:
    """SFS_GITLAB_OIDC_ISSUERS: unset/empty = gitlab.com only; ``none``
    disables GitLab trusted publishing; otherwise the listed origins REPLACE
    the default (list https://gitlab.com too to keep it). Raises ValueError
    on any malformed entry (the server refuses to start)."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_ISSUERS
    if text.lower() in _DISABLED:
        return ()
    issuers: list[str] = []
    for entry in text.split(","):
        if not entry.strip():
            continue
        issuer = normalize_issuer(entry)
        if issuer not in issuers:
            issuers.append(issuer)
    if not issuers:
        return DEFAULT_ISSUERS
    if len(issuers) > MAX_ISSUERS:
        raise ValueError(f"at most {MAX_ISSUERS} GitLab issuers")
    return tuple(issuers)


def allowed_issuer(value: str | None, issuers: tuple[str, ...]) -> str | None:
    """The allowlisted issuer *value* names (after normalization), else None."""
    if not value:
        return None
    try:
        issuer = normalize_issuer(value)
    except ValueError:
        return None
    return issuer if issuer in issuers else None


def jwks_url(issuer: str) -> str:
    return f"{issuer}{JWKS_PATH}"


# ---------------------------------------------------------------------------
# Per-issuer JWKS cache (keys are only ever allowlisted issuers)
# ---------------------------------------------------------------------------


@dataclass
class _IssuerCache:
    jwks: dict | None = None
    fetched_at: float = 0.0
    last_attempt: float = 0.0
    # None until a fetch has actually failed (see github_oidc).
    last_failure: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_caches: dict[str, _IssuerCache] = {}


def _cache_for(issuer: str) -> _IssuerCache:
    cache = _caches.get(issuer)
    if cache is None:
        cache = _caches[issuer] = _IssuerCache()
    return cache


async def fetch_jwks(issuer: str) -> dict:
    return await oidc_fetch_json(jwks_url(issuer), timeout=10.0)


def set_jwks_for_tests(issuer: str | None = None, jwks: dict | None = None) -> None:
    """Seed one issuer's cache, or (``issuer`` None) clear every cache.
    Each test runs on its own loop, so locks are re-created."""
    if issuer is None:
        _caches.clear()
        return
    _caches[issuer] = _IssuerCache(
        jwks=jwks, fetched_at=time.monotonic() if jwks is not None else 0.0
    )


def _serveable(cache: _IssuerCache, now: float, force: bool) -> dict | None:
    if cache.jwks is None:
        return None
    age = now - cache.fetched_at
    if age < _CACHE_TTL_SECONDS and not force:
        return cache.jwks
    if (
        age < _CACHE_TTL_SECONDS + _STALE_IF_ERROR_SECONDS
        and now - cache.last_attempt < _REFRESH_COOLDOWN_SECONDS
    ):
        return cache.jwks
    return None


def _stale_fallback(cache: _IssuerCache, now: float) -> dict | None:
    if cache.jwks is not None and now - cache.fetched_at < _CACHE_TTL_SECONDS + _STALE_IF_ERROR_SECONDS:
        return cache.jwks
    return None


def _unavailable(detail: str = "could not fetch the GitLab JWKS") -> TokenValidationError:
    return TokenValidationError("jwks_unavailable", detail)


async def _jwks(issuer: str, force: bool = False) -> dict:
    cache = _cache_for(issuer)
    now = time.monotonic()
    serveable = _serveable(cache, now, force)
    if serveable is not None:
        return serveable
    if cache.last_failure is not None and now - cache.last_failure < _REFRESH_COOLDOWN_SECONDS:
        raise _unavailable()
    async with cache.lock:
        now = time.monotonic()
        serveable = _serveable(cache, now, force)
        if serveable is not None:
            return serveable
        if cache.last_failure is not None and now - cache.last_failure < _REFRESH_COOLDOWN_SECONDS:
            raise _unavailable()
        cache.last_attempt = now
        try:
            jwks = await fetch_jwks(issuer)
        except Exception as exc:
            cache.last_failure = now
            fallback = _stale_fallback(cache, now)
            if fallback is not None:
                return fallback
            raise _unavailable() from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            cache.last_failure = now
            fallback = _stale_fallback(cache, now)
            if fallback is not None:
                return fallback
            raise _unavailable("GitLab JWKS response is malformed")
        cache.jwks = jwks
        cache.fetched_at = now
        cache.last_failure = None
        return jwks


def _kid_known(jwks: dict, kid: Any) -> bool:
    return any(isinstance(key, dict) and key.get("kid") == kid for key in jwks.get("keys", []))


async def verify_gitlab_ci_token(
    token: str, audience: str | None, issuers: tuple[str, ...]
) -> dict:
    """Verify a GitLab CI ID token against the resolved audience and the
    issuer allowlist; returns the claims or raises TokenValidationError."""
    if audience is None or not issuers:
        raise TokenValidationError("not_configured", "GitLab trusted publishing is not configured")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise TokenValidationError("token_parse_error", "token missing or too large")
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:
        raise TokenValidationError("token_parse_error", "malformed token") from exc
    claimed = unverified.get("iss") if isinstance(unverified, dict) else None
    # Exact match against the allowlist: no normalization of the token's
    # value, so the verified iss is byte-identical to an allowlisted origin.
    if not isinstance(claimed, str) or claimed not in issuers:
        error = TokenValidationError("issuer_not_allowed", "token issuer is not an allowed GitLab instance")
        near = allowed_issuer(claimed, issuers) if isinstance(claimed, str) else None
        if near is not None:
            # Same instance written differently (case, :443, trailing slash).
            # Naming the allowlisted form reveals nothing beyond the public
            # /trusted-publishing/config, and nothing is fetched.
            error.public_hint = (  # type: ignore[attr-defined]
                f"The token's iss must equal the allowed issuer {near!r} exactly. Set GitLab's "
                "external_url to that form (lowercase host, no :443, no trailing slash), or ask "
                "the operator to list the exact iss value in SFS_GITLAB_OIDC_ISSUERS."
            )
        raise error
    kid = header.get("kid")
    jwks = await _jwks(claimed)
    if not _kid_known(jwks, kid):
        jwks = await _jwks(claimed, force=True)
    return validate_gitlab_ci_token(token, jwks, issuer=claimed, audience=audience)
