"""Signup verification links point at the configured server, never a fixed host.

Self-hosted servers used to email links to https://api.sessionfs.dev, so a
new user could never verify. The link now comes from SFS_PUBLIC_API_URL, then
SFS_DASHBOARD_URL (the Helm dashboard serves /api), and is never built from
the request's Host header.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")


class _Mail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_verification(self, email: str, link: str) -> None:
        self.sent.append((email, link))


def _app(client):
    return client._transport.app  # type: ignore[attr-defined]


@pytest.fixture
def mail(client, monkeypatch):
    monkeypatch.setenv("SFS_REQUIRE_EMAIL_VERIFICATION", "true")
    box = _Mail()
    _app(client).state.email_service = box
    return box


async def _signup(client, email: str, **headers):
    resp = await client.post("/api/v1/auth/signup", json={"email": email}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp


async def test_link_uses_public_api_url(client, mail):
    cfg = _app(client).state.config
    cfg.public_api_url = "https://sfs.example.internal/"
    cfg.dashboard_url = "https://dash.example.internal"
    await _signup(client, "a@example.com", Host="evil.example")
    assert len(mail.sent) == 1
    email, link = mail.sent[0]
    assert email == "a@example.com"
    assert link.startswith("https://sfs.example.internal/api/v1/auth/verify?token=")
    assert "api.sessionfs.dev" not in link
    assert "evil.example" not in link


async def test_link_falls_back_to_dashboard_origin(client, mail):
    cfg = _app(client).state.config
    cfg.public_api_url = ""
    cfg.dashboard_url = "https://sessionfs.corp.example"
    await _signup(client, "b@example.com")
    assert mail.sent[0][1].startswith(
        "https://sessionfs.corp.example/api/v1/auth/verify?token="
    )


async def test_no_link_without_a_configured_origin(client, mail):
    cfg = _app(client).state.config
    cfg.public_api_url = ""
    cfg.dashboard_url = ""
    await _signup(client, "c@example.com", Host="evil.example")
    assert mail.sent == []
