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
async def test_list_sessions_filter_source_tool_alias_family(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "gemini-cli"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "gemini"}
    )
    assert resp.status_code == 200
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


@pytest.mark.asyncio
async def test_get_session_provenance(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """GET /api/v1/sessions/{id}/provenance returns the 4 provenance
    fields. Sessions captured before migration 028 (or with provenance
    capture disabled) return all-null fields rather than 404 — only
    nonexistent sessions 404."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert upload.status_code == 201
    session_id = upload.json()["session_id"]

    # Existing session, no provenance recorded (uploaded fresh) — should
    # return 200 with all-null fields and an empty artifacts list.
    resp = await client.get(
        f"/api/v1/sessions/{session_id}/provenance",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["rules_version"] is None
    assert data["rules_hash"] is None
    # rules_source defaults to DB literal "none" — surface as None
    assert data["rules_source"] is None
    assert data["instruction_artifacts"] == []

    # Nonexistent session → 404
    nf = await client.get(
        "/api/v1/sessions/ses_nonexistent12ab/provenance",
        headers=auth_headers,
    )
    assert nf.status_code == 404


@pytest.mark.asyncio
async def test_get_session_provenance_cross_user_404(
    client: AsyncClient,
    auth_headers: dict,
    sample_sfs_tar: bytes,
    db_session,
):
    """A user must not be able to read another user's session provenance.
    _get_user_session() filters by ownership and returns 404 (not 403) so
    the route does not leak the existence of sessions across user
    boundaries. Tier A added this endpoint, so we lock the behaviour in."""
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from sessionfs.server.auth.keys import (
        generate_api_key as _gen_key,
        hash_api_key as _hash_key,
    )
    from sessionfs.server.db.models import ApiKey as _ApiKey, User as _User

    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert upload.status_code == 201
    owned_session_id = upload.json()["session_id"]

    outsider = _User(
        id=str(_uuid.uuid4()),
        email=f"outsider_{_uuid.uuid4().hex[:8]}@example.com",
        tier="pro",
        email_verified=True,
        created_at=_dt.now(_tz.utc),
    )
    db_session.add(outsider)
    await db_session.commit()
    raw = _gen_key()
    db_session.add(_ApiKey(
        id=str(_uuid.uuid4()),
        user_id=outsider.id,
        key_hash=_hash_key(raw),
        name="outsider-key",
        created_at=_dt.now(_tz.utc),
    ))
    await db_session.commit()
    outsider_headers = {"Authorization": f"Bearer {raw}"}

    resp = await client.get(
        f"/api/v1/sessions/{owned_session_id}/provenance",
        headers=outsider_headers,
    )
    assert resp.status_code == 404, (
        f"Cross-user provenance must 404 (not leak existence), got "
        f"{resp.status_code}: {resp.text[:200]}"
    )
