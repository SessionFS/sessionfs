"""SSRF-guard tests for oidc_fetch — R2 HIGH 2 (§3.2.1).

Tests the full SSRF guard including:
  - Rejection of private/loopback/link-local/metadata IPs
  - Non-HTTPS rejection
  - R2-N1: per-host IP-range guard (Google Workspace cross-host case)
  - Redirect rejection
  - DNS-rebind defense
"""

from __future__ import annotations

import json
import socket
from unittest import mock

import httpx
import pytest

from sessionfs.server.services.oidc_fetch import (
    SsrfError,
    __name__ as _module_name,
    _is_private_or_metadata_ip,
    _resolve_and_validate_host,
    _set_test_transport,
    _validate_url,
    oidc_fetch,
    oidc_fetch_json,
)


class FakeTransport(httpx.AsyncHTTPTransport):
    """Test transport that returns canned responses."""
    def __init__(self, status_code=200, json_body=None, headers=None):
        super().__init__()
        self.status_code = status_code
        self.json_body = json_body
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request):
        self.requests.append(request)
        body = json.dumps(self.json_body).encode() if self.json_body else b"{}"
        return httpx.Response(
            self.status_code,
            headers=self.headers,
            content=body,
            request=request,
        )


class FakeRedirectTransport(httpx.AsyncHTTPTransport):
    """Test transport that returns a 302 redirect."""
    def __init__(self, redirect_location: str):
        super().__init__()
        self.location = redirect_location

    async def handle_async_request(self, request: httpx.Request):
        return httpx.Response(
            302,
            headers={"location": self.location},
            content=b"",
            request=request,
        )


# ---------------------------------------------------------------------------
# IP range tests — _is_private_or_metadata_ip
# ---------------------------------------------------------------------------


class TestIsPrivateOrMetadataIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1",
        "127.255.255.255",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
        "169.254.0.1",
        "169.254.169.254",  # metadata IP
        "::1",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",    # metadata v6
    ])
    def test_private_ips_rejected(self, ip):
        assert _is_private_or_metadata_ip(ip) is True

    @pytest.mark.parametrize("ip", [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "142.250.80.14",
        "2607:f8b0:4004:c17::1f",
    ])
    def test_public_ips_allowed(self, ip):
        assert _is_private_or_metadata_ip(ip) is False


# ---------------------------------------------------------------------------
# URL / host validation tests — _resolve_and_validate_host, _validate_url
# ---------------------------------------------------------------------------


class TestResolveAndValidateHost:
    def test_public_host_ok(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            ips = _resolve_and_validate_host("accounts.google.com")
            assert "8.8.8.8" in ips

    def test_loopback_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="127.0.0.1"):
                _resolve_and_validate_host("evil.internal")

    def test_metadata_ip_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("169.254.169.254", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="169.254.169.254"):
                _resolve_and_validate_host("metadata.internal")

    def test_private_ip_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("192.168.1.1", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="192.168.1.1"):
                _resolve_and_validate_host("internal.corp")

    def test_dns_failure_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            with pytest.raises(SsrfError, match="DNS resolution failed"):
                _resolve_and_validate_host("nonexistent.invalid")

    def test_mixed_public_private_rejected(self):
        """If ANY resolved IP is private, the host is rejected."""
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("192.168.1.1", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="192.168.1.1"):
                _resolve_and_validate_host("dual.internal")


