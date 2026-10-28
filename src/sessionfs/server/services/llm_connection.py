"""Server side of "Test connection" and the save-time Base URL check.

Both the personal judge settings (``routes/settings.py``) and the org helper
key (``routes/org_llm_settings.py``) use this module:

* ``run_test`` — the body of ``POST .../test``: SSRF guard first, then one
  minimal chat request (``judge.connection_check``).
* ``save_time_check`` — run on PUT when a custom Base URL is set and either
  the URL or the key changed. ``endpoint_not_api`` / ``invalid_key`` /
  ``endpoint_blocked`` refuse the save (422 ``{code, message}``); transient
  failures save anyway and come back as ``warnings``.
* ``saved_key_bound_to`` — the strict form of the saved-key binding: a stored
  key may only be sent to exactly the provider/endpoint it was saved with.

Outbound requests are rate limited per user per process (10/minute; the
durable multi-replica limit belongs at the edge).
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import logging
from urllib.parse import urlparse

from fastapi import HTTPException

from sessionfs.judge import llm_errors
from sessionfs.judge.connection_check import (
    ConnectionCheckResult,
    check_llm_connection,
    endpoint_host,
)
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter

logger = logging.getLogger("sessionfs.server.services.llm_connection")

TESTS_PER_MINUTE = 10
# Failures against a private target (self-hosted mode only) take at least
# this long, so timing can't tell a refused port from a filtered one or a
# fast 404 from a slow one. Tests patch it to 0.
PRIVATE_FAILURE_MIN_SECONDS = 15.0
_limiter = SlidingWindowRateLimiter(max_requests=TESTS_PER_MINUTE, window_seconds=60)
# Model discovery ("Fetch models", Base URL lookups) makes the same kind of
# outbound call, so it gets the same per-user limit in its own bucket: typing
# a Base URL must not use up the connection tests, nor the reverse.
_discovery_limiter = SlidingWindowRateLimiter(max_requests=TESTS_PER_MINUTE, window_seconds=60)

KEY_REQUIRED = {
    "code": "key_required",
    "message": "Enter the key to test a different provider or endpoint.",
}
RATE_LIMITED = {
    "code": "test_rate_limited",
    "message": "Too many connection tests. Wait a minute and try again.",
}
DISCOVERY_RATE_LIMITED = {
    "code": "models_rate_limited",
    "message": "Too many model lookups. Wait a minute and try again.",
}


def reset_rate_limiter() -> None:
    """Forget recorded tests (used by tests)."""
    _limiter._requests.clear()
    _discovery_limiter._requests.clear()


def take_test_slot(user_id: str) -> bool:
    return _limiter.is_allowed(user_id)


def take_discovery_slot(user_id: str) -> bool:
    return _discovery_limiter.is_allowed(user_id)


def normalise_base_url(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/")


def saved_key_bound_to(
    saved_provider: str | None,
    saved_base_url: str | None,
    *,
    provider: str | None,
    base_url: str | None,
) -> bool:
    """Whether a request for (provider, base_url) may use a stored key.

    Strict: the effective values must equal the saved ones. With a custom
    Base URL the endpoint is what routes the request, so the URLs must match;
    without one, the saved row must have no custom URL and the same provider.
    Unlike ``saved_judge_key_matches``, an empty ``base_url`` here means "no
    custom endpoint", never "fall back to the saved one".
    """
    saved_base = normalise_base_url(saved_base_url)
    requested_base = normalise_base_url(base_url)
    if requested_base or saved_base:
        return bool(requested_base) and requested_base == saved_base
    return (provider or "").strip().lower() == (saved_provider or "").strip().lower()


async def _guard(base_url: str) -> str | None:
    """Run the SSRF policy; returns the checked URL or None when blocked."""
    from sessionfs.server.llm_url_guard import SsrfError, validate_llm_base_url

    try:
        return await asyncio.to_thread(validate_llm_base_url, base_url)
    except SsrfError as exc:
        logger.warning("Connection test refused endpoint %s: %s", base_url[:200], exc)
        return None


def _resolves_private(base_url: str) -> bool:
    """Whether the endpoint's host resolves to a non-public address.

    Only reachable when private hosts are allowed (self-hosted mode). Such
    targets get coarse results — refused and silent ports both read as
    ``unreachable`` with no timing — so the test can't map an internal
    network.
    """
    from sessionfs.server.llm_url_guard import _resolve_any

    host = urlparse(base_url).hostname or ""
    if not host:
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a) for a in _resolve_any(host)]
        except (OSError, ValueError):
            return True
    for ip in addresses:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return True
    return False


def _coarsen_private(result: ConnectionCheckResult, *, key_sent: bool) -> ConnectionCheckResult:
    """Coarse failures for a private target (an internal-network probe guard).

    ``ok`` and ``invalid_key`` are kept (the operator needs them to set up a
    self-hosted model); timeouts, connect failures and "not an API" replies
    (HTML / 404 from some other internal service) all read ``unreachable``;
    no upstream ``detail`` snippet and no timing are returned.
    """
    if result.ok:
        return result
    code = result.code
    if code in (llm_errors.TIMEOUT, llm_errors.UNREACHABLE, llm_errors.ENDPOINT_NOT_API):
        code = llm_errors.UNREACHABLE
    message = llm_errors.friendly_message(
        code,
        provider=result.provider,
        model=result.model,
        host=result.endpoint_host,
        key_sent=key_sent,
        detail=None,
    )
    return dataclasses.replace(
        result, code=code, message=message, latency_ms=None, definitive=False
    )


async def _check(
    *, provider: str, model: str, api_key: str, base_url: str | None
) -> ConnectionCheckResult:
    private = bool(base_url) and await asyncio.to_thread(_resolves_private, base_url or "")
    started = asyncio.get_running_loop().time()
    result = await check_llm_connection(
        provider=provider, model=model, api_key=api_key, base_url=base_url
    )
    if not private or result.ok:
        return result
    remaining = PRIVATE_FAILURE_MIN_SECONDS - (asyncio.get_running_loop().time() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)
    return _coarsen_private(result, key_sent=bool(api_key))


def _blocked(provider: str, model: str, base_url: str) -> ConnectionCheckResult:
    return ConnectionCheckResult(
        ok=False,
        code=llm_errors.ENDPOINT_BLOCKED,
        message=llm_errors.friendly_message(llm_errors.ENDPOINT_BLOCKED),
        provider=provider,
        model=model,
        endpoint_host=endpoint_host(provider, base_url),
        latency_ms=None,
    )


async def run_test(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None,
) -> ConnectionCheckResult:
    """SSRF guard, then one minimal chat request."""
    checked: str | None = None
    if base_url:
        checked = await _guard(base_url)
        if checked is None:
            return _blocked(provider, model, base_url)
    return await _check(provider=provider, model=model, api_key=api_key, base_url=checked)


async def save_time_check(
    *,
    user_id: str,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    base_changed: bool = True,
) -> list[dict]:
    """Check a custom Base URL before it is saved.

    Raises 422 ``{code, message}`` for a configuration that can never work;
    returns ``warnings`` (possibly empty) otherwise. ``base_url`` has already
    passed the SSRF guard on the save path.

    Refused:
    * ``endpoint_blocked``;
    * ``invalid_key`` when certain (a 401, or a 400 that says the key is
      invalid), or when a 403 says the key is invalid AND the URL changed;
    * ``endpoint_not_api`` when the URL changed and the reply is definitive
      (405, a web page on 2xx/404/405, a known console host).
    Everything else — including ``access_denied`` (a 403 about region or
    model permissions) — saves with a warning, so a key rotation on an
    unchanged URL is never stopped by an outage or a permission quirk.
    """
    if not take_test_slot(user_id):
        return [
            {
                "code": "connection_not_checked",
                "message": (
                    "Saved without checking the connection (too many checks in the "
                    "last minute). Use Test connection to check it."
                ),
            }
        ]
    result = await _check(provider=provider, model=model, api_key=api_key, base_url=base_url)
    if result.ok:
        return []
    code = result.code
    blocking = (
        code == llm_errors.ENDPOINT_BLOCKED
        or (code == llm_errors.INVALID_KEY and (result.definitive or base_changed))
        or (base_changed and code == llm_errors.ENDPOINT_NOT_API and result.definitive)
    )
    if blocking:
        raise HTTPException(
            status_code=422,
            detail={"code": result.code, "message": result.message},
        )
    return [{"code": result.code, "message": result.message}]
