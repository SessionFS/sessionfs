"""Integration tests for session aliases."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_set_alias(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Set an alias on a session."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "auth-debug"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["alias"] == "auth-debug"
    assert data["id"] == session_id


@pytest.mark.asyncio
async def test_alias_in_list(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Alias appears in session list."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "my-alias"},
    )

    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["alias"] == "my-alias"


@pytest.mark.asyncio
async def test_resolve_by_alias(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Resolve a session by its alias instead of ID."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "my-session"},
    )

    # GET by alias
    resp = await client.get("/api/v1/sessions/my-session", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == session_id
    assert resp.json()["alias"] == "my-session"


@pytest.mark.asyncio
async def test_clear_alias(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Clear an alias."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "temp-alias"},
    )

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["alias"] is None

    # Should no longer resolve by alias
    resp = await client.get("/api/v1/sessions/temp-alias", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_alias_uniqueness(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Two sessions cannot share the same alias for one user."""
    upload1 = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid1 = upload1.json()["session_id"]

    upload2 = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid2 = upload2.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{sid1}/alias",
        headers=auth_headers,
        json={"alias": "unique-name"},
    )

    resp = await client.put(
        f"/api/v1/sessions/{sid2}/alias",
        headers=auth_headers,
        json={"alias": "unique-name"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_alias_validation_too_short(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Alias must be at least 3 characters."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "ab"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_alias_validation_bad_chars(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Alias cannot contain spaces or special characters."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "has spaces"},
    )
    assert resp.status_code == 422

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "has@special!"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_alias_update(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Can change an existing alias."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "old-name"},
    )

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "new-name"},
    )
    assert resp.status_code == 200
    assert resp.json()["alias"] == "new-name"

    # Old alias should no longer resolve
    resp = await client.get("/api/v1/sessions/old-name", headers=auth_headers)
    assert resp.status_code == 404

    # New alias should resolve
    resp = await client.get("/api/v1/sessions/new-name", headers=auth_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_alias_via_patch(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Alias can also be set via PATCH metadata update."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        json={"alias": "patched-alias"},
    )
    assert resp.status_code == 200
    assert resp.json()["alias"] == "patched-alias"


@pytest.mark.asyncio
async def test_alias_resolve_in_messages(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Can use alias to fetch messages endpoint."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    await client.put(
        f"/api/v1/sessions/{session_id}/alias",
        headers=auth_headers,
        json={"alias": "msg-test"},
    )

    resp = await client.get("/api/v1/sessions/msg-test/messages", headers=auth_headers)
    assert resp.status_code == 200
    assert "messages" in resp.json()
