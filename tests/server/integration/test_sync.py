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
async def test_sync_push_oversized_member_returns_413_for_paid_tier(
    client: AsyncClient, auth_headers: dict
):
    """Pushing an archive whose messages.jsonl exceeds the paid-tier 50 MB
    cap returns a structured 413 (not a 500 / ValueError from deep in DLP).

    The default test_user fixture is tier='pro', so the cap that applies
    here is the paid one. Free-tier behaviour is covered by the test
    below. Regression for the Baptist Health 57MB session bug.
    """
    archive = _build_oversized_archive(member_size=51 * 1024 * 1024)
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
    assert detail.get("size_bytes") == 51 * 1024 * 1024
    assert detail.get("limit_bytes") == 50 * 1024 * 1024
    assert "compact" in detail.get("suggestion", "").lower()


@pytest.mark.asyncio
async def test_sync_push_starter_tier_gets_paid_member_cap(
    client: AsyncClient, db_session
):
    """Starter is a paid plan (tk_59efad03840b4790): an 11 MB member that the
    10 MB free bucket would reject must sync under the paid 50 MB cap. Tier
    resolution runs through get_effective_tier so org membership inheritance
    is honoured.
    """
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from sessionfs.server.auth.keys import (
        generate_api_key as _gen_key,
        hash_api_key as _hash_key,
    )
    from sessionfs.server.db.models import ApiKey as _ApiKey, User as _User

    starter_user = _User(
        id=str(_uuid.uuid4()),
        email=f"starter_{_uuid.uuid4().hex[:8]}@example.com",
        tier="starter",
        email_verified=True,
        created_at=_dt.now(_tz.utc),
    )
    db_session.add(starter_user)
    await db_session.commit()
    raw = _gen_key()
    db_session.add(_ApiKey(
        id=str(_uuid.uuid4()),
        user_id=starter_user.id,
        key_hash=_hash_key(raw),
        name="starter-key",
        created_at=_dt.now(_tz.utc),
    ))
    await db_session.commit()
    starter_headers = {"Authorization": f"Bearer {raw}"}

    archive = _build_oversized_archive(member_size=11 * 1024 * 1024)
    resp = await client.put(
        "/api/v1/sessions/ses_startercap1234/sync",
        headers=starter_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:200]


