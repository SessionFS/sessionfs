"""Integration tests for chunked, resumable session uploads (GitHub #91)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Session,
    SessionUpload,
    SessionUploadPart,
    User,
)
from sessionfs.server.routes.uploads import (
    _finalize_upload,
    _read_part_body,
    abort_expired_pending_uploads,
)
from sessionfs.server.storage.local import LocalBlobStore
from sessionfs.server.tiers import Tier


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _initiate(
    client: AsyncClient,
    auth_headers: dict,
    session_id: str,
    total_size: int,
    sha256: str,
    *,
    part_size: int | None = None,
    etag: str | None = None,
):
    headers = dict(auth_headers)
    if etag:
        headers["If-Match"] = f'"{etag}"'
    body: dict = {"total_size": total_size, "sha256": sha256}
    if part_size is not None:
        body["part_size"] = part_size
    return await client.post(
        f"/api/v1/sessions/{session_id}/uploads", headers=headers, json=body
    )


async def _put_part(
    client: AsyncClient,
    auth_headers: dict,
    upload_id: str,
    part_number: int,
    data: bytes,
    *,
    sha256: str | None = None,
):
    headers = {**auth_headers, "X-Part-SHA256": sha256 or _sha256(data)}
    return await client.put(
        f"/api/v1/uploads/{upload_id}/parts/{part_number}",
        headers=headers,
        content=data,
    )


@pytest.mark.asyncio
async def test_initiate_and_status(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    data = sample_sfs_tar
    resp = await _initiate(
        client, auth_headers, "ses_uploadinit123", len(data), _sha256(data)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["upload_id"].startswith("up_")
    assert body["part_size"] == 8 * 1024 * 1024
    assert body["part_count"] == 1
    assert body["expires_at"]

    st = await client.get(f"/api/v1/uploads/{body['upload_id']}", headers=auth_headers)
    assert st.status_code == 200
    status = st.json()
    assert status["status"] == "pending"
    assert status["part_count"] == 1
    assert status["received"] == []


@pytest.mark.asyncio
async def test_complete_happy_path(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    data = sample_sfs_tar
    session_id = "ses_uploadhappy1"
    init = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    upload_id = init.json()["upload_id"]

    put = await _put_part(client, auth_headers, upload_id, 1, data)
    assert put.status_code == 200, put.text

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers
    )
    assert comp.status_code == 201, comp.text
    body = comp.json()
    assert body["session_id"] == session_id
    assert body["etag"] == _sha256(data)

    st = await client.get(f"/api/v1/uploads/{upload_id}", headers=auth_headers)
    assert st.json()["status"] == "completed"

    # The assembled archive became a real session on the single-shot path.
    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=auth_headers)
    assert pull.status_code == 200
    assert pull.content == data


@pytest.mark.asyncio
async def test_parts_out_of_order_and_duplicate(client: AsyncClient, auth_headers: dict):
    data = b"x" * (12 * 1024 * 1024)
    part_size = 5 * 1024 * 1024
    init = await _initiate(
        client, auth_headers, "ses_uploadparts1", len(data), _sha256(data),
        part_size=part_size,
    )
    assert init.json()["part_count"] == 3
    upload_id = init.json()["upload_id"]

    parts = [data[i : i + part_size] for i in range(0, len(data), part_size)]
    # Out of order, then a duplicate of part 2 (idempotent overwrite).
    for n in (3, 1, 2):
        assert (await _put_part(client, auth_headers, upload_id, n, parts[n - 1])).status_code == 200
    assert (await _put_part(client, auth_headers, upload_id, 2, parts[1])).status_code == 200

    st = await client.get(f"/api/v1/uploads/{upload_id}", headers=auth_headers)
    assert st.json()["received"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_part_hash_mismatch_400(client: AsyncClient, auth_headers: dict):
    data = b"x" * (6 * 1024 * 1024)
    init = await _initiate(
        client, auth_headers, "ses_uploadbadhash", len(data), _sha256(data),
        part_size=5 * 1024 * 1024,
    )
    upload_id = init.json()["upload_id"]
    part = b"y" * (5 * 1024 * 1024)
    resp = await _put_part(
        client, auth_headers, upload_id, 1, part, sha256="0" * 64
    )
    assert resp.status_code == 400
    assert "part_hash_mismatch" in resp.text


@pytest.mark.asyncio
async def test_part_size_mismatch_400(client: AsyncClient, auth_headers: dict):
    data = b"x" * (6 * 1024 * 1024)
    init = await _initiate(
        client, auth_headers, "ses_uploadbadsize", len(data), _sha256(data),
        part_size=5 * 1024 * 1024,
    )
    upload_id = init.json()["upload_id"]
    wrong = b"z" * 1024  # far short of the expected 5 MiB
    resp = await _put_part(client, auth_headers, upload_id, 1, wrong)
    assert resp.status_code == 400
    assert "part_size_mismatch" in resp.text


@pytest.mark.asyncio
async def test_complete_sha256_mismatch_400(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    data = sample_sfs_tar
    init = await _initiate(
        client, auth_headers, "ses_uploadbadfull", len(data), _sha256(b"other bytes")
    )
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers
    )
    assert comp.status_code == 400
    assert "sha256_mismatch" in comp.text


@pytest.mark.asyncio
async def test_initiate_rejects_stale_if_match(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Finding #7: initiate runs the same cheap ETag check the push does, so a
    stale If-Match fails fast (409) before any part is staged."""
    data = sample_sfs_tar
    session_id = "ses_uploadconflict"
    created = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(data), "application/gzip")},
    )
    assert created.status_code == 201

    resp = await _initiate(
        client, auth_headers, session_id, len(data), _sha256(data), etag="0" * 64
    )
    assert resp.status_code == 409
    assert "etag_mismatch" in resp.text


