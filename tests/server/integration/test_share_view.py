"""Integration tests for public share view endpoint (v0.14.0).

Tests: revoked/expired/bad token → 404, password flow, DLP gate blocks
seeded-secret session, caps enforced, no PII fields in response.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Session, ShareLink, User


# ── Helpers ──────────────────────────────────────────────────────────

def _make_tar(messages: list[dict] | None = None) -> bytes:
    """Create a minimal .sfs tar.gz with the given messages."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps({
            "sfs_version": "0.1.0",
            "title": "Share Test Session",
            "source": {"tool": "claude-code", "tool_version": "1.0.0"},
            "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
            "stats": {"message_count": len(messages) if messages else 0, "turn_count": 1},
        }).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        if messages:
            msgs_bytes = "\n".join(json.dumps(m) for m in messages).encode()
            info = tarfile.TarInfo(name="messages.jsonl")
            info.size = len(msgs_bytes)
            tar.addfile(info, io.BytesIO(msgs_bytes))

    return buf.getvalue()


async def _create_share_link(
    db_session: AsyncSession,
    blob_store,
    test_user: User,
    *,
    password: str | None = None,
    expires_delta: timedelta | None = None,
    revoked: bool = False,
    messages: list[dict] | None = None,
    dlp_checked: bool | None = None,
) -> tuple[ShareLink, Session]:
    """Create a session with blob and a share link, return both."""
    import hashlib

    if messages is None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        ]

    tar_data = _make_tar(messages)
    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    key = f"sessions/{test_user.id}/{session_id}/session.tar.gz"
    await blob_store.put(key, tar_data)

    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=test_user.id,
        title="Share Test Session",
        tags="[]",
        source_tool="claude-code",
        blob_key=key,
        blob_size_bytes=len(tar_data),
        etag=hashlib.sha256(tar_data).hexdigest(),
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db_session.add(session)

    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    link_id = str(uuid.uuid4())

    password_hash = None
    if password:
        import hashlib as _hashlib
        salt = _secrets.token_hex(16)
        dk = _hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        password_hash = f"{salt}${dk.hex()}"

    expires_at = now + (expires_delta or timedelta(hours=24))
    if expires_delta is not None and expires_delta.total_seconds() < 0:
        expires_at = now + expires_delta  # already in past

    link = ShareLink(
        id=link_id,
        session_id=session_id,
        user_id=test_user.id,
        token=token,
        expires_at=expires_at,
        password_hash=password_hash,
        is_revoked=revoked,
        dlp_checked=dlp_checked,
    )
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(session)
    await db_session.refresh(link)
    return link, session


# ── Basic access tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_returns_json_with_correct_fields(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """GET /share/{token}/view returns JSON with title, tool, messages, owner name."""
    link, _session = await _create_share_link(db_session, blob_store, test_user)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    data = resp.json()

    assert data["title"] == "Share Test Session"
    assert data["tool"] == "claude-code"
    assert "created_at" in data
    assert data["message_count"] == 2
    assert len(data["messages"]) == 2
    assert data["owner_display_name"] == "Test User"
    # Messages have content but NOT raw msg_id or internal ids
    assert "role" in data["messages"][0]
    assert "content" in data["messages"][0]