@pytest.mark.asyncio
async def test_sync_push_at_limit_succeeds(
    client: AsyncClient, auth_headers: dict
):
    """A session well under the cap still goes through fine."""
    # 5MB messages.jsonl — well under both 10MB and 50MB caps.
    archive = _build_oversized_archive(member_size=5 * 1024 * 1024)
    resp = await client.put(
        "/api/v1/sessions/ses_atlimit1234abcd/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201), (
        f"Expected success at the limit, got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.asyncio
async def test_sync_push_paid_tier_at_30mb_succeeds(
    client: AsyncClient, auth_headers: dict
):
    """Pro tier should accept a 30 MB messages.jsonl — the new headroom
    the v0.9.9.7 bump was designed to give long Claude Code sessions."""
    # 30 MB messages.jsonl — comfortably above the old 10 MB cap, well
    # under the new 50 MB paid cap.
    archive = _build_oversized_archive(member_size=30 * 1024 * 1024)
    resp = await client.put(
        "/api/v1/sessions/ses_paid30mb1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201), (
        f"Pro tier should accept 30MB member, got {resp.status_code}: {resp.text[:200]}"
    )


# ---- /sync/settings: max_member_bytes exposure (tk_bb56f6a56da34b05) ----


@pytest.mark.asyncio
async def test_get_sync_settings_exposes_paid_tier_max_member_bytes(
    client: AsyncClient, auth_headers: dict
):
    """Paid-tier caller (test_user fixture is tier='pro') must see the
    50 MB paid-tier cap in `max_member_bytes`. Sourced from
    `_member_size_limit_for_tier()` so the field stays in sync if an
    operator overrides `SFS_MAX_SYNC_MEMBER_BYTES_PAID`.

    Parent Issue `tk_714456298d424202` — CLI relies on this field to
    drop its hardcoded 50MB fallback (Task 3).
    """
    from sessionfs.server.routes.sessions import SFS_MAX_SYNC_MEMBER_BYTES_PAID

    resp = await client.get("/api/v1/sync/settings", headers=auth_headers)
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert "max_member_bytes" in body, (
        f"GET /sync/settings response missing max_member_bytes: {body}"
    )
    assert body["max_member_bytes"] == SFS_MAX_SYNC_MEMBER_BYTES_PAID, (
        f"Paid tier should see paid-tier cap, got {body['max_member_bytes']} "
        f"expected {SFS_MAX_SYNC_MEMBER_BYTES_PAID}"
    )


@pytest.mark.asyncio
async def test_get_sync_settings_exposes_paid_max_member_bytes_for_starter(
    client: AsyncClient, db_session
):
    """Starter is the smallest paid plan, so `max_member_bytes` must report the
    paid per-member cap, not the 10 MB free bucket (tk_59efad03840b4790).
    """
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from sessionfs.server.auth.keys import (
        generate_api_key as _gen_key,
        hash_api_key as _hash_key,
    )
    from sessionfs.server.db.models import ApiKey as _ApiKey, User as _User
    from sessionfs.server.routes.sessions import SFS_MAX_SYNC_MEMBER_BYTES_PAID

    starter_user = _User(
        id=str(_uuid.uuid4()),
        email=f"starter_settings_{_uuid.uuid4().hex[:8]}@example.com",
        tier="starter",
        email_verified=True,
        created_at=_dt.now(_tz.utc),
    )
    db_session.add(starter_user)
    await db_session.commit()
    raw = _gen_key()
    db_session.add(_ApiKey(
        id=str(_uuid.uuid4()),
        user_id=starter_user.id,
        key_hash=_hash_key(raw),
        name="starter-settings-key",
        created_at=_dt.now(_tz.utc),
    ))
    await db_session.commit()
    starter_headers = {"Authorization": f"Bearer {raw}"}

    resp = await client.get("/api/v1/sync/settings", headers=starter_headers)
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body["max_member_bytes"] == SFS_MAX_SYNC_MEMBER_BYTES_PAID, (
        f"Starter tier should see the paid cap, got {body['max_member_bytes']} "
        f"expected {SFS_MAX_SYNC_MEMBER_BYTES_PAID}"
    )


@pytest.mark.asyncio
async def test_put_sync_settings_returns_max_member_bytes(
    client: AsyncClient, auth_headers: dict
):
    """PUT /sync/settings response shape must match GET so CLI callers
    can use either entry point to discover the cap.
    """
    from sessionfs.server.routes.sessions import SFS_MAX_SYNC_MEMBER_BYTES_PAID

    resp = await client.put(
        "/api/v1/sync/settings",
        headers=auth_headers,
        json={"mode": "off"},
    )
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body["max_member_bytes"] == SFS_MAX_SYNC_MEMBER_BYTES_PAID, (
        f"PUT should echo the paid-tier cap, got {body['max_member_bytes']}"
    )


# ---- NUL-byte stripping in archive-derived metadata (GH #67) ----


def _build_nul_archive(session_id: str) -> bytes:
    """Build a valid .sfs tar.gz whose manifest/messages/workspace carry NUL
    bytes (JSON ``\\u0000`` escapes, decoded to real ``\\x00`` on the server)
    in archive-derived fields. Reproduces the asyncpg
    ``CharacterNotInRepertoireError`` that made the daemon retry forever.
    """
    import json
    import tarfile

    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": session_id,
        "title": "NUL\x00Title",
        "source": {
            "tool": "claude\x00code",
            "tool_version": "1.0\x00.0",
            "original_session_id": "abc\x00123",
        },
        "model": {
            "provider": "anthropic\x00",
            "model_id": "claude-opus\x004-6",
        },
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()

    msg = json.dumps({
        "msg_id": "m1",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello\x00world"}],
        "timestamp": "2026-03-20T14:31:00Z",
    }).encode()

    workspace = json.dumps({
        "root_path": "/home/user/project",
        "git": {
            "remote_url": "https://github.com/example/repo",
            "branch": "ma\x00in",
            "commit_sha": "abc123",
        },
        "files": [{"path": "src/main.py", "role": "edited"}],
    }).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("manifest.json", manifest),
            ("messages.jsonl", msg + b"\n"),
            ("workspace.json", workspace),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_sync_push_nul_bytes_in_archive_metadata_stripped(
    client: AsyncClient, auth_headers: dict, db_session
):
    """GH #67: a pushed archive whose derived metadata contains NUL bytes must
    be ACCEPTED (2xx) with NULs stripped from the derived DB columns. The blob
    itself is preserved byte-for-byte (never sanitized in place)."""
    from sqlalchemy import select

    from sessionfs.server.db.models import Session

    session_id = "ses_nulmeta1234abcd"
    archive = _build_nul_archive(session_id)

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:300]

    # Derived metadata columns are NUL-free.
    detail = (await client.get(
        f"/api/v1/sessions/{session_id}", headers=auth_headers
    )).json()
    assert detail["source_tool"] == "claudecode"
    assert detail["source_tool_version"] == "1.0.0"
    assert detail["model_id"] == "claude-opus4-6"
    assert detail["model_provider"] == "anthropic"
    assert detail["original_session_id"] == "abc123"
    assert "\x00" not in (detail["title"] or "")

    # The internal search text column is also NUL-free (bound from the same
    # archive-derived extraction).
    row = (
        await db_session.execute(
            select(Session.messages_text).where(Session.id == session_id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert "\x00" not in row

    # The pushed blob is preserved untouched — only derived columns are
    # sanitized, never the archive itself.
    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=auth_headers)
    assert pull.status_code == 200
    assert pull.content == archive


@pytest.mark.asyncio
async def test_sync_push_dlp_redact_catches_a_quoted_secret_and_keeps_json_valid(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """GH #65 + review: with an org DLP policy in REDACT mode, a secret quoted
    inside message text (escaped quotes in the serialized JSONL, invisible to a
    raw-text scan) is found, redacted, and every stored line stays valid JSON."""
    import json
    import tarfile
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    session_id = "ses_dlpquoted1234ab"
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": session_id,
        "title": "dlp",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    messages = (
        json.dumps({
            "msg_id": "m1",
            "role": "assistant",
            "content": [{
                "type": "text",
                # Only a quoted secret: the raw scan of the serialized line sees
                # \"…\" and finds nothing, so only the decoded scan can catch it.
                "text": 'password = "supersecretvalue123"',
            }],
        }) + "\n"
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:300]

    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=auth_headers)
    assert pull.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(pull.content), mode="r:gz") as tar:
        stored = tar.extractfile("messages.jsonl").read().decode("utf-8")
    lines = [ln for ln in stored.splitlines() if ln.strip()]
    text = json.loads(lines[0])["content"][0]["text"]  # still valid JSON
    assert "supersecretvalue123" not in stored
    assert "[REDACTED:generic_password_assignment]" in text


@pytest.mark.asyncio
async def test_sync_push_dlp_refuses_content_too_deep_to_scan(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """A JSON member DLP cannot fully inspect must not be stored as clean."""
    import json
    import tarfile
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    session_id = "ses_dlpdeep12345678"
    manifest = json.dumps({
        "sfs_version": "0.1.0", "session_id": session_id, "title": "deep",
        "source": {"tool": "claude-code"}, "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    deep_line = ('{"msg_id":"m1","role":"user","content":' + "[" * 300 + '"x"' + "]" * 300 + "}\n").encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", deep_line)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
    )
    assert resp.status_code == 400, resp.text[:300]
    assert "dlp_uninspectable" in resp.text


async def _dlp_redact_org(db_session, test_user):
    import json
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()


def _sfs_archive(session_id: str, messages: bytes, tags: list[str] | None = None) -> bytes:
    import json
    import tarfile

    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": session_id,
        "title": "deploy",
        "tags": tags or [],
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_sync_push_dlp_redacts_manifest_tags_in_the_database(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """Session title/tags are read from the manifest; redaction must reach
    the database copy, not just the stored archive."""
    import json
    import tarfile

    from sqlalchemy import select

    from sessionfs.server.db.models import Session

    await _dlp_redact_org(db_session, test_user)
    session_id = "ses_dlptags12345678"
    good = json.dumps({"msg_id": "m1", "role": "user", "content": "hello"})
    archive = _sfs_archive(
        session_id, (good + "\n").encode(), tags=['password = "tagsecretvalue99"']
    )
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:300]

    row = (await db_session.execute(select(Session).where(Session.id == session_id))).scalar_one()
    await db_session.refresh(row)
    assert "tagsecretvalue99" not in (row.tags or "")
    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=auth_headers)
    with tarfile.open(fileobj=io.BytesIO(pull.content), mode="r:gz") as tar:
        assert "tagsecretvalue99" not in tar.extractfile("manifest.json").read().decode()


