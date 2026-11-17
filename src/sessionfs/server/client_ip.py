"""The real client IP, trusting forwarding headers only from known proxies.

Every place the server needs the caller's address (per-IP rate limits, the
4xx request log, ``api_keys.last_used_ip``, Helm license validation rows)
goes through :func:`client_ip`. Nothing else may read ``X-Forwarded-For``,
``X-Real-IP``, ``CF-Connecting-IP`` or ``Forwarded``.

Rules:

* ``SFS_TRUSTED_PROXIES`` lists the proxies allowed to report a client
  address: CIDRs, bare addresses and the named presets in :data:`PRESETS`,
  comma-separated. Unset = no proxy is trusted.
* The direct TCP peer (``request.client.host``) is the starting point. When
  it is not a trusted proxy the headers are ignored and the peer is the
  client, whatever the request claims.
* When the peer is trusted, ``X-Forwarded-For`` is walked right to left
  (every header line, in order, joined) and the first address that is NOT a
  trusted proxy is the client. Each trusted proxy appends the address it saw,
  so everything to the left of the right-most untrusted entry was written by
  the client and can be forged.
* Only ``X-Forwarded-For`` is read. ``X-Real-IP``, ``CF-Connecting-IP`` and
  ``Forwarded`` are ignored: one header keeps one trust rule. Cloudflare
  (proxied DNS and Tunnel), the GCP load balancer, nginx and ingress-nginx
  all append to ``X-Forwarded-For``.
* A malformed entry stops the walk at the nearest trusted hop to its right
  (a trusted proxy never writes garbage, so the garbage came from further
  left). Every entry trusted: the left-most one is the client.
* IPv4-mapped IPv6 addresses (``::ffff:203.0.113.7``) are compared and
  returned as IPv4; ports, brackets and IPv6 zone ids are stripped.

The API image starts uvicorn with ``--no-proxy-headers`` so
``request.client`` is always the real TCP peer; this module is the only
place forwarding headers are interpreted.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Iterable

from starlette.requests import HTTPConnection

logger = logging.getLogger("sessionfs.api")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Named presets usable in SFS_TRUSTED_PROXIES. Keep these in step with
# docs/environment-variables.md and the self-hosted guide.
PRESETS: dict[str, tuple[str, ...]] = {
    # Loopback and private ranges: an in-cluster ingress controller, the Helm
    # chart's dashboard nginx, a cloudflared pod, a sidecar proxy.
    "private": (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    ),
    # Google Front End ranges that reach VM / GKE backends of a Google Cloud
    # external Application Load Balancer. The load balancer also appends its
    # OWN forwarding-rule address to X-Forwarded-For, so list that address
    # too (e.g. "gcp-lb,203.0.113.10").
    "gcp-lb": (
        "35.191.0.0/16",
        "130.211.0.0/22",
    ),
    # The address Cloud Run's in-sandbox proxy connects from (the container
    # sees every request from a 169.254.x.x peer). Combine with the load
    # balancer's forwarding-rule address when a load balancer fronts Cloud Run.
    "cloud-run": ("169.254.0.0/16",),
    # Cloudflare's published edge ranges (https://www.cloudflare.com/ips/,
    # as of 2026-11). Only for proxied DNS straight to the origin; behind a
    # Cloudflare Tunnel the peer is cloudflared, so trust its address instead.
    # These ranges carry EVERY Cloudflare customer's traffic (a Worker on any
    # account can reach your origin from them and set the header), so this is
    # only safe when the origin accepts nothing but your own zone: Authenticated
    # Origin Pulls with a zone-specific certificate, a firewall that only admits
    # your zone, or a Tunnel.
    "cloudflare": (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    ),
}

# An X-Forwarded-For chain longer than this is not walked any further; the
# nearest trusted hop stands in for the client. Real chains have a handful.
MAX_FORWARDED_HOPS = 32

# The broadest range an operator may list by hand (presets are exempt).
# Anything wider trusts a large slice of the internet to report addresses.
MIN_IPV4_PREFIX = 8
MIN_IPV6_PREFIX = 16

_trusted: tuple[IPNetwork, ...] = ()


def parse_trusted_proxies(raw: str | Iterable[str] | None) -> tuple[IPNetwork, ...]:
    """Parse ``SFS_TRUSTED_PROXIES`` into networks.

    Accepts a comma-separated string (or an iterable of entries) of CIDRs,
    bare addresses and preset names (case-insensitive). Raises ``ValueError``
    on any entry it can't parse, so a typo refuses startup instead of quietly
    trusting nothing (or too much).
    """
    if raw is None:
        return ()
    entries = raw.split(",") if isinstance(raw, str) else list(raw)
    networks: list[IPNetwork] = []
    for entry in entries:
        item = str(entry).strip()
        if not item:
            continue
        preset = PRESETS.get(item.lower())
        if preset is not None:
            networks.extend(ipaddress.ip_network(c) for c in preset)
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"SFS_TRUSTED_PROXIES: {item!r} is not a CIDR, an IP address or one of "
                f"the presets {', '.join(sorted(PRESETS))}"
            ) from exc
        if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped is not None:
            mapped = net.network_address.ipv4_mapped
            v4_prefix = max(net.prefixlen - 96, 0)
            net = ipaddress.ip_network(f"{mapped}/{v4_prefix}", strict=False)
        # Checked after the IPv4-mapped normalisation, so ::ffff:0.0.0.0/97
        # is caught as 0.0.0.0/1. Presets (above) are exempt.
        min_prefix = MIN_IPV4_PREFIX if net.version == 4 else MIN_IPV6_PREFIX
        if net.prefixlen < min_prefix:
            raise ValueError(
                f"SFS_TRUSTED_PROXIES: {item!r} is too broad (a /{net.prefixlen}); any caller in it "
                f"could forge its address. Use IPv4 ranges of /{MIN_IPV4_PREFIX} or narrower and IPv6 "
                f"ranges of /{MIN_IPV6_PREFIX} or narrower, or a preset"
            )
        networks.append(net)
    return tuple(dict.fromkeys(networks))


def configure_trusted_proxies(raw: str | Iterable[str] | None) -> tuple[IPNetwork, ...]:
    """Set the process-wide trusted proxy list (called by ``create_app``)."""
    global _trusted
    _trusted = parse_trusted_proxies(raw)
    return _trusted


def trusted_proxies() -> tuple[IPNetwork, ...]:
    return _trusted


def parse_ip(value: str | None) -> IPAddress | None:
    """One address from a peer value or an X-Forwarded-For entry, or None.

    Accepts ``1.2.3.4``, ``1.2.3.4:5678``, ``2001:db8::1``,
    ``[2001:db8::1]`` and ``[2001:db8::1]:443``, with an optional IPv6 zone
    id. IPv4-mapped IPv6 comes back as IPv4. Anything else (``unknown``,
    obfuscated identifiers, hostnames, empty) is None.
    """
    if value is None:
        return None
    text = value.strip().strip('"').strip()
    if not text or len(text) > 64:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        rest = text[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        text = text[1:end]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if not port.isdigit():
            return None
        text = host
    text = text.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: IPAddress, trusted: tuple[IPNetwork, ...]) -> bool:
    return any(ip.version == net.version and ip in net for net in trusted)


def resolve_client_ip(
    peer: str | None,
    forwarded_for: Iterable[str],
    trusted: tuple[IPNetwork, ...],
) -> str | None:
    """Pure resolution step behind :func:`client_ip` (see the module doc).

    ``forwarded_for`` is every ``X-Forwarded-For`` header value in the order
    received. Returns the client address as a string, or None when the peer
    itself is not an IP address (e.g. a unix socket or a test client).
    """
    peer_ip = parse_ip(peer)
    if peer_ip is None:
        return None
    if not trusted or not _is_trusted(peer_ip, trusted):
        return str(peer_ip)

    hops: list[str] = []
    for value in forwarded_for:
        hops.extend(value.split(","))

    current: IPAddress = peer_ip
    walked = 0
    for raw in reversed(hops):
        if not raw.strip():
            # "a,,b" or a trailing comma: skip empty entries.
            continue
        walked += 1
        if walked > MAX_FORWARDED_HOPS:
            break
        hop = parse_ip(raw)
        if hop is None:
            # A trusted proxy writes a real address; garbage came from further
            # left. The nearest trusted hop stands in for the client.
            break
        current = hop
        if not _is_trusted(hop, trusted):
            break
    return str(current)


def client_ip(request: HTTPConnection) -> str | None:
    """The caller's address for rate limits, logs and audit fields.

    None when the connection has no IP peer (tests, unix sockets); callers
    that need a key use ``client_ip(request) or "unknown"``.
    """
    client = request.client
    peer = client.host if client else None
    try:
        values = request.headers.getlist("x-forwarded-for")
    except Exception:  # pragma: no cover - malformed scope
        values = []
    return resolve_client_ip(peer, values, _trusted)


def client_ip_or_unknown(request: HTTPConnection) -> str:
    return client_ip(request) or "unknown"
