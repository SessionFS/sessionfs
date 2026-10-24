"""GET /api/v1/projects/{id-or-remote} resolves by project id as well as
by normalized git remote, with identical access and tombstone handling."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Project, User


async def _other_user(db: AsyncSession) -> str:
    user = User(
        id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Other",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    raw = generate_api_key()
    db.add(ApiKey(
        id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
        name="other", created_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return raw


async def _create(client: AsyncClient, headers: dict, remote: str) -> dict:
    resp = await client.post(
        "/api/v1/projects/",
        headers=headers,
        json={"name": remote, "git_remote_normalized": remote},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _tombstone(db: AsyncSession, source_id: str, target_id: str) -> None:
    await db.execute(
        update(Project)
        .where(Project.id == source_id)
        .values(merged_into_project_id=target_id, merged_at=datetime.now(timezone.utc))
    )
    await db.commit()


@pytest.mark.asyncio
async def test_get_by_id_matches_get_by_remote(client, auth_headers):
    created = await _create(client, auth_headers, "github.com/acme/byid")
    by_id = await client.get(f"/api/v1/projects/{created['id']}", headers=auth_headers)
    by_remote = await client.get(
        "/api/v1/projects/github.com/acme/byid", headers=auth_headers,
    )
    assert by_id.status_code == 200, by_id.text
    assert by_remote.status_code == 200
    assert by_id.json() == by_remote.json()
    assert by_id.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_get_by_id_unknown_is_404(client, auth_headers):
    resp = await client.get(
        "/api/v1/projects/proj_0123456789abcdef", headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_by_id_without_access_matches_remote_path(client, auth_headers, db_session):
    created = await _create(client, auth_headers, "github.com/acme/private")
    stranger = await _other_user(db_session)
    headers = {"Authorization": f"Bearer {stranger}"}
    by_id = await client.get(f"/api/v1/projects/{created['id']}", headers=headers)
    by_remote = await client.get(
        "/api/v1/projects/github.com/acme/private", headers=headers,
    )
    # Same outcome whichever way the project is addressed.
    assert by_id.status_code == by_remote.status_code
    assert by_id.status_code in (403, 404)
    assert by_id.json() == by_remote.json()


@pytest.mark.asyncio
async def test_get_by_id_tombstone_is_410_with_target(client, auth_headers, db_session):
    source = await _create(client, auth_headers, "github.com/acme/old")
    target = await _create(client, auth_headers, "github.com/acme/new")
    await _tombstone(db_session, source["id"], target["id"])

    resp = await client.get(f"/api/v1/projects/{source['id']}", headers=auth_headers)
    assert resp.status_code == 410
    body = resp.json()
    # The app's error envelope carries the HTTPException detail as `details`.
    details = body["error"]["details"]
    assert details["error"] == "project_merged"
    assert details["merged_into"] == target["id"]


@pytest.mark.asyncio
async def test_get_by_id_tombstone_hidden_from_stranger(client, auth_headers, db_session):
    source = await _create(client, auth_headers, "github.com/acme/old2")
    target = await _create(client, auth_headers, "github.com/acme/new2")
    await _tombstone(db_session, source["id"], target["id"])
    stranger = await _other_user(db_session)
    resp = await client.get(
        f"/api/v1/projects/{source['id']}",
        headers={"Authorization": f"Bearer {stranger}"},
    )
    assert resp.status_code == 404
    assert target["id"] not in resp.text


@pytest.mark.asyncio
async def test_remote_path_unchanged_and_siblings_not_shadowed(client, auth_headers):
    created = await _create(client, auth_headers, "github.com/acme/sib")
    ok = await client.get("/api/v1/projects/github.com/acme/sib", headers=auth_headers)
    assert ok.status_code == 200
    ctx = await client.put(
        "/api/v1/projects/github.com/acme/sib/context",
        headers=auth_headers, json={"context_document": "# hi"},
    )
    assert ctx.status_code == 200, ctx.text
    repos = await client.get(
        f"/api/v1/projects/{created['id']}/repos", headers=auth_headers,
    )
    assert repos.status_code == 200
    renamed = await client.patch(
        f"/api/v1/projects/{created['id']}", headers=auth_headers,
        json={"name": "renamed"},
    )
    assert renamed.status_code == 200, renamed.text