@pytest.mark.asyncio
async def test_sync_push_dlp_finds_quoted_secrets_in_malformed_lines(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    import json
    import tarfile

    await _dlp_redact_org(db_session, test_user)
    session_id = "ses_dlpmalformed123"
    good = json.dumps({"msg_id": "m1", "role": "user", "content": "hello"})
    malformed = '{"msg_id": "m2", "role": "user", "content": "password = \\"linesecretvalue99\\"",}'
    archive = _sfs_archive(session_id, (good + "\n" + malformed + "\n").encode())
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:300]
    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=auth_headers)
    with tarfile.open(fileobj=io.BytesIO(pull.content), mode="r:gz") as tar:
        stored = tar.extractfile("messages.jsonl").read().decode("utf-8")
    assert "linesecretvalue99" not in stored
    assert stored.splitlines()[0] == good  # the valid line is untouched


@pytest.mark.asyncio
async def test_sync_push_of_an_already_redacted_session_is_not_blocked(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """Genuine markers from an earlier redaction are not new findings, but a
    secret wrapped to look like a marker still is."""
    import json
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    redacted = json.dumps({"msg_id": "m1", "input": {"password": "[REDACTED:generic_password_assignment]"}})
    resp = await client.put(
        "/api/v1/sessions/ses_dlprepush12345a/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(_sfs_archive("ses_dlprepush12345a", (redacted + "\n").encode())), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text[:300]

    disguised = json.dumps({"msg_id": "m1", "content": '[REDACTED:password = "supersecret123"]'})
    resp = await client.put(
        "/api/v1/sessions/ses_dlprepush12345b/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(_sfs_archive("ses_dlprepush12345b", (disguised + "\n").encode())), "application/gzip")},
    )
    assert resp.status_code == 403, resp.text[:300]


@pytest.mark.asyncio
async def test_sync_push_blocks_a_secret_next_to_a_genuine_marker(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    import json
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    for i, value in enumerate((
        "[REDACTED:database_url]supersecret123",
        "supersecret123[REDACTED:database_url]",
    )):
        sid = f"ses_dlpadjacent123{i}"
        line = json.dumps({"msg_id": "m1", "input": {"password": value}})
        resp = await client.put(
            f"/api/v1/sessions/{sid}/sync",
            headers=auth_headers,
            files={"file": ("s.tar.gz", io.BytesIO(_sfs_archive(sid, (line + "\n").encode())), "application/gzip")},
        )
        assert resp.status_code == 403, (value, resp.text[:300])


@pytest.mark.asyncio
async def test_marker_collision_suffix_cannot_hide_a_numeric_secret(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    import json
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    sid = "ses_dlpsuffix123456"
    line = json.dumps({"msg_id": "m1", "content": 'password = "[REDACTED:database_url]#12345678"'})
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(_sfs_archive(sid, (line + "\n").encode())), "application/gzip")},
    )
    assert resp.status_code == 403, resp.text[:300]


@pytest.mark.asyncio
async def test_sync_dlp_counts_one_secret_once(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    import json
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    sid = "ses_dlpcountonce123"
    line = json.dumps({"msg_id": "m1", "input": {"api_key": "sk-proj-abcdefghijklmnopqrstuvwx1234"}})
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(_sfs_archive(sid, (line + "\n").encode())), "application/gzip")},
    )
    assert resp.status_code == 403, resp.text[:300]
    body = resp.json()
    detail = body.get("detail", body.get("error", {}).get("details", body.get("error", body)))
    if isinstance(detail, dict) and "detail" in detail:
        detail = detail["detail"]
    openai = [f for f in detail["findings"] if f["pattern"] == "openai_api_key"]
    assert len(openai) == 1, detail


@pytest.mark.asyncio
async def test_sync_dlp_refuses_unparseable_member_before_a_secret(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """A member that stops the parser (an integer past Python's digit limit)
    must not end the scan early and let a later member's secret through."""
    import json
    import tarfile
    import uuid as _uuid

    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    sid = "ses_dlpbigint123456"
    big = ('{"n": ' + "9" * 5000 + "}").encode()
    manifest = json.dumps({
        "sfs_version": "0.1.0", "session_id": sid, "title": "x",
        "source": {"tool": "claude-code"}, "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    secret_line = (json.dumps({"msg_id": "m1", "content": "key sk-proj-abcdefghijklmnopqrstuvwx1234"}) + "\n").encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("aaa.json", big), ("manifest.json", manifest), ("messages.jsonl", secret_line)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
    )
    assert resp.status_code in (400, 403), resp.text[:300]
    assert resp.status_code != 201
