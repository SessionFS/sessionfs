"""Integration tests for sync push/pull endpoints."""

from __future__ import annotations

import hashlib
import io

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_sync_push_new_session(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Push to a new session ID -> 201."""
    resp = await client.put(
        "/api/v1/sessions/ses_newsync1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"] == "ses_newsync1234abcd"
    assert data["etag"] == hashlib.sha256(sample_sfs_tar).hexdigest()


@pytest.mark.asyncio
async def test_sync_push_update(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Push update with matching If-Match -> 200."""
    # First push (create)
    resp = await client.put(
        "/api/v1/sessions/ses_update1234abcdef/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    etag = resp.json()["etag"]

    # Second push with If-Match — create a new valid tar.gz with different content
    new_manifest = b'{"sfs_version": "0.1.0", "session_id": "ses_update1234abcdef", "updated": true}'
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(new_manifest)
        tar.addfile(info, io.BytesIO(new_manifest))
    new_data = buf.getvalue()

    resp = await client.put(
        "/api/v1/sessions/ses_update1234abcdef/sync",
        headers={**auth_headers, "If-Match": f'"{etag}"'},
        files={"file": ("session.tar.gz", io.BytesIO(new_data), "application/gzip")},
    )
    assert resp.status_code == 200
    assert resp.json()["etag"] == hashlib.sha256(new_data).hexdigest()


@pytest.mark.asyncio
async def test_sync_push_conflict_409(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Push with wrong If-Match -> 409."""
    # Create
    await client.put(
        "/api/v1/sessions/ses_conflict1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    # Push with wrong ETag
    resp = await client.put(
        "/api/v1/sessions/ses_conflict1234abcd/sync",
        headers={**auth_headers, "If-Match": '"wrong_etag"'},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_sync_pull(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Pull an existing session -> 200 with blob."""
    await client.put(
        "/api/v1/sessions/ses_pulltest1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get("/api/v1/sessions/ses_pulltest1234abcd/sync", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == sample_sfs_tar
    assert "etag" in resp.headers


@pytest.mark.asyncio
async def test_sync_pull_304_not_modified(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Pull with matching If-None-Match -> 304."""
    push_resp = await client.put(
        "/api/v1/sessions/ses_cached1234abcdef/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    etag = push_resp.json()["etag"]

    resp = await client.get(
        "/api/v1/sessions/ses_cached1234abcdef/sync",
        headers={**auth_headers, "If-None-Match": f'"{etag}"'},
    )
    assert resp.status_code == 304


@pytest.mark.asyncio
async def test_sync_pull_after_update(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Pull after an update returns the new data."""
    # Create
    resp = await client.put(
        "/api/v1/sessions/ses_updated1234abcde/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    old_etag = resp.json()["etag"]

    # Create a different valid tar.gz
    import tarfile
    new_manifest = b'{"sfs_version": "0.1.0", "session_id": "ses_updated1234abcde", "v": 2}'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(new_manifest)
        tar.addfile(info, io.BytesIO(new_manifest))
    new_data = buf.getvalue()

    # Update
    await client.put(
        "/api/v1/sessions/ses_updated1234abcde/sync",
        headers={**auth_headers, "If-Match": f'"{old_etag}"'},
        files={"file": ("session.tar.gz", io.BytesIO(new_data), "application/gzip")},
    )

    # Pull should get new data
    resp = await client.get("/api/v1/sessions/ses_updated1234abcde/sync", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.content == new_data


@pytest.mark.asyncio
async def test_sync_push_extracts_metadata(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Push extracts title, source_tool, model, stats from manifest.json."""
    resp = await client.put(
        "/api/v1/sessions/ses_metadata1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201

    # Fetch session detail and verify metadata was extracted
    detail_resp = await client.get(
        "/api/v1/sessions/ses_metadata1234abcd", headers=auth_headers
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()

    assert detail["title"] == "Test session title"
    assert detail["source_tool"] == "claude-code"
    assert detail["source_tool_version"] == "1.0.0"
    assert detail["model_id"] == "claude-opus-4-6"
    assert detail["model_provider"] == "anthropic"
    assert detail["original_session_id"] == "abc-123-def"
    assert detail["message_count"] == 5
    assert detail["turn_count"] == 3
    assert detail["tool_use_count"] == 2
    assert detail["total_input_tokens"] == 1500
    assert detail["total_output_tokens"] == 800
    assert detail["duration_ms"] == 45000
    assert detail["tags"] == ["test", "fixture"]


@pytest.mark.asyncio
async def test_sync_push_update_refreshes_metadata(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Push update also refreshes metadata from the new archive."""
    import tarfile

    # First push
    resp = await client.put(
        "/api/v1/sessions/ses_metaupdate1234ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    etag = resp.json()["etag"]

    # Push an update with different metadata
    import json
    updated_manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_metaupdate1234ab",
        "title": "Updated title",
        "source": {"tool": "claude-code", "tool_version": "2.0.0"},
        "model": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        "stats": {"message_count": 20, "turn_count": 10},
    }).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(updated_manifest)
        tar.addfile(info, io.BytesIO(updated_manifest))
    new_data = buf.getvalue()

    resp = await client.put(
        "/api/v1/sessions/ses_metaupdate1234ab/sync",
        headers={**auth_headers, "If-Match": f'"{etag}"'},
        files={"file": ("session.tar.gz", io.BytesIO(new_data), "application/gzip")},
    )
    assert resp.status_code == 200

    # Verify metadata was updated
    detail_resp = await client.get(
        "/api/v1/sessions/ses_metaupdate1234ab", headers=auth_headers
    )
    detail = detail_resp.json()
    assert detail["title"] == "Updated title"
    assert detail["source_tool_version"] == "2.0.0"
    assert detail["model_id"] == "claude-sonnet-4-6"
    assert detail["message_count"] == 20


@pytest.mark.asyncio
async def test_get_session_messages(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Get paginated messages from a pushed session."""
    await client.put(
        "/api/v1/sessions/ses_messages1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_messages1234abcd/messages",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data
    assert data["total"] >= 1
    assert data["page"] == 1
    assert isinstance(data["messages"], list)


@pytest.mark.asyncio
async def test_get_session_messages_pagination(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Messages endpoint respects pagination params."""
    await client.put(
        "/api/v1/sessions/ses_msgpage1234abcde/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_msgpage1234abcde/messages?page=1&page_size=1",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) <= 1
    assert data["page_size"] == 1


@pytest.mark.asyncio
async def test_admin_reindex(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Reindex endpoint re-extracts metadata from stored archives."""
    # Push a session
    await client.put(
        "/api/v1/sessions/ses_reindex1234abcde/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    # Verify it has metadata already (from push)
    detail = (await client.get(
        "/api/v1/sessions/ses_reindex1234abcde", headers=auth_headers
    )).json()
    assert detail["source_tool"] == "claude-code"

    # Reindex
    resp = await client.post("/api/v1/sessions/admin/reindex", headers=auth_headers)
    assert resp.status_code == 200
    result = resp.json()
    assert result["reindexed"] >= 1
    assert result["updated"] >= 1
    assert result["errors"] == 0

    # Verify metadata is still correct after reindex
    detail = (await client.get(
        "/api/v1/sessions/ses_reindex1234abcde", headers=auth_headers
    )).json()
    assert detail["source_tool"] == "claude-code"
    assert detail["message_count"] == 5
    assert detail["model_id"] == "claude-opus-4-6"
