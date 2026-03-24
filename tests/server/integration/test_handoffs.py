"""Integration tests for handoff endpoints."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Handoff


@pytest.fixture
async def pushed_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes) -> str:
    """Push a session and return its ID."""
    session_id = f"ses_handoff{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    return session_id


@pytest.mark.asyncio
async def test_create_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Create a handoff -> 201 with handoff ID."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "recipient@example.com",
            "message": "Please continue this task.",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("hnd_")
    assert data["session_id"] == pushed_session
    assert data["recipient_email"] == "recipient@example.com"
    assert data["message"] == "Please continue this task."
    assert data["status"] == "pending"
    assert data["sender_email"] == "test@example.com"
    assert data["session_title"] == "Test session title"
    assert data["session_tool"] == "claude-code"


@pytest.mark.asyncio
async def test_create_handoff_missing_session(
    client: AsyncClient, auth_headers: dict,
):
    """Creating a handoff for nonexistent session -> 404."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": "ses_doesnotexist1234",
            "recipient_email": "someone@example.com",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Get handoff details by ID."""
    # Create handoff first
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "recipient@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Get details (no auth required)
    resp = await client.get(f"/api/v1/handoffs/{handoff_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == handoff_id
    assert data["session_id"] == pushed_session


@pytest.mark.asyncio
async def test_get_handoff_not_found(client: AsyncClient):
    """Get nonexistent handoff -> 404."""
    resp = await client.get("/api/v1/handoffs/hnd_nonexistent12345")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_claim_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Claim a handoff -> 200 with claimed status."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/handoffs/{handoff_id}/claim",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "claimed"


@pytest.mark.asyncio
async def test_claim_handoff_twice(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Claiming a handoff twice -> 409."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    resp = await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_inbox(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Inbox lists handoffs sent to the current user's email."""
    # Create a handoff to the test user's email
    await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )

    resp = await client.get("/api/v1/handoffs/inbox", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(h["recipient_email"] == "test@example.com" for h in data["handoffs"])


@pytest.mark.asyncio
async def test_sent(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Sent lists handoffs created by the current user."""
    await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "other@example.com",
        },
    )

    resp = await client.get("/api/v1/handoffs/sent", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(h["recipient_email"] == "other@example.com" for h in data["handoffs"])


@pytest.mark.asyncio
async def test_expired_handoff_get(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
    db_session: AsyncSession,
):
    """Getting an expired handoff -> 410."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Manually expire the handoff
    result = await db_session.execute(
        __import__("sqlalchemy").select(Handoff).where(Handoff.id == handoff_id)
    )
    handoff = result.scalar_one()
    handoff.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    resp = await client.get(f"/api/v1/handoffs/{handoff_id}")
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_expired_handoff_claim(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
    db_session: AsyncSession,
):
    """Claiming an expired handoff -> 410."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Manually expire
    result = await db_session.execute(
        __import__("sqlalchemy").select(Handoff).where(Handoff.id == handoff_id)
    )
    handoff = result.scalar_one()
    handoff.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    resp = await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    assert resp.status_code == 410