class TestValidateUrl:
    def test_https_ok(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            hostname = _validate_url("https://accounts.google.com/.well-known/openid-configuration")
            assert hostname == "accounts.google.com"

    def test_http_rejected(self):
        with pytest.raises(SsrfError, match="Non-HTTPS"):
            _validate_url("http://evil.com/.well-known/openid-configuration")

    def test_private_host_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("10.0.0.1", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="10.0.0.1"):
                _validate_url("https://internal.example.com")


# ---------------------------------------------------------------------------
# SSRF fetch tests — oidc_fetch (end-to-end through SSRF guard)
# ---------------------------------------------------------------------------


class TestOidcFetch:
    def teardown_method(self):
        _set_test_transport(None)

    # ---- Happy path ----

    async def test_fetch_public_url(self):
        transport = FakeTransport(
            status_code=200,
            json_body={"issuer": "https://accounts.google.com"},
        )
        _set_test_transport(transport)
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            resp = await oidc_fetch("https://accounts.google.com/.well-known/openid-configuration")
            assert resp.status_code == 200
            assert resp.json()["issuer"] == "https://accounts.google.com"

    # ---- Non-HTTPS rejection ----

    async def test_http_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="Non-HTTPS"):
                await oidc_fetch("http://evil.com/.well-known/openid-configuration")

    # ---- Private-IP rejection for issuer ----

    async def test_issuer_resolves_to_loopback_rejected(self):
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="127.0.0.1"):
                await oidc_fetch("https://evil.internal/.well-known/openid-configuration")

    # ---- R2-N1: per-host guard — discovery jwks_uri on internal host ----

    async def test_discovery_jwks_uri_on_internal_host_rejected(self):
        """Discovery advertises a jwks_uri on an internal IP — REJECTED (R2-N1)."""
        # First fetch must be the discovery doc fetching from the issuer
        transport = FakeTransport(
            status_code=200,
            json_body={
                "issuer": "https://evil-idp.example.com",
                "jwks_uri": "https://jwks-evil.internal/keys",
                "token_endpoint": "https://evil-idp.example.com/token",
            },
        )
        _set_test_transport(transport)

        # Allow the issuer to resolve to a public IP
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            side_effect=lambda host, *args, **kwargs: (
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
                if "evil-idp.example.com" in host
                else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
            ),
        ):
            # Fetch discovery doc (this works — issuer resolves to public)
            resp_promise = await oidc_fetch(
                "https://evil-idp.example.com/.well-known/openid-configuration"
            )
            discovery = resp_promise.json()
            jwks_uri = discovery["jwks_uri"]

            # Now fetch jwks_uri independently — this MUST fail because
            # jwks-evil.internal resolves to 10.0.0.1 (private)
            with pytest.raises(SsrfError, match="10.0.0.1"):
                await oidc_fetch(jwks_uri)

    # ---- R2-N1: Google Workspace cross-host case — ALLOWED ----

    async def test_google_workspace_cross_public_hosts_allowed(self):
        """Google Workspace: issuer accounts.google.com, token endpoint
        oauth2.googleapis.com, jwks www.googleapis.com — all public
        hosts, ALL ALLOWED (R2-N1 regression)."""
        def _mock_resolve(host, *args, **kwargs):
            public_map = {
                "accounts.google.com": "142.250.80.14",
                "oauth2.googleapis.com": "142.251.40.202",
                "www.googleapis.com": "142.250.80.14",
            }
            ip = public_map.get(host.split(":")[0], "8.8.8.8")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            side_effect=_mock_resolve,
        ):
            # All three Google hosts should pass the SSRF guard
            transport = FakeTransport(status_code=200, json_body={"keys": []})
            _set_test_transport(transport)
            resp = await oidc_fetch("https://accounts.google.com/.well-known/openid-configuration")
            assert resp.status_code == 200

            # jwks_uri check
            resp2 = await oidc_fetch("https://www.googleapis.com/oauth2/v3/certs")
            assert resp2.status_code == 200

            # token_endpoint check
            resp3 = await oidc_fetch("https://oauth2.googleapis.com/token")
            assert resp3.status_code == 200

    # ---- Redirect rejection ----

    async def test_redirect_not_followed(self):
        _set_test_transport(
            FakeRedirectTransport("https://169.254.169.254/latest/meta-data/")
        )
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            with pytest.raises(SsrfError, match="Redirect"):
                await oidc_fetch("https://evil.example.com")

    # ---- DNS rebind defense ----

    async def test_dns_rebind_rejected(self):
        """DNS rebind: first lookup returns public IP → second returns private.
        Our resolve-once model stops this because we validate BEFORE each fetch.
        """
        call_count = [0]

        def _rebind_resolve(host, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         ("8.8.8.8", 0))]
            else:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         ("127.0.0.1", 0))]

        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            side_effect=_rebind_resolve,
        ):
            # First call — public IP, should work
            transport = FakeTransport(status_code=200, json_body={"ok": True})
            _set_test_transport(transport)
            resp = await oidc_fetch("https://rebind.example.com/.well-known/openid-configuration")
            assert resp.status_code == 200

            # Second call — rebinds to 127.0.0.1, should be REJECTED
            with pytest.raises(SsrfError, match="127.0.0.1"):
                await oidc_fetch("https://rebind.example.com/.well-known/openid-configuration")


# ---------------------------------------------------------------------------
# oidc_fetch_json tests
# ---------------------------------------------------------------------------


class TestOidcFetchJson:
    def teardown_method(self):
        _set_test_transport(None)

    async def test_fetch_json(self):
        transport = FakeTransport(
            status_code=200,
            json_body={"key": "value"},
        )
        _set_test_transport(transport)
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            result = await oidc_fetch_json("https://example.com/api")
            assert result == {"key": "value"}

    async def test_fetch_json_non_2xx_raises(self):
        transport = FakeTransport(status_code=500, json_body={"error": "internal"})
        _set_test_transport(transport)
        with mock.patch(
            f"{_module_name}.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("8.8.8.8", 0)),
            ],
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await oidc_fetch_json("https://example.com/api")
