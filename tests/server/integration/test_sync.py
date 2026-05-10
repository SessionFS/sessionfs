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


# ── Oversized session protection (10MB per-file cap) ──

def _build_oversized_archive(member_size: int = 11 * 1024 * 1024) -> bytes:
    """Construct a valid tar.gz with a single oversized messages.jsonl member.

    The contents are highly compressible (single repeated byte) so the wire
    payload stays tiny — but the in-archive uncompressed size is what
    _check_member_sizes inspects, so the rejection still fires.
    """
    import tarfile

    payload = b"x" * member_size
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # A minimal manifest so _extract_manifest_metadata doesn't choke.
        manifest = b'{"sfs_version": "0.1.0", "session_id": "ses_huge1234abcdef0"}'
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_sync_push_oversized_member_returns_413(
    client: AsyncClient, auth_headers: dict
):
    """Pushing an archive whose messages.jsonl exceeds 10MB returns a
    structured 413 (not a 500 / ValueError from deep in DLP).

    Regression for the Baptist Health 57MB session bug.
    """
    archive = _build_oversized_archive()
    resp = await client.put(
        "/api/v1/sessions/ses_huge1234abcdef0/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code == 413, (
        f"Expected 413 for oversized archive, got {resp.status_code}: {resp.text[:200]}"
    )

    body = resp.json()
    # FastAPI may wrap the detail in a custom error envelope. Normalise.
    detail = body.get("detail", body.get("error", {}).get("details", body.get("error", body)))
    if isinstance(detail, dict) and "detail" in detail:
        detail = detail["detail"]
    assert isinstance(detail, dict), f"Detail should be structured, got: {detail!r}"
    assert detail.get("error") == "session_too_large"
    assert detail.get("file") == "messages.jsonl"
    assert detail.get("size_bytes") == 11 * 1024 * 1024
    assert detail.get("limit_bytes") == 10 * 1024 * 1024
    assert "compact" in detail.get("suggestion", "").lower() or \
           "clear" in detail.get("suggestion", "").lower()


@pytest.mark.asyncio
async def test_sync_push_at_limit_succeeds(
    client: AsyncClient, auth_headers: dict
):
    """A session with each member <= 10MB still goes through fine."""
    # 5MB messages.jsonl — well under the 10MB cap.
    archive = _build_oversized_archive(member_size=5 * 1024 * 1024)
    resp = await client.put(
        "/api/v1/sessions/ses_atlimit1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201), (
        f"Expected success at the limit, got {resp.status_code}: {resp.text[:200]}"
    )
