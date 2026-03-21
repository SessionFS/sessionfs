"""Integration tests for messages, workspace, and tools endpoints."""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from httpx import AsyncClient


# --- Messages endpoint ---


@pytest.mark.asyncio
async def test_get_messages_returns_all_fields(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Messages endpoint returns structured message objects."""
    await client.put(
        "/api/v1/sessions/ses_msgfields1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_msgfields1234abc/messages",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["has_more"] is False

    msgs = data["messages"]
    assert len(msgs) == 3

    # First message is user
    assert msgs[0]["msg_id"] == "msg_001"
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["type"] == "text"

    # Second message is assistant with thinking + text + tool_use
    assert msgs[1]["msg_id"] == "msg_002"
    assert msgs[1]["role"] == "assistant"
    content_types = [b["type"] for b in msgs[1]["content"]]
    assert "thinking" in content_types
    assert "text" in content_types
    assert "tool_use" in content_types
    assert msgs[1]["model_used"] == "claude-opus-4-6"

    # Third message is tool result
    assert msgs[2]["msg_id"] == "msg_003"
    assert msgs[2]["role"] == "tool"


@pytest.mark.asyncio
async def test_messages_pagination(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Pagination slices messages correctly."""
    await client.put(
        "/api/v1/sessions/ses_msgpaginate1234a/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    # Page 1 with page_size=1
    resp = await client.get(
        "/api/v1/sessions/ses_msgpaginate1234a/messages?page=1&page_size=1",
        headers=auth_headers,
    )
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["total"] == 3
    assert data["has_more"] is True
    assert data["messages"][0]["msg_id"] == "msg_001"

    # Page 2 with page_size=1
    resp = await client.get(
        "/api/v1/sessions/ses_msgpaginate1234a/messages?page=2&page_size=1",
        headers=auth_headers,
    )
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["msg_id"] == "msg_002"
    assert data["has_more"] is True

    # Page 3 with page_size=1
    resp = await client.get(
        "/api/v1/sessions/ses_msgpaginate1234a/messages?page=3&page_size=1",
        headers=auth_headers,
    )
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["msg_id"] == "msg_003"
    assert data["has_more"] is False

    # Page 4 — beyond end
    resp = await client.get(
        "/api/v1/sessions/ses_msgpaginate1234a/messages?page=4&page_size=1",
        headers=auth_headers,
    )
    data = resp.json()
    assert len(data["messages"]) == 0
    assert data["has_more"] is False


@pytest.mark.asyncio
async def test_messages_page_size_clamped(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """page_size > 100 is rejected."""
    await client.put(
        "/api/v1/sessions/ses_msgclamp1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_msgclamp1234abcd/messages?page_size=200",
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_messages_invalid_page(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """page < 1 is rejected."""
    await client.put(
        "/api/v1/sessions/ses_msginvpage1234ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_msginvpage1234ab/messages?page=0",
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_messages_404_not_found(client: AsyncClient, auth_headers: dict):
    """Messages for a nonexistent session returns 404."""
    resp = await client.get(
        "/api/v1/sessions/ses_notexist1234abcd/messages",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_messages_requires_auth(client: AsyncClient, sample_sfs_tar: bytes):
    """Messages endpoint requires authentication."""
    resp = await client.get("/api/v1/sessions/ses_noauth1234abcdef/messages")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_messages_cached_on_second_request(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Second request for the same session uses the cache."""
    from sessionfs.server.routes.sessions import _archive_cache, clear_archive_cache

    clear_archive_cache()

    await client.put(
        "/api/v1/sessions/ses_cachetest1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    # First request populates cache
    resp1 = await client.get(
        "/api/v1/sessions/ses_cachetest1234abc/messages",
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    # Verify cache has an entry for this session
    cache_keys = list(_archive_cache.keys())
    matching = [k for k in cache_keys if k[0] == "ses_cachetest1234abc"]
    assert len(matching) == 1

    # Second request should still work (served from cache)
    resp2 = await client.get(
        "/api/v1/sessions/ses_cachetest1234abc/messages",
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()

    clear_archive_cache()


@pytest.mark.asyncio
async def test_messages_cache_invalidated_on_etag_change(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Cache entry is evicted when session etag changes (new push)."""
    from sessionfs.server.routes.sessions import _archive_cache, clear_archive_cache

    clear_archive_cache()

    # Push initial version
    resp = await client.put(
        "/api/v1/sessions/ses_cacheinval1234ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    etag1 = resp.json()["etag"]

    # Fetch messages (populates cache)
    await client.get(
        "/api/v1/sessions/ses_cacheinval1234ab/messages",
        headers=auth_headers,
    )
    keys_after_first = [k for k in _archive_cache if k[0] == "ses_cacheinval1234ab"]
    assert len(keys_after_first) == 1
    assert keys_after_first[0][1] == etag1

    # Push updated version
    new_manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_cacheinval1234ab",
        "title": "Updated",
    }).encode()
    new_msg = b'{"msg_id": "msg_new", "role": "user", "content": [{"type": "text", "text": "updated"}]}\n'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(new_manifest)
        tar.addfile(info, io.BytesIO(new_manifest))
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(new_msg)
        tar.addfile(info, io.BytesIO(new_msg))
    new_data = buf.getvalue()

    await client.put(
        "/api/v1/sessions/ses_cacheinval1234ab/sync",
        headers={**auth_headers, "If-Match": f'"{etag1}"'},
        files={"file": ("session.tar.gz", io.BytesIO(new_data), "application/gzip")},
    )

    # Fetch messages again — should get new data with new cache key
    resp2 = await client.get(
        "/api/v1/sessions/ses_cacheinval1234ab/messages",
        headers=auth_headers,
    )
    data2 = resp2.json()
    assert data2["total"] == 1
    assert data2["messages"][0]["msg_id"] == "msg_new"

    clear_archive_cache()


# --- Workspace endpoint ---


@pytest.mark.asyncio
async def test_get_workspace(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Workspace endpoint returns parsed workspace.json."""
    await client.put(
        "/api/v1/sessions/ses_workspace1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_workspace1234abc/workspace",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    ws = data["workspace"]
    assert ws["root_path"] == "/home/user/project"
    assert ws["git"]["branch"] == "main"
    assert ws["git"]["remote_url"] == "https://github.com/example/repo"
    assert len(ws["files"]) == 1


@pytest.mark.asyncio
async def test_workspace_404_no_file(client: AsyncClient, auth_headers: dict):
    """Workspace returns 404 when archive has no workspace.json."""
    # Create a tar with only manifest
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = b'{"sfs_version": "0.1.0"}'
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    minimal_tar = buf.getvalue()

    await client.put(
        "/api/v1/sessions/ses_nows1234abcdefgh/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(minimal_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_nows1234abcdefgh/workspace",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_workspace_404_session_not_found(client: AsyncClient, auth_headers: dict):
    """Workspace for nonexistent session returns 404."""
    resp = await client.get(
        "/api/v1/sessions/ses_nowsession1234ab/workspace",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_workspace_requires_auth(client: AsyncClient):
    """Workspace endpoint requires authentication."""
    resp = await client.get("/api/v1/sessions/ses_noauthws1234abcd/workspace")
    assert resp.status_code == 401


# --- Tools endpoint ---


@pytest.mark.asyncio
async def test_get_tools(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Tools endpoint returns parsed tools.json."""
    await client.put(
        "/api/v1/sessions/ses_toolstest1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_toolstest1234abc/tools",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    tools = data["tools"]
    assert tools["working_directory"] == "/home/user/project"
    assert tools["default_shell"] == "/bin/bash"
    assert len(tools["tools"]) == 2
    assert tools["tools"][0]["name"] == "bash"


@pytest.mark.asyncio
async def test_tools_404_no_file(client: AsyncClient, auth_headers: dict):
    """Tools returns 404 when archive has no tools.json."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = b'{"sfs_version": "0.1.0"}'
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    minimal_tar = buf.getvalue()

    await client.put(
        "/api/v1/sessions/ses_notool1234abcdef/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(minimal_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_notool1234abcdef/tools",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tools_404_session_not_found(client: AsyncClient, auth_headers: dict):
    """Tools for nonexistent session returns 404."""
    resp = await client.get(
        "/api/v1/sessions/ses_notoolsess1234ab/tools",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tools_requires_auth(client: AsyncClient):
    """Tools endpoint requires authentication."""
    resp = await client.get("/api/v1/sessions/ses_noauthtool1234ab/tools")
    assert resp.status_code == 401


# --- Cross-endpoint cache sharing ---


@pytest.mark.asyncio
async def test_messages_workspace_tools_share_cache(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """All three endpoints share the same cache — archive is parsed once."""
    from sessionfs.server.routes.sessions import _archive_cache, clear_archive_cache

    clear_archive_cache()

    await client.put(
        "/api/v1/sessions/ses_sharecache1234ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    # First call: messages — populates cache
    resp1 = await client.get(
        "/api/v1/sessions/ses_sharecache1234ab/messages", headers=auth_headers
    )
    assert resp1.status_code == 200
    cache_size_after_messages = len(_archive_cache)

    # Second call: workspace — reuses cache
    resp2 = await client.get(
        "/api/v1/sessions/ses_sharecache1234ab/workspace", headers=auth_headers
    )
    assert resp2.status_code == 200
    assert len(_archive_cache) == cache_size_after_messages  # No new entry

    # Third call: tools — reuses cache
    resp3 = await client.get(
        "/api/v1/sessions/ses_sharecache1234ab/tools", headers=auth_headers
    )
    assert resp3.status_code == 200
    assert len(_archive_cache) == cache_size_after_messages  # Still no new entry

    clear_archive_cache()
