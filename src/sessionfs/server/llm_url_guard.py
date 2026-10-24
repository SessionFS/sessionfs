"""SSRF policy for user-supplied LLM endpoints (judge / summaries / knowledge).

Users may point the server at their own OpenAI-compatible endpoint
(``base_url`` in judge settings, or per request on audit / narrative
summaries). The API then makes server-side requests to that host, so the URL
gets the same policy as the GitLab integration:

* Hosted mode (default): https only, every resolved address public
  (no loopback / private / link-local / CGNAT / unspecified / metadata).
* Self-hosted mode (``SFS_LLM_ALLOW_PRIVATE_HOSTS``): http or https, private
  and loopback hosts allowed (an in-cluster Ollama / vLLM); link-local,
  unspecified and cloud-metadata addresses stay blocked.
* Credentials or a fragment in the URL are always refused.

The check runs when a URL is saved or submitted AND right before every
outbound request (``guard_llm_base_url`` is installed into
``sessionfs.judge.providers`` by ``create_app``), so a hostname that starts
resolving to an internal address after it was saved is refused. Requests
never follow redirects.

The deployment-wide ``SFS_JUDGE_BASE_URL`` is operator configuration, not
user input: that exact URL is checked in self-hosted mode (metadata still
blocked) regardless of the flag.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

from sessionfs.server.config import ServerConfig
from sessionfs.server.services.oidc_fetch import (
    _METADATA_IPS,
    SsrfError,
    _validate_url,
)

__all__ = [
    "SsrfError",
    "guard_llm_base_url",
    "llm_allow_private_hosts",
    "validate_llm_base_url",
]


def llm_allow_private_hosts() -> bool:
    return ServerConfig().llm_allow_private_hosts


def _operator_base_url() -> str:
    return (os.environ.get("SFS_JUDGE_BASE_URL") or "").strip().rstrip("/")


def _resolve_any(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list({str(i[4][0]) for i in infos})


def validate_llm_base_url(url: str, *, allow_private: bool | None = None) -> str:
    """Validate an LLM endpoint URL (blocking DNS). Returns it without a
    trailing slash. Raises SsrfError."""
    url = (url or "").strip()
    if not url:
        raise SsrfError("The endpoint URL is empty")
    if len(url) > 500:
        raise SsrfError("The endpoint URL is too long")
    if allow_private is None:
        operator = _operator_base_url()
        allow_private = llm_allow_private_hosts() or (
            bool(operator) and url.rstrip("/") == operator
        )
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise SsrfError("Credentials in the endpoint URL are not allowed")
    if parsed.fragment:
        raise SsrfError("The endpoint URL must not contain a fragment")
    if not allow_private:
        _validate_url(url)  # https-only + public addresses
        return url.rstrip("/")
    if parsed.scheme not in ("http", "https"):
        raise SsrfError(f"Unsupported scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise SsrfError("Could not parse hostname from the endpoint URL")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i) for i in _resolve_any(host)]
        except OSError as exc:
            raise SsrfError(f"DNS resolution failed for {host}") from exc
    if not ips:
        raise SsrfError(f"No addresses resolved for {host}")
    for ip in ips:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip in _METADATA_IPS or ip.is_link_local or ip.is_unspecified:
            raise SsrfError(f"Host {host} resolves to a disallowed address")
        if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("0.0.0.0/8"):
            raise SsrfError(f"Host {host} resolves to a disallowed address")
    return url.rstrip("/")


async def guard_llm_base_url(url: str) -> None:
    """Async wrapper used right before each outbound request."""
    await asyncio.to_thread(validate_llm_base_url, url)
