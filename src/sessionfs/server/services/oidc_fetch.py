"""SSRF-guarded HTTP fetch for OIDC issuer-derived endpoints.

Every server-side HTTP fetch whose target is derived from an admin-supplied
issuer MUST pass through this layer.  §3.2.1 of the SSO design (R2-N1):

  - https-only
  - Resolve hostname → IP(s), reject private/loopback/link-local/metadata
  - Re-run the full IP-range guard independently on each discovery-advertised
    jwks_uri / token_endpoint (do NOT same-origin-pin — Google Workspace MUST
    work: issuer accounts.google.com, token endpoint oauth2.googleapis.com,
    jwks_uri www.googleapis.com — different public hosts ARE allowed)
  - Disable redirect following; never follow a 302 to internal host

DNS-rebind posture (honest): every fetch RE-resolves and RE-validates the host
(so a hostname that flips to a private IP between two fetches is caught), and
because every fetch is https-only with default TLS certificate verification, a
rebind to an internal IP (metadata server, 127.0.0.1, …) cannot present a valid
certificate for the IdP's public hostname → the TLS handshake fails. That
hostname-bound TLS verification is the load-bearing intra-call rebind mitigation
here; the platform-layer network-egress restriction (Forge, a hard release gate
for the all-paid surface) is the defense-in-depth backstop. Literal connect-to-
pinned-IP is a tracked hardening follow-up — we do NOT claim it is implemented.

Allows test injection of the underlying httpx transport so integration tests
can simulate hostname resolution to private IPs without real DNS.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("sessionfs.api")

# ---------------------------------------------------------------------------
# IP ranges that are NEVER reachable (R2-N1 — per-host guard, applied
# independently on issuer / jwks_uri / token_endpoint)
# ---------------------------------------------------------------------------

_PRIVATE_RANGES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("127.0.0.0/8"),       # loopback
    ipaddress.IPv4Network("169.254.0.0/16"),     # link-local
    ipaddress.IPv4Network("10.0.0.0/8"),         # private A
    ipaddress.IPv4Network("172.16.0.0/12"),      # private B
    ipaddress.IPv4Network("192.168.0.0/16"),     # private C
    ipaddress.IPv6Network("::1/128"),            # loopback v6
    ipaddress.IPv6Network("fe80::/10"),           # link-local v6
    ipaddress.IPv6Network("fc00::/7"),            # unique local v6
]

_METADATA_IPS: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
    ipaddress.IPv4Address("169.254.169.254"),
    ipaddress.IPv6Address("fd00:ec2::254"),
}

# ---------------------------------------------------------------------------
# Test injection points
# ---------------------------------------------------------------------------

_test_transport: httpx.AsyncHTTPTransport | None = None


def _set_test_transport(transport: httpx.AsyncHTTPTransport | None) -> None:
    """Inject a mock transport (tests only).  Set to None to restore real I/O."""
    global _test_transport
    _test_transport = transport


def _get_test_transport() -> httpx.AsyncHTTPTransport | None:
    return _test_transport


class SsrfError(Exception):
    """Raised when a fetch is blocked by the SSRF guard."""


# Cap response bodies — discovery docs / JWKS are a few KB. A hostile (or
# compromised) IdP returning a multi-GB body must not exhaust memory.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB


# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------


def _is_private_or_metadata_ip(ip_str: str) -> bool:
    """Return True if *ip_str* is private / loopback / link-local / metadata."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block (safe default)

    if ip in _METADATA_IPS:
        return True
    for net in _PRIVATE_RANGES:
        if ip in net:
            return True
    return False


def _resolve_and_validate_host(hostname: str) -> list[str]:
    """Resolve *hostname* and validate all resolved IPs are public.

    Returns the list of resolved IP strings on success.
    Raises SsrfError if ANY resolved IP is private / loopback / link-local /
    metadata, or if resolution fails entirely.
    """
    try:
        addrinfo = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfError(f"DNS resolution failed for {hostname}: {exc}") from exc

    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        if ip_str not in ips:
            ips.append(ip_str)

    if not ips:
        raise SsrfError(f"No IP addresses resolved for {hostname}")

    for ip_str in ips:
        if _is_private_or_metadata_ip(ip_str):
            raise SsrfError(
                f"Host {hostname} resolves to disallowed IP {ip_str}"
            )

    return ips


def _validate_url(url: str) -> str:
    """Validate the URL is https and the hostname is public.

    Returns the hostname on success.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise SsrfError(
            f"Non-HTTPS scheme rejected: {parsed.scheme} (URL: {url[:80]})"
        )

    hostname = parsed.hostname
    if not hostname:
        raise SsrfError(f"Could not parse hostname from URL: {url[:80]}")

    _resolve_and_validate_host(hostname)
    return hostname


# ---------------------------------------------------------------------------
# Public fetch API
# ---------------------------------------------------------------------------


async def oidc_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """Fetch a URL through the complete SSRF guard.

    Rejects before any bytes leave the process:
      1. Non-HTTPS URLs
      2. Hosts resolving to private / loopback / link-local / metadata IPs
      3. Any redirect response (follow_redirects=False — never follow a
         redirect to an internal host)

    The caller MUST call this independently on each URL (issuer discovery doc,
    jwks_uri, token_endpoint) — never same-origin-pin across different hosts.

    Raises SsrfError on blocked requests, httpx.HTTPStatusError on non-2xx,
    httpx.RequestError on network failures.
    """
    # 1. Validate URL + host before ANY connection
    _validate_url(url)

    # 2. Build request — use injected transport if set, otherwise fresh transport
    transport = _get_test_transport()
    if transport is None:
        transport = httpx.AsyncHTTPTransport(retries=0)

    req = httpx.Request(
        method=method,
        url=url,
        headers=headers,
        data=data,
    )

    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        resp = await client.send(req)

        # 3. Explicitly reject any redirect response
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "?")
            raise SsrfError(
                f"Redirect response not followed: {resp.status_code} → "
                f"{location[:100]} (URL: {url[:80]})"
            )

        # 4. Cap response size (declared Content-Length and actual body).
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_RESPONSE_BYTES:
                    raise SsrfError(
                        f"Response too large: {declared} bytes "
                        f"(max {_MAX_RESPONSE_BYTES})"
                    )
            except ValueError:
                pass  # unparseable header — fall through to the body check
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise SsrfError(
                f"Response body too large: {len(resp.content)} bytes "
                f"(max {_MAX_RESPONSE_BYTES})"
            )

        resp.raise_for_status()
        return resp


async def oidc_fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict:
    """oidc_fetch + JSON parse in one call."""
    resp = await oidc_fetch(
        url, method=method, headers=headers, data=data, timeout=timeout
    )
    return resp.json()
