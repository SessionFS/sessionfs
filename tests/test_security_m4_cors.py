"""M4: CORS default empty instead of wildcard."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")

from sessionfs.server.config import ServerConfig


class TestCORSDefaults:

    def test_default_cors_is_empty(self):
        config = ServerConfig()
        assert config.cors_origins == []

    def test_cors_can_be_set_explicitly(self):
        config = ServerConfig(cors_origins=["https://app.sessionfs.com"])
        assert config.cors_origins == ["https://app.sessionfs.com"]

    def test_no_cors_middleware_when_empty(self):
        """When CORS origins is empty, middleware should not be added."""
        from sessionfs.server.app import create_app

        config = ServerConfig(cors_origins=[])
        app = create_app(config)
        # Check that CORSMiddleware is not in the middleware stack
        middleware_classes = [type(m).__name__ for m in getattr(app, "user_middleware", [])]
        assert "CORSMiddleware" not in middleware_classes

    def test_preflight_allows_if_match_header(self):
        """CORS preflight with If-Match in Access-Control-Request-Headers must
        return 200 — dashboard PUT /rules sends If-Match for ETag-based
        optimistic concurrency. Without this header in allow_headers the
        browser preflight 400s and the PUT never fires. Caught 2026-05-26 on
        enterprise project proj_a40ce92c16e9415d.

        Dashboard call site: dashboard/src/api/client.ts:1137 (rules PUT)."""
        from fastapi.testclient import TestClient

        from sessionfs.server.app import create_app

        config = ServerConfig(cors_origins=["https://app.sessionfs.dev"])
        app = create_app(config)
        with TestClient(app) as client:
            resp = client.options(
                "/api/v1/projects/proj_test/rules",
                headers={
                    "Origin": "https://app.sessionfs.dev",
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "content-type,authorization,if-match",
                },
            )
        assert resp.status_code == 200, (
            f"Preflight rejected (status={resp.status_code}); If-Match must be "
            "in allow_headers or dashboard rules PUT 400s before firing."
        )
        allowed = resp.headers.get("access-control-allow-headers", "").lower()
        assert "if-match" in allowed, (
            f"If-Match missing from access-control-allow-headers: {allowed!r}"
        )

    def test_preflight_allows_content_type_for_json_writes(self):
        """Every dashboard POST/PUT/PATCH sends Content-Type: application/json.
        Removing Content-Type from allow_headers would break ~50 dashboard
        write sites including OrgPage, useOrgMembers, useOrgSettings,
        useTransfers, useInvites, BillingPage, auth signup, and every
        request()/requestWithEtag() helper call.

        Dashboard call sites (representative): dashboard/src/api/client.ts:551
        (request helper), dashboard/src/api/client.ts:574 (requestWithEtag
        helper), dashboard/src/api/client.ts:1284 (auth signup)."""
        from fastapi.testclient import TestClient

        from sessionfs.server.app import create_app

        config = ServerConfig(cors_origins=["https://app.sessionfs.dev"])
        app = create_app(config)
        with TestClient(app) as client:
            resp = client.options(
                "/api/v1/auth/signup",
                headers={
                    "Origin": "https://app.sessionfs.dev",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        assert resp.status_code == 200, (
            f"Preflight rejected (status={resp.status_code}); Content-Type must "
            "be in allow_headers or every dashboard JSON write 400s before firing."
        )
        allowed = resp.headers.get("access-control-allow-headers", "").lower()
        assert "content-type" in allowed, (
            f"Content-Type missing from access-control-allow-headers: {allowed!r}"
        )

    def test_preflight_allows_authorization_for_authenticated_requests(self):
        """Every authenticated dashboard request sends Authorization: Bearer
        <api_key>. Removing Authorization from allow_headers would break
        every cloud-backed dashboard fetch.

        Dashboard call sites (representative): dashboard/src/api/client.ts:550
        (request helper), dashboard/src/api/client.ts:573 (requestWithEtag
        helper), dashboard/src/api/client.ts:689 (session download)."""
        from fastapi.testclient import TestClient

        from sessionfs.server.app import create_app

        config = ServerConfig(cors_origins=["https://app.sessionfs.dev"])
        app = create_app(config)
        with TestClient(app) as client:
            resp = client.options(
                "/api/v1/auth/me",
                headers={
                    "Origin": "https://app.sessionfs.dev",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
        assert resp.status_code == 200, (
            f"Preflight rejected (status={resp.status_code}); Authorization "
            "must be in allow_headers or every authenticated dashboard fetch "
            "400s before firing."
        )
        allowed = resp.headers.get("access-control-allow-headers", "").lower()
        assert "authorization" in allowed, (
            f"Authorization missing from access-control-allow-headers: {allowed!r}"
        )

    def test_allow_headers_pinned_to_audited_set(self):
        """Drift guard — pins the exact CORSMiddleware.allow_headers list
        audited 2026-05-26 (tk_f61ec6da37994897). Any addition or removal
        must update this test AND re-run the dashboard enumeration sweep:

            grep -rEn "headers:" dashboard/src 2>/dev/null | grep -v node_modules

        Adding a header without re-audit risks silent CORS preflight failure
        (see tk_23f523c1bdd94fc5 incident). Removing a header risks breaking
        a live dashboard call site.

        Wildcard ['*'] is explicitly forbidden — explicit allowlist is the
        security-meaningful choice."""
        import inspect

        from sessionfs.server import app as app_module

        source = inspect.getsource(app_module.create_app)
        # Source-level pin: the literal list must appear verbatim. Audit
        # findings: 0 additional latent headers beyond this set as of
        # 2026-05-26. See KB entry entity_ref='cors-header-audit-2026-05'.
        expected_literal = (
            '["Content-Type", "Authorization", "If-Match", "If-None-Match"]'
        )
        assert expected_literal in source, (
            f"CORS allow_headers list drifted from audited set. Expected literal "
            f"{expected_literal!r} in create_app source. If this change is "
            "intentional, re-run the dashboard header enumeration sweep and "
            "update this test + add a per-header preflight regression test."
        )
        assert '"*"' not in source.split("allow_headers")[1].split("]")[0], (
            "allow_headers must not be wildcard ['*']; explicit allowlist required."
        )
