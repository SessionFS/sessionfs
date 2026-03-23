"""Integration tests for full-text search endpoint."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Session, User


def _make_sfs_tar(
    title: str = "Test session",
    source_tool: str = "claude-code",
    messages: list[dict] | None = None,
) -> bytes:
    """Create a .sfs tar.gz with custom content for search tests."""
    if messages is None:
        messages = [
            {"msg_id": "msg_001", "role": "user",
             "content": [{"type": "text", "text": "hello world"}]},
            {"msg_id": "msg_002", "role": "assistant",
             "content": [{"type": "text", "text": "Hi there, how can I help?"}]},
        ]

    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_test",
        "title": title,
        "source": {"tool": source_tool},
        "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        "stats": {"message_count": len(messages)},
    }).encode()

    messages_data = "\n".join(json.dumps(m) for m in messages).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(messages_data)
        tar.addfile(info, io.BytesIO(messages_data))

    return buf.getvalue()


async def _create_session_with_text(
    db: AsyncSession,
    user: User,
    *,
    session_id: str | None = None,
    title: str = "Test session",
    source_tool: str = "claude-code",
    messages_text: str = "",
    days_ago: int = 0,
) -> Session:
    """Create a session record with searchable text directly in the database."""
    sid = session_id or f"ses_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    session = Session(
        id=sid,
        user_id=user.id,
        title=title,
        tags="[]",
        source_tool=source_tool,
        model_id="claude-opus-4-6",
        message_count=5,
        messages_text=messages_text,
        blob_key=f"sessions/{user.id}/{sid}/session.tar.gz",
        blob_size_bytes=100,
        etag=hashlib.sha256(sid.encode()).hexdigest(),
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@pytest.mark.asyncio
async def test_search_returns_results(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search returns matching sessions."""
    await _create_session_with_text(
        db_session, test_user,
        title="Refactor authentication module",
        messages_text="We need to refactor the authentication module to use OAuth2 tokens",
    )
    await _create_session_with_text(
        db_session, test_user,
        title="Fix database migration",
        messages_text="The database migration is failing due to a missing column",
    )

    # Set user tier to pro for search access
    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "authentication"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["query"] == "authentication"
    assert len(data["results"]) >= 1
    # The matching session should be the auth one
    session_ids = [r["session_id"] for r in data["results"]]
    titles = [r["title"] for r in data["results"]]
    assert any("authentication" in (t or "").lower() or "auth" in (t or "").lower() for t in titles)


@pytest.mark.asyncio
async def test_search_with_tool_filter(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search with tool filter only returns sessions from that tool."""
    await _create_session_with_text(
        db_session, test_user,
        title="Debugging network issues",
        source_tool="claude-code",
        messages_text="debugging network connectivity issues with the server",
    )
    await _create_session_with_text(
        db_session, test_user,
        title="Debugging network config",
        source_tool="cursor",
        messages_text="debugging network configuration in the proxy settings",
    )

    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "debugging network", "tool": "cursor"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    for r in data["results"]:
        assert r["source_tool"] == "cursor"


@pytest.mark.asyncio
async def test_search_with_days_filter(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search with days filter only returns recent sessions."""
    await _create_session_with_text(
        db_session, test_user,
        title="Recent deployment fix",
        messages_text="fixing the deployment pipeline for production",
        days_ago=2,
    )
    await _create_session_with_text(
        db_session, test_user,
        title="Old deployment fix",
        messages_text="fixing the deployment pipeline for staging",
        days_ago=30,
    )

    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "deployment", "days": 7},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # Only the recent session should match
    assert data["total"] >= 1
    for r in data["results"]:
        assert "old" not in (r["title"] or "").lower()


@pytest.mark.asyncio
async def test_search_empty_results(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search with no matching content returns empty results."""
    await _create_session_with_text(
        db_session, test_user,
        title="Python project",
        messages_text="Working on a Python web application",
    )

    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "xyznonexistent123"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_free_tier_gets_403(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Free tier users get 403 when trying to search."""
    # Ensure user is on free tier (default)
    test_user.tier = "free"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "test query"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
    data = resp.json()
    # Custom error handler wraps as: {"error": {"code": "403", "details": {...}}}
    tier_error = data["error"]["details"]["error"]
    assert tier_error["code"] == "TIER_LIMIT"
    assert "Pro" in tier_error["message"]
    assert tier_error["required_tier"] == "pro"


@pytest.mark.asyncio
async def test_search_pro_tier_gets_results(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Pro tier users can search successfully."""
    await _create_session_with_text(
        db_session, test_user,
        title="Pro user session",
        messages_text="This session was created by a pro user working on features",
    )

    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "features"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["query"] == "features"


@pytest.mark.asyncio
async def test_search_pagination(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search supports pagination."""
    # Create multiple matching sessions
    for i in range(5):
        await _create_session_with_text(
            db_session, test_user,
            title=f"Pagination test session {i}",
            messages_text=f"pagination test content item {i} with searchable keywords",
        )

    test_user.tier = "pro"
    await db_session.commit()

    # Page 1 with page_size=2
    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "pagination", "page": 1, "page_size": 2},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["results"]) == 2
    assert data["total"] >= 5

    # Page 2
    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "pagination", "page": 2, "page_size": 2},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 2
    assert len(data["results"]) == 2


@pytest.mark.asyncio
async def test_search_result_schema(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Search results match the expected schema."""
    await _create_session_with_text(
        db_session, test_user,
        title="Schema validation session",
        source_tool="claude-code",
        messages_text="schema validation test content for checking response format",
    )

    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "schema validation"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "query" in data

    if data["results"]:
        result = data["results"][0]
        assert "session_id" in result
        assert "title" in result
        assert "source_tool" in result
        assert "model_id" in result
        assert "message_count" in result
        assert "updated_at" in result
        assert "matches" in result
        assert isinstance(result["matches"], list)


@pytest.mark.asyncio
async def test_search_sync_push_populates_messages_text(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
):
    """Verify that sync push extracts and stores messages_text."""
    tar_data = _make_sfs_tar(
        title="Sync search test",
        messages=[
            {"msg_id": "msg_001", "role": "user",
             "content": [{"type": "text", "text": "How do I implement binary search?"}]},
            {"msg_id": "msg_002", "role": "assistant",
             "content": [{"type": "text", "text": "Binary search works by dividing the array in half"}]},
        ],
    )

    resp = await client.put(
        "/api/v1/sessions/ses_srchsync12345678/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(tar_data), "application/gzip")},
    )
    assert resp.status_code == 201

    # Now search for it (set tier to pro)
    test_user.tier = "pro"
    await db_session.commit()

    resp = await client.get(
        "/api/v1/sessions/search",
        params={"q": "binary search"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    found = any(r["session_id"] == "ses_srchsync12345678" for r in data["results"])
    assert found, f"Expected ses_srchsync12345678 in results: {data['results']}"