@pytest.mark.asyncio
async def test_complete_uses_if_match_from_request(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Finding #1: complete takes If-Match from the COMPLETE request, not the
    value saved at initiate, so a stale initiate ETag can't loop forever."""
    data = sample_sfs_tar
    session_id = "ses_uploadreqif"
    created = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(data), "application/gzip")},
    )
    assert created.status_code == 201
    etag = _sha256(data)

    # Initiate with the CURRENT etag so it passes the finding-#7 check...
    init = await _initiate(
        client, auth_headers, session_id, len(data), _sha256(data), etag=etag
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    # ...then complete with a DIFFERENT If-Match. The complete-request value
    # wins: it mismatches the session and 409s. (Replaying the initiate-time
    # value would have matched and pushed.)
    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        headers={**auth_headers, "If-Match": '"' + "0" * 64 + '"'},
    )
    assert comp.status_code == 409
    assert "etag_mismatch" in comp.text


@pytest.mark.asyncio
async def test_complete_dlp_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    org = Organization(
        id=f"org_{_uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{_uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "block", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    session_id = "ses_uploaddlp123"
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
            "role": "user",
            "content": [{"type": "text", "text": 'password = "supersecretvalue123"'}],
        }) + "\n"
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()

    init = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers
    )
    assert comp.status_code == 403
    assert "dlp_blocked" in comp.text


