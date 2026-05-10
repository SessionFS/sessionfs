"""Integration tests for handoff endpoints."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Handoff


@pytest.fixture
async def pushed_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes) -> str:
    """Push a session and return its ID."""
    session_id = f"ses_handoff{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    return session_id


@pytest.mark.asyncio
async def test_create_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Create a handoff -> 201 with handoff ID."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "recipient@example.com",
            "message": "Please continue this task.",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("hnd_")
    assert data["session_id"] == pushed_session
    assert data["recipient_email"] == "recipient@example.com"
    assert data["message"] == "Please continue this task."
    assert data["status"] == "pending"
    assert data["sender_email"] == "test@example.com"
    assert data["session_title"] == "Test session title"
    assert data["session_tool"] == "claude-code"


@pytest.mark.asyncio
async def test_create_handoff_missing_session(
    client: AsyncClient, auth_headers: dict,
):
    """Creating a handoff for nonexistent session -> 404."""
    resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": "ses_doesnotexist1234",
            "recipient_email": "someone@example.com",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Get handoff details by ID."""
    # Create handoff first
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "recipient@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Get details (auth required — sender can view)
    resp = await client.get(f"/api/v1/handoffs/{handoff_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == handoff_id
    assert data["session_id"] == pushed_session


@pytest.mark.asyncio
async def test_get_handoff_not_found(client: AsyncClient, auth_headers: dict):
    """Get nonexistent handoff -> 404."""
    resp = await client.get("/api/v1/handoffs/hnd_nonexistent12345", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_claim_handoff(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Claim a handoff -> 200 with claimed status."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/handoffs/{handoff_id}/claim",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "claimed"


@pytest.mark.asyncio
async def test_claim_handoff_twice(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Claiming a handoff twice -> 409."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    resp = await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_inbox(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Inbox lists handoffs sent to the current user's email."""
    # Create a handoff to the test user's email
    await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )

    resp = await client.get("/api/v1/handoffs/inbox", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(h["recipient_email"] == "test@example.com" for h in data["handoffs"])


@pytest.mark.asyncio
async def test_inbox_finds_legacy_mixed_case_row_via_fallback(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user,
    pushed_session: str,
):
    """Codex perf-2 round 2 finding: inbox must surface legacy rows
    where recipient_email_normalized is NULL and the raw column has
    surrounding whitespace + mixed case. The OR-fallback predicate is
    what catches them.
    """
    legacy = Handoff(
        id=f"hnd_{uuid.uuid4().hex[:8]}",
        session_id=pushed_session,
        sender_id=test_user.id,
        # Mixed case + whitespace + NULL normalized — matches the
        # pre-migration-032 state of an existing row.
        recipient_email=f"  {test_user.email.upper()}  ",
        recipient_email_normalized=None,
        message="legacy",
        status="pending",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(legacy)
    await db_session.commit()

    resp = await client.get("/api/v1/handoffs/inbox", headers=auth_headers)
    assert resp.status_code == 200
    ids = {h["id"] for h in resp.json()["handoffs"]}
    assert legacy.id in ids, (
        "inbox must find legacy handoff with mixed-case + whitespace "
        "raw recipient_email when normalized column is NULL"
    )


@pytest.mark.asyncio
async def test_inbox_missing_sender_renders_unknown_not_viewer(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user,
    pushed_session: str,
):
    """Codex perf-2 round 2 finding: when the sender User row is gone,
    inbox must render sender_email='unknown', NOT the recipient's own
    email. The pre-perf-2 N+1 path returned 'unknown'; the batched path
    initially regressed to viewer_email — caught here.
    """
    ghost_sender_id = str(uuid.uuid4())  # No User row inserted for this id
    handoff = Handoff(
        id=f"hnd_{uuid.uuid4().hex[:8]}",
        session_id=pushed_session,
        sender_id=ghost_sender_id,
        recipient_email=test_user.email,
        recipient_email_normalized=test_user.email.lower(),
        message="from a ghost",
        status="pending",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(handoff)
    await db_session.commit()

    resp = await client.get("/api/v1/handoffs/inbox", headers=auth_headers)
    assert resp.status_code == 200
    matched = [h for h in resp.json()["handoffs"] if h["id"] == handoff.id]
    assert matched, "ghost-sender handoff should still be listed"
    assert matched[0]["sender_email"] == "unknown", (
        f"inbox must NOT use viewer email as sender fallback; got "
        f"{matched[0]['sender_email']!r} for ghost sender"
    )


@pytest.mark.asyncio
async def test_sent(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Sent lists handoffs created by the current user."""
    await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "other@example.com",
        },
    )

    resp = await client.get("/api/v1/handoffs/sent", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(h["recipient_email"] == "other@example.com" for h in data["handoffs"])


@pytest.mark.asyncio
async def test_expired_handoff_get(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
    db_session: AsyncSession,
):
    """Getting an expired handoff -> 410."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Manually expire the handoff
    result = await db_session.execute(
        __import__("sqlalchemy").select(Handoff).where(Handoff.id == handoff_id)
    )
    handoff = result.scalar_one()
    handoff.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    resp = await client.get(f"/api/v1/handoffs/{handoff_id}", headers=auth_headers)
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_expired_handoff_claim(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
    db_session: AsyncSession,
):
    """Claiming an expired handoff -> 410."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Manually expire
    result = await db_session.execute(
        __import__("sqlalchemy").select(Handoff).where(Handoff.id == handoff_id)
    )
    handoff = result.scalar_one()
    handoff.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    resp = await client.post(f"/api/v1/handoffs/{handoff_id}/claim", headers=auth_headers)
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_claimed_handoff_summary_returns_410(
    client: AsyncClient, auth_headers: dict, pushed_session: str,
):
    """Summary of a claimed handoff -> 410."""
    create_resp = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": pushed_session,
            "recipient_email": "test@example.com",
        },
    )
    handoff_id = create_resp.json()["id"]

    # Claim it
    claim_resp = await client.post(
        f"/api/v1/handoffs/{handoff_id}/claim",
        headers=auth_headers,
    )
    assert claim_resp.status_code == 200

    # Summary should now be blocked
    resp = await client.get(
        f"/api/v1/handoffs/{handoff_id}/summary",
        headers=auth_headers,
    )
    assert resp.status_code == 410