@pytest.mark.asyncio
async def test_view_bad_token_returns_404(client: AsyncClient):
    """Bad token returns 404."""
    resp = await client.get("/api/v1/share/bad-token-that-does-not-exist/view")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_view_revoked_link_returns_404(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Revoked share link returns constant 404 (leaks no info)."""
    link, _session = await _create_share_link(db_session, blob_store, test_user, revoked=True)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_view_expired_link_returns_404(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Expired share link returns constant 404."""
    link, _session = await _create_share_link(
        db_session, blob_store, test_user,
        expires_delta=timedelta(hours=-1),
    )

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 404


# ── Password flow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_password_link_get_returns_401(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """GET on a password-protected link returns 401 with 'Password required'."""
    link, _session = await _create_share_link(
        db_session, blob_store, test_user, password="secret123",
    )

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_view_password_link_post_with_correct_password(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """POST with correct password returns the view JSON."""
    link, _session = await _create_share_link(
        db_session, blob_store, test_user, password="secret123",
    )

    resp = await client.post(
        f"/api/v1/share/{link.token}/view",
        json={"password": "secret123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message_count"] == 2


@pytest.mark.asyncio
async def test_view_password_link_post_with_wrong_password(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """POST with wrong password returns 401."""
    link, _session = await _create_share_link(
        db_session, blob_store, test_user, password="secret123",
    )

    resp = await client.post(
        f"/api/v1/share/{link.token}/view",
        json={"password": "wrongpassword"},
    )
    assert resp.status_code == 401


# ── DLP gate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dlp_gate_blocks_seeded_secret(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """A session containing a real-looking API key is blocked by DLP (451)."""
    # Seed a message with a realistic secret pattern
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Here is my API key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Got it, I'll use that key."}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    # First access triggers DLP scan
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, f"Expected 451 DLP block, got {resp.status_code}: {resp.text}"

    # Verify dlp_checked is now False (blocked)
    await db_session.refresh(link)
    assert link.dlp_checked is False
    assert link.dlp_checked_at is not None


@pytest.mark.asyncio
async def test_dlp_gate_clean_session_passes(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """A clean session with no secrets passes DLP and is served."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "What is the capital of France?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "The capital of France is Paris."}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200

    # Verify dlp_checked is now True (clean)
    await db_session.refresh(link)
    assert link.dlp_checked is True


@pytest.mark.asyncio
async def test_dlp_cached_result_on_second_access(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Second access uses cached DLP result — no re-scan."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Hello world"}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    # First access — scans
    resp1 = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp1.status_code == 200
    await db_session.refresh(link)
    first_check_at = link.dlp_checked_at

    # Second access — uses cache
    resp2 = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp2.status_code == 200
    await db_session.refresh(link)
    # dlp_checked_at should NOT change (cache hit, no re-scan)
    assert link.dlp_checked_at == first_check_at


@pytest.mark.asyncio
async def test_dlp_blocked_persists(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Once blocked, subsequent accesses also get 451 (cached block)."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "API key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    # First access — DLP blocks
    resp1 = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp1.status_code == 451

    # Second access — DLP still blocks (cached)
    resp2 = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp2.status_code == 451


# ── Content cap enforcement ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_content_blocks_are_capped(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Text content blocks exceeding 8000 chars are capped."""
    long_text = "x" * 10_000
    messages = [
        {"role": "user", "content": [{"type": "text", "text": long_text}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    data = resp.json()
    msg_content = data["messages"][0]["content"]
    text = msg_content[0]["text"]
    assert len(text) <= 8000
    assert len(text) < len(long_text)


# ── No PII in response ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_response_contains_no_pii_fields(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Response must NOT contain session_id, org_id, or user email."""
    link, _session = await _create_share_link(db_session, blob_store, test_user)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    data = resp.json()
    body = json.dumps(data)

    # No internal IDs or emails in the response
    assert "session_id" not in data or data.get("session_id") is None
    assert "org_id" not in data
    assert "user_id" not in data
    assert "@" not in body  # No email addresses anywhere


@pytest.mark.asyncio
async def test_view_response_has_owner_display_name_only(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Owner display name is present, but nothing else about the owner."""
    link, _session = await _create_share_link(db_session, blob_store, test_user)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    data = resp.json()

    # Only display name, not email or internal IDs
    assert data["owner_display_name"] == "Test User"
    assert "owner_email" not in data
    assert "owner_id" not in data


@pytest.mark.asyncio
async def test_view_no_auth_required(client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User):
    """Share view endpoint is public — no auth headers needed."""
    link, _session = await _create_share_link(db_session, blob_store, test_user)

    # No auth headers at all
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200


# ── Tool result inclusion ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_includes_tool_results(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Tool results are included in the public view (full transcript)."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Run ls"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "content": "file1.py\nfile2.py"},
        ]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message_count"] == 3
    # Third message should be the tool result
    assert data["messages"][2]["role"] == "tool"


@pytest.mark.asyncio
async def test_dlp_blocks_secret_in_tool_use_input(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """H1 (Shield): tool_use INPUTS are exactly where secrets live in coding
    transcripts — a key inside a Bash command must trip the gate."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "t1",
             "input": {"command": "export AWS_KEY=AKIAIOSFODNN7EXAMPLE && deploy"}},
        ]},
    ]
    link, _s = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451


@pytest.mark.asyncio
async def test_dlp_blocks_secret_in_nested_tool_result(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """H1 (Shield): the standard nested-blocks tool_result shape must be
    scanned (the old text-builder skipped it entirely)."""
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text",
                          "text": "output: token AKIAIOSFODNN7EXAMPLE found"}]},
        ]},
    ]
    link, _s = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451


