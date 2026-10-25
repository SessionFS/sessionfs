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


# --- Message line numbers + ?around= (helper citation deep links) ---


def _archive_with_lines(lines: list[str]) -> bytes:
    """A .sfs archive whose messages.jsonl is exactly ``lines`` (one per line)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps({
            "sfs_version": "0.1.0",
            "session_id": "ses_test123",
            "title": "Around test",
            "source": {"tool": "claude-code"},
        }).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        body = ("\n".join(lines) + "\n").encode()
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _msg(n: int, **extra) -> str:
    return json.dumps({
        "msg_id": f"m{n}",
        "role": "user" if n % 2 == 0 else "assistant",
        "content": [{"type": "text", "text": f"message {n}"}],
        **extra,
    })


# Line 1 is blank, line 3 a sidechain entry, line 4 an empty message; every
# other line 0..24 is a visible message.
_AROUND_LINES = [
    _msg(0),
    "",
    _msg(2),
    _msg(3, is_sidechain=True),
    json.dumps({"msg_id": "m4", "role": "assistant", "content": []}),
    *[_msg(n) for n in range(5, 25)],
]
_VISIBLE = [0, 2, *range(5, 25)]


async def _upload(client: AsyncClient, headers: dict, sid: str, lines: list[str]) -> None:
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("session.tar.gz", io.BytesIO(_archive_with_lines(lines)), "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_messages_report_jsonl_line_numbers(client: AsyncClient, auth_headers: dict):
    """Each message carries the messages.jsonl line number the helper cites."""
    await _upload(client, auth_headers, "ses_lines1234abcdefg", _AROUND_LINES)
    resp = await client.get(
        "/api/v1/sessions/ses_lines1234abcdefg/messages?page_size=100", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message_indexes"] == _VISIBLE
    assert [m["msg_id"] for m in data["messages"]] == [f"m{n}" for n in _VISIBLE]
    assert data["target"] is None

    newest = (
        await client.get(
            "/api/v1/sessions/ses_lines1234abcdefg/messages?page_size=3&order=newest",
            headers=auth_headers,
        )
    ).json()
    assert newest["message_indexes"] == [24, 23, 22]


def test_message_line_numbers_match_helper_numbering():
    """The messages endpoint and the helper's archive reader number lines alike."""
    from sessionfs.server.routes.sessions import _extract_archive_content
    from sessionfs.server.services.session_archive import extract_archive

    data = _archive_with_lines(_AROUND_LINES)
    assert _extract_archive_content(data)["message_indexes"] == extract_archive(data)["message_indexes"]


@pytest.mark.asyncio
async def test_messages_around_returns_page_containing_target(
    client: AsyncClient, auth_headers: dict
):
    await _upload(client, auth_headers, "ses_around1234abcdef", _AROUND_LINES)
    base = "/api/v1/sessions/ses_around1234abcdef/messages?page_size=5"

    # Oldest-first pages: [0,2,5,6,7] [8,9,10,11,12] [13..17] ...
    data = (await client.get(f"{base}&around=11&page=1", headers=auth_headers)).json()
    assert data["page"] == 2
    assert data["message_indexes"] == [8, 9, 10, 11, 12]
    assert data["target"] == {"index": 11, "status": "found", "anchor_index": 11}

    # Newest-first counts from the end: [24..20] [19..15] ...
    data = (await client.get(f"{base}&around=16&order=newest", headers=auth_headers)).json()
    assert data["page"] == 2
    assert 16 in data["message_indexes"]
    assert data["target"]["status"] == "found"


@pytest.mark.asyncio
async def test_messages_around_hidden_and_missing_fall_back_to_nearest(
    client: AsyncClient, auth_headers: dict
):
    await _upload(client, auth_headers, "ses_aroundhid1234abc", _AROUND_LINES)
    base = "/api/v1/sessions/ses_aroundhid1234abc/messages?page_size=5"

    # Sidechain line 3: nearest visible are 2 and 5 → 2 (closer).
    data = (await client.get(f"{base}&around=3", headers=auth_headers)).json()
    assert data["target"] == {"index": 3, "status": "hidden", "anchor_index": 2}
    assert data["page"] == 1

    # Empty message on line 4: 5 is closer than 2.
    data = (await client.get(f"{base}&around=4", headers=auth_headers)).json()
    assert data["target"] == {"index": 4, "status": "hidden", "anchor_index": 5}

    # Blank line 1 is not a message at all; tie between 0 and 2 → earlier.
    data = (await client.get(f"{base}&around=1", headers=auth_headers)).json()
    assert data["target"] == {"index": 1, "status": "missing", "anchor_index": 0}

    # Past the end → the last message, on the last page.
    data = (await client.get(f"{base}&around=999", headers=auth_headers)).json()
    assert data["target"] == {"index": 999, "status": "missing", "anchor_index": 24}
    assert data["page"] == 5
    assert data["message_indexes"][-1] == 24


@pytest.mark.asyncio
async def test_messages_around_with_nothing_visible(client: AsyncClient, auth_headers: dict):
    await _upload(
        client, auth_headers, "ses_aroundnone1234ab", [_msg(0, is_sidechain=True)]
    )
    data = (
        await client.get(
            "/api/v1/sessions/ses_aroundnone1234ab/messages?around=0", headers=auth_headers
        )
    ).json()
    assert data["target"] == {"index": 0, "status": "hidden", "anchor_index": None}
    assert data["page"] == 1
    assert data["messages"] == []


@pytest.mark.asyncio
async def test_messages_around_validation_and_access(
    client: AsyncClient, auth_headers: dict, db_session
):
    """``around`` is validated and never bypasses the ownership check."""
    from datetime import datetime, timezone
    import uuid

    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey, User

    await _upload(client, auth_headers, "ses_aroundacl1234abc", _AROUND_LINES)
    resp = await client.get(
        "/api/v1/sessions/ses_aroundacl1234abc/messages?around=-1", headers=auth_headers
    )
    assert resp.status_code == 422

    other = User(
        id=str(uuid.uuid4()),
        email="other-around@example.com",
        display_name="Other",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(other)
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=other.id,
            key_hash=hash_api_key(raw),
            name="other",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    resp = await client.get(
        "/api/v1/sessions/ses_aroundacl1234abc/messages?around=2",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 404
