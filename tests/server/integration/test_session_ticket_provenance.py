"""v0.10.1 Phase 6 — session upload propagates persona_name + ticket_id.

The watcher annotates `manifest.json` with `persona_name` and `ticket_id`
when the user is working under a ticket. The server-side metadata
extractor and Session constructor must pick those up so the `sessions`
table is queryable by ticket and persona.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Session, User


async def _make_user(db: AsyncSession) -> tuple[User, dict[str, str]]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Alice",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="alice-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


def _tar_with_manifest(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _manifest(**overrides) -> dict:
    base = {
        "sfs_version": "0.1.0",
        "session_id": "ses_x",
        "title": "Phase 6 test",
        "tags": [],
        "source": {"tool": "claude-code"},
        "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        "stats": {"message_count": 1},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_session_upload_propagates_ticket_and_persona(
    client: AsyncClient, db_session: AsyncSession
):
    user, headers = await _make_user(db_session)
    sid = f"ses_{uuid.uuid4().hex[:16]}"
    archive = _tar_with_manifest(
        _manifest(session_id=sid, ticket_id="tk_42", persona_name="atlas")
    )
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text

    row = (
        await db_session.execute(select(Session).where(Session.id == sid))
    ).scalar_one()
    assert row.ticket_id == "tk_42"
    assert row.persona_name == "atlas"


@pytest.mark.asyncio
async def test_session_upload_without_ticket_leaves_columns_null(
    client: AsyncClient, db_session: AsyncSession
):
    user, headers = await _make_user(db_session)
    sid = f"ses_{uuid.uuid4().hex[:16]}"
    archive = _tar_with_manifest(_manifest(session_id=sid))
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201)

    row = (
        await db_session.execute(select(Session).where(Session.id == sid))
    ).scalar_one()
    assert row.ticket_id is None
    assert row.persona_name is None


@pytest.mark.asyncio
async def test_session_resync_updates_ticket_columns(
    client: AsyncClient, db_session: AsyncSession
):
    """A re-upload from a machine that's started a different ticket must
    update the columns — manifest is the source of truth."""
    user, headers = await _make_user(db_session)
    sid = f"ses_{uuid.uuid4().hex[:16]}"

    # First upload: tagged as tk_A.
    archive_a = _tar_with_manifest(
        _manifest(session_id=sid, ticket_id="tk_A", persona_name="atlas")
    )
    push_a = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive_a), "application/gzip")},
    )
    etag_a = push_a.json()["etag"]

    # Second upload: same session id but the manifest now references tk_B.
    archive_b = _tar_with_manifest(
        _manifest(session_id=sid, ticket_id="tk_B", persona_name="prism")
    )
    push_b = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers={**headers, "If-Match": f'"{etag_a}"'},
        files={"file": ("s.tar.gz", io.BytesIO(archive_b), "application/gzip")},
    )
    assert push_b.status_code == 200

    db_session.expire_all()
    row = (
        await db_session.execute(select(Session).where(Session.id == sid))
    ).scalar_one()
    assert row.ticket_id == "tk_B"
    assert row.persona_name == "prism"


@pytest.mark.asyncio
async def test_session_upload_truncates_oversized_fields(
    client: AsyncClient, db_session: AsyncSession
):
    """Bundle persona_name is bounded by the API (50 chars) but the
    column is also 50 chars. The metadata extractor must truncate so a
    corrupted manifest cannot violate the schema."""
    user, headers = await _make_user(db_session)
    sid = f"ses_{uuid.uuid4().hex[:16]}"
    archive = _tar_with_manifest(
        _manifest(
            session_id=sid,
            ticket_id="tk_" + ("x" * 200),
            persona_name="p" * 200,
        )
    )
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201)

    row = (
        await db_session.execute(select(Session).where(Session.id == sid))
    ).scalar_one()
    assert row.persona_name is not None
    assert len(row.persona_name) <= 50
    assert row.ticket_id is not None
    assert len(row.ticket_id) <= 64