@pytest.mark.asyncio
async def test_dlp_verdict_invalidated_on_blob_repush(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """H2 (Shield): a clean verdict must not survive a blob re-push — the
    etag binding forces a re-scan, and new secrets flip the link to 451."""
    clean = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    link, session = await _create_share_link(db_session, blob_store, test_user, messages=clean)
    ok = await client.get(f"/api/v1/share/{link.token}/view")
    assert ok.status_code == 200
    await db_session.refresh(link)
    assert link.dlp_checked is True and link.dlp_checked_etag == session.etag

    # Simulate a sync re-push that adds a secret + changes the etag.
    dirty = clean + [{"role": "user", "content": [
        {"type": "text", "text": "my key: AKIAIOSFODNN7EXAMPLE"}]}]
    new_blob = _make_tar(dirty)
    await blob_store.put(session.blob_key, new_blob)
    session.etag = "repushed-etag-123"
    await db_session.commit()

    blocked = await client.get(f"/api/v1/share/{link.token}/view")
    assert blocked.status_code == 451
    await db_session.refresh(link)
    assert link.dlp_checked is False


@pytest.mark.asyncio
async def test_view_response_has_no_store_and_noindex_headers(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """M3 (Shield): a shared cache must never outlive revocation."""
    clean = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    link, _s = await _create_share_link(db_session, blob_store, test_user, messages=clean)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("x-robots-tag") == "noindex"


@pytest.mark.asyncio
async def test_dlp_gate_blocks_a_quoted_secret_inside_json(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """A quoted secret (password = "…") is hidden behind escaped quotes in the
    serialized JSON the gate scans; the decoded-text scan must still catch it."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": 'config: password = "supersecretvalue123"'}]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)

    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_blocks_a_secret_held_as_a_json_field(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    messages = [
        {"role": "assistant", "content": [{
            "type": "tool_use", "name": "connect", "input": {"password": "supersecretvalue123"},
        }]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_blocks_messages_it_cannot_fully_inspect(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """A messages.jsonl line too deep to parse must block the link, not be
    served as an empty transcript cached as clean."""
    deep = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    for _ in range(300):
        deep = {"n": deep}
    link, _session = await _create_share_link(
        db_session, blob_store, test_user, messages=[deep]
    )
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, resp.text


def test_dlp_gate_blocks_an_unreadable_message_line():
    """One malformed messages.jsonl line makes the lenient transcript reader
    return nothing; that must block the link, not cache it as clean."""
    from sessionfs.server.routes.sessions import _run_share_dlp_scan

    good = json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    payload = (good + "\n" + '{"role": "user", "content": [broken' + "\n").encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    passed, count = _run_share_dlp_scan(buf.getvalue())
    assert passed is False
    assert count >= 1

    clean = io.BytesIO()
    with tarfile.open(fileobj=clean, mode="w:gz") as tar:
        data = (good + "\n").encode()
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert _run_share_dlp_scan(clean.getvalue()) == (True, 0)


@pytest.mark.asyncio
async def test_dlp_gate_serves_an_already_redacted_session(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    messages = [
        {"role": "assistant", "content": [{
            "type": "tool_use", "name": "connect",
            "input": {"password": "[REDACTED:generic_password_assignment]"},
        }]},
    ]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_catches_a_secret_next_to_a_genuine_marker(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    for value in (
        "[REDACTED:database_url]supersecret123",
        "supersecret123[REDACTED:database_url]",
        "super[REDACTED:database_url]secret123xyz",
    ):
        messages = [{"role": "assistant", "content": [{
            "type": "tool_use", "name": "connect", "input": {"password": value},
        }]}]
        link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
        resp = await client.get(f"/api/v1/share/{link.token}/view")
        assert resp.status_code == 451, (value, resp.text)


@pytest.mark.asyncio
async def test_dlp_gate_recognises_the_owner_orgs_custom_markers(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {
            "enabled": True, "mode": "redact", "categories": ["secrets"],
            "custom_patterns": [{"name": "employee_code", "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}],
        }}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    messages = [{"role": "assistant", "content": [{
        "type": "tool_use", "name": "connect", "input": {"password": "[REDACTED:employee_code]"},
    }]}]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_recognises_custom_markers_after_dlp_is_disabled(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {
            "enabled": False, "mode": "redact", "categories": ["secrets"],
            "custom_patterns": [{"name": "employee_code", "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}],
        }}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    messages = [{"role": "assistant", "content": [{
        "type": "tool_use", "name": "connect", "input": {"password": "[REDACTED:employee_code]"},
    }]}]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_trusts_labels_recorded_with_the_session(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """The custom pattern that redacted a session may since have been renamed
    or the owner may have left that org; the labels recorded on the session
    when it was scanned still count."""
    messages = [{"role": "assistant", "content": [{
        "type": "tool_use", "name": "connect", "input": {"password": "[REDACTED:old_rule]"},
    }]}]
    link, session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    session.dlp_scan_results = json.dumps({"action_taken": "redact", "marker_labels": ["old_rule"]})
    await db_session.commit()
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_does_not_trust_a_marker_named_after_a_secret(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """Even if an org rule is named like a secret, a marker carrying that name
    is scanned rather than trusted."""
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    secret_name = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {
            "enabled": True, "mode": "redact", "categories": ["secrets"],
            "custom_patterns": [{"name": secret_name, "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}],
        }}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    messages = [{"role": "assistant", "content": [{"type": "text", "text": f"[REDACTED:{secret_name}]"}]}]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, resp.text


@pytest.mark.asyncio
async def test_dlp_gate_vets_owner_org_labels_against_their_own_rules(
    client: AsyncClient, db_session: AsyncSession, blob_store, test_user: User,
):
    """A rule whose name its own regex matches (EMP-12345 / EMP-\\d{5}) is not
    a safe label, so a marker carrying it is scanned rather than trusted."""
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {
            "enabled": True, "mode": "redact", "categories": ["secrets"],
            "custom_patterns": [{"name": "EMP-12345", "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}],
        }}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    messages = [{"role": "assistant", "content": [{
        "type": "text", "text": 'password = "[REDACTED:EMP-12345]"',
    }]}]
    link, _session = await _create_share_link(db_session, blob_store, test_user, messages=messages)
    resp = await client.get(f"/api/v1/share/{link.token}/view")
    assert resp.status_code == 451, resp.text
