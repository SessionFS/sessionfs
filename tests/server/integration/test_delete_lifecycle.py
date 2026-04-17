"""Integration tests for the session delete lifecycle (v0.9.9)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from sessionfs.server.db.models import Session


# ── Test 1: DELETE with scope=cloud sets correct fields + purge_after ──

@pytest.mark.asyncio
async def test_delete_scope_cloud(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
    db_session, test_user,
):
    """DELETE with scope=cloud sets is_deleted, deleted_at, deleted_by, delete_scope, purge_after."""
    # Upload a session
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    session_id = resp.json()["session_id"]

    # Delete with scope=cloud
    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_deleted"] is True
    assert data["delete_scope"] == "cloud"
    assert data["deleted_by"] == test_user.id
    assert data["purge_after"] is not None
    # purge_after should be ~30 days from now
    purge_str = data["purge_after"]
    if purge_str.endswith("Z"):
        purge_str = purge_str[:-1] + "+00:00"
    purge = datetime.fromisoformat(purge_str)
    if purge.tzinfo is None:
        purge = purge.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert (purge - now).days >= 29


# ── Test 2: DELETE with scope=everywhere sets correct fields ──

@pytest.mark.asyncio
async def test_delete_scope_everywhere(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
    db_session, test_user,
):
    """DELETE with scope=everywhere sets delete_scope to 'both'."""
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "everywhere"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_deleted"] is True
    assert data["delete_scope"] == "both"
    assert data["deleted_by"] == test_user.id
    assert data["purge_after"] is not None


# ── Test 3: DELETE without scope returns 400 ──

@pytest.mark.asyncio
async def test_delete_without_scope_returns_400(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ── Test 4: DELETE on already-deleted returns 410 ──

@pytest.mark.asyncio
async def test_delete_already_deleted_returns_410(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # First delete
    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Second delete
    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 410


# ── Test 5: Restore clears all delete fields ──

@pytest.mark.asyncio
async def test_restore_clears_delete_fields(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # Delete
    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )

    # Restore
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_deleted"] is False
    assert data["deleted_at"] is None
    assert data["deleted_by"] is None
    assert data["delete_scope"] is None
    assert data["purge_after"] is None


# ── Test 6: Restore on non-deleted returns 409 ──

@pytest.mark.asyncio
async def test_restore_non_deleted_returns_409(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    resp = await client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers=auth_headers,
    )
    assert resp.status_code == 409


# ── Test 7: Restore on purged (nonexistent) returns 410 ──

@pytest.mark.asyncio
async def test_restore_purged_returns_410(
    client: AsyncClient, auth_headers: dict,
):
    resp = await client.post(
        "/api/v1/sessions/ses_doesnotexist99/restore",
        headers=auth_headers,
    )
    assert resp.status_code == 410


# ── Test 8: Purge deletes expired rows + blobs ──

@pytest.mark.asyncio
async def test_purge_deletes_expired(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
    db_session, test_user,
):
    """Admin purge deletes rows where purge_after < now."""
    # Upload and soft-delete a session
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Set purge_after to the past so it's eligible for purge
    from sqlalchemy import update
    await db_session.execute(
        update(Session).where(Session.id == session_id).values(
            purge_after=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db_session.commit()

    # Make test user an admin
    test_user.tier = "admin"
    await db_session.commit()

    # Purge
    resp = await client.post(
        "/api/v1/admin/purge-deleted",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["purged"] == 1
    assert data["bytes_reclaimed"] > 0


# ── Test 9: Purge skips non-expired rows ──

@pytest.mark.asyncio
async def test_purge_skips_non_expired(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
    db_session, test_user,
):
    """Admin purge does not delete sessions still in retention window."""
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )

    # purge_after is 30 days from now — should NOT be purged
    test_user.tier = "admin"
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/purge-deleted",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0


# ── Test 10: List with deleted=true returns only soft-deleted sessions ──

@pytest.mark.asyncio
async def test_list_deleted_true(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    # Upload two sessions
    for _ in range(2):
        await client.post(
            "/api/v1/sessions",
            headers=auth_headers,
            params={"source_tool": "claude-code"},
            files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
        )

    # Get IDs
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    sessions = resp.json()["sessions"]
    assert len(sessions) == 2

    # Delete one
    session_id = sessions[0]["id"]
    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )

    # Normal list should show 1
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.json()["total"] == 1

    # Deleted list should show 1
    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"deleted": "true"},
    )
    assert resp.json()["total"] == 1
    assert resp.json()["sessions"][0]["id"] == session_id


# ── Test 11: List without deleted=true excludes soft-deleted sessions ──

@pytest.mark.asyncio
async def test_list_excludes_deleted(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # Delete it
    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )

    # Normal list should be empty
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.json()["total"] == 0


# ── Test 12: Storage check excludes soft-deleted sessions ──

@pytest.mark.asyncio
async def test_storage_excludes_deleted(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    """The sync status storage_used_bytes should not count deleted sessions."""
    # Upload a session
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # Get sync status — should show storage used
    resp = await client.get("/api/v1/sync/status", headers=auth_headers)
    if resp.status_code == 200:
        storage_before = resp.json().get("storage_used_bytes", 0)
        assert storage_before > 0

        # Delete the session
        await client.delete(
            f"/api/v1/sessions/{session_id}",
            headers=auth_headers,
            params={"scope": "cloud"},
        )

        # Storage should now be 0
        resp = await client.get("/api/v1/sync/status", headers=auth_headers)
        storage_after = resp.json().get("storage_used_bytes", 0)
        assert storage_after == 0


# ── Test 13: sync_push does NOT un-delete without explicit intent header ──

@pytest.mark.asyncio
async def test_sync_push_no_undelete_without_header(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    """Pushing to a deleted session without X-SessionFS-Undelete header returns 410."""
    # Upload via sync_push
    resp = await client.put(
        "/api/v1/sessions/ses_testundelete01/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201

    # Delete the session
    resp = await client.delete(
        "/api/v1/sessions/ses_testundelete01",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Push again without undelete header — should get 410
    resp = await client.put(
        "/api/v1/sessions/ses_testundelete01/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 410


# ── Test 13b: sync_push WITH X-SessionFS-Undelete header un-deletes ──

@pytest.mark.asyncio
async def test_sync_push_undelete_with_header(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    """Pushing with X-SessionFS-Undelete: true restores the session."""
    # Upload
    resp = await client.put(
        "/api/v1/sessions/ses_testundelete02/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201

    # Delete
    resp = await client.delete(
        "/api/v1/sessions/ses_testundelete02",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Push with undelete header
    undelete_headers = {**auth_headers, "X-SessionFS-Undelete": "true"}
    resp = await client.put(
        "/api/v1/sessions/ses_testundelete02/sync",
        headers=undelete_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 200

    # Session should now be visible in list
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    ids = [s["id"] for s in resp.json()["sessions"]]
    assert "ses_testundelete02" in ids


# ── Test 14: Restore after scope=cloud returns correct guidance fields ──

@pytest.mark.asyncio
async def test_restore_after_cloud_delete_guidance(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    """Restore after scope=cloud returns restored_from_scope='cloud' and local_copy_may_be_missing=false."""
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # Delete with scope=cloud
    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )

    # Restore
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored_from_scope"] == "cloud"
    assert data["local_copy_may_be_missing"] is False


# ── Test 15: Restore after scope=everywhere returns correct guidance fields ──

@pytest.mark.asyncio
async def test_restore_after_everywhere_delete_guidance(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
):
    """Restore after scope=everywhere returns restored_from_scope='both' and local_copy_may_be_missing=true."""
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = resp.json()["session_id"]

    # Delete with scope=everywhere (stored as 'both')
    await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "everywhere"},
    )

    # Restore
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/restore",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored_from_scope"] == "both"
    assert data["local_copy_may_be_missing"] is True
