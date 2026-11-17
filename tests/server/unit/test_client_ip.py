"""Client IP resolution: forwarding headers only from trusted proxies (tk_32e88a6ed5434fcf)."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from sessionfs.server import client_ip as cip
from sessionfs.server.client_ip import (
    MAX_FORWARDED_HOPS,
    PRESETS,
    client_ip,
    client_ip_or_unknown,
    configure_trusted_proxies,
    parse_ip,
    parse_trusted_proxies,
    resolve_client_ip,
)
from sessionfs.server.config import ServerConfig

PRIVATE = parse_trusted_proxies("private")


@pytest.fixture(autouse=True)
def _reset_trusted():
    configure_trusted_proxies("")
    yield
    configure_trusted_proxies("")


def _request(peer: str | None, *xff: str, extra: list[tuple[bytes, bytes]] | None = None) -> Request:
    headers = [(b"x-forwarded-for", v.encode("latin-1")) for v in xff]
    headers += extra or []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


# ── parse_trusted_proxies ──


def test_empty_config_trusts_nothing():
    assert parse_trusted_proxies("") == ()
    assert parse_trusted_proxies(None) == ()
    assert parse_trusted_proxies(" , ,") == ()


def test_presets_and_cidrs_and_bare_addresses():
    nets = parse_trusted_proxies("private, gcp-lb ,203.0.113.10, 2001:db8::/32, CLOUD-RUN")
    assert ipaddress.ip_network("10.0.0.0/8") in nets
    assert ipaddress.ip_network("35.191.0.0/16") in nets
    assert ipaddress.ip_network("130.211.0.0/22") in nets
    assert ipaddress.ip_network("203.0.113.10/32") in nets
    assert ipaddress.ip_network("2001:db8::/32") in nets
    assert ipaddress.ip_network("169.254.0.0/16") in nets


def test_list_form_accepted():
    assert parse_trusted_proxies(["10.0.0.0/8", "cloudflare"])[0] == ipaddress.ip_network("10.0.0.0/8")


def test_every_preset_parses():
    for name in PRESETS:
        assert parse_trusted_proxies(name)


@pytest.mark.parametrize("bad", ["bogus", "10.0.0.0/33", "1.2.3", "0.0.0.0/0", "::/0", "::ffff:0:0/96"])
def test_bad_entries_refuse(bad):
    with pytest.raises(ValueError):
        parse_trusted_proxies(bad)


@pytest.mark.parametrize(
    "broad",
    ["0.0.0.0/1", "8.0.0.0/7", "2000::/3", "2001::/15", "::ffff:0.0.0.0/97", "::ffff:8.0.0.0/103"],
)
def test_too_broad_ranges_refuse(broad):
    with pytest.raises(ValueError, match="too broad"):
        parse_trusted_proxies(broad)


@pytest.mark.parametrize("ok", ["10.0.0.0/8", "2001:db8::/16", "::ffff:10.0.0.0/104", "203.0.113.10"])
def test_narrowest_allowed_ranges_accepted(ok):
    assert parse_trusted_proxies(ok)


def test_presets_exempt_from_breadth_floor():
    # The cloudflare preset includes /13–/29 IPv6 ranges and the private
    # preset fc00::/7 — both broader than /16 but accepted as presets.
    nets = parse_trusted_proxies("private,cloudflare")
    assert ipaddress.ip_network("fc00::/7") in nets
    with pytest.raises(ValueError, match="too broad"):
        parse_trusted_proxies("fc00::/7")


def test_too_broad_refuses_server_config():
    with pytest.raises(ValidationError):
        ServerConfig(trusted_proxies="private,1.0.0.0/7")


def test_server_config_refuses_bad_value():
    with pytest.raises(ValidationError):
        ServerConfig(trusted_proxies="10.0.0.0/8,nope")
    assert ServerConfig(trusted_proxies=" private ").trusted_proxies == "private"


def test_ipv4_mapped_cidr_is_stored_as_ipv4():
    assert parse_trusted_proxies("::ffff:10.0.0.0/104") == (ipaddress.ip_network("10.0.0.0/8"),)


# ── parse_ip ──


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("203.0.113.7:51000", "203.0.113.7"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("fe80::1%eth0", "fe80::1"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ('"203.0.113.7"', "203.0.113.7"),
    ],
)
def test_parse_ip_forms(raw, expected):
    assert str(parse_ip(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "unknown", "_hidden", "example.com", "1.2.3.4:http", "[2001:db8::1", "[::1]x", "999.1.1.1", "a" * 100, None],
)
def test_parse_ip_rejects(raw):
    assert parse_ip(raw) is None


# ── resolve_client_ip ──


def test_no_trusted_proxies_ignores_headers():
    assert resolve_client_ip("10.0.0.5", ["198.51.100.1"], ()) == "10.0.0.5"


def test_spoofed_header_from_untrusted_peer_ignored():
    # The peer is a public address that isn't a configured proxy.
    assert resolve_client_ip("198.51.100.20", ["1.1.1.1"], PRIVATE) == "198.51.100.20"


def test_single_trusted_proxy():
    assert resolve_client_ip("10.0.0.5", ["198.51.100.1"], PRIVATE) == "198.51.100.1"


def test_client_supplied_prefix_is_not_believed():
    # Client sent "X-Forwarded-For: 1.1.1.1"; the ingress appended the real
    # address; the dashboard nginx appended the ingress pod.
    chain = ["1.1.1.1, 198.51.100.1, 10.1.0.7"]
    assert resolve_client_ip("10.1.0.9", chain, PRIVATE) == "198.51.100.1"


def test_multi_hop_chain_with_named_proxies():
    trusted = parse_trusted_proxies("cloud-run,8.233.150.3")
    # Cloud Run behind a global external ALB: "<spoof>, <client>, <LB ip>".
    assert resolve_client_ip("169.254.169.126", ["6.6.6.6, 203.0.113.9, 8.233.150.3"], trusted) == "203.0.113.9"
    # Someone hitting run.app directly with a forged LB hop: Cloud Run appends
    # the real address last, so the forged hops never win.
    assert (
        resolve_client_ip("169.254.169.126", ["6.6.6.6, 8.233.150.3, 192.0.2.44"], trusted)
        == "192.0.2.44"
    )


def test_multiple_header_lines_joined_in_order():
    assert resolve_client_ip("10.0.0.5", ["1.1.1.1", "198.51.100.1, 10.0.0.9"], PRIVATE) == "198.51.100.1"


def test_all_trusted_returns_left_most():
    assert resolve_client_ip("10.0.0.5", ["10.9.9.9, 10.0.0.7"], PRIVATE) == "10.9.9.9"


def test_empty_header_from_trusted_peer_uses_peer():
    assert resolve_client_ip("10.0.0.5", [], PRIVATE) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", [""], PRIVATE) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", [" , ,"], PRIVATE) == "10.0.0.5"


def test_malformed_entry_stops_at_nearest_trusted_hop():
    # Garbage directly appended to the peer: the peer stands in.
    assert resolve_client_ip("10.0.0.5", ["unknown"], PRIVATE) == "10.0.0.5"
    # Garbage to the left of a trusted hop: that hop stands in.
    assert resolve_client_ip("10.0.0.5", ["not-an-ip, 10.0.0.8"], PRIVATE) == "10.0.0.8"
    # Garbage to the left of the real client never matters.
    assert resolve_client_ip("10.0.0.5", ["<script>, 198.51.100.1"], PRIVATE) == "198.51.100.1"


def test_empty_entries_skipped():
    assert resolve_client_ip("10.0.0.5", ["198.51.100.1,,10.0.0.8,"], PRIVATE) == "198.51.100.1"


def test_hop_cap():
    chain = ", ".join(["198.51.100.1"] + ["10.0.0.8"] * (MAX_FORWARDED_HOPS + 5))
    assert resolve_client_ip("10.0.0.5", [chain], PRIVATE) == "10.0.0.8"


def test_ipv6_chain():
    trusted = parse_trusted_proxies("fd00::/16,2001:db8:aaaa::/48")
    assert (
        resolve_client_ip("fd00::5", ["2001:db8:1::7, [2001:db8:aaaa::1]:443"], trusted)
        == "2001:db8:1::7"
    )
    # IPv6 peer that isn't trusted: headers ignored.
    assert resolve_client_ip("2001:db8:2::1", ["2001:db8:1::7"], trusted) == "2001:db8:2::1"


def test_ipv4_mapped_peer_and_hops():
    # Dual-stack sockets report IPv4 peers as ::ffff:a.b.c.d.
    assert resolve_client_ip("::ffff:10.0.0.5", ["::ffff:198.51.100.1"], PRIVATE) == "198.51.100.1"
    assert resolve_client_ip("::ffff:198.51.100.20", ["1.1.1.1"], PRIVATE) == "198.51.100.20"


def test_ipv4_mapped_hop_still_trusted():
    assert resolve_client_ip("10.0.0.5", ["198.51.100.1, ::ffff:10.0.0.8"], PRIVATE) == "198.51.100.1"


def test_non_ip_peer_is_none():
    assert resolve_client_ip("testclient", ["198.51.100.1"], PRIVATE) is None
    assert resolve_client_ip(None, ["198.51.100.1"], PRIVATE) is None


# ── client_ip(request) ──


def test_request_helper_uses_configured_list():
    req = _request("10.0.0.5", "1.1.1.1, 198.51.100.1")
    assert client_ip(req) == "10.0.0.5"  # nothing trusted yet
    configure_trusted_proxies("private")
    assert client_ip(req) == "198.51.100.1"


def test_request_helper_ignores_other_headers():
    configure_trusted_proxies("private")
    req = _request(
        "10.0.0.5",
        extra=[
            (b"x-real-ip", b"1.1.1.1"),
            (b"cf-connecting-ip", b"1.1.1.2"),
            (b"forwarded", b"for=1.1.1.3"),
            (b"true-client-ip", b"1.1.1.4"),
        ],
    )
    assert client_ip(req) == "10.0.0.5"


def test_request_without_client():
    assert client_ip(_request(None, "198.51.100.1")) is None
    assert client_ip_or_unknown(_request(None)) == "unknown"


def test_create_app_installs_list():
    from sessionfs.server.app import create_app

    create_app(ServerConfig(database_url="sqlite+aiosqlite://", trusted_proxies="cloud-run"))
    assert cip.trusted_proxies() == (ipaddress.ip_network("169.254.0.0/16"),)
    create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    assert cip.trusted_proxies() == ()


# ── no other module reads forwarding headers or the raw peer ──


def test_only_the_helper_reads_client_addresses():
    root = Path(__file__).resolve().parents[3] / "src" / "sessionfs"
    # Header names as string literals (a read), not mentions in comments.
    needles = ("client.host",) + tuple(
        f"{q}{name}{q}"
        for name in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip", "forwarded")
        for q in ('"', "'")
    )
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "client_ip.py" and path.parent.name == "server":
            continue
        text = path.read_text(encoding="utf-8").lower()
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")
    assert offenders == []