@pytest.mark.asyncio
async def test_initiate_rejects_over_tier_limit(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    from sessionfs.server.routes.sessions import _sync_limit_for_tier

    limit = _sync_limit_for_tier(Tier.PRO)
    resp = await _initiate(
        client, auth_headers, "ses_uploadtoobig", limit + 1, "a" * 64
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_other_user_gets_404(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, sample_sfs_tar: bytes
):
    data = sample_sfs_tar
    init = await _initiate(
        client, auth_headers, "ses_uploadother", len(data), _sha256(data)
    )
    upload_id = init.json()["upload_id"]

    other = User(
        id=str(_uuid.uuid4()),
        email="other@example.com",
        display_name="Other",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(_uuid.uuid4()),
            user_id=other.id,
            key_hash=hash_api_key(raw),
            name="other-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {raw}"}

    assert (await client.get(f"/api/v1/uploads/{upload_id}", headers=other_headers)).status_code == 404
    assert (await _put_part(client, other_headers, upload_id, 1, data)).status_code == 404
    assert (await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=other_headers)).status_code == 404
    assert (await client.delete(f"/api/v1/uploads/{upload_id}", headers=other_headers)).status_code == 404


@pytest.mark.asyncio
async def test_abort_upload(
    client: AsyncClient,
    auth_headers: dict,
    sample_sfs_tar: bytes,
    blob_store: LocalBlobStore,
):
    data = sample_sfs_tar
    init = await _initiate(
        client, auth_headers, "ses_uploadabort1", len(data), _sha256(data)
    )
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    key = f"uploads/{upload_id}/part-00001"
    assert await blob_store.get(key) == data

    resp = await client.delete(f"/api/v1/uploads/{upload_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "aborted"

    st = await client.get(f"/api/v1/uploads/{upload_id}", headers=auth_headers)
    assert st.json()["status"] == "aborted"
    assert await blob_store.get(key) is None


@pytest.mark.asyncio
async def test_abort_expired_pending_uploads(
    db_session: AsyncSession, blob_store: LocalBlobStore, test_user: User
):
    now = datetime.now(timezone.utc)
    expired = SessionUpload(
        id="up_" + "a" * 24,
        user_id=test_user.id,
        session_id="ses_expired",
        total_size=100,
        sha256="b" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="pending",
        created_at=now - timedelta(hours=25),
        expires_at=now - timedelta(hours=1),
    )
    active = SessionUpload(
        id="up_" + "c" * 24,
        user_id=test_user.id,
        session_id="ses_active",
        total_size=100,
        sha256="d" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db_session.add(expired)
    db_session.add(active)
    await db_session.flush()
    db_session.add(
        SessionUploadPart(
            upload_id=expired.id, part_number=1, size=100, sha256="e" * 64
        )
    )
    await db_session.commit()

    key = f"uploads/{expired.id}/part-00001"
    await blob_store.put(key, b"x" * 100)

    aborted = await abort_expired_pending_uploads(db_session, blob_store, now)
    assert aborted == 1

    await db_session.refresh(expired)
    await db_session.refresh(active)
    assert expired.status == "aborted"
    assert active.status == "pending"

    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == expired.id)
    )
    assert parts.scalars().all() == []
    assert await blob_store.get(key) is None


@pytest.mark.asyncio
async def test_initiate_rejects_invalid_if_match_format(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Finding #5: an If-Match longer than 64 hex chars is rejected as 400
    instead of reaching the DB as a too-long string."""
    data = sample_sfs_tar
    resp = await _initiate(
        client, auth_headers, "ses_uploadifmatch", len(data), _sha256(data),
        etag="f" * 65,
    )
    assert resp.status_code == 400
    assert "invalid_if_match" in resp.text


@pytest.mark.asyncio
async def test_initiate_rejects_other_users_session(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """Finding #7: initiating on another user's session id is 409, like the push."""
    data = sample_sfs_tar
    other = User(
        id=str(_uuid.uuid4()),
        email="upload-other@example.com",
        display_name="Upload Other",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(other)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    session_id = "ses_othersu123"
    db_session.add(
        Session(
            id=session_id,
            user_id=other.id,
            title="Other's",
            tags="[]",
            source_tool="claude-code",
            blob_key="k",
            blob_size_bytes=len(data),
            etag=_sha256(data),
            created_at=now,
            updated_at=now,
            uploaded_at=now,
        )
    )
    await db_session.commit()

    resp = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    assert resp.status_code == 409
    assert "claimed by another user" in resp.text


@pytest.mark.asyncio
async def test_initiate_rejects_deleted_session(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
    sample_sfs_tar: bytes,
):
    """Finding #7: initiating on a deleted session is 410, like the push."""
    data = sample_sfs_tar
    now = datetime.now(timezone.utc)
    session_id = "ses_deleted123"
    db_session.add(
        Session(
            id=session_id,
            user_id=test_user.id,
            title="Deleted",
            tags="[]",
            source_tool="claude-code",
            blob_key="k",
            blob_size_bytes=len(data),
            etag=_sha256(data),
            created_at=now,
            updated_at=now,
            uploaded_at=now,
            is_deleted=True,
        )
    )
    await db_session.commit()

    resp = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_initiate_lazily_aborts_expired_pending_uploads(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    """Finding #2b: initiate aborts the caller's expired pending uploads, so
    their slots don't count against the pending cap."""
    now = datetime.now(timezone.utc)
    expired = SessionUpload(
        id="up_" + "e" * 24,
        user_id=test_user.id,
        session_id="ses_expired1",
        total_size=100,
        sha256="b" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="pending",
        created_at=now - timedelta(hours=25),
        expires_at=now - timedelta(hours=1),
    )
    db_session.add(expired)
    await db_session.flush()
    db_session.add(
        SessionUploadPart(upload_id=expired.id, part_number=1, size=100, sha256="c" * 64)
    )
    await db_session.commit()

    data = b"x" * 100
    resp = await _initiate(client, auth_headers, "ses_uploadlazy1", len(data), _sha256(data))
    assert resp.status_code == 201, resp.text

    await db_session.refresh(expired)
    assert expired.status == "aborted"
    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == expired.id)
    )
    assert parts.scalars().all() == []


@pytest.mark.asyncio
async def test_initiate_pending_cap_counts_only_active(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    """Finding #8: the pending cap counts only non-expired uploads (best-effort
    atomic under the per-user lock; asserted sequentially on SQLite)."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(
            SessionUpload(
                id=f"up_cap{i:024d}",
                user_id=test_user.id,
                session_id=f"ses_cap{i:08d}",
                total_size=100,
                sha256="a" * 64,
                part_size=5 * 1024 * 1024,
                part_count=1,
                status="pending",
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
    await db_session.commit()

    resp = await _initiate(
        client, auth_headers, "ses_capnew123", 100, _sha256(b"x" * 100)
    )
    assert resp.status_code == 409
    assert "too_many_pending_uploads" in resp.text


@pytest.mark.asyncio
async def test_get_reports_expired_for_overdue(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    """Finding #2c: GET reports `expired` for an overdue pending upload, so the
    client treats it as start-over without waiting for the sweeper."""
    now = datetime.now(timezone.utc)
    up = SessionUpload(
        id="up_" + "g" * 24,
        user_id=test_user.id,
        session_id="ses_getexp123",
        total_size=100,
        sha256="b" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="pending",
        created_at=now - timedelta(hours=25),
        expires_at=now - timedelta(hours=1),
    )
    db_session.add(up)
    await db_session.commit()

    st = await client.get(f"/api/v1/uploads/{up.id}", headers=auth_headers)
    assert st.status_code == 200
    assert st.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_complete_rejects_when_not_pending(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """Finding #3: the pending→completing claim must hit exactly one row, so a
    second (concurrent) complete is rejected instead of double-pushing."""
    data = sample_sfs_tar
    init = await _initiate(client, auth_headers, "ses_uploadcas1", len(data), _sha256(data))
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    # Simulate a concurrent complete that already claimed the row.
    await db_session.execute(
        update(SessionUpload).where(SessionUpload.id == upload_id).values(status="completing")
    )
    await db_session.commit()

    comp = await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert comp.status_code == 409
    assert "upload_not_pending" in comp.text


@pytest.mark.asyncio
async def test_abort_rejects_completing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """Finding #3: abort must not touch a `completing` upload mid-push."""
    data = sample_sfs_tar
    init = await _initiate(
        client, auth_headers, "ses_uploadabortcmp", len(data), _sha256(data)
    )
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    await db_session.execute(
        update(SessionUpload).where(SessionUpload.id == upload_id).values(status="completing")
    )
    await db_session.commit()

    resp = await client.delete(f"/api/v1/uploads/{upload_id}", headers=auth_headers)
    assert resp.status_code == 409
    assert "upload_completing" in resp.text


@pytest.mark.asyncio
async def test_complete_is_idempotent(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """Finding #4: a retried complete replays the stored push result without
    re-pushing (exactly one session is created)."""
    data = sample_sfs_tar
    session_id = "ses_uploadidem1"
    init = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    first = await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert first.status_code == 201, first.text
    assert first.json()["etag"] == _sha256(data)

    # Lost-response retry: same status code and stored result.
    second = await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert second.status_code == 201, second.text
    assert second.json()["etag"] == _sha256(data)
    assert second.json()["session_id"] == session_id

    count = await db_session.scalar(
        select(func.count()).select_from(Session).where(Session.id == session_id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_complete_without_if_match_on_existing_session_succeeds(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Round 3: complete mirrors single-shot exactly — with no If-Match the push
    is unconditional, so completing against an existing session succeeds (200)."""
    data = sample_sfs_tar
    session_id = "ses_uploadnoifm"
    created = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(data), "application/gzip")},
    )
    assert created.status_code == 201

    init = await _initiate(client, auth_headers, session_id, len(data), _sha256(data))
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert comp.status_code == 200, comp.text
    assert comp.json()["session_id"] == session_id
    assert comp.json()["etag"] == _sha256(data)


@pytest.mark.asyncio
async def test_complete_deleted_session_undelete_without_etag_succeeds(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
    sample_sfs_tar: bytes,
):
    """Round 3: complete mirrors single-shot exactly — a soft-deleted session
    restored via X-SessionFS-Undelete without If-Match succeeds (200)."""
    data = sample_sfs_tar
    now = datetime.now(timezone.utc)
    session_id = "ses_undelnoif"
    db_session.add(
        Session(
            id=session_id,
            user_id=test_user.id,
            title="Deleted",
            tags="[]",
            source_tool="claude-code",
            blob_key="k",
            blob_size_bytes=len(data),
            etag=_sha256(data),
            created_at=now,
            updated_at=now,
            uploaded_at=now,
            is_deleted=True,
        )
    )
    await db_session.commit()

    headers = {**auth_headers, "X-SessionFS-Undelete": "true"}
    init = await client.post(
        f"/api/v1/sessions/{session_id}/uploads",
        headers=headers,
        json={"total_size": len(data), "sha256": _sha256(data)},
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete", headers=headers
    )
    assert comp.status_code == 200, comp.text

    sess = await db_session.get(Session, session_id)
    assert sess is not None and sess.is_deleted is False


@pytest.mark.asyncio
async def test_complete_conflict_after_concurrent_push(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_sfs_tar: bytes,
):
    """Finding #1 (reviewer's scenario): a push between initiate and complete
    moves the etag, so the stale complete 409s instead of silently overwriting."""
    data = sample_sfs_tar
    session_id = "ses_uploadrace"
    created = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("s.tar.gz", io.BytesIO(data), "application/gzip")},
    )
    assert created.status_code == 201
    etag = _sha256(data)

    init = await _initiate(
        client, auth_headers, session_id, len(data), _sha256(data), etag=etag
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    # Another device pushes between initiate and complete, moving the etag.
    await db_session.execute(
        update(Session).where(Session.id == session_id).values(etag="0" * 64)
    )
    await db_session.commit()

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        headers={**auth_headers, "If-Match": f'"{etag}"'},
    )
    assert comp.status_code == 409
    assert "etag_mismatch" in comp.text


@pytest.mark.asyncio
async def test_initiate_undelete_header_allows_deleted_session(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user: User,
    sample_sfs_tar: bytes,
):
    """Finding #2: initiate honors X-SessionFS-Undelete: true like the
    single-shot push, so a soft-deleted session can be restored chunked."""
    data = sample_sfs_tar
    now = datetime.now(timezone.utc)
    session_id = "ses_undeleteup"
    db_session.add(
        Session(
            id=session_id,
            user_id=test_user.id,
            title="Deleted",
            tags="[]",
            source_tool="claude-code",
            blob_key="k",
            blob_size_bytes=len(data),
            etag=_sha256(data),
            created_at=now,
            updated_at=now,
            uploaded_at=now,
            is_deleted=True,
        )
    )
    await db_session.commit()

    # Without the header it is 410, matching the single-shot push.
    no_header = await _initiate(
        client, auth_headers, session_id, len(data), _sha256(data)
    )
    assert no_header.status_code == 410

    headers = {**auth_headers, "X-SessionFS-Undelete": "true"}
    init = await client.post(
        f"/api/v1/sessions/{session_id}/uploads",
        headers={**headers, "If-Match": f'"{_sha256(data)}"'},
        json={"total_size": len(data), "sha256": _sha256(data)},
    )
    assert init.status_code == 201, init.text
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        headers={**headers, "If-Match": f'"{_sha256(data)}"'},
    )
    # The session existed (soft-deleted), so this is an update/undelete → 200.
    assert comp.status_code == 200, comp.text

    sess = await db_session.get(Session, session_id)
    assert sess is not None and sess.is_deleted is False


@pytest.mark.asyncio
async def test_complete_semaphore_timeout_reverts_to_pending(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_sfs_tar: bytes,
    monkeypatch,
):
    """Finding #3: a 429 semaphore timeout during complete reverts the row to
    pending (not stuck completing), so a retry reuses the staged parts."""
    import sessionfs.server.routes.uploads as uploads_module

    async def _timeout(user_id, timer):
        raise HTTPException(
            429,
            detail={"error": "sync_rate_limit", "message": "Too many concurrent syncs."},
        )

    monkeypatch.setattr(uploads_module, "_acquire_user_sync_semaphore", _timeout)

    data = sample_sfs_tar
    init = await _initiate(
        client, auth_headers, "ses_upload429", len(data), _sha256(data)
    )
    upload_id = init.json()["upload_id"]
    assert (await _put_part(client, auth_headers, upload_id, 1, data)).status_code == 200

    comp = await client.post(f"/api/v1/uploads/{upload_id}/complete", headers=auth_headers)
    assert comp.status_code == 429

    row = await db_session.get(SessionUpload, upload_id)
    assert row is not None
    assert row.status == "pending"
    assert row.completing_since is None
    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == upload_id)
    )
    assert len(parts.scalars().all()) == 1


@pytest.mark.asyncio
async def test_initiate_reclaims_stale_completing_upload(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    """Finding #3: initiate lazily reclaims a completing row stuck past the
    grace window back to pending, so a retry can reuse its staged parts."""
    now = datetime.now(timezone.utc)
    stale = SessionUpload(
        id="up_" + "s" * 24,
        user_id=test_user.id,
        session_id="ses_stalecmp",
        total_size=100,
        sha256="b" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="completing",
        completing_since=now - timedelta(minutes=40),
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=23),
    )
    db_session.add(stale)
    await db_session.flush()
    db_session.add(
        SessionUploadPart(upload_id=stale.id, part_number=1, size=100, sha256="c" * 64)
    )
    await db_session.commit()

    resp = await _initiate(
        client, auth_headers, "ses_uploadlazy2", 100, _sha256(b"x" * 100)
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(stale)
    assert stale.status == "pending"
    assert stale.completing_since is None
    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == stale.id)
    )
    assert len(parts.scalars().all()) == 1


@pytest.mark.asyncio
async def test_abort_expired_pending_uploads_reclaims_stale_completing(
    db_session: AsyncSession, blob_store: LocalBlobStore, test_user: User
):
    """Finding #3: the sweeper reclaims stale completing rows — an expired one
    is aborted (parts dropped), a still-valid one reverts to pending."""
    now = datetime.now(timezone.utc)
    expired_cmp = SessionUpload(
        id="up_" + "x" * 24,
        user_id=test_user.id,
        session_id="ses_cmpexp",
        total_size=100,
        sha256="b" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="completing",
        completing_since=now - timedelta(minutes=40),
        created_at=now - timedelta(hours=25),
        expires_at=now - timedelta(hours=1),
    )
    valid_cmp = SessionUpload(
        id="up_" + "y" * 24,
        user_id=test_user.id,
        session_id="ses_cmpvalid",
        total_size=100,
        sha256="c" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="completing",
        completing_since=now - timedelta(minutes=40),
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=23),
    )
    db_session.add(expired_cmp)
    db_session.add(valid_cmp)
    await db_session.flush()
    db_session.add(
        SessionUploadPart(
            upload_id=expired_cmp.id, part_number=1, size=100, sha256="d" * 64
        )
    )
    db_session.add(
        SessionUploadPart(
            upload_id=valid_cmp.id, part_number=1, size=100, sha256="e" * 64
        )
    )
    await db_session.commit()

    key = f"uploads/{expired_cmp.id}/part-00001"
    await blob_store.put(key, b"x" * 100)

    aborted = await abort_expired_pending_uploads(db_session, blob_store, now)
    assert aborted == 1

    await db_session.refresh(expired_cmp)
    await db_session.refresh(valid_cmp)
    assert expired_cmp.status == "aborted"
    assert valid_cmp.status == "pending"
    assert valid_cmp.completing_since is None

    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == expired_cmp.id)
    )
    assert parts.scalars().all() == []
    assert await blob_store.get(key) is None


@pytest.mark.asyncio
async def test_stale_complete_finalize_fenced_out_by_completion_token(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    blob_store: LocalBlobStore,
):
    """Round 3: finalize/revert only apply while the row still carries the token
    this complete wrote at claim time. Gated simulation — the first complete
    claims the row, then a reclaim reverts it and a second complete re-claims it
    with a fresh token. When the stale first complete finally finalizes, its
    token no longer matches, so the update is fenced out: it must not overwrite
    the newer claimant's stored result or drop its staged parts."""
    now = datetime.now(timezone.utc)
    up = SessionUpload(
        id="up_" + "f" * 24,
        user_id=test_user.id,
        session_id="ses_fencefinal",
        total_size=100,
        sha256="a" * 64,
        part_size=5 * 1024 * 1024,
        part_count=1,
        status="completing",
        completing_since=now,
        completion_token="second-claim-token",
        result_etag="c" * 64,
        result_size=100,
        result_synced_at=now,
        result_created=True,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db_session.add(up)
    await db_session.flush()
    db_session.add(
        SessionUploadPart(upload_id=up.id, part_number=1, size=100, sha256="d" * 64)
    )
    await db_session.commit()

    # The stale first complete finalizes with its own (already replaced) token.
    await _finalize_upload(
        up.id,
        status="completed",
        created=False,
        result_etag="e" * 64,
        result_size=999,
        result_synced_at=now,
        blob_store=blob_store,
        part_numbers=[1],
        completion_token="stale-token",
    )

    await db_session.refresh(up)
    assert up.status == "completing"
    assert up.completion_token == "second-claim-token"
    assert up.result_etag == "c" * 64
    assert up.result_size == 100
    assert up.result_created is True
    parts = await db_session.execute(
        select(SessionUploadPart).where(SessionUploadPart.upload_id == up.id)
    )
    assert len(parts.scalars().all()) == 1


class _CountingStreamRequest:
    """Minimal stand-in for a Starlette Request used by ``_read_part_body``."""

    def __init__(self, content_length: str | None, chunks: list[bytes]):
        self.headers = {"content-length": content_length} if content_length else {}
        self._chunks = chunks
        self.bytes_read = 0

    async def stream(self):
        for chunk in self._chunks:
            self.bytes_read += len(chunk)
            yield chunk


@pytest.mark.asyncio
async def test_read_part_body_rejects_oversize_content_length_before_reading():
    """Finding #6: a 40 MiB Content-Length for a 100-byte part is rejected
    before a single byte is read."""
    req = _CountingStreamRequest("41943040", [b"x" * 100])
    with pytest.raises(HTTPException) as exc_info:
        await _read_part_body(req, 100)
    assert exc_info.value.status_code == 400
    assert req.bytes_read == 0


@pytest.mark.asyncio
async def test_read_part_body_stream_caps_at_expected_size():
    """Finding #6: the stream read hard-caps at the expected size even when the
    client omits Content-Length."""
    req = _CountingStreamRequest(None, [b"x" * 100, b"y"])
    with pytest.raises(HTTPException) as exc_info:
        await _read_part_body(req, 100)
    assert exc_info.value.status_code == 400
    assert req.bytes_read == 101
