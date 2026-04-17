"""Integration tests for session CRUD operations."""

from __future__ import annotations

import io
import json

import pytest
from httpx import AsyncClient

from sessionfs.server.db.models import Session


@pytest.mark.asyncio
async def test_upload_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "My Session"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"].startswith("ses_")
    assert data["etag"]
    assert data["blob_size_bytes"] > 0


@pytest.mark.asyncio
async def test_list_sessions_empty(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_sessions_with_data(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    # Upload a session first
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["sessions"]) == 1


@pytest.mark.asyncio
async def test_list_sessions_filter_source_tool(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "codex"}
    )
    assert resp.json()["total"] == 0

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "claude-code"}
    )
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_get_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "Test"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["title"] == "Test"


@pytest.mark.asyncio
async def test_get_session_404(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions/ses_nonexistent12ab", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.get(f"/api/v1/sessions/{session_id}/download", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.content == sample_sfs_tar


@pytest.mark.asyncio
async def test_patch_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "Old Title"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        json={"title": "New Title", "tags": ["updated"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New Title"
    assert data["tags"] == ["updated"]


@pytest.mark.asyncio
async def test_delete_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Should be 404 after soft delete
    resp = await client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_404(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions/ses_nope12345678ab/download", headers=auth_headers)
    assert resp.status_code == 404
