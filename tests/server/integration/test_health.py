"""Integration tests for the health endpoint."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from sessionfs import __version__


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_returns_correct_fields(client: AsyncClient):
    resp = await client.get("/health")
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == __version__
    assert data["service"] == "sessionfs-api"


@pytest.mark.asyncio
async def test_health_api_v1_alias(client: AsyncClient):
    """The /api/v1/health alias must serve the same payload as /health.

    Operators sometimes route only /api/v1/* to the API service through their
    ingress; this alias makes sure their existing health checks keep working.
    """
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == __version__
    assert data["service"] == "sessionfs-api"


@pytest.mark.asyncio
async def test_health_aliases_match(client: AsyncClient):
    """/health and /api/v1/health must return identical payloads."""
    a = await client.get("/health")
    b = await client.get("/api/v1/health")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
