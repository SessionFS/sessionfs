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
        enterprise project proj_a40ce92c16e9415d."""
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
