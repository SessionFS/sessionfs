"""CLI side of trusted publishing and publisher targets."""

from __future__ import annotations

import json

import httpx
import pytest

from sessionfs.skills import publishing


def test_personal_public_target_sends_visibility() -> None:
    target = publishing.resolve_target(org_id=None, visibility="public", namespace="acme", collection=None)
    assert target.path == "/api/v1/users/me/skills/packages"
    assert target.form() == {"namespace": "acme", "collection": "personal", "visibility": "public"}
    assert "personal publisher" in target.describe()
    with pytest.raises(ValueError):
        publishing.resolve_target(org_id=None, visibility="org", namespace=None, collection=None)


def test_github_oidc_token_requests_audience_without_leaking() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"value": "jwt-value"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.example/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-secret",
    }
    assert publishing.github_oidc_token("sessionfs", env=env, client=client) == "jwt-value"
    assert seen["url"].endswith("api-version=2.0&audience=sessionfs")
    assert seen["auth"] == "Bearer runner-secret"
    with pytest.raises(publishing.TrustedPublishingError, match="id-token: write"):
        publishing.github_oidc_token("sessionfs", env={}, client=client)


def test_exchange_and_upload() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls.append(("token", json.loads(request.content)))
            return httpx.Response(200, json={"token": "sfs_tp_x", "namespace": "acme"})
        calls.append(("publish", {"auth": request.headers["authorization"]}))
        return httpx.Response(201, json={"version_id": "skv_1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    body = publishing.exchange_trusted_publishing_token(
        api_url="https://api.example", oidc_token="jwt", namespace="acme", package="pkg", client=client
    )
    assert body["token"] == "sfs_tp_x"
    status, result, _ = publishing.upload_trusted(
        api_url="https://api.example", publish_token="sfs_tp_x", filename="p.tar.gz", data=b"x",
        visibility=None, collection=None, client=client,
    )
    assert status == 201 and result == {"version_id": "skv_1"}
    assert calls == [
        ("token", {"token": "jwt", "namespace": "acme", "package": "pkg"}),
        ("publish", {"auth": "Bearer sfs_tp_x"}),
    ]


def test_audience_is_read_from_the_server() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"enabled": True, "audience": "https://api.acme.example"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    audience = publishing.trusted_publishing_audience(api_url="https://api.acme.example", client=client)
    assert audience == "https://api.acme.example"
    assert seen == ["/api/v1/marketplace/trusted-publishing/config"]

    def off(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"enabled": False, "audience": None})

    with pytest.raises(publishing.TrustedPublishingError, match="not configured"):
        publishing.trusted_publishing_audience(
            api_url="https://x", client=httpx.Client(transport=httpx.MockTransport(off))
        )


def test_exchange_failure_is_explained() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "invalid_oidc_token", "message": "rejected", "details": {"reason": "invalid_aud"}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(publishing.TrustedPublishingError, match="invalid_aud"):
        publishing.exchange_trusted_publishing_token(
            api_url="https://api.example", oidc_token="jwt", namespace="acme", package=None, client=client
        )


def test_trusted_validation_target_follows_the_publisher() -> None:
    assert publishing.trusted_validation_visibility(None, {"default_visibility": "user"}) == "personal"
    assert publishing.trusted_validation_visibility(None, {"default_visibility": "org"}) == "org"
    assert publishing.trusted_validation_visibility(None, {}) == "org"
    assert publishing.trusted_validation_visibility("public", {"default_visibility": "user"}) == "public"
