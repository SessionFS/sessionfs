"""Integration tests for admin API endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Session, User


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user."""
    user = User(
        id=str(uuid.uuid4()),
        email="admin@sessionfs.dev",
        display_name="Admin",
        tier="admin",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_api_key(db_session: AsyncSession, admin_user: User) -> tuple[str, ApiKey]:
    """Create an API key for the admin user."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin_user.id,
        key_hash=hash_api_key(raw_key),
        name="admin-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw_key, api_key


@pytest.fixture
def admin_headers(admin_api_key: tuple[str, ApiKey]) -> dict[str, str]:
    """Authorization headers for the admin user."""
    return {"Authorization": f"Bearer {admin_api_key[0]}"}


@pytest.fixture
async def extra_user(db_session: AsyncSession) -> User:
    """Create an additional non-admin user for testing."""
    user = User(
        id=str(uuid.uuid4()),
        email="regular@example.com",
        display_name="Regular User",
        tier="free",
        email_verified=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def extra_session(db_session: AsyncSession, extra_user: User) -> Session:
    """Create a session owned by extra_user."""
    import hashlib

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=extra_user.id,
        title="Extra session",
        tags="[]",
        source_tool="codex",
        blob_key=f"sessions/{extra_user.id}/{session_id}/session.tar.gz",
        blob_size_bytes=1024,
        etag=hashlib.sha256(b"test").hexdigest(),
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Non-admin gets 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_gets_403(client: AsyncClient, auth_headers: dict):
    """Regular users cannot access admin endpoints."""
    resp = await client.get("/api/v1/admin/users", headers=auth_headers)
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_non_admin_stats_403(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/admin/stats", headers=auth_headers)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2  # admin + extra + test_user
    assert isinstance(data["users"], list)
    emails = [u["email"] for u in data["users"]]
    assert "regular@example.com" in emails


@pytest.mark.asyncio
async def test_list_users_search(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users?search=regular", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["users"][0]["email"] == "regular@example.com"


@pytest.mark.asyncio
async def test_list_users_tier_filter(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users?tier_filter=admin", headers=admin_headers)
    assert resp.status_code == 200
    for u in resp.json()["users"]:
        assert u["tier"] == "admin"


# ---------------------------------------------------------------------------
# Get user detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_detail(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "regular@example.com"
    assert data["session_count"] == 0
    assert "storage_used_bytes" in data
    assert "api_key_count" in data


@pytest.mark.asyncio
async def test_get_user_detail_not_found(client: AsyncClient, admin_headers: dict):
    resp = await client.get("/api/v1/admin/users/nonexistent", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Change tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_user_tier(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "pro"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_tier"] == "free"
    assert data["new_tier"] == "pro"

    # Verify change persisted
    detail = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert detail.json()["tier"] == "pro"


@pytest.mark.asyncio
async def test_change_tier_invalid(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "invalid"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Force verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_verify(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/verify",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["email_verified"] is True


# ---------------------------------------------------------------------------
# Delete user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{extra_user.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204

    # User should now show as inactive
    detail = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert detail.json()["is_active"] is False


@pytest.mark.asyncio
async def test_delete_self_rejected(client: AsyncClient, admin_headers: dict, admin_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{admin_user.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "own account" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_sessions(
    client: AsyncClient, admin_headers: dict, extra_session: Session,
):
    resp = await client.get("/api/v1/admin/sessions", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [s["id"] for s in data["sessions"]]
    assert extra_session.id in ids


@pytest.mark.asyncio
async def test_delete_session(
    client: AsyncClient, admin_headers: dict, extra_session: Session,
):
    resp = await client.delete(
        f"/api/v1/admin/sessions/{extra_session.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204

    # Session should no longer appear in listing
    listing = await client.get("/api/v1/admin/sessions", headers=admin_headers)
    ids = [s["id"] for s in listing.json()["sessions"]]
    assert extra_session.id not in ids


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats(client: AsyncClient, admin_headers: dict, extra_session: Session):
    resp = await client.get("/api/v1/admin/stats", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    assert "sessions" in data
    assert "handoffs" in data
    assert "storage" in data
    assert data["users"]["total"] >= 2
    assert data["sessions"]["total"] >= 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_records_actions(
    client: AsyncClient, admin_headers: dict, extra_user: User,
):
    # Perform an action that gets logged
    await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "team"},
        headers=admin_headers,
    )

    resp = await client.get("/api/v1/admin/audit-log", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    actions = data["actions"]
    assert any(a["action"] == "tier_change" and a["target_id"] == extra_user.id for a in actions)
