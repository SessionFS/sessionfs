"""v0.10.1 Phase 2 — persona CRUD API regression tests.

Covers GET/POST/PUT/DELETE on `/api/v1/projects/{project_id}/personas`.
Project access gate is the same shape as wiki + project_transfers
(owner OR has a session in the project's git remote). Tier gating
on `agent_personas` (Pro+). Soft-delete via is_active=false preserves
the name UNIQUE so duplicate POSTs after a delete still 409 (caller
must PUT is_active=true to reactivate or rename the deactivated row).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    Project,
    User,
)


async def _make_user(
    db: AsyncSession, name: str = "alice", tier: str = "pro"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
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
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_project(
    db: AsyncSession, owner: User, suffix: str = ""
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"phase2-{suffix or uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/p2-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


# ── happy-path CRUD ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persona_returns_201_with_full_body(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={
            "name": "atlas",
            "role": "Backend Architect",
            "content": "# Atlas\n\nBackend persona content.",
            "specializations": ["backend", "api", "cli"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "atlas"
    assert body["role"] == "Backend Architect"
    assert body["content"].startswith("# Atlas")
    assert body["specializations"] == ["backend", "api", "cli"]
    assert body["is_active"] is True
    assert body["version"] == 1
    assert body["created_by"] == user.id


@pytest.mark.asyncio
async def test_list_personas_empty_then_populated(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    # Empty.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key)
    )
    assert resp.status_code == 200
    assert resp.json() == []

    # Populate.
    for name in ("atlas", "prism", "scribe"):
        await client.post(
            f"/api/v1/projects/{project.id}/personas",
            headers=_hdrs(key),
            json={"name": name, "role": "test"},
        )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key)
    )
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert names == ["atlas", "prism", "scribe"]  # ordered alphabetically


@pytest.mark.asyncio
async def test_list_filters_inactive_by_default(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )

    # Default: hidden.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key)
    )
    assert resp.json() == []

    # include_inactive=true: visible with is_active=false.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas?include_inactive=true",
        headers=_hdrs(key),
    )
    body = resp.json()
    assert len(body) == 1
    assert body[0]["is_active"] is False


@pytest.mark.asyncio
async def test_get_one_persona_returns_404_when_inactive(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_updates_fields_and_bumps_version(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create_resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    assert create_resp.json()["version"] == 1

    put_resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(key),
        json={
            "role": "Senior Backend Architect",
            "content": "Updated content.",
            "specializations": ["backend", "perf"],
        },
    )
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["role"] == "Senior Backend Architect"
    assert body["content"] == "Updated content."
    assert body["specializations"] == ["backend", "perf"]
    assert body["version"] == 2


@pytest.mark.asyncio
async def test_put_no_op_does_not_bump_version(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create_resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    initial_version = create_resp.json()["version"]
    put_resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(key),
        json={},  # nothing changes
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["version"] == initial_version


@pytest.mark.asyncio
async def test_put_can_reactivate_soft_deleted_persona(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    # GET 404 on the soft-deleted persona.
    assert (
        await client.get(
            f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
        )
    ).status_code == 404

    # PUT reactivates.
    put_resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(key),
        json={"is_active": True},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["is_active"] is True

    # GET is now 200.
    assert (
        await client.get(
            f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_delete_returns_204_and_soft_deletes(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    # Row remains in the DB with is_active=false.
    row = (
        await db_session.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project.id,
                AgentPersona.name == "atlas",
            )
        )
    ).scalar_one()
    assert row.is_active is False


@pytest.mark.asyncio
async def test_delete_blocked_by_open_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    """KB 339 MEDIUM — delete must refuse when non-terminal tickets
    reference the persona, unless ?force=true is passed."""
    user, key = await _make_user(db_session, tier="team")  # ticket gate needs TEAM+
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "still-open", "assigned_to": "atlas"},
    )
    assert tk.status_code == 201

    # Without force: 409 with friendly count.
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    # Global error handler reshapes detail → error.message.
    message = (resp.json().get("error", {}) or {}).get("message", "")
    assert "atlas" in message
    assert "1" in message  # the count

    # With ?force=true: 204, persona soft-deleted.
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas?force=true",
        headers=_hdrs(key),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_ignores_done_and_cancelled_tickets(
    client: AsyncClient, db_session: AsyncSession
):
    """Terminal-status tickets must NOT block delete — only non-terminal
    (suggested/open/in_progress/blocked/review) statuses do."""
    user, key = await _make_user(db_session, tier="team")
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    # Create a ticket and dismiss it (→ cancelled).
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "cancel-me", "assigned_to": "atlas"},
    )
    tk_id = tk.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/dismiss",
        headers=_hdrs(key),
    )

    # No non-terminal references → delete proceeds without --force.
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    assert resp.status_code == 204


# ── duplicate-name handling ────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_name_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Different"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_duplicate_name_against_soft_deleted_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    """The UNIQUE(project_id, name) constraint covers soft-deleted rows.

    Caller must PUT is_active=true to reactivate, not POST.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key)
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Re-created"},
    )
    assert resp.status_code == 409
    # The error message hints at the soft-delete path. The error
    # envelope varies (some middleware paths reshape `detail` into
    # `error`/`message`); check the raw response text instead.
    assert "soft-deleted" in resp.text.lower()


# ── validation ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "",
        "  ",
        "has space",
        "has/slash",
        "has.dot",
        "a" * 51,
        # Codex Phase 2 Round 1 (KB 322): the earlier validator used
        # `str.isalnum()` which accepts Unicode letters/digits. ASCII
        # regex must reject these — they'd otherwise leak into
        # ticket.assigned_to / CLI argv / MCP prompts / URL paths.
        "åtlås",
        "后端",
        "atlas\u200b",  # zero-width space
        "atlas🤖",
    ],
)
async def test_invalid_persona_name_rejected(
    client: AsyncClient, db_session: AsyncSession, name: str
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": name, "role": "test"},
    )
    assert resp.status_code in (400, 422), f"name={name!r} should be rejected"


# ── tier gating ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_free_tier_gets_403(client: AsyncClient, db_session: AsyncSession):
    """agent_personas feature is Pro+ — free users get 403 on every route."""
    user, key = await _make_user(db_session, "free", tier="free")
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_starter_tier_gets_403(
    client: AsyncClient, db_session: AsyncSession
):
    """Starter tier doesn't include agent_personas either."""
    user, key = await _make_user(db_session, "starter", tier="starter")
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key)
    )
    assert resp.status_code == 403


# ── project access ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_without_sessions_blocked(
    client: AsyncClient, db_session: AsyncSession
):
    """Non-owner without any session in the project's git_remote is blocked.

    Reuses the wiki._get_project_or_404 access shape — same surface every
    project-scoped route in v0.10.x uses.
    """
    owner, _ = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    project = await _make_project(db_session, owner)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(outsider_key)
    )
    assert resp.status_code in (403, 404), resp.text


# ── content round-trip on JSON storage ─────────────────────


@pytest.mark.asyncio
async def test_specializations_handle_unicode_and_empty(
    client: AsyncClient, db_session: AsyncSession
):
    """Specializations is JSON-encoded text — verify unicode + empty list."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={
            "name": "scribe",
            "role": "Documentation",
            "specializations": ["docs", "i18n-spëcial 🌐"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["specializations"] == ["docs", "i18n-spëcial 🌐"]

    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "empty", "role": "no specs"},
    )
    assert resp2.json()["specializations"] == []
