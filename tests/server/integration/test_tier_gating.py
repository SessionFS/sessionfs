"""Integration tests for tier gating on API endpoints."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import User


def _get_error_details(resp) -> dict:
    """Extract error details from the custom error response format."""
    body = resp.json()
    # Custom error handler wraps: {"error": {"code": ..., "message": ..., "details": {...}}}
    if "error" in body and isinstance(body["error"], dict):
        return body["error"].get("details", {})
    # Fallback to standard FastAPI format
    if "detail" in body:
        return body["detail"] if isinstance(body["detail"], dict) else {}
    return body


@pytest.fixture
async def free_user(db_session: AsyncSession) -> User:
    """User with free tier."""
    user = User(
        id=str(uuid.uuid4()),
        email="freeuser@example.com",
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def free_api_key(db_session: AsyncSession, free_user: User):
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=free_user.id,
        key_hash=hash_api_key(raw_key),
        name="free-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    return raw_key


@pytest.fixture
def free_headers(free_api_key: str) -> dict:
    return {"Authorization": f"Bearer {free_api_key}"}


@pytest.fixture
async def pro_user(db_session: AsyncSession) -> User:
    """User with pro tier."""
    user = User(
        id=str(uuid.uuid4()),
        email="prouser@example.com",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def pro_api_key(db_session: AsyncSession, pro_user: User):
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=pro_user.id,
        key_hash=hash_api_key(raw_key),
        name="pro-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    return raw_key


@pytest.fixture
def pro_headers(pro_api_key: str) -> dict:
    return {"Authorization": f"Bearer {pro_api_key}"}


@pytest.mark.asyncio
async def test_free_user_cannot_sync_push(
    client: AsyncClient, free_headers: dict, sample_sfs_tar: bytes,
):
    """Free users get 403 on sync push (requires cloud_sync)."""
    resp = await client.put(
        "/api/v1/sessions/ses_test12345678/sync",
        headers=free_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 403
    details = _get_error_details(resp)
    assert details["error"] == "upgrade_required"
    assert details["feature"] == "cloud_sync"
    assert details["required_tier"] == "starter"
    assert "upgrade_url" in details


@pytest.mark.asyncio
async def test_free_user_cannot_create_handoff(
    client: AsyncClient, free_headers: dict,
):
    """Free users get 403 on handoff creation (requires handoff feature)."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=free_headers,
        json={
            "session_id": "ses_doesnotmatter",
            "recipient_email": "someone@example.com",
        },
    )
    assert resp.status_code == 403
    details = _get_error_details(resp)
    assert details["error"] == "upgrade_required"
    assert details["feature"] == "handoff"


@pytest.mark.asyncio
async def test_free_user_can_list_sessions(
    client: AsyncClient, free_headers: dict,
):
    """Free users CAN list sessions (no tier gate on listing)."""
    resp = await client.get("/api/v1/sessions", headers=free_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pro_user_can_sync_push(
    client: AsyncClient, pro_headers: dict, sample_sfs_tar: bytes,
):
    """Pro users can sync push (has cloud_sync feature)."""
    session_id = f"ses_pro{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=pro_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_upgrade_required_includes_url(
    client: AsyncClient, free_headers: dict,
):
    """403 response includes upgrade URL for CLI to display."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=free_headers,
        json={
            "session_id": "ses_doesnotmatter",
            "recipient_email": "someone@example.com",
        },
    )
    assert resp.status_code == 403
    details = _get_error_details(resp)
    assert details["upgrade_url"] == "https://sessionfs.dev/pricing"


@pytest.mark.asyncio
async def test_free_user_cannot_create_project(
    client: AsyncClient, free_headers: dict,
):
    """Free users get 403 on project creation (requires project_context)."""
    resp = await client.post(
        "/api/v1/projects/",
        headers=free_headers,
        json={
            "name": "Test Project",
            "git_remote_normalized": "github.com/test/repo",
        },
    )
    assert resp.status_code == 403
    details = _get_error_details(resp)
    assert details["feature"] == "project_context"


@pytest.mark.asyncio
async def test_billing_status_returns_tier(
    client: AsyncClient, free_headers: dict,
):
    """Billing status returns user's tier and storage info."""
    resp = await client.get("/api/v1/billing/status", headers=free_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "free"
    assert data["storage_limit_bytes"] == 0
    assert data["has_subscription"] is False


@pytest.mark.asyncio
async def test_helm_validate_invalid_key(client: AsyncClient):
    """Invalid Helm license key returns 403."""
    resp = await client.post(
        "/api/v1/helm/validate",
        json={"license_key": "sfs_helm_invalid123"},
    )
    assert resp.status_code == 403
    assert resp.json()["valid"] is False


@pytest.mark.asyncio
async def test_telemetry_accepts_valid_payload(client: AsyncClient):
    """Telemetry endpoint accepts valid anonymous data."""
    resp = await client.post(
        "/api/v1/telemetry",
        json={
            "install_id": "abc123def456",
            "version": "0.9.5",
            "os": "darwin",
            "tools_active": ["claude-code", "cursor"],
            "sessions_captured_24h": 5,
            "tier": "free",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_telemetry_rejects_pii(client: AsyncClient):
    """Telemetry endpoint rejects payloads with email-like PII."""
    resp = await client.post(
        "/api/v1/telemetry",
        json={
            "install_id": "user@company.com",
            "version": "0.9.5",
            "os": "darwin",
        },
    )
    assert resp.status_code == 400
