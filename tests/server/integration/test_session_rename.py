"""Integration tests for session rename (PATCH /sessions/{id}) — tk_cf9f1691091d4e8e.

Covers:
- Empty-title rejection via Pydantic validator (422).
- All-None body rejection (400).
- Title-only edit on free tier (no tier gate, 200).
- Alias edit on free tier (tier-gate 403 — aliases_cloud is Starter+).
- Pro-tier alias edit succeeds (200).
- PUT /alias also tier-gates against aliases_cloud (404-shape regression).
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Session, User


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
async def free_user(db_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"free-{uuid.uuid4().hex[:8]}@example.com",
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def free_headers(db_session: AsyncSession, free_user: User) -> dict[str, str]:
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=free_user.id,
        key_hash=hash_api_key(raw_key),
        name="free-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    return {"Authorization": f"Bearer {raw_key}"}


async def _inject_free_user_session(db_session: AsyncSession, owner: User) -> str:
    """Create a Session row directly (bypasses POST /sessions tier gate)."""
    sid = f"ses_{uuid.uuid4().hex[:24]}"
    s = Session(
        id=sid,
        user_id=owner.id,
        source_tool="claude-code",
        title="Original title",
        alias=None,
        message_count=0,
        total_input_tokens=0,
        total_output_tokens=0,
        tags="[]",
        blob_key=f"sessions/{sid}.tar.gz",
        blob_size_bytes=0,
        etag=f"etag-{sid}",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    await db_session.commit()
    return sid


# -- PATCH: input validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_patch_session_rejects_empty_title(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Empty title is rejected by the Pydantic validator (422)."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={"title": "   "},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_session_rejects_only_html_title(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """A title that becomes empty after HTML-strip + trim is rejected."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={"title": "<b></b>  <i></i>"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_session_rejects_all_none_body(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Empty PATCH body (no title/alias/tags) is rejected with 400."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_patch_session_title_edit_happy_path(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Title-only PATCH succeeds on Pro tier."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={"title": "Renamed session"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Renamed session"


@pytest.mark.asyncio
async def test_patch_session_alias_edit_happy_path(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Alias-only PATCH succeeds on Pro tier."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={"alias": "my-debug-session"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias"] == "my-debug-session"


@pytest.mark.asyncio
async def test_patch_session_combined_title_and_alias(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """Title + alias in one PATCH call both apply."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    sid = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=auth_headers,
        json={"title": "Atomic rename", "alias": "atomic-rename"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Atomic rename"
    assert body["alias"] == "atomic-rename"


# -- PATCH: tier gating on alias -----------------------------------------------


@pytest.mark.asyncio
async def test_patch_session_alias_requires_aliases_cloud(
    client: AsyncClient,
    db_session: AsyncSession,
    free_user: User,
    free_headers: dict,
):
    """Free-tier user is rejected when patching alias (aliases_cloud is Starter+)."""
    sid = await _inject_free_user_session(db_session, free_user)

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=free_headers,
        json={"alias": "free-alias"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_patch_session_title_works_on_free_tier(
    client: AsyncClient,
    db_session: AsyncSession,
    free_user: User,
    free_headers: dict,
):
    """Title-only edit is NOT tier-gated — free tier can rename titles."""
    sid = await _inject_free_user_session(db_session, free_user)

    resp = await client.patch(
        f"/api/v1/sessions/{sid}",
        headers=free_headers,
        json={"title": "Free-tier rename"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Free-tier rename"


@pytest.mark.asyncio
async def test_put_alias_requires_aliases_cloud(
    client: AsyncClient,
    db_session: AsyncSession,
    free_user: User,
    free_headers: dict,
):
    """PUT /alias also gates against aliases_cloud (closes pre-existing hole)."""
    sid = await _inject_free_user_session(db_session, free_user)

    resp = await client.put(
        f"/api/v1/sessions/{sid}/alias",
        headers=free_headers,
        json={"alias": "free-alias"},
    )
    assert resp.status_code == 403, resp.text
