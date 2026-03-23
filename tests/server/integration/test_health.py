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
