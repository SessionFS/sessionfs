"""Routes record the resolved client IP, never a spoofed one (tk_32e88a6ed5434fcf).

httpx's ASGITransport connects from 127.0.0.1.
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.client_ip import configure_trusted_proxies
from sessionfs.server.db.models import ApiKey


@pytest.fixture
def trust(client):  # depends on client so create_app's reset runs first
    def _set(value: str) -> None:
        configure_trusted_proxies(value)

    yield _set
    configure_trusted_proxies("")


async def _last_used_ip(db_session: AsyncSession, key_id: str) -> str | None:
    db_session.expire_all()
    row = (await db_session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one()
    return row.last_used_ip


@pytest.mark.asyncio
async def test_last_used_ip_ignores_spoofed_header_from_untrusted_peer(
    client: AsyncClient, auth_headers, test_api_key, db_session: AsyncSession, trust
):
    trust("")
    r = await client.get(
        "/api/v1/auth/me", headers={**auth_headers, "X-Forwarded-For": "6.6.6.6"}
    )
    assert r.status_code == 200
    assert await _last_used_ip(db_session, test_api_key[1].id) == "127.0.0.1"

    # A public proxy list that doesn't include the peer: still ignored.
    trust("gcp-lb")
    await client.get("/api/v1/auth/me", headers={**auth_headers, "X-Forwarded-For": "6.6.6.6"})
    assert await _last_used_ip(db_session, test_api_key[1].id) == "127.0.0.1"


@pytest.mark.asyncio
async def test_last_used_ip_from_trusted_chain(
    client: AsyncClient, auth_headers, test_api_key, db_session: AsyncSession, trust
):
    trust("private")
    r = await client.get(
        "/api/v1/auth/me",
        headers={**auth_headers, "X-Forwarded-For": "6.6.6.6, 203.0.113.9, 10.1.0.7"},
    )
    assert r.status_code == 200
    assert await _last_used_ip(db_session, test_api_key[1].id) == "203.0.113.9"

    await client.get(
        "/api/v1/auth/me",
        headers={**auth_headers, "X-Forwarded-For": "2001:db8::7, fd00::1"},
    )
    assert await _last_used_ip(db_session, test_api_key[1].id) == "2001:db8::7"


@pytest.mark.asyncio
async def test_4xx_log_uses_resolved_ip(client: AsyncClient, trust, caplog):
    caplog.set_level(logging.WARNING, logger="sessionfs.api")
    trust("")
    await client.get("/api/v1/auth/me", headers={"X-Forwarded-For": "6.6.6.6"})
    assert "client=127.0.0.1" in caplog.text
    assert "6.6.6.6" not in caplog.text

    caplog.clear()
    trust("private")
    await client.get("/api/v1/auth/me", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.4"})
    assert "client=198.51.100.4" in caplog.text


@pytest.mark.asyncio
async def test_signup_limiter_buckets_by_resolved_ip(client: AsyncClient, trust, monkeypatch):
    from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
    from sessionfs.server.routes import auth as auth_routes

    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=3600)
    monkeypatch.setattr(auth_routes, "_signup_limiter", limiter)

    # Untrusted peer: rotating the header doesn't buy a fresh bucket.
    trust("")
    await client.post("/api/v1/auth/signup", json={"email": "a1@example.com"}, headers={"X-Forwarded-For": "1.1.1.1"})
    r = await client.post(
        "/api/v1/auth/signup", json={"email": "a2@example.com"}, headers={"X-Forwarded-For": "2.2.2.2"}
    )
    assert r.status_code == 429

    # Trusted proxy: distinct real clients get distinct buckets.
    trust("private")
    limiter2 = SlidingWindowRateLimiter(max_requests=1, window_seconds=3600)
    monkeypatch.setattr(auth_routes, "_signup_limiter", limiter2)
    r1 = await client.post(
        "/api/v1/auth/signup", json={"email": "b1@example.com"}, headers={"X-Forwarded-For": "198.51.100.1"}
    )
    r2 = await client.post(
        "/api/v1/auth/signup", json={"email": "b2@example.com"}, headers={"X-Forwarded-For": "198.51.100.2"}
    )
    assert r1.status_code != 429 and r2.status_code != 429
    r3 = await client.post(
        "/api/v1/auth/signup", json={"email": "b3@example.com"}, headers={"X-Forwarded-For": "198.51.100.1"}
    )
    assert r3.status_code == 429
